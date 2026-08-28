"""Helper functions for conversation routes (DB ops, routing, archiving)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from .. import config
from ..core.agent import resolve_agent_mention, async_route_agent
from ..core.memory import save_memories
from ..llm.pipeline import async_extract_keywords, async_extract_memories_from_turns


def get_active_turns(conn, conv_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM turns WHERE conversation_id=? AND archived=0 ORDER BY created_at",
        (conv_id,),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def save_turn(
    conn, conv_id: str, role: str, content: str,
    *, thinking="", memories_json=None, context_pct=None, prompt_tokens=None, max_tokens=None,
    sources_json=None,
) -> str:
    tid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO turns
           (id, conversation_id, role, content, thinking, memories_json,
            context_pct, prompt_tokens, max_tokens, sources_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, conv_id, role, content, thinking or "", memories_json,
         context_pct, prompt_tokens, max_tokens, sources_json, now),
    )
    conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
    conn.commit()
    return tid


def auto_title(text: str) -> str:
    return text[:30].strip() + ("..." if len(text) > 30 else "")


async def _resolve_routing(
    content: str, conv, conn
) -> tuple[str, dict | None, str, list[str]]:
    user_content, used_agent = resolve_agent_mention(content, conn)
    routing_method = "none"
    keywords: list[str] = []

    if used_agent:
        routing_method = "mention"
        if user_content:
            keywords = await async_extract_keywords(user_content)
    elif conv["router_mode"]:
        all_agents = [dict(r) for r in conn.execute("SELECT * FROM agents").fetchall()]
        if all_agents:
            keywords, (used_agent, routing_method) = await asyncio.gather(
                async_extract_keywords(user_content),
                async_route_agent(content, all_agents),
            )
        else:
            keywords = await async_extract_keywords(user_content)
    else:
        if conv["agent_id"]:
            row = conn.execute("SELECT * FROM agents WHERE id=?", (conv["agent_id"],)).fetchone()
            if row:
                used_agent = dict(row)
                routing_method = "fixed"
        keywords = await async_extract_keywords(user_content)

    return user_content, used_agent, routing_method, keywords


async def _maybe_archive(conn, conv_id: str, context_pct: float, max_model_len: int) -> int:
    if not (max_model_len and context_pct >= config.ARCHIVE_THRESHOLD):
        return 0

    active_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM turns WHERE conversation_id=? AND archived=0 ORDER BY created_at",
            (conv_id,),
        ).fetchall()
    ]
    to_archive = active_ids[:-config.KEEP_RECENT_TURNS]
    if not to_archive:
        return 0

    ph = ",".join("?" * len(to_archive))
    archive_turns = [
        {"role": r["role"], "content": r["content"]}
        for r in conn.execute(
            f"SELECT role, content FROM turns WHERE id IN ({ph})", to_archive
        ).fetchall()
    ]
    new_mems = await async_extract_memories_from_turns(archive_turns)
    if new_mems:
        save_memories(conn, new_mems)
    conn.executemany("UPDATE turns SET archived=1 WHERE id=?", [(i,) for i in to_archive])
    conn.commit()
    return len(to_archive)
