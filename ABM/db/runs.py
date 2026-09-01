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
        start_wave: int = 0,
    ):
        """`start_wave` = 이 run의 첫 wave가 갖는 누적 표시 wave 번호.

        fresh /start는 0, /continue·/resume은 직전 run의 (start_wave + total_waves).
        누적 마지막 wave = start_wave + total_waves 로 유도한다(`finish_run`은
        `total_waves`에 계속 per-run 값을 넣는다 — 하위호환·run_number 분석용).
        """
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
            "(run_id, scenario_id, scenario_name, run_number, status, started_at, config_json, start_wave) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, scenario_id, scenario_name, run_number, "running", time.time(),
             config_json, int(start_wave or 0)),
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
        elapsed_minutes: int | None = None,
    ):
        conn = self._conn()
        conn.execute(
            "UPDATE simulation_runs "
            "SET status=?, total_waves=?, total_turns=?, finished_at=?, "
            "active_agents_json=?, pending_wave_json=?, elapsed_minutes=? "
            "WHERE run_id=?",
            (
                status, total_waves, total_turns, time.time(),
                json.dumps(list(active_agents), ensure_ascii=False) if active_agents is not None else None,
                json.dumps(pending_wave, ensure_ascii=False) if pending_wave is not None else None,
                elapsed_minutes,
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
        time_str: str | None = None,
    ):
        conn = self._conn()
        conn.execute(
            "INSERT INTO simulation_log "
            "(run_id, wave, turn, speaker, content, action_note, meta_json, targets_json, timestamp, time_str) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, wave, turn, speaker, content, action_note,
                json.dumps(meta, ensure_ascii=False),
                json.dumps(targets, ensure_ascii=False),
                time.time(),
                time_str,
            ),
        )
        conn.commit()

    def get_run_log(self, run_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT wave, turn, speaker, content, action_note, meta_json, targets_json, timestamp, time_str "
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

    def save_agent_snapshots(
        self,
        run_id: str,
        snapshots: dict[str, list],
        states: dict[str, dict] | None = None,
    ):
        """Persist each agent's working memory (and runtime state) for resume.

        `states` carries the per-agent runtime state that is *not* reconstructible
        from the scenario config — current location, appearance, and who each agent
        knows / has seen as a stranger. Without it, load/resume would silently reset
        every agent to its scenario-initial position and appearance.
        """
        states = states or {}
        conn = self._conn()
        # state_json 은 COALESCE 로 조건부 갱신한다 — states 에 없는 키(또는
        # states 자체가 비어 전달된 메모리 전용 저장 호출)가 기존에 저장된
        # 위치/외모 상태를 NULL 로 덮어쓰지 않도록 하기 위함. INSERT OR REPLACE
        # 였다면 memory_json 만 저장하는 호출 한 번으로 state_json 이 소멸한다.
        conn.executemany(
            "INSERT INTO agent_snapshots (run_id, agent_key, memory_json, state_json) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(run_id, agent_key) DO UPDATE SET "
            "  memory_json = excluded.memory_json, "
            "  state_json  = COALESCE(excluded.state_json, agent_snapshots.state_json)",
            [
                (
                    run_id, key,
                    json.dumps(mem, ensure_ascii=False),
                    json.dumps(states[key], ensure_ascii=False) if states.get(key) is not None else None,
                )
                for key, mem in snapshots.items()
            ],
        )
        conn.commit()

    def get_agent_snapshots(self, run_id: str) -> dict[str, list]:
        rows = self._conn().execute(
            "SELECT agent_key, memory_json FROM agent_snapshots WHERE run_id=?", (run_id,)
        ).fetchall()
        return {r["agent_key"]: json.loads(r["memory_json"]) for r in rows}

    def get_agent_states(self, run_id: str) -> dict[str, dict]:
        """Per-agent runtime state saved alongside the memory snapshot.

        Agents whose row predates the `state_json` column (or that were saved
        without state) are simply absent from the result, so callers fall back to
        the scenario-initial values.
        """
        rows = self._conn().execute(
            "SELECT agent_key, state_json FROM agent_snapshots WHERE run_id=?", (run_id,)
        ).fetchall()
        result: dict[str, dict] = {}
        for r in rows:
            raw = r["state_json"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                result[r["agent_key"]] = parsed
        return result

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
        # interview_log has an ON DELETE CASCADE FK to simulation_runs, but the
        # explicit delete keeps this working on connections where
        # `PRAGMA foreign_keys` is off and must run *before* simulation_runs.
        conn.execute("DELETE FROM interview_log     WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM simulation_runs   WHERE run_id=?", (run_id,))
        conn.commit()
