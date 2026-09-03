"""``frontend/js/sim/state.js`` 의 순수 헬퍼 파이썬 포팅.

**다른 언어 구현 위치: ``frontend/js/sim/state.js``.**
두 구현은 같은 문자열을 내야 한다 — 마크다운 내보내기가 양쪽에서 각각 돌기
때문이다(브라우저 다운로드 vs ``python -m ABM.cli``). 한쪽만 고치면
``tests/fixtures/*.md`` 골든 테스트가 깨진다.

포팅 대상: ``normalizeWeekday`` · ``normalizeDurationMinutes`` ·
``normalizeProbability`` · ``normalizeSymptomStages`` · ``buildInfectionModel`` ·
``formatDayHour`` · ``infectionBadge`` · ``meetingNarration`` · ``detectGender`` ·
``getAgentIcon`` · ``agentLabel`` · ``simTimeLabel``.

JS 의 ``parseFloat`` / ``Math.round`` 의미를 그대로 흉내 낸다(``_parse_float`` ·
``_js_round``) — 값이 폼에서 문자열로 흘러 들어오는 경로가 있어 "숫자로 안 읽히면
기본값" 규칙이 눈에 보이는 차이를 만든다.
"""
from __future__ import annotations

import math
import re

from ..simulation._constants import _WEEKDAY_KEYS, _WEEKDAY_LABELS

# ── 요일 ──────────────────────────────────────────────────────────────────────

WEEKDAY_KEYS: tuple[str, ...] = _WEEKDAY_KEYS
WEEKDAY_LABELS: dict[str, str] = dict(zip(_WEEKDAY_KEYS, _WEEKDAY_LABELS))
DEFAULT_START_WEEKDAY = "mon"


def normalize_weekday(v) -> str:
    """임의의 입력을 유효한 요일 코드로 정규화 (알 수 없으면 'mon')."""
    k = str(v if v is not None else "").lower()
    return k if k in WEEKDAY_KEYS else DEFAULT_START_WEEKDAY


# ── JS 숫자 의미 ──────────────────────────────────────────────────────────────

_LEADING_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _parse_float(v):
    """JS ``parseFloat`` 대응. 숫자로 읽히지 않으면 None(=NaN)."""
    if isinstance(v, bool):
        return None                      # JS: parseFloat(true) === NaN
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    m = _LEADING_NUMBER.match(str(v if v is not None else "").strip())
    if not m:
        return None
    try:
        n = float(m.group(0))
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _js_round(n: float) -> int:
    """JS ``Math.round`` (half-up, 0.5 는 위로). 파이썬 내장 round 는 뱅커스 반올림."""
    return math.floor(n + 0.5)


def js_truthy(v) -> bool:
    """JS 진리값. 파이썬과 갈리는 지점은 **빈 리스트/빈 dict** (JS 에서는 참)."""
    if v is None or v is False:
        return False
    if isinstance(v, str):
        return v != ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0 and not (isinstance(v, float) and math.isnan(v))
    return True    # 배열·객체는 비어 있어도 truthy


