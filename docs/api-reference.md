# API 레퍼런스

> REST 엔드포인트 + SSE 이벤트 스펙. Base URL: `http://localhost:8888`.
> 파이프라인 동작은 [`backend.md`](backend.md), 시뮬레이션 흐름은
> [`simulation-overview.md`](simulation-overview.md). 스키마 정의:
> `backend/api/schemas.py`, `backend/api/simulation/schemas.py`.

관례: 명시 안 하면 `Content-Type: application/json`. 삭제는 `204 No Content`.
검증 실패는 `422` (pydantic).

---

## 1. 모델 상태

### `GET /api/model/status`

```json
{
  "model": "google/gemma-4-31B-it",
  "base_url": "http://172.17.3.135:8000",
  "max_model_len": 131072,
  "current_server": { "id": "...", "name": "...", "thinking_level": "off", "thinking": false },
  "servers": [
    { "id": "...", "name": "...", "model": "...", "model_len": 131072,
      "is_default": true, "enabled": true, "thinking_level": "off", "thinking": false }
  ]
}
```

---

## 2. 대화

### `GET /api/conversations`
최근 수정순. `[{ id, title, updated_at, last_msg }]`

### `POST /api/conversations` → `201`
```json
{ "title": "새 대화", "system_prompt": "", "agent_id": null, "router_mode": false }
```
→ `{ id, title, system_prompt, agent_id }`

### `GET /api/conversations/{id}`
대화 + 전체 턴 (아카이브 포함).
```json
{ "id", "title", "system_prompt", "agent_id", "router_mode", "created_at", "updated_at",
  "turns": [
    { "role": "user", "content", "thinking": "", "memories_json": null,
      "context_pct": null, "prompt_tokens": null, "max_tokens": null, "sources_json": null },
    { "role": "assistant", "content", "thinking", "memories_json": "[...]",
      "context_pct": 0.12, "prompt_tokens": 1024, "max_tokens": 131072, "sources_json": null }
  ] }
```

### `PATCH /api/conversations/{id}/title`
`{ "title": "새 제목" }` → `{ "ok": true }`

### `DELETE /api/conversations/{id}` → `204`

### `POST /api/conversations/{id}/chat` — **SSE**

```json
{ "content": "사용자 메시지",
  "thinking_level": "off",       // "off"|"low"|"medium"|"high"|null. null = 서버 기본값
  "web_search": false }
```

`Content-Type: text/event-stream`. 이벤트 순서:

| event | data |
|---|---|
| `search` | `{ query, results: [{title, url, snippet}] }` (웹검색 켜졌을 때) |
| `thinking` | `{ chunk }` (반복) |
| `answer` | `{ chunk }` (반복) |
| `done` | `{ memories: [{type, content}], usage: {prompt_tokens, completion_tokens, max_model_len, context_pct, thinking}, archived_count, title, sources, used_agent: {name, icon, routing}|null, used_server: {id, name, model, model_len}, thinking_level, thinking_mode }` |
| `error` | `{ message }` |

에러: `404` 없는 대화, `503` 사용 가능한 서버 없음.

---

## 3. 채팅 에이전트

### `GET /api/agents` · `GET /api/agents/{id}`
```json
{ "id", "name", "description", "system_prompt", "icon", "model": null,
  "temperature": 0.7, "max_tokens": 1024, "role", "goal", "backstory",
  "gender": "auto", "groups": [], "location": "", "visual_description": "",
  "display_name": "", "initial_active": true, "relationships": {},
  "created_at", "updated_at" }
```

### `POST /api/agents` → `201` (`AgentCreate`)
필수: `name`. 나머지는 위 기본값. `role`/`goal`/`backstory`를 주면 프론트가
시스템 프롬프트 자동 생성에 활용.

### `PUT /api/agents/{id}` (`AgentUpdate`)
**부분 업데이트** (`exclude_unset`). 시뮬레이션 공유 필드(`gender`, `groups`, `location`,
`visual_description`, `display_name`, `initial_active`, `relationships`)는 `null`이 와도
"건드리지 않음"으로 취급 (기본값 리셋 방지). `model`은 `null` = 지우기.

### `DELETE /api/agents/{id}` → `204`

---

## 4. RAG 메모리

### `GET /api/memories?q=&type=`
`type` = `fact`/`decision`/`pending`. `q` = content 또는 keyword LIKE 검색.
```json
[{ "id", "type", "content", "created_at", "last_accessed", "keywords": ["...", ...] }]
```

### `DELETE /api/memories/{id}` → `204`

---

## 5. LLM 서버

