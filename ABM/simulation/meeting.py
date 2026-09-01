"""이동 엇갈림(rendezvous) 해소 — `move_to`로 **사람**을 지목했을 때의 추격/집결.

배경: zone을 쓰면 같은 구역의 다른 장소에 있는 사람을 인지할 수 있다
(`_compute_zone_awareness`). 그런데 A와 B가 서로를 만나려고 각자 상대의 "현재
위치"로 `move_to` 하면 두 위치를 영원히 맞바꾸며 엇갈리고(간선 스왑), 상대가 이동
중이면 매 웨이브 stale 위치를 추격해 수렴하지 않는다.

해법: 위치가 아니라 **의도**를 신호로 받는다. `move_to` 값이 위치 그래프의 노드가
아니면 에이전트 지목(key / alias / `stranger_N`)으로 해석해 "그 사람을 만나러
따라간다"로 처리하고, 시스템이 `_meeting_intent`(추격자 → 목표)를 lock으로 들고
있으면서 매 웨이브 목적 노드를 결정론적으로 다시 정한다.

모듈 상단의 세 함수는 Simulation 상태에 전혀 의존하지 않는 **순수 함수**다
(단위 테스트 대상). 아래 `_MeetingMixin`은 그 결과를 `_agent_path`에 반영만 한다.
"""

import logging

logger = logging.getLogger(__name__)


# ── 순수 계산 헬퍼 ────────────────────────────────────────────────────────────

def hop_count(find_path, start: str, goal: str) -> int | None:
    """start → goal 이동 홉 수. 같은 노드면 0, 도달 불가면 None.

    `_find_path`는 "같은 노드"와 "경로 없음"을 똑같이 `[]`로 돌려주므로, 비용
    비교에 쓰려면 여기서 반드시 둘을 갈라야 한다. 이 구분이 없으면 도달 불가
    노드가 비용 0인 최적 후보로 뽑힌다.
    """
    if start == goal:
        return 0
    path = find_path(start, goal)
    return len(path) if path else None


