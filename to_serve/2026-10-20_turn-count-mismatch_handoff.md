# 인수인계(분석 요청): 실행 기록의 턴 수 불일치

- 작성: Claude Code
- 작성일: 2026-10-20
- 브랜치: `fix/turn-count-mismatch` (아직 코드 변경 없음)
- 기준 커밋: `f35b867` (master)
- 관련 결정: 없음
- 이번 루프의 순서: **Codex가 원인을 먼저 분석**하고, Claude가 그 보고서로 수정한다. 운영 코드는 수정하지 않는다(`AGENTS.md` 2절).

## 증상
실행 `5be80eb1-9ee5-473f-b230-3d72e694252f`(case1_v12_감염병O, 2026-10-13)에서:

| 출처 | 값 |
| --- | --- |
| `logs_graph/simulation.db` `simulation_runs.total_turns` | 1980 |
| `simulation_log`에서 해당 `run_id` 행 수 | 1979 |
| 내보낸 마크다운 머리(`총 턴 1979`) | 1979 |

결과 표에 턴 수를 쓰려면 어느 값이 맞는지, 왜 다른지 알아야 한다.

## 분석 요청
1. `total_turns`를 계산·저장하는 경로와 `simulation_log`에 행을 쓰는 경로를 찾아, 어떤 턴이 한쪽에만 세어지는지 특정하라(예: 실패한 턴, 상태 진입만 한 턴, 이벤트 알림 턴, 재개 경계, 마지막 wave).
2. 위 실행의 DB(읽기 전용)에서 실제로 그 1건을 찾을 수 있으면 찾아라(wave·화자·이벤트).
3. `logs_graph/simulation.db`의 다른 실행들에서도 `total_turns`와 로그 행 수가 다른지 표로 확인하라(차이의 크기와 경향).
4. 어느 값을 "턴 수"의 정의로 삼아야 하는지 의견과, 수정 위치·방향을 제안하라(코드는 diff 제안까지만).
5. 필요하면 재현하는 최소 테스트 아이디어를 제시하라.

## 참고
- 실행 기록 DB는 읽기 전용으로 열 것: `sqlite3.connect('file:logs_graph/simulation.db?mode=ro', uri=True)`.
- 테스트: `C:\Users\ADMIN\miniconda3\envs\vllm\python.exe -m pytest tests/ -q -p no:cacheprovider`
- 관련 위치 후보(확인하지 않은 추정): `backend/api/simulation/runner.py`의 `finalize_run`, `ABM/cli.py`의 `_finish_db_run`, `ABM/db/runs.py`, 엔진의 `total_turns`/`turn_complete` 처리.

## 범위 밖
- 턴 수 외의 실행 메타데이터(`total_waves`, `elapsed_minutes`)는 이번 범위가 아니다. 다만 같은 원인으로 어긋나 있으면 보고서에 한 줄로 적는다.
