# QA Report (Post-modularization)

대상: 백엔드 `backend/api/simulation/` 패키지, `ABM/db/` 패키지, `ABM/simulation.py` 분해,
`frontend/js/sim/` 패키지 (19개 파일)
실행 일자: 2026-05-28

`tests/test_regressions.py` 결과: **PASS** (3/3)
- `VLLMProviderTests.test_extract_reply_accepts_whitespace_before_think_tag` ok
- `VLLMProviderTests.test_stream_parser_accepts_whitespace_before_think_tag` ok
- `ConversationTests.test_user_turn_is_saved_when_llm_stream_fails` ok

import 부트스트랩 점검:
- `from backend.api.simulation import router` → prefix `/api/simulation`, 19개 route ✅
- `from ABM.db import SimDB` → MRO `[SimDB, ConnMixin, Messages, Episodic, Semantic, Relationship, SelfState, Compression, Runs, object]` ✅
- `from ABM.simulation import Simulation; from ABM.agent import Agent; from ABM.constants import DEFAULT_EXTRA_FIELDS` ✅

---

## 1. 백엔드 패키지 구조 검증

| 항목 | 결과 | 비고 |
|------|------|------|
| `backend/api/simulation/` 디렉터리 + 8개 파일 | ✅ | `__init__.py`, `runner.py`, `runs.py`, `runtime.py`, `scenarios.py`, `schemas.py`, `sse.py`, `state.py` |
| `__init__.py`가 `router` 단일 export | ✅ | `backend/api/simulation/__init__.py:23` `__all__ = ["router"]` |
| `state.py`에 `_sim_lock = threading.Lock()` | ✅ | `backend/api/simulation/state.py:16` |
| 공통 `finalize_run` / `swap_event_queue` | ✅ | `runner.py:15` `swap_event_queue`, `runner.py:40` `finalize_run` — 3개 `_run` 함수(start/continue/resume)가 모두 호출 |
| `backend/main.py`의 import 경로 변경 없음 | ✅ | `backend/main.py:10` `from .api import ... simulation` — 패키지 파사드가 그대로 router 노출 |
| B1 race condition 수정 (start/continue/resume에서 `with _sim_lock:`) | ✅ | `runtime.py:26`, `runtime.py:131`, `runtime.py:184` |
| B4 NameError 방지 (db/run_sim_id 사전 None 초기화) | ✅ | `runtime.py:42-44`, `runtime.py:144-145`, `runtime.py:220-222` |
| B9 resume 시 events 무시 (`events=[]`) | ✅ | `runtime.py:275-281` 주석 + 코드 일치 |

---

## 2. ABM 패키지 구조 검증

| 항목 | 결과 | 비고 |
|------|------|------|
| `ABM/db/` 11개 파일 | ✅ | `__init__.py`, `base.py`, `conn.py`, `schema.py`, `messages.py`, `episodic.py`, `semantic.py`, `relationship.py`, `self_state.py`, `compression.py`, `runs.py` |
| `ABM/db/__init__.py`가 `SimDB` export | ✅ | `ABM/db/__init__.py:3` |
| D1 fix: `delete_run`이 `simulation_log`에서 `WHERE run_id=?` 사용 | ✅ | `ABM/db/runs.py:176`. `_MEMORY_TABLES` 튜플(L15-23)은 `simulation_log`를 포함하지 않으며 `sim_id` 컬럼 테이블만 가짐 |
| S5 fix: `agent_exit` 시 `_pending_wave.pop(agent_key, None)` | ✅ | `ABM/simulation.py:177` |
| S7 fix: `_compress_agent` 호출 시 `wave` 전달 | ✅ | `ABM/simulation.py:260` `self._compress_agent(agent, agent_key, wave)` |
| `_step_agent` 분해 (`_inject_incoming`, `_maybe_compress`, `_call_llm_for_agent`, `_apply_turn_result`, `_rollback_incoming`) | ✅ | `ABM/simulation.py:224-430`. Coordinator(`_step_agent`)는 47줄로 축소 |
| `ABM/constants.py` 존재 + 양쪽 import | ✅ | `ABM/constants.py:10`, `ABM/parser.py:5`, `ABM/agent.py:7` |
| `ABM/__init__.py`에서 기존 import 경로 유지 | ✅ | `Agent`, `Simulation`, config 상수 그대로 |

---

## 3. 프론트엔드 모듈 구조 검증

