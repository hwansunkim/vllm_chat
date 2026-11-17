# 검증 보고서: 시각 라벨에 일차 표시

- 작성: Codex
- 작성일: 2026-11-17
- 검토 대상: `to_serve/2026-11-17_day-in-clock_handoff.md`, 브랜치 `day-in-clock`의 미커밋 작업 트리 (`git diff`)
- 기준 커밋: `be09adf5dceef177311cec2dfde85416cf59bde5`
- 대상 파일: 변경된 ABM 시각 포맷·프롬프트·프론트 fallback 및 `backend/`, `frontend/js/`, `ABM/`, `tools/`, `run/analysis*/`의 소비자 전체 검색
- 판정: **수정 필요** — P2 1건. 전체 테스트는 통과하지만 지정된 분석 도구에 형식 호환성 회귀가 있다.

## 테스트 결과

- 실행 ID: `day-in-clock-pytest-20261117-01` (실제 시뮬레이션 실행 아님, 설정 파일 없음)
- 명령: `PYTHONDONTWRITEBYTECODE=1 /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider`
- 결과: **983 passed in 16.00s**, 종료 코드 0.
- 로그: `/tmp/day-in-clock-pytest-20261117.log`.
- 추가 실행 ID: `clock-consumer-20261117-01` — 현재 엔진 formatter 결과를 분석 도구의 시각 검사, 최소 roster CSV, 최소 DB 감사 입력에 전달한 메모리/임시 CSV 재현. 설정 파일 없음, 실제 DB 연결 없음.
- 추가 실행 ID: `clock-contract-20261117-02` — HEAD와 작업 트리의 계약 문자열 길이 비교 및 v16 설정 읽기. 설정: `run/case1_v16_감염병O_2026-10-30_1601.json`, `run/case2_v16_감염병O_2026-10-30_1601.json` (`config.background`).
- `git diff --check` 통과. 운영 코드·테스트·기존 문서·DB는 수정하지 않았으며 커밋하지 않았다.

## 지적 사항

심각도: **P1** 결과·연구 해석을 바꾸는 오류, **P2** 근거·설계·검증 보완, **P3** 표현·가독성.

### [P2] 새 시각 라벨을 위치 분석 도구가 거부하여 CSV와 DB 분석이 중단된다

- 위치: `ABM/simulation/_constants.py:28`, `tools/plot_household_locations.py:245`, `tools/plot_household_locations.py:346` (호출부 `:365`, `:498`, `:503`). 도구는 미추적 로컬 파일이지만 사용자가 지정한 검색 범위에 포함되고 `AGENTS.local.md`에서도 현 컴퓨터 작업물로 명시한다.
- 재현: 저장소 루트에서 다음 명령 하나로 확인한다.

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python - <<'PY'
from ABM.simulation._constants import format_sim_time
from tools.plot_household_locations import clock_label_matches
print(clock_label_matches('월요일 오전 9시 00분', 0, 540, 0))
print(clock_label_matches(format_sim_time(540, 0), 0, 540, 0))
PY
```

- 실제: 구형 문자열은 `True`, 신규 문자열은 `ValueError: Unsupported saved time label: '1일차 월요일 오전 9시 00분'`. 실행 `clock-consumer-20261117-01`에서는 신규 라벨의 최소 roster CSV를 `load_csv_source`에 전달해 `Unsupported initial CSV time label; expected the exported Korean weekday/time`도 확인했다. 동일 라벨을 가진 로그의 `audit_locations`도 실패한다. CSV는 초기 시각 파서에서, DB 입력은 로그 또는 time_jump 시각 감사에서 중단된다.
- 기대: 기존 일차 없는 데이터와 신규 일차 포함 데이터 모두 분석 가능하고, 신규 데이터의 일차는 숫자 경과분과 대조해야 한다.
- 원인: 두 정규식이 모두 요일로 시작하는 문자열만 `fullmatch`한다. 전체 테스트의 해당 도구 테스트는 구형 라벨을 직접 만들어 쓰므로 새 formatter와의 호환성을 검증하지 않는다 (`tests/test_household_location_plot.py`의 `test_korean_clock_week_rollover_and_noon`, `RosterCsvTimelineTests.clock`).
- 제안: 두 파서 모두 일차 접두사를 선택적으로 수용하고, 접두사가 있으면 숫자 경과분으로 계산한 일차까지 검사한다. 아래는 최소 수정 제안이며 적용하지 않았다. 후속 검증에는 구형·신형·혼합 CSV, 잘못된 일차, 자정 및 7일 초과, DB log/time_jump 경로를 포함한다.

```diff
--- a/tools/plot_household_locations.py
+++ b/tools/plot_household_locations.py
@@
 def clock_label_matches(text: str, elapsed: float, start: int, weekday: int) -> bool:
