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
        elapsed_minutes: int | None = None,
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
                        "SET fact=?, confidence=?, source_wave=?, elapsed_minutes=?, "
                        "prev_fact=?, prev_confidence=?, updated_at=? WHERE id=?",
                        (new_fact, new_conf, wave, elapsed_minutes,
                         prev_fact, prev_conf, now, old["id"]),
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
                    "(sim_id, agent_key, fact, confidence, source_wave, elapsed_minutes, "
                    "prev_fact, prev_confidence, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (sim_id, agent_key, new_fact, new_conf, wave, elapsed_minutes,
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

    def get_all_facts(self, sim_id: str, agent_key: str) -> list[dict]:
        """id 포함 전체 사실 — `get_facts()`와 달리 상한·정렬 없이 그대로.

        2차 기억 정리(`ABM/memory_compressor.py::consolidate_facts`) 전용.
        1차 압축의 "기존 기억" 재진술이나 실행 중 프롬프트 주입에는
        `get_facts()`(및 `_fact_lines`의 표시 상한)를 쓸 것 — 여기 없는
        `id`를 굳이 필요로 하지 않는 한 이 메서드를 쓸 이유가 없다.
        """
        rows = self._conn().execute(
            "SELECT id, fact, confidence FROM semantic_memory "
            "WHERE sim_id=? AND agent_key=? ORDER BY id",
            (sim_id, agent_key),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_fact_confidence(self, fact_id: int, confidence: float) -> None:
        """2차 기억 정리가 사실의 확신도만 재평가한다 — 행 자체는 지우지 않는다.

        (강화/쇠퇴 둘 다 이 메서드 하나로 처리 — 방향은 호출부가 정한다.)
        """
        conn = self._conn()
        conn.execute(
            "UPDATE semantic_memory SET confidence=?, updated_at=? WHERE id=?",
            (confidence, time.time(), fact_id),
        )
        conn.commit()
