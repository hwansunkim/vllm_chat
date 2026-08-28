"""실행 라이프사이클 엔드포인트: /start, /stop, /continue.

과거 실행을 되살리는 /load · /resume 은 ``load.py`` · ``resume.py`` 에 있다.
"""
from __future__ import annotations

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
            from ABM.agent import Agent
            from ABM.simulation import Simulation
            from ABM.db import SimDB
            from ABM.config import LOG_DIR

            llm       = _make_llm(cfg.server_id, cfg.temperature)
            agent_llm = _make_agent_llm_map(cfg)

            run_sim_id    = str(uuid.uuid4())
            db            = SimDB(os.path.join(LOG_DIR, "simulation.db"))
            scenario_name = None
            if cfg.scenario_id:
                try:
                    chat_conn = get_db()
                    row = chat_conn.execute(
                        "SELECT name FROM simulation_scenarios WHERE id=?",
                        (cfg.scenario_id,),
                    ).fetchone()
                    chat_conn.close()
                    if row:
                        scenario_name = row["name"]
                except Exception:
                    pass
            db.create_run(run_sim_id, cfg.scenario_id, scenario_name, cfg.model_dump_json())

            agents = {
                a.name: Agent(a.name, a.system_prompt, LOG_DIR,
                              token_limit=cfg.token_limit,
                              extra_fields=[f.model_dump() for f in cfg.extra_fields],
                              output_format_template=cfg.output_format_template or None)
                for a in cfg.agents
            }
            background_log = [{"role": "user", "content": f"[배경] {cfg.background}"}]

            # initial_active 설정 — False인 에이전트는 비활성으로 시작
            initial_agents = [a.name for a in cfg.agents if a.initial_active]
            init_param = None if len(initial_agents) == len(cfg.agents) else initial_agents

            # display_name → key 매핑 (한국어 이름을 target으로 사용해도 올바른 키로 resolve)
            alias_map = {a.display_name: a.name for a in cfg.agents if a.display_name.strip()}
            # 그룹 가시성 맵 — groups 빈 에이전트는 전체 노출 (하위 호환)
            agent_groups    = {a.name: a.groups            for a in cfg.agents}
            agent_locations = {a.name: a.location          for a in cfg.agents}
            agent_visuals   = {a.name: a.visual_description for a in cfg.agents}

            sim = Simulation(
                agents, background_log, LOG_DIR,
                llm=llm,
                event_queue=eq,
                stop_event=stop_ev,
                initial_agents=init_param,
                name_aliases=alias_map,
                sim_id=run_sim_id,
                db=db,
                agent_groups=agent_groups,
                summary_interval=cfg.summary_interval,
                system_agent=cfg.system_agent.model_dump(),
                agent_locations=agent_locations,
                agent_visuals=agent_visuals,
                agent_llm=agent_llm,
                location_graph=[{"name": n.name, "connects_to": n.connects_to, "is_exterior": n.is_exterior, "zone": n.zone} for n in cfg.location_graph],
                lang_fix_enabled=cfg.lang_fix_enabled,
                lang_fix_retries=cfg.lang_fix_retries,
                llm_max_tokens=cfg.llm_max_tokens,
                sim_start_time=cfg.sim_start_time,
                sim_start_weekday=cfg.sim_start_weekday,
                time_per_wave=cfg.time_per_wave,
                time_mode=cfg.time_mode,
                time_categories=[c.model_dump() for c in cfg.time_categories],
                idle_minutes_schedule=cfg.idle_minutes_schedule,
                infection_model=cfg.infection_model.model_dump(),
            )
            _sim["agents"]         = sim.agents
            _sim["background_log"] = sim.background_log
            _sim["sim_obj"]        = sim
            _sim["scenario_id"]    = cfg.scenario_id
            _sim["scenario_name"]  = scenario_name
            _sim["config_json"]    = cfg.model_dump_json()
            sim.run(
                cfg.start_agent,
                max_waves=cfg.max_waves,
                step_delay=cfg.step_delay,
                events=[e.model_dump() for e in cfg.events],
                max_silence_waves=cfg.max_silence_waves,
                early_stop_enabled=cfg.early_stop_enabled,
                target_duration_minutes=cfg.target_duration_minutes,
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

            if db is not None:
                config_json = _sim.get("config_json") or "{}"
                db.create_run(run_sim_id, scenario_id, scenario_name, config_json)

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
                target_duration_minutes=cfg.target_duration_minutes,
            )
            finalize_run(db, run_sim_id, stop_ev, sim_obj, eq)
        except Exception as e:
            finalize_run(db, run_sim_id, stop_ev, sim_obj, eq, error=e)

    t = threading.Thread(target=_run, daemon=True)
    _sim["thread"] = t
    t.start()
    return {"status": "continuing"}
