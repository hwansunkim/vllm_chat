"""스크린플레이 형식 마크다운 내보내기.

**다른 언어 구현 위치: ``frontend/js/sim/export/markdown.js``.**
브라우저 다운로드 버튼은 JS 쪽을, ``python -m ABM.cli`` 는 이쪽을 쓴다. 두 출력이
글자 단위로 같아야 하며 ``tests/fixtures/*.md`` 골든 테스트가 파이썬 쪽을 고정한다.
포맷을 바꿀 때는 반드시 양쪽을 함께 고칠 것.

입력은 GUI의 "이력에서 내보내기"(``exportRunMarkdown``)와 같다:
  - ``config``    — ``SimStartConfig`` 를 dump 한 dict (= DB ``simulation_runs.config_json``)
  - ``shared_log``— dialogue 항목 리스트 (= ``db.get_run_log(run_id)`` / ``sim.shared_log``)
  - ``events``    — ``{event_type, wave, timestamp, data}`` (= ``db.get_run_events(run_id)``)
"""
from __future__ import annotations

import time
from datetime import datetime

from .labels import (
    AgentIndex,
    build_infection_model,
    format_day_hour,
    infection_badge,
    js_str,
    js_truthy,
    meeting_narration,
    normalize_weekday,
    sim_time_label,
)

# metaLine 전용 이모지 표. state.js 의 `_EMOTION_FACE`(아이콘 합성용)와 의도적으로
# 다른 표다 — markdown.js 상단의 EMOTION_EMOJI 를 그대로 옮긴 것.
EMOTION_EMOJI = {"happy": "😊", "angry": "😤", "sad": "😢", "fear": "😨", "neutral": "😐"}

# 내보내기 토글. GUI 체크박스 기본값과 같다(전부 켜짐).
INCLUDE_KEYS = ("time", "action", "move", "appearance", "world",
                "intervention", "infection", "meeting")
DEFAULT_INCLUDE = frozenset(INCLUDE_KEYS)

# 토글 → 스트림에 실을 이벤트 타입.
_TOGGLE_EVENT_TYPE = {
    "move":         "agent_move",
    "appearance":   "appearance_update",
    "intervention": "system_intervention",
    "world":        "world_event",
    "infection":    "infection_update",
    "meeting":      "meeting_update",
}

_STATUS_LABEL = {
    "done":    "완료 ✅",
    "stopped": "중지 ⏹",
    "running": "실행 중 ▶",
    "error":   "오류 ❌",
}


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def _fmt_ko(ts: float | None) -> str:
    """JS ``new Date(ts*1000).toLocaleString('ko-KR', {...})`` 대응.

    → ``2026년 9월 3일 오후 03:24`` (연/월/일은 0 패딩 없음, 시각은 12시간제 2자리).
    로컬 타임존 기준인 것도 JS와 같다.
    """
    if not ts:
        return "—"
    return _fmt_ko_dt(datetime.fromtimestamp(ts))


def _fmt_ko_dt(dt: datetime) -> str:
    ampm = "오전" if dt.hour < 12 else "오후"
    h12 = dt.hour % 12 or 12
    return f"{dt.year}년 {dt.month}월 {dt.day}일 {ampm} {h12:02d}:{dt.minute:02d}"


def _meta_line(meta: dict | None) -> str:
    if not meta:
        return ""
    parts = []
    # LLM 이 emotion/action 에 배열을 뱉는 로그가 실제로 있어 JS 의 문자열화·진리값
    # 규칙을 그대로 따른다(labels.js_str / js_truthy 주석 참고).
    emotion = meta.get("emotion")
    if js_truthy(emotion):
        parts.append(f"{EMOTION_EMOJI.get(js_str(emotion), '😐')} {js_str(emotion)}")
    action = meta.get("action")
    if js_truthy(action) and action != "speak":
        parts.append(f"· {js_str(action)}")
    return " ".join(parts)


def _quote(text: str) -> str:
    """``> `` 인용 블록 본문 — 줄바꿈마다 인용 접두사를 다시 붙인다."""
    return (text if text is not None else "").replace("\n", "\n> ")


# ── 로그 + 이벤트를 하나의 wave 순서 스트림으로 ────────────────────────────────

