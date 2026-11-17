# 인수인계: 시각 라벨에 일차 표시

- 작성: Claude Code
- 작성일: 2026-11-17
- 브랜치: `day-in-clock` (미커밋 작업 트리)
- 기준 커밋: `be09adf` (master) → 작업 트리
- 관련 결정: `docs/decisions.md` 2026-11-17 "시각 라벨에 일차 표시" (프롬프트 계약 변경)

## 바꾼 것
- `ABM/simulation/_constants.py` `format_sim_time`: `요일 오전/오후 N시 M분` → `N일차 요일 오전/오후 N시 M분`(일차 = `총 분 // 1440 + 1`). `Simulation._format_time_str`가 여기에 위임하므로 다음이 모두 바뀐다.
  - 매 턴 ephemeral `[현재 시각: …]` (`step.py`)
  - 디렉터 `[현재 시각]` (`system.py`)
  - 상태 진입·해제 `until_time_str`, time_jump `end_time_str`, 감염 구간 `start/end_time_str`, 시간 분류기 `now_str` (`runner.py`, `infection.py`, `location.py`, `backend/.../queries.py`)
  - 로그 `time_str` → 마크다운 내보내기 wave 머리(`### 🕐 1일차 월요일 …`)
- `ABM/simulation/location.py` `_time_anchor_label`: 앞에 일차를 붙이던 코드를 지우고 `_format_time_str`만 쓴다. 출력은 그대로다(`[날짜 변경] 2일차 화요일 …`).
- `ABM/prompt_contract.py` `_TIME_CONTRACT`: 형식 설명을 `N일차 요일 + 오전/오후 시각`으로 고치고, 두 문장을 추가했다. 하나는 "N일차는 시작일 = 1일차, 배경 설명 속 '오늘'·'어제'는 1일차 기준이니 며칠째인지는 [현재 시각]의 일차로 판단하라"이다. 다른 하나는 "자정을 넘기면 일차와 요일이 함께 넘어간다"이다.
- 구버전 로그용 fallback도 같은 형식으로 맞췄다: `ABM/export/labels.py` `sim_time_label`, `frontend/js/sim/state.js` `simTimeLabel`(fixed 모드, `time_str`이 없는 로그 전용).
- 테스트:
  - 기대값 갱신: `TimeJumpEndTimeStrTests` 2개, `SystemAgentTimeAndOrderTests` 디렉터 시계, `MarkdownGoldenTests` 머리 단언.
  - 골든 재생성: `tests/fixtures/golden_*.md`. 차이는 `1일차 ` 추가 10곳뿐이다.
  - 신규: `ClockDayIndexTests` 3개, `test_agent_clock_carries_the_day_index_across_midnight`.
- 문서: `docs/decisions.md`, `docs/simulation-engine.md`, `docs/simulation-features.md`, `docs/database.md`.

## 의도한 동작
- 사용자 보고(2026-11-17): 기억에는 "1일차 월요일"처럼 일차가 보이는데 매 턴 시계와 `scenario.md`에는 요일만 보인다.
- 실측 혼선: v16 case1 `ba39a953`(설정 `run/case1_v16_감염병O_2026-10-30_1601.json`) W1076, 7일차 일요일 22:19.
  - 짱아: "너 내일 나가는 게 아니라 격리 1일차라고!"
  - W1077 짱구: "정말로 내일부터 1일차인 거지?!"
  - 배경 설명의 "오늘(월요일, 1일차)"과 시계의 "일요일"만으로는 며칠째인지 알 수 없었다.
- 엔진 판정(시각 계산·감염·이동·이벤트 발동)은 바뀌지 않는다. 바뀌는 것은 문자열 라벨과 LLM 입력뿐이다.
- 일차 공식은 시간 경과 마커·압축 구획 머리(`format_sim_day_period`)와 같다.

## 검증 요청
1. 전체 테스트(`coffee` python). Claude 실행 결과: 983 passed.
2. `_format_time_str`/`time_str`/`until_time_str`/`end_time_str` 문자열을 **파싱**하거나 형식을 가정하는 소비자가 있는지 확인해 달라. Claude는 grep으로 파싱 코드는 찾지 못했다.
   - 백엔드 API
   - 프론트(실시간 피드 구분선, 상태 카드 툴팁, CSV 내보내기 `wave_end_time`, 이력 피드)
   - 분석 스크립트
3. 일차가 두 번 붙는 경로가 없는지: `_time_anchor_label`, 압축 원문 머리, 디렉터 프롬프트.
4. 구버전 실행(일차 없는 `time_str`)과 신규 실행이 화면·내보내기에서 섞일 때 문제가 없는지. 예: 이력 피드에서 fallback(일차 있음)과 저장된 `time_str`(일차 없음)이 한 실행 안에 섞이는 경우.
5. `_TIME_CONTRACT`의 추가 문장이 시나리오 배경 설명과 충돌할 여지. 예: 시작일이 1일차가 아닌 것으로 서술된 시나리오, 배경 설명에 '오늘'이 없는 시나리오.
6. 토큰 영향: 매 턴 `[현재 시각]`에 약 3~4자, 시간 계약에 2문장.

## 위험 지점
- `scenario.md`·CSV·화면 표기가 일차를 포함하게 바뀐다. 이전 실행과 나란히 비교하는 분석 스크립트가 시각 문자열을 키로 쓰면 맞지 않는다.
- 프롬프트 계약 변경이므로 이후 실행은 v16 이전 실행과 대화 양상이 다를 수 있다.

## 범위 밖
- 시나리오 배경 설명 문구(예: "오늘(월요일, 1일차)") 수정.
- Methods 원고 반영. `docs/methods-review-guidelines.md`에 따라 따로 판단한다.
