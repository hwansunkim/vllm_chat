# 인수인계: 이력 피드에 위치·상태·감염 꼬리표 표시

- 작성: Claude Code
- 작성일: 2026-11-05
- 브랜치: `history-location-badges` (미커밋 작업 트리)
- 기준 커밋: `3a31b1f` (master) → 작업 트리
- 관련 결정: 없음(관전 화면 표시만 — 엔진·DB·백엔드·LLM 입력 무변경)

## 바꾼 것
- `frontend/js/sim/runs/wave-context.js` (신규): 저장 이벤트 `wave_roster`(wave 시작 위치·감염·이동 중)와 `agent_status_change`(상태 진입·해제)로 `at(wave, agent)` 조회기를 만드는 순수 모듈.
- `frontend/js/sim/run/feed.js`: `historyEntryContext(entry, waveCtx)` 추가. 불러온 피드(`renderHistoricalFeed`)의 대사 머리를 `이름(위치 · 💤 상태 · 감염) W# → 대상(…)`으로 그린다.
- `frontend/js/sim/runs/replay.js`: `fetchChannelEvents` → `fetchFeedEvents`(채널 + 문맥 타입을 한 요청으로). 이력 보기 모달(`buildReplayDialogueItem`)도 같은 꼬리표, W 배지에 `time_str` 툴팁. 쓰지 않게 된 `agentLabel` import 제거.
- `feed.js` `CHANNEL_EVENT_TYPES`: `['phone_message', 'door_delivery']` → `['phone_message']`. 과거 피드(불러온 피드·이력 보기 모달)에서 **문 너머 전달 카드를 그리지 않는다** — 대사 꼬리표가 화자·대상의 서로 다른 위치를 보여 줘 중복(사용자 요청 2026-11-05). 이 상수는 과거 피드에서만 쓴다(실시간은 `sse.js`가 `door_delivery`를 직접 구독하므로 실시간 카드는 그대로). 휴대폰 카드는 본문이 이벤트에만 있어 유지.
- `frontend/css/simulation.css`: `.sim-feed-ctx`, `.sim-feed-ctx-inf.inf-*`(감염 색은 카드 뱃지와 같음).
- `tests/test_regressions.py`: `HistoryWaveContextTests` 3개(신규), `ChannelHistoryFeedDomTests` 하네스에 문맥 이벤트·감염 뱃지 스텁 추가와 새 DOM 테스트 1개, 정적 단언을 새 함수명으로 갱신.

## 의도한 동작
사용자 요청(2026-11-05): 실행 중에는 씬 알림으로 보이던 위치·상태가 이력 화면에 없어 상황을 다시 보기 불편하다.

- 화자: 로그 행에 저장된 `location`·`infection_status`(그 턴 스냅샷)를 우선, 없으면 roster.
- 대상: 같은 wave 의 `wave_roster`. `all`은 "전체", `self`·`system`은 제외(예전과 같음).
- 위치·감염은 **wave 시작 시점**(감염 판정과 같은 기준). 감염 S 는 표시하지 않는다.
- 상태 구간: enter@w … clear@c → [w+1, c). 진입은 턴 안, 해제는 wave 시작(자연 만료)이기 때문. `traveling` 상태는 roster 의 `traveling`으로 표시하고 상태 이벤트에서는 뺀다. 긴 상태 라벨은 12자에서 자르고 전체는 툴팁.
- 문맥 이벤트가 없는 구버전 실행: 화자 위치·감염(로그 행)만 나오고 나머지는 예전과 같다.
- 실데이터 확인: v16 case1 `095fe718` W1059 신짱구 안방·I, W1060 거실·I, 나머지 거실·S(감염 판정 근거와 일치). W31 아빠·누나는 회사/고등학교 + 업무 상태.

## 검증 요청
1. 전체 테스트(`AGENTS.local.md`의 `coffee` python). Claude는 `HistoryWaveContextTests`·`ChannelHistoryFeedDomTests`(7 passed)만 실행했다.
2. 상태 구간 규칙 [w+1, c)이 엔진과 맞는지. Claude 확인: clear 는 두 곳이다 — `runner.py:346` wave 시작 자연 만료, `runner.py:763` 상태 중 턴을 받은 사람이 재선언하지 않아 **턴 뒤에** 해제. 후자는 그 wave 시작엔 상태였지만, 그 턴에서 이미 말하고 있으므로(깨워짐) 꼬리표에 상태를 안 붙이는 쪽을 택했다. 이 해석이 관전에 오해를 주는지 판단해 달라.
3. `/runs/{id}/events?types=…`에 `wave_roster`를 명시 요청하는 것이 응답 크기·지연에 문제없는지(7일 실행 ≈ 1100건).
4. XSS: 위치명·상태 라벨·대상 이름이 모두 `esc`를 거치는지.
5. 문 너머 카드 제거로 잃는 정보가 있는지. 예: 문 너머로 지목했지만 전달되지 않은 경우(인접하지 않은 방) — 카드가 없으면 "다른 방 대상 지목"과 "실제 전달"을 이력에서 구별할 수 없다. 마크다운 내보내기(`export/markdown.js`)는 이 상수를 쓰지 않는지도 확인.

## 위험 지점
- `mergeFeedStream`은 채널 타입만 카드로 만들므로 문맥 이벤트가 섞여도 카드가 늘지 않는다(테스트로 확인). 다른 소비자가 `fetchFeedEvents` 결과를 채널 전용으로 가정하면 안 된다 — 모달의 "기록 없음" 판정은 채널만 걸러 쓴다.
- 실시간 피드는 바꾸지 않았다(실행 중에는 씬 알림이 이미 보인다).

## 범위 밖
- 실시간 피드의 같은 꼬리표, 마크다운 내보내기의 위치 표기.

## 검증 상태
- 2026-11-05 사용자 지시로 Codex 검증을 생략하고 머지했다. 머지 전 Claude가 전체 테스트를 실행했다: 975 passed.
- 위 검증 요청 2·3·5는 확인되지 않은 채 남아 있다. 이력 화면에서 문제가 보이면 이 메모를 근거로 검증을 요청한다.
