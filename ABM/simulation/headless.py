"""GUI 없이 ``SimStartConfig`` 하나로 시뮬레이션을 돌리는 순수 러너.

원래 이 조립 로직은 ``backend/api/simulation/runtime/lifecycle.py:_run()`` 안에
FastAPI 전역(``_sim`` dict, SSE 큐, run row 생성)과 뒤엉켜 있었다. 여기로 옮겨
"설정 → Agent들 + Simulation → sim.run() → 결과 수집"만 남기고, lifecycle 은
SSE 큐 연결과 run row 생명주기만 담당하는 얇은 래퍼가 됐다.

**이 모듈이 GUI ``/start`` 의 유일한 실행 경로다.** 즉 여기를 고치면 CLI와 GUI가
함께 바뀐다(그게 목적이다) — Simulation/sim.run 인자를 하나라도 빠뜨리면 GUI가
같이 회귀한다.

``/load`` · ``/resume`` 은 과거 실행 상태를 복원하는 별도 조립 경로(load.py ·
resume.py)를 그대로 쓴다. 이 함수는 fresh start 전용이다.
"""
from __future__ import annotations

import queue as _queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .core import _PERSIST_EVENTS


@dataclass
class RunResult:
    """한 번의 ``run_config()`` 실행 결과.

    ``events`` 는 ``_PERSIST_EVENTS`` 에 속한 이벤트만 담는다 — DB에 기록되는 것과
    정확히 같은 집합이라 ``db.get_run_events(run_id)`` 로 다시 읽어도 동일하고,
    마크다운 내보내기(``ABM/export/markdown.py``)가 그대로 소비할 수 있다.
    항목 모양도 DB 조회 결과와 같다: ``{wave, event_type, data, timestamp}``.
    """

    run_id:          str
    shared_log:      list[dict]
    events:          list[dict]
    edges:           list[dict]
    end_reason:      str
    completed_waves: int
    total_turns:     int
    # 실행이 끝난 Simulation 객체. lifecycle 이 finalize_run(스냅샷 저장 등)에 쓰고
    # /continue 가 이어받는다. 순수 CLI 소비자는 무시해도 된다.
    sim:             Any = field(default=None, repr=False)


