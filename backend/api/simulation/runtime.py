"""Runtime endpoints: /start, /stop, /continue, /resume, /status, agent ctx, logs."""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import uuid

from fastapi import APIRouter, HTTPException

from ...db.database import get_db
from .interview import (
    build_interview_messages,
    effective_token_limit,
    extract_answer,
    resolve_server_id,
    resolve_temperature,
)
from .runner import finalize_run, swap_event_queue
from .schemas import (
    InterviewRecord,
    InterviewRequest,
    SimContinueConfig,
    SimStartConfig,
)
from .state import _sim, _sim_lock, get_sim_db


logger = logging.getLogger(__name__)
router = APIRouter()


def _make_llm(server_id: str | None, temperature: float = 0.7):
    """server_id/temperature를 고정한 동기 LLM 콜러블을 만든다.

    server_id가 None이면 registry의 기본 서버가 선택된다. DB에 사용 가능한
    서버가 없으면 select()가 즉시 RuntimeError를 던지며, 이는 이 함수 호출
    시점이 아니라 반환된 콜러블을 실제로 호출하는 첫 LLM 요청 시점에 발생해
    기존처럼 _run()의 except 절 → finalize_run(error=)로 UI에 표시된다.

    temperature 기본값 0.7은 이 옵션이 생기기 전 bridge에 하드코딩돼 있던 값이다.
    """
    from ABM.config import API_TIMEOUT
    from ...llm.bridge import make_sync_chat
    return make_sync_chat(server_id=server_id, timeout=API_TIMEOUT, temperature=temperature)


def _make_agent_llm_map(cfg: SimStartConfig) -> dict:
    """시뮬레이션 기본값과 다른 LLM 설정을 가진 에이전트만 이름→콜러블로 매핑한다.

    기준이 되는 축은 두 개다.
      - server_id:  에이전트 값이 없으면(None/빈 문자열) 시뮬레이션 기본값
                    (cfg.server_id)을 쓴다.
      - temperature: 에이전트 값이 None이면 시뮬레이션 기본값(cfg.temperature)을 쓴다.

    두 축을 합친 유효 설정이 시뮬레이션 기본값 `(cfg.server_id, cfg.temperature)`와
    같으면 매핑에서 빠지고, 기본 콜러블 `_make_llm(cfg.server_id, cfg.temperature)`을
    그대로 쓴다(ABM 쪽 `self._agent_llm.get(k) or self._llm`). 한 축만 달라도
    매핑에 포함되므로 "서버만 다름"/"온도만 다름"/"둘 다 다름"이 모두 잡힌다.
    구버전 시나리오처럼 필드 자체가 없으면 Pydantic 기본값(None)이 적용돼 제외된다.

    존재하지 않거나 비활성화된 server_id는 시뮬레이션 기본 서버로 되돌린다 —
    registry.select()가 그런 id를 조용히 "registry 전역 기본 서버"로 폴백시키는데,
    이는 시뮬레이션이 설정한 기본 서버(cfg.server_id)와 다를 수 있다(예: 로컬 vLLM
    시나리오인데 삭제된 서버를 가리키던 에이전트가 유료 OpenAI로 새는 경우).
    이때 temperature 오버라이드는 그대로 살아남는다(서버만 기본값으로 되돌림).
    """
    from ...llm.registry import get_registry
    registry = get_registry()
    cache: dict[tuple[str | None, float], object] = {}
    result = {}
    for a in cfg.agents:
        server_id = a.server_id or cfg.server_id
        if a.server_id and registry.get_provider(a.server_id) is None:
            logger.warning(
                "[%s] server_id=%r 를 찾을 수 없습니다(삭제/비활성) — "
                "시뮬레이션 기본 서버로 대신 동작합니다.",
                a.name, a.server_id,
            )
            server_id = cfg.server_id
        temperature = cfg.temperature if a.temperature is None else a.temperature

        if server_id == cfg.server_id and temperature == cfg.temperature:
            continue  # 기본 콜러블과 동일 — 매핑에 넣을 필요 없음
        key = (server_id, temperature)
        if key not in cache:
            cache[key] = _make_llm(server_id, temperature)
        result[a.name] = cache[key]
    return result


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
            # completed_waves를 0으로 되돌리기 전에 감염 경과 앵커를 먼저 옮겨둔다 —
            # 순서를 바꾸면 재기준화 계산(completed_waves 기준)이 이미 0을 보게 된다.
            sim_obj.rebase_infection_anchors()
            sim_obj.completed_waves = 0

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


