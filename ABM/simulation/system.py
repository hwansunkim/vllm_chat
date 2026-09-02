from ._constants import (
    _REPEAT_WINDOW, _REPEAT_THRESHOLD, _MEMO_MAX_LINES,
    _repetition_score, _normalize_utterance,
)


class _SystemMixin:
    """system 에이전트 실행 (개입, world_event, director_memo 갱신)."""

    def _run_system_agent(self, wave_num: int, current_wave: dict) -> dict:
        """system 에이전트를 실행해 개입/월드이벤트를 주입. 수정된 current_wave 반환.

        `wave_num`은 runner가 넘기는 **누적 표시 wave(disp_wave)**다 — 디렉터 프롬프트의
        "Wave N" 표기, `system_intervention`/`world_event` 이벤트 라벨, director_memo에
        쓰인다. 시각 계산만은 per-run 축이어야 하므로 `self.completed_waves`를 쓴다
        (디렉터는 wave 시작 시점에 도므로 이 값이 곧 '이번 wave 진입 직전까지 완료된
        wave 수' = 옛 per-run wave_num과 동일하다).
        """
        from ..system_agent import run_system_agent

        # 디렉터는 wave_num **시작** 시점에 돈다 → `_last_spoke_wave`는 wave_num-1까지만
        # 반영돼 있다(turn.py가 발화 후에 갱신). "지금까지 몇 wave 연속 침묵인가"는
        # (wave_num - 1) 기준으로 세야, 디렉터가 wave 끝에서 돌던 이전 배치와 임계값
        # 의미가 같다. wave_num을 그대로 쓰면 실효 임계값이 1 줄어든다.
        silent = [
            key for key in self.active_agents
            if (wave_num - 1) - self._last_spoke_wave.get(key, -1) >= self._sys_threshold
        ]

        # 반복 판정은 이 에이전트가 최근 턴에 **표현한 것**(대사 우선, 없으면 행동
        # 묘사) 기준이다. content="..."(말 안 함)만 뽑아 비교하면 과묵한 캐릭터가
        # 매 interval 반복으로 오탐돼 디렉터가 그 한 명만 계속 붙잡는다.
        repetition_info: dict[str, float] = {}
        for key in self.active_agents:
            agent_name = self.agents[key].name
            recent_entries = [
                e for e in self.shared_log[-30:]
                if e.get("speaker") == agent_name
            ][-_REPEAT_WINDOW:]
            recent = [
                u for e in recent_entries
                if (u := _normalize_utterance(e.get("content", ""), e.get("action_note", ""))) is not None
            ]
            score = _repetition_score(recent)   # 유효 항목 2개 미만이면 0.0
            if score >= _REPEAT_THRESHOLD:
                repetition_info[key] = round(score, 2)

        # 디렉터에게 보여줄 현재 시각. 에이전트 프롬프트
        # (`_assemble_agent_prompt`)와 **정확히 같은 식**을 쓴다 — 둘이 갈라지면
        # 디렉터의 world_event 시각과 에이전트가 보는 시계가 어긋난다.
        # 시간 개념이 꺼져 있으면 빈 문자열 → [현재 시각] 섹션 자체가 생략된다.
        current_time_str = ""
        if self._time_mode == "variable" or self._time_per_wave > 0:
            current_time_str = self._format_time_str(
                self._sim_start_minutes
                + self._current_elapsed_minutes(self.completed_waves)
            )

        result = run_system_agent(
            system_prompt     = self._sys_prompt,
            wave              = wave_num,
            current_time_str  = current_time_str,
            summary           = self._last_summary,
            active_agents     = {k: self._key_to_alias.get(k, k) for k in self.active_agents},
            silent_agents     = silent,
            silence_threshold = self._sys_threshold,
            repetition_info   = repetition_info,
            director_note     = self._director_note,
            director_memo     = self._director_memo,
            key_to_alias      = self._key_to_alias,
            llm               = self._llm,
            llm_max_tokens    = self.llm_max_tokens,
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
