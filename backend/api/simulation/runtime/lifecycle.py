"""실행 라이프사이클 엔드포인트: /start, /stop, /continue.

과거 실행을 되살리는 /load · /resume 은 ``load.py`` · ``resume.py`` 에 있다.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import uuid

from fastapi import APIRouter, HTTPException

from ....db.database import get_db
from ..runner import fold_elapsed_and_reset_waves, finalize_run, swap_event_queue
from ..schemas import SimContinueConfig, SimStartConfig
from ..state import _sim, _sim_lock
from .llm_config import _make_agent_llm_map, _make_llm


router = APIRouter()


def _lookup_scenario_name(scenario_id: str | None) -> str | None:
    """채팅 DB에서 시나리오 표시 이름을 찾는다. 없거나 조회 실패면 None."""
    if not scenario_id:
        return None
    try:
        chat_conn = get_db()
        row = chat_conn.execute(
            "SELECT name FROM simulation_scenarios WHERE id=?",
            (scenario_id,),
        ).fetchone()
        chat_conn.close()
        return row["name"] if row else None
    except Exception:
        return None


# ── /start ────────────────────────────────────────────────────────────────────

@router.post("/start")
def start_simulation(cfg: SimStartConfig):
    # Atomic check-and-set so two concurrent /start requests can't both pass.
    with _sim_lock:
        if _sim["status"] == "running":
            raise HTTPException(409, "Simulation already running")
        _sim["status"] = "running"
        _sim["shared_log"] = []
        _sim["edges"] = []
        _sim["sim_obj"] = None

    eq      = queue.Queue()
    stop_ev = threading.Event()
    # Install new queue and send sentinel to any old SSE consumer.
    swap_event_queue(eq, stop_ev)

    def _run():
        # Defensive defaults so the except branch can never NameError on
        # `db` / `run_sim_id` even if the very first imports fail.
        db = None
        run_sim_id = None
        sim = None
        try:
            from ABM.simulation.headless import run_config
            from ABM.db import SimDB
            from ABM.config import LOG_DIR

            llm       = _make_llm(cfg.server_id, cfg.temperature)
            agent_llm = _make_agent_llm_map(cfg)

            run_sim_id    = str(uuid.uuid4())
            db            = SimDB(os.path.join(LOG_DIR, "simulation.db"))
            scenario_name = _lookup_scenario_name(cfg.scenario_id)
            config_json   = cfg.model_dump_json()
            db.create_run(run_sim_id, cfg.scenario_id, scenario_name, config_json)

            def _on_sim_ready(sim_obj):
                # 조립 직후 · run() 시작 전에 전역을 채운다. 예전 인라인 코드와 같은
                # 자리라 /status·/logs·컨텍스트 조회가 실행 중에 그대로 동작한다.
                nonlocal sim
                sim = sim_obj
                _sim["agents"]         = sim_obj.agents
                _sim["background_log"] = sim_obj.background_log
                _sim["sim_obj"]        = sim_obj
                _sim["scenario_id"]    = cfg.scenario_id
                _sim["scenario_name"]  = scenario_name
                _sim["config_json"]    = config_json

            run_config(
                cfg,
                llm=llm,
                agent_llm=agent_llm,
                log_dir=LOG_DIR,
                db=db,
                sim_id=run_sim_id,
                event_queue=eq,
                stop_event=stop_ev,
                on_sim_ready=_on_sim_ready,
            )
            finalize_run(db, run_sim_id, stop_ev, sim, eq)
        except Exception as e:
            finalize_run(db, run_sim_id, stop_ev, sim, eq, error=e)

    t = threading.Thread(target=_run, daemon=True)
    _sim["thread"] = t
    t.start()
    return {"status": "started"}


# ── /stop ─────────────────────────────────────────────────────────────────────

@router.post("/stop")
def stop_simulation():
    ev = _sim.get("stop_event")
    if ev:
        ev.set()
    with _sim_lock:
        _sim["status"] = "stopped"
    return {"status": "stopping"}


# ── /continue ─────────────────────────────────────────────────────────────────

@router.post("/continue")
def continue_simulation(cfg: SimContinueConfig):
    with _sim_lock:
        if _sim["status"] not in ("done", "stopped"):
            raise HTTPException(409, "이어서 실행은 완료 또는 중지된 시뮬레이션에서만 가능합니다")
        sim_obj = _sim.get("sim_obj")
        if sim_obj is None:
            raise HTTPException(409, "이어서 실행할 시뮬레이션 상태가 없습니다")
        _sim["status"] = "running"

    eq      = queue.Queue()
    stop_ev = threading.Event()
    swap_event_queue(eq, stop_ev)

    def _run():
        db = sim_obj._db
        run_sim_id = None
        try:
            run_sim_id    = str(uuid.uuid4())
            scenario_id   = _sim.get("scenario_id")
            scenario_name = _sim.get("scenario_name")

            sim_obj._event_queue    = eq
            sim_obj._stop_event     = stop_ev
            sim_obj._sim_id         = run_sim_id
            # 이번까지의 총 경과를 _elapsed_minutes로 접고 completed_waves를 0으로
            # 되돌린다 — 접기를 빠뜨리면 fixed 모드에서 에이전트가 보는 [현재 시각]이
            # 시나리오 시작 시각으로 되감긴다. 순서 의존이 있어 헬퍼로 묶어뒀다.
            fold_elapsed_and_reset_waves(sim_obj)

            config_json = _sim.get("config_json") or "{}"

            # 3-1: 원래 실행의 조기종료 설정을 복원해 run()에 다시 넘긴다.
            # /continue 요청 본문(SimContinueConfig)에는 이 두 필드가 없어서,
            # 이어서 실행하면 항상 기본값으로 돌던 버그가 있었다.
            max_silence_waves  = 3
            early_stop_enabled = True
            try:
                _start_cfg = SimStartConfig(**json.loads(config_json))
                max_silence_waves  = _start_cfg.max_silence_waves
                early_stop_enabled = _start_cfg.early_stop_enabled
            except Exception:
                pass  # 스냅샷이 없거나 파싱 실패 → 방어적으로 기본값 유지

            if db is not None:
                # fold 이후라 _wave_base 가 누적값(직전 run 들의 wave 합).
                db.create_run(run_sim_id, scenario_id, scenario_name, config_json,
                              start_wave=getattr(sim_obj, "_wave_base", 0))

            # Use pending wave (agents targeted last but not yet responded) if available,
            # otherwise start fresh from cfg.start_agent.
            pending = getattr(sim_obj, "_pending_wave", None) or None
            # B9와 동일한 이유로 이벤트를 재생하지 않는다: continue는 wave 0부터 다시
            # 세므로, 원래 시나리오의 wave 3 이벤트(예: infect_agent 시드)가 이어서
            # 실행할 때마다 다시 발동한다. SIR에서는 이미 감염/면역이라 무해하지만,
            # SIS(재감염 가능) 모드에서는 회복해 S로 돌아간 환자 0번이 이어서 실행할
            # 때마다 계속 재시드된다.
            sim_obj.run(
                cfg.start_agent,
                max_waves=cfg.max_waves,
                step_delay=cfg.step_delay,
                events=[],
                resume_wave=pending,
                max_silence_waves=max_silence_waves,
                early_stop_enabled=early_stop_enabled,
                target_duration_minutes=cfg.target_duration_minutes,
            )
            finalize_run(db, run_sim_id, stop_ev, sim_obj, eq)
        except Exception as e:
            finalize_run(db, run_sim_id, stop_ev, sim_obj, eq, error=e)

    t = threading.Thread(target=_run, daemon=True)
    _sim["thread"] = t
    t.start()
    return {"status": "continuing"}
