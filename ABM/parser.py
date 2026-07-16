import json
import logging
import re

from .constants import DEFAULT_EXTRA_FIELDS as _DEFAULT_EXTRA_FIELDS

logger = logging.getLogger(__name__)

# Matches a response that is entirely wrapped in a markdown code fence.
# Anchored to start/end so triple-backticks inside JSON content don't trigger this.
_CODE_FENCE_RE = re.compile(r'^\s*```(?:json)?\s*\n?([\s\S]*?)\n?```\s*$')


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


def parse_json_response(
    content: str,
    extra_fields: list[dict] | None = None,
) -> tuple[str, dict, list]:
    """
    Parse LLM JSON response.
    Returns (clean_content, meta_values, targets)
      - meta_values: {field_name: value} for all configured extra_fields
    """
    _fields  = extra_fields if extra_fields is not None else _DEFAULT_EXTRA_FIELDS
    defaults = {f["name"]: f["default"] for f in _fields}

    try:
        raw = content
        m = _CODE_FENCE_RE.match(raw)
        if m:
            raw = m.group(1)

        raw  = _sanitize_json_strings(raw.strip())
        data = json.loads(raw)
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
        elif not targets:
            targets = ["self"]
        else:
            # 리스트 내 "system" 값을 "self"로 정규화
            targets = ["self" if t.lower() in ("system", "self") else t for t in targets]

        return clean_content, meta_values, targets

    except Exception:
        logger.warning(f"JSON 파싱 실패. 원본: {content[:200]}")
        return content, defaults, ["self"]


def parse_json_extras(content: str) -> dict:
    """Extract move_to and update_appearance from LLM response without breaking existing API."""
    try:
        raw = content
        m = _CODE_FENCE_RE.match(raw)
        if m:
            raw = m.group(1)
        data = json.loads(_sanitize_json_strings(raw.strip()))
        return {
            "move_to":           (data.get("move_to") or "").strip() or None,
            "update_appearance": (data.get("update_appearance") or "").strip() or None,
        }
    except Exception:
        return {}
