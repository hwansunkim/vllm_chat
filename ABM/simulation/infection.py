"""결정론적 감염병 모델(SEIR/SEIRS).

설계 원칙 — **LLM은 감염 여부를 절대 판단하지 않는다.**
엔진이 매 wave 접촉(같은 wave에 같은 장소에 있었는가)만 보고 상태 전이를 계산하고,
그 결과를 오직 "증상 서사 텍스트"로만 에이전트에게 알린다. status·확률·경과 시간 같은
raw 값은 어떤 경로로도 프롬프트에 들어가지 않는다.

## 모형 — SEIR (연구 스펙 반영, 2026-09)

- **S (감염 가능)** — 아직 노출된 적 없음(또는 SEIRS에서 재감염 가능 상태로 복귀).
- **E (잠복기)** — 노출됐지만 아직 남을 감염시키지 못한다. 지속 시간은 **일 단위**
  잘린 감마분포 `Gamma(k=1.926, theta=1.775)`를 `[1, 10]`일로 절단한 뒤 반올림해
  뽑는다(`_sample_incubation_minutes` — 연구팀 Julia 코드 `Print_Duration_E`와
  동일 파라미터).
- **I (감염기)** — 남을 감염시킬 수 있다. 지속 시간은 **8일 고정**
  (`_INFECTIOUS_FIXED_DAYS` — Julia `Print_Duration_I`와 동일).
- **R (회복기)** — `immune_after_recovery=True`면 종결(SIR 방식, 재감염 불가),
  `False`면 S로 복귀해 재감염 가능(SEIRS).

두 지속 시간 모두 **노출 시점에 한 번만** 뽑혀 그 뒤로는 결정론적으로 흐른다(기존
회복 모델과 같은 철학) — wave 길이가 들쭉날쭉해도(취침 7시간 vs 식사 5분) 개인별
이환 기간은 항상 같은 시간 척도로 유지된다.

## 전염 확률 — Monte Carlo (연구 스펙)

접촉한(같은 wave·같은 장소) 감염기(I) 1명과 비감염(S) 1명 쌍마다 독립적으로 판정한다.

    λ_ij = β × t_d
    P = 1 - exp(-λ)
    rn < P 면 감염(E로 전이)

`β`는 사용자가 설정하는 옵션(기본 0.04). `t_d`(접촉 시간, **일 단위**)는 "지난 감염
판정 이후 실제로 경과한 시간"이다 — 이 엔진은 wave마다 실제 경과 시간(분)이 들쭉날쭉
하므로(가변 시간 모드: 식사 장면 5분 vs 취침 장면 7시간), 그 경과 시간을 일 단위로
환산해 접촉 시간으로 쓴다(사용자 확정 사항). 최초 판정(직전 판정 시각이 없음)은
t_d=0으로 보아 전염을 걸지 않는다 — 시작하자마자 감염되는 부자연스러움을 막는다.

시간 축 — **전염만 wave 기준, 병의 진행(E→I, I→R)은 시간 기준.**
전염은 "이번 wave에 같은 장소에 있었는가"라는 접촉 사건이므로 wave당(정확히는 그
wave까지의 경과 시간 기준) 확률로 판정한다. 반면 잠복기·감염기 progression은 감염
후 경과 분(`_current_elapsed_minutes`)으로 판정한다.

접촉 판정은 `_compute_wave_targets`(내러티브 가시성 — 아는 사람/낯선 사람 구분, 대화 상대
노출용)를 재사용하지 **않는다**. 감염은 "누구를 알아보는가"와 무관하게 순수한 물리적 공존의
문제이므로 `_agent_location` 값만으로 그룹핑한다. 다만 외부 공간(`is_exterior`)은
`_compute_wave_targets`가 "아무도 보이지 않음"으로 처리하는 것과 일관되게 접촉 계산에서도
통째로 제외한다(외부 공간에 함께 있어도 접촉으로 치지 않는다).
"""
import logging
import math
import random

