"""로그·이벤트·엣지 피드 엔드포인트: /logs, /events, /runs/{run_id}/events, /edges."""
from __future__ import annotations

from fastapi import APIRouter

from ..state import _sim, get_sim_db


router = APIRouter()

# 타입을 **명시적으로** 요청할 때만 내주는 이벤트 — wave 마다 1건씩 쌓여 크다(위치 이력 CSV·β 재계산 전용).
# 타입 필터 없이 전체를 받는 소비자(마크다운 내보내기 등 — 어차피 렌더하지 않음)의 응답을 키우지 않게 뺀다.
_EXPLICIT_ONLY_EVENTS = frozenset({"wave_roster"})


def _fetch(db, run_ids: list[str], types: str) -> list[dict]:
    filter_types = [t.strip() for t in types.split(",") if t.strip()] if types else None
    out: list[dict] = []
    for run_id in run_ids:
        out.extend(db.get_run_events(run_id, filter_types))
    if filter_types is None:
        out = [e for e in out if e.get("event_type") not in _EXPLICIT_ONLY_EVENTS]
    return out


@router.get("/logs")
def get_logs():
    sim_obj = _sim.get("sim_obj")
    log = sim_obj.shared_log if sim_obj is not None else _sim["shared_log"]
    # background_log 항목(speaker 없음)은 마크다운 내보내기용 로그에서 제외
    return [e for e in log if "speaker" in e]


@router.get("/events")
def get_sim_events(types: str = ""):
    """저장된 SSE 이벤트 반환. types=agent_move,world_event 형태로 필터 가능.

    범위는 현재 `sim_obj.shared_log`(= /logs)이 담은 run 들 전체(`_export_run_ids`, 오래된 순).
    타입을 안 주면 `_EXPLICIT_ONLY_EVENTS`(wave_roster)는 빠진다 — 필요하면 types 로 명시.
    """
    sim_obj = _sim.get("sim_obj")
    if sim_obj is None or getattr(sim_obj, "_db", None) is None:
        return []
    # `/logs`(sim_obj.shared_log)와 같은 범위 — /continue 로 이어 붙인 run 들, /load 로 불러온 run.
    run_ids = list(getattr(sim_obj, "_export_run_ids", None) or [])
    if not run_ids and getattr(sim_obj, "_sim_id", None):
        run_ids = [sim_obj._sim_id]
    return _fetch(sim_obj._db, run_ids, types)


@router.get("/runs/{run_id}/events")
def get_run_events(run_id: str, types: str = ""):
    """과거 실행의 SSE 이벤트 반환. 타입을 안 주면 `_EXPLICIT_ONLY_EVENTS` 는 빠진다(위와 같은 규칙)."""
    return _fetch(get_sim_db(), [run_id], types)


@router.get("/edges")
def get_edges():
    return _sim["edges"]
