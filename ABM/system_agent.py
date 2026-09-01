"""System agent — simulation narrator/director.

Runs after each wave (configurable interval), receives the latest summary,
silent agents, repetition info, director_note, and accumulated director_memo,
then outputs interventions, an optional world_event broadcast, an updated
director_memo, and a reason string.
"""
from __future__ import annotations

import json
import logging

from .llm import LLMCall

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_AGENT_PROMPT = """\
당신은 멀티에이전트 시뮬레이션의 내레이터이자 진행자입니다.
주어진 시뮬레이션 요약, 침묵·반복 에이전트 정보, 감독 노트를 분석하여
이야기가 감독 노트의 방향으로 흐르도록 필요한 개입을 결정하세요.

개입 수단은 두 가지입니다:
1. interventions: 특정 에이전트에게 1:1로 상황·자극 메시지 전달
2. world_event: 특정 그룹 또는 전체에 환경 변화·사건을 브로드캐스트

이야기가 자연스럽게 흐르고 있다면 interventions는 빈 배열, world_event는 null로 반환하세요.\
"""

_USER_TEMPLATE = """\
[현재 Wave: {wave}]

{current_time_section}\
{director_note_section}\
{director_memo_section}\
[최근 요약]
{summary}

[활성 에이전트]
{agents}

[침묵 중인 에이전트 ({threshold}웨이브 이상 미발화)]
{silent}

[반복 중인 에이전트 (최근 발언 유사도 {repeat_threshold}% 이상)]
{repetition}

반드시 아래 JSON 형식으로만 응답하세요:
{{
  "interventions": [
    {{"agent": "에이전트_ID", "message": "에이전트에게 전달할 상황/자극 메시지"}}
  ],
  "world_event": {{
    "content": "전체 또는 그룹에 브로드캐스트할 세계 사건 묘사",
    "targets": ["all"]
  }},
  "director_memo": "이번 개입 결과와 다음 전략 메모 (간결하게 1~2줄)",
  "reason": "개입 이유 또는 판단 근거"
}}

규칙:
- interventions가 없으면 빈 배열 []
- world_event가 없으면 null (객체가 아닌 null)
- agent는 반드시 활성 에이전트 ID 중 하나 (표시 이름 사용 금지)
- world_event.targets: "all" / "group:그룹명" / 특정 에이전트 ID 목록
- 모든 텍스트는 한국어로 작성
- **시각·시계·시간을 임의로 지어내지 말 것.** 위 [현재 시각]만을 참조하십시오.
  [현재 시각] 섹션이 없다면 이 세계에는 시간 개념이 없는 것이므로 시각을 아예 언급하지 마십시오.
  "벽시계가 N시를 알린다" 같은 표현은 [현재 시각]과 정확히 일치할 때만 쓸 수 있습니다.
- **world_event는 물리적 사실을 새로 만들지 않습니다.** 환경의 분위기·외부 자극만 묘사하십시오.
  사물의 존재(예: 없던 음식이 놓여 있다), 특정 인물의 완료된 행동(예: 누가 요리를 마쳤다),
  물리적 상태 변화를 world_event로 만들어내지 마십시오 — 그건 에이전트가 행동으로 만드는 것입니다.
  디렉터가 주는 것은 '무엇이 일어났다'가 아니라 '무엇이 느껴진다 / 보인다 / 들린다'입니다.\
"""


def run_system_agent(
    *,
    system_prompt: str,
    wave: int,
    summary: dict | None,
    active_agents: dict[str, str],       # key → display_name
    silent_agents: list[str],             # agent keys
    silence_threshold: int,
    repetition_info: dict[str, float],    # key → similarity score
    director_note: str,
    director_memo: str,
    key_to_alias: dict[str, str] | None,
    llm: LLMCall,
    llm_max_tokens: int = 16384,
    current_time_str: str = "",
) -> dict | None:
    """Run the system agent LLM call.

    Returns a dict with ``interventions``, ``world_event``, ``director_memo``,
    and ``reason``, or None on failure.

    ``current_time_str`` 은 에이전트들이 보는 것과 **같은** 시각 문자열
    (`Simulation._format_time_str(...)`, 요일 포함)이다. 비어 있으면 [현재 시각]
    섹션 자체를 생략한다 — 시간 개념이 꺼진 시뮬레이션에서 없는 시계를 만들지
    않기 위해서다. 이 인자가 없던 시절 디렉터는 시각을 전혀 못 받아 world_event에
    엉뚱한 시각("벽시계가 8시를 친다")을 지어냈다.
    """
    alias = key_to_alias or {}

    agent_lines = "\n".join(
        f'  - ID: "{k}"  ({alias.get(k, k)})' if alias.get(k) else f'  - ID: "{k}"'
        for k in active_agents
    )
    silent_lines = "\n".join(
        f'  - ID: "{k}"  ({alias.get(k, k)})' for k in silent_agents
    ) if silent_agents else "  없음"

    if repetition_info:
        repetition_lines = "\n".join(
            f'  - ID: "{k}"  ({alias.get(k, k)})  유사도: {int(v * 100)}%'
            for k, v in repetition_info.items()
        )
    else:
        repetition_lines = "  없음"

    if summary:
        summary_text = summary.get("summary", "")
        key_events   = summary.get("key_events", [])
        mood         = summary.get("mood", "")
        summary_str  = summary_text
        if key_events:
            summary_str += "\n주요 사건: " + ", ".join(key_events)
        if mood:
            summary_str += f"\n분위기: {mood}"
    else:
        summary_str = "아직 요약 없음"

    director_note_section = (
        f"[감독 노트 — 서사 목표]\n{director_note}\n\n"
        if director_note.strip() else ""
    )
    director_memo_section = (
        f"[진행 기록 — 이전 개입 누적]\n{director_memo}\n\n"
        if director_memo.strip() else ""
    )

    current_time_section = (
        f"[현재 시각]\n{current_time_str.strip()}\n\n"
        if (current_time_str or "").strip() else ""
    )

    user_msg = _USER_TEMPLATE.format(
        wave                  = wave,
        current_time_section  = current_time_section,
        director_note_section = director_note_section,
        director_memo_section = director_memo_section,
        summary               = summary_str,
        agents                = agent_lines,
        silent                = silent_lines,
        threshold             = silence_threshold,
        repetition            = repetition_lines,
        repeat_threshold      = int(_REPEAT_THRESHOLD_PCT),
    )

    messages = [
        {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_AGENT_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    try:
        content, _, _ = llm(messages, max_tokens=llm_max_tokens)
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[system_agent] LLM 호출 실패 (W{wave}): {exc}")
        return None


_REPEAT_THRESHOLD_PCT = 65  # simulation.py의 _REPEAT_THRESHOLD * 100
