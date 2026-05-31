import logging

from ..agent import Agent
from ..llm import chat_response
from ..parser import parse_json_extras
from ._constants import _COMPRESSION_THRESHOLD, _COMPRESSION_MIN_MSGS, _has_foreign_chars

logger = logging.getLogger(__name__)


class _StepMixin:
    """에이전트 단일 스텝 — LLM 호출 헬퍼 및 _step_agent."""

    def _compress_agent(self, agent: Agent, agent_key: str, wave: int):
        """Compress agent.memory into structured DB memory, then clear memory."""
        from ..memory_compressor import compress
        self._emit("compression_start", {"agent": agent_key, "wave": wave, "msg_count": len(agent.memory)})
        new_block = compress(
            agent_name   = agent.name,
            agent_key    = agent_key,
            sim_id       = self._sim_id,
            messages     = list(agent.memory),
            wave         = wave,
            db           = self._db,
            model        = self.model,
            base_url     = self.base_url,
            api_timeout  = self.api_timeout,
            key_to_alias = self._key_to_alias,
        )
        if new_block is not None:
            agent._memory_block = new_block
            agent.memory.clear()
            self._emit("compression_done", {"agent": agent_key, "wave": wave})
        else:
            logger.warning(f"[{agent_key}] 압축 실패 — 기존 메모리 유지, 강제 트림으로 폴백")

    def _inject_incoming(self, agent: Agent, incoming: list[dict]) -> list[dict]:
        """Inject incoming utterances into the agent's memory.

        Returns the list of formatted user messages that were appended, so the
        caller can pop them on LLM failure to keep memory clean for retries.
        """
        incoming_msgs = [
            {
                "role":    "user",
                "content": (
                    f"[{msg['speaker']}] {msg['content']}"
                    + (f"\n({msg['action_note']})" if msg.get("action_note") else "")
                ),
            }
            for msg in incoming
        ]
        for msg in incoming_msgs:
            agent.add_to_memory(msg)
        return incoming_msgs

    def _maybe_compress(
        self,
        agent:             Agent,
        agent_key:         str,
        wave:              int,
        other_agents:      list[str],
        target_sections:   list[tuple[str, list[str]]] | None = None,
        situation_targets: bool = False,
        ephemeral_msgs:    list[dict] | None = None,
    ) -> None:
        """Trigger structured-memory compression if context is approaching the token limit."""
        if (
            self._db is None
            or self._sim_id is None
            or len(agent.memory) < _COMPRESSION_MIN_MSGS
        ):
            return
        est = agent.estimate_context_tokens(
            self.background_log, other_agents, self._key_to_alias, target_sections,
            situation_targets=situation_targets, ephemeral_msgs=ephemeral_msgs,
        )
        if est / agent._token_limit >= _COMPRESSION_THRESHOLD:
            self._compress_agent(agent, agent_key, wave)

    def _call_llm_for_agent(
        self,
        agent:           Agent,
        agent_key:       str,
        visible_agents:  list[str],
        target_sections: list[tuple[str, list[str]]] | None = None,
    ) -> tuple[str | None, str, dict, str | None]:
        """Invoke the LLM for an agent. Returns (content, reasoning, usage, error)."""
        call_messages = agent.build_messages(
            self.background_log, visible_agents, self._key_to_alias, target_sections
        )
        return self._call_llm_for_agent_msgs(agent, agent_key, call_messages)

    def _call_llm_for_agent_msgs(
        self,
        agent:         Agent,
        agent_key:     str,
        call_messages: list,
    ) -> tuple[str | None, str, dict, str | None]:
        """Invoke the LLM with pre-built messages. Returns (content, reasoning, usage, error)."""
        try:
            content, reasoning, usage = chat_response(
                call_messages, self.model, self.base_url, self.api_timeout
            )
        except Exception as e:
            logger.error(f"응답 생성 실패 ({agent.name}): {e}")
            return None, "", {}, f"exception:{e}"

        if not content:
            logger.warning(f"빈 응답 ({agent.name})")
            return None, reasoning or "", usage or {}, "empty"

        return content, reasoning, usage, None

    def _retry_language_fix(
        self,
        agent:             Agent,
        agent_key:         str,
        visible_agents:    list[str],
        target_sections,
        bad_content:       str,
        max_retries:       int             = 2,
        key_to_alias:      dict | None     = None,
        location_name:     str             = "",
        situation_targets: bool            = False,
        ephemeral_msgs:    list[dict] | None = None,
    ) -> tuple[str | None, str, dict, str | None]:
        """한자 등 외국어가 섞인 응답을 최대 max_retries회 재시도로 교정."""
        CORRECTION_MSG = (
            "⚠ 방금 응답에 한국어가 아닌 문자(한자, 영어 등)가 포함되어 있습니다. "
            "content 필드를 반드시 한국어로만 다시 작성해주세요."
        )
        alias       = key_to_alias if key_to_alias is not None else self._key_to_alias
        current_bad = bad_content
        reasoning, usage = "", {}
        for attempt in range(1, max_retries + 1):
            fix_msgs = agent.build_messages(
                self.background_log, visible_agents, alias, target_sections,
                location_name, situation_targets, ephemeral_msgs,
            )
            fix_msgs.append({"role": "assistant", "content": current_bad})
            fix_msgs.append({"role": "user",      "content": CORRECTION_MSG})
            try:
                content, reasoning, usage = chat_response(
                    fix_msgs, self.model, self.base_url, self.api_timeout
                )
            except Exception as e:
                logger.error(f"언어 교잡 재시도 실패 ({agent.name}) attempt={attempt}: {e}")
                return None, "", {}, f"exception:{e}"
            if content and not _has_foreign_chars(content):
                logger.info(f"언어 교잡 수정 성공 ({agent.name}) attempt={attempt}")
                return content, reasoning, usage, None
            current_bad = content or current_bad
        logger.warning(f"언어 교잡 수정 미완 ({agent.name}): {max_retries}회 재시도 후 외국어 잔존")
        return current_bad, reasoning, usage, None

    def _step_agent(
        self,
        agent_key: str,
        wave:      int,
        turn:      int,
        incoming:  list[dict],
    ) -> dict:
        """단일 에이전트 한 스텝. 결과 dict 반환."""
        if self._stop_event.is_set():
            return {"success": False, "agent_key": agent_key}

        active_agent  = self.agents[agent_key]
        incoming_msgs = self._inject_incoming(active_agent, incoming)

        known, strangers = self._compute_wave_targets(agent_key)

        # 상황 컨텍스트는 메모리에 저장하지 않고 매 호출 시 ephemeral로 주입 (중복 누적 방지)
        situation_text = self._build_situation_context(agent_key, known, strangers)
        ephemeral_msgs: list[dict] = (
            [{"role": "user", "content": situation_text}] if situation_text else []
        )
        if situation_text:
            self._emit("turn_situation", {
                "wave":  wave,
                "agent": agent_key,
                "text":  situation_text,
            })

        extended_alias = dict(self._key_to_alias)
        for sid, _, visual in strangers:
            extended_alias[sid] = visual

        visible_agents = known + [sid for sid, _, _ in strangers]

        my_loc = self._agent_location.get(agent_key, "")
        target_sections: list[tuple[str, list[str]]] | None = None
        if known or strangers:
            sections: list[tuple[str, list[str]]] = []
            if known:
                sections.append(("아는 사람", known))
            if strangers:
                sections.append(("처음 보는 사람", [sid for sid, _, _ in strangers]))
            target_sections = sections if sections else None
        else:
            other_agents   = [k for k in self.active_agents if k != agent_key]
            visible_agents = [k for k in self._visible_targets.get(agent_key, other_agents)
                              if k in self.active_agents]
            target_sections = self._get_visible_sections(agent_key, visible_agents)

        # 동석자가 있을 때만 상황 컨텍스트로 target 제공 — 혼자면 기존 <TARGETS> 블록 유지
        sit_targets = bool(my_loc and (known or strangers))

        self._maybe_compress(
            active_agent, agent_key, wave, visible_agents, target_sections,
            sit_targets, ephemeral_msgs or None,
        )

        active_agent.trim_to_token_limit(
            self.background_log, visible_agents, extended_alias, target_sections,
            my_loc, sit_targets, ephemeral_msgs or None,
        )
        est_tokens = active_agent.estimate_context_tokens(
            self.background_log, visible_agents, extended_alias, target_sections,
            my_loc, sit_targets, ephemeral_msgs or None,
        )

        self._emit("turn_start", {
            "turn":        turn,
            "wave":        wave,
            "speaker":     agent_key,
            "memory_size": len(active_agent.memory),
            "est_tokens":  est_tokens,
            "token_limit": active_agent._token_limit,
        })

        call_messages = active_agent.build_messages(
            self.background_log, visible_agents, extended_alias, target_sections,
            my_loc, sit_targets, ephemeral_msgs or None,
        )

        content, reasoning, usage, error = self._call_llm_for_agent_msgs(
            active_agent, agent_key, call_messages
        )
        if error is not None:
            self._emit("turn_error", {
                "turn":    turn,
                "speaker": agent_key,
                "error":   "empty response" if error == "empty" else error.split(":", 1)[-1],
            })
            self._rollback_incoming(active_agent, incoming_msgs)
            return {"success": False, "agent_key": agent_key}

        if content and self._lang_fix_enabled and _has_foreign_chars(content):
            logger.warning(f"언어 교잡 감지 ({agent_key}): 재시도 시작")
            self._emit("turn_language_fix", {"speaker": agent_key, "wave": wave, "turn": turn})
            content, reasoning, usage, error = self._retry_language_fix(
                active_agent, agent_key, visible_agents, target_sections, content,
                max_retries=self._lang_fix_retries,
                key_to_alias=extended_alias, location_name=my_loc,
                situation_targets=sit_targets, ephemeral_msgs=ephemeral_msgs or None,
            )
            if error is not None:
                self._emit("turn_error", {
                    "turn":    turn,
                    "speaker": agent_key,
                    "error":   error.split(":", 1)[-1],
                })
                self._rollback_incoming(active_agent, incoming_msgs)
                return {"success": False, "agent_key": agent_key}

        extras = parse_json_extras(content)
        result = self._apply_turn_result(
            active_agent, agent_key, content, reasoning, usage, wave, turn, est_tokens,
        )
        result["move_to"]            = extras.get("move_to")
        result["update_appearance"]  = extras.get("update_appearance")
        return result
