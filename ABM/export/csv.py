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

각 로그 항목은 "그 에이전트가 그 wave 에 턴을 받았다"는 사실이고, 엔진이 실어주는
``location`` / ``is_exterior`` 는 **그 wave 의 이동이 적용되기 전** 값 — 즉 그 wave
동안 실제로 접촉이 일어난 장소다(``ABM/simulation/turn.py`` ``_apply_turn_result``).
구버전 run(컬럼 추가 이전)의 로그는 이 값들이 없다. 그 행을 버리지 않고 빈 값으로
남긴다 — wave/agent 정보 자체는 유효하기 때문.
"""
from __future__ import annotations

from .labels import js_str

CSV_HEADER = ("wave", "wave_start_time", "wave_end_time", "agent", "location", "is_exterior")

# 이 이벤트의 ``data.end_time_str`` 이 wave 종료 시각의 1순위 출처다.
_TIME_JUMP = "time_jump"


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
        ``{event_type, wave, timestamp, data}`` 리스트 전체. ``time_jump`` 만
        골라 쓴다. 생략하면 ``wave_end_time`` 은 "다음 wave 시작 시각" 폴백으로만
        채워진다(마지막 wave 는 빈칸).

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
    for e in entries:
        w = _wave(e)
        if w not in wave_start and e.get("time_str"):
            wave_start[w] = e["time_str"]

    # 2) 각 wave 의 종료 시각. 우선순위:
    #      (a) time_jump 이벤트의 end_time_str  — 마지막 wave 도 채워지는 유일한 경로
    #      (b) 다음 wave 의 시작 시각            — 고정 시간 모드/침묵 wave/구버전 run 폴백
    #      (c) 빈 문자열
    #    (a)와 (b)는 엔진 불변식상 같은 값이므로 섞여도 열 값이 흔들리지 않는다.
    jump_end = _build_wave_end_map(events)
    waves = sorted({_wave(e) for e in entries})
    wave_end: dict[int, str] = {}
    for i, w in enumerate(waves):
        nxt = waves[i + 1] if i + 1 < len(waves) else None
        fallback = "" if nxt is None else wave_start.get(nxt, "")
        wave_end[w] = jump_end.get(w, fallback)

    # 3) wave 오름차순 정렬(같은 wave 안에서는 원래 턴 순서 유지 — sorted 는 안정 정렬).
    ordered = sorted(entries, key=_wave)

    lines = [_csv_row(CSV_HEADER)]
    for e in ordered:
        w = _wave(e)
        exterior = e.get("is_exterior")
        lines.append(_csv_row([
            w,
            wave_start.get(w, ""),
            wave_end.get(w, ""),
            e.get("speaker", ""),
            e.get("location", ""),                                    # 구버전 로그 → ''
            exterior if isinstance(exterior, bool) else "",           # true / false / ''
        ]))
    return "\r\n".join(lines) + "\r\n"
