import json
import re
import time
from difflib import SequenceMatcher
import os
import queue
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from .agent import Agent
from .llm import chat_response
from .parser import parse_json_response, parse_json_extras
from .config import LOG_DIR, MODEL, BASE_URL, API_TIMEOUT

logger = logging.getLogger(__name__)

# CJK Unified / Extension-A / Compatibility Ideographs
_CJK_RE = re.compile(r'[一-鿿㐀-䶿豈-﫿]')

def _has_foreign_chars(text: str) -> bool:
    return bool(_CJK_RE.search(text or ''))


_REPEAT_WINDOW    = 4     # 최근 N발언 비교
_REPEAT_THRESHOLD = 0.65  # 유사도 이 이상이면 반복으로 판단
_MEMO_MAX_LINES   = 12    # director_memo 최대 보존 줄 수

def _repetition_score(texts: list[str]) -> float:
    """최근 발언 목록에서 최대 쌍별 유사도(0–1) 반환."""
    if len(texts) < 2:
        return 0.0
    best = 0.0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            s = SequenceMatcher(None, texts[i], texts[j]).ratio()
            if s > best:
                best = s
    return best


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
        agent_groups:     dict[str, list[str]] | None = None,  # key → group IDs
        summary_interval: int                         = 0,     # 0 = 비활성, N = N웨이브마다 요약
        system_agent:     dict | None                 = None,  # system 에이전트 설정
        agent_locations:  dict[str, str] | None       = None,  # key → 초기 위치
        agent_visuals:    dict[str, str] | None       = None,  # key → 초기 외모
        location_graph:   list[dict] | None           = None,  # [{name: str, connects_to: [str]}]
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

        # 웨이브 요약 설정
        self._summary_interval: int = max(0, summary_interval)
        self._last_summarized_wave: int = -1  # 마지막으로 요약된 웨이브 번호
        self._last_summary: dict | None = None  # 가장 최근 요약 결과

        # system 에이전트 설정
        sa = system_agent or {}
        self._sys_enabled:   bool  = bool(sa.get("enabled", False))
        self._sys_prompt:    str   = sa.get("system_prompt", "")
        self._sys_icon:      str   = sa.get("icon", "🎬")
        self._sys_name:      str   = sa.get("display_name", "내레이터")
        self._sys_interval:  int   = max(1, int(sa.get("intervention_interval", 1)))
        self._sys_threshold: int   = max(1, int(sa.get("silence_threshold", 3)))
        self._director_note: str   = sa.get("director_note", "")
        self._director_memo: str   = ""  # system 에이전트가 매 호출마다 누적 갱신

        # 에이전트별 마지막 발화 웨이브 추적
        self._last_spoke_wave: dict[str, int] = {}

        # 위치 그래프 (인접 리스트)
        self._location_graph: dict[str, list[str]] = {}
        if location_graph:
            for node in location_graph:
                name = node.get("name", "")
                connects = node.get("connects_to", [])
                if name:
                    self._location_graph[name] = list(connects)

        # 에이전트 이동 경로 추적 (key → 남은 경로 리스트)
        self._agent_path: dict[str, list[str]] = {}

        # 위치 기반 시스템 상태
        self._agent_location: dict[str, str] = {}   # 현재 위치 (동적)
        self._agent_visual:   dict[str, str] = {}   # 현재 외모 (동적)
        self._agent_knowledge: dict[str, set] = {}  # 아는 사람 set (knowledge 확장)
        self._stranger_map:  dict[str, dict] = {}   # observer → {stranger_id → real_key}
        self._stranger_rmap: dict[str, dict] = {}   # observer → {real_key → stranger_id}

        # 위치 초기화: agent_locations 파라미터 값, 없으면 첫 번째 그룹, 그것도 없으면 ""
        _groups = self._agent_groups
        for key in self.agents:
            if agent_locations and key in agent_locations:
                self._agent_location[key] = agent_locations[key]
            else:
                first_group = (_groups.get(key) or [None])[0]
                self._agent_location[key] = first_group if first_group else ""

        # 외모 초기화
        for key in self.agents:
            if agent_visuals and key in agent_visuals:
                self._agent_visual[key] = agent_visuals[key]
            else:
                self._agent_visual[key] = ""

        # knowledge 초기화: 같은 그룹 에이전트끼리 서로 앎
        all_keys = list(self.agents.keys())
        for key in all_keys:
            self._agent_knowledge[key] = set()
            self._stranger_map[key] = {}
            self._stranger_rmap[key] = {}
        for key in all_keys:
            my_groups = _groups.get(key, [])
            if my_groups:
                for other_key in all_keys:
                    if other_key == key:
                        continue
                    other_groups = _groups.get(other_key, [])
                    if any(g in other_groups for g in my_groups):
                        self._agent_knowledge[key].add(other_key)
            else:
                # 그룹 미설정 에이전트는 모든 에이전트를 앎
                for other_key in all_keys:
                    if other_key != key:
                        self._agent_knowledge[key].add(other_key)

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

    def _find_path(self, start: str, goal: str) -> list[str]:
        """BFS 최단 경로. 시작 제외, 목표 포함. 그래프 없으면 [goal] 반환."""
        from collections import deque
        if start == goal:
            return []
        if not self._location_graph or start not in self._location_graph or goal not in self._location_graph:
            return [goal]  # 그래프 없음 또는 미등록 위치 → 직접 이동 (하위 호환)
        visited = {start}
        q = deque([(start, [])])
        while q:
            node, path = q.popleft()
            for nb in self._location_graph.get(node, []):
                new_path = path + [nb]
                if nb == goal:
                    return new_path
                if nb not in visited:
                    visited.add(nb)
                    q.append((nb, new_path))
        return [goal]  # 연결 경로 없음 → 직접 이동

    def _get_adjacent(self, location: str) -> list[str]:
        """현재 위치에서 이동 가능한 인접 장소 목록."""
        return list(self._location_graph.get(location, []))

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

    def _build_situation_context(
        self,
        agent_key: str,
        known: list[str],
        strangers: list[tuple],
    ) -> str | None:
        """현재 위치·이동 가능 장소·동석자 정보를 내러티브 user 메시지로 구성."""
        my_loc = self._agent_location.get(agent_key, "")
        if not my_loc:
            return None

        lines = ["[현재 상황]", f"현재 위치: {my_loc}"]

        # 전체 지도 (그래프 설정된 경우)
        if self._location_graph:
            lines.append("")
            lines.append("[지도]")
            for loc_name, connections in self._location_graph.items():
                conn_str = ", ".join(connections) if connections else "(연결 없음)"
                lines.append(f"  {loc_name}: {conn_str}")

        # 이동 가능한 장소 (그래프 설정된 경우)
        adjacent = self._get_adjacent(my_loc)
        if adjacent:
            lines.append(f"이동 가능한 장소: {', '.join(adjacent)}")

        # 이동 중인 경우
        path = self._agent_path.get(agent_key, [])
        if path:
            dest = path[-1]
            steps = len(path)
            if steps == 1:
                lines.append(f"이동 중: {dest}까지 1칸 남음")
            else:
                lines.append(f"이동 중: {dest} 방향 ({steps}칸 남음, 다음: {path[0]})")

        if known or strangers:
            lines.append("")
            lines.append("[이 자리의 사람들]")
            if known:
                labels = [f'{self._key_to_alias.get(k, k)} (ID: "{k}")' for k in known]
                lines.append(f"아는 사람: {', '.join(labels)}")
            if strangers:
                lines.append("처음 보는 사람:")
                for sid, _, visual in strangers:
                    lines.append(f'  - ID: "{sid}"  {visual}' if visual else f'  - ID: "{sid}"')
        else:
            lines.append("이 자리에는 아무도 없다.")

        return "\n".join(lines)

    def _get_or_assign_stranger_id(self, observer_key: str, target_key: str) -> str:
        """observer가 target을 처음 보는 경우 stranger_N ID를 할당, 이미 있으면 기존 ID 반환."""
        self._stranger_map.setdefault(observer_key, {})
        self._stranger_rmap.setdefault(observer_key, {})
        if target_key in self._stranger_rmap[observer_key]:
            return self._stranger_rmap[observer_key][target_key]
        n = len(self._stranger_map[observer_key]) + 1
        sid = f"stranger_{n}"
        self._stranger_map[observer_key][sid] = target_key
        self._stranger_rmap[observer_key][target_key] = sid
        return sid

    def _compute_wave_targets(self, agent_key: str):
        """현재 위치 기준 대화 가능 에이전트 계산.
        Returns (known: list[str], strangers: list[tuple[stranger_id, real_key, visual]])
        위치가 설정된 경우에만 필터링, 미설정 시 기존 동작.
        """
        my_loc = self._agent_location.get(agent_key, "")
        knowledge = self._agent_knowledge.get(agent_key, set())
        known: list[str] = []
        strangers: list[tuple[str, str, str]] = []
        for other_key in self.active_agents:
            if other_key == agent_key:
                continue
            other_loc = self._agent_location.get(other_key, "")
            # 위치가 설정된 경우에만 필터링 (둘 다 위치가 있고 다른 곳이면 제외)
            if my_loc and other_loc and my_loc != other_loc:
                continue
            if other_key in knowledge:
                known.append(other_key)
            else:
                sid = self._get_or_assign_stranger_id(agent_key, other_key)
                visual = self._agent_visual.get(other_key, "") or self._key_to_alias.get(other_key, other_key)
                strangers.append((sid, other_key, visual))
        return known, strangers

    def _resolve_targets(self, targets: list[str], speaker_key: str) -> list[str]:
        """발화 target 해석 — active_agents 기준.

        지원 형식:
          "all"        → 화자 그룹 내 활성 에이전트 (그룹 미소속 시 전체)
          "group:X"    → 그룹 X 소속 활성 에이전트 중 화자가 볼 수 있는 멤버만
          "stranger_N" → 해당 낯선 이 (real_key 로 변환 + knowledge 확장)
          "<key>"      → 특정 에이전트
        브릿지 에이전트(복수 그룹)는 "all" 시 모든 가시 에이전트, "group:X" 시 해당 그룹만.
        """
        resolved = []
        visible_set = set(self._visible_targets.get(speaker_key, []))
        for t in targets:
            t_s = t.strip()
            if t_s.lower() in ("self", "system"):  # 혼잣말 — 전달 대상 없음
                continue
            elif t_s.lower() == "all":
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
            elif t_s.startswith("stranger_"):
                # stranger_N → real_key 변환 + 상호 knowledge 확장
                real_key = self._stranger_map.get(speaker_key, {}).get(t_s)
                if real_key and real_key in self.active_agents:
                    self._agent_knowledge.setdefault(speaker_key, set()).add(real_key)
                    self._agent_knowledge.setdefault(real_key, set()).add(speaker_key)
                    resolved.append(real_key)
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

        elif etype == "update_appearance":
            if not agent_key or agent_key not in self.agents:
                logger.warning(f"update_appearance: 알 수 없는 에이전트 '{agent_key}'")
                return result
            self._agent_visual[agent_key] = message
            display = self._key_to_alias.get(agent_key, agent_key)
            self._emit("appearance_update", {
                "wave": 0, "agent": agent_key,
                "display_name": display,
                "description": message,
            })
            # 같은 위치 에이전트들에게 알림 주입
            my_loc = self._agent_location.get(agent_key, "")
            for name in self._resolve_event_targets(targets):
                if name == agent_key:
                    continue
                other_loc = self._agent_location.get(name, "")
                if my_loc and other_loc and my_loc != other_loc:
                    continue
                self.agents[name].add_to_memory({
                    "role": "user",
                    "content": f"[씬] {display}의 외모가 변했다: {message}",
                })
            logger.info(f"[외모 변경] {agent_key}: {message}")

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
        situation_targets: bool = False,
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
            situation_targets=situation_targets,
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
        return self._call_llm_for_agent_msgs(agent, agent_key, call_messages)

    def _call_llm_for_agent_msgs(
        self,
        agent: Agent,
        agent_key: str,
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
        agent: Agent,
        agent_key: str,
        visible_agents: list[str],
        target_sections,
        bad_content: str,
        max_retries: int = 2,
        key_to_alias: dict | None = None,
        location_name: str = "",
        situation_targets: bool = False,
    ) -> tuple[str | None, str, dict, str | None]:
        """한자 등 외국어가 섞인 응답을 최대 max_retries회 재시도로 교정."""
        CORRECTION_MSG = (
            "⚠ 방금 응답에 한국어가 아닌 문자(한자, 영어 등)가 포함되어 있습니다. "
            "content 필드를 반드시 한국어로만 다시 작성해주세요."
        )
        alias = key_to_alias if key_to_alias is not None else self._key_to_alias
        current_bad = bad_content
        reasoning, usage = "", {}
        for attempt in range(1, max_retries + 1):
            fix_msgs = agent.build_messages(
                self.background_log, visible_agents, alias, target_sections, location_name,
                situation_targets,
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

        # 발화 기록 업데이트 (self/독백 포함 — 에이전트가 활동했으므로)
        self._last_spoke_wave[agent.name] = wave

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

        # 위치 기반 대화 가능 에이전트 계산
        known, strangers = self._compute_wave_targets(agent_key)

        # 현재 위치·동석자 상황을 user 메시지로 주입 (위치 설정된 에이전트만)
        situation_text = self._build_situation_context(agent_key, known, strangers)
        situation_msgs: list[dict] = []
        if situation_text:
            sit_msg = {"role": "user", "content": situation_text}
            active_agent.add_to_memory(sit_msg)
            situation_msgs = [sit_msg]

        # extended alias: stranger_id → visual_description (표시용)
        extended_alias = dict(self._key_to_alias)
        for sid, _, visual in strangers:
            extended_alias[sid] = visual

        # visible agents = known keys + stranger IDs
        visible_agents = known + [sid for sid, _, _ in strangers]

        # target_sections 구성
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
            # 위치 기반 필터링 없이 기존 그룹 기반 섹션으로 폴백
            other_agents = [k for k in self.active_agents if k != agent_key]
            visible_agents = [k for k in self._visible_targets.get(agent_key, other_agents)
                              if k in self.active_agents]
            target_sections = self._get_visible_sections(agent_key, visible_agents)

        # 위치 설정된 에이전트는 상황 컨텍스트에서 대화 상대 ID를 제공 → <TARGETS> 블록 불필요
        sit_targets = bool(my_loc)

        # Compression runs before trimming so memories are structured, not discarded.
        self._maybe_compress(active_agent, agent_key, wave, visible_agents, target_sections, sit_targets)

        # Trim memory to fit within token limit before building the final prompt.
        active_agent.trim_to_token_limit(
            self.background_log, visible_agents, extended_alias, target_sections, my_loc, sit_targets
        )
        est_tokens = active_agent.estimate_context_tokens(
            self.background_log, visible_agents, extended_alias, target_sections, my_loc, sit_targets
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
            self.background_log, visible_agents, extended_alias, target_sections, my_loc, sit_targets
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
            self._rollback_incoming(active_agent, situation_msgs + incoming_msgs)
            return {"success": False, "agent_key": agent_key}

        # 외국어(한자 등) 감지 시 자동 재시도
        if content and _has_foreign_chars(content):
            logger.warning(f"언어 교잡 감지 ({agent_key}): 재시도 시작")
            self._emit("turn_language_fix", {"speaker": agent_key, "wave": wave, "turn": turn})
            content, reasoning, usage, error = self._retry_language_fix(
                active_agent, agent_key, visible_agents, target_sections, content,
                key_to_alias=extended_alias, location_name=my_loc, situation_targets=sit_targets,
            )
            if error is not None:
                self._emit("turn_error", {
                    "turn":    turn,
                    "speaker": agent_key,
                    "error":   error.split(":", 1)[-1],
                })
                self._rollback_incoming(active_agent, situation_msgs + incoming_msgs)
                return {"success": False, "agent_key": agent_key}

        # move_to, update_appearance 추출
        extras = parse_json_extras(content)

        result = self._apply_turn_result(
            active_agent, agent_key, content, reasoning, usage, wave, turn, est_tokens,
        )
        result["move_to"] = extras.get("move_to")
        result["update_appearance"] = extras.get("update_appearance")
        return result

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
                    # 에이전트 완료마다 stop 확인 — 나머지 future는 LLM 콜 중이므로
                    # cancel()은 효과 없지만 더 이상 결과를 기다리지 않고 빠르게 루프를 탈출
                    if self._stop_event.is_set():
                        break

            if self._stop_event.is_set():
                break

            turn_counter += len(current_wave)
            total_turns  += len(current_wave)
            self.completed_waves = wave_num + 1

            # ── 이동·외모 처리 (next_wave 구성 전에 상태 갱신) ──────────────────────
            scene_injections: dict[str, list] = {}

            # 1단계: 이번 웨이브에 선언된 move_to로 경로 계산
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                move_to_raw = result.get("move_to")
                if move_to_raw and isinstance(move_to_raw, str):
                    dest = move_to_raw.strip()
                    if dest:
                        current_loc = self._agent_location.get(speaker_key, "")
                        if dest != current_loc:
                            self._agent_path[speaker_key] = self._find_path(current_loc, dest)

            # 2단계: 모든 활성 에이전트의 이동 경로를 1칸씩 진행
            for agent_key in list(self.active_agents):
                path = self._agent_path.get(agent_key)
                if not path:
                    continue
                next_loc = path.pop(0)
                if not path:
                    self._agent_path.pop(agent_key, None)
                old_loc = self._agent_location.get(agent_key, "")
                if old_loc == next_loc:
                    continue
                self._agent_location[agent_key] = next_loc
                display = self._key_to_alias.get(agent_key, agent_key)
                self._emit("agent_move", {
                    "wave": wave_num, "agent": agent_key,
                    "display_name": display,
                    "from": old_loc, "to": next_loc,
                })
                mover_visual = self._agent_visual.get(agent_key, "") or display
                for other_key in self.active_agents:
                    if other_key == agent_key:
                        continue
                    if self._agent_location.get(other_key, "") == next_loc:
                        if agent_key in self._agent_knowledge.get(other_key, set()):
                            scene_msg = f"[씬] {display}이(가) 이곳에 도착했다."
                        else:
                            scene_msg = (
                                f"[씬] 낯선 이가 나타났다: {mover_visual}"
                                if mover_visual else "[씬] 낯선 이가 나타났다."
                            )
                        scene_injections.setdefault(other_key, []).append({
                            "speaker": "씬", "content": scene_msg, "action_note": ""
                        })

            # 3단계: 외모 변경 처리
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                update_appearance = result.get("update_appearance")
                if update_appearance:
                    self._agent_visual[speaker_key] = update_appearance
                    display = self._key_to_alias.get(speaker_key, speaker_key)
                    self._emit("appearance_update", {
                        "wave": wave_num, "agent": speaker_key,
                        "display_name": display,
                        "description": update_appearance,
                    })
                    my_loc = self._agent_location.get(speaker_key, "")
                    for other_key in self.active_agents:
                        if other_key == speaker_key:
                            continue
                        other_loc = self._agent_location.get(other_key, "")
                        if my_loc and other_loc and my_loc != other_loc:
                            continue
                        scene_msg = f"[씬] {display}의 외모가 변했다: {update_appearance}"
                        scene_injections.setdefault(other_key, []).append({
                            "speaker": "씬", "content": scene_msg, "action_note": ""
                        })

            next_wave: dict[str, list] = {}
            # 씬 알림을 next_wave에 먼저 추가
            for agent_key, msgs in scene_injections.items():
                if agent_key in self.active_agents:
                    next_wave.setdefault(agent_key, []).extend(msgs)

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

            # 웨이브 요약 트리거 — stop 요청 시 스킵
            if self._summary_interval > 0 and not self._stop_event.is_set():
                waves_since = wave_num - self._last_summarized_wave
                if waves_since >= self._summary_interval:
                    self._run_wave_summary(self._last_summarized_wave + 1, wave_num)
                    self._last_summarized_wave = wave_num

            # system 에이전트 — 개입 여부 판단 및 다음 웨이브에 메시지 주입
            if self._sys_enabled and not self._stop_event.is_set():
                if (wave_num + 1) % self._sys_interval == 0:
                    current_wave = self._run_system_agent(wave_num, current_wave)

            # step_delay를 짧은 슬립 단위로 분할해 stop 응답성 확보
            if current_wave and not self._stop_event.is_set():
                elapsed = 0.0
                interval = 0.1
                while elapsed < step_delay and not self._stop_event.is_set():
                    time.sleep(interval)
                    elapsed += interval

        self._pending_wave = current_wave  # agents targeted but not yet responded
        self._save_edges()

        self._emit("simulation_end", {
            "total_turns": total_turns,
            "edges_count": len(self.edges),
            "log_count":   len(self.shared_log),
        })

    def _run_wave_summary(self, wave_start: int, wave_end: int) -> None:
        """shared_log에서 해당 웨이브 구간 엔트리를 추출해 LLM 요약 후 이벤트를 emit."""
        if self._stop_event.is_set():
            return
        from .summarizer import summarize_waves
        entries = [
            e for e in self.shared_log
            if isinstance(e.get("wave"), int)
            and wave_start <= e["wave"] <= wave_end
        ]
        bg_text = ""
        if self.background_log:
            first = self.background_log[0].get("content", "")
            bg_text = first.removeprefix("[배경]").strip()

        result = summarize_waves(
            entries      = entries,
            background   = bg_text,
            wave_start   = wave_start,
            wave_end     = wave_end,
            model        = self.model,
            base_url     = self.base_url,
            api_timeout  = self.api_timeout,
            key_to_alias = self._key_to_alias,
        )
        if result:
            self._last_summary = result  # system 에이전트에 전달
            self._emit("wave_summary", {
                "wave_start":  wave_start,
                "wave_end":    wave_end,
                "summary":     result.get("summary", ""),
                "key_events":  result.get("key_events", []),
                "mood":        result.get("mood", ""),
            })

    def _run_system_agent(self, wave_num: int, current_wave: dict) -> dict:
        """system 에이전트를 실행해 개입/월드이벤트를 주입. 수정된 current_wave 반환."""
        from .system_agent import run_system_agent

        silent = [
            key for key in self.active_agents
            if wave_num - self._last_spoke_wave.get(key, -1) >= self._sys_threshold
        ]

        # 반복 감지: 에이전트별 최근 WINDOW개 발언 유사도 계산
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

        # ── director_memo 누적 갱신 ──────────────────────────────────────────
        new_memo = (result.get("director_memo") or "").strip()
        if new_memo:
            entry = f"Wave {wave_num}: {new_memo}"
            lines = [l for l in self._director_memo.splitlines() if l.strip()]
            if len(lines) >= _MEMO_MAX_LINES:
                lines = lines[-(  _MEMO_MAX_LINES - 1):]
            self._director_memo = "\n".join(lines + [entry])

        # ── interventions: 특정 에이전트에 1:1 주입 ──────────────────────────
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

        # ── world_event: 타겟 그룹/에이전트에 브로드캐스트 ──────────────────
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
