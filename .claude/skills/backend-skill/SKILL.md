---
name: backend-skill
description: "vLLM Chat 백엔드(FastAPI, SQLite, LLM 파이프라인) 개발 가이드. FastAPI 엔드포인트 추가/수정, DB 스키마 변경, LLM 연동 수정, RAG 메모리 시스템 작업 시 참조."
---

# Backend Development Guide — vLLM Chat

전체 구조는 **`docs/backend.md`**, DB 스키마는 **`docs/database.md`**, API 명세는
**`docs/api-reference.md`**, RAG 동작은 **`docs/memory-system.md`**. 이 문서는 패턴만.

## 레이아웃

```
backend/main.py        FastAPI 앱 · lifespan · 라우터 6개 등록 · 정적 프론트 서빙
backend/config.py      환경변수 · 임계값 상수 · 사고 수준
backend/state.py       전역: max_model_len, event_loop (브릿지가 사용)
backend/api/           model · conversations · agents · memories · servers · simulation/(패키지)
                       _conv_helpers.py = 채팅 파이프라인 헬퍼, schemas.py = 채팅 스키마
backend/core/          agent.py(라우팅·멘션) · memory.py(RAG 저장/검색)
backend/llm/           client.py(진입) · registry.py(서버 선택) · providers/ · pipeline.py · bridge.py
backend/websearch/     toggle 모드 웹 검색
backend/db/database.py memory.db — get_db · init_tables · migrate_db · seed
```

> 시뮬레이션 API는 `backend/api/simulation/` **패키지**다 (구 단일 파일 `simulation.py`
> 아님). 라우터가 여럿(`runtime/lifecycle.py`, `sse.py`, `runs.py`, `scenarios.py`,
> `contract.py` …)이고 `__init__.py`가 하나로 묶는다.

## 핵심 패턴

### 새 API 엔드포인트

```python
# backend/api/new_feature.py
from fastapi import APIRouter, HTTPException
from ..db.database import get_db

router = APIRouter(prefix="/api/new-feature", tags=["new-feature"])

@router.get("")
def get_new_feature():
    conn = get_db()
    try:
        rows = conn.execute("SELECT ...").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
```

`backend/main.py`에 등록:
```python
from .api import new_feature
app.include_router(new_feature.router)
```

### DB 스키마 변경 (하위호환 마이그레이션)

`backend/db/database.py`의 `migrate_db()`에 추가:
```python
cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()]
if "new_field" not in cols:
    conn.execute("ALTER TABLE conversations ADD COLUMN new_field TEXT")
conn.commit()
```
컬럼 삭제·이름 변경 금지. 전부 nullable/기본값.

### LLM 호출

```python
from ..llm.client import async_llm, async_stream_chat, async_chat

# 단발 프롬프트 (키워드/메모리 추출, 라우팅)
text = await async_llm(prompt, max_tokens=1000, temperature=0.7)

# 채팅 SSE — {type: "thinking"|"answer"|"usage", chunk/data} 이벤트 제너레이터
async for event in async_stream_chat(messages, temperature=0.7, max_tokens=4096,
                                     model=None, server_id=..., thinking_level=...):
    if event["type"] == "answer":
        yield f"event: answer\ndata: {json.dumps({'chunk': event['chunk']})}\n\n"

# 비스트리밍 멀티턴 (시뮬레이션 브릿지 전용) — (content, usage) 반환, usage["thinking"]
content, usage = await async_chat(messages, ...)
```

### Pydantic 스키마

채팅 도메인은 `backend/api/schemas.py`, 시뮬레이션은 `backend/api/simulation/schemas.py`.

## 아키텍처 원칙

| 원칙 | 방법 |
|---|---|
| DB 접근 | 항상 `get_db()` + `finally: conn.close()` (또는 명시적 close) |
| 비동기 | async/await 기본. 동기 블로킹은 `run_in_executor` |
| 에러 응답 | `HTTPException(status_code, detail="한국어 설명")` |
| 상태 공유 | `backend/state.py` 전역 (`max_model_len`, `event_loop`) |
| 설정 | `backend/config.py` (env + 상수) |

## RAG 메모리 흐름 (채팅)

```
POST /api/conversations/{id}/chat  →  send_chat (conversations.py)
  _resolve_routing()      멘션 / 라우터 모드 / 고정 에이전트  (_conv_helpers.py)
  async_extract_keywords()   LLM, 최대 7개 명사              (llm/pipeline.py)
  retrieve_memories()        키워드 교집합 COUNT + 최근접근    (core/memory.py)
  build_messages()           [system: 페르소나 + 관련 메모리 + 웹컨텍스트] + 최근 턴
  async_stream_chat()        SSE (search → thinking → answer → done)
  save_turn(user/assistant)  turns 테이블
  _maybe_archive()           context_pct >= 0.75 → 오래된 턴(최근 4 제외) 메모리화
```

## LLM 서버 레이어

- **서버 선택** (`registry.py: ServerRegistry.select`) — ①`server_id` 명시 ②`model` 매치
  라운드로빈 ③`is_default` 서버. 없으면 `NoProviderError`(503).
- **프로바이더 추가** — `backend/llm/providers/` 에 클래스 (OpenAI 호환이면
  `OpenAICompatibleProvider` 상속, 아니면 `LLMProvider` Protocol 직접 구현) +
  `registry.py: _PROVIDER_CLASSES`에 등록.
- **사고 수준** — 프로바이더 중립 `off/low/medium/high` → 각 프로바이더의
  `_thinking_body()` / `needs_thinking_headroom()`이 벤더 파라미터로 번역.
- **브릿지** (`bridge.py`) — 시뮬레이션 워커 스레드가 LLM을 쓰는 유일한 경로.
  `make_sync_chat()`이 동기 콜러블 생성. `state.event_loop`에 코루틴 위탁. tenacity 재시도.

## 에이전트 라우팅 (채팅)

- **멘션**: `@name` 접두사 → `resolve_agent_mention()` → 그 에이전트 시스템 프롬프트
- **라우터 모드**: `conversations.router_mode=1` → `async_route_agent()` (LLM이 이름 선택)
- **고정**: `conversations.agent_id`

## 주요 참조 포인터

- 채팅 파이프라인 헬퍼: `backend/api/_conv_helpers.py`
- 메모리 검색: `backend/core/memory.py`
- LLM 진입: `backend/llm/client.py`  · 서버 풀: `backend/llm/registry.py`
- 벤더 HTTP: `backend/llm/providers/{vllm,openai,anthropic,openai_compatible}.py`
- 시뮬레이션 실행 관리: `backend/api/simulation/runtime/lifecycle.py` + `runner.py`
