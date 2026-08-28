"""결정론적 감염병 모델(SIR/SIS).

설계 원칙 — **LLM은 감염 여부를 절대 판단하지 않는다.**
엔진이 매 wave 접촉(같은 wave에 같은 장소에 있었는가)만 보고 상태 전이를 계산하고,
그 결과를 오직 "증상 서사 텍스트"로만 에이전트에게 알린다. status·확률·경과 시간 같은
raw 값은 어떤 경로로도 프롬프트에 들어가지 않는다.

시간 축 — **전염만 wave 기준, 병의 진행은 시간 기준.**
전염은 "이번 wave에 같은 장소에 있었는가"라는 접촉 사건이므로 wave당 확률로 판정한다.
반면 증상 단계 progression과 회복은 감염 후 **시뮬레이션 내 경과 분**
(`_current_elapsed_minutes`)으로 판정한다 — 5분짜리 식사 장면과 7시간짜리 취침 장면이
병의 진행에 똑같이 1 wave로 기여하면 안 되기 때문. 회복 확률(wave당 주사위)도 폐기하고,
감염 시점에 회복까지 걸릴 시간을 [min, max]분에서 한 번 균등 샘플해 그 시간이 지나면
회복시킨다.

접촉 판정은 `_compute_wave_targets`(내러티브 가시성 — 아는 사람/낯선 사람 구분, 대화 상대
노출용)를 재사용하지 **않는다**. 감염은 "누구를 알아보는가"와 무관하게 순수한 물리적 공존의
문제이므로 `_agent_location` 값만으로 그룹핑한다. 다만 외부 공간(`is_exterior`)은
`_compute_wave_targets`가 "아무도 보이지 않음"으로 처리하는 것과 일관되게 접촉 계산에서도
통째로 제외한다(외부 공간에 함께 있어도 접촉으로 치지 않는다).
"""
import logging
import random

logger = logging.getLogger(__name__)

# 감염 상태 코드
_S = "S"  # Susceptible — 감염 가능
_I = "I"  # Infected    — 감염 중
_R = "R"  # Recovered   — 회복(면역)


