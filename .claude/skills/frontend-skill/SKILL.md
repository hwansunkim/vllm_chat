---
name: frontend-skill
description: "vLLM Chat 프론트엔드(HTML, CSS, JavaScript) 개발 가이드. UI 컴포넌트 추가, 스타일 수정, API 연동, SSE 스트리밍 처리 작업 시 참조."
---

# Frontend Development Guide — vLLM Chat

전체 구조·모듈 레퍼런스는 **`docs/frontend.md`**. 패널 호칭은 **`docs/glossary.md` Part A**.
이 문서는 "코드를 만질 때 지켜야 할 것"만 담는다.

## 핵심 제약 (반드시 준수)

- **빌드 도구 없음** — `package.json`·번들러·트랜스파일러 없음. 브라우저가 파일을 그대로 로드
- **ES 모듈** — `import`/`export`는 쓴다 (네이티브). npm 패키지는 금지, 서드파티는 CDN `<script>`만
- **새 JS 파일** → 기존 모듈에서 `import` 하거나 `index.html`에 `<script type="module">` 추가
- **XSS 방어** — 사용자·LLM 문자열을 `innerHTML`에 넣기 전 반드시:
  - `esc(str)` (HTML 이스케이프, `js/utils.js` 또는 `js/sim/state.js`)
  - 또는 `DOMPurify.sanitize()` / DOM API (`textContent`, `createElement`)
  - 마크다운은 `renderMarkdown()`(`js/markdown.js`)이 내부에서 sanitize
- **전역 상태** — 프레임워크 없음. 평범한 객체:
  - 채팅: `state` (`js/state.js`)
  - 시뮬레이션: `sim` (`js/sim/state.js`)
  - 반응성 없음 — 상태를 바꾸면 해당 `render*()`를 직접 호출

## 주요 패턴

### API 호출

```javascript
// 채팅 도메인: js/api.js 의 범용 래퍼
import { api } from './api.js';
const list = await api('GET', '/agents');           // → /api/agents
await api('POST', '/conversations', { title: '...' });

// 시뮬레이션 도메인: 직접 fetch (경로가 /api/simulation/... 로 길고 제각각)
const res = await fetch('/api/simulation/scenarios');
if (!res.ok) throw new Error(await res.text());
```

### DOM 생성 (XSS 안전)

```javascript
// textContent 는 자동 이스케이프
const div = document.createElement('div');
div.textContent = userText;

// 템플릿 문자열 + innerHTML 을 쓸 땐 값마다 esc()
el.innerHTML = `<span class="name">${esc(agent.name)}</span>`;
```

### SSE — 두 종류가 다르다

```javascript
// ① 채팅: POST + fetch 스트림 리더 (EventSource 아님 — POST 바디가 필요)
//    js/stream.js 의 readSSEStream(response.body, row) 참조
const res = await fetch(`/api/conversations/${id}/chat`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ content, thinking_level: state.thinkingLevel, web_search: state.webSearchEnabled }),
});
await readSSEStream(res.body, row);   // event: search → thinking → answer → done

// ② 시뮬레이션: GET + EventSource
//    js/sim/run/sse.js 의 connectSSE() 참조
const es = new EventSource('/api/simulation/stream');
es.addEventListener('turn_complete', e => { const d = JSON.parse(e.data); /* ... */ });
```

### 이벤트 리스너 등록

`js/main.js`가 로드 시점에 `init*Events()`를 호출한다 (`DOMContentLoaded` 미사용 —
`<script type="module">`은 기본 defer). 새 기능은 담당 도메인의 `init*Events()`에서 배선:
- 채팅: `js/main.js`에 `initXxx()` 추가
- 시뮬레이션: `js/sim/index.js`의 `initSimulationEvents()`에 추가

## CSS

`frontend/css/` 7개 파일, `index.html`에서 `<link>`. **CSS 변수 없음** — 색은 hex 직접
(`#4f46e5` 인디고, `#1e293b` 슬레이트900, `#e2e8f0` 보더 등, 기존 파일 참고). 새 컴포넌트
CSS는 해당 기능 파일에 추가하거나 독립 기능이면 새 파일 + `<link>`.

| 파일 | 범위 |
|---|---|
| `base.css` | 리셋, 공통 |
| `layout.css` / `sidebar.css` | 사이드바, `#main`, 채팅 헤더 |
| `messages.css` / `input.css` | 말풍선·thinking / 입력 영역·컨텍스트 바 |
| `modals.css` | 모든 모달 |
| `simulation.css` | 실행 뷰 + 설정 뷰 전체 (2200줄) |

## 상태 정규화 계약 (시뮬레이션)

`js/sim/state.js`의 순수 함수가 UI 입력을 백엔드 pydantic 스키마 모양으로 강제한다.
상태로 들어오는 모든 경로(불러오기·파일 import·편집)가 이걸 통과해야 한다 —
범위 밖 값은 백엔드가 422로 거부:
`normalizeWeekday`, `normalizeTemperature`, `normalizeTargetDuration`, `normalizeProbability`,
`buildInfectionModel`, `normalizeSymptomStages` 등.

> **이중 구현 주의**: `js/sim/state.js`의 순수 헬퍼 일부(`buildInfectionModel`,
> `infectionBadge`, `meetingNarration`, `simTimeLabel`, `getAgentIcon` 등)는
> `ABM/export/labels.py`에 파이썬으로도 있다. 한쪽을 바꾸면 다른 쪽도 —
> `tests/fixtures/*.md` 골든 테스트가 잡는다. 마크다운 내보내기(`js/sim/export/markdown.js`
> ↔ `ABM/export/markdown.py`)도 같은 규칙.

## UI 추가 체크리스트

1. HTML: `index.html` 적절한 위치에 마크업
2. CSS: 관련 `.css` 파일에 스타일 (색은 hex 직접)
3. JS: fetch는 `api.js`(채팅) 또는 직접(시뮬)
4. JS: 로직을 담당 모듈 또는 새 파일에
5. JS: `main.js` 또는 `sim/index.js`에서 초기화 배선
6. 새 파일이면 `index.html`에 `<script type="module">` (또는 기존 모듈에서 import)