| 항목 | 결과 | 비고 |
|------|------|------|
| `frontend/js/sim/` 19개 파일 | ✅ | `index.js`, `state.js`, `views.js`, `scenarios.js`, `context.js`, `resize.js` + `run/{cards,control,feed,sse}.js`, `runs/{history,replay}.js`, `settings/{agents,events,output-fields,page}.js`, `utils/{json,time}.js`, `graph/d3.js` |
| `sim/index.js`가 `initSimulationEvents` export | ✅ | `frontend/js/sim/index.js:27` |
| `frontend/js/main.js`가 `./sim/index.js` import | ✅ | `frontend/js/main.js:7` |
| `sim/utils/time.js`에 단일 `fmtTime` | ✅ | 옵션 `{includeYear}`로 두 포맷 통합 |
| F8 fix: `stripCodeFence` 정규식 앵커 사용 | ✅ | `sim/utils/json.js:5` `/^\s*```...```\s*$/` — 시작/끝 앵커 모두 사용 |
| F1 fix: `CSS.escape` 사용 | ✅ | `sim/run/cards.js:15` (card.id), `sim/run/cards.js:74` (`getCardEl`) |
| 기존 `frontend/js/simulation.js` 삭제 | ✅ | `frontend/js/` 루트에 simulation.js 없음. grep으로 잔존 참조 0건 확인 |

---

## 4. API 경계면 교차 검증 (13개 경로)

라이브 `router.routes` 점검:

| 메서드 | 경로 | 위치 | 결과 |
|--------|------|------|------|
| POST | `/api/simulation/start` | `runtime.py:23` | ✅ |
| POST | `/api/simulation/stop` | `runtime.py:117` | ✅ |
| POST | `/api/simulation/continue` | `runtime.py:129` | ✅ |
| POST | `/api/simulation/resume/{run_id}` | `runtime.py:181` | ✅ |
| GET | `/api/simulation/status` | `runtime.py:294` | ✅ |
| GET | `/api/simulation/agents/{name}/context` | `runtime.py:303` | ✅ |
| GET | `/api/simulation/agents/{name}/memory` | `runtime.py:331` | ✅ |
| GET | `/api/simulation/logs` | `runtime.py:342` | ✅ |
| GET | `/api/simulation/edges` | `runtime.py:347` | ✅ |
| GET | `/api/simulation/stream` | `sse.py:25` | ✅ |
| GET | `/api/simulation/runs` | `runs.py:12` | ✅ |
| GET | `/api/simulation/runs/{run_id}` | `runs.py:18` | ✅ |
| GET | `/api/simulation/runs/{run_id}/log` | `runs.py:27` | ✅ |
| DELETE | `/api/simulation/runs/{run_id}` | `runs.py:33` | ✅ |
| GET | `/api/simulation/scenarios` | `scenarios.py:23` | ✅ |
| POST | `/api/simulation/scenarios` | `scenarios.py:37` | ✅ |
| PUT | `/api/simulation/scenarios/{sid}` | `scenarios.py:53` | ✅ |
| DELETE | `/api/simulation/scenarios/{sid}` | `scenarios.py:67` | ✅ |
| GET | `/api/simulation/default-output-format` | `scenarios.py:17` | ✅ |

프론트 호출처 교차 확인:
- `sim/run/control.js`: `/start`(L38), `/stop`(L66), `/continue`(L79) → 모두 백엔드에 존재 ✅
- `sim/run/sse.js:18`: `/api/simulation/stream` ✅
- `sim/context.js:35`: `/agents/{name}/context` ✅
- `sim/runs/history.js`: `/runs` (L26, L76), `/runs/{id}` DELETE (L67, L131) ✅
- `sim/runs/replay.js`: `/runs/{id}`(L17), `/runs/{id}/log`(L18), `/resume/{id}` POST(L102) ✅
- `sim/scenarios.js`: GET/POST/PUT/DELETE `/scenarios`(L9, L49, L54, L80) ✅
- `sim/index.js:92`: `/default-output-format` ✅

**SSE 이벤트 타입 페이로드 shape 매칭** (백엔드 `_emit` ↔ 프론트 `addEventListener`):

| 이벤트 | 백엔드 emit (`ABM/simulation.py`) | 프론트 handler (`sim/run/sse.js`) | 결과 |
|--------|----------------------------------|-----------------------------------|------|
| `wave_start` | L469 `{wave, agents}` | L21-29 `d.wave, d.agents` | ✅ |
| `turn_start` | L409 `{turn, wave, speaker, memory_size, est_tokens, token_limit}` | L31-34 `d.speaker` | ✅ |
| `turn_complete` | L341 `{turn, wave, speaker, targets, content, action_note, meta, memory_size, prompt_tokens, token_limit, reasoning_preview, new_edges}` | L36-48 모두 사용 | ✅ |
| `turn_error` | L420 `{turn, speaker, error}` | L50-54 `d.speaker` | ✅ |
| `scene_event` | L139 / L164 / L184 `{event_type, agent?, message, targets?}` | L56-65 `d.event_type, d.agent` | ✅ |
| `simulation_end` | L521 `{total_turns, edges_count, log_count}` | L67-77 `d.total_turns` | ✅ |
| `compression_start` / `compression_done` | L200 / L216 | (프론트 핸들러 미등록) | ⚠️ |
| `ping` | `sse.py:22` (백엔드 측 sentinel) | L86 `addEventListener('ping', () => {})` | ✅ |

