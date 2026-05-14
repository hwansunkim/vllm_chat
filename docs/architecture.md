# 아키텍처

## 전체 구조

```
┌─────────────────────────────────────────────────────────┐
│  Browser (static/index.html)                            │
│  - 멀티 대화 사이드바                                      │
│  - markdown-it + KaTeX + highlight.js 렌더링             │
│  - DOMPurify XSS 방어                                   │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP / REST
┌────────────────────▼────────────────────────────────────┐
│  FastAPI  (server.py, :8888)                            │
│  - 대화·턴 CRUD                                          │
│  - 채팅 엔드포인트 (키워드 추출 → 메모리 검색 → LLM 호출)    │
│  - 컨텍스트 초과 시 자동 아카이브                            │
└──────┬─────────────────────────┬───────────────────────┘
       │ SQLite (memory.db)       │ HTTP
┌──────▼──────────┐   ┌──────────▼──────────────────────┐
│  memories       │   │  vLLM OpenAI-compatible API      │
│  memory_keywords│   │  /v1/chat/completions            │
│  conversations  │   │  /v1/models                      │
│  turns          │   └──────────────────────────────────┘
└─────────────────┘
```

## 컴포넌트

### `chat.py` — 핵심 로직 모듈

CLI와 서버 양쪽에서 import해서 사용하는 공유 모듈이다.

| 기능 | 함수 |
|------|------|
| DB 초기화 | `init_db(conn)` |
| 메모리 저장 | `save_memories(conn, items)` |
| 메모리 검색 | `retrieve_memories(conn, keywords, top_k)` |
| 키워드 추출 | `extract_keywords(text)` |
| 메모리 추출 | `extract_memories_from_turns(turns)` |
| 메시지 조립 | `build_messages(system_prompt, retrieved, recent_turns)` |
| LLM 호출 | `chat(messages)` → `(reply, usage)` |
| 컨텍스트 한도 조회 | `get_model_context_limit()` |

### `server.py` — FastAPI 웹 서버

- `lifespan`: 시작 시 DB 테이블 생성 + `MAX_MODEL_LEN` 조회
- 대화(conversation)와 턴(turn)을 SQLite에 영속 저장
- 채팅 요청마다 RAG 파이프라인 실행 후 응답 반환

### `static/index.html` — 단일 파일 프론트엔드

빌드 도구 없이 CDN 라이브러리만 사용한다.

| 라이브러리 | 역할 |
|-----------|------|
| markdown-it | Markdown 파싱 (CommonMark 준수) |
| KaTeX + texmath | LaTeX 수식 렌더링 (`$...$`, `$$...$$`) |
| highlight.js | 코드 블록 문법 강조 |
| DOMPurify | XSS 방어 |

## 데이터 흐름 — 채팅 1회 요청

```
사용자 메시지
    │
    ├─► extract_keywords()       LLM 호출로 핵심 명사 최대 7개 추출
    │
    ├─► retrieve_memories()      키워드 교집합 COUNT DESC + 최근접근 DESC
    │                            상위 5개 메모리 반환
    │
    ├─► build_messages()         [system: 관련 메모리] + 최근 active 턴
    │
    ├─► chat()                   vLLM에 완성된 메시지 전송
    │                            reply + usage(토큰 수) 반환
    │
    ├─► save_turn()              user 턴, assistant 턴 DB 저장
    │                            (memories_json, context_pct, prompt_tokens)
    │
    └─► 컨텍스트 75% 초과 시
            extract_memories_from_turns(오래된 턴)
            save_memories()
            archived = 1 처리
```

## 주요 상수 (`chat.py`)

| 상수 | 기본값 | 설명 |
|------|--------|------|
| `MAX_COMPLETION_TOKENS` | 1024 | LLM 출력 최대 토큰 |
| `WARN_THRESHOLD` | 0.80 | CLI 경고 표시 임계값 |
| `ARCHIVE_THRESHOLD` | 0.75 | 아카이브 트리거 임계값 |
| `KEEP_RECENT_TURNS` | 4 | 아카이브 후 유지할 최근 턴 수 |
| `MAX_RETRIEVED_MEMORIES` | 5 | 한 번에 주입할 최대 메모리 수 |
