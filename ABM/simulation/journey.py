"""여정(journey) — 먼 목적지로 가는 길의 **의도**를 기억하고, 경유지에서 멈추게 한다.

배경 (case1 v9 실측): 짱아가 고등학교에서 하교하며 `move_to: "동네"`를 골랐다.
외부 노드는 거실(집 입구)에만 연결돼 있어 `_find_path`가 조용히 `[거실, 동네]`를
만들었고, 짱아는 출발할 때 집을 거친다는 사실을 몰랐으며("떡볶이 집으로 전력
질주"), 거실에 들렀을 때도 "동네로 가던 길"이라는 맥락이 없었다. 그래프는 실제
길이라 바꾸지 않는다 — 대신 **가는 방법을 엔진이 정하고 알려준다**:

1. 계획 — 상황 안내(`_build_situation_context`)가 "다른 구역을 거쳐야만 닿는 곳"을
   첫 경유 구역별로 묶어 한두 줄로 보여준다(`_route_guide_lines`).
2. 여정 — `move_to` 장소의 경로에 **경유지**가 있으면 `_journey[key]`에 목적지를
   기억한다(`_journey_plan`).
3. 경유지 도착 — 그 자리에 **가용한(상태 없는) 다른 사람**이 있으면 멈추고(남은
   경로 삭제, 여정 `paused`), 없으면 그대로 지나간다(`_journey_on_via_arrival`).
   멈춘 동안은 매 턴 "가던 길: 동네 (거실에서 멈춤) — 다시 출발하려면 move_to" 줄이
   붙고, 같은 목적지를 다시 고르면 재출발한다.

용어
- **경유지(via)** — 경로의 **중간** 노드 중 zone 경계를 넘는 hop으로 도착하는 곳
  (`_path_via`). 구역 안 다중 hop(거실→안방→드레스룸)의 중간 노드는 경유지가 아니다
  — 그런 경로는 여정도 만들지 않고 동작도 예전 그대로다.
- **여정 상태** — `{"destination", "origin", "via", "status": "en_route"|"paused",
  "paused_at"}`. 재개 스냅샷(`export_agent_state`의 `journey`)에 실린다.

경유지 도착은 두 경로로 들어온다:
- zone 이동 시간이 켜져 있으면(`zone_travel_max_minutes > 0`, 기본) 경계 hop은
  `traveling` 상태가 되고, 도착 = wave 시작의 자연 만료(runner 2b →
  `_journey_on_status_expiry`). 여기서 멈춤/통과를 정한 뒤 본인 해제 알림
  (`_status_release_notice`)에 여정 맥락이 붙는다.
- 이동 시간이 꺼져 있으면(0) 경계 hop이 즉시 적용된다. 이동 루프가 모은 도착자를
  루프가 **끝난 뒤**(이번 wave 최종 위치 기준 — 루프 순회 순서에 판정이 흔들리지
  않게) `_journey_after_moves`가 판정하고, 멈춘 사람에게는 다음 wave 씬 알림을 준다.
"""

import logging

logger = logging.getLogger(__name__)


def _ro(word: str) -> str:
    """조사 '(으)로' — 받침 없음·ㄹ 받침이면 '로', 그 외 받침이면 '으로'.

    프롬프트(상황 안내·해제 알림) 전용. 한글 음절이 아니면 '(으)로'로 둔다.
    마크다운 내보내기는 기존 씬 문구처럼 받침 판정을 하지 않는다(JS와 동일하게).
    """
    if not word:
        return "(으)로"
    ch = word[-1]
    if not ("가" <= ch <= "힣"):
        return f"{word}(으)로"
    jong = (ord(ch) - 0xAC00) % 28
    return f"{word}로" if jong in (0, 8) else f"{word}으로"