### `GET /api/servers`
`api_key`는 마스킹. `model_len`은 레지스트리의 라이브 값.

### `POST /api/servers` → `201` (`ServerCreate`)
```json
{ "name", "base_url", "model",
  "provider_type": "vllm",         // "vllm"|"openai"|"anthropic"
  "api_key": "", "weight": 1, "is_default": false,
  "thinking_level": "off", "max_model_len": 0 }
```
등록 즉시 레지스트리에 추가 + `fetch_model_len()`.

### `PUT /api/servers/{id}` (`ServerUpdate`) — 부분 업데이트
### `DELETE /api/servers/{id}` → `204`

### `POST /api/servers/probe-models` (`ModelProbeRequest`)
저장 전 `/v1/models` 조회 (DB 미변경).
```json
요청: { "provider_type", "base_url": "", "api_key": "", "server_id": null }
응답: { "provider_type", "base_url", "models": [{ "id", "context_length": null }] }
```

### `GET /api/servers/{id}/health` (`ServerHealth`)
```json
{ "server_id", "name", "model",
  "status": "ok",              // "ok"|"model_missing"|"unreachable"|"disabled"
  "healthy": true, "reachable": true, "model_ok": true,
  "detail": "...", "available_models": [] }
```

---

## 6. 시뮬레이션 — 시나리오 (정의)

Prefix `/api/simulation`. 저장소는 채팅 DB `simulation_scenarios`.

### `GET /scenarios`
`[{ id, name, description, config_json, config, created_at, updated_at }]` — `config`는
파싱된 객체.

### `POST /scenarios` → `201` (`ScenarioSave`)
`{ name, description, config: SimStartConfig }` → `{ id, name }`

### `PUT /scenarios/{sid}` (`ScenarioSave`) → `{ id }`
### `DELETE /scenarios/{sid}` → `204`
### `GET /default-output-format` — **deprecated**, 항상 `{ "template": "" }`

