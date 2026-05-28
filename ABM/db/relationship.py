"""Relationship memory — latest stance for LLM prompts, full history for audit."""

import time


class RelationshipMixin:
    def upsert_relationships(
        self,
        sim_id: str,
        agent_key: str,
        relationships: list[dict],
        wave: int,
    ):
        conn = self._conn()
        now  = time.time()
        for rel in relationships:
            target = rel["target"]
            stance = rel.get("stance", "neutral")
            reason = rel.get("reason", "")

            conn.execute(
                "INSERT INTO relationship_history "
                "(sim_id, agent_key, target_key, stance, reason, wave, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (sim_id, agent_key, target, stance, reason, wave, now),
            )
            conn.execute(
                "INSERT INTO relationship_memory "
                "(sim_id, agent_key, target_key, stance, reason, updated_wave, updated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(sim_id, agent_key, target_key) DO UPDATE SET "
                "stance=excluded.stance, reason=excluded.reason, "
                "updated_wave=excluded.updated_wave, updated_at=excluded.updated_at",
                (sim_id, agent_key, target, stance, reason, wave, now),
            )
        conn.commit()

    def get_relationships(self, sim_id: str, agent_key: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT target_key, stance, reason FROM relationship_memory "
            "WHERE sim_id=? AND agent_key=?",
            (sim_id, agent_key),
        ).fetchall()
        return [dict(r) for r in rows]
