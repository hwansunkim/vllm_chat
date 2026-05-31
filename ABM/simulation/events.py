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
            for name in self._resolve_event_targets(targets):
                if name == agent_key:
                    continue
                other_loc = self._agent_location.get(name, "")
                if my_loc and other_loc and my_loc != other_loc:
                    continue
                self.agents[name].add_to_memory({
                    "role":    "user",
                    "content": f"[씬] {display}의 외모가 변했다: {message}",
                })
            logger.info(f"[외모 변경] {agent_key}: {message}")

        return result