`SimStartConfig` 필드 표: [`simulation-overview.md §9`](simulation-overview.md#9-시나리오-형식--simstartconfig).

---

## 7. 시뮬레이션 — 실행 라이프사이클

### `POST /start` (`SimStartConfig`) → `{ "status": "started" }`
`409` 이미 실행 중.

### `POST /stop` → `{ "status": "stopping" }`

### `POST /continue` (`SimContinueConfig`) → `{ "status": "continuing" }`
```json
{ "start_agent", "max_waves": 10, "step_delay": 1.0, "events": [],
  "target_duration_minutes": null }
```
`409` `status ∉ {done, stopped}` 또는 `sim_obj` 없음.
**감염 모델·에이전트 등은 바꿀 수 없다** — `/start` 시점 config 스냅샷이 유효.

### `POST /load/{run_id}` → 메모리에 복원, 실행 안 함
```json
{ "status": "loaded", "log": [...simulation_log...], "infection": { "<key>": {status, cause} },
  "start_wave": <int> }
```
`404` 없는 run, `400` config 스냅샷 없음, `409` 실행 중.

### `POST /resume/{run_id}` → `{ "status": "resuming", "start_wave": <int> }`
복원 + 즉시 스레드 실행. `404`/`400`/`409` 동일.

---

## 8. 시뮬레이션 — SSE

### `GET /stream` — **EventSource**

`_sim["event_queue"]`를 소비. 큐가 없으면 `event: error {"message":"no active simulation"}`.
30초간 이벤트 없으면 `event: ping`. `None` sentinel → `event: simulation_end {}`.

| event | data (주요 필드) | 영속 |
|---|---|---|
| `wave_start` | `wave`, `agents[]` | |
| `turn_start` | `turn`, `wave`, `speaker`, `memory_size`, `est_tokens`, `token_limit` | |
| `turn_situation` | `wave`, `agent`, `text` (`[현재 상황]` 또는 `[몸 상태]`) | |
| `turn_complete` | `turn`, `wave`, `speaker`, `targets[]`, `content`, `action_note`, `meta{}`, `prompt_tokens`, `token_limit`, `reasoning_preview`, `new_edges[]`, `is_exterior`, `time_str` | |
| `turn_error` | `turn`, `speaker`, `error` | |
| `turn_language_fix` | `speaker`, `wave`, `turn` | |
| `scene_event` | `event_type` (`system_message`/`agent_enter`/`agent_exit`/`infect_agent`/`update_appearance`), `message`, `targets[]`, `agent`, `observer_only` | ✅ |
| `director_call` | `wave`, `digest_waves`, `prompt_tokens`, `prompt_chars`, `elapsed_ms`, `intervened`, `n_interventions`, `world_event`, `failed`, `icon`, `display_name` | ✅ |
| `system_intervention` | `wave`, `target`, `target_alias`, `message`, `reason`, `icon`, `display_name` | ✅ |
| `world_event` | `wave`, `content`, `targets[]`, `target_aliases[]`, `reason`, `icon`, `display_name` | ✅ |
| `agent_move` | `wave`, `agent`, `display_name`, `from`, `to`, `to_exterior` | ✅ |
| `meeting_update` | `chaser`, `chaser_name`, `target`, `target_name`, `target_location`, `status` (`start`/`arrived`/`cancelled`), `reason`, `wave` | ✅ |
| `infection_update` | `wave`, `elapsed_minutes`, `agent`, `display_name`, `status` (`S`/`I`/`R`), `cause` (`event`/`transmission`/`recovery`), `disease_name` | ✅ |
| `appearance_update` | `wave`, `agent`, `display_name`, `description` | ✅ |
| `time_jump` | `wave`, `mode`, `used_fallback`, `category_id`, `category_label`, `reason`, `raw_minutes`, `minutes`, `clamp_reason`, `end_time_str` | ✅ |
| `compression_start` / `compression_done` | `agent`, `wave`, `msg_count` | |
| `simulation_end` | `total_turns`, `edges_count`, `log_count`, `end_reason` (`max_waves`/`target_duration`/`silence`/`no_agents`/`stopped`) | |
| `ping` | `{}` | |

**영속** = `sim_events` 테이블 저장 (`_PERSIST_EVENTS`) = 마크다운 내보내기 대상.

---

## 9. 시뮬레이션 — 조회

### `GET /status` → `{ status, log_count, edge_count }`

### `GET /logs` → `shared_log` (background 제외)
`[{ speaker, content, meta, action_note, targets, wave, timestamp, time_str, location, is_exterior }]`

### `GET /events?types=agent_move,world_event` → 저장된 SSE 이벤트
`[{ wave, event_type, data, timestamp }]`

### `GET /runs/{run_id}/events?types=` → 과거 실행의 SSE 이벤트

### `GET /edges` → `[{ source, target, emotion, meta, content, timestamp }]`

### `GET /agents/{name}/context`
그 에이전트가 실제로 받는 프롬프트.
```json
{ "name", "memory_size", "trimmed", "prompt_tokens", "est_tokens", "token_limit",
  "messages": [{ "role": "system"|"user"|"assistant", "content" }] }
```
`404` 없는 에이전트.

### `GET /agents/{name}/memory`
```json
{ "episodes": [...], "facts": [...], "relationships": [...], "self_state": "..." }
```
`404` 활성 시뮬레이션·메모리 DB 없음.

---

## 10. 시뮬레이션 — 실행 이력

### `GET /runs?scenario_id=` → `[{ run_id, scenario_name, status, started_at, ... }]`
### `GET /runs/{run_id}` → run 전체 행. `404` 없음.
### `GET /runs/{run_id}/log` → `simulation_log` 항목
### `DELETE /runs/{run_id}` → `204`
### `POST /runs/bulk-delete` → `204`
`{ "run_ids": ["...", "..."] }`

---

## 11. 시뮬레이션 — 계약 미리보기

### `POST /contract-preview` (`ContractPreviewRequest`)

`SimStartConfig`의 계약 관련 부분집합만 받는다:
```json
{ "location_graph": [], "time_mode": "fixed", "time_per_wave": 30,
  "infection_model": {...}, "extra_fields": [...], "output_format_override": "",
  "include_output_schema": true, "situation_targets": false,
  "available_targets": [], "key_to_alias": {}, "relationships": {} }
```
응답 (`ContractPreviewResponse`):
```json
{ "contract": "world + output 전체 (실제 주입 순서/문자열)",
  "world_contract": "지도+시간+감염 정적 블록",
  "output_contract": "출력 스키마 + move_to + target",
  "flags": { "has_location_graph", "has_zone", "time_enabled", "infection_enabled", "include_output_schema" },
  "warnings": [] }
```

---

## 12. 시뮬레이션 — 사후 인터뷰

### `POST /runs/{run_id}/agents/{name}/interview` (`InterviewRequest`)
```json
{ "question": "...", "mode": "memory_only", "max_tokens": null }
```
`mode` = `memory_only` (구조화 메모리만) / `full_log` (전체 로그). 종료된 run만 (`409`).
→ `InterviewRecord { id, run_id, agent_key, mode, question, answer, created_at, meta }`

### `GET /runs/{run_id}/agents/{name}/interview` → `[InterviewRecord]`
