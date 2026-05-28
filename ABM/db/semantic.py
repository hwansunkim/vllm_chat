"""Semantic memory — facts/beliefs with confidence tracking.

Only meaningfully different confidence levels overwrite existing facts; small
fluctuations are ignored to avoid log churn. Contradicting evidence blends
confidence downward rather than flipping the fact.
"""

import sqlite3
import time

# Confidence delta required to overwrite an existing fact with new evidence.
# 0.15: meaningful shift (e.g. 0.6->0.75 updates, 0.8->0.99 updates, 0.8->0.75 keeps)
CONFIDENCE_UPDATE_THRESHOLD = 0.15


class SemanticMixin:
    def upsert_facts(
        self,
        sim_id: str,
        agent_key: str,
        facts: list[dict],
        wave: int,
    ):
        conn = self._conn()
        now  = time.time()

        existing: dict[str, sqlite3.Row] = {
            r["fact"]: r
            for r in conn.execute(
                "SELECT id, fact, confidence FROM semantic_memory "
                "WHERE sim_id=? AND agent_key=?",
                (sim_id, agent_key),
            ).fetchall()
        }

        for f in facts:
            new_fact  = f["fact"]
            new_conf  = float(f.get("confidence", 1.0))
            prev_fact = f.get("prev_fact")
            prev_conf = f.get("prev_confidence")

            if new_fact in existing:
                old      = existing[new_fact]
                old_conf = float(old["confidence"])
                # Update only if new evidence is meaningfully stronger,
                # or the fact itself changed (prev_fact provided).
                if new_conf > old_conf + CONFIDENCE_UPDATE_THRESHOLD or prev_fact:
                    conn.execute(
                        "UPDATE semantic_memory "
                        "SET fact=?, confidence=?, source_wave=?, "
                        "prev_fact=?, prev_confidence=?, updated_at=? WHERE id=?",
                        (new_fact, new_conf, wave, prev_fact, prev_conf, now, old["id"]),
                    )
                elif new_conf < old_conf - CONFIDENCE_UPDATE_THRESHOLD:
                    # Contradicting evidence — lower confidence, note uncertainty.
                    blended = (old_conf + new_conf) / 2
                    conn.execute(
                        "UPDATE semantic_memory SET confidence=?, updated_at=? WHERE id=?",
                        (blended, now, old["id"]),
                    )
                # else: small difference — keep existing entry unchanged.
            else:
                conn.execute(
                    "INSERT INTO semantic_memory "
                    "(sim_id, agent_key, fact, confidence, source_wave, "
                    "prev_fact, prev_confidence, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (sim_id, agent_key, new_fact, new_conf, wave,
                     prev_fact, prev_conf, now),
                )

        conn.commit()

    def get_facts(self, sim_id: str, agent_key: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT fact, confidence FROM semantic_memory "
            "WHERE sim_id=? AND agent_key=? ORDER BY confidence DESC, updated_at DESC",
            (sim_id, agent_key),
        ).fetchall()
        return [dict(r) for r in rows]
