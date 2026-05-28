"""Episodic memory — event-based recollections per agent."""

import json
import time


class EpisodicMixin:
    def upsert_episodes(
        self,
        sim_id: str,
        agent_key: str,
        episodes: list[dict],
        wave: int,
    ):
        conn = self._conn()
        now  = time.time()
        conn.executemany(
            "INSERT INTO episodic_memory "
            "(sim_id, agent_key, wave, event, participants, importance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sim_id, agent_key,
                    ep.get("wave", wave),
                    ep["event"],
                    json.dumps(ep.get("participants", []), ensure_ascii=False),
                    int(ep.get("importance", 3)),
                    now,
                )
                for ep in episodes
            ],
        )
        conn.commit()

    def get_episodes(self, sim_id: str, agent_key: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT wave, event, participants, importance "
            "FROM episodic_memory WHERE sim_id=? AND agent_key=? ORDER BY wave, id",
            (sim_id, agent_key),
        ).fetchall()
        return [dict(r) for r in rows]
