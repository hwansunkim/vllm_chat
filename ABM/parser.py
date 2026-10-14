import json
import logging

from .constants import DEFAULT_EXTRA_FIELDS as _DEFAULT_EXTRA_FIELDS

logger = logging.getLogger(__name__)


def _sanitize_json_strings(raw: str) -> str:
    """Escape literal control characters inside JSON string values.

    LLMs sometimes emit raw newlines / tabs inside strings instead of \\n / \\t,
    which makes json.loads() raise a JSONDecodeError.  Walk the text character-by-
    character, tracking whether we are inside a JSON string, and replace bare
    newline/carriage-return/tab with their JSON escape sequences.
    """
    out: list[str] = []
    in_str = False
    skip   = False
    for ch in raw:
        if skip:
            out.append(ch)
            skip = False
            continue
        if ch == '\\':
            out.append(ch)
            skip = True   # next char is escaped — don't flip in_str
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str:
            if ch == '\n':
                out.append('\\n')
                continue
            if ch == '\r':
                out.append('\\r')
                continue
            if ch == '\t':
                out.append('\\t')
                continue
        out.append(ch)
    return ''.join(out)


def _extract_first_json_object(content: str) -> dict | None:
    """LLM 응답에서 **첫 번째** JSON 객체를 관대하게 뽑아낸다.

    LLM이 실제로 내는 지저분한 모양들을 모두 흡수한다:
      - 맨몸 객체
      - ```json ... ``` 펜스로 감싼 객체 (앞뒤에 산문이 붙어도 됨)
      - **여러 객체를 이어붙인 응답** — 약한 모델이 여러 턴 분량을 한 응답에
        배치로 담을 때 각 객체를 제 펜스로 감싸 나열한다. 첫 객체만 취하고
        나머지는 버린다(`json.JSONDecoder().raw_decode` 가 뒤쪽을 무시).
      - 문자열 값 안의 리터럴 개행/탭 (`_sanitize_json_strings`)

    파싱 가능한 객체가 하나도 없으면 None. 이 경우 호출부는 원본을 대사로
    흘리지 말고 턴 실패로 처리해야 한다(turn_error emit + 롤백).
    """
    text    = _sanitize_json_strings(content)
    decoder = json.JSONDecoder()
    idx     = text.find("{")
    tried   = 0
    while idx != -1 and tried < 5:
        try:
            obj, _end = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        idx    = text.find("{", idx + 1)
        tried += 1
    return None


def parse_json_response(
    content: str,
    extra_fields: list[dict] | None = None,
) -> tuple[str, dict, list, dict | None]:
    """
    Parse LLM JSON response.
    Returns (clean_content, meta_values, targets, parsed)
      - meta_values: {field_name: value} for all configured extra_fields
      - parsed: 성공 시 첫 JSON 객체(dict), 실패 시 None. None이면 호출부는
        이 턴을 실패로 취급한다 — 원본 JSON 텍스트를 대사로 흘리지 않는다.
    """
    _fields  = extra_fields if extra_fields is not None else _DEFAULT_EXTRA_FIELDS
    defaults = {f["name"]: f["default"] for f in _fields}

    data = _extract_first_json_object(content)
    if data is None:
        logger.warning(f"JSON 파싱 실패 — 유효한 객체 없음. 원본: {content[:200]}")
        return "", defaults, ["self"], None

    clean_content = data.get("content") or ""
    meta_values   = {f["name"]: data.get(f["name"], f["default"]) for f in _fields}
    targets       = data.get("target", [])

    if isinstance(targets, str):
        tl = targets.lower().strip()
        if tl in ("system", "self"):   # "system" 은 하위 호환
            targets = ["self"]
        elif tl == "all":
            targets = ["all"]
        else:
            targets = [t.strip() for t in targets.split(',') if t.strip()]
    elif not isinstance(targets, list):
        targets = ["self"]
    elif not targets:
        targets = ["self"]
    else:
        # 리스트 내 "system" 값을 "self"로 정규화
        targets = ["self" if str(t).lower() in ("system", "self") else t for t in targets]

    return clean_content, meta_values, targets, data