# ── /load/{run_id} ────────────────────────────────────────────────────────────

@router.post("/load/{run_id}")
def load_simulation(run_id: str):
    """과거 실행 상태를 메모리에 복원하되 실행은 시작하지 않음.

    /resume 과 달리 스레드를 생성하지 않고 status='done'으로 설정해
    프론트엔드에서 '이어서' 버튼으로 계속할 수 있는 상태를 만든다.
    로그 항목도 반환해 피드를 복원할 수 있게 한다.
    """
    with _sim_lock:
        if _sim["status"] == "running":
            raise HTTPException(409, "Simulation already running")
        _sim["status"] = "loading"

    db = get_sim_db()
    run = db.get_run(run_id)
    if run is None:
        with _sim_lock:
            _sim["status"] = "idle"
        raise HTTPException(404, "Run not found")

    config_json = run.get("config_json") or "{}"
    try:
        cfg_dict = json.loads(config_json)
        if not cfg_dict.get("agents"):
            raise ValueError("empty config")
        cfg = SimStartConfig(**cfg_dict)
    except Exception:
        with _sim_lock:
            _sim["status"] = "idle"
        raise HTTPException(400, "이 실행은 설정 스냅샷이 없어 불러올 수 없습니다")

    snapshots     = db.get_agent_snapshots(run_id)
    saved_states  = db.get_agent_states(run_id)
    active_json   = run.get("active_agents_json")
    pending_json  = run.get("pending_wave_json")
    saved_active  = set(json.loads(active_json)) if active_json  else None
    saved_pending = json.loads(pending_json)      if pending_json else None
    log_entries   = db.get_run_log(run_id)

    try:
        from ABM.agent import Agent
        from ABM.simulation import Simulation
        from ABM.db import SimDB
        from ABM.config import LOG_DIR
        from ABM.memory_compressor import build_memory_block

        llm       = _make_llm(cfg.server_id, cfg.temperature)
        agent_llm = _make_agent_llm_map(cfg)
        alias_map    = {a.display_name: a.name for a in cfg.agents if a.display_name.strip()}
        key_to_alias = {v: k for k, v in alias_map.items()}

        agents = {}
        for a in cfg.agents:
            agent = Agent(
                a.name, a.system_prompt, LOG_DIR,
                token_limit=cfg.token_limit,
                extra_fields=[f.model_dump() for f in cfg.extra_fields],
                output_format_template=cfg.output_format_template or None,
            )
            if a.name in snapshots:
                agent.memory = list(snapshots[a.name])
            block = build_memory_block(run_id, a.name, db, key_to_alias=key_to_alias)
            if block:
                agent._memory_block = block
            agents[a.name] = agent

        background_log  = [{"role": "user", "content": f"[배경] {cfg.background}"}]
        init_agents     = list(saved_active) if saved_active is not None else None
        agent_groups    = {a.name: a.groups            for a in cfg.agents}
        agent_locations = {a.name: a.location          for a in cfg.agents}
        agent_visuals   = {a.name: a.visual_description for a in cfg.agents}

        sim_obj = Simulation(
            agents, background_log, LOG_DIR,
            llm=llm,
            initial_agents=init_agents,
            name_aliases=alias_map,
            db=SimDB(os.path.join(LOG_DIR, "simulation.db")),
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
            elapsed_minutes_init=run.get("elapsed_minutes") or 0,
        )
        # 저장된 런타임 상태(이동한 위치, 바뀐 외모, 인지관계)를 시나리오 초기값 위에 덮어씀.
        # 저장된 상태가 없는 구버전 실행은 위 초기값을 그대로 유지한다.
        sim_obj.restore_agent_state(saved_states)
        if saved_pending:
            sim_obj._pending_wave = saved_pending

        # shared_log을 DB 로그로 재구성 (피드 복원용)
        shared_log = [
            {
                "speaker":     e["speaker"],
                "content":     e["content"],
                "meta":        e.get("meta", {}),
                "targets":     e.get("targets", []),
                "timestamp":   e.get("timestamp", 0),
                "action_note": e.get("action_note", ""),
                "wave":        e.get("wave", 0),
            }
            for e in log_entries
        ]
        # sim_obj.shared_log에도 복원 — /logs 엔드포인트가 sim_obj 우선으로 읽기 때문
        sim_obj.shared_log.extend(shared_log)

        with _sim_lock:
            _sim["agents"]         = sim_obj.agents
            _sim["background_log"] = sim_obj.background_log
            _sim["sim_obj"]        = sim_obj
            _sim["scenario_id"]    = run.get("scenario_id")
            _sim["scenario_name"]  = run.get("scenario_name")
            _sim["config_json"]    = config_json
            _sim["shared_log"]     = shared_log
            _sim["edges"]          = []
            _sim["status"]         = "done"

        # 감염 상태는 이벤트 재생만으로는 복원할 수 없다 — 이 run 안에서 한 번도
        # 상태가 안 바뀐 에이전트는 infection_update 이벤트가 0건이라 프론트가 알
        # 방법이 없다(특히 /continue로 만들어진 run은 새 run_id를 쓰므로 이전 run의
        # 전이 이벤트를 안 물려받는다). 서버가 들고 있는 현재 상태를 그대로 실어준다.
        infection_snapshot = {
            key: {
                "status": entry.get("status", "S"),
                "cause":  "recovery" if entry.get("recovered_wave") is not None else "transmission",
            }
            for key, entry in sim_obj._agent_infection.items()
        }

        return {"status": "loaded", "log": log_entries, "infection": infection_snapshot}

    except Exception as e:
        with _sim_lock:
            _sim["status"] = "idle"
        raise HTTPException(500, str(e))


