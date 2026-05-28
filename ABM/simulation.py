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

    def _normalize_target(self, t: str) -> str:
        """한국어 display_name → 시스템 key 정규화. 이미 key면 그대로 반환."""
        if t in self.agents:
            return t
        return self._alias_to_key.get(t, t)

    def _resolve_targets(self, targets: list[str], speaker_key: str) -> list[str]:
        """발화 target 해석 — active_agents 기준."""
        resolved = []
        for t in targets:
            if t.lower() == "all":
                resolved.extend(k for k in self.active_agents if k != speaker_key)
            else:
                key = self._normalize_target(t)
                if key in self.active_agents and key != speaker_key:
                    resolved.append(key)
        return list(dict.fromkeys(resolved))

    def _resolve_event_targets(self, targets: list[str]) -> list[str]:
        """이벤트 알림 대상 해석 — active_agents 기준 (speaker 제한 없음)."""
        if any(t.lower() == "all" for t in targets):
            return list(self.active_agents)
        resolved = []
        for t in targets:
            key = self._normalize_target(t)
            if key in self.active_agents:
                resolved.append(key)
        return resolved

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
    # 에이전트 단일 스텝
    # ------------------------------------------------------------------

    def _step_agent(
        self,
        agent_key: str,
        wave: int,
        turn: int,
        incoming: list[dict],
    ) -> dict:
        """단일 에이전트 한 스텝. 결과 dict 반환."""
        if self._stop_event.is_set():
            return {"success": False, "agent_key": agent_key}

        active_agent = self.agents[agent_key]

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
            active_agent.add_to_memory(msg)

        other_agents = [k for k in self.active_agents if k != agent_key]

        # Compression check — runs before trimming so memories are structured, not discarded.
        if (
            self._db is not None
            and self._sim_id is not None
            and len(active_agent.memory) >= _COMPRESSION_MIN_MSGS
        ):
            est = active_agent.estimate_context_tokens(
                self.background_log, other_agents, self._key_to_alias
            )
            if est / active_agent._token_limit >= _COMPRESSION_THRESHOLD:
                self._compress_agent(active_agent, agent_key, turn)

        # Trim memory to fit within token limit before building the final prompt
        active_agent.trim_to_token_limit(self.background_log, other_agents, self._key_to_alias)
        est_tokens    = active_agent.estimate_context_tokens(self.background_log, other_agents, self._key_to_alias)

        self._emit("turn_start", {
            "turn":            turn,
            "wave":            wave,
            "speaker":         agent_key,
            "memory_size":     len(active_agent.memory),
            "est_tokens":      est_tokens,
            "token_limit":     active_agent._token_limit,
        })

        call_messages = active_agent.build_messages(self.background_log, other_agents, self._key_to_alias)

        try:
            content, reasoning, usage = chat_response(
                call_messages, self.model, self.base_url, self.api_timeout
            )
        except Exception as e:
            logger.error(f"응답 생성 실패 ({active_agent.name}): {e}")
            self._emit("turn_error", {"turn": turn, "speaker": agent_key, "error": str(e)})
            for _ in incoming_msgs:
                if active_agent.memory and active_agent.memory[-1]["role"] == "user":
                    active_agent.memory.pop()
            return {"success": False, "agent_key": agent_key}

        if not content:
            logger.warning(f"빈 응답 ({active_agent.name}), turn={turn}")
            self._emit("turn_error", {"turn": turn, "speaker": agent_key, "error": "empty response"})
            for _ in incoming_msgs:
                if active_agent.memory and active_agent.memory[-1]["role"] == "user":
                    active_agent.memory.pop()
            return {"success": False, "agent_key": agent_key}

        # Record actual prompt tokens from server response
        prompt_tokens = usage.get("prompt_tokens", est_tokens)
        active_agent._last_prompt_tokens = prompt_tokens

        clean_content, meta, parsed_targets = parse_json_response(content, active_agent._extra_fields)

        active_agent.add_to_memory({"role": "assistant", "content": content})
        active_agent.add_to_log(
            content=clean_content, reasoning=reasoning,
            extra=meta, targets=parsed_targets,
        )

        self.shared_log.append({
            "speaker":   active_agent.name,
            "content":   clean_content,
            "meta":      dict(meta),
            "targets":   parsed_targets,
            "timestamp": time.time(),
        })
        self._save_shared_log()

        # Determine emotion value generically — use 'emotion' field if present, else empty string.
        emotion_val = meta.get("emotion", "")

        new_edges = []
        for target_name in self._resolve_targets(parsed_targets, agent_key):
            edge = {
                "source":    active_agent.name,
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
            "speaker":           active_agent.name,
            "targets":           parsed_targets,
            "content":           clean_content,
            "action_note":       meta.get("action_note", ""),
            "meta":              emit_meta,
            "memory_size":       len(active_agent.memory),
            "prompt_tokens":     prompt_tokens,
            "token_limit":       active_agent._token_limit,
            "reasoning_preview": reasoning[:120] if reasoning else "",
            "new_edges":         new_edges,
        })

        if self._db is not None and self._sim_id is not None:
            self._db.log_turn(
                self._sim_id, wave, turn,
                active_agent.name, clean_content,
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
