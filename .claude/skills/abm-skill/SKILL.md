---
name: abm-skill
description: "vLLM Chat ABM 시뮬레이션 개발 가이드. 새 시나리오 추가, 에이전트 시스템 프롬프트 설계, 시뮬레이션 로직 수정, Wave-based BFS 동작 이해 시 참조."
---

# ABM Development Guide — vLLM Chat

전체 흐름은 **`docs/simulation-overview.md`**, 엔진 내부(`run()` 루프·프롬프트 조립·계약
층)는 **`docs/simulation-engine.md`**, 기능별(위치·시간·감염·디렉터)은
**`docs/simulation-features.md`**. 용어는 **`docs/glossary.md` Part C·D**.

## Wave 기반 BFS

```
Wave 0: start_agent 발화 → 응답의 "target" 이 다음 발화자
Wave 1: 그 target들이 동시 발화 (ThreadPoolExecutor 병렬)
  ⋮
종료: max_waves | 목표 기간 | 연속 침묵(early stop) | 활성 에이전트 없음 | 중지
```

`run()` 루프의 정확한 wave 1회 처리 순서(발화 라우팅 → 외모 → 이동 → 감염 → next_wave)는
`docs/simulation-engine.md §2`. **순서가 곧 정확성** — 라우팅·외모·이동은 이동 전 위치
스냅샷, 감염은 이동 후.

## 에이전트 응답 JSON

엔진이 파싱하는 필드 (`ABM/parser.py`):

```json
{
  "content": "발화 내용 (반드시 한국어)",
  "action_note": "행동/생각 묘사 — 다른 에이전트에게 시각 정보로 전달",
  "target": ["agent_key"],          // 또는 "all" / "self"
  "move_to": null,                  // 장소명 또는 사람 ID (위치 그래프 있을 때)
  "update_appearance": null,        // 외모 변화 시 새 외모 전체 묘사
  "emotion": "...", "action": "..." // extra_fields 로 정의한 추가 메타
}
```

- 필드명은 **`target`** (단수). 값은 배열 / `"all"` / `"self"`.
- `"self"` / `"system"` = 혼잣말 (다음 wave 발화자 없음).

## 시나리오 = `SimStartConfig` (권장 경로)

시나리오는 **JSON 설정 객체**다. 만드는 법:
1. GUI 설정 뷰에서 편집 → 저장 (`simulation_scenarios` 테이블) → 필요하면 JSON 내보내기
2. `SimStartConfig` JSON 파일을 직접 작성 → `python -m ABM.cli run scenario.json`

필드 표: `docs/simulation-overview.md §9`. 최소 예시: `tests/fixtures/golden_scenario.json`.
정의는 `backend/api/simulation/schemas.py`의 `SimStartConfig` / `AgentConfig`.

```json
{
  "agents": [
    { "name": "manager", "display_name": "점장",
      "system_prompt": "당신은 카페 점장입니다. 목표: 손님을 친절히 응대하고 매출을 올린다. 친절하고 적극적인 말투.",
      "location": "", "relationships": {}, "initial_active": true }
  ],
  "background": "오후 3시, 조용한 카페. 손님이 막 들어왔다.",
  "start_agent": "manager",
  "max_waves": 10,
  "events": [
    { "wave": 2, "type": "system_message", "message": "갑자기 카페가 붐빈다.", "targets": ["all"] }
  ]
}
```

## 시스템 프롬프트 설계 — **JSON 형식 지시를 넣지 말 것**

프롬프트는 두 층 (`docs/simulation-engine.md §8`):
- **서사 층** (`system_prompt`, 사용자 소유) — 역할·목표·성격·배경만
- **계약 층** (엔진이 실행 시점에 생성) — JSON 출력 스키마·`move_to`·`target` ID 규칙·
  위치/시간/감염 규칙. `Agent.get_system_message()`가 `system_prompt` 뒤에 자동으로 붙인다.

따라서 `system_prompt`에는 **"반드시 JSON으로 응답하세요 {...}"를 쓰지 않는다** — 엔진이
붙이는 최신 계약과 충돌하고, 옛 스키마를 고착시킨다.

