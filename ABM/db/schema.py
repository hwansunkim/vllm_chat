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
    -- 시뮬레이션 시작부터의 절대 경과분. 이 압축 배치가 실제로 일어난 시점을
    -- 코드가 못박은 값이다(LLM이 "몇 번째 wave"인지 지어내던 `wave`와 달리
    -- 사후 환산이 필요 없다) — build_memory_block()의 "방금/며칠 전" recency
    -- 판정이 이 값을 쓴다. `wave`는 옛 행 호환용으로 남겨둔다.
    elapsed_minutes INTEGER,
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
    -- episodic_memory.elapsed_minutes와 같은 이유. 지금은 렌더링에 안 쓰지만
    -- (사실은 "지속되는 참"이라 recency 라벨을 안 붙인다) 스키마를 맞춰
    -- 나중에 필요해지면 마이그레이션 없이 바로 쓸 수 있게 해둔다.
    elapsed_minutes INTEGER,
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
    start_wave    INTEGER DEFAULT 0,
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
    time_str     TEXT,
    -- 그 턴 시점(=해당 wave의 이동 적용 **전**)의 발화자 위치. 접촉 분석용.
    location     TEXT,
    is_exterior  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_simlog_run ON simulation_log(run_id, id);

-- state_json: 위치/외모/인지관계 등 재개 시 복원해야 하는 에이전트 런타임 상태.
-- 없으면(구버전 행) 시나리오 초기값으로 폴백한다.
CREATE TABLE IF NOT EXISTS agent_snapshots (
    run_id      TEXT NOT NULL,
    agent_key   TEXT NOT NULL,
    memory_json TEXT NOT NULL,
    state_json  TEXT,
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
        ("start_wave",         "ALTER TABLE simulation_runs ADD COLUMN start_wave INTEGER DEFAULT 0"),
    ]:
        if col not in cols:
            conn.execute(ddl)

    # simulation_log 테이블의 time_str 컬럼이 없는 기존 DB를 위한 마이그레이션
    simlog_cols = {r[1] for r in conn.execute("PRAGMA table_info(simulation_log)").fetchall()}
    if "time_str" not in simlog_cols:
        conn.execute("ALTER TABLE simulation_log ADD COLUMN time_str TEXT")

    # location/is_exterior 컬럼이 없는 기존 DB를 위한 마이그레이션.
    # 기존 행은 NULL 로 남는다(옛 로그는 위치를 재구성할 수 없으므로 백필하지 않는다).
    if "location" not in simlog_cols:
        conn.execute("ALTER TABLE simulation_log ADD COLUMN location TEXT")
    if "is_exterior" not in simlog_cols:
        conn.execute("ALTER TABLE simulation_log ADD COLUMN is_exterior INTEGER")

    # agent_snapshots 테이블의 state_json 컬럼이 없는 기존 DB를 위한 마이그레이션.
    # 기존 행은 state_json=NULL 로 남고, 복원 시 시나리오 초기값으로 폴백된다.
    snap_cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_snapshots)").fetchall()}
    if "state_json" not in snap_cols:
        conn.execute("ALTER TABLE agent_snapshots ADD COLUMN state_json TEXT")

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

    # episodic_memory/semantic_memory에 elapsed_minutes 컬럼이 없는 기존 DB용 마이그레이션.
    # 기존 행은 NULL로 남는다 — 옛 `wave` 값은 배치 전체에 같은 숫자가 찍히는 버그가
    # 있었으므로 그걸로 역산 백필하지 않는다(build_memory_block이 NULL을 "아주 오래된
    # 기억"으로 안전하게 취급한다).
    ep_cols = {r[1] for r in conn.execute("PRAGMA table_info(episodic_memory)").fetchall()}
    if "elapsed_minutes" not in ep_cols:
        conn.execute("ALTER TABLE episodic_memory ADD COLUMN elapsed_minutes INTEGER")
    sem_cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memory)").fetchall()}
    if "elapsed_minutes" not in sem_cols:
        conn.execute("ALTER TABLE semantic_memory ADD COLUMN elapsed_minutes INTEGER")

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
