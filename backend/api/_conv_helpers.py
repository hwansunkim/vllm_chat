"""Helper functions for conversation routes (DB ops, routing, archiving)."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from .. import config
from ..core.agent import resolve_agent_mention, async_route_agent
from ..core.memory import save_memories
from ..llm.pipeline import (
    MemoryExtractionError,
    async_extract_keywords,
    async_extract_memories_from_turns,
)

logger = logging.getLogger(__name__)


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

    # 추출이 실패하면(LLM 다운·응답 잘림 등) 아카이브를 통째로 건너뛴다. 원문을
    # 활성 상태로 남겨야 다음 응답 때 다시 시도되고, 그 사이에도 최근 맥락으로
    # 계속 쓰인다. 실패를 무시하고 archived=1 로 넘기면 그 대화 구간이 이후 LLM
    # 입력·RAG 검색에서 영영 사라진다.
    try:
        new_mems = await async_extract_memories_from_turns(archive_turns)
    except MemoryExtractionError:
        logger.warning(
            "메모리 추출 실패 — conv=%s 아카이브를 건너뜁니다(원문 보존, 다음 기회 재시도)",
            conv_id, exc_info=True,
        )
        return 0

    # 메모리 저장 INSERT 와 원문 archived=1 UPDATE 를 한 트랜잭션으로 묶어 한 번에
    # 커밋한다 — 중간에 죽어도 "메모리만 저장되고 원문은 그대로" 또는 그 반대의
    # 부분 상태가 남지 않는다.
    if new_mems:
        save_memories(conn, new_mems, commit=False)
    conn.executemany("UPDATE turns SET archived=1 WHERE id=?", [(i,) for i in to_archive])
    conn.commit()
    return len(to_archive)
