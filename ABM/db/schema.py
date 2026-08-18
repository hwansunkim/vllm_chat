"""SQLite schema DDL and lightweight migrations for the ABM database."""

import sqlite3

SCHEMA = """
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
    timestamp    REAL    NOT NULL,
    time_str     TEXT
);
CREATE INDEX IF NOT EXISTS idx_simlog_run ON simulation_log(run_id, id);

CREATE TABLE IF NOT EXISTS agent_snapshots (
    run_id      TEXT NOT NULL,
    agent_key   TEXT NOT NULL,
    memory_json TEXT NOT NULL,
    PRIMARY KEY (run_id, agent_key)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON agent_snapshots(run_id);

CREATE TABLE IF NOT EXISTS sim_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL,
    wave        INTEGER NOT NULL DEFAULT 0,
    event_type  TEXT    NOT NULL,
    data_json   TEXT    NOT NULL DEFAULT '{}',
    timestamp   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simevents_run ON sim_events(run_id, id);

-- 시뮬레이션 종료 후 사후 인터뷰 기록.
-- simulation_log 와 완전히 분리된 테이블이다. 리플레이/피드 조회
-- (get_run_log)는 절대 이 테이블을 읽어서는 안 된다 — 인터뷰 발화가
-- 시뮬레이션 타임라인에 섞이면 재개/재생 결과가 오염된다.
CREATE TABLE IF NOT EXISTS interview_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT    NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
    agent_key  TEXT    NOT NULL,
    mode       TEXT    NOT NULL,
    question   TEXT    NOT NULL,
    answer     TEXT    NOT NULL,
    meta_json  TEXT    NOT NULL DEFAULT '{}',
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interview_run   ON interview_log(run_id, id);
CREATE INDEX IF NOT EXISTS idx_interview_agent ON interview_log(run_id, agent_key, id);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Apply additive column migrations on simulation_runs."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(simulation_runs)").fetchall()}
    for col, ddl in [
        ("active_agents_json", "ALTER TABLE simulation_runs ADD COLUMN active_agents_json TEXT"),
        ("pending_wave_json",  "ALTER TABLE simulation_runs ADD COLUMN pending_wave_json TEXT"),
        ("elapsed_minutes",    "ALTER TABLE simulation_runs ADD COLUMN elapsed_minutes INTEGER"),
    ]:
        if col not in cols:
            conn.execute(ddl)

    # simulation_log 테이블의 time_str 컬럼이 없는 기존 DB를 위한 마이그레이션
    simlog_cols = {r[1] for r in conn.execute("PRAGMA table_info(simulation_log)").fetchall()}
    if "time_str" not in simlog_cols:
        conn.execute("ALTER TABLE simulation_log ADD COLUMN time_str TEXT")

    # sim_events 테이블이 없는 기존 DB를 위한 마이그레이션
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "sim_events" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sim_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT    NOT NULL,
                wave        INTEGER NOT NULL DEFAULT 0,
                event_type  TEXT    NOT NULL,
                data_json   TEXT    NOT NULL DEFAULT '{}',
                timestamp   REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_simevents_run ON sim_events(run_id, id);
        """)

    # interview_log 테이블이 없는 기존 DB를 위한 마이그레이션
    if "interview_log" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS interview_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id     TEXT    NOT NULL REFERENCES simulation_runs(run_id) ON DELETE CASCADE,
                agent_key  TEXT    NOT NULL,
                mode       TEXT    NOT NULL,
                question   TEXT    NOT NULL,
                answer     TEXT    NOT NULL,
                meta_json  TEXT    NOT NULL DEFAULT '{}',
                created_at REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_interview_run   ON interview_log(run_id, id);
            CREATE INDEX IF NOT EXISTS idx_interview_agent ON interview_log(run_id, agent_key, id);
        """)

    conn.commit()