class _JourneyMixin:
    """여정(먼 목적지로 가는 길) 기억 · 경유지 멈춤/통과 · 경로 안내."""

    # ── 경로 분석 (순수 조회) ────────────────────────────────────────────────

    def _is_zone_boundary(self, a: str, b: str) -> bool:
        """a→b hop이 zone 경계를 넘는가. zone을 안 쓰는 지도는 항상 False.

        runner 이동 루프의 `crossing_zone`과 같은 지리 판정이되 이동 시간 설정
        (`zone_travel_max_minutes`)과 무관하다 — 경유지는 지도상의 사실이다.
        외부·zone 없는 노드는 zone ''로 취급돼 내부 zone과 자연히 구분된다.
        """
        if not self._location_zone:
            return False
        return self._location_zone.get(a, "") != self._location_zone.get(b, "")

    def _path_via(self, start: str, path: list[str]) -> list[str]:
        """경로의 경유지 — zone 경계를 넘는 hop으로 도착하는 **중간** 노드들(순서대로).

        목적지(마지막 노드)는 경유지가 아니다. 구역 안 hop만 있는 경로는 빈 리스트.
        """
        via: list[str] = []
        prev = start
        for i, node in enumerate(path):
            if i < len(path) - 1 and self._is_zone_boundary(prev, node):
                via.append(node)
            prev = node
        return via

    def _route_guide_lines(self, my_loc: str) -> list[str]:
        """지금 위치에서 **경유지를 거쳐야만** 닿는 목적지를 첫 경유 구역별로 묶은 안내.

        예) 고등학교: "가는 길 — 거실(우리집) 경유: 누나학원, 동네, …, 우리집 안
        다른 곳 (구역을 넘을 때마다 약 10~20분)". 경유 구역 **안**의 목적지(안방 등)는
        이름을 다 나열하지 않고 "…안 다른 곳"으로 접는다 — 장황한 전체 목록 금지.
        구역 안 다중 hop(거실→안방화장실)이나 1홉으로 닿는 곳은 대상이 아니다(집 안
        어느 방에서든 바깥은 1홉이라 집 안에서는 이 안내가 나오지 않는다).
        경로는 실제 이동과 같은 `_find_path`로 구한다(동률 처리까지 일치).
        장소 이름에 조사를 붙이지 않는 '경유:' 형식이라 받침 판정이 필요 없다.
        """
        if not self._location_zone or my_loc not in self._location_graph:
            return []
        groups: dict[str, dict] = {}
        order: list[str] = []
        for node in self._location_graph:
            if node == my_loc:
                continue
            path = self._find_path(my_loc, node)
            via = self._path_via(my_loc, path)
            if not via:
                continue
            first = via[0]
            zone  = self._location_zone.get(first, "")
            gkey  = zone or first
            g = groups.get(gkey)
            if g is None:
                g = groups[gkey] = {"via": first, "zone": zone, "outside": [], "inside": False}
                order.append(gkey)
            if zone and self._location_zone.get(node, "") == zone:
                g["inside"] = True
            else:
                g["outside"].append(node)
        lines: list[str] = []
        for gkey in order:
            g = groups[gkey]
            label = f"{g['via']}({g['zone']})" if g["zone"] else g["via"]
            parts = list(g["outside"])
            if g["inside"]:
                parts.append(f"{g['zone']} 안 다른 곳")
            lines.append(f"가는 길 — {label} 경유: {', '.join(parts)}")
        if lines and self._zone_travel_max_minutes > 0:
            lo, hi = self._zone_travel_min_minutes, self._zone_travel_max_minutes
            span = f"약 {hi}분" if lo >= hi else f"약 {lo}~{hi}분"
            lines[-1] += f" (구역을 넘을 때마다 {span})"
        return lines

    def _journey_context_lines(self, key: str, my_loc: str) -> list[str]:
        """[현재 상황]의 여정 줄. 여정이 없으면 빈 리스트.

        - 멈춤: `가던 길: 동네 (거실에서 멈춤) — 다시 출발하려면 move_to: "동네",
          그만두려면 "거실"` — 멈춘 여정은 다른 move_to 가 나올 때까지 매 턴 보이므로
          (그 자리에 오래 머무는 경우) 내려놓는 방법도 함께 적는다.
        - 가는 중: `가던 길: 동네 — 계속 가는 중 (여기 머물려면 move_to: "거실")`
          ("이동 중: 동네까지 1칸 남음" 줄과 목적지가 항상 같다 — 여정의 남은 경로가
          곧 `_agent_path`다.)
        """
        j = self._journey.get(key)
        if not j:
            return []
        dest = j["destination"]
        if j.get("status") == "paused":
            at = j.get("paused_at") or my_loc
            quit_hint = f', 그만두려면 "{my_loc}"' if my_loc else ""
            return [f'가던 길: {dest} ({at}에서 멈춤) — 다시 출발하려면 move_to: "{dest}"'
                    f"{quit_hint}"]
        stay = f' (여기 머물려면 move_to: "{my_loc}")' if my_loc else ""
        return [f"가던 길: {dest} — 계속 가는 중{stay}"]

    def _journey_notice_suffix(self, key: str, arrived_at: str) -> str:
        """경유지 도착 해제 알림(`_status_release_notice`)에 붙는 여정 맥락.

        목적지 도착이거나 여정이 없으면 빈 문자열(알림 문구 불변).
        """
        j = self._journey.get(key)
        if not j or not arrived_at or j["destination"] == arrived_at:
            return ""
        way = f"{_ro(j['destination'])} 가는 길에 들름"
        if j.get("status") == "paused":
            return f" ({way}, 잠시 멈춤)"
        return f" ({way}, 그대로 지나가는 중)"

    def _journey_has_company(self, key: str, loc: str, now_elapsed: int) -> bool:
        """경유지 `loc`에 멈출 이유(만날 사람)가 있는가.

        **가용한** 다른 활성 에이전트 — 어떤 상태(이동 중·수면·개인 용무)에도 묶이지
        않은 사람 — 가 그 자리에 있을 때만 True. `"all"` 방송 대상 규칙
        (targets.py — 수면·개인 용무 제외)·재투입 후보 규칙과 같은 기준이다.
        busy(기본 라벨 "자리를 비우고 하는 개인적인 일")는 같은 방이어도 멈출
        이유로 치지 않는다. 그래도 통과하는 턴에 [이 자리의 사람들]로 보이므로
        말을 걸거나 `move_to`에 지금 위치를 넣어 머물 수 있다. 외부 공간은 서로
        볼 수 없으니 항상 False.
        """
        if not loc or loc in self._exterior_locations:
            return False
        for other in sorted(self.active_agents):
            if other == key or self._agent_location.get(other, "") != loc:
                continue
            if self._agent_unavailable(other, now_elapsed):
                continue
            return True
        return False

    # ── 상태 전이 ────────────────────────────────────────────────────────────

    def _journey_emit(
        self, wave: int, key: str, action: str, j: dict, *,
        at: str | None = None, reason: str | None = None, at_wave_start: bool = False,
    ) -> None:
        """`journey_update` 관전 이벤트(영속). action: start|pause|pass|resume|arrive|cancel.

        `at_wave_start` = wave 시작 시점(대사 전)에 난 것인가 — 경유지 도착(자연
        만료)·퇴장 취소. 마크다운 내보내기가 대사 전/후 배치(phase)에 쓴다.
        """
        self._emit("journey_update", {
            "wave":          wave,
            "agent":         key,
            "display_name":  self._key_to_alias.get(key, key),
            "destination":   j.get("destination", ""),
            "action":        action,
            "at":            at if at is not None else self._agent_location.get(key, ""),
            "via":           list(j.get("via") or []),
            "reason":        reason,
            "at_wave_start": at_wave_start,
        })

    def _journey_plan(
        self, key: str, dest: str, origin: str, path: list[str], wave: int,
    ) -> str:
        """장소 `move_to`로 새 경로가 깔린 직후 여정을 세우거나 이어간다. 해석 종류 반환.

        - 같은 목적지의 여정이 있으면: 멈춰 있었으면 `resume`(재출발), 가는 중이면
          `continue`(그대로). 여정은 목적지 도착 때 풀린다.
        - 다른 목적지면 기존 여정은 취소(`new_move_to`)하고, 새 경로에 경유지가
          있으면 새 여정 `journey`, 없으면 평범한 이동 `place`.
        """
        via = self._path_via(origin, path)
        j = self._journey.get(key)
        if j and j["destination"] == dest:
            was_paused = j.get("status") == "paused"
            j.update(status="en_route", paused_at=None, origin=origin, via=via)
            if was_paused:
                self._journey_emit(wave, key, "resume", j, at=origin)
                return "resume"
            return "continue"
        if j:
            self._journey_cancel(key, wave, "new_move_to")
        if not via:
            return "place"
        j = {"destination": dest, "origin": origin, "via": via,
             "status": "en_route", "paused_at": None}
        self._journey[key] = j
        self._journey_emit(wave, key, "start", j, at=origin)
        return "journey"

    def _journey_cancel(
        self, key: str, wave: int, reason: str, *, at_wave_start: bool = False,
    ) -> bool:
        """여정을 버린다(있으면 `cancel` emit). 남은 경로는 호출부가 정한다."""
        j = self._journey.pop(key, None)
        if j is None:
            return False
        self._journey_emit(wave, key, "cancel", j, reason=reason, at_wave_start=at_wave_start)
        return True

    def _journey_on_via_arrival(
        self, key: str, loc: str, wave: int, now_elapsed: int, *, at_wave_start: bool,
    ) -> str | None:
        """`loc`에 막 도착한 여정 중인 에이전트의 멈춤/통과/도착 판정.

        Returns "pause" | "pass" | "arrive" | None(여정 없음·이미 멈춤).
        - 목적지면 여정 해제(`arrive`).
        - 남은 경로가 없는데 목적지가 아니면(방어적 — 정상 흐름엔 없음) 멈춘 것으로 둔다.
        - 가용한 다른 사람이 있으면 **멈춤**: 남은 경로를 지우고 여정은 `paused`로 유지.
        - 없으면 **통과**: 경로를 그대로 두어 이동 루프가 다음 hop을 잇는다.
        """
        j = self._journey.get(key)
        if not j or j.get("status") != "en_route":
            return None
        if loc == j["destination"]:
            self._journey.pop(key, None)
            self._journey_emit(wave, key, "arrive", j, at=loc, at_wave_start=at_wave_start)
            return "arrive"
        if self._agent_path.get(key) and not self._journey_has_company(key, loc, now_elapsed):
            self._journey_emit(wave, key, "pass", j, at=loc, at_wave_start=at_wave_start)
            return "pass"
        self._agent_path.pop(key, None)
        j.update(status="paused", paused_at=loc)
        self._journey_emit(wave, key, "pause", j, at=loc, at_wave_start=at_wave_start)
        return "pause"

    def _journey_on_status_expiry(
        self, expired: dict[str, dict], wave: int, now_elapsed: int,
    ) -> None:
        """wave 시작의 자연 만료 중 `traveling` 해제 = 도착. 여정 중이면 판정한다.

        반드시 본인 해제 알림(`_status_release_notice`)을 만들기 **전**에 불려야 한다
        — 알림의 여정 맥락(멈춤/지나감)이 이 판정 결과를 읽는다.
        """
        for key in sorted(expired):
            st = expired[key]
            if st.get("state") != "traveling" or key not in self._journey:
                continue
            if key not in self.active_agents:
                continue
            loc = st.get("arrival_location") or self._agent_location.get(key, "")
            self._journey_on_via_arrival(key, loc, wave, now_elapsed, at_wave_start=True)

    def _journey_after_moves(
        self, instant_arrivals: list[str], wave: int, now_elapsed: int,
        scene_injections: dict,
    ) -> None:
        """이동 루프 **뒤** — 즉시 적용된 경계 hop(이동 시간 0)의 경유지 판정 + 도착 정리.

        1) `instant_arrivals`(이번 wave에 이동 시간 없이 경유지에 들어선 사람)를 이번
           wave 최종 위치 기준으로 판정한다. 멈춘 사람에게는 다음 wave에 씬 알림을
           준다(자연 만료 경로의 해제 알림과 같은 문구).
        2) 구역 안 hop으로 목적지에 들어선 여정(도착에 이동 시간이 없음)을 해제한다.
           마지막 hop이 경계 hop이라 아직 이동 중이면 두고, 도착(자연 만료) 때 푼다.
        """
        for key in sorted(set(instant_arrivals)):
            loc = self._agent_location.get(key, "")
            if self._journey_on_via_arrival(key, loc, wave, now_elapsed,
                                            at_wave_start=False) == "pause":
                scene_injections.setdefault(key, []).append({
                    "speaker":     "씬",
                    "content":     f"[씬] {loc}에 도착했다.{self._journey_notice_suffix(key, loc)}",
                    "action_note": "",
                })
        for key in sorted(self._journey):
            j = self._journey.get(key)
            if not j or j.get("status") != "en_route":
                continue
            if self._agent_location.get(key, "") != j["destination"]:
                continue
            if self._agent_traveling(key, now_elapsed):
                continue
            self._journey.pop(key, None)
            self._journey_emit(wave, key, "arrive", j, at=j["destination"])

    # ── move_to 로깅 ─────────────────────────────────────────────────────────

    def _record_move_resolution(
        self, key: str, raw: str, resolution: dict, wave: int,
    ) -> None:
        """LLM이 고른 원본 `move_to`와 엔진의 해석 결과를 남긴다.

        - 에이전트 로그(`logs_graph/<이름>.json`): 이번 턴 항목에 `move_to`(원본)와
          `move_resolved`(해석)를 덧붙인다 — 턴 로그는 LLM 응답 직후 쓰이고 해석은
          모든 턴이 끝난 뒤(이동 전 스냅샷) 확정되므로 사후 주석이다.
        - DB: `move_intent` 이벤트(영속, `sim_events`). simulation_log의 meta에 넣지
          않는 이유 — 피드(feed.js)가 meta의 **모든 키**를 뱃지로 그리고(해석 dict가
          "[object Object]" 뱃지로 샌다), 행은 턴 시점에 이미 INSERT돼 해석을 넣으려면
          사후 UPDATE가 필요하다. 이벤트는 마크다운·SSE 리스너가 없어 화면에 영향이 없다.

        resolution["kind"]: place | journey | resume | continue | stay | person | invalid.
        """
        agent = self.agents.get(key)
        if agent is not None:
            try:
                agent.annotate_last_log({"move_to": raw, "move_resolved": dict(resolution)})
            except OSError as e:
                logger.warning(f"[{key}] move_to 로그 주석 실패: {e}")
        self._emit("move_intent", {
            "wave":         wave,
            "agent":        key,
            "display_name": self._key_to_alias.get(key, key),
            "raw":          raw,
            **resolution,
        })
