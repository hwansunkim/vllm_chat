from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import config
from .. import state
from .schemas import ChatMessage, NewConversation, UpdateTitle
from ..core.agent import (
    build_agent_system_prompt,
    resolve_agent_mention,
    async_route_agent,
)
from ..core.memory import retrieve_memories, save_memories
from ..db.database import get_db
from ..llm.client import async_stream_chat
from ..llm.registry import get_registry
from ..llm.pipeline import (
    async_extract_keywords,
    async_extract_memories_from_turns,
    build_messages,
)

router = APIRouter(prefix="/api/conversations")


def get_active_turns(conn, conv_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM turns WHERE conversation_id=? AND archived=0 ORDER BY created_at",
        (conv_id,),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def save_turn(
    conn, conv_id: str, role: str, content: str,
    *, thinking="", memories_json=None, context_pct=None, prompt_tokens=None, max_tokens=None,
) -> str:
    tid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO turns
           (id, conversation_id, role, content, thinking, memories_json,
            context_pct, prompt_tokens, max_tokens, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (tid, conv_id, role, content, thinking or "", memories_json,
         context_pct, prompt_tokens, max_tokens, now),
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


@router.get("")
def list_conversations():
    conn = get_db()
    rows = conn.execute(
        """SELECT c.id, c.title, c.updated_at,
           (SELECT content FROM turns
            WHERE conversation_id=c.id ORDER BY created_at DESC LIMIT 1) AS last_msg
           FROM conversations c ORDER BY c.updated_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
def create_conversation(body: NewConversation):
    conn = get_db()
    cid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO conversations (id, title, system_prompt, agent_id, router_mode, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (cid, body.title, body.system_prompt, body.agent_id, int(body.router_mode), now, now),
    )
    conn.commit()
    conn.close()
    return {"id": cid, "title": body.title, "system_prompt": body.system_prompt, "agent_id": body.agent_id}


@router.get("/{conv_id}")
def get_conversation(conv_id: str):
    conn = get_db()
    conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        conn.close()
        raise HTTPException(404)
    turns = conn.execute(
        """SELECT role, content, thinking, memories_json, context_pct, prompt_tokens, max_tokens
           FROM turns WHERE conversation_id=? ORDER BY created_at""",
        (conv_id,),
    ).fetchall()
    conn.close()
    return {**dict(conv), "turns": [dict(t) for t in turns]}


@router.patch("/{conv_id}/title")
def update_title(conv_id: str, body: UpdateTitle):
    conn = get_db()
    conn.execute("UPDATE conversations SET title=? WHERE id=?", (body.title, conv_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.delete("/{conv_id}", status_code=204)
def delete_conversation(conv_id: str):
    conn = get_db()
    conn.execute("DELETE FROM turns WHERE conversation_id=?", (conv_id,))
    conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    conn.commit()
    conn.close()


@router.post("/{conv_id}/chat")
async def send_chat(conv_id: str, body: ChatMessage):
    conn = get_db()
    conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        conn.close()
        raise HTTPException(404)

    user_content, used_agent, routing_method, keywords = await _resolve_routing(
        body.content, conv, conn
    )
    if used_agent and routing_method == "mention" and not user_content:
        conn.close()
        raise HTTPException(400, "@mention 뒤에 메시지를 입력해주세요.")

    retrieved = retrieve_memories(conn, keywords)
    effective_system = (
        build_agent_system_prompt(used_agent) if used_agent else conv["system_prompt"]
    )
    active_turns = get_active_turns(conn, conv_id)
    active_turns.append({"role": "user", "content": user_content})
    messages = build_messages(effective_system, retrieved, active_turns)

    temperature = float(used_agent.get("temperature", 0.7)) if used_agent else 0.7
    max_tokens  = int(used_agent.get("max_tokens") or config.MAX_COMPLETION_TOKENS) if used_agent else config.MAX_COMPLETION_TOKENS
    model       = used_agent.get("model") if used_agent else None
    thinking    = body.thinking
    if thinking:
        max_tokens = max(max_tokens, config.MAX_COMPLETION_TOKENS_THINKING)

    try:
        selected_provider = get_registry().select(model=model)
    except RuntimeError as e:
        conn.close()
        raise HTTPException(503, str(e))

    async def event_generator():
        full_thinking = ""
        full_answer   = ""
        usage: dict   = {}

        try:
            async for event in async_stream_chat(
                messages, temperature=temperature, max_tokens=max_tokens,
                model=model, server_id=selected_provider.id, thinking=thinking
            ):
                if event["type"] == "thinking":
                    full_thinking += event["chunk"]
                    yield f"event: thinking\ndata: {json.dumps({'chunk': event['chunk']}, ensure_ascii=False)}\n\n"
                elif event["type"] == "answer":
                    full_answer += event["chunk"]
                    yield f"event: answer\ndata: {json.dumps({'chunk': event['chunk']}, ensure_ascii=False)}\n\n"
                elif event["type"] == "usage":
                    usage         = event["data"]
                    full_thinking = usage.get("thinking", full_thinking)
                    full_answer   = usage.get("answer",   full_answer)
        except Exception as e:
            msg = str(e) or f"({type(e).__name__})"
            yield f"event: error\ndata: {json.dumps({'message': f'LLM 오류: {msg}'}, ensure_ascii=False)}\n\n"
            conn.close()
            return

        max_model_len = selected_provider.model_len or usage.get("max_model_len") or state.max_model_len
        prompt_tokens = usage.get("prompt_tokens", 0)
        context_pct   = (prompt_tokens / max_model_len) if max_model_len else 0
        memories_json = json.dumps(
            [{"type": m["type"], "content": m["content"]} for m in retrieved],
            ensure_ascii=False,
        )

        save_turn(conn, conv_id, "user", user_content)
        save_turn(
            conn, conv_id, "assistant", full_answer,
            thinking=full_thinking,
            memories_json=memories_json,
            context_pct=round(context_pct, 4),
            prompt_tokens=prompt_tokens,
            max_tokens=max_model_len,
        )

        new_title = conv["title"]
        if conv["title"] == "새 대화":
            new_title = auto_title(body.content)
            conn.execute("UPDATE conversations SET title=? WHERE id=?", (new_title, conv_id))
            conn.commit()

        archived_count = await _maybe_archive(conn, conv_id, context_pct, max_model_len)
        conn.close()

        done = {
            "memories": [{"type": m["type"], "content": m["content"]} for m in retrieved],
            "usage": {
                "prompt_tokens":     prompt_tokens,
                "completion_tokens": usage.get("completion_tokens", 0),
                "max_model_len":     max_model_len,
                "context_pct":       context_pct,
                "thinking":          full_thinking,
            },
            "archived_count": archived_count,
            "title":          new_title,
            "used_agent": {
                "name":    used_agent["name"],
                "icon":    used_agent["icon"],
                "routing": routing_method,
            } if used_agent else None,
            "used_server": {
                "id":        selected_provider.id,
                "name":      selected_provider.name,
                "model":     selected_provider.model,
                "model_len": selected_provider.model_len,
            },
            "thinking_mode": thinking,
        }
        yield f"event: done\ndata: {json.dumps(done, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
