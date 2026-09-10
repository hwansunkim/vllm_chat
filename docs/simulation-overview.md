# 시뮬레이션 개요

> ABM 시뮬레이션이 무엇이고, 웹 API와 엔진이 어떻게 나뉘며, 실행이 어떤 생명주기를
> 거치는지. 엔진 내부는 [`simulation-engine.md`](simulation-engine.md), 기능별 상세는
> [`simulation-features.md`](simulation-features.md). 용어는
> [`glossary.md` Part C·D](glossary.md#part-c--abm-시뮬레이션-엔진-abm).

---

## 1. 무엇인가

여러 LLM 에이전트가 **서로 대화하도록** 두고, 그 흐름을 관전·기록·재개하는 시스템.
가족 드라마, 편의점 상황극, 감염병 확산 같은 시나리오를 만들어 돌린다.

- **GUI**: 설정 뷰에서 시나리오 편집 → 실행 뷰에서 시작·관전.
- **CLI**: `python -m ABM.cli run scenario.json` — GUI 없이 배치·스윕·회귀 평가.
- 두 경로 모두 **같은 엔진**(`ABM/simulation/`)과 **같은 마크다운 포매터**를 쓴다.

---

## 2. 핵심 모델 — Wave 기반 BFS

```
Wave 0 : start_agent 발화 → target = [b, c]
Wave 1 : b, c 동시 발화 (ThreadPoolExecutor 병렬) → 각자의 target
Wave 2 : Wave 1 target들이 발화
  ⋮
종료:  max_waves 도달  |  목표 기간 도달  |  활성 에이전트 없음  |  진행 불가(응답 없음)  |  사용자 중지
```

| 용어 | 정의 |
|---|---|
| **Wave** | BFS 한 레벨 = **동시에 발화하는 에이전트 집합** |
| **Turn** | 한 에이전트의 한 발화. wave 안에 turn N개 |
| **target** | 에이전트 응답 JSON의 `target` 필드 — 다음 wave의 발화자를 결정. `"all"` / `"self"` / `["id1","id2"]` |
| **run** | 한 번의 실행. `/start`·`/continue`·`/resume`마다 새 `run_id` |
| **run_wave / disp_wave** | per-run 0부터 카운터(시간·감염 계산) / 누적 표시 wave(피드·DB·이벤트 라벨). `/continue`·`/resume` 후 wave 번호가 이어지도록 분리 → [`simulation-engine.md`](simulation-engine.md#7-disp_wave-vs-run_wave) |
| **shared_log** | 관전자 전지적 발화 로그 |
| **edges** | 관계 그래프 간선 (누가 누구에게) |
| **에이전트 memory** | 각 에이전트의 개인 컨텍스트 (그가 보고 들은 것만) |

wave 1회 안에서 벌어지는 정확한 처리 순서(발화 라우팅 → 이동 → 감염 → 다음 wave 조립)는
[`simulation-engine.md §2`](simulation-engine.md#2-run-루프--wave-1회의-처리-순서).

---

## 3. 경계 — 웹 API vs 엔진

```
backend/api/simulation/       ← 얇은 래퍼: 스레드·SSE 큐·run row·전역 상태
        │  headless.run_config(cfg)  ← fresh start의 유일한 조립 경로
        ▼
ABM/simulation/  (순수 엔진)   ← FastAPI를 모른다. 동기 코드. CLI도 그대로 씀
        │  Simulation(agents, …).run(start_agent, …)
        ▼
ABM/db/  (SimDB)              ← logs_graph/simulation.db
```

**규칙**: 엔진에 기능을 추가하려면 `headless.run_config()`의 `Simulation(...)` 호출과
`sim.run(...)` 호출에 인자를 넘겨야 한다 — 그래야 GUI `/start`와 CLI가 함께 바뀐다.
`/load`·`/resume`은 **별도 조립 경로**(`runtime/load.py`·`resume.py`)라 인자를 빠뜨리면
"`/start`로는 되는데 재개하면 조용히 꺼지는" 버그가 생긴다. (그 파일들의 주석이
이런 종류의 실수를 하나씩 기록하고 있다.)

---

## 4. 전역 상태 — `_sim` (`backend/api/simulation/state.py`)

**단일 프로세스에서 시뮬레이션은 동시에 1개만.** 상태는 모듈 전역 dict 하나:

```python
_sim = {
  "status":        "idle",   # idle | loading | running | stopping | done | stopped | error
  "event_queue":   Queue,    # SSE 큐 (실행마다 교체)
  "stop_event":    Event,    # 외부 중지 신호
  "thread":        Thread,   # 실행 스레드
  "run_sim_id":    "…",      # 현재 전역 상태의 소유자 run id — finalize_run 가 이 값과
                             #   자기 run id 를 대조해 옛 실행의 전역 덮어쓰기를 막는다
  "sim_obj":       Simulation,   # 살아 있는 엔진 인스턴스 (/continue가 이어씀)
  "shared_log": [], "edges": [], "agents": {}, "background_log": [],
  "scenario_id": …, "scenario_name": …, "config_json": …,
}
_sim_lock = threading.Lock()   # status 원자적 전이 (동시 /start 방지)
_BUSY_STATES = ("running", "stopping", "loading")   # 새 실행·재개·불러오기 거부 상태
```

`runner.py`의 헬퍼:

| 함수 | 역할 |
|---|---|
| `swap_event_queue(new_q, new_stop)` | 새 SSE 큐 설치 전에 옛 큐에 `None` sentinel을 넣어, 옛 큐에 블록돼 있던 SSE 소비자를 깔끔히 종료 |
| `fold_elapsed_and_reset_waves(sim)` | `/continue` 준비 — 총 경과 분을 `_elapsed_minutes`로 접고, `_wave_base += completed_waves`, `completed_waves = 0`, 감염 앵커 재기준화 |
| `finalize_run(db, run_id, stop_ev, sim, eq, error=)` | 실행 스레드 종료 공통 처리 — 성공 시 `db.finish_run` + 에이전트 스냅샷 저장, 실패 시 `error` 이벤트 emit, 항상 `None` sentinel push. **전역 `_sim` 쓰기(status·shared_log·edges)는 `_sim["run_sim_id"] == run_id` 일 때만** — `/stop` 직후 새 실행이 슬롯을 차지한 뒤 도착한 옛 finalizer가 새 실행을 덮어쓰는 것을 막는다. DB 영속화는 run id 로 키가 나뉘므로 소유권과 무관하게 수행 |

---

## 5. 실행 라이프사이클

### `POST /start` (`runtime/lifecycle.py`)

```
run_sim_id = uuid4()
_sim_lock: status "running"으로 원자적 flip + _sim["run_sim_id"] = run_sim_id
           (status ∈ {running, stopping, loading} 이면 409 — _BUSY_STATES)
SSE 큐·stop_event 생성, swap_event_queue
데몬 스레드 시작:
    llm = _make_llm(cfg.server_id, cfg.temperature)     # 브릿지 동기 콜러블
    agent_llm = _make_agent_llm_map(cfg)                # 에이전트별 서버 오버라이드
    db.create_run(run_sim_id, …, config_json)
    headless.run_config(cfg, llm=, agent_llm=, db=, sim_id=, event_queue=, stop_event=,
                        on_sim_ready=<_sim 전역 채우기>)
    finalize_run(...)
→ { "status": "started" }
```

### `POST /stop`

`stop_event.set()` + `status "running" → "stopping"`. 그다음 워커 스레드를
`join(timeout=15)` 로 잠깐 기다린다 — 대개 현재 wave(LLM 한 라운드) 안에
빠져나오며 `finalize_run` 이 `status = "stopped"` 로 확정하므로, 응답은
보통 최종 상태(`{"status": "stopped"}`)를 담는다.

`stopping` 동안 `/start`·`/continue`·`/resume`·`/load` 는 409 로 거부된다
(`_BUSY_STATES`). 이렇게 해야 옛 워커가 wave 를 마저 돌다 `finalize_run` 에
도착해 새 실행의 전역 상태·로그를 덮어쓰는 사고가 안 난다. 긴 LLM 호출로
타임아웃을 넘기면 `status` 는 `stopping` 으로 남고 프론트가 `/status` 로
확정을 폴링한다. 이미 끝난(done/stopped/error) 실행에 stop 이 오면 상태는
그대로 두고 `stop_event` 만 세팅한다.

### `POST /continue` (`SimContinueConfig`)

- `status ∈ {done, stopped}` 이고 `sim_obj`가 살아 있어야 함 (아니면 409).
- 메모리에 있는 `sim_obj`를 **그대로 이어 쓴다** (재조립 없음). 새 SSE 큐·새 `run_id`.
- `fold_elapsed_and_reset_waves(sim_obj)` — 시계 연속성.
- **원래 실행의 `config_json`에서** `max_silence_waves` 복원
  (`SimContinueConfig`에는 없는 필드).
- **wave 트리거 이벤트는 재생하지 않는다** — wave 0부터 다시 세므로 과거 wave의
  이벤트가 매번 다시 발동한다. **`at_time` 이벤트만** 재전달한다(이미 지난 시각은
  `run()`이 "발동함"으로 처리 → 안전, 이어가기 중에도 예정 서사 유지).
  `resume_wave = sim_obj._pending_wave`.
- **감염 설정 등은 못 바꾼다** — `/start` 시점 config 스냅샷이 계속 유효.

### `POST /load/{run_id}` (`runtime/load.py`)

과거 실행을 **메모리에 복원하되 실행하지 않는다**. `config_json` + 에이전트 스냅샷
(memory·`state_json`) + `simulation_log`로 `Simulation` 재조립 → `status = "done"`.
프론트가 "이어서" 버튼으로 계속할 수 있는 상태. `{ log, infection, start_wave }` 반환
(피드·감염 뱃지 복원용).

### `POST /resume/{run_id}` (`runtime/resume.py`)

`/load`와 같은 재조립 + **바로 스레드 실행**. `wave_base_init = prior_cum`
(직전 run들의 누적 wave), `elapsed_minutes_init`, `restore_agent_state`,
`resume_wave = saved_pending`. `max_silence_waves`는 복원된 `SimStartConfig`에서.

> `/load`·`/resume` 조립 코드가 `headless.run_config`의 `Simulation(...)` 인자를
> 손으로 복제하고 있다. 새 엔진 인자를 추가하면 **세 곳**(headless, load, resume)을
> 함께 고쳐야 한다.

---

## 6. SSE 스트림 — `GET /stream` (`sse.py`)

```python
async def _gen():
    q = _sim["event_queue"]
    while True:
        item = await loop.run_in_executor(None, _blocking_get, q)   # 30s 타임아웃 → ping
        if item is None:  yield "event: simulation_end\n…"; break   # sentinel
        yield f"event: {item['type']}\ndata: {json.dumps(item['data'])}\n\n"
```

- 엔진의 `_emit(type, data)` → `event_queue.put({type, data})` + (`_PERSIST_EVENTS`면)
  `db.log_event`.
- 소비자는 실행 뷰의 `EventSource` (`frontend/js/sim/run/sse.js`).
- 이벤트 타입 16종의 페이로드는 [`glossary.md` E-2](glossary.md#e-2-시뮬레이션-sse-getapisimulationstream)
  및 [`api-reference.md`](api-reference.md).

---

## 7. 읽기 전용 조회

| 엔드포인트 | 반환 |
|---|---|
| `GET /status` | `{ status, log_count, edge_count }` |
| `GET /agents/{name}/context` | 그 에이전트가 **실제로 받는 프롬프트** 메시지 열 + 토큰. `_assemble_agent_prompt`의 결과 (실행 경로와 동일 규칙). 컨텍스트 탭의 소스 |
| `GET /agents/{name}/memory` | 구조화 DB 메모리 (episodes / facts / relationships / self_state) |
| `GET /logs` | `shared_log` (background 제외) |
| `GET /events?types=` | 저장된 SSE 이벤트 (필터 가능) |
| `GET /runs/{run_id}/events` | 과거 실행의 SSE 이벤트 |
| `GET /edges` | 관계 그래프 간선 |

---

## 8. 영속화 — SimDB (`logs_graph/simulation.db`)

`SimDB` = 도메인별 믹스인 조합 (`ABM/db/base.py`). WAL 모드, 스레드별 커넥션.

| 테이블 | 내용 | 쓰는 곳 |
|---|---|---|
| `simulation_runs` | 실행 메타 (`status`, `start_wave`, `total_waves`, `elapsed_minutes`, `active_agents_json`, `pending_wave_json`, `config_json`) | `create_run` / `finish_run` |
| `simulation_log` | 턴별 발화 (`speaker`, `content`, `meta_json`, `targets_json`, `time_str`, `location`, `is_exterior`) | `_apply_turn_result` |
| `sim_events` | 영속화된 SSE 이벤트 (`_PERSIST_EVENTS`) | `_emit` |
| `agent_snapshots` | `memory_json` + `state_json` (위치·외모·인지관계·감염) — 재개용 | `finalize_run` |
| `episodic_memory` · `semantic_memory` · `relationship_memory` · `relationship_history` · `agent_self_state` | 구조화 메모리 (압축 산물) | `memory_compressor` |
| `compression_log` | 압축 이력 | `_compress_agent` |
| `interview_log` | 사후 인터뷰 (**타임라인과 완전 분리** — 리플레이가 절대 읽지 않음) | `interviews.py` |

`_PERSIST_EVENTS` = `agent_move`, `appearance_update`, `system_intervention`,
`meeting_update`, `scene_event`, `director_call`, `infection_update`, `time_jump`
(+ `world_event` — 레거시, 구 실행 재출력용). (= 마크다운 내보내기가 소비하는 집합.)

전체 컬럼: [`database.md`](database.md).

---

## 9. 시나리오 형식 — `SimStartConfig`

시나리오 *정의*는 채팅 DB `simulation_scenarios` 테이블에 `config_json`으로 저장된다
(실행 *결과*는 SimDB). `run`이 받는 세 가지 모양:

1. `SimStartConfig` 자체 — `{"agents": [...], "background": "...", ...}`
2. `{"name": "...", "config": { ... }}` — 시나리오 저장 형식
3. `{"name": "...", "config_json": "..."}` — `/api/simulation/scenarios` 응답 항목

`SimStartConfig` 주요 필드 (`backend/api/simulation/schemas.py`):

| 필드 | 기본 | 의미 |
|---|---|---|
| `agents[]` | — | `AgentConfig` — `name`(=key), `system_prompt`, `display_name`, `relationships`, `location`, `visual_description`, `server_id`, `temperature`, `initial_active` |
| `background` | — | 공통 장면 전제 |
| `start_agent` | — | wave 0 발화자 (= `agents[].name` 중 하나) |
| `max_waves` | 10 | wave 상한 (안전장치) |
| `target_duration_minutes` | `null` | 목표 기간 (분, ≥1). `null` = 미사용 |
| `token_limit` / `llm_max_tokens` | 8192 / 16384 | 에이전트 프롬프트 상한 / 응답 상한 |
| `extra_fields[]` | emotion·action·action_note | 응답 JSON 추가 메타 필드 |
| `events[]` | `[]` | `ScenarioEvent` — `system_message` / `agent_enter` / `agent_exit` / `update_appearance` / `infect_agent` |
| `location_graph[]` | `[]` | `LocationNode` — `name`, `connects_to`, `is_exterior`, `zone`, `is_zone_entry` |
| `perception_mode` | `targeted` | `targeted` / `spatial` (엿듣기) |
| `time_mode` / `time_per_wave` | `fixed` / 30 | 시간 모델 |
| `time_categories[]` / `time_estimation_mode` | 4종 / `category` | 가변 시간 |
| `max_scene_jump_minutes` / `max_daytime_jump_minutes` | 45 / 180 | 시간 점프 상한 |
| `max_silence_waves` | 3 | 고립 휴면 기준 (연속 혼잣말 N회 → 밀집 재투입 제외) + idle 점프 인덱스 |
| `server_id` / `temperature` | `null` / 0.7 | 시뮬레이션 기본 LLM |
| `lang_fix_enabled` / `lang_fix_retries` | `true` / 2 | 언어 교잡 수정 |
| `output_format_override` | `""` | 출력 계약 오버라이드 (opt-in). 빈 값 = 엔진 자동 생성 |
| `system_agent` | `SystemAgentConfig` | 디렉터 |
| `infection_model` | `InfectionModelConfig` | 감염 모델 |

> **구 `output_format_template`** 필드는 파싱만 하고 **런타임에서 무시**된다 — 옛
> 시나리오도 로드하면 최신 엔진 계약을 받도록. `effective_output_format_override()`가
> `output_format_override or None`을 돌려준다.

---

## 10. 헤드리스 CLI (`python -m ABM.cli`)

> 모든 명령은 **리포지토리 루트에서** (`memory.db`·`servers.json`·`logs_graph/`가 상대 경로).

### `run` — 시나리오 → 마크다운/CSV

```bash
python -m ABM.cli run scenario.json                    # → {이름}_{날짜}.md
python -m ABM.cli run scenario.json -o -                # stdout
python -m ABM.cli run scenario.json --dry-run           # 설정 검증 + 엔진 계약 미리보기
python -m ABM.cli run scenario.json -n 20 --outdir results/ --concurrency 4
python -m ABM.cli run --scenario-id <uuid> --max-waves 200 --server-id <uuid>
python -m ABM.cli run scenario.json --strict --quiet -o -   # 회귀 평가
python -m ABM.cli run scenario.json --format csv -o out.csv  # 위치 이력
```

| 옵션 | 의미 |
|---|---|
| `--scenario-id` | 파일 대신 DB에서 로드 |
| `-o` / `--outdir` / `-n` / `--concurrency` | 출력 / 배치 디렉터리 / 반복 / 병렬 |
| `--max-waves` `--target-minutes` `--server-id` `--temperature` `--step-delay` `--seed` | config 오버라이드 |
| `--include` / `--exclude` | 마크다운 섹션 토글 (`time action move appearance world intervention infection meeting`) |
| `--no-db` | `simulation.db`에 기록 안 함 (기본은 기록 → GUI 이력에 노출) |
| `--dry-run` `--quiet` `--strict` `--format {md,csv}` | 검증만 / 진행률 억제 / 침묵 종료 시 exit 1 / 출력 포맷 |

**종료 코드**: `0` 정상 · `1` `--strict` 조건 · `2` 설정/IO 오류 · `3` 실행 예외.

### `export` — 이미 끝난 실행을 다시 마크다운으로

```bash
python -m ABM.cli export --run-id <uuid> -o out.md
python -m ABM.cli export --scenario "4인가족(엄마아빠누나동생)_v3" --latest
python -m ABM.cli export --run-id <uuid> --include summary
```

이벤트·대화가 `simulation.db`에 영속되므로 GUI로 돌린 실행도 다른 토글로 다시 뽑을 수 있다.

### 마크다운 포매터 이중화

`ABM/export/markdown.py`(파이썬)와 `frontend/js/sim/export/markdown.js`(브라우저) 두 벌.
출력이 바이트 단위로 같도록 `tests/fixtures/golden_*.md` 골든 테스트로 고정.
포맷을 바꾸면 양쪽을 함께 고쳐야 한다.
