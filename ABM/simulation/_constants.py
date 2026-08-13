import re
from difflib import SequenceMatcher

# CJK Unified (U+4E00-U+9FFF) / Extension-A (U+3400-U+4DBF) / Compatibility Ideographs (U+F900-U+FAFF).
# 코드포인트 이스케이프로 명시: 리터럴 한자를 쓰면 육안으로 구별 안 되는 호환 문자(예:
# U+F900 대 정준 형태 U+8C48)가 복붙 과정에서 섞여 들어가 범위가 어긋나기 쉽다 —
# 예전에 U+8C48이 섞여 들어가 U+8C48-U+FAFF로 범위가 벌어지면서 한글 음절
# (U+AC00-U+D7A3)까지 통째로 포함돼 정상 한국어가 외국어로 오탐된 적이 있다.
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')


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
