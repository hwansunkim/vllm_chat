from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import chat as core

MAX_MODEL_LEN: int = 0


# ── DB ───────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(core.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn: sqlite3.Connection) -> None:
    core.init_db(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            system_prompt TEXT NOT NULL DEFAULT '',
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
    """)
    conn.commit()


# ── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global MAX_MODEL_LEN
    conn = get_db()
    init_tables(conn)
    conn.close()
    MAX_MODEL_LEN = core.get_model_context_limit()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("static/index.html")


# ── Pydantic models ──────────────────────────────────────────────────────────

class NewConversation(BaseModel):
    system_prompt: str = ""
    title: str = "새 대화"


class ChatMessage(BaseModel):
    content: str


class UpdateTitle(BaseModel):
    title: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_active_turns(conn: sqlite3.Connection, conv_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM turns WHERE conversation_id=? AND archived=0 ORDER BY created_at",
        (conv_id,),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def save_turn(
    conn: sqlite3.Connection,
    conv_id: str,
    role: str,
    content: str,
    *,
    memories_json: str | None = None,
    context_pct: float | None = None,
    prompt_tokens: int | None = None,
    max_tokens: int | None = None,
) -> str:
    tid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO turns
           (id, conversation_id, role, content, memories_json,
            context_pct, prompt_tokens, max_tokens, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (tid, conv_id, role, content, memories_json,
         context_pct, prompt_tokens, max_tokens, now),
    )
    conn.execute(
        "UPDATE conversations SET updated_at=? WHERE id=?",
        (now, conv_id),
    )
    conn.commit()
    return tid


def auto_title(text: str) -> str:
    return text[:30].strip() + ("..." if len(text) > 30 else "")


# ── API: model ───────────────────────────────────────────────────────────────

@app.get("/api/model/status")
def model_status():
    return {
        "model": core.MODEL,
        "base_url": core.BASE_URL,
        "max_model_len": MAX_MODEL_LEN,
    }


# ── API: conversations ───────────────────────────────────────────────────────

@app.get("/api/conversations")
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


@app.post("/api/conversations", status_code=201)
def create_conversation(body: NewConversation):
    conn = get_db()
    cid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO conversations (id, title, system_prompt, created_at, updated_at) VALUES (?,?,?,?,?)",
        (cid, body.title, body.system_prompt, now, now),
    )
    conn.commit()
    conn.close()
    return {"id": cid, "title": body.title, "system_prompt": body.system_prompt}


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str):
    conn = get_db()
    conv = conn.execute(
        "SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        conn.close()
        raise HTTPException(404)
    turns = conn.execute(
        """SELECT role, content, memories_json, context_pct, prompt_tokens, max_tokens
           FROM turns WHERE conversation_id=? ORDER BY created_at""",
        (conv_id,),
    ).fetchall()
    conn.close()
    return {**dict(conv), "turns": [dict(t) for t in turns]}


@app.patch("/api/conversations/{conv_id}/title")
def update_title(conv_id: str, body: UpdateTitle):
    conn = get_db()
    conn.execute("UPDATE conversations SET title=? WHERE id=?",
                 (body.title, conv_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/conversations/{conv_id}", status_code=204)
def delete_conversation(conv_id: str):
    conn = get_db()
    conn.execute("DELETE FROM turns WHERE conversation_id=?", (conv_id,))
    conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    conn.commit()
    conn.close()


# ── API: chat ────────────────────────────────────────────────────────────────

@app.post("/api/conversations/{conv_id}/chat")
def send_chat(conv_id: str, body: ChatMessage):
    conn = get_db()
    conv = conn.execute(
        "SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        conn.close()
        raise HTTPException(404)

    # 1. 키워드 추출 → 메모리 검색
    keywords = core.extract_keywords(body.content)
    retrieved = core.retrieve_memories(conn, keywords)

    # 2. recent turns 구성 및 LLM 호출
    active_turns = get_active_turns(conn, conv_id)
    active_turns.append({"role": "user", "content": body.content})
    messages = core.build_messages(
        conv["system_prompt"], retrieved, active_turns)

    try:
        reply, usage = core.chat(messages)
    except Exception as e:
        conn.close()
        raise HTTPException(502, f"LLM 오류: {e}")

    prompt_tokens = usage.get("prompt_tokens", 0)
    context_pct = (prompt_tokens / MAX_MODEL_LEN) if MAX_MODEL_LEN else 0
    memories_json = json.dumps(
        [{"type": m["type"], "content": m["content"]} for m in retrieved],
        ensure_ascii=False,
    )

    # 3. DB 저장
    save_turn(conn, conv_id, "user", body.content)
    save_turn(
        conn, conv_id, "assistant", reply,
        memories_json=memories_json,
        context_pct=round(context_pct, 4),
        prompt_tokens=prompt_tokens,
        max_tokens=MAX_MODEL_LEN,
    )

    # 4. 첫 메시지 시 자동 타이틀
    new_title = conv["title"]
    if conv["title"] == "새 대화":
        new_title = auto_title(body.content)
        conn.execute("UPDATE conversations SET title=? WHERE id=?",
                     (new_title, conv_id))
        conn.commit()

    # 5. 임계값 초과 시 아카이브
    archived_count = 0
    if MAX_MODEL_LEN and context_pct >= core.ARCHIVE_THRESHOLD:
        active_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM turns WHERE conversation_id=? AND archived=0 ORDER BY created_at",
                (conv_id,),
            ).fetchall()
        ]
        to_archive = active_ids[:-core.KEEP_RECENT_TURNS]
        if to_archive:
            ph = ",".join("?" * len(to_archive))
            archive_turns = [
                {"role": r["role"], "content": r["content"]}
                for r in conn.execute(
                    f"SELECT role, content FROM turns WHERE id IN ({ph})", to_archive
                ).fetchall()
            ]
            new_mems = core.extract_memories_from_turns(archive_turns)
            if new_mems:
                core.save_memories(conn, new_mems)
            conn.executemany(
                "UPDATE turns SET archived=1 WHERE id=?", [
                    (i,) for i in to_archive]
            )
            conn.commit()
            archived_count = len(to_archive)

    conn.close()
    return {
        "reply": reply,
        "memories": [{"type": m["type"], "content": m["content"]} for m in retrieved],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": usage.get("completion_tokens", 0),
            "max_model_len": MAX_MODEL_LEN,
            "context_pct": context_pct,
        },
        "archived_count": archived_count,
        "title": new_title,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8888, reload=True)
