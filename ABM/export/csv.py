"""위치 이력 CSV 내보내기 — 감염병 접촉 분석용(누가 언제 어느 장소에 있었는가).

**다른 언어 구현 위치: ``frontend/js/sim/export/csv.js``.**
브라우저 다운로드 버튼은 JS 쪽(``buildLocationCsv``)을, ``python -m ABM.cli
--format csv`` 는 이쪽을 쓴다. 마크다운 내보내기와 같은 원칙으로 두 출력이
같아야 하므로 포맷을 바꿀 때는 반드시 양쪽을 함께 고칠 것.

입력은 마크다운 내보내기(``ABM/export/markdown.py``)와 같은 두 덩어리다:
  - ``shared_log`` — dialogue 항목 (= ``db.get_run_log(run_id)`` / ``sim.shared_log``)
  - ``events``     — ``{event_type, wave, timestamp, data}`` (= ``db.get_run_events(run_id)``)

**DB를 읽지 않는다.** ``run_config()`` 가 돌려주는 ``RunResult.shared_log`` /
``RunResult.events`` 는 DB 저장 여부와 무관하게 항상 메모리에 채워지므로, 이
함수는 ``--no-db`` 실행에서도 그대로 동작한다(``render_markdown()`` 과 같은 이유).

## 명단 형식 (2026-10 task B — ``wave_roster`` 이벤트가 있는 run)

엔진이 wave 마다 남기는 ``wave_roster`` 이벤트(``infection.py::_wave_roster`` — 감염 판정 스냅샷과 같은 시점의
활성 에이전트 전원 ``{agent, location, is_exterior, status, traveling, travel_group}`` + 시작·끝 경과분·시각)가
있는 wave 는 **활성 에이전트 전원 1행씩**(명단 순서 = 이름순) 쓴다. 컬럼 3개 추가: ``acted``(그 wave 에 턴 로그가
있나), ``traveling``, ``travel_group``(이동 중일 때만, ``|`` 구분 이름순). ``co_located_with`` 는
``roster_contact_groups`` — 엔진 ``_compute_contact_groups`` 와 같은 규칙의 접촉 그룹 — 에서 자신을 뺀 전원이다.
시각·경과분은 이벤트 값(fixed 모드·시간 없는 wave 도 끝이 있다). ``wave_roster`` 가 하나도 없으면 아래 구형식
출력과 바이트 단위로 같고, 섞여 있으면(옛 run 을 /continue) 명단 없는 wave 는 구형식 행(``acted=true``, 뒤 두
칸 빈 값)으로 둔다 — β 재계산 도구가 이 빈 값으로 형식을 구분한다. 중지된 run(또는 실행 중 내보내기)의
마지막 wave 는 턴 일부만 돌고 판정 전에 멈춰 명단이 없으므로 같은 구형식 행으로 나온다 — 별도 표식은 두지 않고
(바이트 패리티·구형식 유지), 도구가 "마지막 명단 wave 뒤의 구형식 wave = 판정되지 않은 wave"로 빼서 계산한다.

## 구형식 (옛 run — 아래 설명은 이 형식 기준)

각 로그 항목은 "그 에이전트가 그 wave 에 턴을 받았다"는 사실이고, 엔진이 실어주는
``location`` / ``is_exterior`` 는 **그 wave 의 이동이 적용되기 전** 값 — 즉 그 wave
동안 실제로 접촉이 일어난 장소다(``ABM/simulation/turn.py`` ``_apply_turn_result``).
구버전 run(컬럼 추가 이전)의 로그는 이 값들이 없다. 그 행을 버리지 않고 빈 값으로
남긴다 — wave/agent 정보 자체는 유효하기 때문.

접촉 분석 컬럼(2026-10 추가 — 마크다운의 감염 이벤트와 따로 대조하지 않아도 되게):
  - ``infection_status``      그 턴 시점 발화자의 S/E/P/I/R (엔진 스냅샷; 모델이 꺼져
                              있으면 전부 ``S``). 구버전 로그 → 빈 값.
  - ``is_infectious``         ``infection_status`` 가 P 또는 I 인가(전파원). 상태가 빈
                              값이면 빈 값.
  - ``elapsed_minutes_start`` wave 시작 시점의 절대 경과분(로그의 ``elapsed_minutes``).
  - ``elapsed_minutes_end``   wave 종료 시점의 절대 경과분 — ``time_jump`` 이벤트의
                              ``end_elapsed_minutes`` 1순위, 없으면 다음 wave 시작
                              경과분 폴백(``wave_end_time`` 과 같은 우선순위).
  - ``co_located_with``       같은 wave·같은 장소에 있던 **다른 발화자**들(``|`` 구분,
                              이름 오름차순 — 한 wave 안의 턴은 병렬이라 등장 순서가
                              실행마다 달라질 수 있어 정렬해 고정한다). 외부 공간(``is_exterior``)은 엔진의 접촉
                              판정(infection.py)과 같이 서로 보이지 않으므로 비운다.
  - ``new_exposure``          이 wave 에 이 에이전트가 **새로 감염**됐는가 — ``infection_
                              update`` 중 ``cause`` 가 ``event``(환자 0번 시드) 또는
                              ``transmission``(접촉 전파)인 것. 자연 진행(``progression``:
                              E→P/I 등)·회복은 아니다. 상태로 거르지 않는 이유: 길이 0
                              구간 건너뛰기(SIR 등)로 새 감염이 E 가 아니라 P/I 로 시작할
                              수 있다.

⚠ **한계 — 이 CSV 의 행은 "그 wave 에 턴을 받은(발화한) 에이전트"뿐이다.** 같은 방에
있었지만 그 wave 에 발화하지 않은 사람은 행 자체가 없어 ``co_located_with`` 에도 잡히지
않는다 — 완전한 동석 기록이 아니라 **발화한 사람들끼리의 동석**이다. ``new_exposure``
도 마찬가지로, 새로 감염된 사람이 그 wave 에 발화하지 않았으면 표시할 행이 없다. 또
``infection_status`` 는 턴 시점 값이라 그 wave 끝의 접촉 전파로 감염된 행은
``infection_status=S`` 이면서 ``new_exposure=true`` 일 수 있다(전파 판정은 wave 끝 —
시간 누적 뒤 — 에 일어난다).

엔진의 전염 판정(2026-10 wave 타이밍 정정)은 **이 CSV 의 ``location`` = wave 시작 위치**의
접촉 × 그 wave 의 경과(``elapsed_minutes_end - elapsed_minutes_start``)다. 단 zone 경계
이동 중(traveling)인 사람은 raw 위치가 이미 목적지라 ``location`` 이 목적지로 찍히지만
엔진은 동행 외 누구와도 접촉시키지 않는다 — 이 CSV 에는 이동 중 표시가 없다(한계).
"""
from __future__ import annotations