def _normalize_bring_along(raw) -> list[str] | None:
    """`bring_along` 값 → 비지 않은 문자열 목록(순서 유지·중복 제거), 없으면 None.

    배열이 정석이지만 LLM이 한 명을 문자열로 주거나("신짱구") 쉼표로 이어 쓰는 경우도
    흡수한다. 숫자·객체 등 문자열이 아닌 항목은 버린다.
    """
    if raw is None:
        return None
    items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
    out: list[str] = []
    for it in items:
        if isinstance(it, str) and it.strip() and it.strip() not in out:
            out.append(it.strip())
    return out or None


# 문자열 계약 필드 — 값은 장소명·사람 ID·외모 묘사·상태 ID 같은 **문자열**이어야 한다.
# 계약(prompt_contract)이 문자열을 요구해도 모델이 `{"id": "sleep"}` 같은 객체를
# 넣는 일이 실측됐다(case1 v12: `'dict' object has no attribute 'strip'` 5건 —
# 턴 대사가 통째로 사라지고 롤백·turn_error 도 생략됐다).
_STRING_EXTRA_FIELDS = ("move_to", "update_appearance", "enter_state")


def _normalize_str_field(field: str, raw, errors: list[dict] | None = None) -> str | None:
    """문자열 계약 필드 값 정규화 — 문자열이면 strip(빈 문자열은 None), None 이면 None.

    그 외 타입(dict·list·숫자·bool)은 **이 필드만 None 으로 버리고** 타입 위반을
    ``errors`` 에 기록한다. ``str(value)`` 로 강제 변환하지 않는다 — 그러면
    ``"{'id': 'sleep'}"`` 가 실제 상태 ID·장소명으로 전달돼 다른 오류를 숨긴다.
    객체 형태를 지원하려면 허용 키와 의미를 계약에 먼저 명시해야 한다.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    if errors is not None:
        try:
            shown = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            shown = repr(raw)
        errors.append({
            "field":   field,
            "type":    type(raw).__name__,
            "preview": shown[:120],
        })
    return None


def parse_json_extras_checked(content: str) -> tuple[dict, list[dict]]:
    """`parse_json_extras` + 필드 타입 위반 목록.

    반환 ``(extras, type_errors)``. ``type_errors`` 의 각 항목은
    ``{"field", "type", "preview"}`` — 호출부(`_step_agent`)가 wave·agent·원문과 함께
    경고 로그를 남긴다(이 모듈은 그 맥락을 모른다). 타입이 틀린 필드만 None 이 되고
    대사·다른 필드는 그대로 쓰인다 — 턴은 성공한다.
    """
    data = _extract_first_json_object(content)
    if data is None:
        return {}, []
    errors: list[dict] = []
    extras = {
        "move_to":           _normalize_str_field("move_to", data.get("move_to"), errors),
        "bring_along":       _normalize_bring_along(data.get("bring_along")),
        "update_appearance": _normalize_str_field("update_appearance", data.get("update_appearance"), errors),
        "enter_state":       _normalize_str_field("enter_state", data.get("enter_state"), errors),
    }
    return extras, errors


def parse_json_extras(content: str) -> dict:
    """Extract move_to/bring_along/update_appearance/enter_state from the LLM response
    without breaking existing API.

    문자열 필드에 문자열이 아닌 값이 오면 그 필드만 None 이 되고 경고가 남는다
    (`_normalize_str_field`). 타입 위반 목록이 필요하면 `parse_json_extras_checked`.
    """
    extras, errors = parse_json_extras_checked(content)
    for e in errors:
        logger.warning(
            f"[parser] extras 필드 타입 위반 — {e['field']}: {e['type']} "
            f"(문자열/null 이어야 함) → 이 필드만 무시. 값: {e['preview']}"
        )
    return extras
