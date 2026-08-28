"""Runtime endpoints: /start, /stop, /continue, /resume, /status, agent ctx, logs.

기능별 모듈로 나뉘어 있으나 공개 표면은 예전 단일 모듈
``backend.api.simulation.runtime`` 과 동일하다 — ``router`` 는 물론 헬퍼
(``_make_llm``, ``_make_agent_llm_map``, ``_parse_run_config`` …)와 모든 엔드포인트
핸들러가 이 패키지 속성으로 그대로 노출되므로 기존 import 경로가 깨지지 않는다.

  - ``llm_config``  : LLM 콜러블 조립 (``_make_llm``, ``_make_agent_llm_map``)
  - ``lifecycle``   : /start, /stop, /continue
  - ``load``        : /load/{run_id}
  - ``resume``      : /resume/{run_id}
  - ``queries``     : /status, 에이전트 컨텍스트·메모리
  - ``interviews``  : /runs/{run_id}/agents/{name}/interview
  - ``feeds``       : /logs, /events, /runs/{run_id}/events, /edges

라우터 포함 순서는 원본 단일 모듈의 데코레이터 선언 순서와 정확히 일치하므로
FastAPI 라우트 매칭 결과(경로 우선순위)가 리팩터링 전후로 동일하다.
"""
from __future__ import annotations

from fastapi import APIRouter

from . import feeds, interviews, lifecycle, llm_config, load, queries, resume
from .interviews import (
    _INTERVIEWABLE_STATUS,
    _parse_run_config,
    create_agent_interview,
    list_agent_interviews,
)
from .lifecycle import continue_simulation, start_simulation, stop_simulation
from .llm_config import _make_agent_llm_map, _make_llm, logger
from .load import load_simulation
from .feeds import get_edges, get_logs, get_run_events, get_sim_events
from .queries import get_agent_context, get_agent_memory, get_status
from .resume import resume_simulation

# 예전 단일 모듈이 최상위에서 import 해 부수적으로 노출하던 이름들 — 외부에서
# ``runtime.<name>`` 으로 접근하던 코드가 있어도 그대로 동작하도록 유지한다.
from ..runner import finalize_run, swap_event_queue
from ..schemas import (
    InterviewRecord,
    InterviewRequest,
    SimContinueConfig,
    SimStartConfig,
)
from ..state import _sim, _sim_lock, get_sim_db


router = APIRouter()
router.include_router(lifecycle.router)
router.include_router(load.router)
router.include_router(resume.router)
router.include_router(queries.router)
router.include_router(interviews.router)
router.include_router(feeds.router)


__all__ = [
    "router",
    # LLM 헬퍼
    "_make_llm",
    "_make_agent_llm_map",
    # 라이프사이클
    "start_simulation",
    "stop_simulation",
    "continue_simulation",
    "load_simulation",
    "resume_simulation",
    # 조회
    "get_status",
    "get_agent_context",
    "get_agent_memory",
    "get_logs",
    "get_sim_events",
    "get_run_events",
    "get_edges",
    # 인터뷰
    "create_agent_interview",
    "list_agent_interviews",
    "_parse_run_config",
    "_INTERVIEWABLE_STATUS",
]
