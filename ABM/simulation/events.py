import logging

logger = logging.getLogger(__name__)


class _EventsMixin:
    """시나리오 이벤트 실행 관련 메서드."""

    def _execute_event(self, event: dict) -> dict:
        """시나리오 이벤트 실행. agent_enter 시 entrant 키, system_message 시
        notified 키(알림 받은 활성 에이전트 목록) 반환 — 둘 다 runner 가 이번
        wave의 current_wave 에 강제로 끼워 넣는 데 쓴다.

        ``event["wave"]`` 는 이 이벤트가 발동한 disp_wave — runner 가 wave 트리거·
        at_time 트리거 모두 넣어준다. emit 페이로드에 실어야 DB·피드·마크다운
        내보내기가 이벤트를 올바른 wave 에 배치한다(안 넣으면 `_emit` 이 0 으로
        기록해 전부 Wave 0 에 몰린다).

        ``event["at_minutes"]`` 는 이 wave 시작 시점의 실제 경과 분 — 이벤트
        감염의 시각 앵커 전용이다. `_set_infected(wave)` 에 disp_wave 를 넘겨
        `_current_elapsed_minutes` 로 환산하면 재개 후 fixed 모드에서 누적분을
        두 번 세어 감염 시각이 미래로 밀린다(증상·회복 단계가 지연).

        `system_message`의 ``notified``: `add_to_memory`는 대상의 `memory`에
        메시지를 얹을 뿐, 그 대상이 **이번 wave에 실제로 턴을 받는다는 보장은
        없다** — current_wave(이번 wave 발화 후보)는 지난 wave의 라우팅으로 이미
        정해져 있어서, 알림을 받은 에이전트가 마침 그 목록에 없으면 "16:30.
        태권도학원 갈 시간이다" 같은 예정 알림이 memory에 조용히 쌓이기만 하고
        그가 자연히 다시 초대될 때까지(누가 그를 부르거나, 전원 휴면 강제
        재투입이 오거나) 반응이 미뤄질 수 있다. `agent_enter`가 이미 `entrant`로
        강제 편입을 하고 있으니, 같은 원칙을 `system_message`에도 적용한다 —
        시각을 알려주는 시스템이 "이 사실을 알렸다"면 "그 사실을 안 사람이 이번
        wave에 반응할 기회"까지 같이 보장해야 실제 발화 시점이 예정 시각 근처에
        머문다.
        """
        etype      = event.get("type", "")
        message    = event.get("message", "")
        targets    = event.get("targets", ["all"])
        agent_key  = event.get("agent", "")
        wave       = int(event.get("wave", 0) or 0)
        at_minutes = event.get("at_minutes")
        result     = {}

        if etype == "system_message":
            resolved = self._resolve_event_targets(targets)
            for name in resolved:
                self.agents[name].add_to_memory({
                    "role":    "user",
                    "content": f"[시스템] {message}",
                }, elapsed_minutes=at_minutes)
            self._emit("scene_event", {
                "event_type": "system_message",
                "wave":       wave,
                "message":    message,
                "targets":    resolved,
            })
            logger.info(f"[시스템 메시지] → {resolved}: {message}")
            result["notified"] = resolved

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
                    }, elapsed_minutes=at_minutes)
            self.agents[agent_key].add_to_memory({
                "role":    "user",
                "content": f"[시스템] {inject_msg}",
            }, elapsed_minutes=at_minutes)
            self._emit("scene_event", {
                "event_type": "agent_enter",
                "wave":       wave,
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
                }, elapsed_minutes=at_minutes)
            self._emit("scene_event", {
                "event_type": "agent_exit",
                "wave":       wave,
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
            if self._set_infected(agent_key, wave, "event", at_minutes=at_minutes):
                self._emit("scene_event", {
                    "event_type": "infect_agent",
                    "wave":       wave,
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
                "wave": wave, "agent": agent_key,
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
                    }, elapsed_minutes=at_minutes)
            logger.info(f"[외모 변경] {agent_key}: {message}")

        return result
