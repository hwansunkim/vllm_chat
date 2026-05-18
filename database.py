from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

import config

_DEFAULT_AGENTS = [
    {
        "name": "planner", "icon": "🏗️",
        "description": "요구사항 분석 및 실행 계획 수립",
        "role": "전략 기획자", "goal": "기능 정의 및 계획 수립",
        "backstory": "복잡한 요구사항을 분석해 실행 가능한 계획으로 만드는 전문가입니다.",
        "temperature": 0.7, "max_tokens": 1024,
    },
    {
        "name": "developer", "icon": "💻",
        "description": "코드 작성 및 구현",
        "role": "파이썬 개발자", "goal": "코드 작성 및 구현",
        "backstory": "클린 코드와 효율적인 알고리즘을 중시하는 시니어 개발자입니다.",
        "temperature": 0.7, "max_tokens": 1024,
    },
    {
        "name": "reviewer", "icon": "🔍",
        "description": "코드 검토 및 최적화 제안",
        "role": "코드 리뷰어", "goal": "코드 검토 및 최적화",
        "backstory": "보안과 성능 최적화에 매우 까다로운 꼼꼼한 리뷰어입니다.",
        "temperature": 0.3, "max_tokens": 1024,
    },
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id            TEXT PRIMARY KEY,
            type          TEXT NOT NULL,
            content       TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            last_accessed TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_keywords (
            memory_id TEXT NOT NULL REFERENCES memories(id),
            keyword   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_keyword ON memory_keywords(keyword);

        CREATE TABLE IF NOT EXISTS conversations (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            system_prompt TEXT NOT NULL DEFAULT '',
            agent_id      TEXT,
            router_mode   INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turns (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            memories_json   TEXT,
            context_pct     REAL,
            prompt_tokens   INTEGER,
            max_tokens      INTEGER,
            archived        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns(conversation_id, created_at);

        CREATE TABLE IF NOT EXISTS agents (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            description   TEXT NOT NULL DEFAULT '',
            system_prompt TEXT NOT NULL DEFAULT '',
            icon          TEXT NOT NULL DEFAULT '🤖',
            model         TEXT,
            temperature   REAL NOT NULL DEFAULT 0.7,
            max_tokens    INTEGER NOT NULL DEFAULT 1024,
            role          TEXT NOT NULL DEFAULT '',
            goal          TEXT NOT NULL DEFAULT '',
            backstory     TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
    """)
    conn.commit()


def migrate_db(conn: sqlite3.Connection) -> None:
    conv_cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()]
    for col, ddl in [
        ("agent_id", "ALTER TABLE conversations ADD COLUMN agent_id TEXT"),
        ("router_mode", "ALTER TABLE conversations ADD COLUMN router_mode INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in conv_cols:
            conn.execute(ddl)

    agent_cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
    for col in ("role", "goal", "backstory"):
        if col not in agent_cols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

    conn.commit()


def seed_default_agents(conn: sqlite3.Connection) -> None:
    existing = {r[0] for r in conn.execute("SELECT name FROM agents").fetchall()}
    now = datetime.now().isoformat()
    for a in _DEFAULT_AGENTS:
        if a["name"] not in existing:
            conn.execute(
                """INSERT INTO agents
                   (id, name, description, system_prompt, icon, model, temperature, max_tokens,
                    role, goal, backstory, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), a["name"], a["description"], "",
                 a["icon"], None, a["temperature"], a["max_tokens"],
                 a["role"], a["goal"], a["backstory"], now, now),
            )
    conn.commit()
