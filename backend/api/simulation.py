from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db.database import get_db

router = APIRouter(prefix="/api/simulation")


def _get_sim_db():
    """Return a SimDB instance for the shared simulation.db file."""
    from ABM.db import SimDB
    from ABM.config import LOG_DIR
    return SimDB(os.path.join(LOG_DIR, "simulation.db"))


# ── Global simulation state (single-process only) ─────────────────────────────
_sim: dict = {
    "status":         "idle",   # idle | running | done | stopped | error
    "event_queue":    None,
    "stop_event":     None,
    "thread":         None,
    "shared_log":     [],
    "edges":          [],
    "agents":         {},
    "background_log": [],
    "sim_obj":        None,
    "scenario_id":    None,
    "scenario_name":  None,
}


# ── Schemas ───────────────────────────────────────────────────────────────────

class AgentConfig(BaseModel):
    name:           str
    system_prompt:  str
    icon:           str  = "🤖"
    initial_active: bool = True
    display_name:   str  = ""


class ScenarioEvent(BaseModel):
    wave:    int
    type:    str             # "system_message" | "agent_enter" | "agent_exit"
    message: str       = ""
    targets: list[str] = ["all"]
    agent:   str       = ""  # agent_enter / agent_exit 전용


class ExtraField(BaseModel):
    name:    str
    default: str = ""


class SimStartConfig(BaseModel):
    scenario_id:            str | None       = None
    agents:                 list[AgentConfig]
    background:             str
    start_agent:            str
    max_waves:              int              = 10
    step_delay:             float            = 1.0
    token_limit:            int              = 8192
    extra_fields:           list[ExtraField] = [
        ExtraField(name="emotion", default="neutral"),
        ExtraField(name="action",  default="speak"),
    ]
    events:                 list[ScenarioEvent] = []
    output_format_template: str              = ""


class ScenarioSave(BaseModel):
    name:        str
    description: str = ""
    config:      SimStartConfig


class SimContinueConfig(BaseModel):
    start_agent: str
    max_waves:   int              = 10
    step_delay:  float            = 1.0
    events:      list[ScenarioEvent] = []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _blocking_get(q: queue.Queue) -> dict | None:
    """SSE 제너레이터용 블로킹 큐 get (30초 타임아웃 후 ping 반환)."""
    try:
        return q.get(timeout=30)
    except queue.Empty:
        return {"type": "ping", "data": {}}


# ── Simulation control ────────────────────────────────────────────────────────

@router.post("/start")
def start_simulation(cfg: SimStartConfig):
    if _sim["status"] == "running":
        raise HTTPException(409, "Simulation already running")

    eq      = queue.Queue()
    stop_ev = threading.Event()
    _sim.update(
        status="running",
        event_queue=eq,
        stop_event=stop_ev,
        shared_log=[],
        edges=[],
        sim_obj=None,
    )

    def _run():
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

            _sim["shared_log"] = sim.shared_log
            _sim["edges"]      = sim.edges
            _sim["status"]     = "stopped" if stop_ev.is_set() else "done"
            final_status = _sim["status"]
            db.finish_run(
                run_sim_id, final_status, sim.completed_waves, len(sim.shared_log),
                active_agents=sim.active_agents, pending_wave=sim._pending_wave,
            )
        except Exception as e:
            _sim["status"] = "error"
            eq.put({"type": "error", "data": {"message": str(e)}})
            try:
                db.finish_run(run_sim_id, "error", 0, 0)
            except Exception:
                pass
        finally:
            eq.put(None)  # sentinel: SSE 스트림 종료 신호

    t = threading.Thread(target=_run, daemon=True)
    _sim["thread"] = t
    t.start()
    return {"status": "started"}


@router.post("/stop")
def stop_simulation():
    ev = _sim.get("stop_event")
    if ev:
        ev.set()
    _sim["status"] = "stopped"
    return {"status": "stopping"}


@router.post("/continue")
def continue_simulation(cfg: SimContinueConfig):
    if _sim["status"] not in ("done", "stopped"):
        raise HTTPException(409, "이어서 실행은 완료 또는 중지된 시뮬레이션에서만 가능합니다")
    sim_obj = _sim.get("sim_obj")
    if sim_obj is None:
        raise HTTPException(409, "이어서 실행할 시뮬레이션 상태가 없습니다")

    eq      = queue.Queue()
    stop_ev = threading.Event()
    _sim.update(status="running", event_queue=eq, stop_event=stop_ev)

    def _run():
        run_sim_id = None
        db = sim_obj._db
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

            _sim["shared_log"] = sim_obj.shared_log
            _sim["edges"]      = sim_obj.edges
            final_status       = "stopped" if stop_ev.is_set() else "done"
            _sim["status"]     = final_status
            if db is not None:
                db.finish_run(
                    run_sim_id, final_status, sim_obj.completed_waves, len(sim_obj.shared_log),
                    active_agents=sim_obj.active_agents, pending_wave=sim_obj._pending_wave,
                )
        except Exception as e:
            _sim["status"] = "error"
            eq.put({"type": "error", "data": {"message": str(e)}})
            try:
                if db is not None and run_sim_id:
                    db.finish_run(run_sim_id, "error", 0, 0)
            except Exception:
                pass
        finally:
            eq.put(None)

    t = threading.Thread(target=_run, daemon=True)
    _sim["thread"] = t
    t.start()
    return {"status": "continuing"}


