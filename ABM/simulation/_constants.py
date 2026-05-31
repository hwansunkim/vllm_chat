import re
from difflib import SequenceMatcher

# CJK Unified / Extension-A / Compatibility Ideographs
_CJK_RE = re.compile(r'[一-鿿㐀-䶿豈-﫿]')


def _has_foreign_chars(text: str) -> bool:
    return bool(_CJK_RE.search(text or ''))


_REPEAT_WINDOW    = 4      # 최근 N발언 비교
_REPEAT_THRESHOLD = 0.65   # 유사도 이 이상이면 반복으로 판단
_MEMO_MAX_LINES   = 12     # director_memo 최대 보존 줄 수


def _repetition_score(texts: list[str]) -> float:
    """최근 발언 목록에서 최대 쌍별 유사도(0–1) 반환."""
    if len(texts) < 2:
        return 0.0
    best = 0.0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            s = SequenceMatcher(None, texts[i], texts[j]).ratio()
            if s > best:
                best = s
    return best


_COMPRESSION_THRESHOLD = 0.70   # trigger compression at this fraction of token_limit
_COMPRESSION_MIN_MSGS  = 4      # don't compress until agent has at least this many memory entries