`compression_start`/`compression_done`은 백엔드에서 emit하지만 프론트 SSE 핸들러가 무시한다. `addEventListener`로 등록되지 않은 SSE 이벤트는 브라우저가 조용히 폐기하므로 동작상 문제는 없으나, 추후 UX 개선 가능 포인트.

---

## 5. 회귀(잠재) 검증

| 항목 | 결과 | 비고 |
|------|------|------|
| `state.js` ← ... ← `state.js` 순환 | ✅ | `state.js`는 import 없음. 다른 모든 모듈이 `state.js`를 import — 트리 구조 ok |
| 프론트엔드 일반 순환 imports | ⚠️ | `scenarios.js → runs/history.js → runs/replay.js → scenarios.js` (cycle). 단, `applyScenario`와 `refreshRunHistory` 모두 함수 내부에서만 호출되므로 ESM은 안전하게 처리한다. 동작 영향 없음 |
| D3 상태 외부 노출 | ✅ | `_d3Sim`, `_d3Data`가 `graph/d3.js` 모듈 private. `getD3Sim()` accessor 제공 (L121). `initD3Graph`/`addD3Edge`/`exportGraph`만 외부 접근 |
| `turn_start` / `turn_complete` / `wave_start` 핸들러 존재 | ✅ | `sim/run/sse.js:21, 31, 36` |
| 사이드 이펙트로 인한 `simulation.js` 잔존 참조 | ✅ | live source 0건. (`_workspace_*/` 및 `graphify-out/manifest.json` 등 산출물에는 잔존하지만 코드 경로 외부) |
| index.html 의 sim-* DOM id 누락 | ✅ | JS에서 참조하는 id 중 누락은 모달 동적 생성(`sim-all-runs-modal`, `sim-replay-modal`, `sim-replay-feed`, `sim-replay-resume-btn`, `sim-replay-restart-btn`, `sim-replay-close-btn`, `sim-all-runs-close-btn`)뿐이며 실제 모달 코드에서 createElement로 생성됨 |

---

## 6. 추가 발견 이슈

### Issue Q1 (Medium) — `sim/run/cards.js` 의 부분적 `CSS.escape` 적용 누락

`frontend/js/sim/run/cards.js:25, 36, 38, 40, 49, 59, 60, 65, 68`

`card.id`(L15) 와 `getCardEl`(L74)에서는 `CSS.escape`로 안전하게 처리하지만, **하위 요소 id**들은 여전히 HTML escape(`esc()`)만 적용한다.
구체적으로:
```js
// cards.js:25,36,38,40
`simc-meta-${esc(f.name)}-${esc(agent.name)}`
`simc-tok-${esc(agent.name)}`
`simc-tokl-${esc(agent.name)}`
`simc-pre-${esc(agent.name)}`
```
그리고 `updateAgentCard()`(L48)는 selector를 만들 때 `esc()`/`CSS.escape` 어떤 것도 쓰지 않는다:
```js
// cards.js:49, 59, 60, 65, 68
document.getElementById(`simc-meta-${field}-${speaker}`)
document.getElementById(`simc-tok-${speaker}`)
document.getElementById(`simc-tokl-${speaker}`)
document.getElementById(`simc-pre-${speaker}`)
```

증상: agent.name이 한국어/공백/특수문자를 포함하면
- 카드 자체(L15)는 `CSS.escape`된 id를 가지므로 selector lookup이 가능
- 그러나 자식 요소들의 `id`에는 raw 한국어가 들어가고 (`esc()`는 `<`, `>`, `&`, `"`, `'`만 치환), `document.getElementById`는 정확한 문자열 매칭이라 부분적으로는 동작. 하지만 SSE에서 도착한 `d.speaker`도 raw 문자열 — 따라서 한국어 agent.name이라도 `getElementById`는 같은 문자열을 사용하므로 일치.
- 결과적으로 **현 시점에서는 동작하지만 안전성 보증이 약하다**. id에 공백·`.` 등이 들어가면 후속 코드에서 `querySelector('#simc-tok-...')` 같은 selector 사용 시 깨지므로, 일관성을 위해 `getElementById`만 사용하거나 모든 id 생성에 `CSS.escape` 적용을 권장.

수정 제안 (querySelector 미사용을 명문화하거나, id 생성 시점에 정규화):
```js
const safeName = CSS.escape(agent.name);  // 단, getElementById에는 raw 사용
// 또는 별도 dataset 사용 (data-agent="...") + querySelector('[data-agent="..."]')
```