logger = logging.getLogger(__name__)

# 감염 상태 코드
_S = "S"  # Susceptible — 감염 가능
_E = "E"  # Exposed     — 잠복기(비전염)
_I = "I"  # Infectious  — 감염기(전염 가능)
_R = "R"  # Recovered   — 회복(면역 또는 재감염 가능)

# ── 잠복기(E)·감염기(I) 지속 시간 — 연구 스펙 상수, 사용자 설정 대상 아님 ─────────
# (β만 옵션으로 노출된다 — InfectionModelConfig 참고)
_INCUBATION_GAMMA_K:     float = 1.926
_INCUBATION_GAMMA_THETA: float = 1.775
_INCUBATION_MIN_DAYS:    float = 1.0
_INCUBATION_MAX_DAYS:    float = 10.0
_INFECTIOUS_FIXED_DAYS:  int   = 8
_MINUTES_PER_DAY:        int   = 1440


def _sample_incubation_minutes() -> int:
    """잠복기(E) 지속 시간(분). `Gamma(k, theta)`를 `[1, 10]`일로 절단 후 반올림.

    Julia `truncated(Gamma(k, theta), 1, 10)`과 동일한 의미 — 연속분포 표본이
    구간 안에 들 때까지 다시 뽑고(거부 표집), 그 값을 반올림한다(먼저 반올림한
    값이 범위 안인지 보는 것과 미묘하게 다르다 — 절단이 연속값 기준이어야
    원 분포의 형태를 유지한다).
    """
    while True:
        days = random.gammavariate(_INCUBATION_GAMMA_K, _INCUBATION_GAMMA_THETA)
        if _INCUBATION_MIN_DAYS <= days <= _INCUBATION_MAX_DAYS:
            return round(days) * _MINUTES_PER_DAY


def _infectious_duration_minutes() -> int:
    """감염기(I) 지속 시간(분) — 8일 고정."""
    return _INFECTIOUS_FIXED_DAYS * _MINUTES_PER_DAY


