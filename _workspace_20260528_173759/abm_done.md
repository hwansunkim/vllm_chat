# ABM 시뮬레이션 대화 기록 저장 및 재현 구현 완료

## 변경 파일

- `/home/wskim/work/vllm_chat/ABM/db.py`
- `/home/wskim/work/vllm_chat/ABM/simulation.py`
- `/home/wskim/work/vllm_chat/backend/api/simulation.py`
- `/home/wskim/work/vllm_chat/frontend/js/simulation.js`
- `/home/wskim/work/vllm_chat/frontend/css/simulation.css`

## 변경 내용 요약

### ABM/db.py
- `_SCHEMA`에 `simulation_log` 테이블 및 `idx_simlog_run` 인덱스 추가
- `SimDB.log_turn()` 메서드 추가 — 실행 중 매 턴 대화를 DB에 기록
- `SimDB.get_run_log()` 메서드 추가 — run_id로 전체 대화 로그 조회
- `SimDB.get_run()` 메서드 추가 — run_id로 단일 실행 메타 조회
- `SimDB.delete_run()` tables 목록에 `"simulation_log"` 추가 (캐스케이드 삭제)

### ABM/simulation.py
- `_step_agent()` 시그니처에 `wave: int` 파라미터 추가
- `turn_start` / `turn_complete` emit에 `"wave": wave` 필드 추가
- `turn_complete` emit 직후 `self._db.log_turn(...)` 호출로 DB 저장
- `run()` 내 `executor.submit(...)` 호출에 `wave_num` 전달

### backend/api/simulation.py
- `GET /api/simulation/runs/{run_id}` 엔드포인트 추가 — 단일 run 메타 조회
- `GET /api/simulation/runs/{run_id}/log` 엔드포인트 추가 — 전체 대화 로그 조회

### frontend/js/simulation.js
- `refreshRunHistory()` 테이블에 "보기" 버튼 열(`rh-view`) 및 헤더 추가
- `sim-run-view-btn` 클릭 리스너 → `openRunReplay()` 호출
- `openRunReplay(runId, runNum)` 함수 추가 — 모달로 과거 대화 전체 재현, "이 설정으로 시작" 재실행 버튼 포함

### frontend/css/simulation.css
- `.sim-replay-modal-overlay`, `.sim-replay-modal-box`, `.sim-replay-header` 등 모달 스타일 추가
- `.sim-feed-wave-badge`, `.sim-feed-empty-msg`, `.sim-run-view-btn` 스타일 추가
