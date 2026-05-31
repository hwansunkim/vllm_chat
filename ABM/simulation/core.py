import json
import os
import queue
import threading
import logging

from ..agent import Agent
from ..config import LOG_DIR, MODEL, BASE_URL, API_TIMEOUT
from .location import _LocationMixin
from .targets import _TargetsMixin
from .events import _EventsMixin
from .turn import _TurnMixin
from .step import _StepMixin
from .system import _SystemMixin
from .runner import _RunnerMixin

logger = logging.getLogger(__name__)


class Simulation(_LocationMixin, _TargetsMixin, _EventsMixin, _TurnMixin, _StepMixin, _SystemMixin, _RunnerMixin):
    def __init__(
        self,
        agents:           dict[str, Agent],
        background_log:   list,
        log_dir:          str                    = LOG_DIR,
        model:            str                    = MODEL,
        base_url:         str                    = BASE_URL,
        api_timeout:      int                    = API_TIMEOUT,
        event_queue:      queue.Queue | None     = None,
        stop_event:       threading.Event | None = None,
        initial_agents:   list[str] | None       = None,
        name_aliases:     dict[str, str] | None  = None,
        sim_id:           str | None             = None,
        db=None,
        agent_groups:     dict[str, list[str]] | None = None,
        summary_interval: int                         = 0,
        system_agent:     dict | None                 = None,
        agent_locations:  dict[str, str] | None       = None,
        agent_visuals:    dict[str, str] | None       = None,
        location_graph:   list[dict] | None           = None,
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

        self._alias_to_key: dict[str, str] = name_aliases or {}
        self._key_to_alias: dict[str, str] = {v: k for k, v in self._alias_to_key.items()}

        self._sim_id = sim_id
        self._db     = db
        self.completed_waves: int = 0
        self._pending_wave: dict  = {}

        if initial_agents is not None:
            self.active_agents: set[str] = set(initial_agents) & set(agents.keys())
        else:
            self.active_agents = set(agents.keys())

        self._agent_groups: dict[str, list[str]] = agent_groups or {}
        self._visible_targets: dict[str, list[str]] = self._build_visible_targets(
            self._agent_groups
        )

        self._summary_interval:    int        = max(0, summary_interval)
        self._last_summarized_wave: int       = -1
        self._last_summary:        dict | None = None

        sa = system_agent or {}
        self._sys_enabled:   bool = bool(sa.get("enabled", False))
        self._sys_prompt:    str  = sa.get("system_prompt", "")
        self._sys_icon:      str  = sa.get("icon", "🎬")
        self._sys_name:      str  = sa.get("display_name", "내레이터")
        self._sys_interval:  int  = max(1, int(sa.get("intervention_interval", 1)))
        self._sys_threshold: int  = max(1, int(sa.get("silence_threshold", 3)))
        self._director_note: str  = sa.get("director_note", "")
        self._director_memo: str  = ""

        self._last_spoke_wave: dict[str, int] = {}

        # 위치 그래프 (인접 리스트)
        self._location_graph: dict[str, list[str]] = {}
        if location_graph:
            for node in location_graph:
                name     = node.get("name", "")
                connects = node.get("connects_to", [])
                if name:
                    self._location_graph[name] = list(connects)

        # 지도를 각 에이전트 시스템 프롬프트에 정적으로 주입 (에이전트가 처음부터 지도 인식)
        if self._location_graph:
            map_lines = ["\n\n[위치 그래프 — 이동 가능한 경로]"]
            for loc, conns in self._location_graph.items():
                conn_str = ", ".join(conns) if conns else "(연결 없음)"
                map_lines.append(f"  {loc}: {conn_str}")
            map_lines.append("※ move_to 필드에는 반드시 위 그래프에 있는 장소명만 사용할 것. 그 외 장소로의 이동은 무시됩니다.")
            map_section = "\n".join(map_lines)
            for agent in self.agents.values():
                agent.system_prompt += map_section

        self._agent_path: dict[str, list[str]] = {}

        self._agent_location:  dict[str, str]  = {}
        self._agent_visual:    dict[str, str]  = {}
        self._agent_knowledge: dict[str, set]  = {}
        self._stranger_map:    dict[str, dict] = {}
        self._stranger_rmap:   dict[str, dict] = {}

        _groups = self._agent_groups
        for key in self.agents:
            if agent_locations and key in agent_locations:
                self._agent_location[key] = agent_locations[key]
            else:
                first_group = (_groups.get(key) or [None])[0]
                self._agent_location[key] = first_group if first_group else ""

        for key in self.agents:
            self._agent_visual[key] = (agent_visuals or {}).get(key, "")

        all_keys = list(self.agents.keys())
        for key in all_keys:
            self._agent_knowledge[key] = set()
            self._stranger_map[key]    = {}
            self._stranger_rmap[key]   = {}
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
                for other_key in all_keys:
                    if other_key != key:
                        self._agent_knowledge[key].add(other_key)

        os.makedirs(log_dir, exist_ok=True)
        self._save_shared_log()

    # ── I/O ──────────────────────────────────────────────────────────────────

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