def _build_stream(log: list[dict], events: list[dict], include: frozenset) -> list[dict]:
    want = {t for key, t in _TOGGLE_EVENT_TYPE.items() if key in include}

    items: list[dict] = []
    for entry in log:
        items.append({"wave": entry.get("wave") or 0, "ts": entry.get("timestamp") or 0,
                      "kind": "dialogue", "payload": entry})
    for evt in events:
        etype = evt.get("event_type")
        if etype not in want:
            continue
        items.append({"wave": evt.get("wave") or 0, "ts": evt.get("timestamp") or 0,
                      "kind": etype, "payload": evt.get("data") or {}})

    # (wave, timestamp) 안정 정렬 — 이벤트가 대화 사이에 자연스럽게 끼어든다.
    # JS Array.prototype.sort 도 안정 정렬이라 동점 항목의 상대 순서는 삽입 순서
    # (대화 전체 → 이벤트 전체)를 따른다.
    items.sort(key=lambda it: (it["wave"], it["ts"]))
    return items


# ── 이벤트 종류별 포매터 ──────────────────────────────────────────────────────

def _fmt_dialogue(entry: dict, index: AgentIndex, include: frozenset) -> str:
    speaker = entry.get("speaker")
    meta_raw = entry.get("meta") or {}
    icon = index.icon(speaker, meta_raw.get("emotion"))
    agent = index.get(speaker)
    name = (agent.get("display_name") if agent else "") or speaker
    targets = [t for t in (entry.get("targets") or []) if t not in ("self", "system")]
    if targets:
        joined = ", ".join("전체" if t == "all" else index.label(t) for t in targets)
        target_str = f"→ *{joined}*"
    else:
        target_str = "*(독백)*"
    meta = _meta_line(entry.get("meta"))

    s = f"\n**{icon} {name}** {target_str}"
    if meta:
        s += f"  `{meta}`"
    s += "\n"
    s += f"> {_quote(entry.get('content'))}\n"
    if "action" in include and entry.get("action_note"):
        s += f"> *({entry['action_note']})*\n"
    return s


def _fmt_move(data: dict) -> str:
    name = data.get("display_name") or data.get("agent")
    if data.get("to_exterior"):
        return f"\n> **[씬]** *{name}이(가) 자리를 떴다. (→ {data.get('to')})*\n"
    frm = f"{data['from']}에서 " if data.get("from") else ""
    return f"\n> **[씬]** *{name}이(가) {frm}{data.get('to')}(으)로 이동했다.*\n"


def _fmt_appearance(data: dict) -> str:
    name = data.get("display_name") or data.get("agent")
    return f"\n> **[씬]** *{name}의 외모가 변했다: {data.get('description')}*\n"


def _fmt_intervention(data: dict) -> str:
    icon = data.get("icon") or "🎬"
    nm = data.get("display_name") or "내레이터"
    tgt = data.get("target_alias") or data.get("target") or ""
    return f"\n> **[{icon} {nm}]** → *{tgt}* : {data.get('message')}\n"


def _fmt_world_event(data: dict) -> str:
    return f"\n> **[🌍 세계 사건]** *{data.get('content')}*\n"


def _fmt_infection(data: dict, index: AgentIndex, disease_fallback: str) -> str:
    """감염 상태 변화 한 줄.

    엔진이 판정한 사실이지 등장인물이 아는 정보가 아니므로 다른 씬 이벤트와 같은
    형식으로 관전자 시점 서술처럼 적는다.
    """
    badge = infection_badge(data.get("status"), data.get("cause"))
    if not badge:
        return ""
    name = data.get("display_name") or index.label(data.get("agent"))
    # 이벤트 페이로드가 우선. 구버전/누락 시 실행 설정의 질병명으로 폴백한다.
    disease = data.get("disease_name") or disease_fallback or ""
    what = f"{disease}에" if disease else "병에"
    cause = data.get("cause")
    if cause == "recovery":
        frm = f"{disease}에서 " if disease else ""
        tail = " (면역)" if data.get("status") == "R" else " (재감염 가능)"
        text = f"{name}이(가) {frm}회복했다.{tail}"
    elif cause == "event":
        text = f"{name}이(가) {what} 감염됐다. (최초 감염자)"
    else:
        text = f"{name}이(가) {what} 감염됐다. (접촉 전파)"
    return f"\n> **[{badge['icon']} 감염]** *{text}*\n"


