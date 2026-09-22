"""실행 상태 조회 엔드포인트: /status 와 에이전트별 컨텍스트/메모리 점검.

로그·이벤트·엣지 피드는 ``feeds.py`` 에 있다.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..state import _sim


router = APIRouter()


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
        # 실행 경로(_step_agent)와 같은 이유로 "지금" 기준 최신 기억 블록을
        # 매번 새로 렌더링한다 — 캐시된 값을 읽으면 컨텍스트 탭이 몇 wave
        # 전의 "방금"/"며칠 전" 라벨을 그대로 보여준다.
        mem_block         = sim_obj._fresh_memory_block(name, sim_obj._current_elapsed_minutes())
    else:
        visible_names     = [k for k in agents if k != name]
        key_to_alias      = {}
        target_sections   = None
        location_name     = ""
        situation_targets = False
        ephemeral_msgs    = None
        mem_block         = None
    messages   = agent.build_messages(
        bg_log, visible_names, key_to_alias, target_sections,
        location_name, situation_targets, ephemeral_msgs, mem_block,
    )
    est_tokens = agent.estimate_context_tokens(
        bg_log, visible_names, key_to_alias, target_sections,
        location_name, situation_targets, ephemeral_msgs, mem_block,
    )
    prompt_tokens = agent._last_prompt_tokens if agent._last_prompt_tokens is not None else est_tokens

    # 상태(수면·이동 등) — 리뷰에서 지적된 감사 공백(Markdown/DB 어디에도 상태
    # 진입값이 없다)을 메꾸는 세 경로 중 하나. 실행 중 sim_obj가 있을 때만
    # 의미가 있고, 없거나(과거 실행 재생 등) 상태가 없으면 null.
    status = None
    if sim_obj is not None and name in sim_obj.agents:
        now = sim_obj._current_elapsed_minutes(sim_obj.completed_waves)
        st = sim_obj._agent_active_status(name, now)
        if st is not None:
            cat = sim_obj._resolve_state_category(st.get("state"))
            status = {
                "state":            st.get("state"),
                "label":            (cat or {}).get("label"),
                "remaining_minutes": max(0, st.get("until_elapsed", now) - now),
                "until_time_str":   sim_obj._format_time_str(
                    sim_obj._sim_start_minutes + st.get("until_elapsed", now)
                ),
            }

    return {
        "name":           name,
        "memory_size":    len(agent.memory),
        "trimmed":        agent._trimmed_count,
        "prompt_tokens":  prompt_tokens,
        "est_tokens":     est_tokens,
        "token_limit":    agent._token_limit,
        "messages":       messages,
        "status":         status,
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
