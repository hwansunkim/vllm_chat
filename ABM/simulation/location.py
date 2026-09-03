import logging
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class _LocationMixin:
    """위치 그래프, BFS 경로 탐색, 상황 컨텍스트 관련 메서드."""

    def _expand_zone_edges(self, raw_nodes: list[dict]) -> None:
        """connects_to 의 zone 참조를 노드 레벨 엣지로 전개.

        외부 노드 X 의 raw connects_to 에 zone Z(입구 E, 내부 노드 집합 N)가 있으면:
        - 진입: X -> E 엣지 추가 (바깥에서 들어오면 입구를 거침)
        - 탈출: N 의 **모든** 내부 노드 -> X 엣지 추가 (구역 안 어디서든 1홉 탈출)
        E -> X 는 탈출 루프가 자동 추가(E 도 내부 노드) → X <-> E 대칭 + 나머지
        내부 노드는 X 로 단방향 탈출, 복귀는 X -> E -> ... -> 내부.

        전개 후 self._location_graph 는 여전히 순수 노드 인접 리스트라
        _find_path/_get_adjacent/_build_situation_context/인지 로직 전부 무변경.
        zone 이 하나도 없으면 완전한 no-op — flat 그래프는 바이트 단위로 동일하다.
        """
        if not self._location_zone:
            return

        zone_names = set(self._location_zone.values())
        zone_nodes: dict[str, list[str]] = defaultdict(list)
        for loc, z in self._location_zone.items():
            zone_nodes[z].append(loc)

        known_node_names = {n.get("name", "") for n in raw_nodes}

        def _add(a: str, b: str) -> None:
            if a == b or a not in self._location_graph:
                return
            if b not in self._location_graph[a]:
                self._location_graph[a].append(b)

        for node in raw_nodes:
            x = node.get("name", "")
            if not x or x not in self._location_graph:
                continue
            for target in list(node.get("connects_to", [])):
                if target in self._location_graph:
                    continue  # 실존 노드 엣지 — 노드 우선(_is_location_name 선례)
                if target not in zone_names:
                    # zone 도 노드도 아닌 미해결 참조는 건드리지 않는다(기존 동작 유지).
                    if target not in known_node_names:
                        logger.warning(f"[zone 전개] connects_to 미해결: '{x}' -> '{target}'")
                    continue
                # 여기부터 target 은 zone 이름 — 노드 인접 리스트에서 걷어낸다.
                if target in self._location_graph[x]:
                    self._location_graph[x].remove(target)
                if self._location_zone.get(x) == target:
                    logger.warning(f"[zone 전개] 자기 구역 참조 무시: '{x}' -> '{target}'")
                    continue
                entry = self._zone_entry.get(target)
                if entry is None:
                    logger.warning(
                        f"[zone 전개] '{x}' -> '{target}': 해당 zone 에 입구 노드가 "
                        f"없어 무시(is_zone_entry 지정 필요)"
                    )
                    continue
                _add(x, entry)  # 진입
                for interior in zone_nodes.get(target, []):
                    _add(interior, x)  # 탈출 (E 포함)

    def _find_path(self, start: str, goal: str) -> list[str]:
        """BFS 최단 경로. 시작 제외, 목표 포함.

        그래프 없음 → [goal] (하위 호환 직접 이동).
        그래프 있는데 goal이 지도 밖 → [] (이동 무시).
        """
        if start == goal:
            return []
        if not self._location_graph:
            return [goal]  # 그래프 미설정 → 직접 이동 (하위 호환)
        if goal not in self._location_graph:
            return []  # 지도에 없는 목적지 → 이동 무시
        if start not in self._location_graph:
            return [goal]  # 시작 위치가 지도 밖인 예외 상황 → 직접 이동
        visited = {start}
        q = deque([(start, [])])
        while q:
            node, path = q.popleft()
            for nb in self._location_graph.get(node, []):
                new_path = path + [nb]
                if nb == goal:
                    return new_path
                if nb not in visited:
                    visited.add(nb)
                    q.append((nb, new_path))
        return []  # 연결 경로 없음 → 이동 무시

    def _get_adjacent(self, location: str) -> list[str]:
        """현재 위치에서 이동 가능한 인접 장소 목록."""
        return list(self._location_graph.get(location, []))

    def _get_or_assign_stranger_id(self, observer_key: str, target_key: str) -> str:
        """observer가 target을 처음 보는 경우 stranger_N ID를 할당, 이미 있으면 기존 ID 반환.

        ThreadPoolExecutor 워커에서 동시에 불리므로 전체 read-modify-write를 락으로
        직렬화한다 (번호 중복/누락 방지).
        """
        with self._stranger_lock:
            self._stranger_map.setdefault(observer_key, {})
            self._stranger_rmap.setdefault(observer_key, {})
            if target_key in self._stranger_rmap[observer_key]:
                return self._stranger_rmap[observer_key][target_key]
            # 복원된 map에 번호 공백이 있어도 기존 ID를 덮어쓰지 않도록 빈 번호를 찾는다.
            n = len(self._stranger_map[observer_key]) + 1
            while f"stranger_{n}" in self._stranger_map[observer_key]:
                n += 1
            sid = f"stranger_{n}"
            self._stranger_map[observer_key][sid] = target_key
            self._stranger_rmap[observer_key][target_key] = sid
            return sid

    def _appearance_scene_msg(
        self, subject_key: str, observer_key: str, display: str, description: str
    ) -> str:
        """외모 변경 씬 메시지를 관찰자의 인지 상태에 맞춰 구성.

        아는 사이면 실명, 모르는 사이면 stranger_N ID로 익명화한다. 이동 씬 메시지
        (runner.py의 도착/이탈)가 쓰는 knowledge 분기와 같은 규칙이다. 예전엔 외모
        변경만 이 분기가 없어, 처음 보는 사이인데도 실명이 그대로 노출됐다.

        이동 알림과 달리 stranger_N ID를 함께 싣는 이유: 외모 변경은 본질적으로
        "같은 사람인데 모습이 바뀌었다"는 **동일인 연속성** 통보다. 낯선 이가 여럿인
        자리에서 ID가 없으면 관찰자는 누가 변했는지 특정할 수 없고, 새 사람이
        나타났다고 오인한다. `[현재 상황]` 블록(_build_situation_context)도 같은 ID로
        낯선 이를 나열하므로 두 정보가 서로 맞물린다.
        """
        if subject_key in self._agent_knowledge.get(observer_key, set()):
            return f"[씬] {display}의 외모가 변했다: {description}"
        sid = self._get_or_assign_stranger_id(observer_key, subject_key)
        return f'[씬] 낯선 이(ID: "{sid}")의 외모가 변했다: {description}'

    def _compute_wave_targets(self, agent_key: str):
        """현재 위치 기준 대화 가능 에이전트 계산.

        Returns (known: list[str], strangers: list[tuple[stranger_id, real_key, visual]])
        위치가 설정된 경우에만 필터링, 미설정 시 기존 동작.
        외부 공간에 있는 에이전트는 서로를 볼 수 없음.
        """
        my_loc      = self._agent_location.get(agent_key, "")
        is_exterior = my_loc in self._exterior_locations
        if is_exterior:
            return [], []  # 외부 공간 — 아무도 보이지 않음

        knowledge = self._agent_knowledge.get(agent_key, set())
        known:     list[str]                    = []
        strangers: list[tuple[str, str, str]]   = []
        for other_key in self.active_agents:
            if other_key == agent_key:
                continue
            other_loc = self._agent_location.get(other_key, "")
            if other_loc in self._exterior_locations:
                continue  # 외부 공간에 있는 에이전트는 내부에서 보이지 않음
            if my_loc and other_loc and my_loc != other_loc:
                continue
            if other_key in knowledge:
                known.append(other_key)
            else:
                sid    = self._get_or_assign_stranger_id(agent_key, other_key)
                visual = self._agent_visual.get(other_key, "") or self._key_to_alias.get(other_key, other_key)
                strangers.append((sid, other_key, visual))
        return known, strangers

    def _compute_zone_awareness(self, agent_key: str):
        """같은 구역(zone)의 **다른 장소**에 있는 에이전트 인지 계산.

        대화 범위(_compute_wave_targets)와는 별개다 — 여기 잡히는 사람들은
        "저기 있구나"라고 알 뿐, 말을 걸려면 move_to로 그 장소까지 가야 한다.
        (LocationNode.zone은 위치 개념이며 _agent_groups(캐릭터 관계 그룹)와 무관.)

        Returns (known_elsewhere: list[tuple[key, location]],
                 strangers_elsewhere: list[tuple[stranger_id, real_key, visual, location]])
        현재 위치에 zone이 없거나 외부 공간이면 빈 목록.
        같은 장소에 있는 사람(= 이미 known/strangers에 포함)은 제외한다.
        """
        my_loc = self._agent_location.get(agent_key, "")
        if not my_loc or my_loc in self._exterior_locations:
            return [], []
        my_zone = self._location_zone.get(my_loc, "")
        if not my_zone:
            return [], []

        knowledge = self._agent_knowledge.get(agent_key, set())
        known_elsewhere:     list[tuple[str, str]]           = []
        strangers_elsewhere: list[tuple[str, str, str, str]] = []
        for other_key in self.active_agents:
            if other_key == agent_key:
                continue
            other_loc = self._agent_location.get(other_key, "")
            if not other_loc or other_loc == my_loc:
                continue  # 같은 장소 → 이미 대화 스코프에서 노출됨
            if other_loc in self._exterior_locations:
                continue  # 외부 공간은 zone과 무관하게 완전 격리
            if self._location_zone.get(other_loc, "") != my_zone:
                continue
            if other_key in knowledge:
                known_elsewhere.append((other_key, other_loc))
            else:
                sid    = self._get_or_assign_stranger_id(agent_key, other_key)
                visual = self._agent_visual.get(other_key, "") or self._key_to_alias.get(other_key, other_key)
                strangers_elsewhere.append((sid, other_key, visual, other_loc))
        return known_elsewhere, strangers_elsewhere

    def _build_situation_context(
        self,
        agent_key: str,
        known:     list[str],
        strangers: list[tuple],
        zone_awareness: tuple | None = None,
    ) -> str | None:
        """현재 위치·이동 가능 장소·동석자 정보를 내러티브 user 메시지로 구성."""
        my_loc = self._agent_location.get(agent_key, "")
        if not my_loc:
            return None

        is_exterior = my_loc in self._exterior_locations
        lines = ["[현재 상황]", f"현재 위치: {my_loc}"]

        if is_exterior:
            lines.append("※ 이곳은 시뮬레이션 경계 밖의 공허한 공간입니다.")
            lines.append("  아무도 없고, 아무도 당신의 존재를 알지 못합니다.")
            lines.append("  이 공간에서의 말과 행동은 외부로 전달되지 않습니다.")
            adjacent = self._get_adjacent(my_loc)
            if adjacent:
                lines.append(f"  내부로 돌아가려면 move_to로 인접 장소({', '.join(adjacent)})를 선택하세요.")
            return "\n".join(lines)

        adjacent = self._get_adjacent(my_loc)
        if adjacent:
            my_zone_here = self._location_zone.get(my_loc, "")
            def _adj_label(loc: str) -> str:
                if my_zone_here and self._location_zone.get(loc, "") != my_zone_here:
                    return f"{loc} (구역 밖)"
                return loc
            shown = [_adj_label(loc) for loc in adjacent] if my_zone_here else adjacent
            lines.append(f"이동 가능한 장소: {', '.join(shown)}")

        path = self._agent_path.get(agent_key, [])
        if path:
            dest  = path[-1]
            steps = len(path)
            if steps == 1:
                lines.append(f"이동 중: {dest}까지 1칸 남음")
            else:
                lines.append(f"이동 중: {dest} 방향 ({steps}칸 남음, 다음: {path[0]})")

        # 사람을 만나러 가는 중이면 목적지 좌표가 아니라 **누구를** 쫓는지 보여준다.
        # 경로 줄만 있으면 도중에 상대가 움직여 목적지가 바뀔 때 이유를 알 수 없다.
        # (이미 같은 자리에 있으면 [이 자리의 사람들]에 나오므로 중복 표시하지 않는다.
        #  lock 자체는 다음 wave 시작 시 "동석"으로 정리된다.)
        meet_key = self._meeting_intent.get(agent_key)
        if meet_key and meet_key in self.active_agents:
            meet_loc = self._agent_location.get(meet_key, "")
            if meet_loc and meet_loc != my_loc:
                label = self._meeting_label(agent_key, meet_key)
                lines.append(f"{label}을(를) 만나러 이동 중 (현재 {label}는 {meet_loc}에 있음)")
                lines.append("※ 생각이 바뀌면 move_to에 다른 장소나 다른 사람을 지정하세요 — 만나러 가던 것은 취소됩니다.")

        if known or strangers:
            lines.append("")
            lines.append("[이 자리의 사람들]")
            if known:
                # 관계 지도가 있으면 이름·ID 옆에 화자 시점의 관계어를 붙인다
                # (`채민경 (ID: "채민경", 아내)`). 계약의 [아는 사람] 블록과 같은
                # 사람이라는 걸 매 턴 상황 컨텍스트에서도 다시 못박아 준다.
                my_rels = self._agent_relationships.get(agent_key, {})
                labels = []
                for k in known:
                    rel = (my_rels.get(k) or "").strip()
                    name = self._key_to_alias.get(k, k)
                    labels.append(
                        f'{name} (ID: "{k}", {rel})' if rel else f'{name} (ID: "{k}")'
                    )
                lines.append(f"아는 사람: {', '.join(labels)}")
            if strangers:
                lines.append("처음 보는 사람:")
                for sid, _, visual in strangers:
                    lines.append(f'  - ID: "{sid}"  {visual}' if visual else f'  - ID: "{sid}"')
        else:
            alone_msg = "이 자리에는 아무도 없다."
            if adjacent:
                alone_msg += f" move_to로 인접 장소({', '.join(adjacent)})로 이동할 수 있다."
            lines.append(alone_msg)

        # 같은 구역의 다른 장소 — 인지만 되고 대화는 불가 (말을 걸려면 이동해야 함)
        my_zone = self._location_zone.get(my_loc, "")
        known_elsewhere, strangers_elsewhere = zone_awareness or ([], [])
        if my_zone and (known_elsewhere or strangers_elsewhere):
            lines.append("")
            lines.append(f"[같은 구역({my_zone})의 다른 곳]")

            # 각 줄에 그 사람을 만나러 가는 **정확한 입력값**을 인라인으로 붙인다.
            # 블록 끝의 정적 안내("move_to로 이동하세요")만으로는 채택률이 낮았고,
            # 특히 "장소명을 넣어야 하나 ID를 넣어야 하나"에서 모델이 갈렸다.
            # 이미 그 사람을 만나러 가는 중(meet_key)이면 힌트를 생략한다 —
            # 위쪽 "만나러 이동 중" 줄이 이미 상태를 보여주고 있어 중복이다.
            def _meet_hint(real_key: str, addressable_id: str) -> str:
                if meet_key and real_key == meet_key:
                    return ""
                return f'  → 만나려면 move_to: "{addressable_id}"'

            if known_elsewhere:
                lines.append("아는 사람:")
                for k, loc in known_elsewhere:
                    label = self._key_to_alias.get(k, k)
                    lines.append(f'  - {label} (ID: "{k}") — {loc}{_meet_hint(k, k)}')
            if strangers_elsewhere:
                lines.append("처음 보는 사람:")
                for sid, real_key, visual, loc in strangers_elsewhere:
                    desc = f'  - ID: "{sid}"'
                    if visual:
                        desc += f"  {visual}"
                    # 낯선 이는 실명이 아니라 stranger_N ID로만 지목할 수 있다
                    # (`_resolve_meet_target`의 인지 규칙과 같은 제약).
                    lines.append(f"{desc} — {loc}{_meet_hint(real_key, sid)}")
            lines.append("※ 이들은 지금 다른 장소에 있어 말을 걸 수 없습니다. 대화하려면 move_to로 그 사람에게 가야 합니다.")

        return "\n".join(lines)
