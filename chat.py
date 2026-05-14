from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import requests

BASE_URL = "http://172.17.3.135:8000"
MODEL = "google/gemma-4-31B-it"
MAX_COMPLETION_TOKENS = 1024
WARN_THRESHOLD = 0.8
ARCHIVE_THRESHOLD = 0.75
KEEP_RECENT_TURNS = 4
MAX_RETRIEVED_MEMORIES = 5
DB_PATH = Path("memory.db")


# ── DB ───────────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
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
    """)
    conn.commit()


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
    top_k: int = MAX_RETRIEVED_MEMORIES,
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
            [(now, r[0]) for r in rows],
        )
        conn.commit()

    return [{"type": r[1], "content": r[2], "match_count": r[3]} for r in rows]


# ── LLM helpers ──────────────────────────────────────────────────────────────

def _llm(prompt: str, max_tokens: int = 512, temperature: float = 0.1) -> str:
    response = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _parse_json(text: str, array: bool = False) -> list | dict | None:
    pattern = r"\[.*\]" if array else r"\{.*\}"
    match = re.search(pattern, text, re.DOTALL)
    try:
        return json.loads(match.group() if match else text)
    except Exception:
        return [] if array else None


def extract_keywords(text: str) -> list[str]:
    prompt = f"""\
다음 텍스트에서 핵심 키워드를 추출하세요.
명사, 고유명사, 기술 용어 위주로 최대 7개 이내로 추출합니다.
JSON 배열로만 응답하세요. 다른 텍스트 없이 JSON만 출력하세요.

텍스트: {text}

예시: ["서버", "포트", "모델이름"]"""

    try:
        result = _parse_json(_llm(prompt), array=True)
        return result if isinstance(result, list) else []
    except Exception:
        return []


def extract_memories_from_turns(turns: list[dict]) -> list[dict]:
    conversation = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in turns
    )
    prompt = f"""\
다음 대화에서 나중에 참조할 만한 새로운 정보를 추출하세요.

[대화]
{conversation}

type 종류:
- fact: 사실, 수치, 설정값, 고유명사
- decision: 결정된 사항
- pending: 미결 또는 진행 중인 항목

keywords는 이 항목을 나중에 검색할 때 쓸 핵심 단어 (최대 5개).
새로운 정보가 없으면 빈 배열 []을 반환하세요.

JSON 배열로만 응답하세요:
[
  {{"type": "fact", "content": "...", "keywords": ["...", "..."]}},
  {{"type": "decision", "content": "...", "keywords": ["...", "..."]}}
]"""

    try:
        result = _parse_json(_llm(prompt, max_tokens=1024), array=True)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ── Context building ──────────────────────────────────────────────────────────

def build_messages(
    system_prompt: str,
    retrieved: list[dict],
    recent_turns: list,
) -> list:
    parts = [system_prompt] if system_prompt else []

    if retrieved:
        mem_lines = "\n".join(f"[{m['type']}] {m['content']}" for m in retrieved)
        parts.append(f"[관련 메모리]\n{mem_lines}")

    msgs = []
    if parts:
        msgs.append({"role": "system", "content": "\n\n".join(parts)})
    msgs.extend(recent_turns)
    return msgs


# ── Chat ─────────────────────────────────────────────────────────────────────

def chat(messages: list) -> tuple[str, dict]:
    response = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": messages,
            "max_tokens": MAX_COMPLETION_TOKENS,
            "temperature": 0.7,
        },
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"], data.get("usage", {})


# ── Context status ────────────────────────────────────────────────────────────

def display_context_status(usage: dict, max_model_len: int) -> None:
    if not max_model_len or not usage:
        return

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    remaining = max_model_len - prompt_tokens - MAX_COMPLETION_TOKENS
    pct = prompt_tokens / max_model_len

    bar_len = 32
    filled = int(bar_len * pct)
    bar = "█" * filled + "░" * (bar_len - filled)

    icon = "⚠️ " if pct >= WARN_THRESHOLD else "✅"
    print(f"{icon} 컨텍스트: [{bar}] {pct * 100:.1f}%")
    print(f"   사용: {prompt_tokens:,} / {max_model_len:,} 토큰 | 가용: {remaining:,} 토큰")
    print(f"   (입력 누적: {prompt_tokens:,} | 이번 출력: {completion_tokens:,})\n")


# ── Archiving ─────────────────────────────────────────────────────────────────

def maybe_archive(
    conn: sqlite3.Connection,
    usage: dict,
    max_model_len: int,
    recent_turns: list,
) -> list:
    if not max_model_len or not usage:
        return recent_turns

    pct = usage.get("prompt_tokens", 0) / max_model_len
    if pct < ARCHIVE_THRESHOLD:
        return recent_turns

    to_archive = recent_turns[:-KEEP_RECENT_TURNS]
    if not to_archive:
        print("  [경고: 보존 턴만 남아 더 이상 압축할 수 없습니다]\n")
        return recent_turns

    print("  [오래된 대화를 메모리에 저장 중...]\n")
    new_items = extract_memories_from_turns(to_archive)
    if new_items:
        save_memories(conn, new_items)
        print(f"  [메모리 {len(new_items)}건 저장 완료]\n")
    else:
        print("  [저장할 새 정보 없음 — 오래된 턴 제거]\n")

    return recent_turns[-KEEP_RECENT_TURNS:]


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def get_model_context_limit() -> int:
    try:
        data = requests.get(f"{BASE_URL}/v1/models", timeout=5).json()
        for m in data.get("data", []):
            if m["id"] == MODEL:
                return m.get("max_model_len", 0)
    except Exception:
        pass
    return 0


def main():
    print("Gemma 4 31B 채팅 (종료: 'exit' 또는 'quit')")
    print(f"서버: {BASE_URL}\n")

    try:
        requests.get(f"{BASE_URL}/health", timeout=3).raise_for_status()
    except Exception:
        print("서버에 연결할 수 없습니다. vLLM이 실행 중인지 확인하세요.")
        return

    max_model_len = get_model_context_limit()
    if max_model_len:
        print(f"모델 컨텍스트 한도: {max_model_len:,} 토큰\n")
    else:
        print("컨텍스트 한도를 가져올 수 없습니다. 토큰 모니터링이 비활성화됩니다.\n")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    system_prompt = input("시스템 프롬프트 (없으면 Enter): ").strip()
    print()

    recent_turns: list = []

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("종료합니다.")
            break

        # 1. 키워드 추출 → 관련 메모리 검색
        keywords = extract_keywords(user_input)
        retrieved = retrieve_memories(conn, keywords)
        if retrieved:
            labels = [m["content"][:30] + ("..." if len(m["content"]) > 30 else "") for m in retrieved]
            print(f"  [메모리 {len(retrieved)}건 로드] {' / '.join(labels)}\n")

        # 2. 메시지 구성 및 응답
        recent_turns.append({"role": "user", "content": user_input})
        messages = build_messages(system_prompt, retrieved, recent_turns)

        try:
            reply, usage = chat(messages)
            recent_turns.append({"role": "assistant", "content": reply})
            print(f"\nGemma: {reply}\n")
            display_context_status(usage, max_model_len)

            # 3. 임계값 초과 시 오래된 턴을 메모리로 아카이브
            recent_turns = maybe_archive(conn, usage, max_model_len, recent_turns)

        except requests.HTTPError as e:
            print(f"API 오류: {e}\n")
            recent_turns.pop()
        except Exception as e:
            print(f"오류: {e}\n")
            recent_turns.pop()

    conn.close()


if __name__ == "__main__":
    main()
