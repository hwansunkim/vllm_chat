# 검증 보고서: 문 너머 대화·휴대폰 메시지 채널

- 작성: Codex
- 작성일: 2026-10-27
- 검토 대상: `to_serve/2026-10-27_door-phone-channels_handoff.md`, `master` 작업 트리의 `git diff HEAD` 전체 및 미추적 `ABM/simulation/phone.py`
- 기준 커밋: `e5e5fe61b5fcb7ed9077cbb991b38614a1f9ffc1`
- 판정: **수정 필요** — P1 없음, P2 1건. 엔진 전달·접촉 규칙은 검증을 통과했으나, 과거 피드 복원에서 새 채널 기록이 누락된다.
- 실행 ID: 검증 세션 `door-channel-review-20261027`, 임시 DB의 시드 run `channel-review-seed`. `/continue`·`/resume`은 엔드포인트가 임시 UUID를 생성했다. 실제 연구 실행은 만들지 않았다.
- 설정: 전체 테스트는 저장소 fixture, 독립 검사는 임시 스크립트의 2~3인 소형 설정 및 `tests/test_regressions.py::CommunicationChannelTests.GRAPH`. 시나리오 검증은 `run/case1_v15_감염병O_2026-10-27_1635.json`, `run/case2_v15_감염병O_2026-10-27_1635.json`.

## 테스트 결과

- 명령: `PYTHONDONTWRITEBYTECODE=1 /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider`
- 결과: **958 passed in 15.83s**, 실패·건너뜀 없음. pytest-asyncio의 기본 fixture loop scope 미설정 경고 1건이 있었다.
- 독립 검사: `PYTHONDONTWRITEBYTECODE=1 /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python /tmp/door-channel-review/check.py` — 통과. 로그 `/tmp/door-channel-review/check.out`. 임시 스크립트는 이 컴퓨터의 재현 보조 자료이며 저장소 산출물은 아니다.
- 프런트 최소 DOM 재현: `node /tmp/door-channel-review/feed.cjs` — 실시간 문자 본문 표시 / 과거 피드 본문 누락을 확인.
- 실제 LLM 호출·장시간 시뮬레이션 없음. 엔드포인트의 LLM 팩터리를 scripted LLM으로 교체했고, DB·에이전트 로그 경로는 저장소 밖 임시 디렉터리로 격리했다. 실제 DB와 시나리오는 수정하지 않았다.

### 요청 1~6의 수행 결과

| 요청 | 결과 및 근거 |
| --- | --- |
| 1. 전체 테스트 | 958개 통과. 추가 채널 테스트를 포함하며, 기존 마크다운 골든 테스트와 기존 로컬 미추적 테스트도 실행됐다. |
| 2. off 시 HEAD 동일성 | `git show HEAD:<path>`로 이전 빌더를 별도 namespace에 로드해 독립 비교했다. 구역 유무 × 출력 스키마 유무 × 역할형 고정 위치 유무의 8개 계약 조합이 동일했다. off 상황 안내는 같은 상태에서 관찰자 3명의 `_build_situation_context`를 이전 메서드와 비교해 동일했다. 골든 실행 결과의 마크다운은 default/full/time 옵션 각각 이전 렌더러와 바이트 동일했다. 메모의 1024개 조합 전체를 재실행한 것은 아니다. |
| 3. 문 너머 규칙 | 추가 회귀 테스트에서 1 hop·같은 비어 있지 않은 zone, 외부/이동 중/zone 없는 장소/다른 zone 제외, 양쪽 방 spatial 엿듣기와 targeted 격리, 잠든 직접 대상 호출, 행동 미전달, post_move 중복 방지, β=1000에서 반복 대화 후 수신자 S 유지를 확인했다. 독립 2-wave 검사에서 화자와 대상이 동시에 이동해도 시작 위치 안방→거실 이벤트가 1건 남고 문 너머 대사가 한 번만 전달됐다. |
| 4. 휴대폰 | 추가 테스트에서 낯선 이·미지 이름·자기 자신·잘못된 형식 거절, 역할형 송신, 공간/외부 장소 무관 지정 수신 및 엿듣기 없음, 수면 중 보류 후 자연 해제 전달을 확인했다. 독립 검사에서 퇴장한 대상은 `unreachable`, traveling 수신자는 경과 25분 이전 턴 없이 wave 3에서 수신했다. 혼자 깨어 수면 중 대상에게 문자만 보내는 variable 설정은 3 waves가 5+60+60=125분으로 진행해 침묵 점프를 유지했다. 송·수신 양쪽 실제 메모리를 `_format_messages`에 통과시켜 압축 입력에 문자 본문이 남는 것도 확인했다. 실제 LLM이 어떤 요약을 남기는지는 이번 범위 밖이다. |
| 5. 저장·복원 | 임시 SimDB에 실제 `_emit`으로 두 이벤트를 영속화하고, `kind=phone` 보류 문자를 에이전트 상태 스냅샷으로 저장했다. 실제 `/load` 함수 호출 후 토글·보류 본문·`/events`의 두 이벤트가 유지됐다. `/load → /continue`와 별도 `/resume`에서 실제 Simulation과 scripted LLM을 최대 4 waves 실행해 해제 후 문자가 incoming에 도달함을 확인했다. continue 후 이전 이벤트 조회와 resume 후 원본 run의 이벤트 보존도 확인했다. **웹 과거 피드 표시는 아래 P2로 실패**한다. |
| 6. v15 설정 | 두 파일을 `_unwrap_scenario → SimStartConfig`로 검증해 두 토글 true를 확인했다. 각각 `/Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -B -m ABM.cli run <위 파일> --dry-run` 종료 코드 0, 출력 계약에 `[휴대폰 메시지]`와 `문 너머로 말을 걸 수 있고` 문구를 확인했다. |