class _InfectionMixin:
    """접촉 기반 상태 전이 계산 + 증상 서사 텍스트 생성."""

    # ── 상태 조회/갱신 ────────────────────────────────────────────────────────

    def _infection_entry(self, agent_key: str) -> dict:
        """에이전트의 감염 상태 dict. 없으면 초기값(S)으로 만들어 반환."""
        entry = self._agent_infection.get(agent_key)
        if entry is None:
            entry = {
                "status":                _S,
                "infected_at_minutes":   None,
                "infectious_at_minutes": None,
                "recover_at_minutes":    None,
                "recovered_wave":        None,
                "recovered_at_minutes":  None,
                "notify_recovery":       False,
            }
            self._agent_infection[agent_key] = entry
        return entry

    def _set_infected(
        self, agent_key: str, wave: int, cause: str, at_minutes: int | None = None,
    ) -> bool:
        """에이전트를 노출(E) 상태로 전이. 실제로 전이됐으면 True.

        이미 S가 아니면(E/I 중이거나 SIR에서 면역 R) 아무 일도 하지 않는다.
        `at_minutes`를 생략하면 이 wave의 경과분을 쓴다.

        `infectious_at_minutes`(E→I까지 걸리는 시간)와 `recover_at_minutes`
        (E→R까지 걸리는 총 시간, 즉 잠복기+감염기)는 **노출 시점의 경과분 기준
        델타**로 저장한다 — 절대 시각이 아니므로 재개(export/restore) 시 앵커
        재기준화 대상이 아니다(기존 `recover_at_minutes`와 같은 규칙).
        """
        if agent_key not in self.agents:
            logger.warning(f"infection: 알 수 없는 에이전트 '{agent_key}'")
            return False
        entry = self._infection_entry(agent_key)
        if entry["status"] != _S:
            return False
        now = self._current_elapsed_minutes(wave) if at_minutes is None else at_minutes
        incubation = _sample_incubation_minutes()
        infectious = _infectious_duration_minutes()
        entry["status"]                = _E
        entry["infected_at_minutes"]   = now
        entry["infectious_at_minutes"] = incubation
        entry["recover_at_minutes"]    = incubation + infectious
        entry["recovered_wave"]        = None
        entry["recovered_at_minutes"]  = None
        entry["notify_recovery"]       = False
        self._emit("infection_update", {
            "wave":            wave,          # UI 타임라인용 — 여전히 wave 축으로 그린다
            "elapsed_minutes": now,           # 시간 축 표시용
            "agent":           agent_key,
            "display_name":    self._key_to_alias.get(agent_key, agent_key),
            "status":          _E,
            "cause":           cause,         # "event" | "transmission"
            "disease_name":    self._infection_disease_name,
        })
        logger.info(
            f"[노출] {agent_key} ← {cause} (wave {wave}, {now}분, "
            f"잠복 {incubation}분 · 총 {incubation + infectious}분 뒤 회복)"
        )
        return True

    def _set_infectious(self, agent_key: str, wave: int, at_minutes: int | None = None) -> None:
        """잠복(E) → 감염기(I) 전이. 이때부터 남을 감염시킬 수 있다."""
        entry = self._infection_entry(agent_key)
        now = self._current_elapsed_minutes(wave) if at_minutes is None else at_minutes
        entry["status"] = _I
        self._emit("infection_update", {
            "wave":            wave,
            "elapsed_minutes": now,
            "agent":           agent_key,
            "display_name":    self._key_to_alias.get(agent_key, agent_key),
            "status":          _I,
            "cause":           "progression",   # 잠복기 종료 — 접촉 전파가 아니라 자연 진행
            "disease_name":    self._infection_disease_name,
        })
        logger.info(f"[감염성 획득] {agent_key} (wave {wave}, {now}분)")

    def _set_recovered(self, agent_key: str, wave: int, at_minutes: int | None = None) -> None:
        """감염기(I) → 회복. immune_after_recovery에 따라 R(면역) 또는 S(재감염 가능)."""
        entry = self._infection_entry(agent_key)
        now = self._current_elapsed_minutes(wave) if at_minutes is None else at_minutes
        new_status = _R if self._infection_immune else _S
        entry["status"]                = new_status
        entry["infected_at_minutes"]   = None
        entry["infectious_at_minutes"] = None
        entry["recover_at_minutes"]    = None
        entry["recovered_wave"]        = wave
        entry["recovered_at_minutes"]  = now
        entry["notify_recovery"]       = True
        self._emit("infection_update", {
            "wave":            wave,
            "elapsed_minutes": now,
            "agent":           agent_key,
            "display_name":    self._key_to_alias.get(agent_key, agent_key),
            "status":          new_status,
            "cause":           "recovery",
            "disease_name":    self._infection_disease_name,
        })
        logger.info(f"[회복] {agent_key} → {new_status} (wave {wave}, {now}분)")

    # ── 접촉 계산 ────────────────────────────────────────────────────────────

    def _compute_contact_groups(self) -> list[list[str]]:
        """이번 wave에 같은 장소에 있던 활성 에이전트 그룹 목록.

        `_agent_location` 값으로 그룹핑하고, 외부 공간은 제외한다. 2명 미만인 그룹은
        접촉이 성립하지 않으므로 버린다.

        위치가 미설정(`""`)인 에이전트는 모든 그룹에 함께 낀다 — `_resolve_targets._same_loc`
        (`targets.py`)과 `_compute_wave_targets`(`location.py`)가 둘 다 "화자 자신의 위치가
        비어 있으면 항상 매치"로 취급해 대화 상대로 노출하는 것과 같은 규칙이다. 이 규칙을
        안 따르면, 위치를 섞어 쓰는 시나리오에서 위치 미설정 에이전트는 누구와도 대화하면서
        절대 감염되지도 감염시키지도 않는 모순이 생긴다(전원 위치 미설정인 순수 레거시
        시나리오는 원래도 단일 버킷이라 이 규칙과 무관하게 정상 동작한다).
        """
        buckets:  dict[str, list[str]] = {}
        floaters: list[str] = []
        for key in sorted(self.active_agents):
            loc = self._agent_location.get(key, "")
            if loc in self._exterior_locations:
                continue  # 외부 공간 — 서로 접촉하지 않음
            if loc:
                buckets.setdefault(loc, []).append(key)
            else:
                floaters.append(key)

        if not buckets:
            # 전원 위치 미설정 — 암묵적 단일 공간(순수 레거시 시나리오).
            return [floaters] if len(floaters) >= 2 else []

        groups = [group + floaters for group in buckets.values()]
        return [group for group in groups if len(group) >= 2]

    # ── 매 wave 모델 적용 ─────────────────────────────────────────────────────

    def _apply_infection_wave(self, run_wave: int, disp_wave: int | None = None) -> None:
        """이번 wave의 전염 + 잠복기/감염기 진행 판정. 이동(move_to) 반영 **이후**에 호출할 것.

        `run_wave` = per-run 카운터 → 경과 시간(`now`) 계산 전용.
        `disp_wave` = 누적 표시 wave → `infection_update` 이벤트·`recovered_wave` 라벨용.
        생략하면 `run_wave`를 그대로 라벨로 쓴다(단위 테스트 하위 호환).

        전염·진행 모두 이번 wave 시작 시점의 명단(`infectious_now`/`exposed_now`)을
        기준으로 판정한다 — 이번 wave에 갓 전이된 사람이 같은 wave 안에서 곧바로
        2차 전파를 일으키거나 다음 단계로 넘어가는 순서 의존성을 없애기 위함.
        """
        if not self._infection_enabled:
            return

        if disp_wave is None:
            disp_wave = run_wave
        now = self._current_elapsed_minutes(run_wave)

        # 접촉 시간(t_d, 일 단위) — 지난 판정 이후 실제로 경과한 시간을 일로 환산한다
        # (가변 시간 모드에서 식사 5분 vs 취침 7시간처럼 wave마다 실제 경과가 다르므로).
        # 첫 판정(직전 기준 시각이 없음)은 t_d=0으로 보아 전염을 걸지 않는다 —
        # 시작하자마자 감염되는 부자연스러움을 막는다. /continue로 원점이 되감기면
        # rebase_infection_anchors가 이 기준을 초기화해 같은 규칙이 다시 적용된다.
        last_check = self._infection_last_check_minutes
        t_d_days = max(0, now - last_check) / _MINUTES_PER_DAY if last_check is not None else 0.0
        self._infection_last_check_minutes = now

        infectious_now = {
            key for key in self.active_agents
            if self._infection_entry(key)["status"] == _I
        }
        exposed_now = {
            key for key in self.active_agents
            if self._infection_entry(key)["status"] == _E
        }

        # 1) 전염 — Monte Carlo: λ = β × t_d, P = 1 - exp(-λ). 같은 장소 그룹 안의
        #    (감염기 × 감염가능) 쌍마다 독립 판정한다.
        if infectious_now and self._infection_beta > 0.0 and t_d_days > 0.0:
            lam = self._infection_beta * t_d_days
            p = 1.0 - math.exp(-lam)
            newly_exposed: set[str] = set()
            for group in self._compute_contact_groups():
                carriers = [k for k in group if k in infectious_now]
                if not carriers:
                    continue
                for key in group:
                    if key in infectious_now or key in newly_exposed:
                        continue
                    if self._infection_entry(key)["status"] != _S:
                        continue  # E/I 는 이미 진행 중, R(면역)은 SEIR에서 재감염 안 됨
                    for _ in carriers:
                        if random.random() < p:
                            if self._set_infected(key, disp_wave, "transmission", at_minutes=now):
                                newly_exposed.add(key)
                            break  # 이미 노출 — 남은 감염기와의 판정은 무의미

        # 2) 진행 — E→I(잠복기 종료), I→R(감염기 종료). 둘 다 주사위를 굴리지 않는다:
        #    노출 시점에 뽑아둔 델타(`infectious_at_minutes`/`recover_at_minutes`)에
        #    도달했는지만 본다.
        for key in sorted(exposed_now):
            entry  = self._infection_entry(key)
            since  = entry.get("infected_at_minutes")
            target = entry.get("infectious_at_minutes")
            if isinstance(since, int) and isinstance(target, int) and now - since >= target:
                self._set_infectious(key, disp_wave, at_minutes=now)

        for key in sorted(infectious_now):
            entry  = self._infection_entry(key)
            since  = entry.get("infected_at_minutes")
            target = entry.get("recover_at_minutes")
            if isinstance(since, int) and isinstance(target, int) and now - since >= target:
                self._set_recovered(key, disp_wave, at_minutes=now)

    # ── 경과분 앵커 재기준화 (/continue 전용) ────────────────────────────────────
    def rebase_infection_anchors(self, now: int | None = None) -> None:
        """리셋으로 '지금'이 되감겼으면 감염 앵커를 새 원점 기준으로 옮긴다.

        `now`는 **리셋 전의 '지금'**(총 경과 분)이다. 생략하면 현재
        `completed_waves`로부터 계산하므로, 그때는 반드시 `completed_waves = 0`
        **직전에** 호출해야 한다. `/continue`처럼 경과를 `_elapsed_minutes`로 접는
        경로는 접기 전 값을 명시적으로 넘기고 리셋 뒤에 호출한다 — 접은 뒤에
        인자 없이 부르면 `now`가 접힌 값을 한 번 더 세어 앵커가 두 배로 밀린다.

        **정상 동작 시 이 함수는 두 시간 모드 모두에서 no-op이다.** `/continue`가
        리셋 직전에 경과를 `_elapsed_minutes`로 접게 고쳐진 뒤로(m4), fixed 모드도
        `_current_elapsed_minutes = _elapsed_minutes + wave*time_per_wave`라 리셋
        전후의 '지금'이 같아 아래 `now == base` 가드에 걸린다 — variable 모드가
        원래 그랬던 것과 동일.

        남겨두는 이유: `_elapsed_minutes` 접기를 빠뜨린 새 재개 경로가 생기면
        (또는 fixed 분기가 다시 wave 전용으로 회귀하면) `infected_at_minutes`가
        옛 기준에 남아 `now - since`가 갑자기 작아지고(심하면 음수) 진행 판정이
        앞 단계로 되감긴다 — 위치/외모가 재개 시 초기화되던 것과 같은 버그
        클래스라, 이 방어선이 그 회귀를 조용히 흡수한다. `infectious_at_minutes`·
        `recover_at_minutes`는 노출 시점부터의 **델타**라 앵커와 무관하게 그대로
        둔다(재기준화 불필요).

        `_infection_last_check_minutes`(전염 판정용 접촉 시간 t_d 기준점)도 원점이
        실제로 밀렸다면 함께 초기화한다 — 옛 원점 기준 절대값을 그대로 두면 다음
        판정에서 t_d가 실제보다 훨씬 크게(또는 음수로) 계산된다. 초기화하면 다음
        판정이 t_d=0(전염 없음)부터 다시 시작하므로 안전하다(과대/과소 계산보다
        한 번의 "판정 건너뜀"이 낫다).

        `/load`·`/resume`은 새 프로세스에서 export/restore를 거치며 같은
        재기준화를 하므로 이 함수와 무관하다.
        """
        if now is None:
            now = self._current_elapsed_minutes(self.completed_waves)
        base = self._current_elapsed_minutes(0)   # 리셋 직후의 '지금'
        if now == base:
            return
        for entry in self._agent_infection.values():
            since = entry.get("infected_at_minutes")
            if entry.get("status") in (_E, _I) and isinstance(since, int):
                entry["infected_at_minutes"] = base - max(0, now - since)
        self._infection_last_check_minutes = None

    # ── 증상 서사 ────────────────────────────────────────────────────────────

    def _find_symptom_stage(self, elapsed_minutes: int) -> dict | None:
        """감염 후 경과 분이 속한 증상 단계. 범위를 벗어나면 가장 늦은 단계를 유지.

        `elapsed_minutes`는 노출(E 진입) 시점부터의 경과라 잠복기·감염기를 가리지
        않는다 — 초기 구간(잠복기)에 "아직 증상 없음" 서사를, 후기 구간(감염기)에
        본격적인 증상 서사를 배치하는 건 시나리오 작성자가 `symptom_stages`의
        `min_minutes`/`max_minutes` 경계로 표현한다(엔진은 그 경계만 조회한다).
        """
        if not self._infection_stages:
            return None
        for stage in self._infection_stages:
            if stage["min_minutes"] <= elapsed_minutes <= stage["max_minutes"]:
                return stage
        # 범위 밖 — 정의된 최대 구간보다 더 지났다면 마지막(가장 늦은) 단계를 계속 보여준다.
        latest = max(self._infection_stages, key=lambda s: s["max_minutes"])
        if elapsed_minutes > latest["max_minutes"]:
            return latest
        return None  # 아직 첫 단계 이전(예: min_minutes=60인데 경과 0) — 증상 없음

    def _consume_recovery_notice(self, agent_key: str) -> None:
        """회복 안내 플래그를 실제로 내린다 — 턴이 성공한 뒤에만 호출할 것.

        `_build_symptom_context`는 이제 읽기 전용 컨텍스트 조회에서도 불릴 수 있어
        플래그를 직접 못 내린다(부작용 없음이 `_assemble_agent_prompt`의 계약). 턴이
        LLM 실패로 롤백되면 이 메서드가 호출되지 않으므로 플래그가 그대로 남아
        다음 성공한 턴에 회복 안내가 다시 뜬다 — 안내 자체가 유실되지 않는다.
        """
        entry = self._agent_infection.get(agent_key)
        if entry:
            entry["notify_recovery"] = False

    def _build_symptom_context(self, agent_key: str, wave: int | None = None) -> str | None:
        """이 에이전트가 이번 턴에 볼 증상/회복 서사. 없으면 None.

        raw status·경과 시간·확률은 절대 포함하지 않는다 — 오직 시나리오가 작성한
        `symptom_text`(그리고 회복 안내 한 줄)만 반환한다. E(잠복기)도 I(감염기)와
        똑같이 `symptom_stages`를 조회한다 — "무증상 잠복기"는 시나리오가 해당
        구간에 symptom_text를 비워두거나 "특별한 증상 없음" 류로 적어 표현한다.
        """
        if not self._infection_enabled:
            return None
        entry = self._agent_infection.get(agent_key)
        if not entry:
            return None

        if entry.get("notify_recovery"):
            # 여기서는 플래그를 읽기만 한다(내리지 않는다) — 이 함수는 이제
            # `_assemble_agent_prompt`(부작용 없음이 계약)를 통해 읽기 전용 컨텍스트
            # 조회(GET .../context)에서도 호출되므로, 여기서 소비하면 실제 턴 없이
            # 조회만 해도 회복 안내가 사라진다. 실제 소비는 턴이 성공한 뒤
            # `_consume_recovery_notice()`(step.py._step_agent)가 담당한다.
            disease = self._infection_disease_name
            what    = f"{disease} 증상이" if disease else "몸의 증상이"
            return f"[몸 상태]\n{what} 씻은 듯이 가셨다. 몸이 다시 가뿐하다."

        if entry["status"] not in (_E, _I):
            return None
        since = entry.get("infected_at_minutes")
        if not isinstance(since, int):
            return None
        elapsed = max(0, self._current_elapsed_minutes(wave) - since)
        stage   = self._find_symptom_stage(elapsed)
        if not stage or not stage.get("symptom_text"):
            return None
        return f"[몸 상태]\n{stage['symptom_text']}"
