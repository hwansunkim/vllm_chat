"""/load/{run_id} — 과거 실행 상태를 메모리에 복원(실행은 시작하지 않음)."""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException

from ..schemas import SimStartConfig
from ..state import _sim, _sim_lock, get_sim_db
from .llm_config import _make_agent_llm_map, _make_llm


router = APIRouter()


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
        # 관계 지도도 config 에서 매번 새로 만든다 — 계약 층과 같은 원칙이다.
        # 여기서 빠뜨리면 /start 로는 관계가 붙고 /load 로는 조용히 사라진다.
        agent_relationships = {a.name: dict(a.relationships) for a in cfg.agents}

        sim_obj = Simulation(
            agents, background_log, LOG_DIR,
            llm=llm,
            initial_agents=init_agents,
            name_aliases=alias_map,
            db=SimDB(os.path.join(LOG_DIR, "simulation.db")),
            agent_groups=agent_groups,
            agent_relationships=agent_relationships,
            system_agent=cfg.system_agent.model_dump(),
            agent_locations=agent_locations,
            agent_visuals=agent_visuals,
            agent_llm=agent_llm,
            location_graph=[{"name": n.name, "connects_to": n.connects_to, "is_exterior": n.is_exterior, "zone": n.zone, "is_zone_entry": n.is_zone_entry} for n in cfg.location_graph],
            lang_fix_enabled=cfg.lang_fix_enabled,
            lang_fix_retries=cfg.lang_fix_retries,
            llm_max_tokens=cfg.llm_max_tokens,
            sim_start_time=cfg.sim_start_time,
            sim_start_weekday=cfg.sim_start_weekday,
            time_per_wave=cfg.time_per_wave,
            time_mode=cfg.time_mode,
            time_categories=[c.model_dump() for c in cfg.time_categories],
            # 빠뜨리면 /start 로는 AI 시간 추론이 걸리고 /load 로 되살린 실행에서는
            # 조용히 category 로 되돌아간다(엔진 기본값). 구버전 config_json 에는
            # 필드가 없지만 SimStartConfig 기본값이 "category" 라 그대로 안전하다.
            time_estimation_mode=cfg.time_estimation_mode,
            idle_minutes_schedule=cfg.idle_minutes_schedule,
            max_scene_jump_minutes=cfg.max_scene_jump_minutes,
            max_daytime_jump_minutes=cfg.max_daytime_jump_minutes,
            infection_model=cfg.infection_model.model_dump(),
            elapsed_minutes_init=run.get("elapsed_minutes") or 0,
            # /load 는 실행하지 않지만, 이후 /continue 로 이어갈 때
            # fold_elapsed_and_reset_waves 가 _wave_base 를 올바르게 누적하도록
            # 직전 run 의 누적 끝 wave 를 미리 세팅해 둔다.
            wave_base_init=(run.get("start_wave") or 0) + (run.get("total_waves") or 0),
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

        # 이 run 을 이어서 실행하면 첫 wave 가 가질 누적 표시 wave 번호.
        # 프론트가 피드 divider 초기화·리플레이 메타 표시에 쓸 수 있다(선택).
        start_wave = (run.get("start_wave") or 0) + (run.get("total_waves") or 0)

        return {"status": "loaded", "log": log_entries, "infection": infection_snapshot,
                "start_wave": start_wave}

    except Exception as e:
        with _sim_lock:
            _sim["status"] = "idle"
        raise HTTPException(500, str(e))
