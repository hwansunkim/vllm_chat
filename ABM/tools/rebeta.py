"""β 재계산 — 위치 이력 CSV로 전염 확산을 다른 β 로 다시 계산한다(LLM 시뮬레이션 재실행 없음).

사용:
    python -m ABM.tools.rebeta <locations.csv> <scenario.json> \\
        --beta 0.02 0.04 0.1 --runs 1000 --seed 42 --out <dir>
        [--initial-mode replay|sample] [--backfill]

입력:
  - `<locations.csv>` — 위치 이력 CSV 내보내기(`ABM/export/csv.py`, 접촉 분석 컬럼 포함판).
    `wave, agent, location, is_exterior, infection_status, elapsed_minutes_start` 가 필요하다.
  - `<scenario.json>` — 시나리오 내보내기(`config.infection_model` + `config.events`), 최상위
    `infection_model`, 또는 infection_model 객체 자체. E/P/I 지속 시간 분포·`immune_after_recovery`·
    원래 β 를 쓰고, `infect_agent` 이벤트가 있으면 환자 0번(시드)으로 쓴다.

**고정 행동 가정 (중요).** 에이전트의 이동·발화는 원래 실행 그대로 고정된다 — 가상의 감염에
반응해 행동이 바뀌지 않는다(예: 원래 실행에선 안 아팠던 사람이 이 재계산에서 감염돼도 여전히
원래대로 돌아다닌다). 결과는 "같은 동선에서 β 만 달랐다면"의 확률 분포다.

**"최종 감염자" = 관찰 기간 끝까지.** 마지막 wave 이후의 감염은 세지 않는다(출력에 기간 X일 명시).
감염자 = 관찰 기간 중 한 번이라도 S 를 벗어난 에이전트(시드 포함).

## 접촉 재구성 (설계 결정)

엔진의 전염 판정(2026-10 wave 타이밍 정정 — `ABM/simulation/infection.py` 모듈 docstring)은
**wave 시작 위치의 접촉 그룹 × 그 wave 자신의 경과 시간**이다. CSV 행의 `location` 이 바로 그 wave
시작 위치(이동 전)이고 `elapsed_minutes_end - elapsed_minutes_start` 가 그 wave 의 경과라, 도구도 같은 계산을
한다 — wave w 의 판정 = wave w 명단 × wave w 경과(원칙: CSV 계산 == 엔진 계산).
  - CSV 에는 **그 wave 에 발화한 에이전트의 행만** 있다. **두 행 사이(gap)는 다음 행의 위치로 채운다** —
    행 w1 다음 wave 의 시작 위치 = w1 의 이동 후 위치이고, 이동은 거의 발화한 wave 에서만 일어나므로(`move_to`)
    그건 다음 행 w2 의 위치와 같다. **마지막 행 이후(tail)는 마지막 위치를 이어 쓴다** — 마지막 발화 wave 의
    이동 목적지는 CSV 어디에도 없다(아래 "한계"). 채운 행은 `is_imputed=true`.
  - **첫 행 이전은 부재(기본).** `agent_enter` 로 나중에 들어온 사람에게 가짜 접촉을 만들지 않으려고 첫 행
    이전 wave 에는 그 사람이 없다고 본다. `--backfill` 이면 첫 위치를 앞쪽 wave 에 채운다. 퇴장(`agent_exit`)은
    CSV 로 알 수 없어 마지막 위치에 남는다.
  - **t_d** = 그 wave 의 경과. 시간 설정은 시나리오 JSON 의 `config.time_mode`·`time_per_wave` 에서 읽는다.
    - **fixed 모드**: 항상 `time_per_wave`(엔진 시계가 wave 단위라 턴 실패와 무관하게 흐른다). fixed 모드는
      `time_jump` 를 emit 하지 않아 CSV 마지막 행의 `elapsed_minutes_end` 가 비고, 중간에 행 없는 wave 가 있으면
      다음 행 시작이 2×tpw 뒤라 CSV 끝값/다음 행 추정은 틀릴 수 있다 — 그래서 CSV 값 대신 tpw 를 쓴다.
    - **variable 모드(또는 설정 없음)**: `elapsed_minutes_end - elapsed_minutes_start`. 종료 경과분이 비면 다음
      관찰 wave 의 시작으로, 마지막 wave 면 **0** 으로 두고 경고한다 — variable 모드에서 종료 경과분은
      `time_jump` 에서 오므로, 비어 있다는 건 그 wave 에 시간이 누적되지 않았다(성공 발화 0·idle 점프 아님 →
      엔진도 t_d=0)는 뜻이거나 실행이 시간 누적 전에 멈췄다는 뜻이다(그 경우 엔진도 판정하지 않는다).
    진행(E→P→I→R)은 엔진처럼 wave 끝 시각으로 판정한다.
  - `co_located_with` 는 쓰지 않는다(발화자끼리만의 동석). 에이전트 집합 = CSV 에 나온 사람 —
    **한 번도 발화하지 않은 활성 에이전트는 보이지 않는다**.
  - 외부 공간(`is_exterior=true`)·위치 미설정 floater 규칙은 엔진 `_compute_contact_groups` 그대로.

## 엔진 재사용

`ABM/simulation/infection.py::_InfectionMixin` 을 상속한 최소 shim(`_ReplayEngine`)으로 엔진의
`_infection_wave_start`(wave 시작 스냅샷) → `_apply_infection_wave`(wave 끝 판정)를 **그대로** 돌린다 —
t_d·λ=β·t_d·p=1-exp(-λ)·쌍별 독립 추첨과 break·P/I 전파원·wave 시작 스냅샷·한 wave 한 단계·노출 시점의
E/P/I 지속 추첨·P 0일이면 E→I 직행·`immune_after_recovery`·새 노출 시각(wave 끝)이 엔진 코드 그 자체다.
엔진은 모듈 전역 `random` 을 쓰므로 실행마다 `random.seed(seed*1000003 + run)` 으로 고정한다 — 같은
run 번호는 β 가 달라도 같은 난수열에서 출발한다(common random numbers). **원래 실행의 난수열과는
같지 않다** — 원래 실행은 이동 시간·시간 카테고리 등 다른 추첨도 같은 전역 난수로 했기 때문이다.

## 시드(환자 0번)와 처음부터 감염 상태인 사람 (설계 결정)

  - **시드 = 시나리오의 `infect_agent` 이벤트**(agent, wave, start_status). 이벤트 정보가 있으면 이게
    정본이다. 시드는 max(이벤트 wave, 그 사람의 첫 등장 wave)의 **시작**에 걸린다(엔진도 이벤트를 wave 시작에
    실행하므로 시작 상태가 P/I 면 그 wave 부터 전파원).
  - 이벤트 정보가 없으면(infection_model 만 준 경우) **대체 규칙**: 첫 행이 E/P/I/R 이고, 그보다 앞선
    wave 에 감염 상태로 관찰된 사람이 아무도 없으면 시드로 본다(그 전엔 옮길 사람이 없었으므로).
  - 둘 다 아닌데 첫 행이 감염 상태인 사람 = **발화 전에 원래 실행에서 감염된 사람**이다. 시드로 두면
    β 와 무관한 고정 감염자가 돼 버리므로, 첫 등장부터 **S 로 시뮬레이션**하고 경고한다(첫 행 이전엔
    부재라 그 전 노출은 반영되지 않는다 — 과소 추정 방향).
  - 시드의 병의 경과 — 기본 `replay`: 원래 감염 시각을 모르므로, β 와 무관한 그 사람의 경과를
    **관찰된 상태 그대로** 재생한다(진행 시계는 얼림). 행 사이의 상태는 위치와 같은 규칙으로 **다음 행의
    상태**를 쓴다 — 다음 행에서 관찰된 변화는 gap 의 첫 wave 부터 적용한다(가장 이른 시점). 그래서
    gap 중의 회복(I→R)이 늦게 반영돼 전파원으로 더 오래 남는 일은 없다(대신 E→I 같은 변화는 가장
    이르게 잡혀 전파를 약간 과대 추정할 수 있다 — 방향을 명시). 관찰상 S 로 돌아오면(SEPIRS) 그때부터
    일반 시뮬레이션. `--initial-mode sample` 은 엔진의 `_set_infected(start_status=…)` 로 지속 시간을 새로
    뽑는다(원래 실행의 환자 0번과 같은 규칙; 시각 앵커 = 이벤트 wave 의 시작 경과분).

## 재개(/resume) 경계

엔진의 t_d 는 각 wave 자신의 시작·끝 차이라 저장 상태가 없어 `/resume`·`/load` 경계에서도 이어 돌린 것과
같다 — 경계 보정이 필요 없다(예전의 `--reset-at` 은 제거됨).

## 한계 (CSV 에 정보가 없는 것 — 후속 과제)
  - 마지막 발화 이후 이동(tail)의 목적지는 알 수 없다 — 한 번 `move_to` 로 떠나고 다시 발화하지 않은
    사람은 출발지에 남는다(QA e2e2).
  - zone 경계 이동 중(traveling)인 사람을 엔진은 동행 외 누구와도 접촉시키지 않지만, CSV 에는 이동 중
    표시가 없다(이동 중엔 보통 턴이 없어 행도 없고, gap 은 도착지 위치로 채워진다 — 이동 시간 동안의
    접촉을 과대 추정할 수 있다). 여러 hop 여정·`bring_along` 동행도 근사다.
  - 한 번도 발화하지 않은 사람·퇴장은 보이지 않는다.
  - **행이 하나도 없는 wave 는 보이지 않는다** — variable 모드에서 전원의 턴이 실패했는데 연속 침묵 idle
    점프로 시간이 흐른 wave 등. 그 wave 의 접촉 시간은 어느 행에도 실리지 않는다(앞 wave 의 종료 경과분은
    그 wave 직전까지라 사라진다). fixed 모드는 wave 마다 tpw 를 쓰므로 빠진 wave 의 tpw 만큼 사라진다.

## 출력 (`--out`)
  - `<stem>_rebeta_summary.csv` — β 별 요약(평균·표준오차·표준편차·최소·최대 최종 감염자, 최종 감염자
    0..N 확률, 에이전트별 감염 확률과 표준오차, 첫 새 감염 시각 평균, 관찰 결과 재현 확률과 표준오차).
  - `<stem>_rebeta_beta<β>_waves.csv` — β 별 **대표 run 하나**(최종 감염자 수가 하위 중앙값과 같은
    run 중 가장 작은 번호)의 wave 별 전체 명단: 원래 컬럼 + `is_imputed`, `sim_status`(wave 시작 시점
    상태 — CSV 의 `infection_status` 와 같은 의미), `sim_is_infectious`, `sim_contact_location`(그 wave
    판정에 쓴 위치 = 그 wave 시작 위치. 원래 행이면 `location` 과 같고, 채운 행이면 채운 값), `sim_t_d_minutes`
    (그 wave 의 접촉 시간, 분), `sim_new_exposure`(이 wave 판정에서
    새로 감염), `sim_p_infect`(이 wave 판정에서 그 사람이 감염될 **합산 확률** 1-(1-p)^k — k = 그 사람이
    속한 각 그룹의 전파원 수 합. floater 는 여러 그룹에서 각각 판정되므로 그룹마다 더한다. S 가 아니거나
    전파원과 같은 그룹이 아니면 0).
  - 두 CSV 맨 위의 `#` 줄에 가정·관찰 기간을 적는다(`pandas.read_csv(..., comment="#")`).
  - 원래 시나리오 β 가 `--beta` 에 없으면 sanity check 용으로 자동 추가한다.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ABM.simulation.infection import (
    _InfectionMixin,
    DEFAULT_EXPOSED_DURATION,
    DEFAULT_INFECTIOUS_DURATION,
    DEFAULT_PRESYMPTOMATIC_DURATION,
)

_INFECTED_STATES = ("E", "P", "I", "R")
_CARRIER_STATES = ("P", "I")
_EXPOSURE_CAUSES = ("event", "transmission")
_FROZEN = 10 ** 15          # replay 시드의 진행 임계 — 엔진이 절대 다음 단계로 못 넘기게
_SEED_STRIDE = 1_000_003

ASSUMPTION_LINES = (
    "고정 행동 가정: 에이전트의 이동·발화는 원래 실행 그대로이며 가상의 감염에 반응하지 않는다.",
    "최종 감염자 = 관찰 기간 끝까지 한 번이라도 S 를 벗어난 에이전트(그 이후 감염은 세지 않음).",
)

logger = logging.getLogger(__name__)


class RebetaError(ValueError):
    """사용자 입력 문제 — CLI 가 메시지만 보여주고 종료 코드 2 로 끝낸다."""


# ── 입력 ─────────────────────────────────────────────────────────────────────

@dataclass
class Observed:
    """위치 이력 CSV 를 wave 단위로 정리한 것."""
    fieldnames: list[str]
    waves: list[int]                                  # 관찰된 wave(오름차순)
    start: dict[int, int]                             # wave → elapsed_minutes_start
    end: dict[int, int | None]                        # wave → elapsed_minutes_end(없을 수 있음)
    rows: dict[int, list[dict]]                       # wave → 원래 행들(CSV 순서)
    agents: list[str]                                 # 처음 나온 순서
    first_wave: dict[str, int]
    exterior: set[str] = field(default_factory=set)
    blank_status_rows: int = 0                        # infection_status 가 빈 행 수(S 로 취급)
    time_mode: str | None = None                      # 시나리오 config.time_mode (None = 모름 → variable 규칙)
    time_per_wave: int = 0                            # fixed 모드의 wave 길이(분)

    @property
    def fixed_time(self) -> bool:
        return self.time_mode == "fixed"

    @property
    def observed_days(self) -> float:
        if not self.waves:
            return 0.0
        return (wave_ends(self)[self.waves[-1]] - self.start[self.waves[0]]) / 1440

    @property
    def observed_final_infected(self) -> int:
        """관찰된 실행에서 한 번이라도 S 를 벗어난 에이전트 수.

        `infection_status` 는 턴 시점(wave 시작) 값이라 wave 끝의 접촉 전파로 감염된 사람은 그 행이 S 다 —
        그 뒤 다시 발화하지 않으면(마지막 wave 등) 상태로는 안 보이므로 `new_exposure=true` 행도 센다.
        """
        return len({r["agent"] for rs in self.rows.values() for r in rs
                    if _status(r) in _INFECTED_STATES
                    or (r.get("new_exposure") or "").strip().lower() == "true"})


_REQUIRED = ("wave", "agent", "location", "is_exterior", "infection_status", "elapsed_minutes_start")


def _int_or_none(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _status(r: dict) -> str:
    return (r.get("infection_status") or "").strip() or "S"


def _ext(r: dict) -> bool:
    return (r.get("is_exterior") or "").strip().lower() == "true"


def load_location_csv(path) -> Observed:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        missing = [c for c in _REQUIRED if c not in fieldnames]
        if missing:
            raise RebetaError(
                f"CSV 에 필요한 컬럼이 없습니다: {missing} — 접촉 분석 컬럼이 들어간 위치 이력 "
                f"CSV(ABM/export/csv.py)가 필요합니다."
            )
        raw = [r for r in reader if (r.get("agent") or "").strip()]

    rows: dict[int, list[dict]] = {}
    start: dict[int, int] = {}
    end: dict[int, int | None] = {}
    agents: list[str] = []
    first_wave: dict[str, int] = {}
    exterior: set[str] = set()
    blank = 0
    for r in raw:
        w = _int_or_none(r["wave"])
        if w is None:
            continue
        el = _int_or_none(r["elapsed_minutes_start"])
        if el is None:
            raise RebetaError(f"wave {w} 의 elapsed_minutes_start 가 비어 있습니다 — 구버전 CSV 는 쓸 수 없습니다.")
        start.setdefault(w, el)
        if end.get(w) is None:
            end[w] = _int_or_none(r.get("elapsed_minutes_end"))
        rows.setdefault(w, []).append(r)
        if not (r.get("infection_status") or "").strip():
            blank += 1
        a = r["agent"]
        if a not in first_wave:
            first_wave[a] = w
            agents.append(a)
        else:
            first_wave[a] = min(first_wave[a], w)
        if _ext(r) and r.get("location"):
            exterior.add(r["location"])
    if not rows:
        raise RebetaError(f"{path}: 데이터 행이 없습니다(헤더만 있는 CSV).")
    return Observed(fieldnames, sorted(rows), start, end, rows, agents, first_wave, exterior, blank)


@dataclass
class Seed:
    agent: str
    wave: int
    start_status: str


def _load_json(path) -> object:
    with open(path, encoding="utf-8-sig") as fh:      # BOM 허용
        return json.load(fh)


def _find_model(d) -> dict | None:
    if isinstance(d, dict):
        if isinstance(d.get("config"), dict) and isinstance(d["config"].get("infection_model"), dict):
            return d["config"]["infection_model"]
        if isinstance(d.get("infection_model"), dict):
            return d["infection_model"]
        if any(k in d for k in ("beta", "exposed_duration", "infectious_duration")):
            return d
    return None


def load_infection_model(path) -> dict:
    """시나리오 JSON 에서 infection_model 을 꺼낸다(config 아래·최상위·객체 자체 모두 허용)."""
    model = _find_model(_load_json(path))
    if model is None:
        raise RebetaError(f"{path}: infection_model 을 찾지 못했습니다.")
    return model


def load_time_config(path) -> tuple[str | None, int]:
    """시나리오 JSON 의 (time_mode, time_per_wave). config 아래·최상위 모두 허용, 없으면 (None, 0)."""
    d = _load_json(path)
    for c in ((d.get("config") if isinstance(d, dict) else None), d):
        if isinstance(c, dict) and c.get("time_mode") in ("fixed", "variable"):
            tpw = _int_or_none(c.get("time_per_wave"))
            return c["time_mode"], max(0, tpw or 0)
    return None, 0


def load_seed_events(path) -> list[Seed] | None:
    """시나리오의 `infect_agent` 이벤트 → 시드 목록. 이벤트 목록 자체가 없으면 None(대체 규칙)."""
    d = _load_json(path)
    events = None
    if isinstance(d, dict):
        if isinstance(d.get("config"), dict) and isinstance(d["config"].get("events"), list):
            events = d["config"]["events"]
        elif isinstance(d.get("events"), list):
            events = d["events"]
    if events is None:
        return None
    seeds = []
    for e in events:
        if not isinstance(e, dict) or e.get("type") != "infect_agent" or not e.get("agent"):
            continue
        st = e.get("start_status") if e.get("start_status") in ("E", "P", "I") else "E"
        seeds.append(Seed(str(e["agent"]), _int_or_none(e.get("wave")) or 0, st))
    return seeds


def resolve_seeds(obs: Observed, seed_events: list[Seed] | None) -> tuple[list[Seed], list[str], list[str]]:
    """(적용할 시드, 경고, 시드 출처 설명). 규칙은 모듈 docstring "시드" 절."""
    warnings: list[str] = []
    if seed_events is not None:
        seeds = []
        for s in seed_events:
            if s.agent not in obs.first_wave:
                warnings.append(f"시드 {s.agent} 가 CSV 에 한 번도 나오지 않아 반영할 수 없습니다(발화 없음).")
                continue
            seeds.append(s)
        source = "시나리오 infect_agent 이벤트"
    else:
        seeds, seen_infected_before = [], False
        for w in obs.waves:
            first_here = [r for r in obs.rows[w] if obs.first_wave[r["agent"]] == w]
            for r in first_here:
                if _status(r) in _INFECTED_STATES and not seen_infected_before:
                    seeds.append(Seed(r["agent"], w, _status(r) if _status(r) != "R" else "I"))
            if any(_status(r) in _INFECTED_STATES for r in obs.rows[w]):
                seen_infected_before = True
        source = "대체 규칙(이벤트 정보 없음 — 앞선 감염자가 없는 상태에서 감염으로 처음 관찰된 사람)"
    seeded = {s.agent for s in seeds}
    for a in obs.agents:
        w = obs.first_wave[a]
        first = next(r for r in obs.rows[w] if r["agent"] == a)
        if _status(first) in _INFECTED_STATES and a not in seeded:
            warnings.append(
                f"{a} 는 첫 관찰(wave {w})부터 {_status(first)} 이지만 시드가 아닙니다 — 발화 전에 원래 "
                f"실행에서 감염된 것으로 보고 첫 등장부터 S 로 시뮬레이션합니다(그 전 노출은 반영 안 됨).")
    return seeds, warnings, [source]


# ── 명단(roster)·관찰 상태 재구성 ─────────────────────────────────────────────

Roster = dict[int, dict[str, tuple[str, bool, bool]]]   # wave → agent → (location, is_exterior, imputed)


def _fill(obs: Observed, value_of, *, backfill: bool) -> dict[int, dict[str, tuple]]:
    """에이전트별 행 값을 wave 전체로 채운다 — 행 사이는 **다음 행** 값, 마지막 행 이후는 마지막 값,
    첫 행 이전은 부재(`backfill`이면 첫 값). 값은 (value, imputed)."""
    per_agent: dict[str, list[tuple[int, object]]] = {a: [] for a in obs.agents}
    for w in obs.waves:
        for r in obs.rows[w]:
            per_agent[r["agent"]].append((w, value_of(r)))
    out: dict[int, dict[str, tuple]] = {w: {} for w in obs.waves}
    for a, seq in per_agent.items():
        if not seq:
            continue
        j = 0                                         # 다음(또는 현재) 행 인덱스
        for w in obs.waves:
            while j < len(seq) and seq[j][0] < w:
                j += 1
            if j < len(seq) and seq[j][0] == w:
                out[w][a] = (seq[j][1], False)
            elif j < len(seq) and j > 0:              # 두 행 사이 — 다음 행 값
                out[w][a] = (seq[j][1], True)
            elif j >= len(seq):                       # 마지막 행 이후 — 마지막 값
                out[w][a] = (seq[-1][1], True)
            elif backfill:                            # 첫 행 이전
                out[w][a] = (seq[0][1], True)
    return out


def build_roster(obs: Observed, *, backfill: bool = False) -> Roster:
    filled = _fill(obs, lambda r: (r.get("location") or "", _ext(r)), backfill=backfill)
    return {w: {a: (v[0], v[1], imp) for a, (v, imp) in d.items()} for w, d in filled.items()}


def build_observed_status(obs: Observed, *, backfill: bool = False) -> dict[int, dict[str, str]]:
    """wave 별 관찰 감염 상태(행 사이는 다음 행 상태 — 관찰된 변화를 gap 의 첫 wave 부터 적용)."""
    filled = _fill(obs, _status, backfill=backfill)
    return {w: {a: v for a, (v, _imp) in d.items()} for w, d in filled.items()}


# ── 엔진 shim ────────────────────────────────────────────────────────────────

class _ReplayEngine(_InfectionMixin):
    """`_InfectionMixin` 이 필요로 하는 최소 상태만 갖춘 엔진 대역 — 감염 로직은 엔진 코드 그대로."""

    def __init__(self, agents, model: dict, beta: float, exterior):
        def _dur(key, default):
            v = model.get(key)
            return dict(v) if isinstance(v, dict) else dict(default)
        self.agents = {a: None for a in agents}
        self._key_to_alias: dict = {}
        self._infection_enabled = True
        self._infection_disease_name = ""
        self._infection_beta = max(0.0, float(beta or 0.0))       # core.py 와 같은 정규화
        self._infection_immune = bool(model.get("immune_after_recovery", True))
        self._infection_stages: list = []
        self._exposed_duration_spec = _dur("exposed_duration", DEFAULT_EXPOSED_DURATION)
        self._presymptomatic_duration_spec = _dur("presymptomatic_duration", DEFAULT_PRESYMPTOMATIC_DURATION)
        self._infectious_duration_spec = _dur("infectious_duration", DEFAULT_INFECTIOUS_DURATION)
        self._agent_infection: dict = {}
        self._agent_location: dict = {}
        self._exterior_locations = set(exterior)
        self.active_agents: set = set()
        self.completed_waves = 0
        self._now: dict[int, int] = {}          # 관찰 wave 인덱스 i → 시작 경과분(i+1 = i 의 끝)
        self.events: list[dict] = []

    def _current_elapsed_minutes(self, wave):
        # 엔진의 run_wave 자리에 관찰 wave 인덱스 i 를 넘긴다 — i 의 시작 = _now[i], 끝 = _now[i+1].
        return self._now[wave]

    def _emit(self, event_type, data):
        if event_type == "infection_update":
            self.events.append(data)


# ── 한 번 실행 ───────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    infected: set[str]                         # 관찰 기간 중 S 를 벗어난 에이전트
    infection_minutes: dict[str, int]          # 시뮬레이션이 만든 감염(전파·sample 시드)의 시각
    first_new_infection_minutes: int | None    # 시드가 아닌 사람의 첫 감염 시각
    waves: list[dict] | None = None            # record=True 일 때 wave 별 기록
    entries: dict | None = None                # 실행 끝의 엔진 감염 상태(에이전트별 dict 복사본)

    @property
    def final_count(self) -> int:
        return len(self.infected)


def wave_ends(obs: Observed) -> dict[int, int]:
    """관찰 wave → 끝 경과분 (모듈 docstring "t_d").

    fixed 모드: 시작 + time_per_wave. variable/모름: `elapsed_minutes_end` → 다음 관찰 wave 시작 → 없으면
    시작(= t_d 0).
    """
    out = {}
    for i, w in enumerate(obs.waves):
        if obs.fixed_time:
            out[w] = obs.start[w] + max(0, obs.time_per_wave)
            continue
        e = obs.end.get(w)
        if e is None:
            e = obs.start[obs.waves[i + 1]] if i + 1 < len(obs.waves) else obs.start[w]
        out[w] = max(e, obs.start[w])
    return out


def simulate_once(obs: Observed, roster: Roster, model: dict, beta: float, seed: int, *,
                  initial_mode: str = "replay", obs_status=None, seeds: list[Seed] | None = None,
                  record: bool = False) -> RunResult:
    """한 번의 재계산. `seeds=None`이면 대체 규칙(`resolve_seeds(obs, None)`)으로 시드를 정한다."""
    random.seed(seed)
    eng = _ReplayEngine(obs.agents, model, beta, obs.exterior)
    for a in obs.agents:
        eng._infection_entry(a)                               # 전원 S 로 시작
    obs_status = obs_status if obs_status is not None else build_observed_status(obs)
    if seeds is None:
        seeds = resolve_seeds(obs, None)[0]
    ends = wave_ends(obs)
    pending = {s.agent: s for s in seeds}                    # 아직 안 걸린 시드
    seed_status = {s.agent: s.start_status for s in seeds}
    seen_infected: set[str] = set()
    started: set[str] = set()
    replay: set[str] = set()
    seeded: set[str] = set()
    infected: set[str] = set()
    inf_min: dict[str, int] = {}
    rec: list[dict] | None = [] if record else None

    def _anchor(w: int) -> int:
        later = [x for x in obs.waves if x >= w]
        return obs.start[later[0]] if later else obs.start[obs.waves[-1]]

    for i, w in enumerate(obs.waves):
        now = obs.start[w]
        eng._now[i] = now
        eng._now[i + 1] = ends[w]
        started.update(roster[w])
        # 시드 — 이벤트 wave 와 첫 등장 중 늦은 쪽의 wave 시작에 건다(엔진: 이벤트는 wave 시작에 실행).
        for a, s in list(pending.items()):
            if a in started and s.wave <= w:
                del pending[a]
                seeded.add(a)
                infected.add(a)
                if initial_mode == "replay":
                    replay.add(a)
                else:
                    eng._set_infected(a, w, "event", at_minutes=_anchor(s.wave), start_status=s.start_status)
        # replay — 관찰 상태를 강제하고 진행 시계는 얼린다. 관찰상 S 면(시드 직후 아직 감염 관찰 전이면
        # 시드 상태, SEPIRS 회복 후면 해제).
        for a in list(replay):
            st = obs_status.get(w, {}).get(a, "S")
            e = eng._infection_entry(a)
            if st == "S":
                if a in seen_infected:                  # 감염으로 관찰된 뒤 S = SEPIRS 회복 → 해제
                    replay.discard(a)
                    e.update(status="S", infected_at_minutes=None, presymptomatic_at_minutes=None,
                             infectious_at_minutes=None, recover_at_minutes=None)
                    continue
                st = seed_status[a]                     # 시드 직후 아직 감염 관찰 전 → 시드 상태
            else:
                seen_infected.add(a)
            e.update(status=st, infected_at_minutes=now, presymptomatic_at_minutes=_FROZEN,
                     infectious_at_minutes=_FROZEN, recover_at_minutes=_FROZEN)
        # 접촉 = 이 wave 의 시작 위치(= CSV location, gap 은 채운 값). 첫 등장 전인 사람은 부재.
        eng.active_agents = {a for a in roster[w] if a in started}
        eng._agent_location = {a: roster[w][a][0] for a in eng.active_agents}
        start = eng._infection_wave_start(i)
        if record:
            p_of = _infection_probabilities(eng, start, ends[w] - now)
        n_ev = len(eng.events)
        eng._apply_infection_wave(i, w, start)
        new = [d for d in eng.events[n_ev:] if d.get("cause") in _EXPOSURE_CAUSES]
        for d in new:
            infected.add(d["agent"])
            inf_min.setdefault(d["agent"], d["elapsed_minutes"])
        if record:
            exposed_now = {d["agent"] for d in new if d.get("cause") == "transmission"}
            for a, (loc, ext, imputed) in roster[w].items():
                rec.append({"wave": w, "agent": a, "location": loc, "is_exterior": ext,
                            "is_imputed": imputed, "sim_status": start["status"].get(a, "S"),
                            "sim_contact_location": eng._agent_location.get(a, ""),
                            "sim_t_d_minutes": ends[w] - now,
                            "sim_new_exposure": a in exposed_now, "sim_p_infect": p_of.get(a, 0.0)})

    new_times = [m for a, m in inf_min.items() if a not in seeded]
    entries = {a: dict(e) for a, e in eng._agent_infection.items()}
    return RunResult(infected, inf_min, min(new_times) if new_times else None, rec, entries)


def _infection_probabilities(eng: _ReplayEngine, start: dict, t_d_minutes: int) -> dict[str, float]:
    """이번 판정에서 각 S 에이전트가 감염될 합산 확률 1-(1-p)^k (난수 없음, wave 시작 스냅샷 기준).

    엔진은 그룹마다 (그룹의 전파원 수)만큼 독립 추첨하고 감염되면 멈춘다. floater 는 여러 그룹에
    끼므로 그룹마다 따로 판정된다 — k = 그 사람이 속한 각 그룹의 전파원 수 합.
    """
    t_d = max(0, t_d_minutes) / 1440
    status = start["status"]
    infectious = {a for a, st in status.items() if st in _CARRIER_STATES}
    if not infectious or eng._infection_beta <= 0 or t_d <= 0:
        return {}
    p = 1.0 - math.exp(-eng._infection_beta * t_d)
    k: dict[str, int] = {}
    for group in start["groups"]:
        carriers = sum(1 for x in group if x in infectious)
        if not carriers:
            continue
        for x in group:
            if status.get(x) == "S":
                k[x] = k.get(x, 0) + carriers
    return {x: 1.0 - (1.0 - p) ** n for x, n in k.items()}


# ── Monte Carlo ──────────────────────────────────────────────────────────────

@dataclass
class BetaSummary:
    beta: float
    runs: int
    counts: list[int]
    per_agent: dict[str, float]
    first_new: list[int]
    representative_run: int

    @property
    def mean(self) -> float:
        return statistics.fmean(self.counts) if self.counts else 0.0

    @property
    def std(self) -> float:
        return statistics.pstdev(self.counts) if len(self.counts) > 1 else 0.0

    @property
    def se_mean(self) -> float:
        return statistics.stdev(self.counts) / math.sqrt(len(self.counts)) if len(self.counts) > 1 else 0.0

    def p_count(self, k: int) -> float:
        return self.counts.count(k) / len(self.counts) if self.counts else 0.0

    def se_p(self, p: float) -> float:
        """이항 비율의 표준오차 sqrt(p(1-p)/n)."""
        return math.sqrt(p * (1 - p) / self.runs) if self.runs else 0.0


def run_seed(seed: int, run: int) -> int:
    return seed * _SEED_STRIDE + run


def run_beta(obs, roster, model, beta, runs, seed, *, initial_mode="replay", obs_status=None,
             seeds=None) -> BetaSummary:
    if runs < 1:
        raise RebetaError("--runs 는 1 이상이어야 합니다.")
    obs_status = obs_status if obs_status is not None else build_observed_status(obs)
    seeds = seeds if seeds is not None else resolve_seeds(obs, None)[0]
    counts, first_new = [], []
    hits = {a: 0 for a in obs.agents}
    for r in range(runs):
        res = simulate_once(obs, roster, model, beta, run_seed(seed, r), initial_mode=initial_mode,
                            obs_status=obs_status, seeds=seeds)
        counts.append(res.final_count)
        for a in res.infected:
            hits[a] += 1
        if res.first_new_infection_minutes is not None:
            first_new.append(res.first_new_infection_minutes)
    median_low = sorted(counts)[(len(counts) - 1) // 2]
    rep = counts.index(median_low)
    return BetaSummary(beta, runs, counts, {a: hits[a] / runs for a in obs.agents}, first_new, rep)


# ── 출력 ─────────────────────────────────────────────────────────────────────

def fmt_beta(b: float) -> str:
    return f"{b:g}"


def _header_lines(obs: Observed) -> list[str]:
    return [f"# {line}" for line in ASSUMPTION_LINES] + [
        f"# 관찰 기간: {obs.observed_days:.2f}일 (wave {obs.waves[0]}~{obs.waves[-1]}), "
        f"에이전트 {len(obs.agents)}명(CSV 에 나온 사람만)"]


def write_summary(path, obs: Observed, summaries: list[BetaSummary], *, seed: int,
                  original_beta: float | None) -> None:
    n = len(obs.agents)
    cols = (["beta", "is_original_beta", "runs", "seed", "observed_days",
             "mean_final_infected", "se_mean_final_infected", "std_final_infected",
             "min_final_infected", "max_final_infected"]
            + [f"p_final_{k}" for k in range(n + 1)]
            + [c for a in obs.agents for c in (f"p_infected_{a}", f"se_infected_{a}")]
            + ["runs_with_new_infection", "mean_first_new_infection_minutes",
               "observed_final_infected", "p_reproduce_observed", "se_reproduce_observed"])
    obs_final = obs.observed_final_infected
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for line in _header_lines(obs):
            fh.write(line + "\n")
        w = csv.writer(fh)
        w.writerow(cols)
        for s in summaries:
            is_orig = original_beta is not None and math.isclose(s.beta, original_beta)
            p_rep = s.p_count(obs_final)
            w.writerow([fmt_beta(s.beta), str(is_orig).lower(), s.runs, seed, f"{obs.observed_days:.4f}",
                        f"{s.mean:.4f}", f"{s.se_mean:.4f}", f"{s.std:.4f}", min(s.counts), max(s.counts)]
                       + [f"{s.p_count(k):.4f}" for k in range(n + 1)]
                       + [v for a in obs.agents
                          for v in (f"{s.per_agent[a]:.4f}", f"{s.se_p(s.per_agent[a]):.4f}")]
                       + [len(s.first_new),
                          f"{statistics.fmean(s.first_new):.1f}" if s.first_new else "",
                          obs_final, f"{p_rep:.4f}", f"{s.se_p(p_rep):.4f}"])


def write_waves(path, obs: Observed, rep: RunResult, seed_used: int) -> None:
    extra = ["is_imputed", "sim_status", "sim_is_infectious", "sim_contact_location",
             "sim_t_d_minutes", "sim_new_exposure", "sim_p_infect"]
    cols = list(obs.fieldnames) + [c for c in extra if c not in obs.fieldnames]
    by = {(r["wave"], r["agent"]): r for r in rep.waves}
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for line in _header_lines(obs):
            fh.write(line + "\n")
        fh.write(f"# 대표 run: seed={seed_used} (최종 감염자 수가 하위 중앙값인 run 중 가장 작은 번호), "
                 f"최종 감염자 {rep.final_count}명\n")
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        for wv in obs.waves:
            orig = {r["agent"]: r for r in obs.rows[wv]}
            template = obs.rows[wv][0]
            order = [r["agent"] for r in obs.rows[wv]] + [a for a in obs.agents if a not in orig]
            for a in order:
                sim = by.get((wv, a))
                if sim is None:
                    continue                          # 아직 등장 전(명단에 없음)
                if a in orig:
                    row = dict(orig[a])
                else:
                    row = {c: "" for c in obs.fieldnames}
                    for c in ("wave", "wave_start_time", "wave_end_time",
                              "elapsed_minutes_start", "elapsed_minutes_end"):
                        if c in template:
                            row[c] = template[c]
                    row.update(agent=a, location=sim["location"], is_exterior=str(sim["is_exterior"]).lower())
                row.update(is_imputed=str(sim["is_imputed"]).lower(), sim_status=sim["sim_status"],
                           sim_is_infectious=str(sim["sim_status"] in _CARRIER_STATES).lower(),
                           sim_contact_location=sim["sim_contact_location"],
                           sim_t_d_minutes=sim["sim_t_d_minutes"],
                           sim_new_exposure=str(sim["sim_new_exposure"]).lower(),
                           sim_p_infect=f"{sim['sim_p_infect']:.6f}")
                wr.writerow(row)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _run_cli(args) -> int:
    if args.runs < 1:
        raise RebetaError("--runs 는 1 이상이어야 합니다.")
    if any(b < 0 or not math.isfinite(b) for b in args.beta):
        raise RebetaError(f"--beta 는 0 이상의 유한한 값이어야 합니다: {args.beta}")
    obs = load_location_csv(args.csv)
    obs.time_mode, obs.time_per_wave = load_time_config(args.scenario)
    model = load_infection_model(args.scenario)
    seed_events = load_seed_events(args.scenario)
    warnings: list[str] = []
    if model.get("enabled") is False:
        warnings.append("시나리오의 감염 모델이 꺼져 있습니다(enabled=false) — 원래 실행에서는 감염 판정이 "
                        "없었으니 sanity check 는 의미가 없습니다.")
    last = obs.waves[-1]
    if obs.time_mode is None:
        warnings.append("시나리오에 time_mode 가 없어 variable 규칙(CSV 의 종료 경과분)으로 접촉 시간을 계산합니다.")
    if not obs.fixed_time and obs.end.get(last) is None:
        warnings.append(f"마지막 wave {last} 의 종료 경과분이 없어 그 wave 의 접촉 시간을 0 으로 둡니다"
                        f"(시간 누적 전에 끝난 실행).")
    if obs.blank_status_rows:
        warnings.append(f"infection_status 가 빈 행 {obs.blank_status_rows}개를 S 로 취급했습니다.")
    seeds, seed_warnings, source = resolve_seeds(obs, seed_events)
    warnings += seed_warnings

    original = model.get("beta")
    original = float(original) if isinstance(original, (int, float)) and not isinstance(original, bool) else None
    betas = list(dict.fromkeys(args.beta))
    added = original is not None and not any(math.isclose(b, original) for b in betas)
    if added:
        betas.append(original)
    roster = build_roster(obs, backfill=args.backfill)
    obs_status = build_observed_status(obs, backfill=args.backfill)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(args.csv).stem

    logging.getLogger("ABM.simulation.infection").setLevel(logging.WARNING)
    summaries = []
    for b in betas:
        s = run_beta(obs, roster, model, b, args.runs, args.seed, initial_mode=args.initial_mode,
                     obs_status=obs_status, seeds=seeds)
        summaries.append(s)
        rep = simulate_once(obs, roster, model, b, run_seed(args.seed, s.representative_run),
                            initial_mode=args.initial_mode, obs_status=obs_status, seeds=seeds,
                            record=True)
        write_waves(out / f"{stem}_rebeta_beta{fmt_beta(b)}_waves.csv", obs, rep,
                    run_seed(args.seed, s.representative_run))
    write_summary(out / f"{stem}_rebeta_summary.csv", obs, summaries, seed=args.seed, original_beta=original)

    n = len(obs.agents)
    print(f"β 재계산 — {Path(args.csv).name}")
    for line in ASSUMPTION_LINES:
        print(f"  * {line}")
    tm = (f"fixed {obs.time_per_wave}분/wave" if obs.fixed_time else (obs.time_mode or "모름(variable 규칙)"))
    print(f"  * 관찰 기간 {obs.observed_days:.2f}일 · 에이전트 {n}명({', '.join(obs.agents)}) · "
          f"runs={args.runs} · seed={args.seed} · 시드 경과: {args.initial_mode} · 시간: {tm}")
    print(f"  * 시드: {', '.join(f'{s.agent}(wave {s.wave}, {s.start_status})' for s in seeds) or '없음'} "
          f"— {source[0]}")
    for wmsg in warnings:
        print(f"  ⚠ {wmsg}")
    print(f"\n최종 감염자(관찰 기간 {obs.observed_days:.2f}일 끝까지) 분포 — 평균 ± 표준오차")
    print(f"{'β':>8} {'평균±SE':>14} {'표준편차':>8} " + " ".join(f"{'P('+str(k)+')':>7}" for k in range(n + 1)))
    for s in summaries:
        tag = ""
        if original is not None and math.isclose(s.beta, original):
            tag = "  ← 원래 β" + (" (sanity check 용으로 추가)" if added else "")
        print(f"{fmt_beta(s.beta):>8} {s.mean:>7.3f}±{s.se_mean:<6.3f} {s.std:>8.3f} "
              + " ".join(f"{s.p_count(k):>7.3f}" for k in range(n + 1)) + tag)
    print("\n에이전트별 감염 확률 ± 표준오차")
    print(f"{'β':>8} " + " ".join(f"{a:>16}" for a in obs.agents))
    for s in summaries:
        print(f"{fmt_beta(s.beta):>8} " + " ".join(
            f"{s.per_agent[a]:>9.3f}±{s.se_p(s.per_agent[a]):<6.3f}" for a in obs.agents))
    if original is not None:
        s0 = next(s for s in summaries if math.isclose(s.beta, original))
        obs_final = obs.observed_final_infected
        p = s0.p_count(obs_final)
        print(f"\nSanity check: 원래 β={fmt_beta(original)} 에서 관찰된 최종 감염자 {obs_final}명이 재현될 확률 "
              f"= {p:.3f} ± {s0.se_p(p):.3f}")
        if p < 0.05:
            print("  ⚠ 경고: 5% 미만 — 접촉 재구성이나 시드 처리 방식이 원래 실행과 크게 다를 수 있습니다.")
    print(f"\n출력: {out / (stem + '_rebeta_summary.csv')} 외 β 별 waves CSV {len(summaries)}개")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ABM.tools.rebeta", description=__doc__.split("\n\n")[0])
    ap.add_argument("csv", help="위치 이력 CSV(_locations.csv)")
    ap.add_argument("scenario", help="시나리오 JSON(config.infection_model) 또는 infection_model JSON")
    ap.add_argument("--beta", type=float, nargs="+", required=True)
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=".")
    ap.add_argument("--initial-mode", choices=("replay", "sample"), default="replay")
    ap.add_argument("--backfill", action="store_true",
                    help="첫 행 이전 wave 에도 첫 위치로 그 에이전트를 채운다(기본: 부재)")
    args = ap.parse_args(argv)
    try:
        return _run_cli(args)
    except RebetaError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
