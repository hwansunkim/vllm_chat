"""System agent — simulation narrator/director.

Runs after each wave (configurable interval), receives the latest summary
and a list of agents silent for too long, then outputs intervention messages
to be injected into those agents' next-wave incoming context.
"""
from __future__ import annotations

import json
import logging

from .llm import chat_response

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_AGENT_PROMPT = """\
당신은 멀티에이전트 시뮬레이션의 내레이터이자 진행자입니다.
주어진 시뮬레이션 요약과 침묵 중인 에이전트 목록을 분석하여
이야기 흐름을 자연스럽게 이어가기 위한 개입이 필요한지 판단하세요.

개입이 필요한 경우, 해당 에이전트에게 이야기 흐름에 맞는
상황 묘사·사건·주변 변화·대화 유도 등의 메시지를 전달하세요.
개입이 불필요한 경우(이야기가 자연스럽게 흐르고 있다면) interventions를 빈 배열로 반환하세요.\
"""

_USER_TEMPLATE = """\
[현재 Wave: {wave}]

[최근 요약]
{summary}

[활성 에이전트]
{agents}

[침묵 중인 에이전트 ({threshold}웨이브 이상 미발화)]
{silent}

반드시 아래 JSON 형식으로만 응답하세요:
{{
  "interventions": [
    {{"agent": "에이전트_ID", "message": "에이전트에게 전달할 상황/자극 메시지"}}
  ],
  "reason": "개입 이유 (없으면 빈 문자열)"
}}

규칙:
- interventions가 없으면 빈 배열 []
- agent는 반드시 활성 에이전트 ID 중 하나 (표시 이름 사용 금지)
- message는 에이전트가 행동하도록 유도하는 상황 묘사나 사건 (한국어)\
"""


def run_system_agent(
    *,
    system_prompt: str,
    wave: int,
    summary: dict | None,
    active_agents: dict[str, str],   # key → display_name
    silent_agents: list[str],         # agent keys
    silence_threshold: int,
    key_to_alias: dict[str, str] | None,
    model: str,
    base_url: str,
    api_timeout: int,
) -> dict | None:
    """Run the system agent LLM call.

    Returns a dict with ``interventions`` (list of {agent, message}) and
    ``reason``, or None on failure.
    """
    alias = key_to_alias or {}

    agent_lines = "\n".join(
        f'  - ID: "{k}"  ({alias.get(k, k)})' if alias.get(k) else f'  - ID: "{k}"'
        for k in active_agents
    )
    silent_lines = "\n".join(
        f'  - ID: "{k}"  ({alias.get(k, k)})' for k in silent_agents
    ) if silent_agents else "  없음"

    if summary:
        summary_text = summary.get("summary", "")
        key_events = summary.get("key_events", [])
        mood = summary.get("mood", "")
        summary_str = summary_text
        if key_events:
            summary_str += "\n주요 사건: " + ", ".join(key_events)
        if mood:
            summary_str += f"\n분위기: {mood}"
    else:
        summary_str = "아직 요약 없음"

    user_msg = _USER_TEMPLATE.format(
        wave=wave,
        summary=summary_str,
        agents=agent_lines,
        silent=silent_lines,
        threshold=silence_threshold,
    )

    messages = [
        {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_AGENT_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    try:
        content, _, _ = chat_response(
            messages, model=model, base_url=base_url, timeout=api_timeout
        )
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[system_agent] LLM 호출 실패 (W{wave}): {exc}")
        return None