# ── /resume/{run_id} ──────────────────────────────────────────────────────────

@router.post("/resume/{run_id}")
def resume_simulation(run_id: str):
    """과거 실행 상태를 복원해 이어서 실행."""
    with _sim_lock:
        if _sim["status"] == "running":
            raise HTTPException(409, "Simulation already running")
        _sim["status"] = "running"

    db = get_sim_db()
    run = db.get_run(run_id)
    if run is None:
        # Roll back the optimistic status flip before raising.
        with _sim_lock:
            _sim["status"] = "idle"
        raise HTTPException(404, "Run not found")

    config_json = run.get("config_json") or "{}"
    try:
        cfg_dict = json.loads(config_json)
        if not cfg_dict.get("agents"):
            raise ValueError("empty config")
        cfg = SimStartConfig(**cfg_dict)
    except Exception:
        with _sim_lock:
            _sim["status"] = "idle"
        raise HTTPException(400, "이 실행은 설정 스냅샷이 없어 재개할 수 없습니다")

    snapshots        = db.get_agent_snapshots(run_id)
    saved_states     = db.get_agent_states(run_id)
    active_json      = run.get("active_agents_json")
    pending_json     = run.get("pending_wave_json")
    saved_active     = set(json.loads(active_json))  if active_json  else None
    saved_pending    = json.loads(pending_json)       if pending_json else None

    eq      = queue.Queue()
    stop_ev = threading.Event()
    swap_event_queue(eq, stop_ev,
                     shared_log=[], edges=[], sim_obj=None)

    def _run():
        new_db     = None
        run_sim_id = None
        sim = None
        try:
            from ABM.agent import Agent
            from ABM.simulation import Simulation
            from ABM.db import SimDB
            from ABM.config import LOG_DIR
            from ABM.memory_compressor import build_memory_block

            llm       = _make_llm(cfg.server_id, cfg.temperature)
            agent_llm = _make_agent_llm_map(cfg)
            run_sim_id    = str(uuid.uuid4())
            new_db        = SimDB(os.path.join(LOG_DIR, "simulation.db"))
            scenario_name = run.get("scenario_name")
            new_db.create_run(run_sim_id, run.get("scenario_id"), scenario_name, config_json)

            alias_map    = {a.display_name: a.name for a in cfg.agents if a.display_name.strip()}
            key_to_alias = {v: k for k, v in alias_map.items()}

            agents = {}
            for a in cfg.agents:
                agent = Agent(
                    a.name, a.system_prompt, LOG_DIR,
                    token_limit=cfg.token_limit,
                    extra_fields=[f.model_dump() for f in cfg.extra_fields],
                    output_format_template=cfg.output_format_template or None,
                )
                if a.name in snapshots:
                    agent.memory = list(snapshots[a.name])
                block = build_memory_block(run_id, a.name, db, key_to_alias=key_to_alias)
                if block:
                    agent._memory_block = block
                agents[a.name] = agent

            background_log  = [{"role": "user", "content": f"[배경] {cfg.background}"}]
            init_agents     = list(saved_active) if saved_active is not None else None

            agent_groups    = {a.name: a.groups            for a in cfg.agents}
            agent_locations = {a.name: a.location          for a in cfg.agents}
            agent_visuals   = {a.name: a.visual_description for a in cfg.agents}

            sim = Simulation(
                agents, background_log, LOG_DIR,
                llm=llm,
                event_queue=eq, stop_event=stop_ev,
                initial_agents=init_agents,
                name_aliases=alias_map,
                sim_id=run_sim_id, db=new_db,
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
                elapsed_minutes_init=run.get("elapsed_minutes") or 0,
            )
            # 저장된 런타임 상태(이동한 위치, 바뀐 외모, 인지관계)를 시나리오 초기값 위에 덮어씀.
            # 저장된 상태가 없는 구버전 실행은 위 초기값을 그대로 유지한다.
            sim.restore_agent_state(saved_states)
            _sim["agents"]         = sim.agents
            _sim["background_log"] = sim.background_log
            _sim["sim_obj"]        = sim
            _sim["scenario_id"]    = run.get("scenario_id")
            _sim["scenario_name"]  = scenario_name
            _sim["config_json"]    = config_json

            # B9 fix: do NOT replay the original scenario events on resume.
            # Resume picks up from ``saved_pending`` (the wave the previous
            # run was paused at), so any events scheduled at earlier waves
            # already fired. Passing them again would re-inject system
            # messages and re-toggle agent enter/exit states.
            sim.run(
                cfg.start_agent,
                max_waves=cfg.max_waves,
                step_delay=cfg.step_delay,
                events=[],
                resume_wave=saved_pending,
                target_duration_minutes=cfg.target_duration_minutes,
            )
            finalize_run(new_db, run_sim_id, stop_ev, sim, eq)
        except Exception as e:
            finalize_run(new_db, run_sim_id, stop_ev, sim, eq, error=e)

    t = threading.Thread(target=_run, daemon=True)
    _sim["thread"] = t
    t.start()
    return {"status": "resuming"}


