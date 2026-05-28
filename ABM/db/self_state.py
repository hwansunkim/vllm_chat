"""Agent self-state — single description summarizing how the agent sees itself."""

import time


class SelfStateMixin:
    def upsert_self_state(
        self,
        sim_id: str,
        agent_key: str,
        description: str,
        wave: int,
    ):
        conn = self._conn()
        conn.execute(
            "INSERT INTO agent_self_state "
            "(sim_id, agent_key, description, updated_wave, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(sim_id, agent_key) DO UPDATE SET "
            "description=excluded.description, "
            "updated_wave=excluded.updated_wave, updated_at=excluded.updated_at",
            (sim_id, agent_key, description, wave, time.time()),
        )
        conn.commit()

    def get_self_state(self, sim_id: str, agent_key: str) -> str | None:
        row = self._conn().execute(
            "SELECT description FROM agent_self_state WHERE sim_id=? AND agent_key=?",
            (sim_id, agent_key),
        ).fetchone()
        return row["description"] if row else None