from .labels import js_str

CSV_HEADER = (
    "wave", "wave_start_time", "wave_end_time", "agent", "location", "is_exterior",
    "infection_status", "is_infectious", "elapsed_minutes_start", "elapsed_minutes_end",
    "co_located_with", "new_exposure",
)

# 명단 형식(``wave_roster`` 이벤트가 있는 run)에서만 붙는 컬럼.
ROSTER_EXTRA_HEADER = ("acted", "traveling", "travel_group")

# 이 이벤트의 ``data.end_time_str`` / ``end_elapsed_minutes`` 가 wave 종료의 1순위 출처다.
_TIME_JUMP = "time_jump"
# wave 시작 시점 활성 에이전트 전원 명단 + 시작·끝(엔진 infection.py::_wave_roster) — 명단 형식의 정본.
_WAVE_ROSTER = "wave_roster"
_INFECTION_UPDATE = "infection_update"
# 새 감염(노출)으로 치는 원인 — 자연 진행(progression)·회복(recovery)은 제외.
_EXPOSURE_CAUSES = ("event", "transmission")
_INFECTIOUS_STATUSES = ("P", "I")
_CO_LOCATED_SEP = "|"


# ── CSV 원시 포매팅 ───────────────────────────────────────────────────────────

def _csv_field(value) -> str:
    """CSV 필드 이스케이프 (RFC 4180): 쉼표·따옴표·개행이 있으면 따옴표로 감싸고 내부 ``"`` 는 ``""``.

    문자열화는 ``js_str`` 를 거친다 — JS 쪽 ``String(value)`` 와 같은 결과를 내기
    위해서다(특히 ``True`` → ``"true"``: 파이썬 기본 ``str()`` 은 ``"True"`` 라 그대로
    쓰면 두 구현이 갈린다).
    """
    s = js_str(value)
    if any(c in s for c in ',"\r\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


def _csv_row(values) -> str:
    return ",".join(_csv_field(v) for v in values)


# ── wave → 시각 맵 ───────────────────────────────────────────────────────────

def _build_wave_end_map(events: list[dict] | None) -> dict[int, str]:
    """``time_jump`` 이벤트 → ``{wave: end_time_str}``.

    JS 쪽은 ``/api/simulation/events?types=time_jump`` 로 서버에서 걸러진 배열을
    받지만, 여기서는 이벤트 전체 리스트를 받으므로 타입 필터를 직접 한다.

    이벤트의 ``wave`` 는 표시 wave(``disp_wave`` — ``/continue`` 의 ``wave_base`` 가
    반영된 값)라 턴 로그의 ``wave`` 와 같은 좌표계다. ``end_time_str`` 이 없는
    (= 이 필드 추가 이전에 저장된) 이벤트는 건너뛰어 폴백이 그 wave 를 처리하게 둔다.
    한 wave 에 이벤트가 두 번 실린 경우(재개 등)엔 나중 값이 최신이므로 덮어쓴다.
    """
    out: dict[int, str] = {}
    for evt in (events or []):
        if not isinstance(evt, dict) or evt.get("event_type") != _TIME_JUMP:
            continue
        data = evt.get("data")
        end = data.get("end_time_str") if isinstance(data, dict) else None
        if not isinstance(end, str) or not end:
            continue
        wave = evt.get("wave")
        out[0 if wave is None else wave] = end
    return out


def _build_wave_end_elapsed_map(events: list[dict] | None) -> dict[int, int]:
    """``time_jump`` 이벤트 → ``{wave: end_elapsed_minutes}``. 규칙은 `_build_wave_end_map`
    과 같다(필드가 없거나 정수가 아니면 건너뜀, 나중 값이 이김)."""
    out: dict[int, int] = {}
    for evt in (events or []):
        if not isinstance(evt, dict) or evt.get("event_type") != _TIME_JUMP:
            continue
        data = evt.get("data")
        end = data.get("end_elapsed_minutes") if isinstance(data, dict) else None
        if isinstance(end, bool) or not isinstance(end, int):
            continue
        wave = evt.get("wave")
        out[0 if wave is None else wave] = end
    return out


def _build_new_exposure_set(events: list[dict] | None) -> set[tuple[str, int]]:
    """``infection_update`` 중 새 감염(cause ∈ event/transmission) → ``{(agent, wave)}``."""
    out: set[tuple[str, int]] = set()
    for evt in (events or []):
        if not isinstance(evt, dict) or evt.get("event_type") != _INFECTION_UPDATE:
            continue
        data = evt.get("data")
        if not isinstance(data, dict) or data.get("cause") not in _EXPOSURE_CAUSES:
            continue
        agent = data.get("agent")
        if not isinstance(agent, str) or not agent:
            continue
        wave = evt.get("wave")
        out.add((agent, 0 if wave is None else wave))
    return out


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _build_roster_map(events: list[dict] | None) -> dict[int, dict]:
    """``wave_roster`` 이벤트 → ``{wave: data}``. ``agents`` 가 리스트가 아니면 건너뛴다.
    한 wave 에 두 번 실리면(재개 등) 나중 값이 이긴다(time_jump 와 같은 규칙)."""
    out: dict[int, dict] = {}
    for evt in (events or []):
        if not isinstance(evt, dict) or evt.get("event_type") != _WAVE_ROSTER:
            continue
        data = evt.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
            continue
        wave = evt.get("wave")
        out[0 if wave is None else wave] = data
    return out


def roster_contact_groups(agents: list[dict]) -> list[list[str]]:
    """명단 → 엔진 ``_compute_contact_groups`` 와 **같은 규칙**의 접촉 그룹(2명 이상).

    - zone 경계 이동 중(``traveling``)인 사람은 ``travel_group``(함께 이동하는 사람)끼리만.
    - 외부 공간(``is_exterior``)은 접촉 없음.
    - 위치가 빈 사람(floater)은 모든 장소 그룹에 낀다(이동 그룹에는 안 낌). 장소 그룹이 하나도
      없으면 floater 끼리 한 그룹.
    순서: 명단 순서(엔진은 key 정렬) 그대로 — JS ``rosterContactGroups`` 와 같아야 한다.
    """
    buckets: dict[str, list[str]] = {}
    travel: dict[str, list[str]] = {}
    floaters: list[str] = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        key = a.get("agent") or ""
        if a.get("traveling") is True:
            members = a.get("travel_group") if isinstance(a.get("travel_group"), list) else []
            tkey = "\u0000".join(sorted(str(m) for m in (members or [key])))
            travel.setdefault(tkey, []).append(key)
            continue
        loc = a.get("location") or ""
        if a.get("is_exterior") is True:
            continue
        if loc:
            buckets.setdefault(loc, []).append(key)
        else:
            floaters.append(key)
    groups = [g + floaters for g in buckets.values()] if buckets else [floaters]
    groups += list(travel.values())
    return [g for g in groups if len(g) >= 2]


# ── 메인 ──────────────────────────────────────────────────────────────────────

def render_location_csv(shared_log: list[dict] | None, events: list[dict] | None = None) -> str:
    """위치 이력 CSV 문자열을 만든다. 순수 함수(DB·네트워크 접근 없음).

    Parameters
    ----------
    shared_log
        dialogue 항목 리스트. ``speaker`` 키가 없는 background 항목은 여기서
        걸러진다 (GUI ``/api/simulation/logs`` 가 하던 필터와 같은 규칙 —
        JS 쪽은 이미 걸러진 배열을 받는다). 항목에 ``time_str`` / ``location`` /
        ``is_exterior`` 가 없어도(구버전 로그) 행은 유지되고 값만 빈 문자열이 된다.
    events
        ``{event_type, wave, timestamp, data}`` 리스트 전체. ``time_jump``(종료
        시각·경과분)와 ``infection_update``(``new_exposure``)만 골라 쓴다. 생략하면
        ``wave_end_time``/``elapsed_minutes_end`` 는 "다음 wave 시작" 폴백으로만
        채워지고(마지막 wave 는 빈칸) ``new_exposure`` 는 전부 false 다.

    Returns
    -------
    str
        CRLF 줄바꿈 CSV (헤더 1행 + 로그 항목당 1행, 마지막 줄 뒤에도 CRLF).
    """
    # background_log 항목(speaker 없음)은 내보내기 대상이 아니다.
    entries = [e for e in (shared_log or []) if isinstance(e, dict) and "speaker" in e]

    def _wave(e: dict) -> int:
        w = e.get("wave")
        return 0 if w is None else w

    # 1) wave → time_str. 같은 wave 의 모든 턴은 같은 time_str 을 공유하므로 처음 만난
    #    값이면 충분하다. (앞쪽 항목에 time_str 이 없을 수 있어 wave 의 첫 항목만 보지
    #    않고 전체를 훑는다.)
    wave_start: dict[int, str] = {}
    wave_start_elapsed: dict[int, int] = {}
    for e in entries:
        w = _wave(e)
        if w not in wave_start and e.get("time_str"):
            wave_start[w] = e["time_str"]
        if w not in wave_start_elapsed and _is_int(e.get("elapsed_minutes")):
            wave_start_elapsed[w] = e["elapsed_minutes"]

    # 2) 각 wave 의 종료 시각. 우선순위:
    #      (a) time_jump 이벤트의 end_time_str  — 마지막 wave 도 채워지는 유일한 경로
    #      (b) 다음 wave 의 시작 시각            — 고정 시간 모드/침묵 wave/구버전 run 폴백
    #      (c) 빈 문자열
    #    (a)와 (b)는 엔진 불변식상 같은 값이므로 섞여도 열 값이 흔들리지 않는다.
    jump_end = _build_wave_end_map(events)
    jump_end_elapsed = _build_wave_end_elapsed_map(events)
    waves = sorted({_wave(e) for e in entries})
    wave_end: dict[int, str] = {}
    wave_end_elapsed: dict[int, object] = {}
    for i, w in enumerate(waves):
        nxt = waves[i + 1] if i + 1 < len(waves) else None
        fallback = "" if nxt is None else wave_start.get(nxt, "")
        wave_end[w] = jump_end.get(w, fallback)
        fallback_el = "" if nxt is None else wave_start_elapsed.get(nxt, "")
        wave_end_elapsed[w] = jump_end_elapsed.get(w, fallback_el)

    new_exposure = _build_new_exposure_set(events)
    roster = _build_roster_map(events)
    if roster:
        return _render_with_roster(entries, roster, wave_start, wave_end, wave_end_elapsed, new_exposure)

    # 3) wave 오름차순 정렬(같은 wave 안에서는 원래 턴 순서 유지 — sorted 는 안정 정렬).
    ordered = sorted(entries, key=_wave)

    # 4) (wave, 장소) → 그 자리에 있던 발화자들(중복 없음, 출력은 이름 오름차순). 장소가 없거나
    #    외부 공간이면 동석으로 치지 않는다(엔진 접촉 판정과 같은 규칙).
    present: dict[tuple[int, str], list[str]] = {}
    for e in ordered:
        loc = e.get("location")
        if not isinstance(loc, str) or not loc or e.get("is_exterior") is True:
            continue
        names = present.setdefault((_wave(e), loc), [])
        who = e.get("speaker") or ""   # 키는 있고 값이 None 인 경우도 빈 문자열
        if who not in names:
            names.append(who)

    lines = [_csv_row(CSV_HEADER)]
    for e in ordered:
        w = _wave(e)
        exterior = e.get("is_exterior")
        speaker = e.get("speaker") or ""
        loc = e.get("location")
        status = e.get("infection_status")
        status = status if isinstance(status, str) and status else ""
        if isinstance(loc, str) and loc and exterior is not True:
            others = sorted(n for n in present.get((w, loc), []) if n != speaker)
            co_located = _CO_LOCATED_SEP.join(others)
        else:
            co_located = ""
        start_el = e.get("elapsed_minutes")
        lines.append(_csv_row([
            w,
            wave_start.get(w, ""),
            wave_end.get(w, ""),
            speaker,
            e.get("location", ""),                                    # 구버전 로그 → ''
            exterior if isinstance(exterior, bool) else "",           # true / false / ''
            status,                                                   # S/E/P/I/R / ''
            (status in _INFECTIOUS_STATUSES) if status else "",       # true / false / ''
            start_el if _is_int(start_el) else "",
            wave_end_elapsed.get(w, ""),
            co_located,
            (speaker, w) in new_exposure,                             # true / false
        ]))
    return "\r\n".join(lines) + "\r\n"


def _legacy_rows(ordered: list[dict], wave_start, wave_end, wave_end_elapsed, new_exposure) -> list[list]:
    """발화 행(구형식) — 같은 wave·같은 장소의 **다른 발화자**만 동석으로 친다."""
    def _wave(e: dict) -> int:
        w = e.get("wave")
        return 0 if w is None else w

    present: dict[tuple[int, str], list[str]] = {}
    for e in ordered:
        loc = e.get("location")
        if not isinstance(loc, str) or not loc or e.get("is_exterior") is True:
            continue
        names = present.setdefault((_wave(e), loc), [])
        who = e.get("speaker") or ""
        if who not in names:
            names.append(who)
    rows = []
    for e in ordered:
        w = _wave(e)
        exterior = e.get("is_exterior")
        speaker = e.get("speaker") or ""
        loc = e.get("location")
        status = e.get("infection_status")
        status = status if isinstance(status, str) and status else ""
        if isinstance(loc, str) and loc and exterior is not True:
            co_located = _CO_LOCATED_SEP.join(sorted(n for n in present.get((w, loc), []) if n != speaker))
        else:
            co_located = ""
        start_el = e.get("elapsed_minutes")
        rows.append([
            w, wave_start.get(w, ""), wave_end.get(w, ""), speaker, e.get("location", ""),
            exterior if isinstance(exterior, bool) else "",
            status, (status in _INFECTIOUS_STATUSES) if status else "",
            start_el if _is_int(start_el) else "", wave_end_elapsed.get(w, ""),
            co_located, (speaker, w) in new_exposure,
        ])
    return rows


def _render_with_roster(entries, roster, wave_start, wave_end, wave_end_elapsed, new_exposure) -> str:
    """명단 형식 — ``wave_roster`` 가 있는 wave 는 **활성 에이전트 전원 1행씩**(엔진 감염 스냅샷과
    같은 값), 없는 wave(같은 로그 안의 옛 run 등)는 발화 행(구형식)으로 둔다. 컬럼은 기존 12개 +
    ``acted``·``traveling``·``travel_group``. 구형식 행은 ``acted=true`` 이고 뒤 두 칸이 빈 값이다
    (그래서 β 재계산 도구가 wave 별로 형식을 구분한다)."""
    def _wave(e: dict) -> int:
        w = e.get("wave")
        return 0 if w is None else w

    by_wave: dict[int, list[dict]] = {}
    for e in entries:
        by_wave.setdefault(_wave(e), []).append(e)
    lines = [_csv_row(CSV_HEADER + ROSTER_EXTRA_HEADER)]
    for w in sorted(set(by_wave) | set(roster)):
        spoken = by_wave.get(w, [])
        data = roster.get(w)
        if data is None:
            for row in _legacy_rows(spoken, wave_start, wave_end, wave_end_elapsed, new_exposure):
                lines.append(_csv_row(row + [True, "", ""]))
            continue
        agents = [a for a in data["agents"] if isinstance(a, dict)]
        groups = roster_contact_groups(agents)
        acted = {e.get("speaker") or "" for e in spoken}
        start_el, end_el = data.get("start_elapsed_minutes"), data.get("end_elapsed_minutes")
        start_str = data.get("start_time_str") if isinstance(data.get("start_time_str"), str) else ""
        end_str = data.get("end_time_str") if isinstance(data.get("end_time_str"), str) else ""
        listed = set()
        for a in agents:
            key = a.get("agent") or ""
            listed.add(key)
            status = a.get("status") if isinstance(a.get("status"), str) else ""
            others = sorted({m for g in groups if key in g for m in g} - {key})
            traveling = a.get("traveling") is True
            group = a.get("travel_group") if traveling and isinstance(a.get("travel_group"), list) else []
            ext = a.get("is_exterior")
            lines.append(_csv_row([
                w, start_str, end_str, key, a.get("location") or "",
                ext if isinstance(ext, bool) else "",
                status, (status in _INFECTIOUS_STATUSES) if status else "",
                start_el if _is_int(start_el) else "", end_el if _is_int(end_el) else "",
                _CO_LOCATED_SEP.join(others), (key, w) in new_exposure,
                key in acted, traveling, _CO_LOCATED_SEP.join(sorted(str(m) for m in group)),
            ]))
        # 명단에 없는 발화자(엔진 불변식상 없어야 한다) — 버리지 않고 구형식 행으로 남긴다.
        stray = [e for e in spoken if (e.get("speaker") or "") not in listed]
        for row in _legacy_rows(stray, wave_start, wave_end, wave_end_elapsed, new_exposure):
            lines.append(_csv_row(row + [True, "", ""]))
    return "\r\n".join(lines) + "\r\n"
