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


def parse_json_extras(content: str) -> dict:
    """Extract move_to and update_appearance from LLM response without breaking existing API."""
    data = _extract_first_json_object(content)
    if data is None:
        return {}
    return {
        "move_to":           (data.get("move_to") or "").strip() or None,
        "update_appearance": (data.get("update_appearance") or "").strip() or None,
    }
