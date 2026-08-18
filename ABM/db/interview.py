"""Post-simulation interview log — questions put to an agent after a run ends.

Deliberately kept in its own table (`interview_log`) and its own mixin so that
interview turns can never leak into replay/resume paths. `simulation_log`
(RunsMixin.get_run_log) drives the feed and the resume logic; nothing here is
read by those code paths.

Like the other run-scoped tables this keys off `run_id` (== `sim_id` for the
memory tables), not `sim_id`.
"""

import json
import time


class InterviewMixin:
    def log_interview(
        self,
        run_id: str,
        agent_key: str,
        mode: str,
        question: str,
        answer: str,
        meta: dict | None = None,
    ) -> dict:
        """Persist one interview Q&A and return the stored row as a dict."""
        conn = self._conn()
        now  = time.time()
        cur  = conn.execute(
            "INSERT INTO interview_log "
            "(run_id, agent_key, mode, question, answer, meta_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                run_id, agent_key, mode, question, answer,
                json.dumps(meta or {}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        return {
            "id":         cur.lastrowid,
            "run_id":     run_id,
            "agent_key":  agent_key,
            "mode":       mode,
            "question":   question,
            "answer":     answer,
            "meta":       meta or {},
            "created_at": now,
        }

    def get_interviews(
        self,
        run_id: str,
        agent_key: str | None = None,
    ) -> list[dict]:
        """Return interview records for a run, oldest first.

        `agent_key=None` returns every agent's interviews for the run.
        """
        conn = self._conn()
        if agent_key is None:
            rows = conn.execute(
                "SELECT id, run_id, agent_key, mode, question, answer, meta_json, created_at "
                "FROM interview_log WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, run_id, agent_key, mode, question, answer, meta_json, created_at "
                "FROM interview_log WHERE run_id=? AND agent_key=? ORDER BY id",
                (run_id, agent_key),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.pop("meta_json"))
            except Exception:
                d.pop("meta_json", None)
                d["meta"] = {}
            result.append(d)
        return result

    def delete_interviews(self, run_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM interview_log WHERE run_id=?", (run_id,))
        conn.commit()