| 항목 | 가이드 |
|---|---|
| 역할 정의 | "당신은 [역할]입니다" — 명확한 정체성 |
| 목표 | 달성하려는 것 1~2개 |
| 성격/태도 | 대화 방식, 언어 스타일 |
| 관계 | `system_prompt`에 "딸이 있다"만 쓰면 LLM이 그 딸의 ID를 모른다 → `relationships` 필드로 `{상대 key: "딸"}` 바인딩 |
| 토큰 효율 | 500 토큰 이내 권장 |

## 이벤트 타입 (`ScenarioEvent`)

| type | 설명 | 필드 |
|---|---|---|
| `system_message` | 특정/전체 에이전트에 상황 알림 | `message`, `targets` |
| `agent_enter` | 새 에이전트 등장 (`active_agents` 추가) | `agent` (key), `message` |
| `agent_exit` | 에이전트 퇴장 | `agent` (key), `message` |
| `update_appearance` | 외모 변경 + 같은 장소에 씬 메시지 | `agent`, `message` (새 외모) |
| `infect_agent` | 환자 0번 시드 (감염 모델 켜져 있을 때) | `agent`, `wave`, `message` |

## `extra_fields` (커스텀 메타)

`SimStartConfig.extra_fields` = `[{name, default}]`. 기본 `emotion`·`action`·`action_note`.
LLM 응답 JSON에 해당 필드가 있으면 `meta` 딕셔너리에 파싱되어 로그·이벤트·마크다운에 실린다.

## 엔진 코드 수정

`ABM/simulation/`의 믹스인 (`Simulation` = 9개 상속). 어느 믹스인인지는
`docs/glossary.md C-1` / `docs/simulation-engine.md §1`.

- **fresh start의 유일한 조립 경로** = `ABM/simulation/headless.py: run_config()`. 여기의
  `Simulation(...)` / `sim.run(...)` 호출에 인자를 넘겨야 GUI `/start`와 CLI가 함께 바뀐다.
- `/load`·`/resume`은 **별도 조립 경로** (`backend/api/simulation/runtime/load.py`·`resume.py`).
  새 엔진 인자를 추가하면 **세 곳**(headless, load, resume)을 함께 고칠 것 —
  안 그러면 "`/start`로는 되는데 재개하면 조용히 꺼지는" 버그.
- 계약 블록을 추가하면 `ABM/prompt_contract.py: verify_contract`의 `_CONTRACT_ASSERTIONS`에
  필수 토큰을 등록 (기능은 있는데 지시어가 빠진 상태를 시작 시 잡는 안전망).

## ABM API 엔드포인트

`backend/api/simulation/` **패키지** (구 단일 파일 아님). 전체 명세는 `docs/api-reference.md §7~12`.
- `POST /api/simulation/start` · `/stop` · `/continue`
- `POST /api/simulation/load/{run_id}` · `/resume/{run_id}`
- `GET  /api/simulation/stream` — **SSE** (구 `/events` 아님)
- `GET  /api/simulation/scenarios` · `POST` · `PUT` · `DELETE`
- `POST /api/simulation/contract-preview` — 현재 설정으로 엔진이 만들 계약 문자열

## 코드 시나리오 (`ABM/scenarios/`)

`convenience_store.py`는 백엔드·DB 없이 vLLM에 직접 붙는 **로컬 스모크테스트 스크립트**다
(`python agent.py`로 실행). 실사용 시나리오는 GUI/JSON 경로를 쓴다 — `scenarios/__init__.py`에
등록 메커니즘은 없다.

## 마크다운 내보내기 이중화

`ABM/export/markdown.py`(파이썬) ↔ `frontend/js/sim/export/markdown.js`(브라우저) 두 벌.
`tests/fixtures/golden_*.md` 골든 테스트로 바이트 단위 고정. 포맷 변경 시 양쪽 함께.
`ABM/export/labels.py`의 순수 헬퍼도 `frontend/js/sim/state.js`와 동기화.
