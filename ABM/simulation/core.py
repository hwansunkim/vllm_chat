import json
import os
import queue
import threading
import logging

from ..agent import Agent
from ..config import LOG_DIR
from ..llm import LLMCall
from .location import _LocationMixin
from .infection import _InfectionMixin
from .targets import _TargetsMixin
from .events import _EventsMixin
from .turn import _TurnMixin
from .step import _StepMixin
from .system import _SystemMixin
from .runner import _RunnerMixin

logger = logging.getLogger(__name__)

_PERSIST_EVENTS: frozenset[str] = frozenset({
    "agent_move",
    "appearance_update",
    "system_intervention",
    "world_event",
    "scene_event",
    "wave_summary",
    "infection_update",
})

# 시작 요일 키(프론트/스키마와 동일) → 표시 라벨. 인덱스 = 월요일 기준 0~6.
_WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_LABELS: tuple[str, ...] = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")

_DEFAULT_TIME_CATEGORIES: list[dict] = [
    {"id": "meal_or_brief",     "label": "식사·짧은 용무 등 스킵되듯 지나가는 장면", "min_minutes": 5,   "max_minutes": 10},
    {"id": "normal_scene",      "label": "그 외 일반적인 대화/활동이 이어지는 장면",   "min_minutes": 15,  "max_minutes": 30},
    {"id": "alone_or_offscreen", "label": "혼자 있거나 외부에 나가 직접 대화가 없는 상태", "min_minutes": 60,  "max_minutes": 120},
    {"id": "night_sleep",       "label": "취침 등 야간 장시간 경과",                   "min_minutes": 240, "max_minutes": 420},
]


