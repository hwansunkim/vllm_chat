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


_sim: dict = {
    "status":         "idle",   # idle | running | done | stopped | error
    "event_queue":    None,
    "stop_event":     None,
    "thread":         None,
    "shared_log":     [],
    "edges":          [],
    "agents":         {},
    "background_log": [],
    "sim_obj":        None,
    "scenario_id":    None,
    "scenario_name":  None,
}


def get_sim_db():
    """Return a SimDB instance for the shared simulation.db file."""
    from ABM.db import SimDB
    from ABM.config import LOG_DIR
    return SimDB(os.path.join(LOG_DIR, "simulation.db"))