def weak_components(intent: dict[str, str]) -> list[list[str]]:
    """의도 방향 그래프(추격자 → 목표)의 약한 연결 컴포넌트.

    A→B, C→B 처럼 방향이 한쪽으로만 나 있어도 한 덩어리로 묶어야 세 사람이 한
    자리에 모인다. 반환 순서와 컴포넌트 내부 순서 모두 key 사전순으로 고정해
    같은 입력이 항상 같은 결과를 내도록(결정론) 한다.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rb < ra:          # 사전순 앞선 쪽을 루트로 — 입력 순서에 흔들리지 않게
            ra, rb = rb, ra
        parent[rb] = ra

    for chaser, target in intent.items():
        union(chaser, target)

    groups: dict[str, list[str]] = {}
    for key in parent:
        groups.setdefault(find(key), []).append(key)
    return [sorted(members) for _, members in sorted(groups.items())]


def gathering_node(positions: dict[str, str], find_path) -> str | None:
    """전원이 모일 노드. 후보는 참가자들이 **지금 서 있는** 노드들.

    - 총 이동 홉(각자 현재 노드 → 후보)의 합이 최소인 노드를 고른다.
    - 동점이면 그 노드에 서 있는 에이전트 key가 사전순 더 앞선 쪽을 택한다.
    - 참가자 중 한 명이라도 도달할 수 없는 후보는 제외한다.
    - 유효한 후보가 없으면 None.

    후보를 "현재 위치들"로 제한하는 이유: 아무도 없는 제3의 노드까지 후보에
    넣으면 지도가 커질수록 엉뚱한 빈 방에서 만나게 되고, 그 방은 서로가 인지·
    합의한 적 없는 장소다. 최소한 한 명은 이미 그 자리에 있어야 "찾아간다"는
    서사가 성립한다.

    **호출자 계약**: 이 함수는 순수 함수라 외부 공간(`_exterior_locations`)을
    모른다. 격리 공간이 랑데부 지점으로 뽑히면 안 되므로, 호출자는 외부 공간에
    있는 참가자를 `positions`에서 미리 제외해야 한다. 현재 `_update_meeting_paths`는
    `_release_stale_meetings`가 그 앞에서 외부 공간 참가자를 `gone`/`invalid`로
    걷어내므로 이 계약이 지켜진다.
    """
    if not positions:
        return None
    best: tuple[int, str, str] | None = None   # (총 홉, 그 노드의 대표 key, 노드)
    for node in sorted(set(positions.values())):
        if not node:
            continue
        total = 0
        reachable = True
        for loc in positions.values():
            h = hop_count(find_path, loc, node)
            if h is None:
                reachable = False
                break
            total += h
        if not reachable:
            continue
        owner = min(k for k, loc in positions.items() if loc == node)
        cand  = (total, owner, node)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


class _MeetingMixin:
    """`move_to` 사람 지목 → 추격/랑데부 lock 관리."""

    # ── move_to 값 해석 ──────────────────────────────────────────────────────

    def _is_location_name(self, dest: str) -> bool:
        """dest가 위치 그래프의 노드인가.

        그래프 미설정 시나리오는 **항상 True** — `_find_path`가 임의 문자열을
        직접 이동으로 처리하는 기존(하위 호환) 경로를 그대로 태운다. 사람 지목
        해석은 위치 그래프가 있을 때만 의미가 있다.
        """
        if not self._location_graph:
            return True
        return dest in self._location_graph

    def _resolve_meet_target(self, speaker_key: str, dest: str) -> str | None:
        """`move_to` 값에서 '만나러 갈 사람'을 해석. 해석 불가면 None.

        `_resolve_targets`의 인지 규칙을 그대로 따른다 — 아직 '낯선 이'로만 아는
        상대는 `stranger_N` ID로만 지목할 수 있다(이름은 만나서 알아내는 것).
        그리고 zone이 설정된 시나리오에서는 **인지 가능한 상대**(같은 노드 또는
        같은 구역)만 지목할 수 있다. 인지한 적도 없는 사람을 지도 반대편까지
        쫓아가는 건 zone이 세운 벽을 그대로 무너뜨리는 일이고, 나중에 그가 구역을
        벗어났을 때 "어디론가 가버렸다" 씬이 뜬금없이 날아간다.
        """
        if dest.startswith("stranger_"):
            target = self._stranger_map.get(speaker_key, {}).get(dest)
        else:
            target = self._normalize_target(dest)
            if target not in self.agents:
                return None
            if self._is_anonymous_to(speaker_key, target):
                return None
        if not target or target == speaker_key or target not in self.active_agents:
            return None

        my_loc     = self._agent_location.get(speaker_key, "")
        target_loc = self._agent_location.get(target, "")
        # 외부 공간은 완전 격리 — 추격 주체도 대상도 될 수 없다.
        if my_loc in self._exterior_locations or target_loc in self._exterior_locations:
            return None
        if self._location_zone and my_loc != target_loc:
            my_zone = self._location_zone.get(my_loc, "")
            if not my_zone or self._location_zone.get(target_loc, "") != my_zone:
                return None
        return target

    def _apply_move_intents(self, results: dict) -> None:
        """이번 wave의 `move_to` 발화를 장소 이동 / 만남 의도로 분류해 반영.

        반드시 **이동 적용 전 위치 스냅샷**에서 불려야 한다(runner의 발화 라우팅·
        외모 처리와 같은 원칙). 어떤 형태든 새 `move_to`가 나오면 기존 만남 lock은
        그 발화로 대체되거나 취소된다 — LLM이 마음을 바꿀 수 있는 유일한 손잡이다.

        만남 처리 한 쌍(`_apply_move_intents` → `_update_meeting_paths`)의 선두이므로
        해제 사유 버퍼를 여기서 비운다.
        """
        self._meeting_break_log.clear()
        for speaker_key, result in results.items():
            if not result.get("success"):
                continue
            raw = result.get("move_to")
            if not raw or not isinstance(raw, str):
                continue
            dest = raw.strip()
            if not dest:
                continue

            # 장소명이 우선. 사람 alias와 장소명이 겹치면 장소로 읽는다.
            if not self._is_location_name(dest):
                target = self._resolve_meet_target(speaker_key, dest)
                if target:
                    self._meeting_intent[speaker_key] = target
                    continue

            had_intent  = self._meeting_intent.pop(speaker_key, None) is not None
            current_loc = self._agent_location.get(speaker_key, "")
            if dest != current_loc:
                if had_intent:
                    self._meeting_break_log[speaker_key] = "new_move_to"
                path = self._find_path(current_loc, dest)
                # 해석 불가(장소도 사람도 아님)면 _find_path가 []를 준다 — 죽은 키를
                # _agent_path에 남기지 않는다. 동작은 어느 쪽이든 같지만("경로 없음"),
                # 디버깅 시 "경로가 있는데 안 움직인다"로 오독되는 걸 막는다.
                if path:
                    self._agent_path[speaker_key] = path
                else:
                    self._agent_path.pop(speaker_key, None)
            elif had_intent:
                self._meeting_break_log[speaker_key] = "staying"
                # "나는 여기 있겠다" — 만나러 가던 길도 그 자리에서 버린다. 이걸
                # 빠뜨리면 lock만 풀리고 몸은 계속 옛 목적지로 걸어가는 유령 추격이
                # 남는다. 만남 lock이 없던 경우(순수 장소 이동 시나리오)는 기존
                # 동작을 그대로 둔다 — 하위 호환.
                self._agent_path.pop(speaker_key, None)

    # ── lock 유지/해제 ────────────────────────────────────────────────────────

    def _meeting_goal_node(self, key: str) -> str:
        """그 에이전트가 결국 도착할 노드 — 이동 중이면 최종 목적지, 아니면 현재 노드.

        추격이 수렴하는 핵심이다. 이동 중인 상대의 **현재** 위치를 쫓으면 매 웨이브
        stale 목표를 따라가며 엇갈리지만, 최종 목적지를 쫓으면 늦어도 상대가 멈추는
        시점에 반드시 만난다.
        """
        path = self._agent_path.get(key)
        if path:
            dest = path[-1]
            # 외부 공간으로 나가는 중이면 따라 들어가지 않는다(그곳은 격리 공간).
            # 실제로 나가는 순간 아래 해제 조건이 "가버렸다"로 lock을 푼다.
            if dest not in self._exterior_locations:
                return dest
        return self._agent_location.get(key, "")

    def _meeting_break_reason(self, chaser: str, target: str) -> str | None:
        """lock을 풀어야 하는 이유. 유지해야 하면 None.

        "met"     — 동석 성립(목적 달성)
        "gone"    — 목표가 사라짐(이탈/외부공간/구역 밖) → 추격자에게 씬 통보
        "invalid" — 추격자 쪽 사정으로 무효(비active/외부공간) → 조용히 폐기
        """
        if chaser not in self.active_agents:
            return "invalid"
        chaser_loc = self._agent_location.get(chaser, "")
        if chaser_loc in self._exterior_locations:
            return "invalid"
        if target not in self.active_agents:
            return "gone"
        target_loc = self._agent_location.get(target, "")
        if target_loc in self._exterior_locations:
            return "gone"
        if chaser_loc and chaser_loc == target_loc:
            return "met"
        if self._location_zone:
            chaser_zone = self._location_zone.get(chaser_loc, "")
            if chaser_zone and self._location_zone.get(target_loc, "") != chaser_zone:
                return "gone"
        return None

    def _release_stale_meetings(self, scene_injections: dict) -> None:
        """해제 조건에 걸린 lock을 정리하고, 목표를 놓친 추격자에게 씬을 주입."""
        for chaser, target in sorted(self._meeting_intent.items()):
            reason = self._meeting_break_reason(chaser, target)
            if reason is None:
                continue
            self._meeting_intent.pop(chaser, None)
            self._meeting_break_log[chaser] = reason
            if reason != "gone":
                continue
            # 목적을 잃었으므로 가던 길도 멈춘다 — 안 그러면 아무도 없는 방까지
            # 계속 걸어간 뒤에야 상황을 다시 판단하게 된다.
            self._agent_path.pop(chaser, None)
            label = self._meeting_label(chaser, target)
            scene_injections.setdefault(chaser, []).append({
                "speaker":     "씬",
                "content":     f"[씬] {label}이(가) 어디론가 가버렸다.",
                "action_note": "",
            })

    def _meeting_label(self, observer_key: str, target_key: str) -> str:
        """만남 상대를 관찰자의 인지 상태에 맞춰 부르는 이름.

        아는 사이면 표시 이름, 아니면 `stranger_N`. 씬 메시지·상황 블록이 같은
        규칙을 공유해야 "낯선 이(stranger_1)"와 실명이 뒤섞이지 않는다.
        """
        if target_key in self._agent_knowledge.get(observer_key, set()):
            return self._key_to_alias.get(target_key, target_key)
        sid = self._get_or_assign_stranger_id(observer_key, target_key)
        return f'낯선 이(ID: "{sid}")'

    # ── 경로 반영 ─────────────────────────────────────────────────────────────

    def _update_meeting_paths(self, scene_injections: dict) -> None:
        """살아 있는 만남 의도를 실제 이동 경로로 환산.

        컴포넌트 단위로 처리한다:
        - **정지 목표(anchor)가 있는 경우** — anchor는 남을 만나러 가지 않는 사람이다.
          그를 끌고 다니면 안 되므로 추격자들만 anchor의 도착 노드로 보낸다.
          (A→B 일방 추적, A→B←C 합류, A→B→C 연쇄가 모두 이 갈래다.)
        - **anchor가 없는 경우**(상호 의도/순환) — 아무도 가만히 있지 않으므로 서로의
          현재 위치 중 총 이동 홉이 최소인 노드로 전원 집결한다. 여기서 위치를 서로
          맞바꾸는 스왑이 끊긴다.
        """
        if not self._meeting_intent:
            return
        self._release_stale_meetings(scene_injections)
        if not self._meeting_intent:
            return

        for members in weak_components(self._meeting_intent):
            movers  = [k for k in members if k in self._meeting_intent]
            anchors = [k for k in members if k not in self._meeting_intent]

            if anchors:
                goals = {self._meeting_goal_node(a) for a in anchors}
                if len(goals) == 1:
                    self._steer(movers, goals.pop())
                else:
                    # 정지 목표가 여러 곳 — 한 점 집결이 불가능하다. 억지로 한 곳을
                    # 고르면 아무도 자기 목표를 못 만나므로, 각자 자기 목표만 쫓는다.
                    for m in movers:
                        self._steer([m], self._meeting_goal_node(self._meeting_intent[m]))
                continue

            # 이미 전원이 한 노드로 수렴 중이면 재계산하지 않는다(lock). 매 웨이브
            # 다시 계산하면 서로 다가가는 도중 위치가 바뀌며 랑데부 지점이 흔들린다.
            if len({self._meeting_goal_node(k) for k in members}) == 1:
                continue

            positions = {k: self._agent_location.get(k, "") for k in members}
            node = gathering_node(positions, self._find_path)
            if node is None:
                logger.debug("rendezvous 불가(연결 경로 없음): %s", members)
                for m in members:
                    if self._meeting_intent.pop(m, None) is not None:
                        self._meeting_break_log[m] = "unreachable"
                continue
            self._steer(members, node)

    def _steer(self, keys: list[str], goal: str) -> None:
        """목적 노드가 바뀐 에이전트만 경로를 다시 깐다(최소 개입)."""
        if not goal:
            return
        for key in keys:
            if self._meeting_goal_node(key) == goal:
                continue
            loc = self._agent_location.get(key, "")
            if loc == goal:
                # 이미 약속 장소에 서 있는데 다른 데로 가던 길이 남아 있는 경우
                # (예: 상대가 뒤늦게 내가 있는 쪽으로 오기 시작). 발길을 멈추고
                # 기다린다. 여기서 _find_path를 부르면 []가 나와 아래 '도달 불가'
                # 분기로 새고, 멀쩡한 만남이 취소된다.
                self._agent_path.pop(key, None)
                continue
            path = self._find_path(loc, goal)
            if path:
                self._agent_path[key] = path
            else:
                # 도달 불가 — lock을 유지하면 영원히 제자리걸음이므로 의도를 버린다.
                self._agent_path.pop(key, None)
                if self._meeting_intent.pop(key, None) is not None:
                    self._meeting_break_log[key] = "unreachable"

    # ── 이벤트 (meeting_update) ───────────────────────────────────────────────

    def _emit_meeting_updates(self, wave_num: int, previous: dict[str, str]) -> None:
        """만남 lock 변화를 `meeting_update` 이벤트로 내보낸다.

        `previous`는 이번 wave의 만남 처리가 **시작되기 전**(= 지난 wave 종료 시점)
        스냅샷이다. `_apply_move_intents`가 새 lock을 세우고 `_update_meeting_paths`가
        해소하므로, 둘 다 끝난 뒤 한 번만 diff 해야 "이번 wave에 실제로 무엇이
        바뀌었나"가 나온다.

        해제 사유는 `_meeting_break_log`에서 가져와 소비 후 비운다(그 wave 한정).
        """
        current = self._meeting_intent
        for chaser in sorted(set(previous) | set(current)):
            before = previous.get(chaser)
            after  = current.get(chaser)
            if before == after:
                continue
            if after is not None:
                # 새로 생겼거나 목표가 바뀜 — 둘 다 "이제 이 사람을 쫓는다" 한 건으로
                # 표현한다(프론트는 chaser별로 한 줄만 그린다).
                self._emit_meeting_event(wave_num, chaser, after, "start", None)
                continue
            # 사라짐 — 사유에 따라 arrived / cancelled.
            reason = self._meeting_break_log.get(chaser, "unreachable")
            if reason == "met":
                self._emit_meeting_event(wave_num, chaser, before, "arrived", "met")
            elif reason == "invalid":
                # 추격자 쪽이 시뮬레이션에서 빠졌거나 외부 공간으로 나간 경우.
                # 계약서의 reason 열거에는 없는 내부 사유라 일반 취소 버킷으로 접는다
                # — 프론트에서 "그만뒀다" 문구로 처리되고 추격선도 정리된다.
                self._emit_meeting_event(wave_num, chaser, before, "cancelled", "unreachable")
            else:
                self._emit_meeting_event(wave_num, chaser, before, "cancelled", reason)
        self._meeting_break_log.clear()

    def _emit_meeting_event(
        self, wave_num: int, chaser: str, target: str, status: str, reason: str | None
    ) -> None:
        self._emit("meeting_update", {
            "wave":            wave_num,
            "chaser":          chaser,
            "chaser_name":     self._key_to_alias.get(chaser, chaser),
            "target":          target,
            # 추격자가 아직 모르는 상대면 실명이 아니라 stranger_N으로 나간다 —
            # 피드/지도에 이름이 새면 "이름은 만나서 알아내는 것"이 무너진다.
            "target_name":     self._meeting_label(chaser, target),
            "target_location": self._agent_location.get(target, ""),
            "status":          status,
            "reason":          reason,
        })
