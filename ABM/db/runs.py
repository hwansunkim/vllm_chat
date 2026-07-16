"""Simulation runs — top-level run lifecycle, turn log, and snapshots.

This module also owns `delete_run`, which cascades across all memory + run
tables. Note that memory tables use `sim_id` whereas the run-scoped tables
(simulation_log, agent_snapshots, simulation_runs) use `run_id`. In practice
sim_id == run_id for a given simulation, but they live in different columns so
the DELETE statements must use the correct column per table.
"""

import json
import time


# Memory tables keyed by `sim_id`.
_MEMORY_TABLES = (
    "messages",
    "episodic_memory",
    "semantic_memory",
    "relationship_memory",
    "relationship_history",
    "agent_self_state",
    "compression_log",
)


class RunsMixin:
    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        scenario_id: str | None,
        scenario_name: str | None,
        config_json: str,
    ):
        conn = self._conn()
        run_number = 1
        if scenario_id:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM simulation_runs WHERE scenario_id=?",
                (scenario_id,),
            ).fetchone()
            run_number = (row["cnt"] or 0) + 1
        conn.execute(
            "INSERT INTO simulation_runs "
            "(run_id, scenario_id, scenario_name, run_number, status, started_at, config_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, scenario_id, scenario_name, run_number, "running", time.time(), config_json),
        )
        conn.commit()

    def finish_run(
        self,
        run_id: str,
        status: str,
        total_waves: int,
        total_turns: int,
        active_agents: set | None = None,
        pending_wave:  dict | None = None,
    ):
        conn = self._conn()
        conn.execute(
            "UPDATE simulation_runs "
            "SET status=?, total_waves=?, total_turns=?, finished_at=?, "
            "active_agents_json=?, pending_wave_json=? "
            "WHERE run_id=?",
            (
                status, total_waves, total_turns, time.time(),
                json.dumps(list(active_agents), ensure_ascii=False) if active_agents is not None else None,
                json.dumps(pending_wave, ensure_ascii=False) if pending_wave is not None else None,
                run_id,
            ),
        )
        conn.commit()

    def get_runs(self, scenario_id: str | None = None) -> list[dict]:
        conn = self._conn()
        if scenario_id:
            rows = conn.execute(
                "SELECT * FROM simulation_runs WHERE scenario_id=? ORDER BY started_at DESC",
                (scenario_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM simulation_runs ORDER BY started_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Per-turn simulation log
    # ------------------------------------------------------------------

    def log_turn(
        self,
        run_id: str,
        wave: int,
        turn: int,
        speaker: str,
        content: str,
        action_note: str,
        meta: dict,
        targets: list,
    ):
        conn = self._conn()
        conn.execute(
            "INSERT INTO simulation_log "
            "(run_id, wave, turn, speaker, content, action_note, meta_json, targets_json, timestamp) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id, wave, turn, speaker, content, action_note,
                json.dumps(meta, ensure_ascii=False),
                json.dumps(targets, ensure_ascii=False),
                time.time(),
            ),
        )
        conn.commit()

    def get_run_log(self, run_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT wave, turn, speaker, content, action_note, meta_json, targets_json, timestamp "
            "FROM simulation_log WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["meta"]    = json.loads(d.pop("meta_json"))
            d["targets"] = json.loads(d.pop("targets_json"))
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # Agent snapshots (working memory persisted for resume)
    # ------------------------------------------------------------------

    def save_agent_snapshots(self, run_id: str, snapshots: dict[str, list]):
        """Persist each agent's working memory list for potential resume."""
        conn = self._conn()
        conn.executemany(
            "INSERT OR REPLACE INTO agent_snapshots (run_id, agent_key, memory_json) "
            "VALUES (?,?,?)",
            [(run_id, key, json.dumps(mem, ensure_ascii=False)) for key, mem in snapshots.items()],
        )
        conn.commit()

    def get_agent_snapshots(self, run_id: str) -> dict[str, list]:
        rows = self._conn().execute(
            "SELECT agent_key, memory_json FROM agent_snapshots WHERE run_id=?", (run_id,)
        ).fetchall()
        return {r["agent_key"]: json.loads(r["memory_json"]) for r in rows}

    # ------------------------------------------------------------------
    # Simulation events (SSE 이벤트 영속화)
    # ------------------------------------------------------------------

    def log_event(self, run_id: str, wave: int, event_type: str, data: dict) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO sim_events (run_id, wave, event_type, data_json, timestamp) VALUES (?,?,?,?,?)",
            (run_id, wave, event_type, json.dumps(data, ensure_ascii=False), time.time()),
        )
        conn.commit()

    def get_run_events(
        self,
        run_id: str,
        event_types: list[str] | None = None,
    ) -> list[dict]:
        if event_types:
            placeholders = ",".join("?" * len(event_types))
            rows = self._conn().execute(
                f"SELECT wave, event_type, data_json, timestamp "
                f"FROM sim_events WHERE run_id=? AND event_type IN ({placeholders}) ORDER BY id",
                (run_id, *event_types),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT wave, event_type, data_json, timestamp "
                "FROM sim_events WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d.pop("data_json"))
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # Cascade delete
    # ------------------------------------------------------------------

    def delete_run(self, run_id: str) -> None:
        """Remove a run and all memory/log rows associated with it.

        D1 fix: memory tables use `sim_id` column; simulation_log and
        agent_snapshots use `run_id`. Earlier versions of this method
        incorrectly used `WHERE sim_id=?` on simulation_log, leaving orphans.
        """
        conn = self._conn()
        # Memory tables (sim_id column) — sim_id == run_id for these.
        for table in _MEMORY_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE sim_id=?", (run_id,))
        # Run-scoped tables (run_id column).
        conn.execute("DELETE FROM simulation_log    WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM agent_snapshots   WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM sim_events        WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM simulation_runs   WHERE run_id=?", (run_id,))
        conn.commit()
