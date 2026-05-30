import json
import time
import os
import queue
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from .agent import Agent
from .llm import chat_response
from .parser import parse_json_response
from .config import LOG_DIR, MODEL, BASE_URL, API_TIMEOUT

logger = logging.getLogger(__name__)


_COMPRESSION_THRESHOLD = 0.70   # trigger compression at this fraction of token_limit
_COMPRESSION_MIN_MSGS  = 4      # don't compress until agent has at least this many memory entries


class Simulation:
    def __init__(
        self,
        agents:          dict[str, Agent],
        background_log:  list,
        log_dir:         str                    = LOG_DIR,
        model:           str                    = MODEL,
        base_url:        str                    = BASE_URL,
        api_timeout:     int                    = API_TIMEOUT,
        event_queue:     queue.Queue | None     = None,
        stop_event:      threading.Event | None = None,
        initial_agents:  list[str] | None       = None,
        name_aliases:    dict[str, str] | None  = None,
        sim_id:          str | None             = None,
        db=None,  # SimDB | None — avoid import cycle with type annotation
        agent_groups:    dict[str, list[str]] | None = None,  # key → group IDs
    ):
        self.agents         = agents
        self.background_log = background_log
        self.log_dir        = log_dir
        self.model          = model
        self.base_url       = base_url
        self.api_timeout    = api_timeout
        self.shared_log: list = list(background_log)
        self.edges:      list = []
        self._event_queue = event_queue
        self._stop_event  = stop_event or threading.Event()
        self._file_lock   = threading.Lock()

        # display_name(한국어) → 시스템 key 매핑 (LLM이 한국어 이름을 사용해도 올바른 키로 resolve)
        self._alias_to_key: dict[str, str] = name_aliases or {}
        # key → display_name (OUTPUT FORMAT에 표시)
        self._key_to_alias: dict[str, str] = {v: k for k, v in self._alias_to_key.items()}

        self._sim_id = sim_id
        self._db     = db
        self.completed_waves: int = 0
        self._pending_wave: dict  = {}  # last targeted agents not yet responded

        # 초기 활성 에이전트 — None이면 전체 활성
        if initial_agents is not None:
            self.active_agents: set[str] = set(initial_agents) & set(agents.keys())
        else:
            self.active_agents = set(agents.keys())

        # 그룹 기반 <TARGETS> 가시성 — 에이전트별 볼 수 있는 다른 에이전트 목록
        self._agent_groups: dict[str, list[str]] = agent_groups or {}
        self._visible_targets: dict[str, list[str]] = self._build_visible_targets(
            self._agent_groups
        )

        os.makedirs(log_dir, exist_ok=True)
        self._save_shared_log()

    # ------------------------------------------------------------------
    # 이벤트 / 파일 I/O
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, data: dict):
        if self._event_queue is not None:
            self._event_queue.put({"type": event_type, "data": data})

    def _save_shared_log(self):
        path = os.path.join(self.log_dir, "shared_log.json")
        with self._file_lock:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.shared_log, f, ensure_ascii=False, indent=2)

    def _save_edges(self):
        path = os.path.join(self.log_dir, "edges.json")
        with self._file_lock:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.edges, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # 대상 해석
    # ------------------------------------------------------------------

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
                # 그룹 미설정 → 전체 노출 (기존 동작)
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
        """발화 target 해석 — active_agents 기준.

        지원 형식:
          "all"       → 화자 그룹 내 활성 에이전트 (그룹 미소속 시 전체)
          "group:X"   → 그룹 X 소속 활성 에이전트 중 화자가 볼 수 있는 멤버만
          "<key>"     → 특정 에이전트
        브릿지 에이전트(복수 그룹)는 "all" 시 모든 가시 에이전트, "group:X" 시 해당 그룹만.
        """
        resolved = []
        visible_set = set(self._visible_targets.get(speaker_key, []))
        for t in targets:
            t_s = t.strip()
            if t_s.lower() == "all":
                my_groups = self._agent_groups.get(speaker_key, [])
                if my_groups:
                    resolved.extend(k for k in visible_set if k in self.active_agents)
                else:
                    resolved.extend(k for k in self.active_agents if k != speaker_key)
            elif t_s.lower().startswith("group:"):
                gid = t_s[6:]
                # 화자가 볼 수 있는 에이전트 중 해당 그룹 소속만 전달
                resolved.extend(
                    k for k in visible_set
                    if k in self.active_agents and gid in self._agent_groups.get(k, [])
                )
            else:
                key = self._normalize_target(t_s)
                if key in self.active_agents and key != speaker_key:
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

    # ------------------------------------------------------------------
    # 시나리오 이벤트 실행
    # ------------------------------------------------------------------

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
            # 기존 활성 에이전트들에게 등장 알림
            for name in self._resolve_event_targets(targets):
                if name != agent_key:
                    self.agents[name].add_to_memory({
                        "role":    "user",
                        "content": f"[시스템] {inject_msg}",
                    })
            # 진입 에이전트 자신에게도 상황 주입
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

        return result

    # ------------------------------------------------------------------
    # 메모리 압축
    # ------------------------------------------------------------------

    def _compress_agent(self, agent: Agent, agent_key: str, wave: int):
        """Compress agent.memory into structured DB memory, then clear memory."""
        from .memory_compressor import compress
        self._emit("compression_start", {"agent": agent_key, "wave": wave, "msg_count": len(agent.memory)})
        new_block = compress(
            agent_name=agent.name,
            agent_key=agent_key,
            sim_id=self._sim_id,
            messages=list(agent.memory),
            wave=wave,
            db=self._db,
            model=self.model,
            base_url=self.base_url,
            api_timeout=self.api_timeout,
            key_to_alias=self._key_to_alias,
        )
        if new_block is not None:
            agent._memory_block = new_block
            agent.memory.clear()
            self._emit("compression_done", {"agent": agent_key, "wave": wave})
        else:
            logger.warning(f"[{agent_key}] 압축 실패 — 기존 메모리 유지, 강제 트림으로 폴백")

    # ------------------------------------------------------------------
    # 에이전트 단일 스텝 — _step_agent decomposed into helpers
    # ------------------------------------------------------------------

    def _inject_incoming(self, agent: Agent, incoming: list[dict]) -> list[dict]:
        """Inject incoming utterances into the agent's memory.

        Returns the list of formatted user messages that were appended, so the
        caller can pop them on LLM failure to keep memory clean for retries.
        """
        incoming_msgs = [
            {
                "role": "user",
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
        agent: Agent,
        agent_key: str,
        wave: int,
        other_agents: list[str],
        target_sections: list[tuple[str, list[str]]] | None = None,
    ) -> None:
        """Trigger structured-memory compression if context is approaching the token limit."""
        if (
            self._db is None
            or self._sim_id is None
            or len(agent.memory) < _COMPRESSION_MIN_MSGS
        ):
            return
        est = agent.estimate_context_tokens(
            self.background_log, other_agents, self._key_to_alias, target_sections
        )
        if est / agent._token_limit >= _COMPRESSION_THRESHOLD:
            self._compress_agent(agent, agent_key, wave)

    def _call_llm_for_agent(
        self,
        agent: Agent,
        agent_key: str,
        visible_agents: list[str],
        target_sections: list[tuple[str, list[str]]] | None = None,
    ) -> tuple[str | None, str, dict, str | None]:
        """Invoke the LLM for an agent. Returns (content, reasoning, usage, error).

        On success `error` is None. On failure `content` is None and `error`
        carries a short reason ("exception:..." or "empty"). The caller is
        responsible for popping injected memory entries and emitting events.
        """
        call_messages = agent.build_messages(
            self.background_log, visible_agents, self._key_to_alias, target_sections
        )
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

    def _apply_turn_result(
        self,
        agent: Agent,
        agent_key: str,
        raw_content: str,
        reasoning: str,
        usage: dict,
        wave: int,
        turn: int,
        est_tokens: int,
    ) -> dict:
        """Parse the LLM response, update memory/log/edges, emit events, persist to DB.

        Returns the per-turn result dict used by the wave coordinator.
        """
        prompt_tokens = usage.get("prompt_tokens", est_tokens)
        agent._last_prompt_tokens = prompt_tokens

        clean_content, meta, parsed_targets = parse_json_response(raw_content, agent._extra_fields)

        agent.add_to_memory({"role": "assistant", "content": raw_content})
        agent.add_to_log(
            content=clean_content, reasoning=reasoning,
            extra=meta, targets=parsed_targets,
        )

        self.shared_log.append({
            "speaker":     agent.name,
            "content":     clean_content,
            "meta":        {k: v for k, v in meta.items() if k != "action_note"},
            "action_note": meta.get("action_note", ""),
            "targets":     parsed_targets,
            "wave":        wave,
            "timestamp":   time.time(),
        })
        self._save_shared_log()

        # Determine emotion value generically — use 'emotion' field if present, else empty string.
        emotion_val = meta.get("emotion", "")

        new_edges = []
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
        })

        if self._db is not None and self._sim_id is not None:
            self._db.log_turn(
                self._sim_id, wave, turn,
                agent.name, clean_content,
                meta.get("action_note", ""),
                emit_meta, parsed_targets,
            )

        return {
            "success":       True,
            "agent_key":     agent_key,
            "clean_content": clean_content,
            "action_note":   meta.get("action_note", ""),
            "targets":       parsed_targets,
        }

    def _rollback_incoming(self, agent: Agent, incoming_msgs: list[dict]) -> None:
        """Pop the just-injected incoming messages off the agent's memory tail.

        Used after an LLM failure so a future retry doesn't double-consume the same input.
        """
        for _ in incoming_msgs:
            if agent.memory and agent.memory[-1]["role"] == "user":
                agent.memory.pop()

    def _step_agent(
        self,
        agent_key: str,
        wave: int,
        turn: int,
        incoming: list[dict],
    ) -> dict:
        """단일 에이전트 한 스텝. 결과 dict 반환.

        Coordinator: inject -> (compress?) -> trim -> emit start -> call LLM
                     -> on failure rollback, on success apply result.
        """
        if self._stop_event.is_set():
            return {"success": False, "agent_key": agent_key}

        active_agent = self.agents[agent_key]
        incoming_msgs = self._inject_incoming(active_agent, incoming)
        other_agents  = [k for k in self.active_agents if k != agent_key]
        # 그룹 필터 적용: 이 에이전트가 볼 수 있는 활성 에이전트만 <TARGETS>에 노출
        visible_agents  = [k for k in self._visible_targets.get(agent_key, other_agents)
                           if k in self.active_agents]
        # 그룹 소속이 있으면 섹션 구조로 <TARGETS> 렌더링
        target_sections = self._get_visible_sections(agent_key, visible_agents)

        # Compression runs before trimming so memories are structured, not discarded.
        self._maybe_compress(active_agent, agent_key, wave, visible_agents, target_sections)

        # Trim memory to fit within token limit before building the final prompt.
        active_agent.trim_to_token_limit(
            self.background_log, visible_agents, self._key_to_alias, target_sections
        )
        est_tokens = active_agent.estimate_context_tokens(
            self.background_log, visible_agents, self._key_to_alias, target_sections
        )

        self._emit("turn_start", {
            "turn":        turn,
            "wave":        wave,
            "speaker":     agent_key,
            "memory_size": len(active_agent.memory),
            "est_tokens":  est_tokens,
            "token_limit": active_agent._token_limit,
        })

        content, reasoning, usage, error = self._call_llm_for_agent(
            active_agent, agent_key, visible_agents, target_sections
        )
        if error is not None:
            self._emit("turn_error", {
                "turn":    turn,
                "speaker": agent_key,
                "error":   "empty response" if error == "empty" else error.split(":", 1)[-1],
            })
            self._rollback_incoming(active_agent, incoming_msgs)
            return {"success": False, "agent_key": agent_key}

        return self._apply_turn_result(
            active_agent, agent_key, content, reasoning, usage, wave, turn, est_tokens,
        )

    # ------------------------------------------------------------------
    # 시뮬레이션 실행
    # ------------------------------------------------------------------

    def run(
        self,
        start_agent:  str,
        max_waves:    int        = 10,
        step_delay:   float      = 1.0,
        events:       list       = None,
        resume_wave:  dict | None = None,
    ):
        """Wave-based BFS + 시나리오 이벤트 실행."""
        # wave → event list 인덱스 구성
        events_by_wave: dict[int, list] = {}
        for e in (events or []):
            w = e.get("wave", 0) if isinstance(e, dict) else 0
            events_by_wave.setdefault(w, []).append(e)

        current_wave: dict[str, list] = resume_wave if resume_wave else {start_agent: []}
        turn_counter = 0
        total_turns  = 0

        for wave_num in range(max_waves):
            if self._stop_event.is_set():
                break

            # 이벤트 먼저 실행 (agent_enter가 current_wave에 추가될 수 있음)
            for event in events_by_wave.get(wave_num, []):
                ev_result = self._execute_event(event)
                entrant   = ev_result.get("entrant")
                if entrant and entrant not in current_wave:
                    current_wave[entrant] = []

            if not current_wave:
                break

            self._emit("wave_start", {
                "wave":   wave_num,
                "agents": list(current_wave.keys()),
            })

            results: dict[str, dict] = {}
            with ThreadPoolExecutor(max_workers=len(current_wave)) as executor:
                future_map = {
                    executor.submit(
                        self._step_agent, agent_key, wave_num, turn_counter + i, incoming
                    ): agent_key
                    for i, (agent_key, incoming) in enumerate(current_wave.items())
                }
                for future in as_completed(future_map):
                    agent_key = future_map[future]
                    try:
                        results[agent_key] = future.result()
                    except Exception as e:
                        logger.error(f"Wave {wave_num} agent {agent_key} 예외: {e}")
                        results[agent_key] = {"success": False, "agent_key": agent_key}

            turn_counter += len(current_wave)
            total_turns  += len(current_wave)
            self.completed_waves = wave_num + 1

            next_wave: dict[str, list] = {}
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                for target_key in self._resolve_targets(result["targets"], speaker_key):
                    if target_key not in next_wave:
                        next_wave[target_key] = []
                    next_wave[target_key].append({
                        "speaker":     speaker_key,
                        "content":     result["clean_content"],
                        "action_note": result.get("action_note", ""),
                    })

            current_wave = next_wave

            if current_wave:
                time.sleep(step_delay)

        self._pending_wave = current_wave  # agents targeted but not yet responded
        self._save_edges()

        if self._db is not None and self._sim_id is not None:
            self._db.save_agent_snapshots(
                self._sim_id,
                {key: list(agent.memory) for key, agent in self.agents.items()},
            )

        self._emit("simulation_end", {
            "total_turns": total_turns,
            "edges_count": len(self.edges),
            "log_count":   len(self.shared_log),
        })
