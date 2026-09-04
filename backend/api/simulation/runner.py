"""Shared bootstrap/finalize helpers for simulation _run threads.

Centralizes the try/except/finally tail logic that ``/start``, ``/continue``,
and ``/resume`` previously duplicated, and provides a safe way to swap the
SSE event queue without orphaning any previously connected consumers.
"""
from __future__ import annotations

import queue
import threading

from .state import _sim, _sim_lock


def swap_event_queue(new_queue: queue.Queue, new_stop_event: threading.Event,
                     **extra) -> None:
    """Atomically install a new SSE event queue and stop event.

    Before installing the new queue, push a sentinel ``None`` onto the
    *previous* queue (if any) so that any SSE consumer still blocked on
    ``_blocking_get`` for the old queue can terminate cleanly instead of
    silently switching to events that belong to the next simulation run.
    """
    with _sim_lock:
        old_q = _sim.get("event_queue")
        if old_q is not None and old_q is not new_queue:
            try:
                old_q.put_nowait(None)
            except Exception:
                # If the queue is bounded and full we still want to fall
                # through; the consumer will detect the stale state via
                # the next read or timeout.
                pass
        _sim["event_queue"] = new_queue
        _sim["stop_event"]  = new_stop_event
        if extra:
            _sim.update(extra)


def _total_elapsed_minutes(sim_obj) -> int | None:
    """저장용 총 경과 분 — 이전 run들의 누적 + 이번 run의 wave 경과.

    `_current_elapsed_minutes`가 그 총계를 계산하는 단일 진실 공급원이라 그것을
    쓰고, 없는 구버전/목(mock) sim 객체에서는 raw `_elapsed_minutes`로 물러난다.
    """
    fn = getattr(sim_obj, "_current_elapsed_minutes", None)
    if callable(fn):
        try:
            return fn(getattr(sim_obj, "completed_waves", 0))
        except Exception:
            pass
    return getattr(sim_obj, "_elapsed_minutes", None)


def fold_elapsed_and_reset_waves(sim_obj) -> None:
    """`/continue` 준비: 총 경과를 `_elapsed_minutes`로 접고 `completed_waves`를 0으로.

    `run()`은 호출마다 wave 0부터 다시 세므로 `completed_waves`를 리셋해야 하는데,
    그 전에 이번까지의 총 경과를 `_elapsed_minutes`(= 이전 run들의 누적을 담는
    자리)로 접어 넣어야 시뮬레이션 시계가 연속된다. 접지 않으면 fixed 모드에서
    에이전트가 보는 `[현재 시각]`이 시나리오 시작 시각으로 되감긴다.

      - fixed:    `_elapsed_minutes += completed_waves * time_per_wave`
      - variable: `_elapsed_minutes`가 이미 누적 총계라 같은 값 → no-op

    **순서가 곧 정확성이다.** 접기 전의 '지금'(`before`)을 먼저 붙잡고, 접고,
    리셋한 뒤, 그 `before`를 명시적으로 넘겨 앵커를 재기준화한다. 접은 뒤에
    인자 없이 `rebase_infection_anchors()`를 부르면 `now`가 접힌 `_elapsed_minutes`
    를 한 번 더 세어(= before + completed_waves*tpw) 감염 앵커가 두 배로 밀린다.

    접기가 제대로 됐다면 `before == _current_elapsed_minutes(0)`이라 재기준화는
    no-op이다 — 접기를 빠뜨린 회귀를 흡수하는 방어선으로만 남겨둔다.

    경과 분과 같은 맥락으로 wave 표시 카운터도 누적으로 올린다: `completed_waves`를
    0으로 되돌리기 전에 `_wave_base += completed_waves` 해야 이어서 실행한 run 의
    wave 라벨(피드 뱃지·DB `simulation_log.wave`)이 되감기지 않고 연속된다.
    """
    before = _total_elapsed_minutes(sim_obj) or 0
    sim_obj._elapsed_minutes = before
    sim_obj._wave_base = getattr(sim_obj, "_wave_base", 0) + sim_obj.completed_waves
    sim_obj.completed_waves  = 0
    sim_obj.rebase_infection_anchors(now=before)


def finalize_run(db, run_sim_id: str | None, stop_event: threading.Event,
                 sim_obj, eq: queue.Queue, *, error: Exception | None = None) -> None:
    """Common termination path for _run threads.

    - On success: persist final status + log/edge state, mark _sim status.
    - On failure: emit an error event and best-effort finish_run('error').
    - Always: push the sentinel ``None`` so the SSE generator exits.
    """
    try:
        if error is None and sim_obj is not None:
            _sim["shared_log"] = sim_obj.shared_log
            _sim["edges"]      = sim_obj.edges
            final_status       = "stopped" if stop_event.is_set() else "done"
            _sim["status"]     = final_status
            if db is not None and run_sim_id:
                try:
                    db.finish_run(
                        run_sim_id, final_status,
                        sim_obj.completed_waves, len(sim_obj.shared_log),
                        active_agents=sim_obj.active_agents,
                        pending_wave=sim_obj._pending_wave,
                        # 이전 run들의 누적 + 이번 run의 wave 경과 = 총 경과.
                        # raw _elapsed_minutes를 저장하면 fixed 모드에서 이번 run의
                        # wave 경과가 통째로 누락돼 /load·/resume이 시계를 되감는다.
                        elapsed_minutes=_total_elapsed_minutes(sim_obj),
                    )
                except Exception:
                    pass
                try:
                    snapshots = {
                        key: agent.memory
                        for key, agent in sim_obj.agents.items()
                    }
                    # 위치/외모/인지관계도 함께 저장 — 없으면 load/resume이
                    # 모든 에이전트를 시나리오 초기값으로 되돌린다.
                    try:
                        states = sim_obj.export_agent_state()
                    except Exception:
                        states = None
                    db.save_agent_snapshots(run_sim_id, snapshots, states)
                except Exception:
                    pass
        elif error is not None:
            _sim["status"] = "error"
            try:
                eq.put({"type": "error", "data": {"message": str(error)}})
            except Exception:
                pass
            if db is not None and run_sim_id:
                try:
                    db.finish_run(run_sim_id, "error", 0, 0)
                except Exception:
                    pass
    finally:
        try:
            eq.put(None)  # sentinel: SSE stream end signal
        except Exception:
            pass
