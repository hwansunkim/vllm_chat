import logging

logger = logging.getLogger(__name__)


class _TargetsMixin:
    """에이전트 target 해석 및 가시성 관련 메서드."""

    def _build_visible_targets(self, agent_groups: dict[str, list[str]]) -> dict[str, list[str]]:
        """에이전트별 <TARGETS>에 노출할 다른 에이전트 key 목록 계산.

        groups가 빈 에이전트는 모든 에이전트를 볼 수 있음 (하위 호환).
        groups가 있으면 같은 그룹에 속한 에이전트만 노출.
        """
        all_keys = list(self.agents.keys())
        result: dict[str, list[str]] = {}
        for key in all_keys:
            groups = agent_groups.get(key, [])
            if not groups:
                result[key] = [k for k in all_keys if k != key]
            else:
                visible: set[str] = set()
                for gid in groups:
                    for other_key in all_keys:
                        if gid in agent_groups.get(other_key, []):
                            visible.add(other_key)
                visible.discard(key)
                result[key] = sorted(visible)
        return result

    def _normalize_target(self, t: str) -> str:
        """한국어 display_name → 시스템 key 정규화. 이미 key면 그대로 반환."""
        if t in self.agents:
            return t
        return self._alias_to_key.get(t, t)

    def _get_visible_sections(
        self, agent_key: str, visible_agents: list[str]
    ) -> list[tuple[str, list[str]]] | None:
        """그룹 소속 에이전트의 <TARGETS>를 그룹-섹션으로 분류.

        그룹 미설정 에이전트는 None 반환 (flat 목록 유지, 하위 호환).
        """
        my_groups = self._agent_groups.get(agent_key, [])
        if not my_groups:
            return None

        sections: list[tuple[str, list[str]]] = []
        seen: set[str] = set()
        for gid in my_groups:
            members = [
                k for k in visible_agents
                if k not in seen and gid in self._agent_groups.get(k, [])
            ]
            if members:
                sections.append((gid, members))
                seen.update(members)

        ungrouped = [k for k in visible_agents if k not in seen]
        if ungrouped:
            sections.append(("기타", ungrouped))

        return sections or None

    def _resolve_targets(self, targets: list[str], speaker_key: str) -> list[str]:
        """발화 target 해석 — active_agents 기준, 위치 기반 필터링 적용.

        지원 형식:
          "all"        → 화자와 같은 위치의 활성 에이전트
          "group:X"    → 그룹 X 소속 + 같은 위치 에이전트
          "stranger_N" → 해당 낯선 이 (real_key 변환 + knowledge 확장)
          "<key>"      → 특정 에이전트 (같은 위치일 때만)
        위치 미설정 에이전트는 기존 동작 유지 (하위 호환).
        외부 공간 에이전트는 아무에게도 메시지를 전달할 수 없음.
        """
        speaker_loc = self._agent_location.get(speaker_key, "")
        if speaker_loc in self._exterior_locations:
            return []  # 외부 공간 화자 — 메시지 전달 불가

        resolved    = []
        visible_set = set(self._visible_targets.get(speaker_key, []))

        def _same_loc(key: str) -> bool:
            """위치 시스템 활성 시 같은 위치인지 확인. 위치 미설정이면 항상 True."""
            if not speaker_loc:
                return True
            other_loc = self._agent_location.get(key, "")
            if not other_loc:
                return True
            if speaker_loc == other_loc:
                return True
            # 위치 불일치로 타깃이 폐기되는 경로. 무성 폐기(silent drop)는 웨이브가
            # 통째로 비는 원인이 되므로 추적 가능하도록 남긴다.
            logger.debug(
                "target drop (location mismatch): speaker=%s@%s target=%s@%s",
                speaker_key, speaker_loc, key, other_loc,
            )
            return False

        for t in targets:
            t_s = t.strip()
            if t_s.lower() in ("self", "system"):
                continue
            elif t_s.lower() == "all":
                my_groups = self._agent_groups.get(speaker_key, [])
                if my_groups:
                    candidates = (k for k in visible_set if k in self.active_agents)
                else:
                    candidates = (k for k in self.active_agents if k != speaker_key)
                resolved.extend(k for k in candidates if _same_loc(k))
            elif t_s.lower().startswith("group:"):
                gid = t_s[6:]
                resolved.extend(
                    k for k in visible_set
                    if k in self.active_agents
                    and gid in self._agent_groups.get(k, [])
                    and _same_loc(k)
                )
            elif t_s.startswith("stranger_"):
                # stranger_N ID는 같은 장소에서뿐 아니라 같은 zone의 다른 장소를
                # 인지할 때도 발급된다. 대화 가능 범위는 어디까지나 "같은 장소"이므로
                # 여기서도 _same_loc()로 걸러야 zone 인지가 대화 채널로 새지 않는다.
                real_key = self._stranger_map.get(speaker_key, {}).get(t_s)
                if real_key and real_key in self.active_agents and _same_loc(real_key):
                    self._agent_knowledge.setdefault(speaker_key, set()).add(real_key)
                    self._agent_knowledge.setdefault(real_key, set()).add(speaker_key)
                    resolved.append(real_key)
            else:
                key = self._normalize_target(t_s)
                if key in self.active_agents and key != speaker_key and _same_loc(key):
                    resolved.append(key)
        return list(dict.fromkeys(resolved))

    def _resolve_event_targets(self, targets: list[str]) -> list[str]:
        """이벤트 알림 대상 해석 — active_agents 기준 (speaker 제한 없음).

        지원 형식:
          "all"      → 모든 활성 에이전트
          "group:X"  → 그룹 X 소속 활성 에이전트 전원
          "<key>"    → 특정 에이전트
        """
        if any(t.strip().lower() == "all" for t in targets):
            return list(self.active_agents)
        resolved = []
        for t in targets:
            t_s = t.strip()
            if t_s.lower().startswith("group:"):
                gid = t_s[6:]
                for key in self.active_agents:
                    if gid in self._agent_groups.get(key, []):
                        resolved.append(key)
            else:
                key = self._normalize_target(t_s)
                if key in self.active_agents:
                    resolved.append(key)
        return list(dict.fromkeys(resolved))