### Issue Q2 (Low) — `sim/runs/history.js` 와 `replay.js` 가 만든 동적 모달의 클린업

`frontend/js/sim/runs/history.js:80, 116-118` (modal append/remove)
`frontend/js/sim/runs/replay.js:55, 92-94`

dynamic modal이 `escape` 키나 페이지 이동 시 cleanup 핸들러가 없다 (mousedown 이외의 dismiss 패스가 없음). 키보드 접근성/UX 이슈로만 분류되며 정합성 문제는 아니다.

### Issue Q3 (Low) — 프론트엔드 순환 import 경로 (`scenarios.js ↔ runs/`)

`frontend/js/sim/scenarios.js:6` `import { refreshRunHistory } from './runs/history.js';`
`frontend/js/sim/runs/replay.js:6` `import { applyScenario } from '../scenarios.js';`

세 파일 사이의 cyclic import가 존재한다(`scenarios.js → runs/history.js → runs/replay.js → scenarios.js`). 두 cross-edge 모두 함수 본문 내부에서만 호출되므로 ESM 모듈 평가 시점에 hoisted binding이 미초기화 상태로 참조될 위험이 없다. 즉 동작상 안전.

다만 의존성 그래프 관점에서 cleaner한 형태가 가능하다:
- `runs/replay.js`에서 `applyScenario`를 받는 대신, `replay.js`가 callback을 인자로 받아 `openRunReplay(runId, runNum, { onApply: applyScenario })` 형태로 호출자(`history.js`)에서 주입.

영향 없음. 노트로만 기록.

### Issue Q4 (Low) — `state.py` 의 `event_queue` 가 여전히 단일 슬롯

`backend/api/simulation/state.py:19-31` + `runner.py:15`

`swap_event_queue`는 이전 큐에 sentinel `None`을 push하는 방식으로 cross-run 이벤트 누수를 완화하지만, 여전히 `_sim["event_queue"]`는 단 한 개의 큐만 보유. 이전 QA 리포트(`qa_report.md`)의 B2가 "queue를 run_id별 dict로 분리"를 권장했던 점 대비, 현 구현은 **부분 수정**이다.

증상 시나리오:
1. `/start` → 큐A 생성
2. `/stream` 클라이언트1 접속 → 큐A 폴링
3. 시뮬레이션 종료 후 큐A에 sentinel `None` push, `_sim["status"]="done"`
4. 새 `/start` → 큐A에 미리 sentinel push (swap_event_queue가 처리), 큐B 설치
5. `/stream` 클라이언트2가 큐B를 잡는다 — 정상

검증된 정상 경로. **다만**:
- 큐A에 push된 sentinel이 클라이언트1에 의해 이미 소비된 후라면, swap의 `put_nowait(None)`이 중복 sentinel을 push하지만 무해.
- 클라이언트1이 떠난 시점에 큐A의 sentinel은 GC 대상.

→ B2 후속 fix는 충분한 수준이라 판단. **정합성 OK**.

### Issue Q5 (Low) — `delete_run` 의 트랜잭션 경계

`ABM/db/runs.py:164-179`

11개 테이블 `DELETE`가 한 connection의 직렬 실행으로 발생하지만 명시적 `BEGIN ... COMMIT` 트랜잭션 블록이 없다. SQLite는 default isolation으로 각 `execute`를 implicit transaction에 묶지만, 한 statement씩 커밋되지는 않고 마지막 `conn.commit()`까지 누적되므로 부분 실패 시 롤백된다 — 동작상 안전. 단 가독성 측면에서 `try/conn.commit()/except/conn.rollback()` 추가 시 더 명확.

---

## 7. 종합 평가

핵심 결정 사항 5건(D1, B1, B4, S5, S7) **모두 수정 적용 확인됨**. 백엔드/ABM/프론트 모든 패키지 분할이 외부 import 표면 호환을 유지하면서 완료되었고, FastAPI 라우터 19개, SSE 이벤트 7종 페이로드 shape 모두 프론트와 일치한다. 회귀 테스트 3건 모두 통과.

**즉시 차단 이슈 없음.**

부분 개선 권장 (모두 P2/P3 수준):
- Q1: `cards.js` 자식 요소 id의 `CSS.escape` 일관 적용
- Q3: `scenarios.js ↔ runs/` 순환 의존 정리 (callback 주입 패턴)
- Q4: SSE 큐 격리는 sentinel 보호로 사실상 cross-run leak 차단됨 (현재 안전)
- 미배선 SSE 이벤트(`compression_start`/`compression_done`) — 프론트 UX 확장 시 활용

검증 완료.
