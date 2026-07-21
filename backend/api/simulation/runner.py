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
                        elapsed_minutes=getattr(sim_obj, "_elapsed_minutes", None),
                    )
                except Exception:
                    pass
                try:
                    snapshots = {
                        key: agent.memory
                        for key, agent in sim_obj.agents.items()
                    }
                    db.save_agent_snapshots(run_sim_id, snapshots)
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
