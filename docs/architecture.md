# 아키텍처 개요

> 이 문서는 vLLM Chat의 **전체 구조**를 다룬다. 각 파트의 상세 동작은 하위 문서로
> 이어진다(맨 아래 [문서 지도](#9-문서-지도)). 파트·패널·모듈의 호칭은 모두
> [`glossary.md`](glossary.md)를 따른다.

---

## 1. 한눈에 보기

vLLM Chat은 **로컬 vLLM(또는 OpenAI·Anthropic) 서버와 연동하는 웹 애플리케이션**이며,
세 가지 일을 한다.

| 도메인 | 하는 일 |
|---|---|
| **채팅** | RAG 메모리로 컨텍스트 윈도우를 관리하는 멀티 대화 LLM 채팅 |
| **시뮬레이션 API** | ABM 엔진을 웹에서 실행·관전·재개하는 얇은 래퍼 |
| **ABM 엔진** | Wave 기반 멀티 에이전트 시뮬레이션 (GUI·CLI 공용) |

```
┌───────────────────────────────────────────────────────────────────┐
│  브라우저 — frontend/  (빌드 도구 없음, CDN 라이브러리 + ES 모듈)      │
│                                                                   │
│   채팅 뷰            실행 뷰                    설정 뷰              │
│   #main             #sim-view                 #sim-settings-view   │
│   메시지·입력·        타임라인·에이전트 카드·      10개 섹션 아코디언     │
│   컨텍스트 바         인스펙터(그래프/지도/컨텍스트)                    │
└───────────────┬───────────────────────────────────────────────────┘
                │  HTTP REST  +  SSE (text/event-stream)
┌───────────────▼───────────────────────────────────────────────────┐
│  FastAPI 앱 — backend/main.py   (:8888, 정적 프론트도 서빙)          │
│                                                                   │
│  API 라우터 6개   model · conversations · agents · memories ·       │
│                   servers · simulation                             │
│                                                                   │
│  코어 레이어      backend/core/   에이전트 라우팅 · RAG 검색/저장     │
│  LLM 레이어       backend/llm/    서버 레지스트리 · 프로바이더 ·       │
│                                  파이프라인 · 브릿지                 │
│  웹검색 레이어     backend/websearch/                                │
└───────┬───────────────────────────────────┬───────────────────────┘
        │ sqlite3 (memory.db)                │ import + 워커 스레드
┌───────▼───────────────┐         ┌──────────▼────────────────────────┐
│  memory.db            │         │  ABM 엔진 — ABM/                    │
│  대화 · 턴 · 메모리 ·   │         │  Simulation 클래스 = 믹스인 9종      │
│  에이전트 · 서버 ·      │         │  Agent · 계약 층 · 시간/감염/위치    │
│  시나리오              │         │                                   │
└───────────────────────┘         │  logs_graph/simulation.db (SimDB) │
                                  │  실행 이력 · 턴 로그 · 이벤트 ·      │
                                  │  에이전트 스냅샷 · 구조화 메모리      │
                                  └───────────────────────────────────┘
        ▲                                          ▲
        │ /v1/chat/completions, /v1/models         │
┌───────┴──────────────────────────────────────────┴───────────────┐
│  LLM 서버   vLLM (OpenAI 호환) · OpenAI · Anthropic               │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. 기술 스택

| 층 | 선택 | 메모 |
|---|---|---|
| 백엔드 | **FastAPI** + uvicorn | `requirements.txt`는 `httpx · fastapi · uvicorn[standard] · tenacity` 4개뿐 |
| HTTP 클라이언트 | **httpx.AsyncClient** | 프로바이더가 메인 이벤트 루프에 바인딩된 클라이언트를 재사용 |
| 재시도 | **tenacity** | 브릿지(`backend/llm/bridge.py`)의 LLM 호출 재시도 |
| 검증 | **pydantic** | API 스키마 (`backend/api/**/schemas.py`) |
| 저장소 | **SQLite** ×2 | `memory.db`(채팅) + `logs_graph/simulation.db`(SimDB). ORM 없음, 순수 `sqlite3` |
| 프론트엔드 | **Vanilla JS (ES 모듈)** | **빌드 단계 없음.** `npm`·번들러 미사용 |
| 프론트 라이브러리 | CDN `<script>` | markdown-it · KaTeX · highlight.js · DOMPurify · **D3 v7** |
| 프론트 상태 | 평범한 객체 | `state`(채팅) / `sim`(시뮬). 프레임워크 없음 |

프론트엔드에 빌드가 없다는 것이 여러 제약을 만든다 — 자세히는
[`frontend.md`](frontend.md).

---

## 3. 프로세스·스레드 모델

단일 프로세스에서 **이벤트 루프 하나 + 필요 시 워커 스레드**로 돈다.

```
┌─ FastAPI 프로세스 ────────────────────────────────────────────────┐
│                                                                  │
│  메인 이벤트 루프 (asyncio)                                         │
│   · 모든 HTTP 요청 핸들러 (async)                                   │
│   · 채팅 SSE 제너레이터                                             │
│   · httpx.AsyncClient 들이 여기에 바인딩됨                           │
│   · lifespan()이 state.event_loop 에 자기 참조를 저장               │
│                                                                  │
│  ┌─ ABM 시뮬레이션 스레드 (threading.Thread, daemon) ────────────┐  │
│  │  · /start · /continue · /resume 마다 1개 생성                 │  │
│  │  · Simulation.run() = Wave 루프 (순수 동기 코드)              │  │
│  │  · wave 안에서 ThreadPoolExecutor 로 에이전트 병렬 스텝        │  │
│  │  · LLM 이 필요하면 → 브릿지                                    │  │
│  │      run_coroutine_threadsafe(coro, state.event_loop)        │  │
│  │      → 메인 루프에서 실행 → future.result(timeout) 로 대기     │  │
│  └─────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

핵심 규칙:

- **시뮬레이션은 이벤트 루프 밖의 OS 스레드에서 돈다.** ABM 코드는 순수 동기이고
  FastAPI를 import하지 않는다(덕 타이핑).
- 프로바이더의 `httpx.AsyncClient`는 `lifespan()`이 실행된 **메인 루프에 바인딩**된다.
  워커 스레드가 새 루프(`asyncio.run`)를 만들어 호출하면 *"attached to a different
  loop"*로 깨진다. 그래서 **브릿지**(`backend/llm/bridge.py`)가 코루틴을 원래 루프로
  되돌려 실행한다 — `state.event_loop` + `asyncio.run_coroutine_threadsafe`.
- **시뮬레이션은 단일 프로세스에서 동시에 1개만.** 상태는 전역 `_sim` dict
  (`backend/api/simulation/state.py`) + `_sim_lock`으로 원자적 전이.
- 채팅에는 이런 스레드가 없다 — 요청 핸들러가 async로 직접 처리한다.

---

## 4. 저장소 — SQLite 2개

| 파일 | 소유 | 내용 | 접근 |
|---|---|---|---|
| `memory.db` | 백엔드 | 대화·턴·RAG 메모리·채팅 에이전트·LLM 서버·시나리오 정의 | `backend/db/database.py: get_db()` |
| `logs_graph/simulation.db` | ABM (SimDB) | 실행 이력·턴 로그·SSE 이벤트·에이전트 스냅샷·구조화 메모리(에피소드/사실/관계/자아) | `ABM/db/` (`SimDB`) |

**왜 분리했나** — 채팅 DB는 FastAPI 요청이 짧게 열고 닫는다. SimDB는 별도 스레드에서
장시간 도는 시뮬레이션이 소유하며, CLI(`python -m ABM.cli`)에서도 백엔드 없이 그대로
쓴다. 시나리오 *정의*는 채팅 DB(`simulation_scenarios`)에 있고, 실행 *결과*는
SimDB에 있다.

전체 스키마는 [`database.md`](database.md).

---

## 5. 세 가지 요청 경로

### ① 채팅 — `POST /api/conversations/{id}/chat` (SSE)

```
사용자 메시지
  → 라우팅 해석 (@멘션 / 라우터 모드 / 기본 시스템 프롬프트)
  → 키워드 추출(LLM) → retrieve_memories (RAG)
  → build_messages ([system: 페르소나 + 관련 메모리 + 웹검색 컨텍스트] + 최근 턴)
  → 서버 선택 (레지스트리) + 사고 수준 확정
  → SSE 스트림:  event: search → thinking → answer → done
  → save_turn → 컨텍스트 75% 초과 시 _maybe_archive
```

상세: [`backend.md`](backend.md) · [`memory-system.md`](memory-system.md).

### ② 시뮬레이션 실행 — `POST /api/simulation/start` + `GET /api/simulation/stream`

```
/start → _sim["status"]="running", SSE 큐 생성, 데몬 스레드 시작
         스레드: headless.run_config(cfg) → Agent들 + Simulation → sim.run()
         run() 이 매 wave 마다 _emit(event) → SSE 큐
/stream → EventSource, 큐를 소비해 브라우저로 흘림
         (wave_start, turn_complete, agent_move, infection_update, … simulation_end)
```

상세: [`simulation-overview.md`](simulation-overview.md) · [`simulation-engine.md`](simulation-engine.md).

### ③ 읽기 전용 조회

`GET /api/simulation/status` · `/agents/{name}/context`(컨텍스트 탭) ·
`/logs` · `/events` · `/runs` 등. 실행 중이든 끝났든 `_sim` 전역 또는 SimDB에서 읽는다.

---

## 6. 실행 방법

```bash
pip install -r requirements.txt
python run.py                 # → http://localhost:8888  (uvicorn reload)
# 또는
uvicorn backend.main:app --host 0.0.0.0 --port 8888 --reload
```

- LLM 서버는 웹 UI **서버 설정 모달** 또는 `POST /api/servers`로 등록한다.
  `servers.json`이 있으면 첫 실행 시 DB에 시드된다.
- `docker-compose.yaml` — 로컬 vLLM(gemma-4-31B, speculative decoding) +
  Prometheus + Grafana 스택. 앱 자체는 컨테이너화돼 있지 않다.
- **헤드리스 CLI** — `python -m ABM.cli run scenario.json` (GUI 없이 시뮬레이션 →
  마크다운). 상세: [`simulation-overview.md`](simulation-overview.md#7-헤드리스-cli).

---

## 7. 디렉터리 지도

```
backend/
  main.py            FastAPI 앱 · lifespan · 라우터 등록 · 정적 서빙
  config.py          환경변수 · 임계값 상수 · 사고 수준 정의
  state.py           전역: max_model_len, event_loop (브릿지가 사용)
  api/
    model.py         GET /api/model/status
    conversations.py 대화 CRUD + POST /{id}/chat (채팅 파이프라인)
    agents.py        채팅 에이전트 CRUD
    memories.py      RAG 메모리 조회·삭제
    servers.py       LLM 서버 CRUD · probe-models · health
    _conv_helpers.py 채팅 파이프라인 헬퍼 (라우팅·저장·아카이브)
    schemas.py       채팅 도메인 pydantic 스키마
    simulation/      시뮬레이션 API 패키지 → simulation-overview.md
  core/
    agent.py         resolve_agent_mention · async_route_agent · 시스템 프롬프트 조립
    memory.py        save_memories · retrieve_memories (RAG)
  llm/
    client.py        공개 진입: async_llm · async_stream_chat · async_chat
    registry.py      ServerRegistry — 서버 풀 + 선택 우선순위
    providers/       base · vllm · openai · anthropic · openai_compatible
    pipeline.py      키워드/메모리 추출 · build_messages
    bridge.py        워커 스레드 → 메인 루프 브릿지 · tenacity 재시도
    utils.py         parse_json
  websearch/         toggle 모드 웹 검색 (DuckDuckGo)
  db/database.py     memory.db — get_db · init_tables · migrate_db · seed

ABM/
  simulation/
    core.py          Simulation 클래스 (믹스인 조합) · __init__ · _emit · 시각 계산
    runner.py        _RunnerMixin — run() Wave 루프
    step.py          _StepMixin — _step_agent · _assemble_agent_prompt
    turn.py          _TurnMixin — _apply_turn_result
    targets.py       _TargetsMixin — 발화 대상 해석 · stranger
    location.py      _LocationMixin — 위치 그래프 · zone · 씬 메시지
    meeting.py       _MeetingMixin — 만남 lock (사람 추격)
    infection.py     _InfectionMixin — 결정론적 SIR/SIS
    events.py        _EventsMixin — 시나리오 이벤트
    system.py        _SystemMixin — 디렉터(system 에이전트)
    headless.py      run_config() — fresh start 유일 경로 (GUI·CLI 공용)
    _constants.py    요일 라벨 · 디렉터 반복 감지 상수
  agent.py           Agent 클래스 — 페르소나 + memory + engine_contract
  prompt_contract.py 계약 층 정본 — build_world/output/relationship_contract
  time_classifier.py 가변 시간 분류/추론 LLM 호출
  memory_compressor.py 에이전트 memory → 구조화 DB 메모리 압축
  system_agent.py    디렉터 LLM 호출 (run_system_agent)
  parser.py          LLM JSON 응답 파싱
  cli.py             python -m ABM.cli  run / export
  db/                SimDB — logs_graph/simulation.db
  export/            markdown.py (파이썬 골든 포매터) · csv.py · labels.py
  scenarios/         코드로 정의하는 시나리오 (convenience_store.py)

frontend/
  index.html         전체 마크업 (모든 뷰·모달)
  css/               base · layout · sidebar · messages · input · modals · simulation
  js/
    main.js          부팅 — init*Events() 8개
    state.js         채팅 전역 상태
    api.js chat.js stream.js messages.js markdown.js context-bar.js
    conversations.js agents.js memories.js servers.js mention.js sidebar.js sources.js
    sim/
      index.js state.js views.js scenarios.js context.js resize.js
      run/     control · sse · feed · cards · errors
      graph/d3.js  map/d3.js
      runs/    history · replay · interview
      settings/  섹션별 렌더 (10개 섹션)
      export/  markdown.js  csv.js
```

> `run.py`, 루트 `agent.py`(레거시 `convenience_store` 러너), 루트 `api/`·`core/`·`llm/`
> 디렉터리는 리팩터링 잔재이며 현재 코드 경로가 아니다. 정본은 `backend/`·`ABM/`.

---

## 8. 횡단 관심사

| 관심사 | 어디서 처리 |
|---|---|
| **LLM 서버 선택** | `ServerRegistry.select()` — ①`server_id` 명시 ②`model` 매치 라운드로빈 ③기본 서버 |
| **사고(thinking) 수준** | 프로바이더 중립 4단계(`off/low/medium/high`) → 벤더별 파라미터로 번역 (`backend.md`) |
| **컨텍스트 관리 (채팅)** | RAG 메모리 + 75% 초과 시 오래된 턴 아카이브 (`memory-system.md`) |
| **컨텍스트 관리 (시뮬)** | 에이전트별 `token_limit` — 근접 시 구조화 메모리 압축, 초과 시 오래된 메모리 트림 (`simulation-engine.md`) |
| **프롬프트 = 서사 층 + 계약 층** | 사용자 소유(페르소나·배경) vs 엔진 소유(출력 스키마·이동·시간·감염). 계약은 저장 안 하고 실행 시 생성 (`simulation-engine.md`) |
| **재현성** | CLI `--seed`(전역 `random`), wave 결과 키순 정규화. LLM 응답 자체는 비결정적 |
| **에러 격리** | 웹검색 실패 → 검색 없이 진행 · 진행률 콜백 예외 무시 · 턴 실패 → 롤백 후 다음 wave · 디렉터 예외 → 로그만 |
| **XSS** | 프론트 `DOMPurify.sanitize()` 또는 DOM API. `innerHTML` 직접 할당 금지 |

---

## 9. 문서 지도

| 문서 | 내용 |
|---|---|
| [`glossary.md`](glossary.md) | **파트·패널·모듈·용어의 공통 호칭** (디버깅 대화용) |
| **`architecture.md`** (이 문서) | 전체 구조 · 프로세스 모델 · 요청 경로 |
| [`frontend.md`](frontend.md) | 화면 구조 · 상태 관리 · SSE 소비 · 렌더링 · 모듈 레퍼런스 |
| [`backend.md`](backend.md) | 채팅 파이프라인 · LLM 레이어 · 서버 레지스트리 · 프로바이더 · 브릿지 · 웹검색 |
| [`memory-system.md`](memory-system.md) | RAG 메모리 — 키워드 검색 · 아카이브 |
| [`simulation-overview.md`](simulation-overview.md) | Wave 모델 · 실행 라이프사이클 · SSE · SimDB · CLI |
| [`simulation-engine.md`](simulation-engine.md) | `Simulation` 믹스인 · `run()` 루프 순서 · 프롬프트 조립 · 계약 층 · 압축 |
| [`simulation-features.md`](simulation-features.md) | 위치/zone · 공간 인지 · 관계 지도 · 만남 · 시간 모델 · 디렉터 · 감염 |
| [`api-reference.md`](api-reference.md) | REST 엔드포인트 · SSE 이벤트 스펙 · pydantic 스키마 |
| [`database.md`](database.md) | `memory.db` + `simulation.db` 전체 스키마 |
| [`spatial-perception.md`](spatial-perception.md) | 공간 기반 인지 심층 (features에서 링크) |
| [`director-repetition-detection.md`](director-repetition-detection.md) | 디렉터 반복 감지 D1+D2 심층 (features에서 링크) |
| [`vectordb.md`](vectordb.md) | SQLite → Vector DB 전환 검토 |
