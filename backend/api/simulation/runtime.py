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
            from ABM.config import MODEL, BASE_URL, API_TIMEOUT, LOG_DIR

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

            sim = Simulation(
                agents, background_log, LOG_DIR,
                MODEL, BASE_URL, API_TIMEOUT,
                event_queue=eq,
                stop_event=stop_ev,
                initial_agents=init_param,
                name_aliases=alias_map,
                sim_id=run_sim_id,
                db=db,
            )
            _sim["agents"]         = sim.agents
            _sim["background_log"] = sim.background_log
            _sim["sim_obj"]        = sim
            _sim["scenario_id"]    = cfg.scenario_id
            _sim["scenario_name"]  = scenario_name
            sim.run(
                cfg.start_agent,
                max_waves=cfg.max_waves,
                step_delay=cfg.step_delay,
                events=[e.model_dump() for e in cfg.events],
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
                db.create_run(run_sim_id, scenario_id, scenario_name, "{}")

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
            from ABM.config import MODEL, BASE_URL, API_TIMEOUT, LOG_DIR
            from ABM.memory_compressor import build_memory_block

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

            background_log = [{"role": "user", "content": f"[배경] {cfg.background}"}]
            init_agents    = list(saved_active) if saved_active is not None else None

            sim = Simulation(
                agents, background_log, LOG_DIR,
                MODEL, BASE_URL, API_TIMEOUT,
                event_queue=eq, stop_event=stop_ev,
                initial_agents=init_agents,
                name_aliases=alias_map,
                sim_id=run_sim_id, db=new_db,
            )
            _sim["agents"]         = sim.agents
            _sim["background_log"] = sim.background_log
            _sim["sim_obj"]        = sim
            _sim["scenario_id"]    = run.get("scenario_id")
            _sim["scenario_name"]  = scenario_name

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
        other_names = [k for k in sim_obj.active_agents if k != name]
        key_to_alias = sim_obj._key_to_alias
    else:
        other_names = [k for k in agents if k != name]
        key_to_alias = {}
    messages      = agent.build_messages(bg_log, other_names, key_to_alias)
    est_tokens    = agent.estimate_context_tokens(bg_log, other_names, key_to_alias)
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
    return _sim["shared_log"]


@router.get("/edges")
def get_edges():
    return _sim["edges"]