class _InfectionMixin:
    """접촉 기반 상태 전이 계산 + 증상 서사 텍스트 생성."""

    # ── 상태 조회/갱신 ────────────────────────────────────────────────────────

    def _infection_entry(self, agent_key: str) -> dict:
        """에이전트의 감염 상태 dict. 없으면 초기값(S)으로 만들어 반환."""
        entry = self._agent_infection.get(agent_key)
        if entry is None:
            entry = {
                "status":               _S,
                "infected_at_minutes":  None,
                "recover_at_minutes":   None,
                "recovered_wave":       None,
                "recovered_at_minutes": None,
                "notify_recovery":      False,
            }
            self._agent_infection[agent_key] = entry
        return entry

    def _sample_recovery_minutes(self) -> int | None:
        """이번 감염에서 회복까지 걸릴 시간(분). 자연 회복이 없으면 None.

        감염 **시점에 한 번만** 뽑는다 — wave마다 주사위를 굴리던 옛
        `recovery_probability` 모델과 달리, 개인별 이환 기간이 감염 순간에 결정되고
        그 뒤로는 결정론적으로 흐른다. 그래서 wave 길이가 들쭉날쭉해도(취침 7시간
        vs 식사 5분) 앓는 기간은 항상 같은 시간 척도로 유지된다.
        """
        hi = self._infection_recovery_max
        if hi <= 0:
            return None  # 만성 — 자연 회복 없음
        lo = min(self._infection_recovery_min, hi)
        return random.randint(lo, hi)

    def _set_infected(
        self, agent_key: str, wave: int, cause: str, at_minutes: int | None = None,
    ) -> bool:
        """에이전트를 감염(I) 상태로 전이. 실제로 전이됐으면 True.

        이미 I이거나 (SIR에서) 면역 R이면 아무 일도 하지 않는다.
        `at_minutes`를 생략하면 이 wave의 경과분을 쓴다.
        """
        if agent_key not in self.agents:
            logger.warning(f"infection: 알 수 없는 에이전트 '{agent_key}'")
            return False
        entry = self._infection_entry(agent_key)
        if entry["status"] != _S:
            return False
        now = self._current_elapsed_minutes(wave) if at_minutes is None else at_minutes
        recover_at = self._sample_recovery_minutes()
        entry["status"]               = _I
        entry["infected_at_minutes"]  = now
        entry["recover_at_minutes"]   = recover_at
        entry["recovered_wave"]       = None
        entry["recovered_at_minutes"] = None
        entry["notify_recovery"]      = False
        self._emit("infection_update", {
            "wave":            wave,          # UI 타임라인용 — 여전히 wave 축으로 그린다
            "elapsed_minutes": now,           # 시간 축 표시용
            "agent":           agent_key,
            "display_name":    self._key_to_alias.get(agent_key, agent_key),
            "status":          _I,
            "cause":           cause,         # "event" | "transmission"
            "disease_name":    self._infection_disease_name,
        })
        logger.info(f"[감염] {agent_key} ← {cause} (wave {wave}, {now}분, 회복까지 {recover_at}분)")
        return True

    def _set_recovered(self, agent_key: str, wave: int, at_minutes: int | None = None) -> None:
        """감염(I) → 회복. immune_after_recovery에 따라 R(면역) 또는 S(재감염 가능)."""
        entry = self._infection_entry(agent_key)
        now = self._current_elapsed_minutes(wave) if at_minutes is None else at_minutes
        new_status = _R if self._infection_immune else _S
        entry["status"]               = new_status
        entry["infected_at_minutes"]  = None
        entry["recover_at_minutes"]   = None
        entry["recovered_wave"]       = wave
        entry["recovered_at_minutes"] = now
        entry["notify_recovery"]      = True
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
        for key in self.active_agents:
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

    def _apply_infection_wave(self, wave: int) -> None:
        """이번 wave의 전염 + 회복 판정. 이동(move_to) 반영 **이후**에 호출할 것.

        전염과 회복은 이번 wave 시작 시점의 감염자 명단(`infected_now`)을 기준으로 함께
        판정한다 — 이번 wave에 갓 감염된 사람이 같은 wave 안에서 곧바로 2차 전파를
        일으키거나 회복 판정을 받는 순서 의존성을 없애기 위함.
        """
        if not self._infection_enabled:
            return

        now = self._current_elapsed_minutes(wave)
        infected_now = {
            key for key in self.active_agents
            if self._infection_entry(key)["status"] == _I
        }

        # 1) 전염 — 같은 장소 그룹 안의 (감염자 × 비감염자) 쌍마다 독립 판정
        if infected_now and self._infection_transmission > 0.0:
            newly_infected: set[str] = set()
            for group in self._compute_contact_groups():
                carriers = [k for k in group if k in infected_now]
                if not carriers:
                    continue
                for key in group:
                    if key in infected_now or key in newly_infected:
                        continue
                    if self._infection_entry(key)["status"] != _S:
                        continue  # R(면역) — SIR에서는 재감염되지 않음
                    for _ in carriers:
                        if random.random() < self._infection_transmission:
                            if self._set_infected(key, wave, "transmission", at_minutes=now):
                                newly_infected.add(key)
                            break  # 이미 감염 — 남은 감염자와의 판정은 무의미

        # 2) 회복 — 이번 wave 시작 시점의 감염자만 대상.
        # 주사위를 굴리지 않는다: 감염 시점에 뽑아둔 `recover_at_minutes`(감염 후
        # 경과 분)에 도달했는지만 본다. recover_at_minutes가 None이면 만성이라
        # 자연 회복하지 않는다.
        for key in sorted(infected_now):
            entry  = self._infection_entry(key)
            since  = entry.get("infected_at_minutes")
            target = entry.get("recover_at_minutes")
            if not isinstance(since, int) or not isinstance(target, int):
                continue
            if now - since >= target:
                self._set_recovered(key, wave, at_minutes=now)

    # ── 경과분 앵커 재기준화 (/continue 전용) ────────────────────────────────────
    def rebase_infection_anchors(self) -> None:
        """`completed_waves`를 0으로 되돌리기 **직전에** 호출해 감염 경과를 보존한다.

        fixed 시간 모드에서 `_current_elapsed_minutes`는 `wave * time_per_wave`라
        `run()`이 호출마다 wave 0부터 다시 세면 '지금'이 0분으로 되감긴다.
        `infected_at_minutes`를 옛 기준 그대로 둔 채 `/continue`하면 다음 wave에서
        `now - since`가 갑자기 작아져(심하면 음수) 증상 단계가 앞 단계로 되감긴다 —
        위치/외모가 재개 시 초기화되던 것과 같은 버그 클래스다.

        variable 모드는 `_elapsed_minutes`가 누적된 채 유지되므로 재기준화 전후의
        '지금'이 같고, 아래 계산은 자연히 no-op이 된다. `/load`·`/resume`은 새
        프로세스에서 export/restore를 거치며 같은 재기준화를 하므로 영향 없다 —
        `/continue`만 같은 프로세스의 `sim_obj`를 그대로 재사용해서 이 단계를
        건너뛰었다.
        """
        now  = self._current_elapsed_minutes(self.completed_waves)
        base = self._current_elapsed_minutes(0)   # 리셋 직후의 '지금'
        if now == base:
            return
        for entry in self._agent_infection.values():
            since = entry.get("infected_at_minutes")
            if entry.get("status") == _I and isinstance(since, int):
                entry["infected_at_minutes"] = base - max(0, now - since)

    # ── 증상 서사 ────────────────────────────────────────────────────────────

    def _find_symptom_stage(self, elapsed_minutes: int) -> dict | None:
        """감염 후 경과 분이 속한 증상 단계. 범위를 벗어나면 가장 늦은 단계를 유지."""
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
        `symptom_text`(그리고 회복 안내 한 줄)만 반환한다.
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

        if entry["status"] != _I:
            return None
        since = entry.get("infected_at_minutes")
        if not isinstance(since, int):
            return None
        elapsed = max(0, self._current_elapsed_minutes(wave) - since)
        stage   = self._find_symptom_stage(elapsed)
        if not stage or not stage.get("symptom_text"):
            return None
        return f"[몸 상태]\n{stage['symptom_text']}"
