from __future__ import annotations

import json
import re


def parse_json(text: str, *, array: bool = False) -> list | dict | None:
    """JSON 블록 또는 인라인 JSON을 파싱한다. array=True면 빈 리스트를 기본값으로 반환."""
    open_ch, close_ch = ('[', ']') if array else ('{', '}')
    fallback: list | None = [] if array else None

    fence = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass

    start = text.find(open_ch)
    if start == -1:
        return fallback
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return fallback
    return fallback
