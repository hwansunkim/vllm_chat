"""로그·이벤트·엣지 피드 엔드포인트: /logs, /events, /runs/{run_id}/events, /edges."""
from __future__ import annotations

from fastapi import APIRouter

from ..state import _sim, get_sim_db


router = APIRouter()


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
