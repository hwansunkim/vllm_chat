"""System agent — simulation narrator/director.

Runs after each wave (configurable interval), receives the latest summary,
silent agents, repetition info, director_note, and accumulated director_memo,
then outputs interventions, an optional world_event broadcast, an updated
director_memo, and a reason string.
"""
from __future__ import annotations

import json
import logging

from .llm import chat_response

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
- 모든 텍스트는 한국어로 작성\
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
    model: str,
    base_url: str,
    api_timeout: int,
    llm_max_tokens: int = 16384,
) -> dict | None:
    """Run the system agent LLM call.

    Returns a dict with ``interventions``, ``world_event``, ``director_memo``,
    and ``reason``, or None on failure.
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

    user_msg = _USER_TEMPLATE.format(
        wave                  = wave,
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
        content, _, _ = chat_response(
            messages, model=model, base_url=base_url, timeout=api_timeout,
            max_tokens=llm_max_tokens,
        )
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[system_agent] LLM 호출 실패 (W{wave}): {exc}")
        return None


_REPEAT_THRESHOLD_PCT = 65  # simulation.py의 _REPEAT_THRESHOLD * 100
