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
