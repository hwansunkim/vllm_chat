"""/resume/{run_id} — 과거 실행 상태를 복원해 새 run 으로 이어서 실행."""
from __future__ import annotations

import json
import os
import queue
import threading
import uuid

from fastapi import APIRouter, HTTPException

from ..runner import finalize_run, swap_event_queue
from ..schemas import SimStartConfig
from ..state import _sim, _sim_lock, get_sim_db
from .llm_config import _make_agent_llm_map, _make_llm


router = APIRouter()


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
                    # 오버라이드가 설정된 실행만 전달. 옛 실행의 프리즈 스냅샷은
                    # 무시되고 엔진이 최신 계약을 다시 만든다.
                    output_format_template=cfg.effective_output_format_override(),
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
