---
name: abm-skill
description: "vLLM Chat ABM 시뮬레이션 개발 가이드. 새 시나리오 추가, 에이전트 시스템 프롬프트 설계, 시뮬레이션 로직 수정, Wave-based BFS 동작 이해 시 참조."
---

# ABM Development Guide — vLLM Chat

## 시뮬레이션 엔진 핵심 개념

**Wave-based BFS 흐름**:
```
Wave 0: start_agent가 발화 → targets=[agent_b, agent_c]
Wave 1: agent_b, agent_c가 병렬 발화 (ThreadPoolExecutor)
Wave 2: agent_b/c의 targets가 다음 발화자
...
max_waves 또는 targets 없으면 종료
```

**에이전트 응답 JSON 형식**:
```json
{
  "content": "대화 내용",
  "targets": ["agent_key"],
  "emotion": "기쁨",
  "custom_field": "추가 메타"
}
```

## 새 시나리오 생성 (convenience_store.py 참고)

```python
# ABM/scenarios/my_scenario.py
from ABM.agent import Agent
from ABM.simulation import Simulation

def build_scenario(event_queue=None, stop_event=None):
    # 1. 에이전트 정의
    agents = {
        "manager": Agent(
            name="점장",
            system_prompt="""당신은 카페 점장입니다.
목표: 손님을 친절히 응대하고 매출을 올린다.
대화 방식: 친절하고 적극적.

반드시 다음 JSON 형식으로 응답하세요:
{"content": "발화 내용", "targets": ["customer"], "emotion": "친절함"}""",
            token_limit=8192,
        ),
        "customer": Agent(
            name="손님",
            system_prompt="""당신은 카페를 방문한 손님입니다.
목표: 마음에 드는 음료를 주문하고 편안히 쉰다.

반드시 다음 JSON 형식으로 응답하세요:
{"content": "발화 내용", "targets": ["manager"], "emotion": "호기심"}""",
            token_limit=8192,
        ),
    }

    # 2. 공유 배경 (모든 에이전트가 아는 사실)
    background_log = [
        {"role": "user", "content": "오후 3시, 조용한 카페. 손님이 막 들어왔다."}
    ]

    # 3. 시나리오 이벤트 (선택사항)
    events = [
        {
            "wave": 2,
            "type": "system_message",
            "message": "갑자기 카페가 붐비기 시작한다.",
            "targets": ["all"]
        },
    ]

    # 4. 한국어 이름 매핑
    name_aliases = {"점장": "manager", "손님": "customer"}

    sim = Simulation(
        agents=agents,
        background_log=background_log,
        event_queue=event_queue,
        stop_event=stop_event,
        name_aliases=name_aliases,
    )
    return sim, events
```

## 시나리오 등록

`ABM/scenarios/__init__.py`에 추가:
```python
from .my_scenario import build_scenario as my_scenario
```

## 시스템 프롬프트 설계 원칙

| 항목 | 가이드 |
|------|------|
| 역할 정의 | "당신은 [역할]입니다" — 명확한 정체성 |
| 목표 | 에이전트가 달성하려는 것 (1~2개) |
| 성격/태도 | 대화 방식, 언어 스타일 |
| JSON 형식 | 반드시 예시 포함 (targets 필드 설명) |
| 토큰 효율 | 500 토큰 이내 권장 |

## 이벤트 타입

| 타입 | 설명 | 필수 필드 |
|------|------|----------|
| `system_message` | 모든/특정 에이전트에 상황 알림 | `message`, `targets` |
| `agent_enter` | 새 에이전트 등장 (active_agents 추가) | `agent` (key), `message` |
| `agent_exit` | 에이전트 퇴장 (active_agents 제거) | `agent` (key), `message` |

## extra_fields 활용 (에이전트별 커스텀 메타)

```python
Agent(
    name="형사",
    system_prompt="...",
    extra_fields=["emotion", "suspicion_level", "target_suspect"],
)
```
LLM 응답에 해당 필드가 있으면 자동으로 `meta` 딕셔너리에 파싱됨.

## ABM API 엔드포인트

백엔드 API (`backend/api/simulation.py`):
- `POST /api/simulation/start` — 시뮬레이션 시작
- `GET /api/simulation/events` — SSE로 실시간 이벤트 수신
- `POST /api/simulation/stop` — 시뮬레이션 중단
- `GET /api/simulation/scenarios` — 사용 가능한 시나리오 목록
