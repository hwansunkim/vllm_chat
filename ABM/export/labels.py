"""``frontend/js/sim/state.js`` 의 순수 헬퍼 파이썬 포팅.

**다른 언어 구현 위치: ``frontend/js/sim/state.js``.**
두 구현은 같은 문자열을 내야 한다 — 마크다운 내보내기가 양쪽에서 각각 돌기
때문이다(브라우저 다운로드 vs ``python -m ABM.cli``). 한쪽만 고치면
``tests/fixtures/*.md`` 골든 테스트가 깨진다.

포팅 대상: ``normalizeWeekday`` ·
``normalizeProbability`` · ``normalizeBeta`` · ``normalizeSymptomStages`` ·
``normalizeDurationSpec`` · ``buildInfectionModel`` · ``infectionBadge`` ·
``meetingNarration`` · ``detectGender`` · ``getAgentIcon`` · ``agentLabel`` ·
``simTimeLabel``.

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


# ── 분 단위 상수 ──────────────────────────────────────────────────────────────
# (옛 증상 단계 "일 + 시간" 입력용 normalize_duration_minutes/format_day_hour는 증상 문구가
#  상태 기반으로 바뀌며 JS·파이썬 양쪽에서 쓰이지 않게 돼 제거했다.)

MINUTES_PER_DAY = 1440


def normalize_probability(v, fallback: float = 0) -> float:
    n = _parse_float(v)
    if n is None:
        return fallback
    return min(1, max(0, _js_round(n * 100) / 100))


def normalize_beta(v, fallback: float = 0) -> float:
    """β(전염 확률 계수) 정규화 — 확률이 아니라 비율(rate)이라 1을 넘을 수 있다.
    0 이상이기만 하면 된다. state.js의 normalizeBeta와 동일 규칙(4자리 반올림)."""
    n = _parse_float(v)
    if n is None:
        return fallback
    return max(0, _js_round(n * 10000) / 10000)


# ── 감염병 모델 (SEPIR) ───────────────────────────────────────────────────────

DEFAULT_BETA = 0.04

# E/P/I 지속 시간 분포(DurationSpec, 일 단위) — 백엔드 스키마 기본값과 1:1.
# state.js 의 DEFAULT_*_DURATION 과 같은 값이어야 한다.
MAX_DURATION_DAYS = 36500
DURATION_KINDS = ("uniform", "gamma", "gaussian")
_DURATION_BASE = {"kind": "uniform", "shape": 2.0, "scale": 1.0, "mean": 5.0,
                  "stddev": 2.0, "min_days": 1.0, "max_days": 10.0}
DEFAULT_EXPOSED_DURATION = {**_DURATION_BASE, "kind": "gamma", "shape": 1.926,
                            "scale": 1.775, "min_days": 1.0, "max_days": 10.0}
DEFAULT_PRESYMPTOMATIC_DURATION = {**_DURATION_BASE, "min_days": 0.0, "max_days": 0.0}
DEFAULT_INFECTIOUS_DURATION = {**_DURATION_BASE, "min_days": 8.0, "max_days": 8.0}


def _round4(n: float):
    return _js_round(n * 10000) / 10000


def normalize_duration_spec(raw, default: dict) -> dict:
    """DurationSpec 정규화 — state.js ``normalizeDurationSpec`` 과 동일 규칙.

    kind 가 알 수 없으면 기본 kind, 양수여야 하는 파라미터(shape/scale/stddev)가
    0 이하·비숫자면 기본값, min 은 0~36500, max 가 min 보다 작으면 min 을 max 로
    낮춘다(증상 단계와 같은 규칙).
    """
    src = raw if isinstance(raw, dict) else {}
    kind = src.get("kind") if src.get("kind") in DURATION_KINDS else default["kind"]

    def _pos(key):
        n = _parse_float(src.get(key))
        return _round4(n) if (n is not None and n > 0) else default[key]

    def _any(key):
        n = _parse_float(src.get(key))
        return _round4(n) if n is not None else default[key]

    def _days(key, fallback):
        n = _parse_float(src.get(key))
        if n is None:
            return fallback
        return min(MAX_DURATION_DAYS, max(0, _round4(n)))

    lo = _days("min_days", default["min_days"])
    hi = _days("max_days", default["max_days"])
    if hi < lo:
        lo = hi
    return {"kind": kind, "shape": _pos("shape"), "scale": _pos("scale"),
            "mean": _any("mean"), "stddev": _pos("stddev"),
            "min_days": lo, "max_days": hi}

# 증상 문구 — 상태(E/P/I)별. 같은 status 항목은 등록 순서가 곧 그 상태 안의 진행
# 순서다(엔진이 그 상태의 진행률로 균등 분할). state.js 의 DEFAULT_SYMPTOM_STAGES 와 같아야 한다.
SYMPTOM_STATUSES = ("E", "P", "I")
SYMPTOM_STATUS_LABELS = {"E": "잠복기(E)", "P": "무증상 전염기(P)", "I": "감염기(I)"}

DEFAULT_SYMPTOM_STAGES: list[dict] = [
    {"status": "E", "symptom_text": "목이 조금 칼칼하고 살짝 피곤하다. 별일 아니겠지 싶은 정도다."},
    {"status": "P", "symptom_text": "특별히 아픈 데는 없지만 왠지 몸이 무겁게 느껴진다."},
    {"status": "I", "symptom_text": "열이 나고 기침이 멎지 않는다. 코가 막히고 목이 따갑다. 냄새와 맛이 잘 안 느껴진다."},
    {"status": "I", "symptom_text": "고열로 눈앞이 흐리다. 온몸이 쑤시고 기침이 심해 숨쉬기도 버겁다. 서 있기조차 힘들다."},
]

MODEL_TYPES = ("sir", "seir", "sepir")


def normalize_symptom_stages(raw) -> list[dict]:
    """state.js ``normalizeSymptomStages`` 와 동일 규칙.

    status 가 E/P/I 가 아닌 항목(구버전 min/max 형식 포함)은 버리고, E→P→I 순으로
    **안정 정렬**한다 — 같은 status 안의 상대 순서(= 진행 순서)는 그대로라 엔진 결과는
    바뀌지 않고, 편집 화면·내보내기가 상태별로 묶여 보인다.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("status") not in SYMPTOM_STATUSES:
            continue
        out.append({"status": item["status"], "symptom_text": str(item.get("symptom_text") or "")})
    out.sort(key=lambda st: SYMPTOM_STATUSES.index(st["status"]))
    return out


