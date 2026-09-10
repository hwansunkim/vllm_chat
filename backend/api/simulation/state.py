"""Global simulation state (single-process only).

Provides the shared ``_sim`` dict, an ``_sim_lock`` for atomic status
transitions, and helpers that build infrastructure-level objects (SimDB).
"""
from __future__ import annotations

import os
import threading


# Lock guarding status transitions in ``_sim`` (idle <-> running, etc.).
# Use ``with _sim_lock:`` whenever you need to atomically check-and-set
# ``_sim["status"]`` to avoid race conditions between concurrent /start,
# /continue, and /resume requests.
_sim_lock = threading.Lock()


# 새 실행·재개·불러오기를 거부해야 하는 "바쁨" 상태들. `/stop` 직후의
# ``stopping`` 도 포함 — 워커 스레드가 아직 wave 루프 안에 있을 수 있으므로,
# 그 스레드가 finalize 로 빠져나오기 전에 새 실행이 들어오면 두 실행의 전역
# 상태·로그가 섞인다(이전 실행 finalizer 가 새 실행을 덮어씀).
_BUSY_STATES = ("running", "stopping", "loading")


_sim: dict = {
    "status":         "idle",   # idle | running | stopping | stopped | done | loading | error
    "event_queue":    None,
    "stop_event":     None,
    "thread":         None,
    "run_sim_id":     None,      # 현재 전역 상태의 소유자인 run id. finalizer 가
                                 # 이 값과 자기 run id 를 대조해, 이미 다른 실행이
                                 # 슬롯을 차지했으면 전역 쓰기를 건너뛴다.
    "shared_log":     [],
    "edges":          [],
    "agents":         {},
    "background_log": [],
    "sim_obj":        None,
    "scenario_id":    None,
    "scenario_name":  None,
}


def is_current_run(run_sim_id: str | None) -> bool:
    """``run_sim_id`` 가 현재 전역 상태의 소유자인지. ``_sim_lock`` 아래에서 호출."""
    return run_sim_id is not None and _sim.get("run_sim_id") == run_sim_id


def get_sim_db():
    """Return a SimDB instance for the shared simulation.db file."""
    from ABM.db import SimDB
    from ABM.config import LOG_DIR
    return SimDB(os.path.join(LOG_DIR, "simulation.db"))