class Simulation(_LocationMixin, _InfectionMixin, _TargetsMixin, _EventsMixin, _TurnMixin, _StepMixin, _SystemMixin, _RunnerMixin):
    def __init__(
        self,
        agents:           dict[str, Agent],
        background_log:   list,
        log_dir:          str                    = LOG_DIR,
        *,
        llm:              LLMCall | None         = None,
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
        agent_llm:        dict[str, LLMCall] | None   = None,
        location_graph:   list[dict] | None           = None,
        lang_fix_enabled: bool                        = True,
        lang_fix_retries: int                         = 2,
        llm_max_tokens:   int                         = 16384,
        sim_start_time:   str                         = "09:00",
        sim_start_weekday: str                        = "mon",
        time_per_wave:    int                         = 30,
        time_mode:        str                         = "fixed",
        time_categories:  list[dict] | None            = None,
        idle_minutes_schedule: list[int] | None        = None,
        elapsed_minutes_init: int                      = 0,
        infection_model:  dict | None                  = None,
    ):
        self.agents         = agents
        self.background_log = background_log
        self.log_dir        = log_dir
        self._llm           = llm
        self._agent_llm: dict[str, LLMCall] = agent_llm or {}
        self.llm_max_tokens = llm_max_tokens
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

        # 언어 교잡 수정 설정
        self._lang_fix_enabled: bool = lang_fix_enabled
        self._lang_fix_retries: int  = max(1, int(lang_fix_retries))

        # 시간 개념 설정
        try:
            h, m = sim_start_time.split(":")
            self._sim_start_minutes: int = int(h) * 60 + int(m)
        except Exception:
            self._sim_start_minutes = 9 * 60  # fallback: 09:00
        # 시작 요일. 알 수 없는 값/누락(구버전 시나리오)이면 월요일로 폴백.
        try:
            self._sim_start_weekday_idx: int = _WEEKDAY_KEYS.index(str(sim_start_weekday).lower())
        except ValueError:
            self._sim_start_weekday_idx = 0
        self._time_per_wave: int = max(0, int(time_per_wave))

        # 가변 시간 모드 설정
        self._time_mode: str = time_mode if time_mode in ("fixed", "variable") else "fixed"
        self._time_categories: list[dict] = time_categories if time_categories is not None else list(_DEFAULT_TIME_CATEGORIES)
        self._idle_minutes_schedule: list[int] = idle_minutes_schedule if idle_minutes_schedule is not None else [60, 120, 180]
        self._elapsed_minutes: int = elapsed_minutes_init

        # 위치 그래프 (인접 리스트) + 외부 공간 집합 + 인지 구역(zone) 맵
        # 주의: 여기서의 zone은 **위치 기반 인지 범위**이며, _agent_groups(캐릭터 관계
        # 그룹)와는 완전히 별개의 개념이다. 같은 zone의 다른 장소에 있는 사람은 서로
        # 존재를 인지하지만, 대화는 여전히 같은 장소(노드)에 있어야만 가능하다.
        self._location_graph:    dict[str, list[str]] = {}
        self._exterior_locations: set[str]            = set()
        self._location_zone:     dict[str, str]       = {}
        if location_graph:
            for node in location_graph:
                name     = node.get("name", "")
                connects = node.get("connects_to", [])
                if name:
                    self._location_graph[name] = list(connects)
                    if node.get("is_exterior", False):
                        self._exterior_locations.add(name)
                    zone = (node.get("zone") or "").strip()
                    if zone:
                        self._location_zone[name] = zone

        # 지도를 각 에이전트 시스템 프롬프트에 정적으로 주입 (에이전트가 처음부터 지도 인식)
        if self._location_graph:
            map_lines = ["\n\n[위치 그래프 — 이동 가능한 경로]"]
            for loc, conns in self._location_graph.items():
                conn_str = ", ".join(conns) if conns else "(연결 없음)"
                exterior_mark = " [외부 공간]" if loc in self._exterior_locations else ""
                zone_name  = self._location_zone.get(loc, "")
                zone_mark  = f" [구역: {zone_name}]" if zone_name else ""
                map_lines.append(f"  {loc}{exterior_mark}{zone_mark}: {conn_str}")
            map_lines.append("※ move_to 필드에는 반드시 위 그래프에 있는 장소명만 사용할 것. 그 외 장소로의 이동은 무시됩니다.")
            if self._exterior_locations:
                map_lines.append("※ [외부 공간]으로 표시된 장소는 시뮬레이션 경계 밖입니다. 그곳에서는 다른 누구도 볼 수 없고, 누구도 당신을 볼 수 없습니다.")
            if self._location_zone:
                map_lines.append("※ [구역: ...]은 같은 생활권을 뜻합니다. 같은 구역 안의 다른 장소에 있는 사람은 서로 존재를 인지하지만, 대화는 같은 장소에 있어야만 할 수 있습니다. 말을 걸고 싶다면 move_to로 그 장소까지 이동하세요.")
            map_section = "\n".join(map_lines)
            for agent in self.agents.values():
                agent.system_prompt += map_section

        # 시간 인식 안내를 에이전트 시스템 프롬프트에 정적 주입
        if self._time_mode == "variable" or self._time_per_wave > 0:
            time_section = (
                "\n\n[시간 인식]\n"
                "매 대화 맥락에 [현재 시각: 요일 + 오전/오후 시각] 정보가 제공됩니다. "
                "이를 자연스럽게 인지하고 시간대에 맞는 행동을 하세요. "
                "예) 점심 시간엔 식사를 제안하거나, 퇴근 시간이 다가오면 마무리 행동을 취하는 등.\n"
                "요일도 함께 고려하세요. 평일(월~금)과 주말(토·일)의 일상은 다릅니다. "
                "예) 평일 아침엔 출근·등교를 준비하고, 주말엔 늦잠을 자거나 여가·약속 위주로 움직이는 등. "
                "자정을 넘기면 요일이 자동으로 다음 날로 바뀝니다."
            )
            for agent in self.agents.values():
                agent.system_prompt += time_section

        # 감염병 모델 설정. enabled=False(기본)면 _agent_infection은 전원 "S"로
        # 초기화만 되고 상태 전이도, 프롬프트 주입도 일어나지 않는다(완전한 하위 호환).
        im = infection_model or {}
        self._infection_enabled:      bool  = bool(im.get("enabled", False))
        self._infection_disease_name: str   = im.get("disease_name", "") or ""
        # 전염만 wave/접촉 기준 확률로 남는다. 증상 진행과 회복은 시뮬레이션 내
        # 경과 시간(분) 기준이다 — 같은 wave라도 야간 취침처럼 오래 경과한 wave와
        # 5분짜리 wave가 병의 진행에 다르게 기여해야 하기 때문.
        self._infection_transmission: float = float(im.get("transmission_probability", 0.3) or 0.0)
        self._infection_immune:       bool  = bool(im.get("immune_after_recovery", True))
        # 회복까지 걸리는 시간(분) 구간. 감염 시점에 [min, max]에서 균등 샘플한다.
        # max <= 0 이면 자연 회복이 없다(만성) — 구 recovery_probability=0에 대응.
        self._infection_recovery_min: int = max(0, int(im.get("recovery_min_minutes", 7200) or 0))
        self._infection_recovery_max: int = max(0, int(im.get("recovery_max_minutes", 14400) or 0))
        if 0 < self._infection_recovery_max < self._infection_recovery_min:
            self._infection_recovery_min = self._infection_recovery_max
        self._infection_stages:       list[dict] = []
        for s in (im.get("symptom_stages") or []):
            lo = max(0, int(s.get("min_minutes", 0) or 0))
            hi = max(0, int(s.get("max_minutes", 0) or 0))
            if lo > hi:
                lo = hi  # 스키마 validator와 같은 규칙 — min을 max로 낮춘다
            self._infection_stages.append({
                "id":           s.get("id", ""),
                "label":        s.get("label", ""),
                "min_minutes":  lo,
                "max_minutes":  hi,
                "symptom_text": s.get("symptom_text", ""),
            })

        self._agent_path: dict[str, list[str]] = {}

        # {agent_key: {"status": "S"|"I"|"R",
        #              "infected_at_minutes": int|None,   # 감염 시점의 경과분 앵커
        #              "recover_at_minutes":  int|None,   # 감염 후 회복까지의 목표 경과분(델타)
        #              "recovered_wave": int|None, "recovered_at_minutes": int|None,
        #              "notify_recovery": bool}}
        self._agent_infection: dict[str, dict] = {}

        self._agent_location:  dict[str, str]  = {}
        self._agent_visual:    dict[str, str]  = {}
        self._agent_knowledge: dict[str, set]  = {}
        self._stranger_map:    dict[str, dict] = {}
        self._stranger_rmap:   dict[str, dict] = {}
        # stranger_N 할당은 워커 스레드(_step_agent)에서 read-modify-write 되므로
        # 직렬화한다. 락 없이 두면 같은 wave에 두 관찰자가 서로를 처음 볼 때
        # 번호가 중복되거나 건너뛸 수 있다.
        self._stranger_lock = threading.Lock()

        _groups = self._agent_groups
        for key in self.agents:
            if agent_locations and key in agent_locations:
                self._agent_location[key] = agent_locations[key]
            else:
                first_group = (_groups.get(key) or [None])[0]
                self._agent_location[key] = first_group if first_group else ""

        for key in self.agents:
            self._agent_visual[key] = (agent_visuals or {}).get(key, "")
            self._agent_infection[key] = {
                "status":               "S",
                "infected_at_minutes":  None,
                "recover_at_minutes":   None,
                "recovered_wave":       None,
                "recovered_at_minutes": None,
                "notify_recovery":      False,
            }

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

    # ── LLM ──────────────────────────────────────────────────────────────────

    def _llm_for(self, agent_key: str) -> LLMCall:
        """에이전트 턴용 LLM 콜러블.

        해당 에이전트에 서버 오버라이드가 설정돼 있으면 그것을, 없으면 시뮬레이션
        기본 콜러블(self._llm)을 반환한다. 시뮬레이션 레벨 호출(시간 분류, 웨이브
        요약, system agent)은 특정 에이전트의 턴이 아니므로 이 헬퍼를 쓰지 않고
        항상 self._llm 을 직접 사용한다.
        """
        return self._agent_llm.get(agent_key) or self._llm

    # ── 시뮬레이션 내 경과 시간 ────────────────────────────────────────────────

    def _current_elapsed_minutes(self, wave: int | None = None) -> int:
        """이번 run 기준 '지금'의 시뮬레이션 내 경과 분.

        에이전트에게 보여지는 시각 계산(`_assemble_agent_prompt`)과 목표 기간 판정
        (`run()`)이 쓰는 기준과 **정확히 같은 값**이어야 한다 — 감염 진행이 프롬프트
        속 시계와 어긋나면 "밤새 앓았는데 증상은 그대로"류의 모순이 생긴다.

        - variable 모드: LLM 분류로 누적된 `self._elapsed_minutes`
        - fixed 모드(time_per_wave > 0): `wave * time_per_wave` (결정론적)
        - 시간 개념 비활성(fixed + time_per_wave == 0): 항상 0
          → 감염자는 첫 증상 단계에 머물고 자연 회복도 일어나지 않는다. 시간 기준
            모델에서 이는 버그가 아니라 "시간이 흐르지 않는 세계"의 정의다.
        """
        if self._time_mode == "variable":
            return self._elapsed_minutes
        if self._time_per_wave > 0:
            wave_idx = self.completed_waves if wave is None else int(wave)
            return max(0, wave_idx) * self._time_per_wave
        return 0

    # ── 런타임 상태 스냅샷 (재개용) ────────────────────────────────────────────

    def export_agent_state(self) -> dict[str, dict]:
        """재개 시 복원해야 하는 에이전트별 런타임 상태를 직렬화 가능한 형태로 반환.

        시나리오 config로부터 재구성할 수 없는 값들만 담는다 — 이동으로 바뀐 위치,
        update_appearance로 바뀐 외모, 누가 누구를 아는지(인지관계)와 낯선 이 ID 할당,
        그리고 감염 모델이 계산해 온 SIR 상태. _stranger_rmap은 _stranger_map의
        역함수라 저장하지 않고 복원 시 재생성한다.
        """
        state: dict[str, dict] = {}
        for key in self.agents:
            state[key] = {
                "location":     self._agent_location.get(key, ""),
                "visual":       self._agent_visual.get(key, ""),
                "knowledge":    sorted(self._agent_knowledge.get(key, set())),
                "stranger_map": dict(self._stranger_map.get(key, {})),
                "path":         list(self._agent_path.get(key, [])),
                # 감염 상태를 빼먹으면 resume/load 때 전원이 "S"로 되돌아가 유행이
                # 통째로 초기화된다 — 위치/외모와 정확히 같은 버그 클래스.
                "infection":    self._export_infection(key),
            }
        return state

    def _export_infection(self, key: str) -> dict:
        """감염 상태 직렬화.

        `infected_at_minutes`는 **이번 run 기준의 경과분 앵커**다(fixed 모드에서
        `_current_elapsed_minutes`는 재개할 때마다 wave 0 = 0분부터 다시 센다).
        그대로 저장하면 재개 후 `now - infected_at_minutes`가 음수가 되어 증상
        진행이 1단계로 되감긴다. 그래서 저장 시점의 **경과 분**을
        `elapsed_minutes_since_infection`으로 함께 남겨 복원 쪽에서 새 run의
        기준점으로 재기준화한다. `recover_at_minutes`는 절대값이 아니라 감염
        시점부터의 델타라 앵커와 무관하게 그대로 저장하면 된다.
        """
        entry = dict(self._agent_infection.get(key, {}))
        since = entry.get("infected_at_minutes")
        if entry.get("status") == "I" and isinstance(since, int):
            now = self._current_elapsed_minutes(self.completed_waves)
            entry["elapsed_minutes_since_infection"] = max(0, now - since)
        return entry

    def restore_agent_state(self, states: dict[str, dict] | None) -> None:
        """export_agent_state()가 만든 상태를 복원. 없는 키/에이전트는 초기값 유지."""
        if not states:
            return
        for key, st in states.items():
            if key not in self.agents or not isinstance(st, dict):
                continue
            if st.get("location") is not None:
                self._agent_location[key] = st["location"] or ""
            if st.get("visual") is not None:
                self._agent_visual[key] = st["visual"] or ""
            knowledge = st.get("knowledge")
            if knowledge is not None:
                self._agent_knowledge[key] = {k for k in knowledge if k in self.agents}
            stranger_map = st.get("stranger_map")
            if stranger_map is not None:
                valid = {
                    sid: real_key
                    for sid, real_key in stranger_map.items()
                    if real_key in self.agents
                }
                self._stranger_map[key]  = valid
                self._stranger_rmap[key] = {rk: sid for sid, rk in valid.items()}
            path = st.get("path")
            if path is not None:
                self._agent_path[key] = list(path)
            infection = st.get("infection")
            if isinstance(infection, dict) and infection.get("status") in ("S", "I", "R"):
                status     = infection["status"]
                rec        = infection.get("recovered_wave")
                rec_min    = infection.get("recovered_at_minutes")
                since      = None
                recover_at = None
                if status == "I":
                    # 새 run은 (fixed 모드에서) 경과 0분부터 다시 세므로, 저장된 경과
                    # 분만큼 과거로 앵커를 옮긴다 → 재개 첫 wave에서 경과분이 저장
                    # 시점 값을 그대로 이어받는다. variable 모드는 elapsed_minutes_init
                    # 으로 누적 경과가 복원되므로 base가 그 값이 되어 역시 이어진다.
                    base    = self._current_elapsed_minutes(0)
                    elapsed = infection.get("elapsed_minutes_since_infection")
                    since   = base - max(0, elapsed) if isinstance(elapsed, int) else base
                    recover_at = infection.get("recover_at_minutes")
                    if not isinstance(recover_at, int):
                        # 구버전 스냅샷(회복 목표 없음) — 지금 규칙으로 새로 뽑는다.
                        recover_at = self._sample_recovery_minutes()
                self._agent_infection[key] = {
                    "status":               status,
                    "infected_at_minutes":  since,
                    "recover_at_minutes":   recover_at,
                    "recovered_wave":       rec     if isinstance(rec, int)     else None,
                    "recovered_at_minutes": rec_min if isinstance(rec_min, int) else None,
                    "notify_recovery":      bool(infection.get("notify_recovery", False)),
                }

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _emit(self, event_type: str, data: dict):
        if self._event_queue is not None:
            self._event_queue.put({"type": event_type, "data": data})
        if event_type in _PERSIST_EVENTS and self._db is not None and self._sim_id is not None:
            wave = data.get("wave", 0)
            self._db.log_event(self._sim_id, wave, event_type, data)

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

    def _format_time_str(self, total_min: int) -> str:
        """총 분(시작 시각 + 경과분) 값을 '요일 + 오전/오후 N시 M분' 문자열로 변환.

        total_min을 1440(하루)으로 나눈 몫만큼 시작 요일에서 날짜가 넘어간 것으로 보고
        자정 롤오버마다 요일을 순환시킨다. fixed 모드(sim_start_minutes + wave*time_per_wave)와
        variable 모드(sim_start_minutes + elapsed_minutes) 모두 같은 '총 경과 분'을 넘기므로
        이 계산 하나가 양쪽 모드를 모두 커버한다.
        """
        day_offset, minute_of_day = divmod(total_min, 24 * 60)
        weekday = _WEEKDAY_LABELS[(self._sim_start_weekday_idx + day_offset) % 7]
        hour, minute = divmod(minute_of_day, 60)
        if hour < 12:
            return f"{weekday} 오전 {hour}시 {minute:02d}분"
        display_hour = hour if hour == 12 else hour - 12
        return f"{weekday} 오후 {display_hour}시 {minute:02d}분"
