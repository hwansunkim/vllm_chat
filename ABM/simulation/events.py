import logging

logger = logging.getLogger(__name__)


class _EventsMixin:
    """시나리오 이벤트 실행 관련 메서드."""

    def _execute_event(self, event: dict) -> dict:
        """시나리오 이벤트 실행. agent_enter 시 entrant 키 반환."""
        etype     = event.get("type", "")
        message   = event.get("message", "")
        targets   = event.get("targets", ["all"])
        agent_key = event.get("agent", "")
        result    = {}

        if etype == "system_message":
            resolved = self._resolve_event_targets(targets)
            for name in resolved:
                self.agents[name].add_to_memory({
                    "role":    "user",
                    "content": f"[시스템] {message}",
                })
            self._emit("scene_event", {
                "event_type": "system_message",
                "message":    message,
                "targets":    resolved,
            })
            logger.info(f"[시스템 메시지] → {resolved}: {message}")

        elif etype == "agent_enter":
            if not agent_key or agent_key not in self.agents:
                logger.warning(f"agent_enter: 알 수 없는 에이전트 '{agent_key}'")
                return result
            self.active_agents.add(agent_key)
            inject_msg = message or f"{agent_key}이(가) 등장했다."
            for name in self._resolve_event_targets(targets):
                if name != agent_key:
                    self.agents[name].add_to_memory({
                        "role":    "user",
                        "content": f"[시스템] {inject_msg}",
                    })
            self.agents[agent_key].add_to_memory({
                "role":    "user",
                "content": f"[시스템] {inject_msg}",
            })
            self._emit("scene_event", {
                "event_type": "agent_enter",
                "agent":      agent_key,
                "message":    inject_msg,
            })
            logger.info(f"[에이전트 등장] {agent_key}: {inject_msg}")
            result["entrant"] = agent_key

        elif etype == "agent_exit":
            if not agent_key or agent_key not in self.active_agents:
                logger.warning(f"agent_exit: '{agent_key}'는 활성 에이전트가 아님")
                return result
            self.active_agents.discard(agent_key)
            self._pending_wave.pop(agent_key, None)
            exit_msg = message or f"{agent_key}이(가) 퇴장했다."
            for name in self._resolve_event_targets(targets):
                self.agents[name].add_to_memory({
                    "role":    "user",
                    "content": f"[시스템] {exit_msg}",
                })
            self._emit("scene_event", {
                "event_type": "agent_exit",
                "agent":      agent_key,
                "message":    exit_msg,
            })
            logger.info(f"[에이전트 퇴장] {agent_key}: {exit_msg}")

        elif etype == "infect_agent":
            # 환자 0번 시드. 감염 모델이 꺼져 있으면 조용히 무시한다(하위 호환).
            if not self._infection_enabled:
                logger.info(f"infect_agent 무시 — 감염 모델 비활성 ('{agent_key}')")
                return result
            if not agent_key or agent_key not in self.agents:
                logger.warning(f"infect_agent: 알 수 없는 에이전트 '{agent_key}'")
                return result
            # 이벤트는 시작 시점에 실행되므로 "이번 wave에 감염됨"으로 기록한다.
            # message는 관전용 이벤트 피드에만 쓰이고 에이전트 메모리에는 넣지 않는다 —
            # LLM은 오직 증상 서사(_build_symptom_context)로만 자기 몸 상태를 인지한다.
            wave = int(event.get("wave", 0) or 0)
            if self._set_infected(agent_key, wave, "event"):
                self._emit("scene_event", {
                    "event_type": "infect_agent",
                    "agent":      agent_key,
                    "message":    message or f"{agent_key}이(가) 감염되었다.",
                    "observer_only": True,
                })
            logger.info(f"[감염 시드] {agent_key} (wave {wave})")

        elif etype == "update_appearance":
            if not agent_key or agent_key not in self.agents:
                logger.warning(f"update_appearance: 알 수 없는 에이전트 '{agent_key}'")
                return result
            self._agent_visual[agent_key] = message
            display = self._key_to_alias.get(agent_key, agent_key)
            self._emit("appearance_update", {
                "wave": 0, "agent": agent_key,
                "display_name": display,
                "description":  message,
            })
            my_loc = self._agent_location.get(agent_key, "")
            # 외부 공간(is_exterior)은 완전 격리 — 외모는 갱신·emit 하되 씬 메시지는
            # 브로드캐스트하지 않는다. 런타임 경로(runner.py)와 같은 규칙.
            if my_loc not in self._exterior_locations:
                for name in self._resolve_event_targets(targets):
                    if name == agent_key:
                        continue
                    other_loc = self._agent_location.get(name, "")
                    if other_loc in self._exterior_locations:
                        continue  # 외부 공간의 에이전트에게는 씬 메시지 전달 안 함
                    if my_loc and other_loc and my_loc != other_loc:
                        continue
                    self.agents[name].add_to_memory({
                        "role":    "user",
                        # 아는 사이가 아니면 실명 대신 stranger_N ID로 익명화한다.
                        "content": self._appearance_scene_msg(
                            agent_key, name, display, message
                        ),
                    })
            logger.info(f"[외모 변경] {agent_key}: {message}")

        return result
