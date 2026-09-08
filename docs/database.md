# 데이터베이스 스키마

> 두 개의 SQLite 파일. 왜 분리했는지는 [`architecture.md §4`](architecture.md#4-저장소--sqlite-2개).
> ORM 없이 순수 `sqlite3`.

| 파일 | 소유 | 코드 |
|---|---|---|
| `memory.db` | 백엔드 (채팅) | `backend/db/database.py` |
| `logs_graph/simulation.db` | ABM (SimDB) | `ABM/db/` (`base.py` + 도메인 믹스인, DDL은 `schema.py`) |

**마이그레이션 규칙 (양쪽 공통)**: `CREATE TABLE IF NOT EXISTS` + 별도 `migrate()`에서
`PRAGMA table_info`로 컬럼 존재 확인 후 `ALTER TABLE ... ADD COLUMN`. 전부
nullable/기본값이라 기존 행은 영향 없음. 컬럼 삭제·이름 변경은 하지 않는다.

---

# 1. `memory.db` (채팅)

`init_tables()` + `migrate_db()`. 시드: `seed_default_agents` (planner/developer/reviewer),
`seed_default_servers` (`servers.json` 있으면).

## `memories`

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | TEXT PK | UUID |
| `type` | TEXT | `fact` / `decision` / `pending` |
| `content` | TEXT | 메모리 내용 |
| `created_at` | TEXT | ISO 생성 시각 |
| `last_accessed` | TEXT | 마지막 검색 조회 시각 (RAG 랭킹 2차 정렬 키) |

## `memory_keywords`

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `memory_id` | TEXT FK → `memories.id` | |
| `keyword` | TEXT | 소문자 정규화 |

인덱스 `idx_keyword ON (keyword)`.

## `conversations`

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | TEXT PK | UUID |
| `title` | TEXT | 대화 제목 (`"새 대화"`면 첫 메시지로 자동 생성) |
| `system_prompt` | TEXT | 대화 기본 시스템 프롬프트 |
| `agent_id` | TEXT | 고정 에이전트 (`agents.id`). null 가능 |
| `router_mode` | INTEGER | 1 = 메시지마다 LLM이 에이전트 선택 |
| `created_at` / `updated_at` | TEXT | ISO |

## `turns`

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | TEXT PK | UUID |
| `conversation_id` | TEXT | 소속 대화 |
| `role` | TEXT | `user` / `assistant` |
| `content` | TEXT | 메시지 본문 |
| `thinking` | TEXT | 추론 과정 (사고 켜졌을 때) |
| `memories_json` | TEXT | 이 응답에 주입된 메모리 목록 (JSON) |
| `context_pct` | REAL | 응답 시점 컨텍스트 사용률 0.0~1.0 |
| `prompt_tokens` | INTEGER | 입력 토큰 |
| `max_tokens` | INTEGER | 모델 컨텍스트 한도 |
| `sources_json` | TEXT | 웹 검색 출처 (JSON) |
| `archived` | INTEGER | 0 active / 1 아카이브됨 |
| `created_at` | TEXT | ISO |

인덱스 `idx_turns_conv ON (conversation_id, created_at)`.

## `agents` (채팅 에이전트)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | TEXT PK | UUID |
| `name` | TEXT | 이름 (`@멘션`·라우팅 매칭 키) |
| `description` | TEXT | 라우터 모드가 참조하는 설명 |
| `system_prompt` | TEXT | |
| `icon` | TEXT | 기본 `🤖` |
| `model` | TEXT | 모델 오버라이드 (null = 기본) |
| `temperature` | REAL | 기본 0.7 |
| `max_tokens` | INTEGER | 기본 1024 |
| `role` / `goal` / `backstory` | TEXT | 구조화 입력 → 시스템 프롬프트 자동 생성 |
| `gender` · `groups` · `location` · `visual_description` · `display_name` · `initial_active` · `relationships` | TEXT/BOOL | **시뮬레이션 에이전트와 공유되는 필드** — 채팅 로직은 해석 안 함. 채팅↔시뮬 왕복 시 보존만. `relationships`는 JSON 객체 문자열. `groups`(JSON 배열 문자열)는 **휴면 필드** — ABM 엔진에서 제거됐고 관계 지도로 대체됐다. 마이그레이션 회피용으로 컬럼만 유지 |
| `created_at` / `updated_at` | TEXT | ISO |

## `servers` (LLM 서버)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | TEXT PK | UUID |
| `name` | TEXT | |
| `base_url` | TEXT | 예: `http://172.17.3.135:8000` |
| `model` | TEXT | 모델명 |
| `provider_type` | TEXT | `vllm` / `openai` / `anthropic` (기본 `vllm`) |
| `weight` | INTEGER | (현재 미사용 — 라운드로빈은 균등) |
| `enabled` | INTEGER | 1 = 레지스트리에 로드 |
| `is_default` | INTEGER | 1 = 기본 서버 |
| `thinking` | INTEGER | 하위호환 파생값 (`thinking_level != 'off'`) |
| `thinking_level` | TEXT | `off` / `low` / `medium` / `high` (소스 오브 트루스) |
| `max_model_len` | INTEGER | 0 = 자동 감지 |
| `api_key` | TEXT | (마이그레이션으로 추가) 평문. API 응답에서는 마스킹 |
| `created_at` | TEXT | ISO |

## `simulation_scenarios` (시나리오 **정의**)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | TEXT PK | UUID |
| `name` | TEXT | 시나리오 이름 (내보내기 파일 제목) |
| `description` | TEXT | |
| `config_json` | TEXT | `SimStartConfig` JSON (구 `output_format_template`은 저장 시 비움) |
| `created_at` / `updated_at` | TEXT | ISO |

> 시나리오 **실행 결과**는 여기가 아니라 `simulation.db`에 있다.

---

# 2. `logs_graph/simulation.db` (SimDB)

`ABM/db/schema.py`의 `SCHEMA` + `migrate()`. WAL 모드, 스레드별 커넥션
(`threading.local`).

## 실행·타임라인

### `simulation_runs`

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `run_id` | TEXT PK | 실행 UUID |
| `scenario_id` | TEXT | 채팅 DB `simulation_scenarios.id` (파일 실행이면 null) |
| `scenario_name` | TEXT | 표시 이름 |
| `run_number` | INTEGER | 배치 실행 순번 (기본 1) |
| `status` | TEXT | `running` / `done` / `stopped` / `error` |
| `start_wave` | INTEGER | 이 run 첫 wave의 누적 표시 wave 번호 (재개 체인용) |
| `total_waves` | INTEGER | 이 run이 완료한 wave 수 |
| `total_turns` | INTEGER | 총 턴 |
| `started_at` / `finished_at` | REAL | epoch |
| `config_json` | TEXT | 실행 시점 `SimStartConfig` 스냅샷 (`/load`·`/resume`이 읽음) |
| `active_agents_json` | TEXT | 종료 시점 활성 에이전트 (재개용) |
| `pending_wave_json` | TEXT | 마지막으로 target됐지만 미발화한 wave (재개 시작점) |
| `elapsed_minutes` | INTEGER | 총 경과 분 (이전 run 누적 + 이번 wave 경과) |

인덱스 `idx_runs_scenario ON (scenario_id)`.

### `simulation_log` (턴별 발화 = 타임라인)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `run_id` | TEXT | |
| `wave` / `turn` | INTEGER | 누적 표시 wave / 턴 번호 |
| `speaker` | TEXT | 발화자 `agent.name` |
| `content` | TEXT | 정제된 발화 (`clean_content`) |
| `action_note` | TEXT | 행동 묘사 |
| `meta_json` | TEXT | extra_fields (emotion 등, `action_note` 제외) |
| `targets_json` | TEXT | 원본 `target` 배열 |
| `timestamp` | REAL | epoch |
| `time_str` | TEXT | 시뮬레이션 내 시각 (`월요일 오전 9시 30분`) |
| `location` | TEXT | 그 턴 시점(이동 전) 발화자 위치 — 접촉 분석용 |
| `is_exterior` | INTEGER | 외부 공간 여부 |

인덱스 `idx_simlog_run ON (run_id, id)`.

### `sim_events` (영속화된 SSE 이벤트)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | INTEGER PK | |
| `run_id` | TEXT | |
| `wave` | INTEGER | |
| `event_type` | TEXT | `agent_move`, `infection_update`, `system_intervention`, `meeting_update`, `scene_event`, `director_call`, `appearance_update`, `time_jump` (= `_PERSIST_EVENTS`). `world_event`는 레거시 — 구 실행 행에만 존재 |
| `data_json` | TEXT | 이벤트 페이로드 |
| `timestamp` | REAL | epoch |

인덱스 `idx_simevents_run ON (run_id, id)`.

### `agent_snapshots` (재개용 상태)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `run_id` + `agent_key` | TEXT, PK | |
| `memory_json` | TEXT | 에이전트 `memory` (개인 컨텍스트) |
| `state_json` | TEXT | 위치·외모·인지관계·낯선 이 ID·경로·만남 lock·감염 SIR 상태 (`export_agent_state()` 산물). null = 구버전 → 시나리오 초기값 폴백 |

인덱스 `idx_snapshots_run ON (run_id)`.

### `interview_log` (사후 인터뷰)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | INTEGER PK | |
| `run_id` | TEXT FK (ON DELETE CASCADE) | |
| `agent_key` | TEXT | |
| `mode` | TEXT | `memory_only` / `full_log` |
| `question` / `answer` | TEXT | |
| `meta_json` | TEXT | |
| `created_at` | REAL | epoch |

> **타임라인과 완전 분리.** 리플레이·피드 조회(`get_run_log`)는 이 테이블을 절대
> 읽지 않는다 — 인터뷰 발화가 섞이면 재개/재생 결과가 오염된다.

## 구조화 메모리 (압축 산물)

에이전트 `memory`가 `token_limit`의 70%에 근접하면 `memory_compressor.compress()`가
LLM으로 델타 압축해 아래 테이블을 upsert하고 raw는 `messages`에 아카이브.
전부 `(sim_id, agent_key)` 스코프. `get_full_memory()`가 4개를 묶어 반환.

### `messages` (raw 아카이브)

`id` PK · `sim_id` · `agent_key` · `role` · `content` · `wave` · `token_est` · `created_at`.
인덱스 `idx_msg_sim_agent`.

### `episodic_memory`

`id` PK · `sim_id` · `agent_key` · `wave` · `event`(사건 요약) · `participants` ·
`importance`(1-5, 기본 3) · `created_at`. 인덱스 `idx_ep_sim_agent`.

### `semantic_memory` (사실/믿음)

`id` PK · `sim_id` · `agent_key` · `fact` · `confidence`(0-1) · `source_wave` ·
`prev_fact` · `prev_confidence` (믿음 변경 이력) · `updated_at`. 인덱스 `idx_sem_sim_agent`.

### `relationship_memory` (현재 인물 관계)

`(sim_id, agent_key, target_key)` PK · `stance`(`trust`/`neutral`/`suspect`/`hostile`) ·
`reason` · `updated_wave` · `updated_at`. 인덱스 `idx_rel_sim_agent`.

### `relationship_history` (관계 변화 이력)

`id` PK · `sim_id` · `agent_key` · `target_key` · `stance` · `reason` · `wave` · `created_at`.

### `agent_self_state`

`(sim_id, agent_key)` PK · `description`(동기·감정 한 문장) · `updated_wave` · `updated_at`.

### `compression_log`

`id` PK · `sim_id` · `agent_key` · `msg_count` · `wave` · `created_at`.
