"""Compression log — audit trail of when raw memory was compressed."""

import time


class CompressionMixin:
    def log_compression(
        self,
        sim_id: str,
        agent_key: str,
        msg_count: int,
        wave: int,
    ):
        conn = self._conn()
        conn.execute(
            "INSERT INTO compression_log (sim_id, agent_key, msg_count, wave, created_at) "
            "VALUES (?,?,?,?,?)",
            (sim_id, agent_key, msg_count, wave, time.time()),
        )
        conn.commit()

    def count_compressions(self, sim_id: str, agent_key: str) -> int:
        """이 에이전트가 지금까지 1차 압축을 몇 번 거쳤는지.

        `ABM/simulation/step.py::_maybe_consolidate_facts`가 이 값을 보고
        `_CONSOLIDATION_EVERY_N_COMPRESSIONS`번마다 2차 정리를 트리거한다.
        """
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM compression_log WHERE sim_id=? AND agent_key=?",
            (sim_id, agent_key),
        ).fetchone()
        return int(row["n"]) if row else 0
