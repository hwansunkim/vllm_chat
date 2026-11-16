# 인수인계: 엇갈린 대사 표시 (동시에 나온 말)

- 작성: Claude Code
- 작성일: 2026-11-05
- 브랜치: `crossed-speech` (미커밋 작업 트리)
- 기준 커밋: `ad949e6` (master) → 작업 트리
- 관련 결정: `docs/decisions.md` 2026-11-05 "엇갈린 대사 표시" (프롬프트 계약 변경)

## 바꾼 것
- `ABM/simulation/status.py`: 표식 상수 `CROSSED_KEY = "crossed"`.
- `ABM/simulation/runner.py` next_wave 조립: 이번 wave 에 성공한 턴을 한 수신자(`acted_now`)가 받는 `routed` 항목(직접·문 너머·엿듣기·이동 후 보강)에 `crossed: True`. `scene_injections`(도착·이탈·휴대폰·행동 관찰)와 보류분(`_hold_incoming`)에는 붙이지 않는다.
- `ABM/simulation/step.py` `_inject_incoming`: `crossed` 항목은 대사(·action_note) 아래에 `CROSSED_NOTE` 줄을 붙인다:
  `(↔ 동시에 나온 말: 네가 방금 한 말·행동과 같은 때에 나왔다. 말한 사람은 아직 그걸 모르고 말했다.)`
- `tests/test_regressions.py`: `CrossedSpeechTests` 3개.
- 문서: `docs/decisions.md`, `docs/simulation-engine.md` 17번, `docs/simulation-features.md` §17.

## 의도한 동작
같은 wave 의 턴은 병렬이라, 깬 짱구의 "배고파, 밥 줘"(→엄마)와 엄마의 "일어나!"(→짱구)가 같은 wave 에 나오면 다음 wave 짱구 기억엔 `나: 배고파 → [엄마] 일어나!` 순서로 쌓여 엄마가 듣고도 일어나라고 한 것처럼 읽혔다(사용자 보고). 수정 후:

- 양쪽 다 그 wave 에 턴을 했으면 서로 받는 말이 모두 표시된다.
- 들은 뒤 답하는 정상 순서(받는 사람이 그 wave 에 턴이 없었음)는 표시하지 않는다.
- 표식은 재개 입력(`pending_wave_json`)과 실패 wave 재시도 입력에 그대로 실린다(같은 입력 재시도 원칙).
- 엔진 판정(위치·시간·감염·라우팅·보류)은 바뀌지 않는다. LLM 입력 문구만 늘어난다.

## 검증 요청
1. 전체 테스트(`coffee` python). Claude 실행: 978 passed.
2. 새 테스트가 표식 없이는 실패하는지(`runner.py` 표식 블록을 몽키패치로 끈 상태).
3. 표식 조건의 반례 찾기:
   - 수신자가 상태 중(수면)인데 직접 지목돼 턴을 받은 경우 — 그 wave 에 턴이 없었으면 표시 안 됨이 맞는지.
   - 1-wave 유예(방금 자리를 뜬 상대에게 닿는 말), 이동 후 보강 배달, spatial 엿듣기에서 "동시" 설명이 사실과 맞는지.
   - 수신자의 그 wave 턴이 독백·행동뿐이었을 때 문구("한 말·행동")가 어색하지 않은지.
   - 같은 wave 에 보류(`held_now`)된 사람과 정상 배달된 사람의 경계.
4. 기억 압축(`memory_compressor.compress`)이 이 설명 줄을 대사의 일부로 오인해 사건·사실로 남기지 않는지 — 압축 프롬프트가 incoming user 메시지를 어떻게 읽는지 확인.
5. 토큰 영향: 대화가 활발한 wave 에서 엇갈림이 얼마나 자주 붙는지(v16 기록으로 추정 가능 — 같은 wave 에 서로를 지목/수신한 쌍 수).

## 위험 지점
- 다인 대화에서 대부분의 말이 "동시"로 표시될 수 있다(모두가 매 wave 말하면 매 wave 모든 수신 말이 동시). 사실이지만 문구가 반복된다. 빈도는 검증 5로 확인.
- `strip_held_kind` 뒤에 사본을 만들어 표식을 붙인다 — 원본 `routed` 항목은 바뀌지 않는다.

## 범위 밖
- 이력 화면·마크다운 내보내기의 "⇄ 동시" 표시(로그에 표식이 남지 않음 — 필요하면 로그의 wave·화자·대상에서 계산 가능).
- Methods 원고 반영(프롬프트 계약 변경을 Methods 에 쓸지는 `docs/methods-review-guidelines.md`에 따라 따로 판단).
