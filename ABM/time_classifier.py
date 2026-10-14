"""Wave time classifier — calls LLM to classify how much time a wave's scene spans."""
from __future__ import annotations

import json
import logging

from .llm import LLMCall, preview_raw, summarize_usage
from .parser import _extract_first_json_object, _sanitize_json_strings

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

카테고리를 고르세요. 근거(reason)는 선택이며, 쓴다면 40자 이내로 짧게.
다음 JSON 형식으로만 응답하세요(category 를 먼저):
{{"category": "<카테고리 id 중 하나>", "reason": "40자 이내 근거(선택)"}}"""


_MINUTES_SYSTEM_PROMPT = (
    "당신은 멀티에이전트 시뮬레이션의 시간 관찰자입니다. "
    "한 장면(Wave)이 이야기 속에서 실제로 몇 분에 걸쳐 일어난 일인지 "
    "분 단위 정수로 추정하는 것이 유일한 역할입니다. "
    "반드시 JSON으로만 응답하세요."
)

# 출력 순서: 숫자(minutes/category)를 **먼저**, 근거(reason)는 선택·짧게.
# 예전엔 "먼저 근거를 한 줄로 정리하고" + reason 우선 예시라, 근거가 길어지면
# 256토큰 요청 한도에서 숫자를 쓰기 전에 잘렸다(case1 v12 시간 추론 실패 248건 중
# 83% 가 `Unterminated string starting at line 2 column 13` = reason 값 잘림).
# minutes 뒤 reason 이 잘리는 경우는 여전히 JSON 이 불완전해 실패한다 — 의도된
# 동작이다(잘린 JSON 을 고쳐 성공시키지 않는다).
_MINUTES_USER_TEMPLATE = """\
아래는 이번 Wave의 장면입니다.
{current_time_block}{placement_block}{next_beat_block}[장면] (대사 {turn_count}줄)
{log_text}

{method_block}

이 장면이 이야기 속에서 몇 분 동안 일어난 일인지 추정하세요.
- 답은 {lo}분 이상 {hi}분 이하의 정수. 30·60 같은 라운드 넘버에 얽매이지 마세요.
- 확신이 없으면 장면 성격에 맞는 중간값으로.
- 근거(reason)는 선택이며, 쓴다면 40자 이내로 짧게.