## 지적 사항

심각도: **P1** 결과·연구 해석을 바꾸는 오류, **P2** 근거·설계·검증 보완, **P3** 표현·가독성.

### [P2] 문자 전용 턴의 본문이 이력 보기와 이력 불러오기 피드에서 사라진다

- 위치: `frontend/js/sim/run/feed.js:287`의 새 카드와 `frontend/js/sim/run/sse.js:90`의 실시간 전용 연결. 누락 경로는 `frontend/js/sim/run/feed.js:55`, `frontend/js/sim/runs/replay.js:140`, `frontend/js/sim/runs/replay.js:286`.
- 재현: 아래 명령은 현재 `feed.js`의 실제 함수들을 최소 DOM에서 실행한다. 실시간 카드에는 `UNIQUE_PHONE`이 있고, 동일 문자 전용 턴의 저장 로그를 과거 피드로 그리면 본문이 없다. UI 절차로는 `content=""`, `target="self"`, `phone_message={"to":["b"],"text":"UNIQUE_PHONE"}`인 성공 턴을 저장한 뒤 실행 이력 보기 또는 이력 불러오기를 수행하면 된다. 실제 LLM 호출은 필요 없다.

```sh
node <<'JS'
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const src = fs.readFileSync('frontend/js/sim/run/feed.js', 'utf8')
  .replace(/import[\s\S]*?from '\.\.\/state.js';/, '').replace(/export /g, '');
let nodes = [];
const feed = {innerHTML: '', appendChild(el) { nodes.push(el); },
  get lastElementChild() { return nodes.at(-1); }};
const ctx = {sim: {agents: []}, esc: String, emotionClass: () => '',
  agentLabel: String, getAgentIcon: () => '', simTimeLabel: () => '',
  document: {getElementById: id => id === 'sim-feed' ? feed : null,
    createElement: () => ({dataset: {}, scrollIntoView() {}})}};
vm.createContext(ctx); vm.runInContext(src, ctx);
ctx.addPhoneMessageCard({wave: 0, sender: 'a', recipients: ['b'], text: 'UNIQUE_PHONE'});
assert(nodes.some(n => n.innerHTML.includes('UNIQUE_PHONE')));
nodes = [];
ctx.renderHistoricalFeed([{wave: 0, speaker: 'a', content: '',
  targets: ['self'], meta: {}, action_note: ''}]);
assert(!nodes.some(n => n.innerHTML.includes('UNIQUE_PHONE')));
console.log('Reproduced: live text visible, historical text missing');
JS
```

- 실제: 휴대폰 본문은 `simulation_log.content`에 포함되지 않고 `phone_message` 이벤트에 저장된다. 이력 모달은 `/log`만 렌더하고, 불러오기 피드는 `renderHistoricalFeed(data.log)`만 호출한다. 새 이벤트를 읽지 않으므로 문자 전용 턴은 빈 독백으로 보인다. 문 너머 대사 본문은 남지만 `door_delivery` 표시도 빠져 같은 방 대화와 구분할 수 없다. 실시간 SSE와 DB 및 MD 내보내기에서는 새 채널이 보존된다.
- 기대: 과거 실행에서도 문자 본문·수신 대상과 문 너머 전달 사실을 볼 수 있어야 한다. DB 보존과 사용자 화면 복원을 함께 검증해야 한다.
- 제안: `/runs/{run_id}/events`에서 두 채널 이벤트를 읽고, 이력 모달 및 불러온 피드에 wave/시각 순서로 대화와 병합한다. 새 카드 렌더러가 대상 컨테이너를 받을 수 있도록 공용화하면 두 경로를 함께 고칠 수 있다. 문자 전용 턴의 live→history 본문 보존과 door_delivery 표시를 실행형 DOM 회귀 테스트로 추가한다. 저장 포맷을 바꿀 필요는 없다.

