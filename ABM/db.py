import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_id      TEXT    NOT NULL,
    agent_key   TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    wave        INTEGER,
    token_est   INTEGER,
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS episodic_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_id       TEXT    NOT NULL,
    agent_key    TEXT    NOT NULL,
    wave         INTEGER,
    event        TEXT    NOT NULL,
    participants TEXT,
    importance   INTEGER DEFAULT 3,
    created_at   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_id          TEXT    NOT NULL,
    agent_key       TEXT    NOT NULL,
    fact            TEXT    NOT NULL,
    confidence      REAL    NOT NULL DEFAULT 1.0,
    source_wave     INTEGER,
    prev_fact       TEXT,
    prev_confidence REAL,
    updated_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS relationship_memory (
    sim_id       TEXT    NOT NULL,
    agent_key    TEXT    NOT NULL,
    target_key   TEXT    NOT NULL,
    stance       TEXT,
    reason       TEXT,
    updated_wave INTEGER,
    updated_at   REAL    NOT NULL,
    PRIMARY KEY (sim_id, agent_key, target_key)
);

CREATE TABLE IF NOT EXISTS relationship_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_id      TEXT    NOT NULL,
    agent_key   TEXT    NOT NULL,
    target_key  TEXT    NOT NULL,
    stance      TEXT,
    reason      TEXT,
    wave        INTEGER,
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_self_state (
    sim_id       TEXT    NOT NULL,
    agent_key    TEXT    NOT NULL,
    description  TEXT    NOT NULL,
    updated_wave INTEGER,
    updated_at   REAL    NOT NULL,
    PRIMARY KEY (sim_id, agent_key)
);

CREATE TABLE IF NOT EXISTS compression_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_id      TEXT    NOT NULL,
    agent_key   TEXT    NOT NULL,
    msg_count   INTEGER,
    wave        INTEGER,
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_sim_agent  ON messages(sim_id, agent_key);
CREATE INDEX IF NOT EXISTS idx_ep_sim_agent   ON episodic_memory(sim_id, agent_key);
CREATE INDEX IF NOT EXISTS idx_sem_sim_agent  ON semantic_memory(sim_id, agent_key);
CREATE INDEX IF NOT EXISTS idx_rel_sim_agent  ON relationship_memory(sim_id, agent_key);

