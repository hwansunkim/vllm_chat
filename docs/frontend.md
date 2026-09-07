# 프론트엔드

> 화면 구조·상태 관리·SSE 소비·렌더링 패턴·모듈 레퍼런스. 파트/패널 호칭은
> [`glossary.md`](glossary.md) Part A를 따른다. 상위 맥락은 [`architecture.md`](architecture.md).

---

## 1. 제약 — 빌드 도구가 없다

`frontend/`에는 `package.json`도, 번들러도, 트랜스파일러도 없다. 브라우저가 파일을
그대로 로드한다.

| 제약 | 결과 |
|---|---|
| **번들 없음** | 모듈은 브라우저 네이티브 ES import. `frontend/js/main.js`가 `<script type="module">` 하나로 진입. 새 JS 파일을 만들면 `index.html`에 `<script>` 추가 (또는 기존 모듈에서 import) |
| **npm 패키지 없음** | 서드파티는 전부 CDN `<script>` — markdown-it, KaTeX, highlight.js, DOMPurify, **D3 v7** |
| **JSX/TS 없음** | DOM은 `document.createElement` 또는 템플릿 문자열 + `innerHTML` |
| **XSS 직접 방어** | 사용자·LLM 문자열을 `innerHTML`에 넣기 전 `esc()` (HTML 이스케이프) 또는 `DOMPurify.sanitize()`. 마크다운은 `renderMarkdown()`이 내부에서 sanitize |

`esc()`는 `frontend/js/utils.js`와 `frontend/js/sim/state.js` 양쪽에 있다(순환 import
회피용, 구현 동일).

---

## 2. 화면 구조

세 개의 **뷰**가 상호 배타적으로 하나만 보이고, 그 위에 **사이드바**(항상)와
**모달**(그때그때)이 얹힌다. 마크업은 전부 `frontend/index.html` 하나에 있다.

```
body
├── #sidebar                     항상 표시 · 접기 가능 (localStorage)
│   ├── #conversation-list       대화 목록
│   └── .sidebar-footer          🤖 에이전트 · 🗂 메모리 · 🖥 서버 · 🎭 시뮬레이션
│
├── #main               ┐ 채팅 뷰
├── #sim-view           ├ 실행 뷰       ← .sim-hidden 토글로 하나만 표시
├── #sim-settings-view  ┘ 설정 뷰
│
└── #agent-modal, #mem-modal, #server-modal, #modal,
    #sim-export-modal, #sim-text-editor-overlay, …   모달·오버레이
```

뷰 전환:

| 함수 (`sim/views.js`) | 하는 일 |
|---|---|
| `showSimView()` | `#main`·설정 뷰 숨김 → 실행 뷰. 에이전트 카드·D3 그래프·지도 초기화, 시나리오 목록 로드 |
| `hideSimView()` | 실행 뷰 숨김 → `#main` |
| `showSettingsView()` | 실행 뷰 숨김 → 설정 뷰. `renderSettingsPage()` |
| `hideSettingsView()` | 설정 뷰 숨김 → 실행 뷰. 지도 재측정 |

채팅↔시뮬레이션 전환의 진입점은 사이드바 🎭 버튼(`#sim-btn`)과 실행 헤더 "← 채팅".