def build_infection_model(raw) -> dict:
    """임의의 입력을 백엔드 ``InfectionModelConfig`` 모양으로 정규화.

    ``raw`` 가 dict 가 아니면(구버전 config_json 에는 필드 자체가 없다) 기본값을
    채운 "꺼진 모델"을 돌려준다.
    """
    src = raw if isinstance(raw, dict) else None
    stages_raw = src.get("symptom_stages") if src else None
    # JS 의 `src?.immune_after_recovery ?? true` — 값이 없을 때만 true 로 채운다
    # (명시적 false 는 그대로 살린다).
    immune = src.get("immune_after_recovery") if src else None
    return {
        "enabled":                  bool(src.get("enabled")) if src else False,
        "disease_name":             str((src.get("disease_name") if src else "") or "").strip(),
        "beta":                     normalize_beta(
            src.get("beta") if src else None, DEFAULT_BETA),
        # 감염 설정 자체가 없던 시나리오만 기본 단계로 채운다.
        "symptom_stages":           normalize_symptom_stages(stages_raw)
                                    if (src is not None and isinstance(stages_raw, list))
                                    else [dict(s) for s in DEFAULT_SYMPTOM_STAGES],
        "immune_after_recovery":    True if immune is None else immune,
        "model_type":               (src.get("model_type") if src and src.get("model_type") in MODEL_TYPES
                                     else "sepir"),
        "exposed_duration":         normalize_duration_spec(
            src.get("exposed_duration") if src else None, DEFAULT_EXPOSED_DURATION),
        "presymptomatic_duration":  normalize_duration_spec(
            src.get("presymptomatic_duration") if src else None, DEFAULT_PRESYMPTOMATIC_DURATION),
        "infectious_duration":      normalize_duration_spec(
            src.get("infectious_duration") if src else None, DEFAULT_INFECTIOUS_DURATION),
    }


def infection_badge(status, cause) -> dict | None:
    """``infection_update`` 이벤트 → 표시 뱃지. 표시할 게 없으면 None.

    ``status='S'`` 는 "한 번도 안 걸림"과 "회복했지만 재감염 가능(SEIRS)" 두 뜻이라
    cause 로 구분한다 — 전자는 뱃지를 달지 않는다.
    """
    if status == "E":
        return {"icon": "⏳", "label": "잠복기",   "cls": "exposed"}
    if status == "P":
        return {"icon": "😶", "label": "무증상 전염기", "cls": "presymptomatic"}
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
