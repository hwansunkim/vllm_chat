from ._constants import _REPEAT_WINDOW, _REPEAT_THRESHOLD, _MEMO_MAX_LINES, _repetition_score


class _SystemMixin:
    """system 에이전트 실행 (개입, world_event, director_memo 갱신)."""

    def _run_system_agent(self, wave_num: int, current_wave: dict) -> dict:
        """system 에이전트를 실행해 개입/월드이벤트를 주입. 수정된 current_wave 반환."""
        from ..system_agent import run_system_agent

        silent = [
            key for key in self.active_agents
            if wave_num - self._last_spoke_wave.get(key, -1) >= self._sys_threshold
        ]

        repetition_info: dict[str, float] = {}
        for key in self.active_agents:
            agent_name = self.agents[key].name
            recent = [
                e["content"] for e in self.shared_log[-30:]
                if e["speaker"] == agent_name
            ][-_REPEAT_WINDOW:]
            score = _repetition_score(recent)
            if score >= _REPEAT_THRESHOLD:
                repetition_info[key] = round(score, 2)

        result = run_system_agent(
            system_prompt     = self._sys_prompt,
            wave              = wave_num,
            summary           = self._last_summary,
            active_agents     = {k: self._key_to_alias.get(k, k) for k in self.active_agents},
            silent_agents     = silent,
            silence_threshold = self._sys_threshold,
            repetition_info   = repetition_info,
            director_note     = self._director_note,
            director_memo     = self._director_memo,
            key_to_alias      = self._key_to_alias,
            model             = self.model,
            base_url          = self.base_url,
            api_timeout       = self.api_timeout,
        )
        if not result:
            return current_wave

        wave_copy = {k: list(v) for k, v in current_wave.items()}
        reason    = result.get("reason", "")

        new_memo = (result.get("director_memo") or "").strip()
        if new_memo:
            entry = f"Wave {wave_num}: {new_memo}"
            lines = [l for l in self._director_memo.splitlines() if l.strip()]
            if len(lines) >= _MEMO_MAX_LINES:
                lines = lines[-(_MEMO_MAX_LINES - 1):]
            self._director_memo = "\n".join(lines + [entry])

        for iv in (result.get("interventions") or []):
            agent_key = self._normalize_target(iv.get("agent", ""))
            message   = (iv.get("message") or "").strip()
            if not agent_key or agent_key not in self.active_agents or not message:
                continue
            wave_copy.setdefault(agent_key, []).append({
                "speaker":     self._sys_name,
                "content":     message,
                "action_note": "",
            })
            self._emit("system_intervention", {
                "wave":         wave_num,
                "target":       agent_key,
                "target_alias": self._key_to_alias.get(agent_key, agent_key),
                "message":      message,
                "reason":       reason,
                "icon":         self._sys_icon,
                "display_name": self._sys_name,
            })

        we = result.get("world_event")
        if we and isinstance(we, dict):
            we_content = (we.get("content") or "").strip()
            we_targets = we.get("targets") or ["all"]
            if we_content:
                target_keys: set[str] = set()
                for t in we_targets:
                    if t == "all":
                        target_keys.update(self.active_agents)
                    elif isinstance(t, str) and t.startswith("group:"):
                        gname = t[6:]
                        for k in self.active_agents:
                            if gname in self._agent_groups.get(k, []):
                                target_keys.add(k)
                    elif t in self.active_agents:
                        target_keys.add(t)

                for key in target_keys:
                    wave_copy.setdefault(key, []).append({
                        "speaker":     "세계 사건",
                        "content":     we_content,
                        "action_note": "",
                    })

                sorted_targets = sorted(target_keys)
                self._emit("world_event", {
                    "wave":           wave_num,
                    "content":        we_content,
                    "targets":        sorted_targets,
                    "target_aliases": [self._key_to_alias.get(k, k) for k in sorted_targets],
                    "icon":           self._sys_icon,
                    "display_name":   self._sys_name,
                    "reason":         reason,
                })

        return wave_copy
