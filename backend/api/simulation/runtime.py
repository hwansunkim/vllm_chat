"""Runtime endpoints: /start, /stop, /continue, /resume, /status, agent ctx, logs."""
from __future__ import annotations

import json
import os
import queue
import threading
import uuid

from fastapi import APIRouter, HTTPException

from ...db.database import get_db
from .runner import finalize_run, swap_event_queue
from .schemas import SimContinueConfig, SimStartConfig
from .state import _sim, _sim_lock, get_sim_db


router = APIRouter()


def _make_llm(server_id: str | None):
    """server_id로 provider를 고정한 동기 LLM 콜러블을 만든다.

    server_id가 None이면 registry의 기본 서버가 선택된다. DB에 사용 가능한
    서버가 없으면 select()가 즉시 RuntimeError를 던지며, 이는 이 함수 호출
    시점이 아니라 반환된 콜러블을 실제로 호출하는 첫 LLM 요청 시점에 발생해
    기존처럼 _run()의 except 절 → finalize_run(error=)로 UI에 표시된다.
    """
    from ABM.config import API_TIMEOUT
    from ...llm.bridge import make_sync_chat
    return make_sync_chat(server_id=server_id, timeout=API_TIMEOUT)



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

            llm = _make_llm(cfg.server_id)

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
                location_graph=[{"name": n.name, "connects_to": n.connects_to, "is_exterior": n.is_exterior} for n in cfg.location_graph],
                lang_fix_enabled=cfg.lang_fix_enabled,
                lang_fix_retries=cfg.lang_fix_retries,
                llm_max_tokens=cfg.llm_max_tokens,
                sim_start_time=cfg.sim_start_time,
                sim_start_weekday=cfg.sim_start_weekday,
                time_per_wave=cfg.time_per_wave,
                time_mode=cfg.time_mode,
                time_categories=[c.model_dump() for c in cfg.time_categories],
                idle_minutes_schedule=cfg.idle_minutes_schedule,
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
            sim_obj.completed_waves = 0

            if db is not None:
                config_json = _sim.get("config_json") or "{}"
                db.create_run(run_sim_id, scenario_id, scenario_name, config_json)

            # Use pending wave (agents targeted last but not yet responded) if available,
            # otherwise start fresh from cfg.start_agent.
            pending = getattr(sim_obj, "_pending_wave", None) or None
            sim_obj.run(
                cfg.start_agent,
                max_waves=cfg.max_waves,
                step_delay=cfg.step_delay,
                events=[e.model_dump() for e in cfg.events],
                resume_wave=pending,
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

        llm = _make_llm(cfg.server_id)
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
            location_graph=[{"name": n.name, "connects_to": n.connects_to, "is_exterior": n.is_exterior} for n in cfg.location_graph],
            lang_fix_enabled=cfg.lang_fix_enabled,
            lang_fix_retries=cfg.lang_fix_retries,
            llm_max_tokens=cfg.llm_max_tokens,
            sim_start_time=cfg.sim_start_time,
            sim_start_weekday=cfg.sim_start_weekday,
            time_per_wave=cfg.time_per_wave,
            time_mode=cfg.time_mode,
            time_categories=[c.model_dump() for c in cfg.time_categories],
            idle_minutes_schedule=cfg.idle_minutes_schedule,
            elapsed_minutes_init=run.get("elapsed_minutes") or 0,
        )
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

        return {"status": "loaded", "log": log_entries}

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

            llm = _make_llm(cfg.server_id)
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
                location_graph=[{"name": n.name, "connects_to": n.connects_to, "is_exterior": n.is_exterior} for n in cfg.location_graph],
                lang_fix_enabled=cfg.lang_fix_enabled,
                lang_fix_retries=cfg.lang_fix_retries,
                llm_max_tokens=cfg.llm_max_tokens,
                sim_start_time=cfg.sim_start_time,
                sim_start_weekday=cfg.sim_start_weekday,
                time_per_wave=cfg.time_per_wave,
                time_mode=cfg.time_mode,
                time_categories=[c.model_dump() for c in cfg.time_categories],
                idle_minutes_schedule=cfg.idle_minutes_schedule,
                elapsed_minutes_init=run.get("elapsed_minutes") or 0,
            )
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
    if sim_obj is not None:
        all_others      = [k for k in sim_obj.active_agents if k != name]
        visible_names   = [k for k in sim_obj._visible_targets.get(name, all_others)
                           if k in sim_obj.active_agents]
        key_to_alias    = sim_obj._key_to_alias
        target_sections = sim_obj._get_visible_sections(name, visible_names)
    else:
        visible_names   = [k for k in agents if k != name]
        key_to_alias    = {}
        target_sections = None
    messages      = agent.build_messages(bg_log, visible_names, key_to_alias, target_sections)
    est_tokens    = agent.estimate_context_tokens(bg_log, visible_names, key_to_alias, target_sections)
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
