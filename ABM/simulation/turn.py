import json
import time
import logging

from ..agent import Agent
from ..parser import parse_json_response

logger = logging.getLogger(__name__)


class _TurnMixin:
    """턴 결과 적용 및 메모리 롤백 관련 메서드."""

    def _apply_turn_result(
        self,
        agent:       Agent,
        agent_key:   str,
        raw_content: str,
        reasoning:   str,
        usage:       dict,
        wave:        int,
        turn:        int,
        est_tokens:  int,
        time_str:    str | None = None,
    ) -> dict:
        """Parse the LLM response, update memory/log/edges, emit events, persist to DB.

        파싱 불가(유효한 JSON 객체 없음)면 상태를 전혀 건드리지 않고
        ``{"success": False}`` 를 돌려준다 — 호출부(``_step_agent``)가 LLM 실패와
        똑같이 turn_error emit + incoming 롤백을 한다. 원본 JSON 텍스트가 대사로
        새어 shared_log·피드·메모리를 오염시키던 경로를 끊는다.
        """
        prompt_tokens = usage.get("prompt_tokens", est_tokens)
        agent._last_prompt_tokens = prompt_tokens

        clean_content, meta, parsed_targets, parsed = parse_json_response(raw_content, agent._extra_fields)
        if parsed is None:
            logger.warning(
                f"[{agent.name}] 턴 결과 파싱 실패 (유효한 JSON 객체 없음) — turn_error 로 처리. "
                f"원본: {raw_content[:200]}"
            )
            self._emit("turn_error", {
                "turn":    turn,
                "speaker": agent.name,
                "error":   "JSON 파싱 실패 (유효한 객체 없음)",
            })
            return {"success": False, "agent_key": agent_key}

        # 메모리에는 원본이 아니라 **정규화한 첫 객체**를 남긴다 — 모델이 자기
        # 히스토리의 깨진 형식(중첩 펜스·다중 객체·앞뒤 산문)을 다시 보고 같은
        # 패턴을 반복하는 루프를 끊는다. 정상 응답에서는 사실상 no-op(같은 내용).
        agent.add_to_memory({
            "role":    "assistant",
            "content": json.dumps(parsed, ensure_ascii=False),
        })
        agent.add_to_log(
            content=clean_content, reasoning=reasoning,
            extra=meta, targets=parsed_targets,
        )

        self._last_spoke_wave[agent.name] = wave

        # 이 턴 시점의 위치 = 이 wave의 이동이 적용되기 **전** 값(이동은 wave 안
        # 모든 턴이 끝난 뒤 runner에서 적용된다). 즉 이 에이전트가 이 wave 동안
        # 실제로 있던 장소이고, 접촉 분석이 필요로 하는 바로 그 값이다.
        # 아래 shared_log / turn_complete / DB 세 곳이 같은 스냅샷을 공유한다.
        agent_loc   = self._agent_location.get(agent_key, "")
        is_exterior = agent_loc in self._exterior_locations

        self.shared_log.append({
            "speaker":     agent.name,
            "content":     clean_content,
            "meta":        {k: v for k, v in meta.items() if k != "action_note"},
            "action_note": meta.get("action_note", ""),
            "targets":     parsed_targets,
            "wave":        wave,
            "timestamp":   time.time(),
            "time_str":    time_str,
            "location":    agent_loc,
            "is_exterior": is_exterior,
        })
        self._save_shared_log()

        emotion_val = meta.get("emotion", "")
        new_edges   = []
        for target_name in self._resolve_targets(parsed_targets, agent_key):
            edge = {
                "source":    agent.name,
                "target":    target_name,
                "emotion":   emotion_val,
                "meta":      dict(meta),
                "content":   clean_content,
                "timestamp": time.time(),
            }
            self.edges.append(edge)
            new_edges.append(edge)

        emit_meta = {k: v for k, v in meta.items() if k != "action_note"}
        self._emit("turn_complete", {
            "turn":              turn,
            "wave":              wave,
            "speaker":           agent.name,
            "targets":           parsed_targets,
            "content":           clean_content,
            "action_note":       meta.get("action_note", ""),
            "meta":              emit_meta,
            "memory_size":       len(agent.memory),
            "prompt_tokens":     prompt_tokens,
            "token_limit":       agent._token_limit,
            "reasoning_preview": reasoning[:120] if reasoning else "",
            "new_edges":         new_edges,
            "is_exterior":       is_exterior,
            "time_str":          time_str,
        })

        if self._db is not None and self._sim_id is not None:
            self._db.log_turn(
                self._sim_id, wave, turn,
                agent.name, clean_content,
                meta.get("action_note", ""),
                emit_meta, parsed_targets,
                time_str=time_str,
                location=agent_loc,
                is_exterior=is_exterior,
            )

        return {
            "success":       True,
            "agent_key":     agent_key,
            "clean_content": clean_content,
            "action_note":   meta.get("action_note", ""),
            "targets":       parsed_targets,
        }

    def _rollback_incoming(self, agent: Agent, incoming_msgs: list[dict]) -> None:
        """Pop the just-injected incoming messages off the agent's memory tail."""
        for _ in incoming_msgs:
            if agent.memory and agent.memory[-1]["role"] == "user":
                agent.memory.pop()
