"""Wave time classifier — calls LLM to classify how much time a wave's scene spans."""
from __future__ import annotations

import json
import logging

from .llm import LLMCall

logger = logging.getLogger(__name__)

# ── 공통 방법론 (두 모드가 같은 판단 기준을 쓰도록 한곳에) ──────────────────────
# 2026-09 재보정: 예전 앵커("대사 줄 수 = 실시간 자막 재생 시간")가 너무 강하게
# 짧은 쪽으로 쏠려, 실제 시뮬레이션에서 wave당 평균 2~5분씩만 흘러 하루를
# 지나는 데 수백 wave가 필요했다. 대사만 문자 그대로 읽는 속도가 아니라 그
# 사이사이 있었을 화면 밖 동작(차리기·치우기·오가기 등)까지 포함한 "장면
# 전체 길이"로 옮기고, "의심스러우면 짧게"는 제거했다 — 예정된 시각을 넘기는
# 점프는 `_clamp_time_jump`의 at_time 가드가 이 문구와 무관하게 이미 막고
# 있어서, 판단 자체를 느슨하게 풀어도 일정을 건너뛰는 사고는 재발하지 않는다.
_METHOD_BLOCK = """\
판단 재료:
- 대사만 문자 그대로 읽는 속도가 아니라, 그 대사들 사이사이 있었을 화면 밖
  동작(차리기·치우기·씻기·오가기 등 장면에 직접 드러나지 않는 자잘한 행동)까지
  포함한 "장면 전체 길이"로 판단합니다. 짧은 대화 한두 마디는 2~5분, 여러 마디를
  주고받는 보통의 장면은 15~20분 정도가 자연스럽습니다.
- 행동 묘사(*설거지를 끝냈다*, *한숨 자고 일어났다*)나 대사 속 시간 표현
  (*이따 봐*, *다음 날*, *한참 뒤*)이 있으면 그 길이를 우선 반영합니다.
- 인물들이 한 공간에서 **활발하게 대화를 주고받는 도중**이면 크게 압축하지
  마십시오. 서로 떨어져 상호작용이 없거나 대화가 잦아든 상황에서만 수십 분~몇
  시간이 정당합니다(위 인물 배치 참고).
- 큰 값을 고르면 그 사이 벌어졌을 다른 인물의 귀가·식사·마중·등원 장면이 통째로
  사라질 수 있습니다(단, 예정된 시각을 넘기는 점프는 엔진이 별도로 막습니다).
  확신이 없으면 장면의 성격에 맞는 중간값을 고르십시오."""


_SYSTEM_PROMPT = (
    "당신은 멀티에이전트 시뮬레이션의 시간 관찰자입니다. "
    "한 장면(Wave)이 이야기 속에서 몇 분에 걸쳐 일어난 일인지 판단해, 주어진 "
    "카테고리 중 가장 잘 맞는 것 하나를 고르는 것이 유일한 역할입니다. "
    "반드시 JSON으로만 응답하세요."
)

_USER_TEMPLATE = """\
아래는 이번 Wave의 장면입니다.
{current_time_block}{placement_block}{next_beat_block}[장면] (대사 {turn_count}줄)
{log_text}

[카테고리]
{category_list}

{method_block}

먼저 근거를 한 줄로 정리하고, 그다음 카테고리를 고르세요.
다음 JSON 형식으로만 응답하세요:
{{
  "reason": "무엇을 보고 이 카테고리라 판단했는지 한 줄",
  "category": "<카테고리 id 중 하나>"
}}"""


_MINUTES_SYSTEM_PROMPT = (
    "당신은 멀티에이전트 시뮬레이션의 시간 관찰자입니다. "
    "한 장면(Wave)이 이야기 속에서 실제로 몇 분에 걸쳐 일어난 일인지 "
    "분 단위 정수로 추정하는 것이 유일한 역할입니다. "
    "반드시 JSON으로만 응답하세요."
)

_MINUTES_USER_TEMPLATE = """\
아래는 이번 Wave의 장면입니다.
{current_time_block}{placement_block}{next_beat_block}[장면] (대사 {turn_count}줄)
{log_text}

{method_block}

이 장면이 이야기 속에서 몇 분 동안 일어난 일인지 추정하세요.
- 먼저 근거를 한 줄로 정리하고, 그다음 숫자를 정하세요.
- 답은 {lo}분 이상 {hi}분 이하의 정수. 30·60 같은 라운드 넘버에 얽매이지 마세요.
- 확신이 없으면 장면 성격에 맞는 중간값으로.

다음 JSON 형식으로만 응답하세요:
{{
  "reason": "무엇을 보고 몇 분이라 판단했는지 한 줄",
  "minutes": <정수>
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


def _context_blocks(current_time: str, placement: str, next_beat: str) -> dict:
    """프롬프트 템플릿의 선택적 컨텍스트 블록들을 조립한다.

    각 블록은 값이 있을 때만 나타나고, 없으면 빈 문자열이라 템플릿이 그대로
    접힌다(``[현재 시각]`` 이 원래 그랬던 것과 같은 방식).
    """
    return {
        "current_time_block": f"[현재 시각] {current_time}\n" if current_time else "",
        "placement_block":     f"[인물 배치] {placement}\n" if placement else "",
        "next_beat_block": (
            f"[다음 예정 시각] {next_beat} — 이 시각을 넘겨 점프하지 마십시오.\n"
            if next_beat else ""
        ),
    }


def estimate_wave_minutes(
    entries: list[dict],
    llm: LLMCall,
    key_to_alias: dict[str, str] | None = None,
    llm_max_tokens: int = 256,
    current_time: str = "",
    lo: int = 1,
    hi: int = 480,
    placement: str = "",
    next_beat: str = "",
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

    user_msg = _MINUTES_USER_TEMPLATE.format(
        **_context_blocks(current_time, placement, next_beat),
        method_block       = _METHOD_BLOCK,
        turn_count         = len(entries),
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
    placement: str = "",
    next_beat: str = "",
) -> str | None:
    """Call LLM to classify the elapsed-time category of a single wave's scene.

    Returns the chosen category id, or None if classification fails or the
    returned id is not among ``categories``.
    """
    if not entries:
        return None

    category_list = "\n".join(f"- {c['id']}: {c.get('label', '')}" for c in categories)

    user_msg = _USER_TEMPLATE.format(
        **_context_blocks(current_time, placement, next_beat),
        method_block       = _METHOD_BLOCK,
        turn_count         = len(entries),
        log_text           = _format_entries(entries, key_to_alias),
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