각 뷰의 하위 패널 이름은 [`glossary.md` Part A-3~A-5](glossary.md#a-3-채팅-뷰-구성요소) 참조.

---

## 3. 상태 관리

프레임워크가 없으므로 **평범한 모듈 스코프 객체 2개**가 전역 상태다. 반응성 없음 —
상태를 바꾸면 해당 `render*()` 함수를 직접 호출한다.

### `state` — 채팅 (`frontend/js/state.js`)

```js
{ currentConvId, isSending,
  thinkingLevel, currentServerThinkingLevel,   // 'off'|'low'|'medium'|'high'
  webSearchEnabled, agentList }
```

- `thinkingLevel` = 다음 메시지에 실어 보낼 값. `currentServerThinkingLevel` = 선택된
  서버의 기본값(서버 전환 시 위 값을 여기로 리셋).

### `sim` — 시뮬레이션 (`frontend/js/sim/state.js`)

시나리오 설정 전체 + 실행 런타임 상태를 한 객체에 담는다. 주요 필드:

| 그룹 | 필드 |
|---|---|
| 식별 | `status`, `currentScenarioId`, `currentScenarioName`, `selectedAgent` |
| 시나리오 | `agents[]`, `background`, `start_agent`, `max_waves`, `target_duration_minutes`, `extra_fields[]`, `events[]` |
| 위치 | `location_graph[]`, `perception_mode` (`targeted`\|`spatial`) |
| 시간 | `time_mode`, `time_per_wave`, `time_categories[]`, `time_estimation_mode`, `idle_minutes_schedule[]`, `max_scene_jump_minutes`, `max_daytime_jump_minutes`, `sim_start_time`, `sim_start_weekday` |
| 휴면 | `max_silence_waves` (고립 휴면 기준) |
| LLM | `server_id`, `temperature`, `token_limit`, `llm_max_tokens`, `lang_fix_*` |
| 하위 모델 | `system_agent{}`, `infection_model{}` |
| 런타임 | `eventSource`, `scenarios[]`, `agentEmotions{}`, `agentInfection{}`, `errorLog[]` |

### 정규화 헬퍼 계층 — 프론트가 지키는 백엔드 계약

`sim/state.js`의 순수 함수들이 **UI 입력값을 백엔드 pydantic 스키마가 받는 모양으로**
강제한다. 백엔드가 범위 밖 값을 422로 거부하기 때문에, 상태로 들어오는 모든 경로
(불러오기·파일 import·UI 편집)가 이 함수들을 통과해야 한다.

| 함수 | 규칙 |
|---|---|
| `normalizeWeekday` | 알 수 없으면 `'mon'` |
| `normalizeTemperature` / `normalizeAgentTemperature` | `[0, 2]` 클램프. 에이전트는 빈 값 → `null`(= 시뮬레이션 기본값) |
| `normalizeTargetDuration` | 빈 값/0 이하 → `null`(= 미사용). 그 외 1 이상 정수(분) |
| `normalizeProbability` | `[0, 1]` 클램프 + 반올림(부동소수 잡음 제거) |
| `normalizeSymptomStages` | `max < min`이면 min을 낮춤. id 유일화 |
| `buildInfectionModel` | 임의 입력 → `InfectionModelConfig` 모양. 필드 없으면(구버전) "꺼진 모델 + 기본 증상 3단계" |
| `durationPartsToMinutes` / `minutesToDurationParts` | (숫자+단위) ↔ 분 왕복. "딱 떨어지는 가장 큰 단위" 선택 |

> **이중 구현 주의**: 이 파일의 순수 헬퍼 일부(`normalizeWeekday`, `buildInfectionModel`,
> `formatDayHour`, `infectionBadge`, `meetingNarration`, `detectGender`, `getAgentIcon`,
> `agentLabel`, `simTimeLabel` …)는 `ABM/export/labels.py`에 **파이썬으로도** 구현돼 있다
> (마크다운 내보내기가 브라우저·CLI 양쪽에서 돌기 때문). 한쪽 문구/규칙을 바꾸면
> 다른 쪽도 고쳐야 하고, `tests/fixtures/*.md` 골든 테스트가 어긋난 쪽을 잡는다.

---

## 4. 부팅 흐름

`frontend/js/main.js` — `DOMContentLoaded` 없이 모듈 로드 시점에 즉시 실행:

```
loadModelStatus()      GET /api/model/status → 모델 배지
loadConversations()    GET /api/conversations → 대화 목록
loadAgents()           GET /api/agents → 에이전트 캐시

initSidebarEvents()        접기 상태 복원
initServerEvents()         서버 모달 · 서버 셀렉터 · 사고 컨트롤
initConversationEvents()   새 대화 모달 · 대화 열기
initChatEvents()           전송 버튼 · Enter · 입력창 오토그로우
initAgentEvents()          에이전트 관리 모달
initMemoryEvents()         메모리 저장소 모달
initMentionEvents()        @멘션 자동완성
initSimulationEvents()     시뮬레이션 전체 배선 (sim/index.js)
```

---

## 5. 채팅 UI 동작

### 5.1 전송 → 스트리밍 (`chat.js` → `stream.js`)

```
sendMessage() (chat.js)
  · state.isSending 가드, 입력창 비우기, 사용자 말풍선 추가, 로딩 버블
  · POST /api/conversations/{id}/chat
      body: { content, thinking_level: state.thinkingLevel, web_search: state.webSearchEnabled }
      ※ thinking_level은 항상 명시 전송 — null은 "서버 기본값에 맡김"이라는 별도 의미
  · 응답 body(ReadableStream) → readSSEStream(body, row) (stream.js)
  · 끝나면 loadConversations() 로 목록 갱신
```

`readSSEStream`은 `\n\n` 구분으로 SSE 프레임을 파싱해 이벤트별로 처리:

| event | 처리 |
|---|---|
| `search` | 검색 쿼리·결과 각주 박스를 말풍선 위에 삽입 (`sources.js`) |
| `thinking` | "🧠 사고 중..." 블록에 청크 누적 (textContent, 이스케이프 자동) |
| `answer` | 첫 청크에서 thinking 블록 접기 → 말풍선 생성. 80ms 스로틀로 `renderMarkdown` |
| `done` | 최종 마크다운 렌더, 메모리 참조 패널(`💡 메모리 N건 참조`), 에이전트 배지, `updateContextBar`, 제목 갱신, 아카이브 알림 |
| `error` | 말풍선에 오류 문구 |

### 5.2 대화 열기 (`conversations.js`)

`GET /api/conversations/{id}` → `data.turns`를 순회하며 `appendMessage()`
(`messages.js`). `turn`에 저장된 `memories_json`·`sources_json`·`thinking`·`context_pct`를
복원해 스트리밍 때와 같은 모양으로 렌더한다.

### 5.3 컨텍스트 바 (`context-bar.js`)

`updateContextBar(usage, server)` — `#context-fill` 게이지 폭·색(초록<0.6<노랑<0.8<빨강)과
`#context-info` 텍스트(`53.2% | 12,340 / 131,072 | 가용 118,732 [서버명]`). 서버가 입력
토큰을 반환하지 않으면 "한계 N tokens"만 표시.

### 5.4 사고 컨트롤 (`servers.js`)

🧠 버튼은 토글이 아니라 **4단계 팝오버 메뉴**(`#thinking-menu`). `servers.js`가 소유하고
`chat.js`의 `initThinkingControl()`이 배선. 서버 전환 시 그 서버의 기본 수준으로 리셋.

### 5.5 멘션 (`mention.js`)

입력창에 `@` 입력 → `state.agentList`에서 매칭되는 에이전트를 `#mention-dropdown`에 표시.
선택 시 `@이름 ` 삽입. 백엔드가 `@이름` 접두사를 파싱해 해당 에이전트로 라우팅.

---

## 6. 시뮬레이션 UI 동작

`sim/index.js`의 `initSimulationEvents()`가 모든 버튼·탭·입력을 배선한다.

### 6.1 설정 뷰

```
renderSettingsPage() (settings/page.js)
  · sim 상태 → 각 섹션 입력 필드 (렌더)
  · 섹션별 모듈 호출: renderAgentListInConfig, renderScenarioEvents,
    renderLocationGraph, renderSystemAgentConfig, renderInfectionConfig,
    renderTimeCategories, renderContractPreview, …
readConfigFromUI() (역방향)
  · 각 입력 필드 → sim 상태 (저장·시작 직전에 호출)
```

- **섹션 아코디언** (`settings/sections.js`) — 10개 섹션 접기/펼치기 + 왼쪽 네비 레일
  (`#sim-settings-nav`) 목차. 섹션 배지가 "설정됨/미설정" 표시.
- **`buildScenarioConfig()`** (`scenarios.js`) — `sim` 상태 → `/scenarios` API 페이로드.
  관계 지도 정규화, 목표 기간/온도/감염 모델을 서버가 받는 모양으로 변환.
- **엔진 계약 미리보기** (`settings/contract-preview.js`) — `POST /contract-preview`로
  "지금 설정이면 엔진이 만들 계약 문자열"을 읽어 표시 (읽기 전용).
- **시나리오 파일 export/import** — 백엔드 호출 없이 브라우저에서 JSON 다운로드/업로드.
  `schema_version` + `MIGRATIONS` 확장점.

### 6.2 실행 뷰 — 시작

```
startSimulation() (run/control.js)
  · readConfigFromUI() + 검증 (시작 에이전트, 에이전트 1명 이상)
  · clearErrorLog(), 타임라인 비우기, resetWaveCardBuffer()
  · POST /api/simulation/start  (sim 상태 전체를 SimStartConfig 모양으로)
  · setStatus('running') → connectSSE()
```

`setStatus()`가 상태 배지 + 버튼 활성/비활성(시작/이어서/중지/MD)을 한 곳에서 관리.

### 6.3 실행 SSE 소비 (`run/sse.js`)

`connectSSE()`가 `new EventSource('/api/simulation/stream')`을 열고 이벤트 타입별로
**타임라인 / 에이전트 카드 열 / 인스펙터**에 분배한다.

| SSE 이벤트 | 타임라인 (`run/feed.js`) | 그 외 |
|---|---|---|
| `wave_start` | wave 구분선 + 시각 뱃지 | 카드 `speaking` 표시, 턴 인디케이터 |
| `turn_start` | 타이핑 인디케이터 | — |
| `turn_situation` | 상황 카드 (접이식, 📍 / 🤒) | — |
| `turn_complete` | 발화 말풍선 + 메타 뱃지 | 카드 갱신, D3 엣지 추가, 컨텍스트 탭 자동 새로고침 |
| `turn_error` | — | 오류 로그 누적 (`run/errors.js`) |
| `scene_event` | 씬 카드 (📢 시스템 / 🎭 등장 / 🚪 퇴장) | 카드 active/exited 클래스 |
| `director_call` | 디렉터 판단 한 줄 (시야·토큰·소요시간·판정) | — |
| `system_intervention` | 개입 카드 🎬 `→ 대상` | — |
| `world_event` (레거시 재생만) | 세계 사건 카드 🌍 | — |
| `agent_move` | 이동 카드 🚶 | 카드 위치 뱃지, 지도 아바타 이동 |
| `meeting_update` | 만남 카드 🤝 (문구는 `state.js:meetingNarration`) | 카드 "→ 목표" 뱃지, 지도 점선 |
| `infection_update` | 감염 카드 🦠 | 카드 뱃지, 그래프 노드 색, 지도 아바타 |
| `appearance_update` | 외모 카드 🪞 | — |
| `time_jump` | 시간 판정 한 줄 (⏱ 카테고리/AI/클램프) | — |
| `simulation_end` | "완료 \| 총 N턴 \| 사유" | 진행 바 100%, 연결 종료 |
| `error` | — | 연결 오류 누적, 상태 `error` |

**wave 카드 버퍼링** — `system_intervention`·`director_call`·`time_jump`는
엔진이 wave 루프 상단/하단에서 emit하므로 해당 wave의 `wave_start`보다 **먼저** 도착할
수 있다. `_appendWaveCard`가 아직 시작 안 된 wave의 카드를 들고 있다가, 그 wave의
구분선을 그린 뒤 `flushPendingWaveCards`로 흘려보낸다.

### 6.4 인스펙터 3탭

| 탭 | 모듈 | 내용 |
|---|---|---|
| 관계 그래프 (`#sim-tab-graph`) | `sim/graph/d3.js` | D3 force 그래프. 노드=에이전트, 엣지=발화(감정색). 감염 노드 강조 |
| 위치 지도 (`#sim-tab-map`) | `sim/map/d3.js` | `location_graph`를 노드-링크로 그리고 에이전트 아바타를 위치에 배치·이동. 만남 추격선 |
| 컨텍스트 (`#sim-tab-context`) | `sim/context.js` | `GET /api/simulation/agents/{name}/context` → 그 에이전트가 실제로 받는 프롬프트 메시지 열 + 토큰 배너. 에이전트 카드 클릭으로 진입 |

탭 전환은 `context.js:switchTab()`. `TAB_PANES` 맵에 항목을 추가하면 탭이 늘어난다.

### 6.5 과거 실행 (`runs/`)

| 모듈 | 역할 |
|---|---|
| `runs/history.js` | 실행 이력 패널(`toggleRunHistory`) + 전체 실행 모달(`openAllRunsModal`) |
| `runs/replay.js` | `POST /load/{run_id}` → 스냅샷 복원, `renderHistoricalFeed`로 타임라인 재구성 |
| `runs/interview.js` | 실행 후 특정 에이전트에게 질문 (`POST /runs/{id}/agents/{name}/interview`) |

---

## 7. 마크다운·CSV 내보내기

- **마크다운** — 프론트(`sim/export/markdown.js`)와 파이썬(`ABM/export/markdown.py`)에
  **두 벌** 존재. 출력이 바이트 단위로 같도록 `tests/fixtures/golden_*.md` 골든 테스트로
  고정. 포맷을 바꾸면 양쪽을 함께 고쳐야 한다.
- 내보낼 요소는 **내보내기 옵션 모달**(`#sim-export-modal`)에서 체크박스로 토글
  (시간 구분·지문·이동·외모·내레이터 개입·감염·만남). "개입" 토글이 구 실행의
  `world_event`까지 함께 제어한다.
- **위치 이력 CSV** (`sim/export/csv.js`) — wave별 에이전트 위치. 감염병 접촉 분석용.

---

## 8. CSS

`frontend/css/` 7개 파일, `index.html`에서 `<link>`로 로드. `base.css`의 CSS 변수 활용.

| 파일 | 범위 |
|---|---|
| `base.css` | 리셋, CSS 변수, 공통 타이포/버튼 |
| `layout.css` | 사이드바, `#main` 레이아웃, 채팅 헤더 |
| `sidebar.css` | 대화 목록, 사이드바 푸터 |
| `messages.css` | 말풍선, thinking 블록, 메모리 참조 |
| `input.css` | 입력 영역, 컨텍스트 바, 사고/웹검색 버튼 |
| `modals.css` | 모든 모달·오버레이 |
| `simulation.css` | 실행 뷰 + 설정 뷰 전체 (2200줄, 가장 큼) |

새 컴포넌트 CSS는 해당 기능의 파일에 추가하거나, 독립 기능이면 새 파일 + `<link>`.

---

## 9. 모듈 레퍼런스

### 채팅 (`frontend/js/`)

| 파일 | 책임 |
|---|---|
| `main.js` | 부팅 |
| `state.js` | 채팅 전역 상태 |
| `api.js` | `api(method, path, body)` fetch 래퍼, `probeModels()` |
| `chat.js` | 전송 · Enter · 입력창 오토그로우 · 사고 컨트롤 배선 |
| `stream.js` | 채팅 SSE 리더 (search/thinking/answer/done) |
| `messages.js` | 말풍선 생성 · 로딩 버블 |
| `markdown.js` | markdown-it + DOMPurify, `renderMarkdown()`, `highlightCodeBlocks()` |
| `context-bar.js` | 컨텍스트 바 갱신 |
| `conversations.js` | 대화 목록 · 열기 · 새 대화 모달 |
| `agents.js` | 에이전트 관리 모달 (CRUD, 구조화 입력 → 프롬프트 자동생성) |
| `memories.js` | 메모리 저장소 모달 |
| `servers.js` | 서버 모달 · 모델 배지 · 사고 컨트롤 팝오버 (757줄) |
| `mention.js` | `@` 자동완성 |
| `sidebar.js` | 사이드바 접기 (localStorage) |
| `sources.js` | 웹 검색 출처 각주 |
| `agent-transfer.js` · `agent-import-modal.js` | 채팅↔시뮬 에이전트 복사 |
| `utils.js` | `esc`, `scrollToBottom`, `removeEmptyState`, `readJSON`/`writeJSON` |

### 시뮬레이션 (`frontend/js/sim/`)

| 파일 | 책임 |
|---|---|
| `index.js` | 시뮬레이션 이벤트 배선 |
| `state.js` | `sim` 상태 + 순수 헬퍼 (일부 `labels.py`와 동기화) |
| `views.js` | 3뷰 전환 |
| `scenarios.js` | 시나리오 CRUD · `buildScenarioConfig` · 파일 export/import |
| `context.js` | 인스펙터 탭 전환 · 컨텍스트 탭 렌더 |
| `resize.js` | 센터 패널 ↔ 인스펙터 드래그 리사이즈 |
| `run/control.js` | 시작/중지/이어서 · 상태 배지 |
| `run/sse.js` | 실행 SSE 리더 |
| `run/feed.js` | 타임라인 카드 렌더 전부 + wave 버퍼링 |
| `run/cards.js` | 에이전트 카드 열 |
| `run/errors.js` | 오류 배지 + 팝업 |
| `graph/d3.js` | 인스펙터 관계 그래프 탭 |
| `map/d3.js` | 인스펙터 위치 지도 탭 (951줄) |
| `runs/history.js` · `runs/replay.js` · `runs/interview.js` | 과거 실행 |
| `settings/page.js` | 설정 뷰 오케스트레이션 (`renderSettingsPage` / `readConfigFromUI`) |
| `settings/*.js` | 섹션별 렌더/수집 (agents, events, location-graph, system-agent, infection-config, time-categories, contract-preview, output-fields, target-duration, temperature, server-select, sections, textareas …) |
| `export/markdown.js` · `export/csv.js` | 내보내기 (프론트 포매터) |
| `utils/download.js` · `utils/json.js` · `utils/time.js` | 소형 헬퍼 |
