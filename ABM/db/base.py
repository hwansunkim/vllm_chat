"""SimDB — composed from per-domain mixins.

Thread-safe SQLite wrapper for simulation memory storage (WAL mode).
Mixins are combined here so `from ABM.db import SimDB` continues to work
exactly as before.
"""

import logging
import os
import sqlite3
import threading

from .conn         import ConnMixin
from .messages     import MessagesMixin
from .episodic     import EpisodicMixin
from .semantic     import SemanticMixin
from .relationship import RelationshipMixin
from .self_state   import SelfStateMixin
from .compression  import CompressionMixin
from .runs         import RunsMixin
from .schema       import SCHEMA, migrate

logger = logging.getLogger(__name__)


class SimDB(
    ConnMixin,
    MessagesMixin,
    EpisodicMixin,
    SemanticMixin,
    RelationshipMixin,
    SelfStateMixin,
    CompressionMixin,
    RunsMixin,
):
    """Thread-safe SQLite wrapper for simulation memory storage (WAL mode)."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local   = threading.local()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # Apply schema once on a temporary connection.
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.close()
        logger.info(f"SimDB initialised: {db_path}")

    # ------------------------------------------------------------------
    # Cross-domain composite reads
    # ------------------------------------------------------------------

    def get_full_memory(self, sim_id: str, agent_key: str) -> dict:
        """Aggregate structured memory across domains for a single agent."""
        return {
            "episodes":      self.get_episodes(sim_id, agent_key),
            "facts":         self.get_facts(sim_id, agent_key),
            "relationships": self.get_relationships(sim_id, agent_key),
            "self_state":    self.get_self_state(sim_id, agent_key),
        }