CREATE TABLE IF NOT EXISTS simulation_runs (
    run_id        TEXT    PRIMARY KEY,
    scenario_id   TEXT,
    scenario_name TEXT,
    run_number    INTEGER DEFAULT 1,
    status        TEXT    DEFAULT 'running',
    total_waves   INTEGER DEFAULT 0,
    total_turns   INTEGER DEFAULT 0,
    started_at    REAL    NOT NULL,
    finished_at   REAL,
    config_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_scenario ON simulation_runs(scenario_id);

CREATE TABLE IF NOT EXISTS simulation_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    wave         INTEGER NOT NULL DEFAULT 0,
    turn         INTEGER NOT NULL DEFAULT 0,
    speaker      TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    action_note  TEXT    NOT NULL DEFAULT '',
    meta_json    TEXT    NOT NULL DEFAULT '{}',
    targets_json TEXT    NOT NULL DEFAULT '[]',
    timestamp    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simlog_run ON simulation_log(run_id, id);

CREATE TABLE IF NOT EXISTS agent_snapshots (
    run_id      TEXT NOT NULL,
    agent_key   TEXT NOT NULL,
    memory_json TEXT NOT NULL,
    PRIMARY KEY (run_id, agent_key)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON agent_snapshots(run_id);
"""

# Confidence delta required to overwrite an existing fact with new evidence.
# 0.15: meaningful shift (e.g. 0.6→0.75 updates, 0.8→0.99 updates, 0.8→0.75 keeps)
_CONFIDENCE_UPDATE_THRESHOLD = 0.15


class SimDB:
    """Thread-safe SQLite wrapper for simulation memory storage (WAL mode)."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local   = threading.local()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # Apply schema once on a temporary connection.
        conn = sqlite3.connect(db_path)
        conn.executescript(_SCHEMA)
        self._migrate(conn)
        conn.close()
        logger.info(f"SimDB initialised: {db_path}")

    def _migrate(self, conn: sqlite3.Connection):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(simulation_runs)").fetchall()}
        for col, ddl in [
            ("active_agents_json", "ALTER TABLE simulation_runs ADD COLUMN active_agents_json TEXT"),
            ("pending_wave_json",  "ALTER TABLE simulation_runs ADD COLUMN pending_wave_json TEXT"),
        ]:
            if col not in cols:
                conn.execute(ddl)
        conn.commit()

    # ------------------------------------------------------------------
    # Connection management (one connection per thread)
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    # ------------------------------------------------------------------
    # Messages (compressed originals — for audit trail)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Episodic memory
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Semantic memory (facts / beliefs)
    # ------------------------------------------------------------------

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
            new_fact = f["fact"]
            new_conf = float(f.get("confidence", 1.0))
            prev_fact = f.get("prev_fact")
            prev_conf = f.get("prev_confidence")

            if new_fact in existing:
                old      = existing[new_fact]
                old_conf = float(old["confidence"])
                # Update only if new evidence is meaningfully stronger,
                # or the fact itself changed (prev_fact provided).
                if new_conf > old_conf + _CONFIDENCE_UPDATE_THRESHOLD or prev_fact:
                    conn.execute(
                        "UPDATE semantic_memory "
                        "SET fact=?, confidence=?, source_wave=?, "
                        "prev_fact=?, prev_confidence=?, updated_at=? WHERE id=?",
                        (new_fact, new_conf, wave, prev_fact, prev_conf, now, old["id"]),
                    )
                elif new_conf < old_conf - _CONFIDENCE_UPDATE_THRESHOLD:
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

    # ------------------------------------------------------------------
    # Relationship memory (latest state for LLM, full history for audit)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Agent self-state
    # ------------------------------------------------------------------

    def upsert_self_state(
        self,
        sim_id: str,
        agent_key: str,
        description: str,
        wave: int,
    ):
        conn = self._conn()
        conn.execute(
            "INSERT INTO agent_self_state "
            "(sim_id, agent_key, description, updated_wave, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(sim_id, agent_key) DO UPDATE SET "
            "description=excluded.description, "
            "updated_wave=excluded.updated_wave, updated_at=excluded.updated_at",
            (sim_id, agent_key, description, wave, time.time()),
        )
        conn.commit()

    def get_self_state(self, sim_id: str, agent_key: str) -> str | None:
        row = self._conn().execute(
            "SELECT description FROM agent_self_state WHERE sim_id=? AND agent_key=?",
            (sim_id, agent_key),
        ).fetchone()
        return row["description"] if row else None

    # ------------------------------------------------------------------
    # Compression log
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Full structured memory read (for API / debugging)
    # ------------------------------------------------------------------

    def get_full_memory(self, sim_id: str, agent_key: str) -> dict:
        return {
            "episodes":      self.get_episodes(sim_id, agent_key),
            "facts":         self.get_facts(sim_id, agent_key),
            "relationships": self.get_relationships(sim_id, agent_key),
            "self_state":    self.get_self_state(sim_id, agent_key),
        }

    # ------------------------------------------------------------------
    # Simulation log (per-turn conversation records)
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

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Simulation runs
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

    def delete_run(self, run_id: str):
        conn = self._conn()
        tables = [
            "messages", "episodic_memory", "semantic_memory",
            "relationship_memory", "relationship_history",
            "agent_self_state", "compression_log", "simulation_log",
        ]
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE sim_id=?", (run_id,))
        conn.execute("DELETE FROM agent_snapshots WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM simulation_runs WHERE run_id=?", (run_id,))
        conn.commit()
