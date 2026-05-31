# Backend 작업 완료 보고

작업 일자: 2026-05-28
담당: backend-dev

## 변경 요약

`backend/api/simulation.py` (단일 567줄 파일) → `backend/api/simulation/` 패키지로 분할.
P0/P1/P2 버그 4건 수정 포함.

## 변경된 파일 목록

### 삭제
- `/home/wskim/work/vllm_chat/backend/api/simulation.py` (단일 파일 → 패키지로 대체)

### 신규 (패키지)
- `/home/wskim/work/vllm_chat/backend/api/simulation/__init__.py`
  - `router = APIRouter(prefix="/api/simulation")` 조합
  - 서브 라우터 4개 include
- `/home/wskim/work/vllm_chat/backend/api/simulation/schemas.py`
  - `AgentConfig`, `ScenarioEvent`, `ExtraField`, `SimStartConfig`,
    `ScenarioSave`, `SimContinueConfig`
- `/home/wskim/work/vllm_chat/backend/api/simulation/state.py`
  - `_sim` dict, **`_sim_lock = threading.Lock()`** (신규), `get_sim_db()` 헬퍼
- `/home/wskim/work/vllm_chat/backend/api/simulation/runner.py`
  - `swap_event_queue(new_q, new_stop_ev, **extra)` — 큐 교체 + 이전 큐에 sentinel 주입
  - `finalize_run(db, run_sim_id, stop_event, sim_obj, eq, error=None)` —
    세 `_run()` 종료부의 중복 try/except/finally 통합
- `/home/wskim/work/vllm_chat/backend/api/simulation/runtime.py`
  - `/start`, `/stop`, `/continue`, `/resume/{run_id}`, `/status`,
    `/agents/{name}/context`, `/agents/{name}/memory`, `/logs`, `/edges`
- `/home/wskim/work/vllm_chat/backend/api/simulation/sse.py`
  - `_blocking_get`, `GET /stream`
- `/home/wskim/work/vllm_chat/backend/api/simulation/runs.py`
  - `GET /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/log`, `DELETE /runs/{run_id}`
- `/home/wskim/work/vllm_chat/backend/api/simulation/scenarios.py`
  - `GET/POST /scenarios`, `PUT/DELETE /scenarios/{sid}`, `GET /default-output-format`

### 변경 없음
- `/home/wskim/work/vllm_chat/backend/main.py` — `from .api import ... simulation`
  후 `simulation.router`를 참조. 패키지의 `__init__.py`가 같은 이름으로
  `router`를 export 하므로 무수정 호환.

## 버그 수정 상세

### B1 (P0) — /start race condition
- `state.py`에 `_sim_lock = threading.Lock()` 도입.
- `runtime.start_simulation`이 `with _sim_lock:` 블록 안에서
  status 체크와 `"running"` 세팅을 원자 처리.
- 동일 패턴을 `/continue`, `/resume/{run_id}`, `/stop`에도 적용.

### B4 (P0) — _run 내 NameError 가능성
- `runtime.start_simulation._run`이 try 블록 진입 전에
  `db = None; run_sim_id = None; sim = None` 초기화.
- 모든 `_run`이 `finalize_run(db, run_sim_id, stop_event, sim, eq, error=e)`로
  통일된 종료 경로 사용 → 어떤 단계에서 실패해도 NameError 불가.

### B9 (P2) — resume 시 원본 events 재실행 방지
- `runtime.resume_simulation._run`이 `sim.run(..., events=[], ...)`로
  빈 이벤트 리스트 전달.
- 주석으로 "resume은 saved_pending wave부터 재개하므로 과거 wave에서
  이미 발화한 system_message / agent_enter / agent_exit가 재실행되면 안 됨"
  명시.

### B2 (P1) — SSE 큐 크로스런 오염
- `runner.swap_event_queue()`가 새 큐를 설치하기 전에 이전 큐에 `None` 센티넬을
  `put_nowait` → 이전 `/stream` 소비자가 `simulation_end` 이벤트와 함께 정상 종료.
- `/start`, `/continue`, `/resume`가 모두 이 헬퍼 경유.

## 검증

```bash
$ python -c "from backend.api.simulation import router; print('import OK')"
import OK

$ python -c "from backend.main import app; ..."
# 16개 /api/simulation/* 경로가 원본과 1:1 일치 (DELETE/GET 중복 path 포함하면 19개)
```

원본 라우트 목록 (path 기준, 메서드 중복 path 합쳐 16개):
```
/api/simulation/start
/api/simulation/stop
/api/simulation/continue
/api/simulation/resume/{run_id}
/api/simulation/status
/api/simulation/agents/{name}/context
/api/simulation/agents/{name}/memory
/api/simulation/logs
/api/simulation/edges
/api/simulation/stream
/api/simulation/runs
/api/simulation/runs/{run_id}
/api/simulation/runs/{run_id}/log
/api/simulation/default-output-format
/api/simulation/scenarios
/api/simulation/scenarios/{sid}
```
→ 모두 패키지 버전에 그대로 존재. **외부 API 인터페이스 변화 없음.**

## 후속 권장 사항 (qa-reviewer / 사용자 확인용)

1. `backend/api/__pycache__/simulation.cpython-312.pyc` 잔존 캐시 파일이
   남아 있을 수 있음 (런타임에 자동 무효화되지만, 클린 빌드를 원하면 수동 삭제).
2. `_sim` dict 자체에 대한 동시 읽기/쓰기 보호는 status 전환 이외 영역에서는
   적용 안 됨. 단일 시뮬 동시 1개 제약 하에서는 무방하나, 멀티런 도입 시
   `_sim` 전체를 잠금 또는 per-run 객체로 캡슐화 필요.
3. `/resume`에서 `_sim["status"] = "running"`으로 선반영한 뒤
   `Run not found` / 잘못된 설정 케이스에서 `"idle"`로 롤백하는 로직 추가됨.
   `/start`, `/continue`는 잠금 안에서 검증 후 status 설정하므로 롤백 불필요.
