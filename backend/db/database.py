from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .. import config

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
            thinking        TEXT NOT NULL DEFAULT '',
            memories_json   TEXT,
            context_pct     REAL,
            prompt_tokens   INTEGER,
            max_tokens      INTEGER,
            sources_json    TEXT,
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
            -- 시뮬레이션(ABM) 에이전트와 공유되는 필드. 채팅 로직에서는 사용되지 않고
            -- 채팅 <-> 시뮬레이션 왕복 시 값이 유실되지 않도록 보존만 한다.
            gender             TEXT,
            "groups"           TEXT,     -- JSON 배열 문자열 (예: '["가족"]')
            location           TEXT,
            visual_description TEXT,
            display_name       TEXT,
            initial_active     BOOLEAN DEFAULT 1,
            relationships      TEXT,     -- JSON 객체 문자열 (예: '{"채민경": "아내"}')
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS servers (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            base_url      TEXT NOT NULL,
            model         TEXT NOT NULL,
            provider_type TEXT NOT NULL DEFAULT 'vllm',
            weight        INTEGER NOT NULL DEFAULT 1,
            enabled       INTEGER NOT NULL DEFAULT 1,
            is_default    INTEGER NOT NULL DEFAULT 0,
            thinking      INTEGER NOT NULL DEFAULT 0,
            thinking_level TEXT NOT NULL DEFAULT 'off',
            max_model_len INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS simulation_scenarios (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            config_json TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
    """)
    conn.commit()


def migrate_db(conn: sqlite3.Connection) -> None:
    conv_cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()]
    for col, ddl in [
        ("agent_id",    "ALTER TABLE conversations ADD COLUMN agent_id TEXT"),
        ("router_mode", "ALTER TABLE conversations ADD COLUMN router_mode INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in conv_cols:
            conn.execute(ddl)

    agent_cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
    for col in ("role", "goal", "backstory"):
        if col not in agent_cols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    # 시뮬레이션 에이전트와의 스키마 합집합. 전부 nullable/기본값이라 기존 row 는 영향 없음.
    for col, ddl in [
        ("gender",             'ALTER TABLE agents ADD COLUMN gender TEXT'),
        ("groups",             'ALTER TABLE agents ADD COLUMN "groups" TEXT'),
        ("location",           'ALTER TABLE agents ADD COLUMN location TEXT'),
        ("visual_description", 'ALTER TABLE agents ADD COLUMN visual_description TEXT'),
        ("display_name",       'ALTER TABLE agents ADD COLUMN display_name TEXT'),
        ("initial_active",     'ALTER TABLE agents ADD COLUMN initial_active BOOLEAN DEFAULT 1'),
        ("relationships",      'ALTER TABLE agents ADD COLUMN relationships TEXT'),
    ]:
        if col not in agent_cols:
            conn.execute(ddl)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "servers" not in tables:
        conn.execute("""
            CREATE TABLE servers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                model TEXT NOT NULL, provider_type TEXT NOT NULL DEFAULT 'vllm',
                weight INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1, is_default INTEGER NOT NULL DEFAULT 0,
                thinking INTEGER NOT NULL DEFAULT 0,
                thinking_level TEXT NOT NULL DEFAULT 'off',
                max_model_len INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
    else:
        server_cols = {r[1] for r in conn.execute("PRAGMA table_info(servers)").fetchall()}
        if "thinking" not in server_cols:
            conn.execute("ALTER TABLE servers ADD COLUMN thinking INTEGER NOT NULL DEFAULT 0")
        if "thinking_level" not in server_cols:
            # 구 bool 컬럼(thinking)을 4단계 문자열로 승격한다. 1 → 'medium'(기존
            # Anthropic budget 10000 에 가장 가까운 단계), 0 → 'off'.
            # `thinking` 컬럼은 남겨두되(외부/구코드 안전) 소스 오브 트루스는 thinking_level.
            conn.execute("ALTER TABLE servers ADD COLUMN thinking_level TEXT NOT NULL DEFAULT 'off'")
            conn.execute(
                "UPDATE servers SET thinking_level = CASE WHEN thinking THEN 'medium' ELSE 'off' END"
            )
        if "max_model_len" not in server_cols:
            conn.execute("ALTER TABLE servers ADD COLUMN max_model_len INTEGER NOT NULL DEFAULT 0")
        if "api_key" not in server_cols:
            conn.execute("ALTER TABLE servers ADD COLUMN api_key TEXT NOT NULL DEFAULT ''")
        if "provider_type" not in server_cols:
            conn.execute("ALTER TABLE servers ADD COLUMN provider_type TEXT NOT NULL DEFAULT 'vllm'")

    # NULL/오타/구버전 값이 섞여도 프로바이더 계층이 언제나 유효한 level 만 보게 한다.
    levels = ",".join(f"'{lv}'" for lv in config.THINKING_LEVELS)
    conn.execute(
        f"UPDATE servers SET thinking_level='off' "
        f"WHERE thinking_level IS NULL OR thinking_level NOT IN ({levels})"
    )
    # 파생 컬럼 재동기화. API 를 거치지 않는 외부 쓰기로 두 컬럼이 드리프트해도
    # (예: thinking_level='high' / thinking=0) 기동 시 thinking_level 기준으로 맞춘다.
    conn.execute(
        "UPDATE servers SET thinking = (thinking_level != 'off') "
        "WHERE thinking != (thinking_level != 'off')"
    )

    turn_cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}
    if "thinking" not in turn_cols:
        conn.execute("ALTER TABLE turns ADD COLUMN thinking TEXT NOT NULL DEFAULT ''")
    if "sources_json" not in turn_cols:
        conn.execute("ALTER TABLE turns ADD COLUMN sources_json TEXT")

    conn.commit()


def seed_default_servers(conn: sqlite3.Connection, path: str = "servers.json") -> None:
    existing = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
    if existing > 0:
        return
    seed_path = Path(path)
    if not seed_path.exists():
        return
    servers = json.loads(seed_path.read_text(encoding="utf-8"))
    now = datetime.now().isoformat()
    for s in servers:
        # seed 파일은 신규 thinking_level 과 구 thinking bool 을 모두 허용한다.
        level = config.normalize_thinking_level(
            s.get("thinking_level"),
            default=config.normalize_thinking_level(s.get("thinking")),
        )
        conn.execute(
            """INSERT INTO servers
               (id, name, base_url, model, provider_type, weight, enabled, is_default,
                thinking, thinking_level, max_model_len, created_at)
               VALUES (?,?,?,?,?,?,1,?,?,?,?,?)""",
            (str(uuid.uuid4()), s["name"], s["base_url"], s["model"],
             s.get("provider_type") or "vllm",
             s.get("weight", 1), int(s.get("is_default", False)),
             int(level != "off"), level, s.get("max_model_len", 0), now),
        )
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