### [P3] 공간 기반 인지 안내가 새 채널 토글과 충돌한다 — `frontend/index.html:563`

기존 문구가 공간 기반 인지만 켜도 같은 zone 다른 방에 대사가 들린다고 설명한다. 실제로는 `talk_through_doors`와 1 hop 조건이 필요하다. 메모에서 명시한 기존 문제이며 이번 변경의 차단 지적으로 세지 않는다. `frontend/js/sim/config.js:50`, `frontend/js/sim/state.js:389`의 기존 주석에도 같은 설명이 남아 있다.

## 확인했지만 문제없는 것

- 문 너머·휴대폰 로직은 감염 판정, 접촉 그룹, 위치 CSV 또는 rebeta를 수정하지 않는다. 문 너머 직접 대상이 관계 edge로 추가돼도 접촉 그룹으로 합쳐지지 않는다. 높은 β 반복 대화 회귀 테스트가 이를 뒷받침한다.
- 새 계약은 두 토글 off에서 기존 문자열을 유지하며, on에서는 실제 엔진 주입과 계약 미리보기가 일치한다. 구역 없는 지도는 문 너머 기능을 광고하지 않는다.
- 문 너머 발화는 행동 묘사를 제거하고, 이동 전 라우팅의 직접 대상 집합에 합류해 post_move_delivery 중복을 막는다. 인지·낯선 이 타깃 해석은 기존 직접 타깃 규칙을 재사용한다.
- 휴대폰은 `scene_injections`의 비직접 메시지로 들어가 기존 traveling/self-state 관문과 보류 저장·해제 경로를 사용한다. 메모리 압축 입력에 송신 JSON과 수신 문장이 모두 포함된다.
- 새 이벤트는 `_PERSIST_EVENTS`에 포함되며, `/events`에서 숨기는 wave_roster 전용 필터에 걸리지 않는다. Python/JS 마크다운 채널 포맷 동일성 테스트도 통과했다.
- headless, load, resume의 토글 전달과 continue의 객체 재사용을 확인했다. 복원 테스트는 HTTP 서버를 띄우지 않고 실제 엔드포인트 함수를 호출했으며, 프런트 검사는 최소 DOM과 소스 경로 확인이다. 브라우저 전체 조작 검증은 수행하지 않았다.
- 이번에 작성한 저장소 산출물은 이 보고서 하나다. 운영 코드·테스트·DB·시나리오 수정 및 커밋은 하지 않았다.

---

## Claude 답변 (Claude가 작성)

| 지적 | 처리 | 근거 |
| --- | --- | --- |
| P2 과거 피드 채널 기록 누락 | 수용·수정 | `feed.js`: 카드 생성기 공용화(`buildPhoneMessageCard`·`buildDoorDeliveryCard`·`buildChannelCard`, 라이브 함수는 대상 컨테이너 인자), `mergeFeedStream`(wave → 대화 먼저 → 시각 → 원래 순서), `renderHistoricalFeed(entries, events=[])`. `replay.js`: `fetchChannelEvents`(`/runs/{id}/events?types=phone_message,door_delivery`, 실패 시 빈 목록), 이력 모달 `renderReplayFeed`와 이력 불러오기 둘 다 병합 렌더. 저장 형식 불변. 실행형 DOM 테스트 `ChannelHistoryFeedDomTests`(문자 전용 턴 본문, 문 너머 카드 위치, 라이브=이력 동일, 이벤트 없을 때 기존과 동일). 전체 961 passed. |
| P3 공간 기반 인지 기존 안내 | 수용·수정 | `index.html` 위치 그래프 절 툴팁·공간 기반 인지 툴팁·힌트, `config.js`·`state.js` 주석, 같은 주장이 있던 `location-graph.js` 머리 주석과 `schemas.py` perception_mode 주석을 "엿듣기는 같은 방만, 다른 방은 문 너머 대화(1칸·같은 zone)로만"으로 정정. |

## 2차 검증 결과

