# 백엔드 — 채팅 · LLM 레이어 · 인프라

> FastAPI 앱, 채팅 파이프라인, LLM 서버 레지스트리/프로바이더, 워커 스레드 브릿지,
> 웹 검색. 시뮬레이션 API는 [`simulation-overview.md`](simulation-overview.md)에서 다룬다.
> 호칭은 [`glossary.md` Part B](glossary.md#part-b--백엔드-fastapi).

---

## 1. 앱 구성 (`backend/main.py`)

```python
@asynccontextmanager
async def lifespan(app):
    conn = get_db(); init_tables(conn); migrate_db(conn)
    seed_default_agents(conn); seed_default_servers(conn)
    await llm_client.setup(conn)          # ServerRegistry.load_from_db
    conn.close()
    state.max_model_len = await llm_client.async_get_model_context_limit()
    state.event_loop = asyncio.get_running_loop()   # ← 브릿지가 쓴다
    yield
    state.event_loop = None
    await llm_client.teardown(); await close_provider()
```

- 라우터 6개 등록: `model` · `conversations` · `agents` · `memories` · `servers` · `simulation`
- `app.mount("/", StaticFiles(directory="frontend", html=True))` — API 아래 경로가 아니면
  프론트 정적 파일. 즉 **백엔드가 프론트도 서빙**한다 (별도 웹서버 없음).
- `lifespan`이 **메인 이벤트 루프 참조**를 `state.event_loop`에 저장하는 것이 핵심이다 —
  시뮬레이션 워커 스레드가 이 참조로 프로바이더 코루틴을 원래 루프에 위탁한다([§4](#4-브릿지--워커-스레드--메인-루프)).

---

## 2. 채팅 파이프라인 — `POST /api/conversations/{id}/chat`

한 요청의 전체 흐름 (`backend/api/conversations.py: send_chat` + `_conv_helpers.py`):

```
요청 { content, thinking_level, web_search }
  │
  1. 대화 조회 (없으면 404)
  │
  2. _resolve_routing(content, conv, conn)  →  (user_content, used_agent, routing_method, keywords)
  │     ├ "@이름 ..." 접두사      → resolve_agent_mention  (routing_method="mention")
  │     ├ conv.router_mode = 1   → async_route_agent (LLM이 에이전트 선택, "router"/"fallback")
  │     └ conv.agent_id 고정      → 그 에이전트 ("fixed")
  │     · 어느 경로든 async_extract_keywords(user_content) 로 키워드 추출
  │       (router 모드는 라우팅과 키워드 추출을 asyncio.gather 로 병렬)
  │
  3. retrieve_memories(conn, keywords)     RAG — memory-system.md
  │
  4. effective_system  =  build_agent_system_prompt(used_agent)  또는  conv.system_prompt
  │  active_turns      =  archived=0 인 턴들  +  {role:user, content:user_content}
  │  messages          =  build_messages(effective_system, retrieved, active_turns)
  │
  5. 서버 선택   get_registry().select(model=used_agent.model)      (NoProviderError → 503)
  │  사고 수준   effective_level = body.thinking_level ?? provider.thinking_level
  │  헤드룸      provider.needs_thinking_headroom(effective_level) → max_tokens 상향
  │
  6. save_turn(user)      user 턴 즉시 저장
  │
  7. StreamingResponse (text/event-stream):
  │     · web_search 켜짐 → async_build_search_query → web_search → event: search
  │                          messages 재조립 (web_context 추가)
  │     · async_stream_chat(messages, …):
  │           event: thinking  (청크)
  │           event: answer    (청크)
  │           event: usage     (prompt_tokens 등)
  │     · context_pct = prompt_tokens / max_model_len
  │     · save_turn(assistant, thinking, memories_json, context_pct, sources_json)
  │     · conv.title == "새 대화" → auto_title (앞 30자)
  │     · _maybe_archive(conn, conv_id, context_pct, max_model_len)   → memory-system.md
  │     event: done  { memories, usage, archived_count, title, sources, used_agent, used_server, thinking_level }
  │
  예외 → event: error { message }
```

에러 격리: 웹 검색 단계의 어떤 실패도 삼키고 검색 없이 답변을 진행한다. LLM 스트림
예외는 `event: error`로 내보내고 종료.

관련 헬퍼 (`_conv_helpers.py`):

| 함수 | 역할 |
|---|---|
| `get_active_turns` | `archived=0` 턴만 (오래된 순) |
| `save_turn` | 턴 INSERT + `conversations.updated_at` 갱신 |
| `auto_title` | 첫 메시지 앞 30자 |
| `_resolve_routing` | 위 2단계 |
| `_maybe_archive` | 컨텍스트 75%↑ → 오래된 턴(최근 4 제외) 메모리화 |

에이전트 라우팅 (`backend/core/agent.py`):

| 방식 | 함수 | 동작 |
|---|---|---|
| **멘션** | `resolve_agent_mention` | `^@(\S+)\s*(.*)` 정규식. 매칭 에이전트의 시스템 프롬프트 사용 |
| **라우터 모드** | `async_route_agent` | 에이전트 설명 목록 + 요청을 LLM에 주고 이름 하나 선택 (`max_tokens=30, temperature=0`). 실패 시 첫 에이전트 fallback |
| **고정** | — | `conversations.agent_id` |
| **시스템 프롬프트 조립** | `build_agent_system_prompt` | `role`/`goal`/`backstory` 있으면 조합, 없으면 `system_prompt` |

---

## 3. LLM 레이어 (`backend/llm/`)

```
conversations.py ─┐
core/*.py         ├─► client.py ──► registry.py ──► providers/*  ──► LLM 서버
ABM (via bridge) ─┘   (진입 함수)   (서버 선택)      (벤더별 HTTP)
```

### 3.1 진입 함수 (`client.py`)

| 함수 | 용도 |
|---|---|
| `async_llm(prompt, max_tokens, temperature)` | 단발 프롬프트 (키워드/메모리 추출, 라우팅, 시간 분류) |
| `async_stream_chat(messages, …)` | 채팅 SSE — `thinking`/`answer`/`usage` 이벤트 제너레이터 |
| `async_chat(messages, …)` | 비스트리밍 멀티턴. `usage`에 `"thinking"` 키 포함. **유일한 호출자 = 시뮬레이션(브릿지)** |
| `async_get_model_context_limit()` | 기본 서버의 컨텍스트 길이 (부팅 시 `state.max_model_len`) |

### 3.2 서버 레지스트리 (`registry.py`)

`ServerRegistry` — `enabled=1`인 서버 행으로 프로바이더를 만들어 풀에 보관.
`get_registry()`가 프로세스 싱글턴.

**선택 우선순위 — `select(model=None, server_id=None)`**

1. `server_id` 명시 → 해당 프로바이더 (`enabled` 무시)
2. `model` 명시 → 그 모델을 서빙하는 `enabled` 프로바이더들 중 **라운드로빈** (`servers.weight`는 미사용, 균등)
3. `get_default()` → `is_default=1` 프로바이더, 없으면 첫 `enabled`
4. 아무것도 없음 → `NoProviderError` (`RuntimeError` 서브클래스, 브릿지 재시도가 즉시 포기)

`register` / `unregister` — 서버 모달에서 CRUD 시 `asyncio.Lock`으로 풀 갱신.

### 3.3 프로바이더 (`providers/`)

`LLMProvider`는 `Protocol` (`base.py`). 계약: `chat` · `llm` · `stream_chat` ·
`needs_thinking_headroom` · `health_status` · `list_models` · `fetch_model_len` · `close`.

```
LLMProvider (Protocol)
├── OpenAICompatibleProvider   /v1/chat/completions 공통 구현
│   ├── VLLMProvider           GET /health,  _thinking_body → chat_template_kwargs.enable_thinking
│   └── OpenAIProvider         /v1/models 로 생존 확인,  MAX_TOKENS_PARAM = max_completion_tokens
│                              gpt-5/o-계열 → reasoning_effort, temperature 생략
└── AnthropicProvider          Messages API (OpenAI 비호환) — 직접 구현
                               system 롤 분리, user/assistant 교대 강제, thinking.budget_tokens
```

`OpenAICompatibleProvider.chat()` 세부:

- **continuation 루프** — `finish_reason == "length"`면 최대 `MAX_CONTINUATION_ROUNDS`(5)회
  이어 생성.
- **컨텍스트 초과 자동 축소** — 400 응답에서 `maximum context length is N` 파싱 →
  `max_tokens` 낮춰 1회 재시도, 감지된 `model_len` 저장.
- **thinking 파싱** (`_extract_reply`) — ① `reasoning_content` 별도 필드 (vLLM 0.6+, Qwen3)
  ② `content` 안 `<think>...</think>` 태그.
- 예외 정규화 — `HTTPStatusError` → `LLMHTTPError(status_code)`, 타임아웃/연결오류 → `RuntimeError`.

### 3.4 사고(thinking) 수준 번역

프로바이더 중립 4단계 `off` / `low` / `medium` / `high` (`config.THINKING_LEVELS`).
`_effective_level()` = 요청값 `??` 서버 기본값(`provider.thinking_level`).

| 프로바이더 | `off` | `low`/`medium`/`high` | 헤드룸 |
|---|---|---|---|
| **vLLM** | 파라미터 없음 | `chat_template_kwargs: {enable_thinking: true, reasoning_effort: <level>}` (템플릿이 모르는 키는 무시) | `level != off` |
| **OpenAI 일반** (gpt-4o 등) | — | **요청 바디 안 바뀜** (`reasoning_effort` 400 → 조용한 no-op) | 불필요 |
| **OpenAI 추론** (gpt-5, o1/o3/o4) | reasoning 못 끔 → 파라미터 생략 (서버 기본 medium) | `reasoning_effort: <level>` (`o1-mini`/`o1-preview` 제외) | **항상 필요** — `off`여도 4096을 reasoning이 먹어 빈 답변 방지 |
| **Anthropic** | thinking 미전송 | `thinking: {budget_tokens: <표>}` — low 2048 / medium 8192 / high 24576 | `level != off` |

헤드룸이 필요하면 `max_tokens = max(max_tokens, MAX_COMPLETION_TOKENS_THINKING=16384)`.

### 3.5 파이프라인 (`pipeline.py`)

| 함수 | 프롬프트 | 반환 |
|---|---|---|
| `async_extract_keywords(text)` | "핵심 키워드 최대 7개, JSON 배열만" | `["...", ...]` (실패 시 `[]`) |
| `async_extract_memories_from_turns(turns)` | "나중에 참조할 정보 추출, type: fact/decision/pending" | `[{type, content, keywords}]` |
| `build_messages(system, retrieved, recent_turns, web_context)` | — | `[{role:system, content: "페르소나\n\n[관련 메모리]\n...\n\n<웹컨텍스트>"}] + recent_turns` |

---

## 4. 브릿지 — 워커 스레드 → 메인 루프 (`backend/llm/bridge.py`)

**문제**: ABM 시뮬레이션은 이벤트 루프 밖의 OS 스레드에서 돈다. 프로바이더의
`httpx.AsyncClient`는 `lifespan()`의 메인 루프에 바인딩돼 있어, 워커 스레드가 새 루프
(`asyncio.run`)로 호출하면 *"attached to a different loop"*로 깨진다.

**해결**:

```python
def run_sync(coro, *, timeout):
    loop = state.event_loop                       # lifespan이 저장한 참조
    if loop is None: raise LoopNotReadyError
    future = asyncio.run_coroutine_threadsafe(coro, loop)   # 메인 루프에서 실행
    return future.result(timeout=timeout)         # 워커 스레드는 여기서 블록
```

`make_sync_chat(server_id, timeout, temperature, thinking_level)` — ABM이 쓰는
**동기 콜러블** `(messages, *, max_tokens) -> (content, reasoning, usage)`을 만든다:

- 프로바이더는 호출 시점마다 레지스트리에서 lazy select — 실행 중 서버 설정이 바뀌어도
  닫힌 클라이언트를 붙들지 않는다.
- `thinking_level=None`이 기본 = "선택된 서버의 기본 사고 수준 상속". **시뮬레이션에는
  별도 사고 UI가 없다** — 서버 설정을 그대로 따른다.
- **tenacity 재시도** — `stop_after_attempt(3)`, 지수 백오프(2~10s). `_is_retryable`:
  - 재시도 O: `LLMHTTPError` 408/429/5xx, 연결오류·타임아웃(`RuntimeError`)
  - 재시도 X (영구 실패): `NoProviderError`, `LoopNotReadyError`, `LLMHTTPError` 400
- 가드 타임아웃 = `timeout * MAX_CONTINUATION_ROUNDS + 30` (continuation 루프 배수 고려).

---

## 5. 웹 검색 (`backend/websearch/`)

```
conversations.send_chat  (body.web_search && WEB_SEARCH_MODE=="toggle")
  → async_build_search_query(user_content, prior_turns)   쿼리 재작성 (WEB_SEARCH_REWRITE_QUERY)
  → web_search(query)  →  service.get_provider().search()  →  DuckDuckGoProvider (HTML 파싱, 키 불필요)
  → format_search_context(results)   →  build_messages(web_context=...)
```

| 파일 | 역할 |
|---|---|
| `service.py` | `get_provider()` (config로 분기, 싱글턴), `web_search()` (실패 → `[]`) |
| `query.py` | `async_build_search_query` — 대화 맥락으로 검색어 재작성 |
| `context.py` | `format_search_context` — 결과를 프롬프트용 텍스트로 |
| `providers/duckduckgo.py` | HTML 파싱 검색 (현재 유일) |
| `schemas.py` | `SearchResult {title, url, snippet}` |

`WEB_SEARCH_MODE`는 현재 `"toggle"`(프론트 🔍 버튼)만 구현. `"tool_calling"`은 미구현
확장점 (`config.py` 주석에 전환 절차).

---

## 6. 나머지 라우터

| 라우터 | 엔드포인트 | 메모 |
|---|---|---|
| `model.py` | `GET /api/model/status` | 기본 서버의 `model` · `base_url` · `max_model_len` |
| `agents.py` | `GET/POST /api/agents`, `GET/PUT/DELETE /api/agents/{id}` | 채팅 에이전트. `POST`에 `role/goal/backstory` 주면 시스템 프롬프트 자동 생성 가능 |
| `memories.py` | `GET /api/memories` (type·검색 필터), `DELETE /api/memories/{id}` | RAG 메모리 저장소 |
| `servers.py` | `GET/POST /api/servers`, `GET/PUT/DELETE /{id}`, `POST /probe-models`, `GET /{id}/health` | `probe-models` = 저장 전 `/v1/models` 조회. `health` = 3-state (`ok`/`model_missing`/`unreachable`) |

상세 요청·응답은 [`api-reference.md`](api-reference.md).

---

## 7. 설정 (`backend/config.py`)

| 상수 | 값 | 의미 |
|---|---|---|
| `MAX_COMPLETION_TOKENS` | 4096 | 기본 출력 상한 |
| `MAX_COMPLETION_TOKENS_THINKING` | 16384 | 사고 켜졌을 때 상향값 |
| `MAX_CONTINUATION_ROUNDS` | 5 | `finish_reason=length` 이어 생성 횟수 |
| `THINKING_LEVELS` | `(off, low, medium, high)` | 프로바이더 중립 4단계 |
| `THINKING_BUDGET_BY_LEVEL` | low 2048 / medium 8192 / high 24576 | Anthropic budget_tokens |
| `ARCHIVE_THRESHOLD` | 0.75 | 아카이브 트리거 (memory-system.md) |
| `KEEP_RECENT_TURNS` | 4 | 아카이브 후 유지 턴 수 |
| `MAX_RETRIEVED_MEMORIES` | 5 | 주입 메모리 상한 |
| `DB_PATH` | `memory.db` | 채팅 DB |
| `WEB_SEARCH_MODE` / `_PROVIDER` / `_MAX_RESULTS` / `_TIMEOUT` / `_REWRITE_QUERY` | env | 웹 검색 |
| `VLLM_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | env | 서버 행에 키가 없을 때 폴백 |

`normalize_thinking_level(value, default)` — `bool`/`int`/`str`/`None`을 정규 레벨
문자열로. 구 `thinking` INTEGER 컬럼(참→`medium`) 호환.

---

## 8. DB 접근 규약 (`backend/db/database.py`)

- **항상 `get_db()` + 명시적 close** (또는 finally). `sqlite3.Row` 팩토리.
- **마이그레이션은 하위호환** — `migrate_db()`에서 `try: ALTER TABLE … ADD COLUMN … except: pass`.
- 시드 — `servers.json`이 있으면 첫 실행 시 `servers` 테이블에, 기본 채팅 에이전트
  3종(planner/developer/reviewer)도 시드.

전체 스키마: [`database.md`](database.md).