# ── status / inspection ───────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    return {
        "status":     _sim["status"],
        "log_count":  len(_sim["shared_log"]),
        "edge_count": len(_sim["edges"]),
    }


@router.get("/agents/{name}/context")
def get_agent_context(name: str):
    agents = _sim.get("agents", {})
    bg_log = _sim.get("background_log", [])
    if name not in agents:
        raise HTTPException(404, f"Agent '{name}' not found")
    agent = agents[name]
    sim_obj = _sim.get("sim_obj")
    if sim_obj is not None and name in sim_obj.agents:
        # 프롬프트 조립 규칙은 실행 경로(ABM/simulation/step.py::_step_agent)와
        # 반드시 같아야 한다 — 예전엔 여기서 규칙을 따로 복제해 [현재 상황]/[현재 시각]
        # 블록이 빠지고 <TARGETS>에 다른 위치의 에이전트까지 노출됐다.
        # _assemble_agent_prompt()가 그 단일 진실 원천이며 부작용(이벤트 emit)이 없다.
        ctx               = sim_obj._assemble_agent_prompt(name)
        visible_names     = ctx["visible_agents"]
        key_to_alias      = ctx["key_to_alias"]
        target_sections   = ctx["target_sections"]
        location_name     = ctx["location_name"]
        situation_targets = ctx["situation_targets"]
        ephemeral_msgs    = ctx["ephemeral_msgs"]
    else:
        visible_names     = [k for k in agents if k != name]
        key_to_alias      = {}
        target_sections   = None
        location_name     = ""
        situation_targets = False
        ephemeral_msgs    = None
    messages   = agent.build_messages(
        bg_log, visible_names, key_to_alias, target_sections,
        location_name, situation_targets, ephemeral_msgs,
    )
    est_tokens = agent.estimate_context_tokens(
        bg_log, visible_names, key_to_alias, target_sections,
        location_name, situation_targets, ephemeral_msgs,
    )
    prompt_tokens = agent._last_prompt_tokens if agent._last_prompt_tokens is not None else est_tokens
    return {
        "name":           name,
        "memory_size":    len(agent.memory),
        "trimmed":        agent._trimmed_count,
        "prompt_tokens":  prompt_tokens,
        "est_tokens":     est_tokens,
        "token_limit":    agent._token_limit,
        "messages":       messages,
    }


