from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db.database import get_db

router = APIRouter(prefix="/api/simulation")

# ── Global simulation state (single-process only) ─────────────────────────────
_sim: dict = {
    "status":       "idle",   # idle | running | done | stopped | error
    "event_queue":  None,
    "stop_event":   None,
    "thread":       None,
    "shared_log":   [],
    "edges":        [],
    "agents":       {},
    "background_log": [],
    "sim_obj":      None,
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


class SimStartConfig(BaseModel):
    agents:       list[AgentConfig]
    background:   str
    start_agent:  str
    max_waves:    int              = 10
    step_delay:   float            = 1.0
    memory_limit: int              = 20
    events:       list[ScenarioEvent] = []


class ScenarioSave(BaseModel):
    name:        str
    description: str = ""
    config:      SimStartConfig


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
            from ABM.config import MODEL, BASE_URL, API_TIMEOUT

            agents = {
                a.name: Agent(a.name, a.system_prompt, "logs_graph",
                              memory_limit=cfg.memory_limit)
                for a in cfg.agents
            }
            background_log = [{"role": "user", "content": f"[배경] {cfg.background}"}]

            # initial_active 설정 — False인 에이전트는 비활성으로 시작
            initial_agents = [a.name for a in cfg.agents if a.initial_active]
            init_param = None if len(initial_agents) == len(cfg.agents) else initial_agents

            # display_name → key 매핑 (한국어 이름을 target으로 사용해도 올바른 키로 resolve)
            alias_map = {a.display_name: a.name for a in cfg.agents if a.display_name.strip()}

            sim = Simulation(
                agents, background_log, "logs_graph",
                MODEL, BASE_URL, API_TIMEOUT,
                event_queue=eq,
                stop_event=stop_ev,
                initial_agents=init_param,
                name_aliases=alias_map,
            )
            _sim["agents"]         = sim.agents
            _sim["background_log"] = sim.background_log
            _sim["sim_obj"]        = sim
            sim.run(
                cfg.start_agent,
                max_waves=cfg.max_waves,
                step_delay=cfg.step_delay,
                events=[e.model_dump() for e in cfg.events],
            )

            _sim["shared_log"] = sim.shared_log
            _sim["edges"]      = sim.edges
            _sim["status"]     = "stopped" if stop_ev.is_set() else "done"
        except Exception as e:
            _sim["status"] = "error"
            eq.put({"type": "error", "data": {"message": str(e)}})
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
    messages = agent.build_messages(bg_log, other_names, key_to_alias)
    trimmed = max(0, agent._total_added - len(agent.memory))
    return {
        "name":         name,
        "memory_size":  len(agent.memory),
        "total_added":  agent._total_added,
        "trimmed":      trimmed,
        "messages":     messages,
    }


@router.get("/logs")
def get_logs():
    return _sim["shared_log"]


@router.get("/edges")
def get_edges():
    return _sim["edges"]


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