다음 JSON 형식으로만 응답하세요(minutes 를 먼저, 7 은 형식 예시일 뿐):
{{"minutes": 7, "reason": "40자 이내 근거(선택)"}}"""


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
    """앞뒤 코드 펜스(```json … ```)를 걷어낸다. 어떤 입력에도 예외를 내지 않는다.

    예전 구현(``raw.split("\n", 1)[1]``)은 개행 없는 펜스(```json{...}``` · 잘려서
    ```json 만 온 응답)에서 IndexError 를 냈다(case1 v12 Wave 423 `list index out
    of range`). 여는 펜스 뒤 언어 태그는 개행이 있든 없든 떼고, 닫는 펜스는
    끝에 있을 때만 뗀다.
    """
    raw = (raw or "").strip()
    if not raw.startswith("```"):
        return raw
    body = raw[3:]
    nl = body.find("\n")
    if nl != -1:
        body = body[nl + 1:]
    else:
        # 개행 없음 — 언어 태그(json 등 영숫자)만 떼고 나머지는 남긴다.
        i = 0
        while i < len(body) and (body[i].isalnum() or body[i] in "_-"):
            i += 1
        body = body[i:]
    body = body.rstrip()
    if body.endswith("```"):
        body = body[:-3]
    return body.strip()


# ── 시간 추론 응답 파싱 ────────────────────────────────────────────────────────
# 실패 유형 코드 — 로그 grep 용(`실패유형=<code>`).
#   llm_exception : LLM 호출 자체가 예외
#   empty         : 빈 응답
#   no_json       : `{` 가 아예 없음(산문만)
#   invalid_json  : `{` 는 있으나 완결된 객체가 없음 — 잘림(Unterminated string 등)
#                   또는 형식 오류. 잘린 JSON 을 임의로 닫거나 정규식으로 값만
#                   뽑아 성공 처리하지 않는다(분석 보고서 권고).
#   missing_field : 객체는 있으나 필수 필드(minutes/category) 없음
#   bad_type      : 필드 타입 위반(minutes 가 문자열·bool·소수 등)
#   out_of_range  : minutes 가 절대 허용 범위(0~_ABS_MAX_MINUTES) 밖
#   unknown_category : category 가 목록에 없는 id
#   unexpected    : 검증 코드 자체의 예상 못한 예외(방어용, 정상이면 나오지 않음)

# minutes 의 절대 허용 범위. 이 범위 안이면 호출부가 준 [lo, hi] 로 clamp 한다
# (hi 는 다음 예정 시각까지의 남은 분으로 좁혀지는 **일정 캡**이기도 해서, 그걸
# 넘었다고 실패시키면 유효한 추정을 버리고 무작위 폴백을 쓰게 된다). 음수나
# 일주일을 넘는 값은 장면 길이로 의미가 없으므로 실패로 본다.
_ABS_MAX_MINUTES = 7 * 1440


def _parse_time_json(content) -> tuple[dict | None, str | None, str]:
    """응답에서 첫 JSON 객체를 꺼낸다 → ``(obj, 실패유형, 상세)``.

    성공이면 ``(dict, None, "")``. 전체 ``json.loads`` 대신 첫 객체만 읽으므로
    뒤에 붙은 두 번째 객체·설명은 무시된다(`Extra data` 방지).
    """
    if not isinstance(content, str) or not content.strip():
        return None, "empty", "빈 응답"
    text = _strip_fence(content)
    if "{" not in text:
        return None, "no_json", "JSON 객체 없음"
    obj = _extract_first_json_object(text)
    if obj is None:
        # 진단용 — 첫 `{` 부터 엄격 파싱했을 때의 오류 메시지(예전 로그와 같은 문구라
        # `Unterminated string` 같은 패턴으로 이전 실행과 비교할 수 있다).
        detail = "완결된 JSON 객체 없음"
        try:
            json.JSONDecoder().raw_decode(_sanitize_json_strings(text[text.index("{"):]))
        except ValueError as e:
            detail = f"{detail}: {e}"
        return None, "invalid_json", detail
    return obj, None, ""


def _parse_minutes(obj: dict) -> tuple[int | None, str | None, str]:
    """``minutes`` 필드 검증 → ``(분, 실패유형, 상세)``.

    허용: bool 이 아닌 int, 또는 정수값 float(7.0). 문자열("7")·소수(7.5)·bool 은
    계약(정수) 위반으로 실패 — 모호한 값을 추측해 성공시키지 않는다.
    """
    if "minutes" not in obj or obj.get("minutes") is None:
        return None, "missing_field", "minutes 없음"
    raw = obj["minutes"]
    if isinstance(raw, bool):
        return None, "bad_type", f"minutes 가 bool({raw!r})"
    if isinstance(raw, float):
        if not raw.is_integer():
            return None, "bad_type", f"minutes 가 정수가 아닌 수({raw!r})"
        raw = int(raw)
    if not isinstance(raw, int):
        return None, "bad_type", f"minutes 가 {type(raw).__name__}({raw!r})"
    if raw < 0 or raw > _ABS_MAX_MINUTES:
        return None, "out_of_range", f"minutes={raw} (허용 0~{_ABS_MAX_MINUTES})"
    return raw, None, ""


def _reason_of(obj: dict) -> str:
    """reason 은 선택 필드 — 문자열일 때만 쓰고 없거나 다른 타입이면 빈 문자열."""
    r = obj.get("reason")
    return r.strip() if isinstance(r, str) else ""


def _log_time_failure(
    call_kind: str, wave, fail_kind: str, detail: str,
    content, max_tokens, usage, reasoning=None,
) -> None:
    """시간 추론/분류 실패 경고 한 줄 — 원문·요청 한도·usage·종료 사유 포함.

    ``finish_reason``/``stop_reason`` 은 provider 가 usage 에 실어 줄 때만 나온다
    (현재는 미제공 — `summarize_usage` 가 "미제공"으로 명시한다).
    """
    label = "AI 시간 추론" if call_kind == "time_estimate" else "시간 분류"
    reasoning_part = (
        f" reasoning_chars={len(reasoning)}" if isinstance(reasoning, str) and reasoning else ""
    )
    logger.warning(
        f"[time_classifier] {label} 실패 kind={call_kind} "
        f"wave={wave if wave is not None else '-'} agent=- 실패유형={fail_kind} — {detail} | "
        f"max_tokens={max_tokens} {summarize_usage(usage)}{reasoning_part} "
        f"원문(앞 200자)={preview_raw(content)}"
    )


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
    wave: int | None = None,
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

    ``minutes`` 는 bool 이 아닌 정수(또는 정수값 float)여야 하고 0~``_ABS_MAX_MINUTES``
    안이어야 한다. 그 안에서 ``lo``~``hi`` 를 벗어나면 clamp(실패 아님). ``reason`` 은
    선택이다. 실패는 유형별로 구분해 원문과 함께 경고한다(`_log_time_failure`).
    ``wave`` 는 로그 식별용(선택).
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
        content, reasoning, usage = llm(messages, max_tokens=llm_max_tokens)
    except Exception as exc:
        _log_time_failure("time_estimate", wave, "llm_exception",
                          f"{type(exc).__name__}: {exc}", None, llm_max_tokens, None)
        return None

    try:
        parsed, fail, detail = _parse_time_json(content)
        if parsed is not None:
            minutes, fail, detail = _parse_minutes(parsed)
        if fail is not None:
            _log_time_failure("time_estimate", wave, fail, detail,
                              content, llm_max_tokens, usage, reasoning)
            return None

        clamped = max(lo, min(hi, minutes))
        if clamped != minutes:
            logger.info(
                f"[time_classifier] AI 시간 추론 {minutes}분 → sanity clamp {clamped}분 "
                f"(범위 {lo}~{hi})"
            )
        reason = _reason_of(parsed)
        logger.info(f"[time_classifier] AI 시간 추론: {clamped}분 — {reason}")
        return clamped, reason
    except Exception as exc:   # 방어 — 위 검증은 예외를 내지 않아야 한다
        _log_time_failure("time_estimate", wave, "unexpected",
                          f"{type(exc).__name__}: {exc}", content, llm_max_tokens, usage)
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
    wave: int | None = None,
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
        content, reasoning, usage = llm(messages, max_tokens=llm_max_tokens)
    except Exception as exc:
        _log_time_failure("time_classify", wave, "llm_exception",
                          f"{type(exc).__name__}: {exc}", None, llm_max_tokens, None)
        return None

    try:
        parsed, fail, detail = _parse_time_json(content)
        if parsed is not None:
            category = parsed.get("category")
            if category is None:
                fail, detail = "missing_field", "category 없음"
            elif not isinstance(category, str):
                fail, detail = "bad_type", f"category 가 {type(category).__name__}({category!r})"
            elif category not in valid_ids:
                fail, detail = "unknown_category", f"category={category!r}"
        if fail is not None:
            _log_time_failure("time_classify", wave, fail, detail,
                              content, llm_max_tokens, usage, reasoning)
            return None
        return category
    except Exception as exc:   # 방어
        _log_time_failure("time_classify", wave, "unexpected",
                          f"{type(exc).__name__}: {exc}", content, llm_max_tokens, usage)
        return None
