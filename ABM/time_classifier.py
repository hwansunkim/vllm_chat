"""Wave time classifier — calls LLM to classify how much time a wave's scene spans."""
from __future__ import annotations

import json
import logging

from .llm import LLMCall

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "당신은 멀티에이전트 시뮬레이션의 시간 관찰자입니다. "
    "주어진 대화 장면을 보고 아래 카테고리 중 가장 적절한 것 하나를 고르세요. "
    "반드시 JSON으로만 응답하세요."
)

_USER_TEMPLATE = """\
아래는 이번 Wave의 대화 장면입니다.

[대화 장면]
{log_text}

[카테고리]
{category_list}

다음 JSON 형식으로 응답하세요:
{{
  "category": "<카테고리 id 중 하나>",
  "reason": "한 줄 이유"
}}"""


def classify_wave_time(
    entries: list[dict],
    categories: list[dict],
    llm: LLMCall,
    key_to_alias: dict[str, str] | None = None,
    llm_max_tokens: int = 256,
) -> str | None:
    """Call LLM to classify the elapsed-time category of a single wave's scene.

    Returns the chosen category id, or None if classification fails or the
    returned id is not among ``categories``.
    """
    if not entries:
        return None

    lines = []
    for e in entries:
        speaker     = (key_to_alias or {}).get(e.get("speaker", ""), e.get("speaker", ""))
        content     = e.get("content", "")
        action_note = e.get("action_note", "")
        line        = f"{speaker}: {content}"
        if action_note:
            line += f"  *{action_note}*"
        lines.append(line)

    category_list = "\n".join(f"- {c['id']}: {c.get('label', '')}" for c in categories)

    user_msg = _USER_TEMPLATE.format(
        log_text      = "\n".join(lines),
        category_list = category_list,
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    valid_ids = {c["id"] for c in categories}

    try:
        content, _, _ = llm(messages, max_tokens=llm_max_tokens)
        raw = content.strip()
        # Strip markdown code fence if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        category = parsed.get("category")
        if category not in valid_ids:
            return None
        return category
    except Exception as exc:
        logger.warning(f"[time_classifier] 시간 분류 실패: {exc}")
        return None
