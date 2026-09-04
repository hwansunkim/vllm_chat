import re
from difflib import SequenceMatcher

# 시작 요일 키(프론트/스키마와 동일) → 표시 라벨. 인덱스 = 월요일 기준 0~6.
# core.py 의 `_format_time_str` 과 마크다운 내보내기(ABM/export/labels.py)가 같은
# 라벨을 써야 해서 엔진 import 없이 읽을 수 있는 이 모듈에 둔다.
_WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_LABELS: tuple[str, ...] = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")

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
    """최근 발언 목록에서 최대 쌍별 유사도(0–1) 반환.

    `SequenceMatcher` 로 원문을 글자 단위 비교한다 — **거의 똑같은 문장을 다시
    내뱉는** 축자 반복만 잡힌다. 표현을 조금씩 바꿔가며 같은 화제·같은 욕구를
    맴도는 "주제 반복"은 어휘 일치율이 0.5 언저리라 이 지표로는 안 잡힌다
    (여러 지표로 캘리브레이션했으나 이 시나리오의 정상 대화와 분리 불가).
    주제 반복은 디렉터가 `[최근 활동]`(`_recent_activity_digest`)을 직접 읽고
    판단하게 한다 — 어휘로 못 하는 의미 판정을, 그걸 할 수 있는 LLM 이 한다.
    """
    if len(texts) < 2:
        return 0.0
    best = 0.0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            s = SequenceMatcher(None, texts[i], texts[j]).ratio()
            if s > best:
                best = s
    return best


_DIRECTOR_DIGEST_WAVES = 6    # 디렉터 최근 활동 창 기본값 (system_agent.digest_waves 로 조절)
_DIGEST_TURN_MAXLEN    = 70   # 다이제스트 한 줄당 발화 자르는 길이
_DIGEST_MAX_LINES      = 120  # 총 라인 하드캡 — digest_waves 를 크게 잡아도 프롬프트 폭주 방지


def _recent_activity_digest(
    shared_log: list[dict],
    key_to_alias: dict | None = None,
    *,
    waves: int = _DIRECTOR_DIGEST_WAVES,
    max_lines: int = _DIGEST_MAX_LINES,
) -> str:
    """디렉터용 최근 활동 다이제스트 — 마지막 `waves` wave의 발화를 wave별로 나열.

    디렉터가 개입을 판단하는 **주된 근거**다. `_repetition_score`(축자 반복)로는
    못 잡는 주제 반복("표현만 바꿔 같은 화제를 맴돎")을 디렉터가 직접 읽고
    판단할 수 있게 한다.

    digest는 압축이 아니라 원문이라 `waves`에 비례해 토큰이 늘어난다. `max_lines`
    로 총 길이를 캡하고, 초과 시 **오래된 wave부터** 버린다(최근이 더 중요).
    """
    alias = key_to_alias or {}
    entries = [e for e in shared_log if isinstance(e.get("wave"), int)]
    if not entries:
        return ""
    cutoff = max(e["wave"] for e in entries) - waves + 1
    lines: list[str] = []
    cur: int | None = None
    for e in entries:
        w = e["wave"]
        if w < cutoff:
            continue
        if w != cur:
            lines.append(f"— Wave {w} —")
            cur = w
        spk = alias.get(e.get("speaker"), e.get("speaker") or "?")
        text = (e.get("content") or "").strip()
        if not text or _FILLER_RE.match(text):
            act = (e.get("action_note") or "말없이 행동").strip()
            text = f"({act[:_DIGEST_TURN_MAXLEN]})"
        else:
            text = text[:_DIGEST_TURN_MAXLEN]
        lines.append(f"  {spk}: {text}")
    if len(lines) > max_lines:
        # 뒤(최근)에서 max_lines 만큼 남기되, 잘린 첫 줄이 wave 헤더가 아니면
        # 맥락용으로 헤더 한 줄을 앞에 붙인다.
        kept = lines[-max_lines:]
        if not kept[0].startswith("— Wave "):
            kept.insert(0, "— (이전 wave 생략) —")
        lines = kept
    return "\n".join(lines)


_COMPRESSION_THRESHOLD = 0.70   # trigger compression at this fraction of token_limit
_COMPRESSION_MIN_MSGS  = 4      # don't compress until agent has at least this many memory entries
