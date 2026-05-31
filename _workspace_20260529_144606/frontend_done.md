# Frontend: simulation.js → sim/ ESM split

## 변경 파일

### 신규 추가 (ESM 모듈)
- `frontend/js/sim/state.js` — 공유 상태(sim, _expandedAgents, EMOTION_COLORS, esc, emotionColor, emotionClass, fmtK)
- `frontend/js/sim/views.js` — show/hide simulation / settings 뷰
- `frontend/js/sim/utils/time.js` — fmtTime(ts, {includeYear}) + statusIcon (중복 3곳 → 1곳)
- `frontend/js/sim/utils/json.js` — stripCodeFence() (F8 강화 정규식)
- `frontend/js/sim/settings/output-fields.js` — Output Fields 에디터
- `frontend/js/sim/settings/agents.js` — Agent accordion 에디터
- `frontend/js/sim/settings/events.js` — Scenario events 에디터
- `frontend/js/sim/settings/page.js` — renderSettingsPage + readConfigFromUI
- `frontend/js/sim/run/cards.js` — Agent Cards 렌더링 + updateAgentCard + getCardEl
- `frontend/js/sim/run/feed.js` — Feed + Typing indicator + Scene event + Wave indicator
- `frontend/js/sim/run/sse.js` — SSE 연결 (connectSSE, disconnectSSE)
- `frontend/js/sim/run/control.js` — start/stop/continue + setStatus
- `frontend/js/sim/scenarios.js` — loadScenarios, saveScenario, deleteScenario, newScenario, applyScenario
- `frontend/js/sim/runs/history.js` — Run history 패널 + all-runs 모달
- `frontend/js/sim/runs/replay.js` — Replay 모달 + resume/restart
- `frontend/js/sim/graph/d3.js` — D3 force graph (init, addD3Edge, exportGraph, getD3Sim)
- `frontend/js/sim/context.js` — Agent context window
- `frontend/js/sim/resize.js` — 패널 리사이즈 핸들
- `frontend/js/sim/index.js` — initSimulationEvents 진입점

### 수정
- `frontend/js/main.js` — `./simulation.js` → `./sim/index.js` 경로 변경

### 삭제
- `frontend/js/simulation.js` — 모놀리식 1484줄 파일 제거

## 버그 수정

1. **F1** — `frontend/js/sim/run/cards.js`
   - `card.id = \`simc-${CSS.escape(agent.name)}\`` (한국어/특수문자 ID 안전)
   - 모든 외부 카드 lookup은 `getCardEl(name)` 헬퍼로 통일 (sse.js에서 사용)

2. **F8** — `frontend/js/sim/utils/json.js` + `context.js`
   - 인라인 `if (raw.includes('```json'))` 분할 → `stripCodeFence(raw)` 함수
   - 정규식 `/^\s*\`\`\`(?:[a-zA-Z0-9_-]+)?\s*\r?\n?([\s\S]*?)\r?\n?\s*\`\`\`\s*$/` — language 태그, CRLF 모두 대응

3. **fmtTime 중복** — 3곳에 흩어져 있던 정의를 `utils/time.js` 한 파일로 통합
   - `refreshRunHistory`: `fmtTime(ts)` (MM-DD HH:MM)
   - `openAllRunsModal`, `openRunReplay`: `fmtTime(ts, { includeYear: true })` (YYYY-MM-DD HH:MM)

## 의존성 그래프 (순환 의존성 ES 모듈 라이브 바인딩으로 해결)

- 순환 1: `run/control.js` ↔ `run/sse.js` — `setStatus` ↔ `connectSSE/disconnectSSE`
- 순환 2: `scenarios.js` → `runs/history.js` → `runs/replay.js` → `scenarios.js` (applyScenario)

모든 순환 참조는 함수 본문 안에서만 사용되어 평가 시점 안전.

## 검증

- `node --input-type=module` 로 18개 모듈 import 해 SyntaxError/Named export not found 없음 확인
- DOM/d3 미존재로 실행은 안 되지만 모듈 그래프는 정상 로드됨
- `index.html`은 변경 없음 (이미 `main.js`만 `type=module`로 로드)

## 호환성

- `initSimulationEvents` export 시그니처 유지 — `main.js`에서 호출 그대로
- 모든 DOM ID/CSS 클래스 그대로 — `frontend/css/simulation.css` 영향 없음
- `/api/simulation/*` 백엔드 API 변경 없음