@router.get("/agents/{name}/memory")
def get_agent_memory(name: str):
    """Return the agent's structured DB memory (episodes, facts, relationships, self_state)."""
    sim_obj = _sim.get("sim_obj")
    if sim_obj is None or sim_obj._db is None or sim_obj._sim_id is None:
        raise HTTPException(404, "No active simulation with memory DB")
    if name not in sim_obj.agents:
        raise HTTPException(404, f"Agent '{name}' not found")
    return sim_obj._db.get_full_memory(sim_obj._sim_id, name)


# ── 사후 인터뷰 (post-simulation interview) ───────────────────────────────────
#
# 인터뷰는 끝난 run 을 대상으로 하는 읽기 전용 회고다. 여기서 오간 발화는
# `interview_log` 에만 저장되며 `simulation_log` 나 에이전트 워킹 메모리에는
# 절대 들어가지 않는다 — /runs/{id}/log, /load, /resume 결과가 오염되지 않도록.

# 인터뷰를 허용할 run 상태. 'running' 은 아직 agent_snapshots 가 기록되지 않아
# (finalize_run 시점에 저장) 기억 상태를 신뢰할 수 없으므로 제외한다.
_INTERVIEWABLE_STATUS = ("done", "stopped", "error")


def _parse_run_config(run: dict) -> SimStartConfig:
    """run 의 config 스냅샷을 SimStartConfig 로 복원. 스냅샷이 없으면 400."""
    try:
        cfg_dict = json.loads(run.get("config_json") or "{}")
        if not cfg_dict.get("agents"):
            raise ValueError("empty config")
        return SimStartConfig(**cfg_dict)
    except Exception:
        raise HTTPException(400, "이 실행은 설정 스냅샷이 없어 인터뷰할 수 없습니다")