-    m = re.fullmatch(r"([월화수목금토일])요일\s+(오전|오후)\s+(\d+)시\s+(\d+)분", text or "")
+    m = re.fullmatch(r"(?:(?:[1-9]\d*)일차\s+)?([월화수목금토일])요일\s+(오전|오후)\s+(\d+)시\s+(\d+)분", text or "")
@@
     day, within = divmod(absolute, 1440)
+    prefix = re.match(r"([1-9]\d*)일차\s+", text)
+    if prefix and int(prefix[1]) != day + 1:
+        return False
@@
-    m = re.fullmatch(r"([월화수목금토일])요일\s+(오전|오후)\s+(\d+)시\s+(\d+)분", first["wave_start_time"])
+    m = re.fullmatch(r"(?:(?:[1-9]\d*)일차\s+)?([월화수목금토일])요일\s+(오전|오후)\s+(\d+)시\s+(\d+)분", first["wave_start_time"])
```

## 확인했지만 문제없는 것

### 검증 요청 1~6에 대한 결론

| 요청 | 확인 결과 |
| --- | --- |
| 1. 전체 테스트 | coffee Python에서 983개 통과. 다만 P2의 신규 분석 입력은 기존 테스트 범위 밖이다. |
| 2. 문자열 소비자 | 위 도구에서 파서 2곳 발견. 다른 지정 경로에서는 해당 시각 문자열을 역파싱하거나 요일 시작으로 가정하는 소비자를 발견하지 못했다. 아래 소비자 목록 참조. |
| 3. 일차 중복 | `_time_anchor_label`의 수동 접두사 제거로 기존 마커 출력 유지. 압축 원문 머리는 별도 `format_sim_day_period`로 만들며 `_format_time_str`를 재포장하지 않는다. 디렉터도 받은 문자열을 그대로 넣는다. 중복 일차 생성 경로를 찾지 못했다. |
| 4. 구버전·신규 혼합 | UI/내보내기 자체는 문자열을 그대로 표시하며 wave 숫자로 묶어 형식 차이로 그룹이 분리되지 않는다. 일차 없는 저장값과 일차 있는 fallback은 한 실행 안에서도 혼재할 수 있다. 이것은 표기 불균일이며, UI 계산 오류는 찾지 못했다. 분석 도구는 P2처럼 신형 값에서 실패한다. |
| 5. 시간 계약 | 시작일=1일차라는 엔진 정의와 일치한다. v16 두 설정의 배경은 월요일 새벽/격리 시작일로 서술되어 직접 충돌하지 않는다. '오늘'이 없는 배경에서는 해당 지시가 적용할 대상이 없을 뿐이다. '격리 5일차에 시작' 같은 사용자 배경에서는 이야기 경과 일차와 격리 일차를 구별해야 하며 이 변경이 격리 시작일을 계산하는 것은 아니다. 임의 시나리오 및 실제 LLM 혼선 개선은 정적 검토·단위 테스트로 보장하지 않는다. |
| 6. 토큰 영향 | 실행 `clock-contract-20261117-02`: `_TIME_CONTRACT` 255→357 Unicode 문자, **+102자**. 시계 접두사는 한 자리 일차에서 **+4자** (`1일차 `), 두 자리부터 더 늘어난다. 디렉터·상태/시간 추론 등 추가 시각 입력에도 영향이 있다. 토큰 수는 사용하는 모델 tokenizer에 따라 달라 실제 토큰 증가량은 측정하지 않았다. 계약은 시스템 입력에 있으므로 추가 텍스트는 각 요청 입력에 포함된다(과금·캐시 효과는 별도). |

### 소비자 추적 근거와 한계

- 검색: `rg -n 'time_str|until_time_str|end_time_str|_format_time_str|simTimeLabel|sim_time_label|format_sim_time' backend frontend/js ABM tools run/analysis*` 및 같은 경로의 `요일|오전|오후`, `wave_start_time|wave_end_time` 검색. 필드명을 사용하지 않고 시각 열을 파싱하는 `load_csv_source`도 이 추가 검색으로 확인했다.
- 백엔드: `backend/api/simulation/runtime/queries.py:82`는 formatter 호출, `runtime/load.py:169`는 저장값 전달, `interview.py:234`는 문자열 삽입이다. DB `ABM/db/runs.py`는 문자열 저장/조회이며 형식 기반 정렬·비교를 하지 않는다.
- 프론트: `run/feed.js:121,605,642`는 저장값/fallback 선택과 표시, `run/sse.js:62`는 표시 갱신, `run/cards.js:272`, `context.js:90`, `runs/replay.js:119`는 텍스트/툴팁 표시다. `export/markdown.js:457`은 wave별 문자열 맵과 fallback, `export/csv.js:72,204,251`은 값 복사다. 시간 라벨을 키로 그룹화하거나 문자열에서 시간을 계산하지 않는다. 브라우저 수동 조작 검증은 수행하지 않았다.
- ABM 내보내기: `export/markdown.py:522,534`는 wave별 저장값 우선 및 fixed fallback, `export/csv.py:131,262,396`은 저장 문자열 복사다. roster 경과분 및 감염/이동 계산은 숫자값을 사용한다. `ABM/tools/rebeta.py`는 숫자 경과분을 사용하고 시각 열은 출력에 전달한다.
- LLM 소비자: `simulation/step.py:263` 매 턴 시계, `system.py:92`와 `system_agent.py:169` 디렉터 시계, `location.py:542` 상태 문구, `runner.py:1902,2035`와 `time_classifier.py:228` 시간 추론 입력은 문자열 삽입이며 Python 파싱은 없다. 단, 시간 추론도 LLM 입력이 바뀌므로 동일한 응답·시뮬레이션 궤적을 보장할 수는 없다. 엔진의 수치 계산 코드는 이번 diff에서 바뀌지 않았다.
- `run/analysis/analyze_runs.py`, `analysis_v14_20261027/analyze.py`는 time_str/CSV 시각 열을 조회·전달하고 시간 계산은 숫자 경과분으로 한다. 나머지 `run/analysis*/` Python 검색에서도 해당 라벨을 파싱하는 코드를 찾지 못했다. 기존 분석 스크립트는 실행하지 않았다(DB 연결 정책 및 고정 출력 경로를 피하기 위해 정적 검토).
- 골든 diff는 두 파일 각각 5개 wave 머리에 `1일차 `를 추가한 것뿐이다(총 10곳). 새 자정 테스트와 기존 시간 점프·디렉터·마커·압축 테스트가 전체 스위트에서 통과했다.
- 실제 시뮬레이션과 실제 DB 조회는 수행하지 않았다. 사용자 입력에 제시된 과거 실행 `ba39a953`의 LLM 혼선은 독립 재현하지 않았으며, 개선 효과의 실증으로 주장하지 않는다.

---

## Claude 답변 (Claude가 작성)

| 지적 | 처리 | 근거 |
| --- | --- | --- |
| P2 분석 도구의 신형 시각 라벨 거부 | 수용·수정 | 보고서의 재현 명령으로 `ValueError: Unsupported saved time label: '1일차 월요일 오전 9시 00분'`을 직접 확인했다. 수정: `tools/plot_household_locations.py`에 정규식 상수 `CLOCK_LABEL`(일차 접두사 선택)을 두고 두 파서가 함께 쓴다. `clock_label_matches`는 일차가 있으면 경과분으로 계산한 일차(`day + 1`)와 대조하고, CSV 첫 wave 라벨은 일차가 없거나 1일차일 때만 받는다. 테스트(`tests/test_household_location_plot.py`): `test_engine_clock_with_day_index_is_accepted_and_checked`(엔진 `format_sim_time` 출력 수용, 틀린 일차·다른 날 거부), `test_csv_with_day_indexed_labels_loads_the_same_timeline`(구형·신형 CSV가 같은 시작·요일·종료로 읽히고, 첫 wave 2일차는 거부). 재현 명령 결과 `True True`, 도구 테스트 26 passed. 두 파일은 이 컴퓨터의 미추적 작업물이라(`AGENTS.local.md`) 커밋 대상이 아니다. 제안의 "혼합 CSV"(한 파일 안에 구형·신형 라벨)는 엔진이 한 실행 안에서 형식을 바꾸지 않으므로 생기지 않는다 — 라벨은 wave마다 독립으로 검사하므로 섞여도 각각 통과한다. |

## 2차 검증 결과

- 작성 에이전트: Codex
- 작성일: 2026-11-17
- 기준 커밋: `be09adf5dceef177311cec2dfde85416cf59bde5`
- 대상: 브랜치 `day-in-clock`의 미커밋 작업 트리, 미추적 `tools/plot_household_locations.py`, `tests/test_household_location_plot.py`. 1차 P2와 위 Claude 답변을 중심으로 재검증했다.
- **최신 판정: 통과.** 1차 P2(신형 시각 라벨 거부)는 해소됐다. 새 P1·P2 지적은 없다. 아래 혼합 CSV 설명에 대한 반박은 실제 파서 결함이 남았다는 의미가 아니다.

### 실행 및 재현 결과

- 실행 ID `day-in-clock-pytest-20261117-02` (테스트 실행, 실제 시뮬레이션 아님, 설정 파일 없음).
  - 명령: `PYTHONDONTWRITEBYTECODE=1 /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider`
  - **985 passed in 16.02s**, 종료 코드 0.
  - 로그: `/tmp/day-in-clock-pytest-20261117-02.log`.
- 실행 ID `day-in-clock-consumer-20261117-02` (coffee Python 표준입력 검증 스크립트, 합성 입력·임시 CSV, 설정 파일 없음).
  - 로그: `/tmp/day-in-clock-consumer-20261117-02.log`.
  - 1차 보고서의 재현 명령 본문을 그대로 재실행: 구형 라벨 및 `format_sim_time(540, 0)` 모두 **True**. 이전 `ValueError`는 재현되지 않았다.
  - 시작 분 `0/350/720/1430`, 시작 요일 7종, 경과분 `0/10/30/1440/10080/11560/21600`의 **196개 조합**에서 엔진 신형 라벨 및 접두사를 제거한 구형 라벨을 모두 수용했다. 각 조합에서 일차만 1 증가시킨 라벨은 모두 False였다. 자정, 정오, 주간 순환 및 7일 초과를 포함한다.
  - 기존 테스트의 roster CSV 생성기를 이용해 구형·신형의 시작 분/요일/종료가 같음을 직접 확인했다. 같은 wave 안에서도 행별로 구형·신형을 섞은 CSV를 정상 수용했다.
  - CSV 첫 wave 시작 라벨의 잘못된 일차는 `Unsupported initial CSV time label`로 거부했다. 후속 wave 시작 및 종료의 잘못된 일차도 각각 `Inconsistent wave_start_time`, `Inconsistent wave_end_time`으로 거부했다. 종료를 8일 초과로 확장하고 엔진 라벨을 넣은 CSV도 수용했다.
  - DB 로더 이후와 같은 구조의 합성 `logs/events`를 `audit_locations`에 직접 전달했다. 신형 로그 `time_str`, 자정을 넘는 `time_jump.data.end_time_str`를 수용했고, 로그와 이벤트의 일차를 각각 독립적으로 틀리게 만들면 `inconsistent clock label`, `inconsistent wave end label`로 거부했다. 구형 로그와 신형 time_jump를 섞어도 수용했다.
  - `CLOCK_LABEL`을 첫 CSV 파서와 공통 검사 함수가 함께 쓰며, 접두사가 있으면 절대 분 기준 `day + 1`을 대조한다. 첫 CSV 라벨만 검사하고 나머지를 놓치는 구조가 아님을 확인했다.
- `git diff --check` 통과. 운영 코드·테스트·다른 문서·DB를 수정하지 않았고 커밋하지 않았다. 이 보고서에만 덧붙였다.

### Claude 답변의 혼합 CSV 반박 판단

“엔진이 한 실행 안에서 형식을 바꾸지 않으므로 혼합 CSV는 생기지 않는다”는 설명은 **일반적으로 타당하지 않다**. 새 실행 하나의 출력 형식이 일정하더라도, CLI 재개 내보내기는 여러 실행의 저장값을 결합한다.

- `ABM/cli.py:255`의 `_resume_history`는 `resumed_from` 사슬의 이전 로그·이벤트를 읽는다. `ABM/cli.py:673`부터 이전 기록을 현재 `shared_log/events` 앞에 붙이고 번들 및 CSV 내보내기로 넘긴다. 구형 엔진에서 멈춘 실행을 신형 엔진으로 재개하면 한 CSV에 구형·신형 라벨이 함께 들어갈 수 있다.
- `ABM/export/csv.py:254`부터 저장된 `time_str`를 그대로 사용하며 정규화하지 않는다. 합성 구형 wave 0·신형 wave 1 로그를 `render_location_csv`에 전달해 실제 출력의 wave 0 시작은 `월요일 오전 9시 00분`, 종료는 `1일차 월요일 오전 9시 10분`으로 혼합됨을 확인했다. 이는 저장값 보존/결합 경로의 최소 재현이며 실제 재개 실행을 돌린 것은 아니다.
- 반면 “섞여도 각각 통과한다”는 답변은 **확인됐다**. 위 혼합 roster CSV 및 구형 로그/신형 time_jump 감사가 정상 통과했다. 따라서 혼합 데이터의 발생 가능성에 관한 설명은 바로잡아야 하지만, 이 설명을 근거로 남은 P2를 제기하거나 추가 구현 반복을 요구할 사유는 없다.

### 검증 범위와 한계

실제 시뮬레이션·LLM 호출 및 실제 DB 연결은 수행하지 않았다. DB log/time_jump 감사는 운영 DB를 열지 않고 `audit_locations`에 로더 이후 형태의 합성 입력을 전달해 검증했다. 전체 스위트는 기존 테스트의 임시/인메모리 DB를 사용하지만 운영 DB에는 연결하지 않았다. 실제 저장 DB의 특정 실행 데이터가 완전한지 또는 LLM 날짜 혼선이 줄었는지를 입증하는 결과는 아니다.

### Claude 확인 (2차 결과 반영)

- 혼합 CSV 반박은 철회한다. `ABM/cli.py:673`의 재개 내보내기가 `resumed_from` 사슬의 이전 로그를 앞에 붙이므로, 구형 엔진에서 멈춘 실행을 신형 엔진으로 재개하면 한 CSV에 두 형식이 섞인다. 수정된 파서가 이를 정상 처리하는 것은 2차 실행 `day-in-clock-consumer-20261117-02`로 확인됐으므로 추가 수정은 하지 않는다.
- 판정 통과로 루프를 닫는다.