def js_str(v) -> str:
    """JS 템플릿 리터럴/객체 키의 문자열화.

    LLM 이 ``emotion`` 같은 필드에 배열을 뱉는 실제 로그가 있어서 필요하다 —
    JS 는 ``${['a','b']}`` 를 ``"a,b"`` 로 만들지만 파이썬 ``str()`` 은
    ``"['a', 'b']"`` 라 그대로 두면 두 구현의 출력이 갈린다.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return ",".join("" if x is None else js_str(x) for x in v)
    if isinstance(v, dict):
        return "[object Object]"
    return str(v)


# ── 분 단위 입력 ──────────────────────────────────────────────────────────────

MINUTES_PER_DAY = 1440
MINUTES_PER_HOUR = 60
MAX_TARGET_DURATION_MINUTES = 52560000   # 100년


def normalize_duration_minutes(v, fallback: int = 0) -> int:
    n = _parse_float(v)
    if n is None:
        return fallback
    return min(MAX_TARGET_DURATION_MINUTES, max(0, _js_round(n)))


def format_day_hour(minutes) -> str:
    """분 → "2일 12시간" 사람이 읽는 문자열 (0분은 "0시간")."""
    m = normalize_duration_minutes(minutes)
    days = m // MINUTES_PER_DAY
    hours = (m % MINUTES_PER_DAY) // MINUTES_PER_HOUR
    if not days and not hours:
        return "0시간"
    return " ".join(p for p in (f"{days}일" if days else "", f"{hours}시간" if hours else "") if p)


def normalize_probability(v, fallback: float = 0) -> float:
    n = _parse_float(v)
    if n is None:
        return fallback
    return min(1, max(0, _js_round(n * 100) / 100))


# ── 감염병 모델 ───────────────────────────────────────────────────────────────

DEFAULT_TRANSMISSION_PROBABILITY = 0.3
DEFAULT_RECOVERY_MIN_MINUTES = 7200    # 5일
DEFAULT_RECOVERY_MAX_MINUTES = 14400   # 10일

DEFAULT_SYMPTOM_STAGES: list[dict] = [
    {"id": "incubation", "label": "잠복기", "min_minutes": 0,    "max_minutes": 2880,
     "symptom_text": "목이 조금 칼칼하다. 피곤해서 그런 거겠지, 별일 아닐 것이다."},
    {"id": "onset",      "label": "발현기", "min_minutes": 2880, "max_minutes": 7200,
     "symptom_text": "몸이 으슬으슬하고 기침이 멎지 않는다. 이마가 뜨겁다."},
    {"id": "acute",      "label": "급성기", "min_minutes": 7200, "max_minutes": 20160,
     "symptom_text": "고열로 눈앞이 흐리다. 온몸이 쑤시고 서 있기조차 버겁다."},
]


def normalize_symptom_stages(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "").strip() or f"stage{i + 1}"
        if sid in seen:
            sid = f"{sid}_{i + 1}"
        seen.add(sid)
        lo = normalize_duration_minutes(item.get("min_minutes"), 0)
        hi = normalize_duration_minutes(item.get("max_minutes"), lo)
        # min 을 max 쪽으로 낮춘다(그 반대가 아니라) — state.js 와 같은 규칙.
        if hi < lo:
            lo = hi
        out.append({
            "id":           sid,
            "label":        str(item.get("label") or "").strip() or sid,
            "min_minutes":  lo,
            "max_minutes":  hi,
            "symptom_text": str(item.get("symptom_text") or ""),
        })
    return out


def build_infection_model(raw) -> dict:
    """임의의 입력을 백엔드 ``InfectionModelConfig`` 모양으로 정규화.

    ``raw`` 가 dict 가 아니면(구버전 config_json 에는 필드 자체가 없다) 기본값을
    채운 "꺼진 모델"을 돌려준다.
    """
    src = raw if isinstance(raw, dict) else None
    rec_min = normalize_duration_minutes(
        src.get("recovery_min_minutes") if src else None, DEFAULT_RECOVERY_MIN_MINUTES)
    rec_max = normalize_duration_minutes(
        src.get("recovery_max_minutes") if src else None, DEFAULT_RECOVERY_MAX_MINUTES)
    # max == 0 은 "자연 회복 없음(만성)" 이라는 별도 의미 — 대소 검증에서 제외.
    if rec_max > 0 and rec_max < rec_min:
        rec_min = rec_max
    stages_raw = src.get("symptom_stages") if src else None
    # JS 의 `src?.immune_after_recovery ?? true` — 값이 없을 때만 true 로 채운다
    # (명시적 false 는 그대로 살린다).
    immune = src.get("immune_after_recovery") if src else None
    return {
        "enabled":                  bool(src.get("enabled")) if src else False,
        "disease_name":             str((src.get("disease_name") if src else "") or "").strip(),
        "transmission_probability": normalize_probability(
            src.get("transmission_probability") if src else None,
            DEFAULT_TRANSMISSION_PROBABILITY),
        # 감염 설정 자체가 없던 시나리오만 기본 단계로 채운다.
        "symptom_stages":           normalize_symptom_stages(stages_raw)
                                    if (src is not None and isinstance(stages_raw, list))
                                    else [dict(s) for s in DEFAULT_SYMPTOM_STAGES],
        "recovery_min_minutes":     rec_min,
        "recovery_max_minutes":     rec_max,
        "immune_after_recovery":    True if immune is None else immune,
    }


def infection_badge(status, cause) -> dict | None:
    """``infection_update`` 이벤트 → 표시 뱃지. 표시할 게 없으면 None.

    ``status='S'`` 는 "한 번도 안 걸림"과 "회복했지만 재감염 가능(SIS)" 두 뜻이라
    cause 로 구분한다 — 전자는 뱃지를 달지 않는다.
    """
    if status == "I":
        return {"icon": "🦠", "label": "감염",     "cls": "infected"}
    if status == "R":
        return {"icon": "💚", "label": "회복·면역", "cls": "recovered"}
    if status == "S" and cause == "recovery":
        return {"icon": "💚", "label": "회복",     "cls": "recovered"}
    return None


# ── 에이전트 표시 ─────────────────────────────────────────────────────────────

_GENDER_BASE = {"male": "👨", "female": "👩", "unknown": "🧑"}

_EMOTION_FACE = {
    "happy":        "😊",
    "sad":          "😢",
    "angry":        "😠",
    "fear":         "😨",
    "surprised":    "😲",
    "excited":      "😄",
    "calm":         "😌",
    "worried":      "😟",
    "anxious":      "😰",
    "embarrassed":  "😳",
    "disappointed": "😞",
    "frustrated":   "😤",
    "confused":     "🤔",
    "proud":        "😎",
}

_MALE_KW = ["남성", "남자", "남편", "아들", "아버지", "아빠", "형", "오빠", "삼촌",
            "할아버지", "소년", "남학생", "남동생", "사내", "남성형", "그는"]
_FEMALE_KW = ["여성", "여자", "아내", "딸", "어머니", "엄마", "언니", "누나", "이모",
              "할머니", "소녀", "여학생", "여동생", "아가씨", "여인", "그녀는", "그녀의"]


def detect_gender(text: str) -> str:
    if not text:
        return "unknown"
    m = sum(1 for k in _MALE_KW if k in text)
    f = sum(1 for k in _FEMALE_KW if k in text)
    if m > f:
        return "male"
    if f > m:
        return "female"
    return "unknown"


def get_agent_icon(agent: dict, emotion: str | None = None) -> str:
    icon = agent.get("icon")
    if icon and icon != "🤖":
        return icon
    g = agent.get("gender")
    if g == "auto" or not js_truthy(g):
        g = detect_gender(f"{agent.get('system_prompt') or ''} {agent.get('display_name') or ''}")
    base = _GENDER_BASE.get(g) or "🧑"
    # 키 조회는 JS 의 객체 프로퍼티 접근과 같이 문자열로 강제 변환한다 — LLM 이
    # emotion 에 배열을 뱉은 로그가 실제로 있고, JS 는 그걸 "a,b" 키로 찾아 miss 한다.
    face = _EMOTION_FACE.get(js_str(emotion) if js_truthy(emotion) else "neutral")
    return base + face if face else base


class AgentIndex:
    """``sim.agents`` 배열에 대한 이름 조회 — state.js 의 ``agentLabel``/``getAgentIcon``.

    JS 는 전역 ``sim.agents`` 를 직접 뒤지지만 여기서는 config 에서 만든 리스트를
    감싼다. 조회 실패 시 동작(키를 그대로 반환)까지 같다.
    """

    def __init__(self, agents: list[dict]):
        self.agents = agents
        self._by_name = {a.get("name"): a for a in agents}

    def get(self, key: str) -> dict | None:
        return self._by_name.get(key)

    def label(self, key: str) -> str:
        a = self._by_name.get(key)
        if a and a.get("display_name"):
            return a["display_name"]
        return a.get("name") if a else key

    def icon(self, key: str, emotion: str | None = None) -> str:
        return get_agent_icon(self._by_name.get(key) or {"name": key}, emotion)


# ── 시뮬레이션 시각 (fixed 모드 폴백) ─────────────────────────────────────────

def sim_time_label(
    wave_num: int,
    *,
    time_mode: str = "fixed",
    time_per_wave=30,
    sim_start_time: str = "09:00",
    sim_start_weekday: str = "mon",
) -> str | None:
    """``time_str`` 이 없는 구버전 로그용 폴백. variable 모드/시간 OFF 면 None.

    반환 포맷은 엔진의 ``Simulation._format_time_str`` 과 같다:
    ``{요일} {오전|오후} {시}시 {분:02d}분``.
    """
    if time_mode == "variable":
        return None
    tpw = 30 if time_per_wave is None else time_per_wave
    if not tpw:
        return None
    parts = str(sim_start_time or "09:00").split(":")
    try:
        h = int(float(parts[0]))
    except (ValueError, IndexError):
        h = 0
    try:
        m = int(float(parts[1]))
    except (ValueError, IndexError):
        m = 0
    start_min = h * 60 + m
    total_min = start_min + wave_num * int(tpw)
    day_offset = math.floor(total_min / MINUTES_PER_DAY)
    total = total_min % MINUTES_PER_DAY
    start_idx = WEEKDAY_KEYS.index(normalize_weekday(sim_start_weekday))
    wd = WEEKDAY_LABELS[WEEKDAY_KEYS[(start_idx + day_offset) % 7]]
    hour, minute = divmod(total, 60)
    if hour < 12:
        return f"{wd} 오전 {hour}시 {minute:02d}분"
    display_hour = 12 if hour == 12 else hour - 12
    return f"{wd} 오후 {display_hour}시 {minute:02d}분"


# ── 만남 서술 ─────────────────────────────────────────────────────────────────

def meeting_narration(d: dict, index: AgentIndex) -> dict | None:
    """``meeting_update`` 이벤트 → 관전자 시점 한 줄. 표시할 게 없으면 None.

    ``target_name`` 은 chaser 의 인지 상태에 따라 실명일 수도 ``낯선 이(ID: …)`` 일
    수도 있어 그대로 쓴다. 모르는 status 는 None → 조용히 무시된다.
    """
    if not d or not d.get("chaser"):
        return None
    chaser = d.get("chaser_name") or index.label(d["chaser"])
    target = d.get("target_name") or (index.label(d["target"]) if d.get("target") else "")
    if not target:
        return None

    status = d.get("status")
    if status == "start":
        where = f" ({d['target_location']})" if d.get("target_location") else ""
        return {"icon": "🏃", "cls": "start", "text": f"{chaser}가 {target}를 만나러 이동 중{where}"}
    if status == "arrived":
        return {"icon": "🤝", "cls": "arrived", "text": f"{chaser}가 {target}와 만났다"}
    if status == "cancelled":
        if d.get("reason") == "gone":
            return {"icon": "💨", "cls": "cancelled",
                    "text": f"{chaser}가 {target}를 찾았지만 자리를 뜬 뒤였다"}
        return {"icon": "↩️", "cls": "cancelled",
                "text": f"{chaser}가 {target}를 만나려던 것을 그만뒀다"}
    return None
