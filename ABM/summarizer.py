"""Wave-based simulation summarizer — calls LLM to produce narrative summaries."""
from __future__ import annotations

import json
import logging

from .llm import LLMCall

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "당신은 멀티에이전트 시뮬레이션의 관찰자입니다. "
    "주어진 대화 기록을 바탕으로 이 구간에서 일어난 일을 간결하게 요약하세요. "
    "반드시 JSON 형식으로만 응답하고, 다른 텍스트는 출력하지 마세요."
)

_USER_TEMPLATE = """\
아래는 Wave {start}~{end}의 대화 기록입니다.

[배경]
{background}

[대화 기록]
{log_text}

다음 JSON 형식으로 응답하세요:
{{
  "summary": "이 구간의 핵심 흐름 (2-3문장)",
  "key_events": ["주요 사건1", "주요 사건2"],
  "mood": "현재 분위기를 한 단어 또는 짧은 구절로"
}}"""


def summarize_waves(
    entries: list[dict],
    background: str,
    wave_start: int,
    wave_end: int,
    llm: LLMCall,
    key_to_alias: dict[str, str] | None = None,
    llm_max_tokens: int = 16384,
) -> dict | None:
    """Call LLM to summarize a slice of the simulation log.

    Returns a dict with keys ``summary``, ``key_events``, ``mood``,
    or None if summarization fails.
    """
    if not entries:
        return None

    lines = []
    for e in entries:
        speaker     = (key_to_alias or {}).get(e["speaker"], e["speaker"])
        targets     = [t for t in e.get("targets", []) if t != "system"]
        target_str  = ", ".join("전체" if t == "all" else (key_to_alias or {}).get(t, t) for t in targets) or "(독백)"
        content     = e.get("content", "")
        action_note = e.get("action_note", "")
        wave        = e.get("wave", "?")
        line        = f"[W{wave}] {speaker} → {target_str}: {content}"
        if action_note:
            line += f"  *{action_note}*"
        lines.append(line)

    user_msg = _USER_TEMPLATE.format(
        start    = wave_start,
        end      = wave_end,
        background = background.strip() or "(없음)",
        log_text = "\n".join(lines),
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    try:
        content, _, _ = llm(messages, max_tokens=llm_max_tokens)
        raw = content.strip()
        # Strip markdown code fence if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[summarizer] 요약 실패 (W{wave_start}-{wave_end}): {exc}")
        return None
