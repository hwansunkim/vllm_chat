from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

import config


def save_memories(conn: sqlite3.Connection, items: list[dict]) -> None:
    now = datetime.now().isoformat()
    for item in items:
        mid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO memories (id, type, content, created_at, last_accessed) VALUES (?,?,?,?,?)",
            (mid, item["type"], item["content"], now, now),
        )
        for kw in item.get("keywords", []):
            conn.execute(
                "INSERT INTO memory_keywords (memory_id, keyword) VALUES (?,?)",
                (mid, kw.lower()),
            )
    conn.commit()


def retrieve_memories(
    conn: sqlite3.Connection,
    keywords: list[str],
    top_k: int = config.MAX_RETRIEVED_MEMORIES,
) -> list[dict]:
    if not keywords:
        return []

    placeholders = ",".join("?" * len(keywords))
    lower_kws = [k.lower() for k in keywords]

    rows = conn.execute(
        f"""
        SELECT m.id, m.type, m.content, COUNT(mk.keyword) AS match_count
        FROM memories m
        JOIN memory_keywords mk ON m.id = mk.memory_id
        WHERE mk.keyword IN ({placeholders})
        GROUP BY m.id
        ORDER BY match_count DESC, m.last_accessed DESC
        LIMIT ?
        """,
        (*lower_kws, top_k),
    ).fetchall()

    if rows:
        now = datetime.now().isoformat()
        conn.executemany(
            "UPDATE memories SET last_accessed=? WHERE id=?",
            [(now, r["id"]) for r in rows],
        )
        conn.commit()

    return [{"type": r[1], "content": r[2], "match_count": r[3]} for r in rows]