def run_config(
    cfg,
    *,
    llm,
    agent_llm:    dict | None                    = None,
    log_dir:      str,
    db                                            = None,
    sim_id:       str | None                     = None,
    event_queue:  "_queue.Queue | None"          = None,
    stop_event:   threading.Event | None         = None,
    on_event:     Callable[[str, dict], None] | None = None,
    on_sim_ready: Callable[[Any], None] | None   = None,
) -> RunResult:
    """``SimStartConfig`` 를 실행하고 결과를 모아 돌려준다.

    Parameters
    ----------
    cfg
        ``backend.api.simulation.schemas.SimStartConfig`` 인스턴스. (ABM 이 pydantic
        모델을 import 하지 않도록 타입 힌트는 붙이지 않는다 — 덕 타이핑.)
    llm
        ``(messages, max_tokens=…) -> (content, reasoning, usage)`` 동기 콜러블.
        보통 ``backend/api/simulation/runtime/llm_config.py:_make_llm()`` 의 결과.
    agent_llm
        에이전트 키 → 콜러블 오버라이드 맵(``_make_agent_llm_map()``). 없으면 전부 ``llm``.
    log_dir
        agent 로그 json · shared_log.json · edges.json 이 쌓이는 디렉토리.
        **동시 실행할 때는 run 마다 반드시 다른 경로를 줄 것** (서로 덮어쓴다).
    db
        ``ABM.db.SimDB`` 또는 None. None 이면 턴/이벤트가 DB에 기록되지 않는다.
        run row(``create_run``/``finish_run``)는 호출자 책임이다.
    sim_id
        DB 기록에 쓰이는 run id. 생략하면 새 uuid4.
    event_queue
        SSE 소비자용 큐(GUI 경로). CLI 는 None.
    stop_event
        외부 중지 신호. 생략하면 내부에서 새로 만든다.
    on_event
        모든 emit 에 대해 ``(event_type, data)`` 로 호출되는 진행률 콜백. 이 콜백에서
        난 예외는 시뮬레이션을 죽이지 않고 무시된다.
    on_sim_ready
        ``Simulation`` 생성 직후 · ``run()`` 시작 **전에** 그 객체로 한 번 호출된다.
        lifecycle 이 ``_sim`` 전역을 채우는 자리라 순서가 곧 GUI 동작이다.
    """
    from ..agent import Agent
    from . import Simulation

    run_sim_id = sim_id or str(uuid.uuid4())
    stop_ev    = stop_event or threading.Event()

    agents = {
        a.name: Agent(a.name, a.system_prompt, log_dir,
                      token_limit=cfg.token_limit,
                      extra_fields=[f.model_dump() for f in cfg.extra_fields],
                      # 오버라이드가 실제로 설정됐을 때만 전달. 비어 있으면
                      # 엔진이 현재 설정으로 출력 계약을 생성한다.
                      output_format_template=cfg.effective_output_format_override())
        for a in cfg.agents
    }
    background_log = [{"role": "user", "content": f"[배경] {cfg.background}"}]

    # initial_active 설정 — False인 에이전트는 비활성으로 시작
    initial_agents = [a.name for a in cfg.agents if a.initial_active]
    init_param = None if len(initial_agents) == len(cfg.agents) else initial_agents

    # display_name → key 매핑 (한국어 이름을 target으로 사용해도 올바른 키로 resolve)
    alias_map = {a.display_name: a.name for a in cfg.agents if a.display_name.strip()}
    # 그룹 가시성 맵 — groups 빈 에이전트는 전체 노출 (하위 호환)
    agent_groups    = {a.name: a.groups             for a in cfg.agents}
    agent_locations = {a.name: a.location           for a in cfg.agents}
    agent_visuals   = {a.name: a.visual_description for a in cfg.agents}
    # 관계 지도 — 에이전트별 [아는 사람] 계약 블록과 <TARGETS> 관계 라벨의 원본.
    # 비어 있는 시나리오는 엔진이 블록을 아예 만들지 않는다(기존 동작 불변).
    agent_relationships = {a.name: dict(a.relationships) for a in cfg.agents}

    sim = Simulation(
        agents, background_log, log_dir,
        llm=llm,
        event_queue=event_queue,
        stop_event=stop_ev,
        initial_agents=init_param,
        name_aliases=alias_map,
        sim_id=run_sim_id,
        db=db,
        agent_groups=agent_groups,
        agent_relationships=agent_relationships,
        system_agent=cfg.system_agent.model_dump(),
        agent_locations=agent_locations,
        agent_visuals=agent_visuals,
        agent_llm=agent_llm or {},
        location_graph=[{"name": n.name, "connects_to": n.connects_to, "is_exterior": n.is_exterior, "zone": n.zone, "is_zone_entry": n.is_zone_entry} for n in cfg.location_graph],
        # 백엔드 스키마가 아직 이 필드를 갖고 있지 않을 수 있으므로 getattr로 안전하게.
        # 기본값 "targeted" = 기존 라우팅 동작 그대로.
        perception_mode=getattr(cfg, "perception_mode", "targeted"),
        lang_fix_enabled=cfg.lang_fix_enabled,
        lang_fix_retries=cfg.lang_fix_retries,
        llm_max_tokens=cfg.llm_max_tokens,
        sim_start_time=cfg.sim_start_time,
        sim_start_weekday=cfg.sim_start_weekday,
        time_per_wave=cfg.time_per_wave,
        time_mode=cfg.time_mode,
        time_categories=[c.model_dump() for c in cfg.time_categories],
        # 백엔드 스키마가 아직 이 필드를 갖고 있지 않을 수 있으므로 getattr로 안전하게.
        time_estimation_mode=getattr(cfg, "time_estimation_mode", "category"),
        idle_minutes_schedule=cfg.idle_minutes_schedule,
        max_scene_jump_minutes=cfg.max_scene_jump_minutes,
        max_daytime_jump_minutes=cfg.max_daytime_jump_minutes,
        infection_model=cfg.infection_model.model_dump(),
    )

    if on_sim_ready is not None:
        on_sim_ready(sim)

    # ── 이벤트 수집 ────────────────────────────────────────────────────────────
    # `_emit` 을 인스턴스 속성으로 감싼다. 원본 바운드 메서드를 그대로 먼저 호출하므로
    # SSE 큐 push 와 DB log_event 는 손대지 않은 채 그대로 일어난다.
    # **끝나면 반드시 벗겨낸다** — /continue 가 같은 Simulation 객체로 run() 을 다시
    # 부르는데, 래퍼가 남아 있으면 끝난 run 의 리스트에 계속 append 되어 샌다.
    collected:  list[dict] = []
    end_data:   dict       = {}
    orig_emit              = sim._emit

    def _wrapped_emit(event_type: str, data: dict) -> None:
        orig_emit(event_type, data)
        if event_type in _PERSIST_EVENTS:
            collected.append({
                # `_emit` 이 DB에 쓰는 wave 와 같은 규칙(없으면 0) — `wave` 키가 없는
                # 이벤트가 DB와 다른 값을 갖지 않도록.
                "wave":       data.get("wave", 0),
                "event_type": event_type,
                "data":       data,
                "timestamp":  time.time(),
            })
        elif event_type == "simulation_end":
            end_data.update(data)
        if on_event is not None:
            try:
                on_event(event_type, data)
            except Exception:
                pass  # 진행률 출력 실패가 시뮬레이션을 죽이면 안 된다

    sim._emit = _wrapped_emit
    try:
        sim.run(
            cfg.start_agent,
            max_waves=cfg.max_waves,
            step_delay=cfg.step_delay,
            events=[e.model_dump() for e in cfg.events],
            max_silence_waves=cfg.max_silence_waves,
            early_stop_enabled=cfg.early_stop_enabled,
            target_duration_minutes=cfg.target_duration_minutes,
        )
    finally:
        sim.__dict__.pop("_emit", None)

    return RunResult(
        run_id          = run_sim_id,
        shared_log      = sim.shared_log,
        events          = collected,
        edges           = sim.edges,
        end_reason      = end_data.get("end_reason", ""),
        completed_waves = sim.completed_waves,
        total_turns     = int(end_data.get("total_turns", 0) or 0),
        sim             = sim,
    )
