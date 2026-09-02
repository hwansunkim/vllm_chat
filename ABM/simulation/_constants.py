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

# 순수 말줄임/공백/대시만으로 이뤄진 문자열 — 발화가 아니라 "말 안 함"의 표기다.
# 과묵한 캐릭터는 매 턴 content="..."를 내는데, 이걸 그대로 비교하면 유사도 100%로
# 반복 에이전트로 오탐돼 디렉터가 끊임없이 개입한다.
_FILLER_RE = re.compile(r'^[\s.．。…·ㆍ\-–—]*$')


def _normalize_utterance(content: str, action_note: str = "") -> str | None:
    """반복 비교용 정규화 — 이 턴에 에이전트가 '표현한 것'.

    content가 실제 대사면 그것, 순수 필러("...")면 action_note(행동 묘사)로 폴백.
    둘 다 비어 있거나 필러면 None(= 비교 대상 아님)을 돌려준다. 그러면
    `_repetition_score`가 유효 항목 2개 미만일 때 0.0을 반환해 오탐이 사라진다.
    """
    for t in ((content or "").strip(), (action_note or "").strip()):
        if t and not _FILLER_RE.match(t):
            return t
    return None


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