def _fmt_meeting(data: dict, index: AgentIndex) -> str:
    """만남 lock 한 줄. 문구는 피드 카드와 같은 ``meeting_narration`` 을 쓴다."""
    info = meeting_narration(data, index)
    if not info:
        return ""
    return f"\n> **[{info['icon']} 씬]** *{info['text']}*\n"


# ── 메인 ──────────────────────────────────────────────────────────────────────

def render_markdown(
    *,
    config: dict,
    shared_log: list[dict],
    events: list[dict],
    scenario_name: str = "",
    started_at: float | None = None,
    ended_at: float | None = None,
    status: str = "done",
    include: set[str] | frozenset[str] | None = None,
    now: float | None = None,
) -> str:
    """스크린플레이 마크다운 문자열을 만든다.

    Parameters
    ----------
    config
        ``SimStartConfig`` dump. ``agents`` / ``background`` / ``sim_start_time`` /
        ``sim_start_weekday`` / ``time_per_wave`` / ``time_mode`` / ``infection_model``
        만 읽는다. 구버전 config_json 처럼 필드가 없어도 GUI와 같은 기본값으로 채운다.
    shared_log
        dialogue 항목. ``speaker`` 키가 없는 background 항목은 여기서 걸러진다
        (GUI ``/api/simulation/logs`` 가 하던 필터와 같은 규칙).
    events
        ``{event_type, wave, timestamp, data}`` 리스트. 토글에서 꺼진 타입은 무시된다.
    started_at / ended_at
        생략하면 GUI와 똑같이 로그 첫/마지막 항목의 timestamp 로 유도한다.
        (``ended_at`` 은 로그가 2개 이상일 때만 표시된다 — JS 동작 그대로.)
    status
        ``done`` / ``stopped`` / ``running`` / ``error``. 그 외 값은 그대로 표시된다.
    include
        표시할 토글 집합. 생략하면 전부 포함.
    now
        "추출 일시" 로 찍을 시각. 생략하면 현재 시각(테스트 결정론용 훅).
    """
    inc = frozenset(include) if include is not None else DEFAULT_INCLUDE

    # background_log 항목(speaker 없음)은 내보내기 대상이 아니다.
    log = [e for e in (shared_log or []) if "speaker" in e]

    raw_agents = config.get("agents") or []
    agents = [{"icon": "🤖", "groups": [], "initial_active": True, **a} for a in raw_agents]
    index = AgentIndex(agents)

    background = config.get("background") or ""
    scenario_name = scenario_name or "시나리오"
    sim_start_time = config.get("sim_start_time") or "09:00"
    sim_start_weekday = normalize_weekday(config.get("sim_start_weekday"))
    time_per_wave = config.get("time_per_wave")
    time_per_wave = 30 if time_per_wave is None else time_per_wave
    time_mode = config.get("time_mode") or "fixed"
    infection = build_infection_model(config.get("infection_model"))

    now_ko = _fmt_ko_dt(datetime.fromtimestamp(now if now is not None else time.time()))

    start_ts = started_at if started_at is not None else (log[0].get("timestamp") if log else None)
    if ended_at is not None:
        end_ts = ended_at
    else:
        end_ts = log[-1].get("timestamp") if len(log) > 1 else None
    max_wave = max((e.get("wave") or 0) for e in log) if log else 0
    status_label = _STATUS_LABEL.get(status, status or "—")

    md = ""
    md += f"# {scenario_name}\n\n"
    md += f"> **추출 일시** {now_ko}\n"
    if start_ts:
        md += f"> **시작** {_fmt_ko(start_ts)}\n"
    if end_ts:
        md += f"> **종료** {_fmt_ko(end_ts)}\n"
    if max_wave > 0:
        md += f"> **Wave** {max_wave} · **총 턴** {len(log)}\n"
    md += f"> **상태** {status_label}\n"
    md += "\n---\n\n"

    md += "## 등장인물\n\n"
    md += "| 아이콘 | 이름 | ID | 그룹 | 초기 활성 |\n"
    md += "|--------|------|----|------|-----------|\n"
    for a in agents:
        groups = ", ".join(a.get("groups") or []) or "—"
        active = "✅" if a.get("initial_active") is not False else "—"
        md += f"| {a.get('icon') or '🤖'} | {a.get('display_name') or a.get('name')} | `{a.get('name')}` | {groups} | {active} |\n"
    md += "\n"

    if background:
        md += f"## 배경\n\n{background}\n\n---\n\n"

    # 감염병 모델이 켜져 있을 때만 — 꺼진 실행에는 아무 영향이 없는 설정이라 노이즈다.
    if infection["enabled"]:
        md += "## 🦠 감염병 모델\n\n"
        md += f"> **질병** {infection['disease_name'] or '(이름 없음)'}\n"
        recovery = ("자연 회복 없음(만성)" if infection["recovery_max_minutes"] == 0
                    else f"{format_day_hour(infection['recovery_min_minutes'])} ~ "
                         f"{format_day_hour(infection['recovery_max_minutes'])}")
        md += (f"> **전염 확률** {_js_num(infection['transmission_probability'])} · "
               f"**회복까지** {recovery}\n")
        md += f"> **회복 후** {'면역 획득 (SIR)' if infection['immune_after_recovery'] else '재감염 가능 (SIS)'}\n\n"
        if infection["symptom_stages"]:
            md += "| 단계 | 감염 후 경과 시간 | 증상 서사 |\n|------|------------------|-----------|\n"
            for s in infection["symptom_stages"]:
                text = (s.get("symptom_text") or "").replace("|", "\\|").replace("\n", " ")
                md += (f"| {s['label']} | {format_day_hour(s['min_minutes'])} ~ "
                       f"{format_day_hour(s['max_minutes'])} | {text} |\n")
            md += "\n"
        md += "---\n\n"

    md += "## 대화 기록\n\n"
    if not log:
        md += "*대화 기록이 없습니다.*\n\n"
        return md

    # wave의 첫 병합 항목이 time_str이 없는 이벤트(agent_move 등)일 수 있으므로,
    # dialogue 로그 전체에서 wave별 time_str을 먼저 찾아둔다 (첫 항목만 보면 놓칠 수 있음).
    wave_time_map: dict[int, str] = {}
    for entry in log:
        w = entry.get("wave")
        if w is not None and w not in wave_time_map and entry.get("time_str"):
            wave_time_map[w] = entry["time_str"]

    disease_fallback = infection["disease_name"]
    cur_wave = None
    for item in _build_stream(log, events or [], inc):
        if item["wave"] != cur_wave:
            cur_wave = item["wave"]
            # 서버가 기록해둔 time_str(정확값)을 우선 사용하고, 없으면(구버전 로그 등)
            # fixed 공식으로 폴백.
            time_label = None
            if "time" in inc:
                time_label = wave_time_map.get(cur_wave) or sim_time_label(
                    cur_wave,
                    time_mode=time_mode,
                    time_per_wave=time_per_wave,
                    sim_start_time=sim_start_time,
                    sim_start_weekday=sim_start_weekday,
                )
            wave_head = (f"### 🕐 {time_label}  ·  Wave {cur_wave}" if time_label
                         else f"### 🌊 Wave {cur_wave}")
            md += f"\n{wave_head}\n\n---\n"

        kind, payload = item["kind"], item["payload"]
        if kind == "dialogue":
            md += _fmt_dialogue(payload, index, inc)
        elif kind == "agent_move":
            md += _fmt_move(payload)
        elif kind == "appearance_update":
            md += _fmt_appearance(payload)
        elif kind == "system_intervention":
            md += _fmt_intervention(payload)
        elif kind == "world_event":
            md += _fmt_world_event(payload)
        elif kind == "infection_update":
            md += _fmt_infection(payload, index, disease_fallback)
        elif kind == "meeting_update":
            md += _fmt_meeting(payload, index)
    md += "\n"
    return md


def _js_num(v) -> str:
    """JS 템플릿 리터럴의 숫자 표기 (``0.3`` → ``0.3``, ``1`` → ``1``)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
