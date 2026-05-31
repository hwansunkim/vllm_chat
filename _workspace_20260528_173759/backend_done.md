# Backend Done: simulation_runs 기능 구현

## 변경 파일 목록

- `/home/wskim/work/vllm_chat/ABM/db.py`
- `/home/wskim/work/vllm_chat/ABM/simulation.py`
- `/home/wskim/work/vllm_chat/backend/api/simulation.py`

## 주요 변경 내용

### ABM/db.py
- `_SCHEMA`에 `simulation_runs` 테이블 및 `idx_runs_scenario` 인덱스 추가
- `SimDB` 클래스에 메서드 4개 추가:
  - `create_run(run_id, scenario_id, scenario_name, config_json)` — 실행 시작 시 레코드 생성, run_number 자동 계산
  - `finish_run(run_id, status, total_waves, total_turns)` — 실행 종료 시 레코드 업데이트
  - `get_runs(scenario_id=None)` — 전체 또는 시나리오별 실행 이력 조회
  - `delete_run(run_id)` — 관련 메모리 테이블 데이터 포함 완전 삭제

### ABM/simulation.py
- `Simulation.__init__`에 `self.completed_waves: int = 0` 필드 추가
- `run()` 루프 안에서 `total_turns += len(current_wave)` 다음에 `self.completed_waves = wave_num + 1` 추가

### backend/api/simulation.py
- `_get_sim_db()` 헬퍼 함수 추가 (simulation.db 접근용)
- `SimStartConfig`에 `scenario_id: str | None = None` 필드 추가
- `_run()` 함수에 `db.create_run(...)` 호출 추가 (시뮬레이션 시작 시)
- `_run()` 함수에 `db.finish_run(...)` 호출 추가 (정상 종료 및 에러 종료 양쪽)
- 새 엔드포인트 2개 추가:
  - `GET /api/simulation/runs?scenario_id=...` — 실행 이력 목록
  - `DELETE /api/simulation/runs/{run_id}` — 실행 이력 삭제 (204)

## 검증
```
python -c "from ABM.db import SimDB; from backend.api.simulation import router; print('OK')"
# 출력: OK
```