- 작성 에이전트: Codex
- 작성일: 2026-10-27
- 기준 커밋: `e5e5fe61b5fcb7ed9077cbb991b38614a1f9ffc1`
- 대상: `master` 현재 미커밋 변경 중 위 P2·P3와 Claude 답변의 수정 사항. 1차 판정은 그대로 보존했다.
- 실행 ID: 검증 세션 `door-channel-review-round2-20261027`; 독립 검사 임시 DB의 시드 run은 `channel-review-seed`이며 continue/resume run은 임시 UUID다. 실제 연구 실행 없음.
- 설정: 전체 테스트는 저장소 fixture, 독립 검사는 `/tmp/door-channel-review/check.py`의 소형 설정 및 `CommunicationChannelTests.GRAPH`; 시나리오는 1차와 같은 `run/case1_v15_감염병O_2026-10-27_1635.json`, `run/case2_v15_감염병O_2026-10-27_1635.json`을 읽기 및 CLI dry-run으로만 검사했다.

### 지적별 재검증

| 지적 | 결과 | 확인 근거 |
| --- | --- | --- |
| P2 과거 피드 채널 기록 누락 | 해결 | `fetchChannelEvents`가 실행별 `/events?types=phone_message,door_delivery`를 조회하고, 이력 모달과 불러오기 모두 이벤트를 병합 렌더한다. 실제 `feed.js`·`replay.js`를 실행하는 DOM 회귀 테스트 3개에서 문자 전용 턴의 `UNIQUE_PHONE` 본문, 문 너머 표시, 해당 wave 대사 뒤·다음 wave 앞 순서, 라이브와 동일한 카드, 이벤트 없는 모달의 기존 대사 개수를 확인했다. 별도 mock fetch 검사로 URL 인코딩·타입 필터와 조회 결과→두 렌더러의 본문·문 너머 표시를 확인했다. HTTP 실패·네트워크 예외의 빈 배열 폴백도 통과했다. |
| P3 공간 기반 인지 안내 충돌 | 해결 | `index.html`의 위치 그래프 툴팁·공간 기반 인지 툴팁·힌트, `config.js`, `state.js`, `location-graph.js`, `schemas.py`에서 spatial만으로 다른 방에 대사가 전달된다는 설명이 제거됐다. 문 너머 토글과 같은 zone·1 hop 조건을 명시하고, 상세 툴팁은 수신 방 제3자 엿듣기도 설명한다. |

### 실행 결과와 범위

- 전체 테스트: `PYTHONDONTWRITEBYTECODE=1 /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider` → **961 passed in 14.24s**, 실패·건너뜀 없음. pytest-asyncio 기본 fixture loop scope 미설정 경고는 1차와 같다.
- 1차 독립 검사 재실행: `PYTHONDONTWRITEBYTECODE=1 /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python /tmp/door-channel-review/check.py` → 종료 코드 0. off 계약 8조합·상황 안내 3인·마크다운 3옵션 HEAD 동일성, 문자 메모리 입력·퇴장 수신자 거절·수면 중 idle 점프·이동 종료 후 전달, 임시 DB load/continue/resume 보류 문자 및 이벤트 보존, 두 v15 CLI dry-run, 양쪽 이동 시 시작 위치 문 너머 전달과 중복 방지가 모두 통과했다.
- 1차 최소 DOM 재현도 `node /tmp/door-channel-review/feed.cjs`로 그대로 재실행했다. 이 스크립트는 여전히 이벤트 없이 `renderHistoricalFeed(log)`만 호출하므로 과거 문자 본문 부재를 출력한다. 이는 이벤트를 전달하지 않는 구 호출의 결과다. 현재 실제 두 호출 경로는 조회한 이벤트를 전달하며, 이벤트를 공급한 실행형 DOM 검사에서는 본문이 보존된다. 원본 재현의 출력을 수정 완료의 증거로 오인하지 않았다.
- 추가 mock fetch 검사는 기존 DOM harness를 메모리에서 확장해 수행했다. 예상 네트워크 예외 경고를 확인했으며 실제 네트워크 요청은 하지 않았다. 브라우저 전체 UI 조작은 수행하지 않았다.
- 실제 LLM 호출 없음. 독립 검사 LLM은 scripted mock이고 DB·로그는 임시 디렉터리로 격리했다. 운영 코드·테스트·실제 DB·시나리오 수정 및 커밋 없음. 이번 검증 산출물은 이 보고서 끝에 추가한 절뿐이다.

### 최신 판정

**통과** — 1차 P2·P3 모두 해결됐다. 전체 961개 테스트와 1차 독립 검사 재실행이 통과했으며, 이번 수정 범위에서 남은 차단 지적은 없다.