@router.post("/runs/{run_id}/agents/{name}/interview", response_model=InterviewRecord)
def create_agent_interview(run_id: str, name: str, body: InterviewRequest):
    """끝난 run 의 에이전트에게 질문하고 답변을 interview_log 에 저장한다."""
    db  = get_sim_db()
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    status = run.get("status") or ""
    if status not in _INTERVIEWABLE_STATUS:
        raise HTTPException(
            409,
            f"인터뷰는 종료된 시뮬레이션에서만 가능합니다 (현재 상태: {status or 'unknown'})",
        )

    cfg = _parse_run_config(run)
    if name not in {a.name for a in cfg.agents}:
        raise HTTPException(404, f"Agent '{name}' not found in run {run_id}")

    # 프롬프트 상한은 run 설정 token_limit, 단 모델 컨텍스트를 아는 경우 답변
    # max_tokens 만큼 더 조인다. 컨텍스트 초과 400 → 502 → 무의미한 재시도 방지.
    server_id   = resolve_server_id(cfg, name)
    # 인터뷰는 자체 온도를 갖지 않고 실행 당시 설정(에이전트 → 실행 기본값)을 그대로 쓴다.
    temperature = resolve_temperature(cfg, name)
    max_tokens = body.max_tokens or cfg.llm_max_tokens
    token_limit = effective_token_limit(cfg, server_id, max_tokens)

    try:
        messages = build_interview_messages(
            run_id, name, body.question, body.mode, cfg, db, token_limit=token_limit,
        )
    except KeyError:
        raise HTTPException(404, f"Agent '{name}' not found in run {run_id}")
    except Exception as e:
        logger.exception("인터뷰 컨텍스트 조립 실패 (run=%s, agent=%s)", run_id, name)
        raise HTTPException(500, f"인터뷰 컨텍스트 조립 실패: {e}")

    from ABM.agent import _estimate_tokens
    est_tokens = sum(_estimate_tokens(m.get("content", "")) + 4 for m in messages)
    if est_tokens > token_limit:
        # 기록·기억을 다 잘라내고도 넘친다 = 페르소나/배경 자체가 한도를 넘는다.
        # 재시도해도 결과가 같으므로 재시도 가능한 502 가 아니라 400 으로 알린다.
        logger.warning(
            "인터뷰 컨텍스트가 한도 초과 (run=%s, agent=%s, est=%d > %d)",
            run_id, name, est_tokens, token_limit,
        )
        raise HTTPException(
            400,
            f"인터뷰 컨텍스트가 모델 한도를 넘습니다 (추정 {est_tokens:,} > {token_limit:,} 토큰). "
            "에이전트 설정의 페르소나/배경이 너무 길어 재시도해도 결과가 같습니다.",
        )

    llm = _make_llm(server_id, temperature)
    try:
        content, _reasoning, usage = llm(messages, max_tokens=max_tokens)
    except Exception as e:
        logger.warning("인터뷰 LLM 호출 실패 (run=%s, agent=%s): %s", run_id, name, e)
        raise HTTPException(502, f"LLM 호출 실패: {e}")

    answer = extract_answer(content)
    if not answer:
        raise HTTPException(502, "LLM이 빈 답변을 반환했습니다")

    try:
        return db.log_interview(
            run_id, name, body.mode, body.question, answer,
            meta={
                "est_prompt_tokens": est_tokens,
                "token_limit":       token_limit,
                "context_messages":  len(messages),
                "server_id":         server_id,
                "temperature":       temperature,
                "usage":             usage or {},
            },
        )
    except sqlite3.IntegrityError:
        # interview_log.run_id 의 FK 위반 — LLM 호출 중에 run 이 삭제된 경우.
        raise HTTPException(404, "Run not found")


@router.get("/runs/{run_id}/agents/{name}/interview", response_model=list[InterviewRecord])
def list_agent_interviews(run_id: str, name: str):
    """해당 run/agent 의 인터뷰 기록을 오래된 순으로 반환."""
    db = get_sim_db()
    if db.get_run(run_id) is None:
        raise HTTPException(404, "Run not found")
    return db.get_interviews(run_id, name)


@router.get("/logs")
def get_logs():
    sim_obj = _sim.get("sim_obj")
    log = sim_obj.shared_log if sim_obj is not None else _sim["shared_log"]
    # background_log 항목(speaker 없음)은 마크다운 내보내기용 로그에서 제외
    return [e for e in log if "speaker" in e]


@router.get("/events")
def get_sim_events(types: str = ""):
    """저장된 SSE 이벤트 반환. types=agent_move,world_event 형태로 필터 가능."""
    sim_obj = _sim.get("sim_obj")
    if sim_obj is None or sim_obj._db is None or sim_obj._sim_id is None:
        return []
    filter_types = [t.strip() for t in types.split(",") if t.strip()] if types else None
    return sim_obj._db.get_run_events(sim_obj._sim_id, filter_types)


@router.get("/runs/{run_id}/events")
def get_run_events(run_id: str, types: str = ""):
    """과거 실행의 SSE 이벤트 반환."""
    db = get_sim_db()
    filter_types = [t.strip() for t in types.split(",") if t.strip()] if types else None
    return db.get_run_events(run_id, filter_types)


@router.get("/edges")
def get_edges():
    return _sim["edges"]
