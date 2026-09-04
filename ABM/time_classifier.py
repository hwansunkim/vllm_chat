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
{current_time_block}
[대화 장면]
{log_text}

[카테고리]
{category_list}

큰 시간 경과를 고르면 이 시각 이후 다른 구성원이 귀가·등장하거나 함께 모이는
장면(식사·귀가·마중 등)을 통째로 건너뛸 수 있습니다. 확실히 아무 일도 일어나지
않는 한적한 장면이거나 밤(모두 취침)일 때만 큰 카테고리를 고르세요.

다음 JSON 형식으로 응답하세요:
{{
  "category": "<카테고리 id 중 하나>",
  "reason": "한 줄 이유"
}}"""


_MINUTES_SYSTEM_PROMPT = (
    "당신은 멀티에이전트 시뮬레이션의 시간 관찰자입니다. "
    "주어진 대화 장면이 실제로 몇 분에 걸쳐 일어났을지 분 단위 정수로 추론하세요. "
    "반드시 JSON으로만 응답하세요."
)

_MINUTES_USER_TEMPLATE = """\
아래는 이번 Wave의 대화 장면입니다.
{current_time_block}
[대화 장면]
{log_text}

이 장면이 실제로 몇 분 동안 일어난 일인지 추론하세요. 답은 {lo}분 이상 {hi}분
이하의 정수여야 합니다.

큰 시간 경과를 고르면 이 시각 이후 다른 구성원이 귀가·등장하거나 함께 모이는
장면(식사·귀가·마중 등)을 통째로 건너뛸 수 있습니다. 확실히 아무 일도 일어나지
않는 한적한 장면이거나 밤(모두 취침)일 때만 큰 값을 고르세요. 대화가 실제로
오가는 장면이라면 대체로 짧은 값이 맞습니다.
30분·60분 같은 라운드 넘버에 얽매이지 말고 장면 길이에 맞는 값을 쓰세요.

다음 JSON 형식으로 응답하세요:
{{
  "minutes": <정수>,
  "reason": "한 줄 이유"
}}"""


def _format_entries(entries: list[dict], key_to_alias: dict[str, str] | None) -> str:
    lines = []
    for e in entries:
        speaker     = (key_to_alias or {}).get(e.get("speaker", ""), e.get("speaker", ""))
        content     = e.get("content", "")
        action_note = e.get("action_note", "")
        line        = f"{speaker}: {content}"
        if action_note:
            line += f"  *{action_note}*"
        lines.append(line)
    return "\n".join(lines)


def _strip_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return raw


def estimate_wave_minutes(
    entries: list[dict],
    llm: LLMCall,
    key_to_alias: dict[str, str] | None = None,
    llm_max_tokens: int = 256,
    current_time: str = "",
    lo: int = 1,
    hi: int = 480,
) -> tuple[int, str] | None:
    """LLM에게 이번 wave의 경과 시간(분)을 **직접** 추론시킨다 (AI 모드).

    ``classify_wave_time``(카테고리 모드)의 대안이며, 그 함수를 대체하지 않는다.

    반환:
        ``(minutes, reason)`` 튜플. ``minutes``는 추론된 경과 분(``lo``~``hi``로
        clamp됨), ``reason``은 LLM이 준 한 줄 이유(없으면 빈 문자열). 이 이유는
        호출부(runner)가 ``time_jump`` 이벤트로 사용자 화면까지 전달한다.
        응답을 정수 분으로 해석할 수 없으면 ``None`` — 호출부는 이때 카테고리
        모드로 폴백해야 한다.

    ``classify_wave_time``과 동일하게 예외를 밖으로 던지지 않는다.
    """
    if not entries:
        return None

    try:
        lo = int(lo)
        hi = int(hi)
    except Exception:
        lo, hi = 1, 480
    if lo > hi:
        lo, hi = hi, lo
    lo = max(0, lo)
    hi = max(lo, hi)

    current_time_block = f"\n[현재 시각]\n{current_time}\n" if current_time else ""

    user_msg = _MINUTES_USER_TEMPLATE.format(
        current_time_block = current_time_block,
        log_text           = _format_entries(entries, key_to_alias),
        lo                 = lo,
        hi                 = hi,
    )

    messages = [
        {"role": "system", "content": _MINUTES_SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    try:
        content, _, _ = llm(messages, max_tokens=llm_max_tokens)
        parsed = json.loads(_strip_fence(content))
        if not isinstance(parsed, dict):
            logger.warning("[time_classifier] AI 시간 추론 응답이 객체가 아님 — 폴백")
            return None
        raw_minutes = parsed.get("minutes")
        # bool은 int의 서브클래스라 명시적으로 배제한다.
        if isinstance(raw_minutes, bool):
            return None
        if isinstance(raw_minutes, (int, float)):
            minutes = int(raw_minutes)
        elif isinstance(raw_minutes, str):
            minutes = int(float(raw_minutes.strip()))
        else:
            logger.warning(f"[time_classifier] AI 시간 추론 minutes 해석 불가({raw_minutes!r}) — 폴백")
            return None

        clamped = max(lo, min(hi, minutes))
        if clamped != minutes:
            logger.info(
                f"[time_classifier] AI 시간 추론 {minutes}분 → sanity clamp {clamped}분 "
                f"(범위 {lo}~{hi})"
            )
        reason = str(parsed.get("reason", "")).strip()
        logger.info(f"[time_classifier] AI 시간 추론: {clamped}분 — {reason}")
        return clamped, reason
    except Exception as exc:
        logger.warning(f"[time_classifier] AI 시간 추론 실패: {exc}")
        return None


def classify_wave_time(
    entries: list[dict],
    categories: list[dict],
    llm: LLMCall,
    key_to_alias: dict[str, str] | None = None,
    llm_max_tokens: int = 256,
    current_time: str = "",
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

    current_time_block = f"\n[현재 시각]\n{current_time}\n" if current_time else ""

    user_msg = _USER_TEMPLATE.format(
        current_time_block = current_time_block,
        log_text           = "\n".join(lines),
        category_list      = category_list,
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
