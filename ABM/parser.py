import json
import logging
import re

logger = logging.getLogger(__name__)

# Matches a response that is entirely wrapped in a markdown code fence.
# Anchored to start/end so triple-backticks inside JSON content don't trigger this.
_CODE_FENCE_RE = re.compile(r'^\s*```(?:json)?\s*\n?([\s\S]*?)\n?```\s*$')

_DEFAULT_EXTRA_FIELDS = [
    {"name": "emotion",     "default": "neutral"},
    {"name": "action",      "default": "speak"},
    {"name": "action_note", "default": ""},
]


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

        data          = json.loads(raw.strip())
        clean_content = data.get("content", "")
        meta_values   = {f["name"]: data.get(f["name"], f["default"]) for f in _fields}
        targets       = data.get("target", [])

        if isinstance(targets, str):
            tl = targets.lower().strip()
            if tl == "system":
                targets = ["system"]
            elif tl == "all":
                targets = ["all"]
            else:
                targets = [t.strip() for t in targets.split(',') if t.strip()]
        elif not targets:
            targets = ["system"]

        return clean_content, meta_values, targets

    except Exception:
        logger.warning(f"JSON 파싱 실패. 원본: {content[:200]}")
        return content, defaults, ["system"]