@router.get("/status")
def get_status():
    return {
        "status":     _sim["status"],
        "log_count":  len(_sim["shared_log"]),
        "edge_count": len(_sim["edges"]),
    }


@router.get("/stream")
async def stream_events():
    q = _sim.get("event_queue")

    async def _gen():
        if q is None:
            yield 'event: error\ndata: {"message":"no active simulation"}\n\n'
            return
        loop = asyncio.get_running_loop()
        while True:
            item = await loop.run_in_executor(None, _blocking_get, q)
            if item is None:  # sentinel
                yield "event: simulation_end\ndata: {}\n\n"
                break
            yield (
                f"event: {item['type']}\n"
                f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# ── Simulation Runs ───────────────────────────────────────────────────────────

@router.get("/runs")
def list_runs(scenario_id: str | None = None):
    db = _get_sim_db()
    return db.get_runs(scenario_id)


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    db = _get_sim_db()
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/runs/{run_id}/log")
def get_run_log(run_id: str):
    db = _get_sim_db()
    return db.get_run_log(run_id)


@router.post("/resume/{run_id}")
def resume_simulation(run_id: str):
    """과거 실행 상태를 복원해 이어서 실행."""
    if _sim["status"] == "running":
        raise HTTPException(409, "Simulation already running")

    db = _get_sim_db()
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")

    config_json = run.get("config_json") or "{}"
    try:
        cfg_dict = json.loads(config_json)
        if not cfg_dict.get("agents"):
            raise ValueError("empty config")
        cfg = SimStartConfig(**cfg_dict)
    except Exception:
        raise HTTPException(400, "이 실행은 설정 스냅샷이 없어 재개할 수 없습니다")

    snapshots        = db.get_agent_snapshots(run_id)
    active_json      = run.get("active_agents_json")
    pending_json     = run.get("pending_wave_json")
    saved_active     = set(json.loads(active_json))  if active_json  else None
    saved_pending    = json.loads(pending_json)       if pending_json else None

    eq      = queue.Queue()
    stop_ev = threading.Event()
    _sim.update(status="running", event_queue=eq, stop_event=stop_ev,
                shared_log=[], edges=[], sim_obj=None)

    def _run():
        new_db     = None
        run_sim_id = None
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

            sim.run(
                cfg.start_agent,
                max_waves=cfg.max_waves,
                step_delay=cfg.step_delay,
                events=[e.model_dump() for e in cfg.events],
                resume_wave=saved_pending,
            )

            _sim["shared_log"] = sim.shared_log
            _sim["edges"]      = sim.edges
            final_status       = "stopped" if stop_ev.is_set() else "done"
            _sim["status"]     = final_status
            new_db.finish_run(
                run_sim_id, final_status, sim.completed_waves, len(sim.shared_log),
                active_agents=sim.active_agents, pending_wave=sim._pending_wave,
            )
        except Exception as e:
            _sim["status"] = "error"
            eq.put({"type": "error", "data": {"message": str(e)}})
            try:
                if new_db and run_sim_id:
                    new_db.finish_run(run_sim_id, "error", 0, 0)
            except Exception:
                pass
        finally:
            eq.put(None)

    t = threading.Thread(target=_run, daemon=True)
    _sim["thread"] = t
    t.start()
    return {"status": "resuming"}


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(run_id: str):
    db = _get_sim_db()
    db.delete_run(run_id)


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


@router.get("/default-output-format")
def get_default_output_format():
    from ABM.agent import DEFAULT_OUTPUT_FORMAT_TEMPLATE
    return {"template": DEFAULT_OUTPUT_FORMAT_TEMPLATE}


# ── Scenarios CRUD ────────────────────────────────────────────────────────────

@router.get("/scenarios")
def list_scenarios():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, description, config_json, created_at, updated_at "
        "FROM simulation_scenarios ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [
        {**dict(r), "config": json.loads(r["config_json"])}
        for r in rows
    ]


@router.post("/scenarios", status_code=201)
def create_scenario(body: ScenarioSave):
    conn = get_db()
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO simulation_scenarios "
        "(id, name, description, config_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, body.name, body.description, body.config.model_dump_json(), now, now),
    )
    conn.commit()
    conn.close()
    return {"id": sid, "name": body.name}


@router.put("/scenarios/{sid}")
def update_scenario(sid: str, body: ScenarioSave):
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE simulation_scenarios "
        "SET name=?, description=?, config_json=?, updated_at=? WHERE id=?",
        (body.name, body.description, body.config.model_dump_json(), now, sid),
    )
    conn.commit()
    conn.close()
    return {"id": sid}


@router.delete("/scenarios/{sid}", status_code=204)
def delete_scenario(sid: str):
    conn = get_db()
    conn.execute("DELETE FROM simulation_scenarios WHERE id=?", (sid,))
    conn.commit()
    conn.close()
