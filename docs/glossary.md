# vLLM Chat 용어집 (Glossary)

이 문서는 **디버깅·기능 논의 시 공통 호칭**을 정하기 위한 것이다.
"왼쪽 아래 패널" 대신 "**타임라인**", "그 엔진 파일" 대신 "**러너 믹스인(`_RunnerMixin`)**"
처럼 서로 같은 단어를 쓰기 위한 기준표다.

- 각 항목은 **canonical 이름** / (DOM id 또는 파일 경로) / 한 줄 역할 로 적는다.
- 이름을 바꾸고 싶으면 이 문서를 먼저 고치고 코드/대화에 반영한다.
- 상세 동작 설명은 [기술문서 세트](#부록--기술문서-세트)에서 다루고, 여기서는 "무엇을 그렇게 부른다"만 정의한다.

**확정 표기 (자주 헷갈리는 것)**

| 쓴다 | 안 쓴다 | 대상 |
|---|---|---|
| **타임라인** | 피드, 로그 | 실행 뷰 중앙 본문 (`#sim-feed`) |
| **인스펙터** | 우측 패널, 그래프 패널 | 실행 뷰 오른쪽 3탭 (`#sim-graph-panel`) |
| **디렉터** | 내레이터, system 에이전트 | 흐름 감시·개입 내레이터 (`system.py`) |
| **센터 패널** | 메인 패널 | 실행 뷰 중앙 열 (`#sim-center`) |

---

## 0. 최상위 구조

```
┌───────────────────────────────────────────────────────────────┐
│ 브라우저 (frontend/) — 빌드 도구 없는 Vanilla JS + CDN 라이브러리  │
│   · 채팅 뷰   · 시뮬레이션 실행 뷰   · 시뮬레이션 설정 뷰            │
└───────────────┬───────────────────────────────────────────────┘
                │ HTTP REST + SSE
┌───────────────▼───────────────────────────────────────────────┐
│ FastAPI 앱 (backend/main.py, :8888)                            │
│   · API 라우터 6개  · 코어/LLM/웹검색/DB 레이어                    │
│   · 시뮬레이션 API 패키지 (실행 스레드 관리 + SSE)                  │
└───────┬──────────────────────────────┬────────────────────────┘
        │ memory.db (채팅)              │ 워커 스레드에서 import
┌───────▼─────────┐          ┌─────────▼────────────────────────┐
│ SQLite: memory.db│          │ ABM 시뮬레이션 엔진 (ABM/)         │
│  대화·턴·메모리·   │          │  Simulation 클래스 + 믹스인 9종    │
│  에이전트·서버     │          │  logs_graph/simulation.db 에 기록 │
└─────────────────┘          └──────────────────────────────────┘
```

**3대 도메인**

| 도메인 | 코드 위치 | 한 줄 정의 |
|---|---|---|
| **채팅** (Chat) | `backend/api/conversations.py`, `backend/core/`, `backend/llm/` | RAG 메모리로 컨텍스트를 관리하는 멀티 대화 LLM 채팅 |
| **시뮬레이션 API** (Sim API) | `backend/api/simulation/` | ABM 엔진을 웹에서 실행·관전·재개하는 얇은 래퍼 |
| **ABM 엔진** (Engine) | `ABM/` | Wave 기반 멀티 에이전트 시뮬레이션. GUI·CLI 공용 |

---

# Part A — 프론트엔드

`frontend/index.html` 하나에 모든 마크업이 있고, `frontend/js/main.js`가
모듈 8개를 초기화한다. 스타일은 `frontend/css/` 7개 파일로 분리.

## A-1. 화면 (View) — 상호 배타적, 한 번에 하나만 표시

| Canonical | DOM id | 역할 |
|---|---|---|
| **채팅 뷰** | `#main` | 기본 화면. 대화 목록에서 고른 대화를 주고받는다 |
| **실행 뷰** | `#sim-view` | 시뮬레이션을 돌리고 실시간 관전하는 화면 |
| **설정 뷰** | `#sim-settings-view` | 시나리오(에이전트·세계·시간·감염 등)를 편집하는 화면 |

전환 로직: `frontend/js/sim/views.js` (`showSimView` / `showSettingsView` / `hideSimView` …).
`.sim-hidden` 클래스로 껐다 켠다.

## A-2. 사이드바 (`#sidebar`)

모든 뷰에서 공통으로 보이는 **맨 왼쪽 짙은 남색 세로 바**.

| Canonical | DOM id | 역할 |
|---|---|---|
| **사이드바** | `#sidebar` | 접기/펼치기 가능 (`#sidebar-toggle`, 상태는 localStorage) |
| **대화 목록** | `#conversation-list` | 최근 수정순 대화 리스트 (`conversations.js`) |
| **사이드바 푸터** | `.sidebar-footer` | 하단 버튼 4개 |
| ├ 에이전트 관리 버튼 | `#agent-btn` 🤖 | → 에이전트 관리 모달 |
| ├ 메모리 저장소 버튼 | `#mem-btn` 🗂 | → 메모리 저장소 모달 |
| ├ 서버 설정 버튼 | `#server-btn` 🖥 | → 서버 설정 모달 |
| └ 시뮬레이션 버튼 | `#sim-btn` 🎭 | → 실행 뷰로 전환 |

## A-3. 채팅 뷰 구성요소

```
#main
├── 채팅 헤더 (#chat-header)   제목 · 서버 셀렉터 · 모델 배지
├── 메시지 영역 (#messages)     대화 기록 (사용자/어시스턴트 말풍선)
└── 입력 영역 (#input-area)
    ├── 컨텍스트 바 (#context-bar)   토큰 사용률 게이지 + 텍스트
    └── 입력 행 (#input-row)
        ├── 메시지 입력창 (#message-input)
        ├── 멘션 드롭다운 (#mention-dropdown)   @에이전트 자동완성
        ├── 사고 컨트롤 (#thinking-control) 🧠   off/low/medium/high 팝오버
        ├── 웹 검색 버튼 (#websearch-btn) 🔍
        └── 전송 버튼 (#send-btn) ▶
```

| Canonical | DOM id | 역할 |
|---|---|---|
| **채팅 헤더** | `#chat-header` | 대화 제목(`#chat-title`), 서버 셀렉터(`#server-selector`), 모델 배지(`#model-badge`) |
| **메시지 영역** | `#messages` | 말풍선 렌더. 아카이브된 메시지도 계속 표시 (`messages.js`) |
| **컨텍스트 바** | `#context-bar` | `#context-fill` 게이지 + `#context-info` 텍스트. 응답 시점 `context_pct` 반영 (`context-bar.js`) |
| **입력 영역** | `#input-area` | 컨텍스트 바 + 입력 행을 묶은 하단 블록 |
| **사고 컨트롤** | `#thinking-control` | 🧠 버튼 + `#thinking-menu` 4단계 팝오버. 소유 모듈은 `servers.js` |
| **멘션 드롭다운** | `#mention-dropdown` | `@` 입력 시 에이전트 자동완성 (`mention.js`) |

## A-4. 실행 뷰 구성요소 (`#sim-view`)

```
#sim-view
├── 실행 헤더 (#sim-header)
│     ← 채팅 · 시나리오 라벨 · 상태 배지 · 오류 배지 · [⚙설정 📋이력 ▶시작 ↩이어서 ■중지 📄MD]
└── 실행 바디 (#sim-body)   ← 좌우 2분할 (드래그 리사이즈)
    ├── 센터 패널 (#sim-center)
    │   ├── 에이전트 카드 열 (#sim-agent-cards)   상단 가로 스트립
    │   ├── 턴 인디케이터 (#sim-turn-indicator)   "Wave N" + 진행 바 + 시각
    │   └── 타임라인 (#sim-feed)                  대화·이벤트 타임라인 (본문)
    ├── 리사이즈 핸들 (#sim-resize-r)
    └── 인스펙터 (#sim-graph-panel)
        ├── 관계 그래프 탭 (#sim-tab-graph)  🕸  누가 누구에게 말했는지 (D3 force)
        ├── 위치 지도 탭 (#sim-tab-map)      🗺  위치 그래프 위 에이전트 이동
        └── 컨텍스트 탭 (#sim-tab-context)   👤  선택한 에이전트가 실제로 받는 프롬프트
```

> **이름 ≠ 코드 식별자**: DOM id는 `#sim-feed`·`#sim-graph-panel`, JS 모듈은
> `sim/run/feed.js`로 옛 이름을 유지한다. 대화에서는 **타임라인 / 인스펙터**로 부르고,
> 코드를 짚을 때만 `feed.js` 등 실제 이름을 쓴다.

| Canonical | DOM id | 역할 | 소유 모듈 |
|---|---|---|---|
| **실행 헤더** | `#sim-header` | 시나리오 라벨(`#sim-scenario-label`), 상태 배지(`#sim-status-badge`), **오류 배지**(`#sim-error-badge` "⚠ N건"), 실행 컨트롤 버튼들 | `sim/run/control.js`, `sim/run/errors.js` |
| **센터 패널** | `#sim-center` | 실행 뷰의 중앙(넓은) 열 | — |
| **에이전트 카드 열** | `#sim-agent-cards` | 에이전트별 카드(`.sim-agent-card`) 가로 나열. 카드에 감정·위치·감염 뱃지·토큰. **카드 클릭 → 컨텍스트 탭** | `sim/run/cards.js` |
| **턴 인디케이터** | `#sim-turn-indicator` | 현재 wave/진행률(`#sim-progress-fill`), 시뮬레이션 내 시각(`#sim-turn-time`) | `sim/run/feed.js` |
| **타임라인** | `#sim-feed` | 실행 중 벌어지는 모든 것의 시간순 흐름. 발화 말풍선 + 각종 이벤트 카드 | `sim/run/feed.js` |
| **인스펙터** | `#sim-graph-panel` | 관계 그래프 / 위치 지도 / 컨텍스트 3탭. 폭 300px, 드래그로 조절 | `sim/context.js` (탭 전환) |
| **관계 그래프 탭** | `#sim-tab-graph` | `#sim-graph-svg` — 발화 엣지 그래프 | `sim/graph/d3.js` |
| **위치 지도 탭** | `#sim-tab-map` | `#sim-map-svg` — 위치 그래프(방/zone) 지도 + 아바타 | `sim/map/d3.js` |
| **컨텍스트 탭** | `#sim-tab-context` | 선택 에이전트(`#sim-context-agent-name`)의 프롬프트 메시지 열(`#sim-context-msgs`) + 토큰 배너 | `sim/context.js` |

> **타임라인 카드 종류** (모두 `sim/run/feed.js`의 `addXxxCard`): 발화 말풍선, 상황 카드(`addSituationCard`),
> 디렉터 판단 카드(`addDirectorCallCard` 🎬), 개입 카드(`addInterventionCard`), 세계 사건 카드(`addWorldEventCard` 🌍),
> 이동 카드(`addMovementCard` 🚶), 외모 변화 카드(`addAppearanceCard` 👗), 만남 카드(`addMeetingCard` 🤝),
> 감염 카드(`addInfectionCard` 🦠), 시간 점프 카드(`addTimeJumpCard` 🕐), 씬 이벤트(`addSceneEventToFeed`).

## A-5. 설정 뷰 구성요소 (`#sim-settings-view`)

```
#sim-settings-view
├── 설정 헤더 (#sim-settings-header)
│     ← 시뮬레이션 · [시나리오 이름칸] [불러오기▾] [＋새 시나리오 💾저장 🗑삭제 📋이력 ⬇내보내기 ⬆불러오기 ▶시작]
└── 설정 레이아웃 (#sim-settings-layout)   최대폭 1080px
    ├── 섹션 네비 레일 (#sim-settings-nav)   왼쪽 세로 목차 (180px)
    └── 설정 본문 (#sim-settings-main)        아코디언 섹션 10개
```

**설정 섹션 (Section)** — `data-section` 값이 정식 키. 아코디언 접기/펼치기는 `sim/settings/sections.js`.

| Canonical | `data-section` | 아이콘 | 편집 대상 | 소유 모듈 |
|---|---|---|---|---|
| **배경 설명** | `background` | 📋 | 모든 에이전트 공통 장면 전제 (`#sim-background`) | `settings/textareas.js` |
| **에이전트** | `agents` | 👤 | 등장인물 카드: 페르소나·그룹·위치·관계·temperature·초기 등장 | `settings/agents.js` |
| **실행 설정** | `run` | 🚀 | 시작 에이전트, 최대 wave, 목표 기간, LLM 서버, temperature | `settings/page.js` |
| **시간 설정** | `time` | ⏱ | 시작 요일·시각, wave당 시간, 시간 모드(고정/가변), 조기 종료, 가변 시간 카테고리 | `settings/page.js`, `settings/time-categories.js` |
| **생성 옵션** | `tuning` | ⚙ | step delay, token limit, LLM max tokens, 언어 교잡 수정 | `settings/page.js` |
| **출력 정의** | `output` | 🏷 | 출력 필드(extra_fields), **엔진 계약 미리보기**, 출력 계약 오버라이드 | `settings/output-fields.js`, `settings/contract-preview.js` |
| **위치 그래프** | `world` | 🗺 | 장소 노드·연결·zone·외부공간, **공간 기반 인지(엿듣기)** 토글 | `settings/location-graph.js` |
| **system 에이전트** | `director` | 🎬 | 디렉터(내레이터) 활성화·페르소나·개입 주기·시야·**감독 노트** | `settings/system-agent.js` |
| **감염병 모델** | `infection` | 🦠 | SIR/SIS, 환자 0번, 전염 확률, 회복 시간, 증상 단계 | `settings/infection-config.js` |
| **시나리오 이벤트** | `events` | 📜 | 특정 wave에 예약 발생: system_message / agent_enter / agent_exit | `settings/events.js` |

## A-6. 모달·오버레이

| Canonical | DOM id | 진입점 |
|---|---|---|
| **에이전트 관리 모달** | `#agent-modal` | 사이드바 🤖 |
| **메모리 저장소 모달** | `#mem-modal` | 사이드바 🗂 |
| **서버 설정 모달** | `#server-modal` | 사이드바 🖥 / 채팅 헤더 서버 셀렉터 |
| **새 대화 모달** | `#modal` | 사이드바 "＋ 새 대화" |
| **내보내기 옵션 모달** | `#sim-export-modal` | 실행 헤더 📄 MD |
| **텍스트 확대 편집 오버레이** | `#sim-text-editor-overlay` | 설정 뷰 각 textarea의 ⤢ 버튼 |
| **에이전트 가져오기 모달** (채팅←시뮬) | `#agent-import-modal` | 에이전트 관리 모달 "🎬 시뮬레이션에서 가져오기" |
| **에이전트 가져오기 모달** (시뮬←채팅) | `#sim-import-chat-modal` | 설정 뷰 에이전트 섹션 "💬 채팅에서 가져오기" |

## A-7. 프론트엔드 JS 모듈 맵

### 채팅 도메인 (`frontend/js/`)

| 파일 | 담당 |
|---|---|
| `main.js` | 부팅 — 각 도메인 `init*Events()` 호출 |
| `state.js` | 채팅 전역 상태 `state` (현재 대화 id, 전송중 플래그, 사고 수준, 웹검색) |
| `api.js` | `fetch` 래퍼 (`api()`, `probeModels()`) |
| `conversations.js` | 대화 목록·열기·생성, 새 대화 모달 |
| `chat.js` | 전송 버튼·Enter·입력창 리사이즈 → `/chat` SSE 시작 |
| `stream.js` | 채팅 SSE 리더 (`readSSEStream`) — thinking/answer/search/done 이벤트 |
| `messages.js` | 말풍선 DOM 생성/추가/로딩 |
| `markdown.js` | markdown-it 인스턴스 + DOMPurify, 코드 하이라이트 |
| `context-bar.js` | 컨텍스트 바 갱신 |
| `agents.js` | 에이전트 관리 모달 (CRUD, 구조화 입력 → 프롬프트 자동생성) |
| `memories.js` | 메모리 저장소 모달 (조회·필터·삭제) |
| `servers.js` | 서버 설정 모달, 모델 배지, 사고 컨트롤 팝오버 |
| `mention.js` | `@에이전트` 멘션 자동완성 |
| `sidebar.js` | 사이드바 접기/펼치기 |
| `sources.js` | 웹 검색 출처 각주 렌더 |
| `agent-transfer.js`, `agent-import-modal.js` | 채팅↔시뮬 에이전트 복사 |
| `utils.js` | `esc`, `scrollToBottom`, localStorage JSON |

### 시뮬레이션 도메인 (`frontend/js/sim/`)

| 파일 | 담당 |
|---|---|
| `index.js` | 시뮬레이션 이벤트 배선 (`initSimulationEvents`) |
| `state.js` | 시뮬레이션 전역 상태 `sim` + **순수 헬퍼** (요일/기간/확률/감염 정규화, 감정색, 아이콘). 일부는 `ABM/export/labels.py`에 파이썬으로도 있음 (골든 테스트로 동기화) |
| `views.js` | 3뷰 전환 |
| `scenarios.js` | 시나리오 목록·저장·불러오기·내보내기, `buildScenarioConfig` (상태→API 페이로드) |
| `context.js` | 인스펙터 탭 전환, 컨텍스트 탭 렌더 |
| `resize.js` | 센터 패널 ↔ 인스펙터 드래그 리사이즈 |
| `run/control.js` | ▶시작 / ■중지 / ↩이어서, 상태 배지 |
| `run/sse.js` | **실행 SSE 리더** (`connectSSE`) — 모든 실시간 이벤트를 타임라인·카드·인스펙터로 분배 |
| `run/feed.js` | 타임라인 카드 렌더 전부 + wave 구분선 버퍼링 |
| `run/cards.js` | 에이전트 카드 열 |
| `run/errors.js` | 오류 배지 + 팝업, `sim.errorLog` |
| `graph/d3.js` | 인스펙터 관계 그래프 탭 (D3) |
| `map/d3.js` | 인스펙터 위치 지도 탭 (D3) |
| `runs/history.js` | 실행 이력 패널·모달 |
| `runs/replay.js` | 과거 실행 재생(`/load`) |
| `runs/interview.js` | 실행 후 에이전트 인터뷰 |
| `settings/*.js` | 설정 뷰 섹션별 렌더/입력 처리 (섹션 표 참고) |
| `export/markdown.js`, `export/csv.js` | 마크다운/CSV 내보내기 (프론트 포매터) |
| `utils/*.js` | 다운로드, JSON, 시간 헬퍼 |

---

# Part B — 백엔드 (FastAPI)

`backend/main.py` = 앱 진입점. `lifespan()`에서 DB 초기화 + 서버 레지스트리 로드 +
메인 이벤트 루프 참조 저장. `/` 이하는 `frontend/` 정적 서빙.

## B-1. API 라우터 6개

| Canonical | 파일 | prefix | 주요 엔드포인트 |
|---|---|---|---|
| **모델 라우터** | `api/model.py` | `/api/model` | `GET /status` (현재 서버·모델·컨텍스트 한도) |
| **대화 라우터** | `api/conversations.py` | `/api/conversations` | 대화 CRUD, `POST /{id}/chat` (SSE) |
| **에이전트 라우터** | `api/agents.py` | `/api/agents` | 채팅 에이전트 CRUD |
| **메모리 라우터** | `api/memories.py` | `/api/memories` | RAG 메모리 조회·삭제 |
| **서버 라우터** | `api/servers.py` | `/api/servers` | LLM 서버 CRUD, `POST /probe-models`, `GET /{id}/health` |
| **시뮬레이션 라우터** | `api/simulation/` | `/api/simulation` | Part B-5 참고 |

### 채팅 파이프라인 — `POST /api/conversations/{id}/chat`

`_resolve_routing` (멘션/라우터/기본) → `retrieve_memories` (RAG) → `build_messages`
→ 서버 선택 (`get_registry().select`) → `async_stream_chat` (SSE: `search`→`thinking`→`answer`→`done`)
→ `save_turn` → `_maybe_archive` (컨텍스트 75% 초과 시).
헬퍼는 `api/_conv_helpers.py`.

## B-2. 코어 레이어 (`backend/core/`)

| 파일 | 담당 함수 |
|---|---|
| `agent.py` | `build_agent_system_prompt`, `resolve_agent_mention` (`@name`), `async_route_agent` (LLM이 에이전트 선택 = **라우터 모드**) |
| `memory.py` | `save_memories`, `retrieve_memories` (키워드 교집합 COUNT DESC + 최근접근 DESC, 상위 5) |

## B-3. LLM 레이어 (`backend/llm/`)

| Canonical | 파일 | 역할 |
|---|---|---|
| **LLM 클라이언트** | `client.py` | 공개 진입 함수: `async_llm` (단발), `async_stream_chat` (SSE), `async_chat` (비스트리밍 멀티턴), `async_get_model_context_limit` |
| **서버 레지스트리** | `registry.py` | `ServerRegistry` — 서버 풀 관리 + **선택 우선순위**: ①`server_id` 명시 ②`model` 매치 라운드로빈 ③기본 서버. `NoProviderError` |
| **프로바이더** | `providers/` | `base.py` (추상), `vllm.py`, `openai.py`, `anthropic.py`, `openai_compatible.py`. 벤더별 요청 변환·스트리밍·**사고 수준** 번역 |
| **파이프라인** | `pipeline.py` | `async_extract_keywords`, `async_extract_memories_from_turns`, `build_messages` (RAG 메시지 조립) |
| **브릿지** | `bridge.py` | **워커 스레드 → 메인 이벤트 루프** 위탁 실행. `make_sync_chat`이 ABM용 `(messages)->(content, reasoning, usage)` 동기 콜러블 생성. tenacity 재시도 |
| **레지스트리 유틸** | `utils.py` | `parse_json` |

> **서버(server)** = DB `servers` 테이블의 레코드 (사용자가 등록). **프로바이더(provider)** = 그 레코드로 만든 런타임 객체 (`LLMProvider` 서브클래스). 대화에서 구분해서 쓴다.

## B-4. 지원 레이어

| Canonical | 위치 | 역할 |
|---|---|---|
| **웹 검색 레이어** | `backend/websearch/` | `service.py` (오케스트레이션), `query.py` (쿼리 재작성), `providers/duckduckgo.py`, `context.py` (검색결과→프롬프트 컨텍스트) |
| **채팅 DB 레이어** | `backend/db/database.py` | `get_db()` (SQLite `memory.db`), `init_tables`, `migrate_db` (하위호환 ALTER), `seed_default_agents/servers` |
| **전역 상태** | `backend/state.py` | `max_model_len`, `event_loop` (브릿지가 사용) |
| **설정** | `backend/config.py` | 환경변수, 임계값 상수, `THINKING_LEVELS`, `normalize_thinking_level` |

### `memory.db` 테이블 (채팅)

`memories`, `memory_keywords`, `conversations`, `turns`, `agents`, `servers`,
`simulation_scenarios` (시나리오 JSON 저장소).

## B-5. 시뮬레이션 API 패키지 (`backend/api/simulation/`)

ABM 엔진을 웹에서 굴리는 얇은 래퍼. **엔진 자체는 별도 OS 스레드에서 돈다.**

| Canonical | 파일 | 역할 |
|---|---|---|
| **시뮬레이션 전역 상태** | `state.py` | `_sim` dict (`status` idle/running/done/stopped/error, `event_queue`, `sim_obj` …) + `_sim_lock` (원자적 상태 전이). **단일 프로세스 = 동시에 1개만 실행** |
| **러너 헬퍼** | `runner.py` | `swap_event_queue` (SSE 큐 교체), `fold_elapsed_and_reset_waves` (`/continue` 준비), `finalize_run` (종료 공통 처리: 스냅샷 저장·상태 확정) |
| **라이프사이클** | `runtime/lifecycle.py` | `POST /start` `/stop` `/continue` — 실행 스레드 생성·중지·이어서 |
| **되살리기** | `runtime/load.py`, `runtime/resume.py` | `POST /load/{run_id}` (과거 실행 스냅샷 열기), `POST /resume/{run_id}` (스냅샷에서 이어 실행) |
| **상태 조회** | `runtime/queries.py` | `GET /status`, `GET /agents/{name}/context` (에이전트가 실제로 받는 프롬프트 — 컨텍스트 탭의 소스), `GET /agents/{name}/memory` (구조화 DB 메모리) |
| **실행 로그 조회** | `runtime/feeds.py` | `GET /logs` (shared_log), `GET /events` (저장된 SSE 이벤트), `GET /edges`. 인스펙터·재생·내보내기의 데이터 소스 |
| **인터뷰** | `runtime/interviews.py` | `POST/GET /runs/{run_id}/agents/{name}/interview` |
| **SSE 스트림** | `sse.py` | `GET /stream` — 실행 뷰가 붙는 EventSource. `_sim["event_queue"]` 소비 |
| **실행 이력** | `runs.py` | `GET /runs`, `GET /runs/{id}`, `DELETE`, `POST /runs/bulk-delete` |
| **시나리오** | `scenarios.py` | 시나리오 CRUD (`simulation_scenarios` 테이블, 채팅 DB에 저장) |
| **계약 미리보기** | `contract.py` | `POST /contract-preview` — 현재 설정으로 엔진이 만들 계약 문자열 (설정 뷰 "엔진 계약 미리보기") |
| **스키마** | `schemas.py` | `SimStartConfig`, `SimContinueConfig`, `AgentConfig`, `InfectionModelConfig`, `TimeCategory` … (pydantic) |
| **LLM 설정** | `runtime/llm_config.py` | `_make_llm`, `_make_agent_llm_map` — config → 브릿지 동기 콜러블 |

---

# Part C — ABM 시뮬레이션 엔진 (`ABM/`)

## C-1. Simulation 엔진 = 믹스인 조합

**`Simulation`** 클래스 (`ABM/simulation/core.py`) 하나가 믹스인 9개를 상속한다.
"엔진 어디를 고친다"는 대화에서는 **믹스인 이름**으로 특정한다.

```python
class Simulation(_LocationMixin, _InfectionMixin, _MeetingMixin, _TargetsMixin,
                 _EventsMixin, _TurnMixin, _StepMixin, _SystemMixin, _RunnerMixin):
```

| Canonical (믹스인) | 파일 | 핵심 메서드 | 담당 |
|---|---|---|---|
| **코어** | `core.py` | `__init__`, `_emit`, `_current_elapsed_minutes`, `export_agent_state` / `restore_agent_state`, `_apply_engine_contract` | 상태 보유, 이벤트 emit, 시각 계산, 재개용 스냅샷, 계약 주입 |
| **러너 믹스인** | `runner.py` | `run()` | **Wave 기반 BFS 루프**. 발화 라우팅, 이동·외모 적용, 감염 처리, 시간 누적, 조기 종료, 목표 기간 판정. 가변 시간 분류/클램프 |
| **스텝 믹스인** | `step.py` | `_step_agent`, `_assemble_agent_prompt` | 단일 에이전트 한 턴: 프롬프트 조립 (**단일 진실 원천**), 메모리 압축 트리거, 토큰 트림, 언어 교잡 수정, LLM 호출 |
| **턴 믹스인** | `turn.py` | `_apply_turn_result` | LLM 응답 파싱 → `agent.memory` / `shared_log` / `edges` / DB 기록, `turn_complete` emit |
| **타깃 믹스인** | `targets.py` | `_resolve_targets`, `_compute_wave_targets` | 발화 대상 해석: 그룹 가시성, 같은 장소 필터, **낯선 이(stranger_N)** 할당, `all`/`self` 처리 |
| **로케이션 믹스인** | `location.py` | `_expand_zone_edges`, `_compute_zone_awareness`, 씬 메시지 빌더 | 위치 그래프(BFS 이동), **zone(구역) 인지**, 외부공간 격리, 도착/이탈 씬 메시지 |
| **미팅 믹스인** | `meeting.py` | `_apply_move_intents`, `_update_meeting_paths` | **만남 lock** (`move_to`에 장소가 아닌 **사람**을 지목): 추격·랑데부·집결, `meeting_update` emit |
| **감염 믹스인** | `infection.py` | `_apply_infection_wave`, `_sample_recovery_minutes` | **결정론적 SIR/SIS**. 같은 wave·같은 장소 접촉 → 확률 전염. 증상 진행·회복은 경과 분 기준. LLM은 상태·확률을 절대 안 봄 |
| **이벤트 믹스인** | `events.py` | `_execute_event` | 시나리오 이벤트: `system_message`, `agent_enter`, `agent_exit` |
| **시스템 믹스인** | `system.py` | `_run_system_agent` | **디렉터(system 에이전트)** 실행: 침묵·반복 감지, 개입/세계 사건 주입, `director_memo` 갱신, `director_call` emit |

## C-2. Agent (`ABM/agent.py`)

시뮬레이션 참여 등장인물 1명. **채팅 에이전트와는 별개 클래스다.**

- `system_prompt` — **서사 층**(사용자 소유: 페르소나 + 배경)
- `engine_contract` — **계약 층**(엔진 소유: 지도/시간/감염/관계 규칙). `set_engine_contract()`로 주입
- `memory` — 이 에이전트의 개인 대화 컨텍스트 (list of messages)
- `_memory_block` — 압축된 구조화 메모리 블록
- `relationships` — 이 에이전트 시점의 `{상대 key: 관계어}`
- `build_messages()` — `[system: 페르소나+계약+출력계약] + background_log + [memory_block] + memory + [ephemeral]`
- 로그 파일: `logs_graph/{name}.json`

## C-3. 조립·실행 경로

| Canonical | 파일 | 역할 |
|---|---|---|
| **헤드리스 러너** | `ABM/simulation/headless.py` | `run_config(SimStartConfig)` — **fresh start의 유일한 경로**. GUI `/start`와 CLI가 공유. `SimStartConfig → Agent들 + Simulation → sim.run()`. `RunResult` 반환 |
| **CLI** | `ABM/cli.py` | `python -m ABM.cli run` (시나리오→마크다운), `export` (과거 실행 재출력) |
| **되살리기 조립** | `backend/api/simulation/runtime/load.py`·`resume.py` | 얼려진 `config_json` + 에이전트 스냅샷에서 재구성 (별도 경로) |

## C-4. 지원 모듈 (`ABM/`)

| Canonical | 파일 | 역할 |
|---|---|---|
| **프롬프트 계약** | `prompt_contract.py` | **계약 층의 정본**. `build_world_contract` (지도+시간+감염), `build_output_contract` (JSON 스키마+`move_to`+`target`, 매 턴), `build_relationship_contract` (에이전트별 [아는 사람] 블록), `verify_contract` (기능은 있는데 지시어가 빠졌는지 검증) |
| **시간 분류기** | `time_classifier.py` | 가변 시간 모드에서 `classify_wave_time` (카테고리 선택) / `estimate_wave_minutes` (AI 직접 추론) LLM 호출 |
| **메모리 압축기** | `memory_compressor.py` | `compress()` — 에이전트 `memory`가 토큰 한도에 근접하면 구조화 DB 메모리(에피소드·사실·관계·자아상태)로 압축 후 비움 |
| **시스템 에이전트 러너** | `system_agent.py` | `run_system_agent()` — 디렉터 LLM 호출 (개입/세계사건/메모 판단) |
| **파서** | `parser.py` | `parse_json_response` (content/meta/targets 추출), `parse_json_extras` (`move_to`, `update_appearance`) |
| **프롬프트 계약 검증** | `prompt_contract.py::verify_contract` | 위와 동일 |
| **설정/상수** | `config.py`, `constants.py`, `simulation/_constants.py` | `LOG_DIR`, `TOKEN_LIMIT`, 요일 라벨, 디렉터 반복 감지 상수 |
| **LLM 타입** | `llm.py` | `LLMCall` 콜러블 타입 별칭 |
| **내보내기** | `export/markdown.py` (파이썬 골든 포매터), `export/csv.py` (위치 이력), `export/labels.py` (프론트 `sim/state.js`와 동기화되는 순수 헬퍼) |
| **코드 시나리오** | `scenarios/convenience_store.py` | `build_scenario()`로 코드에서 정의하는 시나리오 (GUI 시나리오와 별개) |

## C-5. SimDB (`ABM/db/`, `logs_graph/simulation.db`)

| 파일 | 담당 테이블/역할 |
|---|---|
| `base.py` | `SimDB` 클래스 (모든 하위 믹스인 조합) |
| `conn.py` | 커넥션 |
| `schema.py` | 스키마 정의 |
| `runs.py` | `simulation_runs` (실행 메타), `create_run`/`finish_run`, `agent_snapshots` (재개용 상태) |
| `messages.py` | `simulation_log` (턴별 발화) |
| `schema.py` → `sim_events` | 영속화된 SSE 이벤트 (`_PERSIST_EVENTS`) |
| `episodic.py` | `episodic_memory` |
| `semantic.py` | `semantic_memory` |
| `relationship.py` | `relationship_memory`, `relationship_history` |
| `self_state.py` | `agent_self_state` |
| `interview.py` | `interview_log` |
| `compression.py` | `compression_log` |

---

# Part D — 핵심 도메인 용어

## D-1. 시뮬레이션 실행

| 용어 | 정의 |
|---|---|
| **Wave (웨이브)** | BFS 한 레벨. 한 wave = **동시에 발화하는 에이전트 집합** (ThreadPoolExecutor 병렬). 다음 wave의 발화자는 이번 wave 응답의 `target` |
| **Turn (턴)** | 한 에이전트의 한 번 발화. wave 안에 turn N개 |
| **run_wave / disp_wave** | `run_wave` = 이번 `run()` 호출의 0부터 카운터 (시간·감염 계산용). `disp_wave` = `_wave_base + run_wave` = 누적 표시 wave (피드 뱃지·DB·이벤트 라벨용). `/continue`·`/resume` 후에도 wave 번호가 이어지도록 분리 |
| **run / run_id** | 한 번의 실행. `/start`·`/continue`·`/resume`마다 새 `run_id`. `simulation_runs`에 기록 |
| **shared_log** | 관전자 전지적 시점의 전체 발화 로그. `GET /logs`, 마크다운 내보내기 소스 |
| **edges (엣지)** | 관계 그래프 간선. `{source, target, emotion}` — 누가 누구에게 말했는가 |
| **에이전트 memory** | 각 에이전트의 **개인** 컨텍스트 윈도우 (그 에이전트가 보고 들은 것만). `shared_log`와 다름 |
| **start_agent (시작 에이전트)** | wave 0에서 첫 발화하는 에이전트 |
| **initial_active / 초기 등장** | `false`면 비활성으로 시작 → `agent_enter` 이벤트로 등장 |
| **조기 종료 (early stop)** | 아무도 발화 안 하는 wave가 `max_silence_waves`회 연속되면 `max_waves` 전에 종료 |
| **휴면 (dormancy)** | `early_stop_enabled=False`에서, 대화 상대 없이 혼잣말만 `max_silence_waves`회 연속한 에이전트를 밀집 재투입에서 제외 (`_solo_streak`). 누군가 도달하면 깨어남. 흩어진 가족이 무한 독백하는 것 방지 |
| **침묵 (silence)** | 발화 성공이 0인 wave. `end_reason` 값 중 하나 |
| **end_reason** | 종료 사유: `max_waves` / `target_duration` / `silence` / `no_agents` / `stopped` |
| **목표 기간 (target_duration_minutes)** | 시뮬레이션 내 경과 시간이 이 값에 도달하면 정상 종료. `max_waves`와 함께 쓰면 먼저 도달하는 쪽 |
| **step delay** | wave 사이 실제 대기 시간(초). 관전·서버 부하 조절용 |

## D-2. 프롬프트 구조

| 용어 | 정의 |
|---|---|
| **서사 층 (Narrative Layer)** | 프롬프트 중 **사용자 소유**: 페르소나, 배경, 세계 설정, 감독 노트. 시나리오에 저장 |
| **계약 층 (Engine Contract Layer)** | 프롬프트 중 **엔진 소유**: JSON 출력 스키마, `move_to` 의미, target ID 규칙, 위치/zone/외부공간 규칙, 시간 인식 포맷, 감염 증상 포맷. **저장 안 함 — 실행 시점 config로 매번 생성** (엔진 업그레이드 시 기존 시나리오도 새 계약을 받도록) |
| **world contract** | 계약 층 중 시뮬레이션 수명 동안 고정인 부분 (지도+시간+감염). Agent에 한 번 주입 |
| **output contract (출력 계약)** | 계약 층 중 매 턴 바뀌는 부분 (`<TARGETS>` 목록 등). `Agent.get_system_message()`가 호출마다 생성 |
| **relationship contract** | 에이전트별 `[아는 사람]` 블록. 화자 시점이라 사람마다 다름 |
| **출력 계약 오버라이드** | 사용자가 출력 형식 계약만 직접 쓴 문자열로 대체 (설정 뷰 "고급"). 지도·시간·감염 계약은 그대로 자동 최신화 |
| **ephemeral 메시지** | 메모리에 저장 안 하고 매 턴 새로 주입하는 메시지: `[현재 시각]`, `[현재 상황]`, `[몸 상태]` (증상 서사) |
| **extra_fields (출력 필드)** | 에이전트 응답 JSON의 추가 메타 필드. `content`·`target`은 항상 포함. 기본: `emotion`, `action`, `action_note` |

## D-3. 위치·인지

| 용어 | 정의 |
|---|---|
| **위치 그래프 (location graph)** | 장소 노드 + 연결(인접 리스트). 연결된 장소로만 이동 가능. 이동은 wave당 한 칸(BFS 경로) |
| **zone (구역)** | **위치 기반 인지 범위**. 같은 zone의 다른 장소에 있는 사람은 서로 존재를 인지 (대화는 여전히 같은 장소여야). `_agent_groups`(캐릭터 관계 그룹)와 **완전 별개** |
| **외부 공간 (exterior)** | 완전 격리 장소. 그 안의 에이전트는 아무도 못 보고 못 들음 (씬 메시지도 안 감) |
| **groups (그룹)** | 캐릭터 관계 그룹. "누구를 target할 수 있는가"의 서사적 가시성. zone과 무관 |
| **관계 지도 (relationships)** | `{상대 key: 내가 그를 부르는 관계어}`. **각자 자기 시점** (김봉남→채민경 "아내", 채민경→김봉남 "남편"). 대칭 불필요. 비어 있으면 기능 미사용 |
| **낯선 이 / stranger_N** | 인지 관계에 없는 상대를 만났을 때 부여되는 임시 ID (`stranger_1`, `stranger_2` …). 외모 묘사로 표시 |
| **공간 기반 인지 (perception_mode)** | `targeted` (기본) = 발화는 target 지목 상대에게만. `spatial` = ①같은 방 제3자 엿듣기 ②같은 zone 다른 방에 대사만 원거리 전달 ③혼잣말은 행동만 같은 방에 브로드캐스트 |
| **씬 메시지 (`[씬]`)** | 환경 관찰 메시지: 도착/이탈, 외모 변화, 독백 행동, 만남 취소. `speaker="씬"` |
| **만남 lock (`_meeting_intent`)** | `move_to`에 장소가 아닌 **사람**을 지목 → 그 사람을 따라감(추격/랑데부). 동석·다른 `move_to`·목표 이탈에서 해제 |

## D-4. 시간

| 용어 | 정의 |
|---|---|
| **시간 개념 활성** | `time_mode="variable"` **또는** (`fixed` + `time_per_wave > 0`). 꺼져 있으면 목표 기간·감염 진행이 동작 안 함 |
| **고정 모드 (fixed)** | wave당 `time_per_wave`분 균일 경과 |
| **가변 모드 (variable)** | wave가 끝날 때마다 LLM이 대화 내용을 보고 경과 분을 결정 |
| **time_estimation_mode** | 가변 모드 내에서: `category` (LLM이 카테고리 선택 → 범위 내 랜덤) / `ai` (LLM이 경과 분 직접 추론, 카테고리 min~max로 clamp) |
| **시간 카테고리 (time_categories)** | 가변 모드 분류 대상. `{id, label, min_minutes, max_minutes}`. 기본 4종 (식사·일반·혼자·취침) |
| **시간 점프 클램프** | LLM이 고른 경과 분을 엔진이 벽시계·동석 상황 기준으로 결정론적으로 상한 (`max_scene_jump_minutes` 동석 장면, `max_daytime_jump_minutes` 주간). 약한 모델이 재집결 장면을 건너뛰는 것 방지 |
| **강제 재투입 시간 (idle_minutes_schedule)** | 연속 침묵으로 전원 강제 재투입될 때 경과시킬 분. 침묵 회차가 늘수록 다음 값 |

## D-5. 디렉터 (system 에이전트)

| 용어 | 정의 |
|---|---|
| **디렉터 / system 에이전트 / 내레이터** | **셋 다 같은 것**. 내부 식별자는 `system`, 표시 이름은 기본 "내레이터" (변경 가능). 이야기 흐름을 감시하다 정체·반복 시 개입 |
| **개입 (intervention)** | 특정 에이전트를 지목해 메시지 주입 (`system_intervention` 이벤트) |
| **세계 사건 (world_event)** | 상황 자체를 서술해 여러 에이전트에 주입 (`world_event` 이벤트) |
| **감독 노트 (director_note)** | 사용자가 쓰는 **불변** 서사 목표·결말 조건. 디렉터가 매 개입마다 참조 (페르소나보다 우선) |
| **director_memo** | 디렉터가 **스스로** 갱신하는 진행 메모 (최근 N줄) |
| **시야 (digest_waves)** | 디렉터가 개입 판단 시 원문으로 되짚는 최근 wave 수 (엔진 clamp [2,20]) |
| **director_call 이벤트** | 디렉터가 돈 사실 + 비용(토큰·소요시간). 개입 여부와 무관하게 emit (성능 관측용) |
| **반복 감지 (D1 + D2)** | D1 = 어휘 유사도(축자 반복), D2 = 디렉터가 다이제스트를 읽고 주제 반복 판단. 상세: `docs/director-repetition-detection.md` |

## D-6. 감염병 모델

| 용어 | 정의 |
|---|---|
| **SIR / SIS** | 회복 후 면역(SIR) / 재감염 가능(SIS) |
| **환자 0번 (patient zero)** | 감염 시작점. 지정 안 하면 유행이 시작되지 않음 |
| **발병 시점 (onset wave)** | 환자 0번이 감염 상태로 전환되는 wave |
| **전염 확률 (transmission_probability)** | 같은 wave·같은 장소(외부공간 제외)에 있는 감염자 1명당 전염 확률 |
| **증상 단계 (symptom stage)** | `{min_minutes, max_minutes, symptom_text}`. **감염 후 경과 분**이 범위에 들면 그 서사가 주입됨. 첫 단계는 0분에서 시작해야 감염 직후에도 증상 |
| **회복까지 (recovery_min/max_minutes)** | 감염 시점에 [min, max]분에서 균등 샘플. max=0 = 자연 회복 없는 만성 |
| **원칙** | 감염 판정은 **전적으로 엔진**. LLM은 status·확률을 절대 안 보고 `symptom_text`만 받음 |

## D-7. 채팅 (RAG 메모리)

| 용어 | 정의 |
|---|---|
| **턴 (turn)** | 대화의 한 메시지 (user 또는 assistant). `turns` 테이블 |
| **대화 (conversation)** | 턴의 묶음. `conversations` 테이블 |
| **RAG 메모리** | 오래된 대화를 구조화 메모리로 변환하고, 현재 질문 키워드와 관련된 것만 선택 주입 |
| **아카이브 (archive)** | 컨텍스트 사용률이 `ARCHIVE_THRESHOLD`(75%) 초과 시 오래된 턴(최근 4턴 제외)을 메모리로 추출하고 `archived=1` 처리. UI엔 계속 보임, LLM엔 안 감 |
| **메모리 타입** | `fact` (사실·수치·고유명사) / `decision` (결정) / `pending` (미결). 문서상 `pending`, 코드 일부는 `decision` 병기 |
| **context_pct** | 응답 시점 컨텍스트 사용률 (0.0~1.0). 컨텍스트 바 게이지의 값 |
| **사고 수준 (thinking level)** | `off` / `low` / `medium` / `high`. 프로바이더 중립 4단계 → 벤더별 파라미터로 번역 (vLLM `enable_thinking`, OpenAI `reasoning_effort`, Anthropic `budget_tokens`) |
| **라우터 모드 (router mode)** | 대화 옵션. 메시지마다 LLM이 가장 적합한 에이전트를 자동 선택 (`async_route_agent`) |
| **멘션 (mention)** | `@에이전트명` 접두사로 특정 에이전트 직접 호출 |
| **채팅 에이전트 vs 시뮬레이션 에이전트** | 서로 다른 개념·다른 테이블. 모달로 한쪽→다른쪽 **복사** 가능 (이후 동기화 안 됨) |

---

# Part E — SSE 이벤트 타입 레퍼런스

## E-1. 채팅 SSE (`POST /api/conversations/{id}/chat`)

`search` → `thinking`(청크) → `answer`(청크) → `done` / `error`.
리더: `frontend/js/stream.js`.

## E-2. 시뮬레이션 SSE (`GET /api/simulation/stream`)

리더: `frontend/js/sim/run/sse.js`. **굵게** = `_PERSIST_EVENTS` (DB `sim_events`에 저장, 마크다운 내보내기 대상).

| 이벤트 | 의미 | 타임라인 표현 |
|---|---|---|
| `wave_start` | wave 시작, 발화자 목록 | 카드 speaking 표시, 턴 인디케이터 |
| `turn_start` | 한 에이전트 발화 시작 | 타이핑 인디케이터 |
| `turn_situation` | 그 턴에 주입된 `[현재 상황]`/`[몸 상태]` | 상황 카드 |
| `turn_complete` | 발화 완료 (content, meta, targets, tokens, time_str) | 발화 말풍선 + 엣지 추가 |
| `turn_error` | 발화 실패 (빈 응답/예외) | 오류 배지 누적 |
| `turn_language_fix` | 언어 교잡 감지 → 재시도 시작 | — |
| **`scene_event`** | 시나리오 이벤트 실행 (`system_message`/`agent_enter`/`agent_exit`) | 씬 카드 |
| **`system_intervention`** | 디렉터 개입 (특정 에이전트 지목) | 개입 카드 🎬 |
| **`world_event`** | 디렉터 세계 사건 | 세계 사건 카드 🌍 |
| `director_call` | 디렉터가 돈 사실 + 비용 | 디렉터 판단 카드 |
| **`agent_move`** | 에이전트 이동 (from → to) | 이동 카드 🚶 + 지도 갱신 |
| **`meeting_update`** | 만남 lock 생성/해소 (start/arrived/cancelled) | 만남 카드 🤝 |
| **`infection_update`** | 감염 상태 전이 (시드/전파/회복) | 감염 카드 🦠 + 뱃지 |
| **`appearance_update`** | `update_appearance`로 외모 변경 | 외모 카드 👗 |
| **`time_jump`** | 가변 시간 모드의 경과 분 결정. `mode`: `category`/`ai`/`idle`(전원 침묵·전원 휴면 강제 점프) | 시간 점프 카드 🕐 |
| `compression_start` / `compression_done` | 에이전트 메모리 압축 | — |
| **`simulation_end`** | 종료 (total_turns, end_reason) | "완료 \| 총 N턴 \| 사유" |
| `error` | 연결 오류 (브라우저 스펙상 메시지 없음) | 연결 오류 누적 |
| `ping` | keepalive | 무시 |

---

# Part F — 디버깅 시 호칭 규약 (예시)

이렇게 말하면 서로 바로 알아듣는다:

| ✅ 이렇게 | ❌ 이렇게 말고 |
|---|---|
| "**타임라인**에 **씬 카드**가 중복으로 뜬다" | "왼쪽 아래에 뭔가 두 번 나와" |
| "**인스펙터 → 컨텍스트 탭**에서 `[현재 시각]` **ephemeral 메시지**가 빠졌다" | "오른쪽 위 그거에 시간이 안 보여" |
| "**러너 믹스인**의 `run()` 루프, 발화 라우팅 블록" | "엔진 어딘가 for 문" |
| "**설정 뷰 → 위치 그래프 섹션**의 **공간 기반 인지** 토글" | "설정에서 그 체크박스" |
| "**계약 층**의 **output contract**에서 `<TARGETS>`가 비어 나온다" | "프롬프트에 타겟이 없어" |
| "**서버 레지스트리**의 `select()` 우선순위 ② (model 라운드로빈)" | "서버 고르는 로직" |
| "**브릿지**의 tenacity 재시도가 `NoProviderError`를 안 삼킨다" | "재시도가 이상해" |
| "`disp_wave`는 이어지는데 `run_wave` 기준 **감염 앵커**가 어긋난다" | "이어서 하면 wave가 꼬여" |
| "**디렉터**(system 에이전트)가 **감독 노트**를 무시하고 개입" | "내레이터가 말을 안 들어" |
| "**아카이브**가 `ARCHIVE_THRESHOLD` 전에 트리거된다" | "메모리가 너무 빨리 넘어가" |

---

## 부록 — 기술문서 세트

이 용어집을 기준으로 작성된 기술문서 (2026-09 현행화):

| 문서 | 내용 |
|---|---|
| [`architecture.md`](architecture.md) | 전체 구조 · 프로세스/스레드 모델 · 요청 경로 3종 |
| [`frontend.md`](frontend.md) | 화면 구조 · 상태 관리 · SSE 소비 · 렌더링 · 모듈 맵 |
| [`backend.md`](backend.md) | 채팅 파이프라인 · LLM 레이어 · 서버 레지스트리 · 프로바이더 · 브릿지 · 웹검색 |
| [`memory-system.md`](memory-system.md) | RAG 메모리 — 키워드 검색 · 아카이브 |
| [`simulation-overview.md`](simulation-overview.md) | Wave 모델 · 실행 라이프사이클 · SSE · SimDB · CLI |
| [`simulation-engine.md`](simulation-engine.md) | `Simulation` 믹스인 · `run()` 루프 순서 · 프롬프트 조립 · 계약 층 · 압축 |
| [`simulation-features.md`](simulation-features.md) | 위치/zone · 공간 인지 · 관계 지도 · 만남 · 시간 · 디렉터 · 감염 |
| [`api-reference.md`](api-reference.md) | REST 엔드포인트 · SSE 이벤트 스펙 · pydantic 스키마 |
| [`database.md`](database.md) | `memory.db` + `simulation.db` 전체 스키마 |
| [`spatial-perception.md`](spatial-perception.md) · [`director-repetition-detection.md`](director-repetition-detection.md) | 심층 (features에서 링크) |
| [`vectordb.md`](vectordb.md) | Vector DB 전환 검토 |

개발 가이드 `.claude/skills/{frontend,backend,abm}-skill/SKILL.md` 도 함께 현행화됨
(패턴 위주, 상세는 이 문서 세트로 링크).
