from collections import deque


class _LocationMixin:
    """위치 그래프, BFS 경로 탐색, 상황 컨텍스트 관련 메서드."""

    def _find_path(self, start: str, goal: str) -> list[str]:
        """BFS 최단 경로. 시작 제외, 목표 포함. 그래프 없으면 [goal] 반환."""
        if start == goal:
            return []
        if not self._location_graph or start not in self._location_graph or goal not in self._location_graph:
            return [goal]  # 그래프 없음 또는 미등록 위치 → 직접 이동 (하위 호환)
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
        return [goal]  # 연결 경로 없음 → 직접 이동

    def _get_adjacent(self, location: str) -> list[str]:
        """현재 위치에서 이동 가능한 인접 장소 목록."""
        return list(self._location_graph.get(location, []))

    def _get_or_assign_stranger_id(self, observer_key: str, target_key: str) -> str:
        """observer가 target을 처음 보는 경우 stranger_N ID를 할당, 이미 있으면 기존 ID 반환."""
        self._stranger_map.setdefault(observer_key, {})
        self._stranger_rmap.setdefault(observer_key, {})
        if target_key in self._stranger_rmap[observer_key]:
            return self._stranger_rmap[observer_key][target_key]
        n = len(self._stranger_map[observer_key]) + 1
        sid = f"stranger_{n}"
        self._stranger_map[observer_key][sid] = target_key
        self._stranger_rmap[observer_key][target_key] = sid
        return sid

    def _compute_wave_targets(self, agent_key: str):
        """현재 위치 기준 대화 가능 에이전트 계산.

        Returns (known: list[str], strangers: list[tuple[stranger_id, real_key, visual]])
        위치가 설정된 경우에만 필터링, 미설정 시 기존 동작.
        """
        my_loc    = self._agent_location.get(agent_key, "")
        knowledge = self._agent_knowledge.get(agent_key, set())
        known:     list[str]                    = []
        strangers: list[tuple[str, str, str]]   = []
        for other_key in self.active_agents:
            if other_key == agent_key:
                continue
            other_loc = self._agent_location.get(other_key, "")
            if my_loc and other_loc and my_loc != other_loc:
                continue
            if other_key in knowledge:
                known.append(other_key)
            else:
                sid    = self._get_or_assign_stranger_id(agent_key, other_key)
                visual = self._agent_visual.get(other_key, "") or self._key_to_alias.get(other_key, other_key)
                strangers.append((sid, other_key, visual))
        return known, strangers

    def _build_situation_context(
        self,
        agent_key: str,
        known:     list[str],
        strangers: list[tuple],
    ) -> str | None:
        """현재 위치·이동 가능 장소·동석자 정보를 내러티브 user 메시지로 구성."""
        my_loc = self._agent_location.get(agent_key, "")
        if not my_loc:
            return None

        lines = ["[현재 상황]", f"현재 위치: {my_loc}"]

        adjacent = self._get_adjacent(my_loc)
        if adjacent:
            lines.append(f"이동 가능한 장소: {', '.join(adjacent)}")

        path = self._agent_path.get(agent_key, [])
        if path:
            dest  = path[-1]
            steps = len(path)
            if steps == 1:
                lines.append(f"이동 중: {dest}까지 1칸 남음")
            else:
                lines.append(f"이동 중: {dest} 방향 ({steps}칸 남음, 다음: {path[0]})")

        if known or strangers:
            lines.append("")
            lines.append("[이 자리의 사람들]")
            if known:
                labels = [f'{self._key_to_alias.get(k, k)} (ID: "{k}")' for k in known]
                lines.append(f"아는 사람: {', '.join(labels)}")
            if strangers:
                lines.append("처음 보는 사람:")
                for sid, _, visual in strangers:
                    lines.append(f'  - ID: "{sid}"  {visual}' if visual else f'  - ID: "{sid}"')
        else:
            lines.append("이 자리에는 아무도 없다.")

        return "\n".join(lines)
