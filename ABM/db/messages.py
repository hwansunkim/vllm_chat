"""Raw message audit-trail storage."""

import time


class MessagesMixin:
    def save_messages(
        self,
        sim_id: str,
        agent_key: str,
        messages: list[dict],
        wave: int,
    ):
        conn = self._conn()
        now  = time.time()
        conn.executemany(
            "INSERT INTO messages (sim_id, agent_key, role, content, wave, token_est, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sim_id, agent_key,
                    m["role"], m["content"], wave,
                    max(1, len(m["content"].encode("utf-8")) // 4),
                    now,
                )
                for m in messages
            ],
        )
        conn.commit()
