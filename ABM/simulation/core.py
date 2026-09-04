import json
import os
import queue
import threading
import logging

from ..agent import Agent
from ..config import LOG_DIR
from ..llm import LLMCall
from ..prompt_contract import (
    build_relationship_contract,
    build_world_contract,
    verify_contract,
)
from .location import _LocationMixin
from .infection import _InfectionMixin
from .meeting import _MeetingMixin
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
    "meeting_update",
    "scene_event",
    "wave_summary",
    "infection_update",
})

# 시작 요일 키(프론트/스키마와 동일) → 표시 라벨. 인덱스 = 월요일 기준 0~6.
# 정의는 `_constants.py` 에 있다 — 마크다운 내보내기(ABM/export/labels.py)가 엔진
# 전체를 import 하지 않고도 같은 라벨을 쓰기 위함. 이름은 여기서도 그대로 노출한다.
from ._constants import _WEEKDAY_KEYS, _WEEKDAY_LABELS  # noqa: F401

_DEFAULT_TIME_CATEGORIES: list[dict] = [
    {"id": "meal_or_brief",     "label": "식사·짧은 용무 등 스킵되듯 지나가는 장면", "min_minutes": 5,   "max_minutes": 10},
    {"id": "normal_scene",      "label": "그 외 일반적인 대화/활동이 이어지는 장면",   "min_minutes": 15,  "max_minutes": 30},
    {"id": "alone_or_offscreen", "label": "혼자 있거나 외부에 나가 직접 대화가 없는 상태", "min_minutes": 60,  "max_minutes": 120},
    {"id": "night_sleep",       "label": "취침 등 야간 장시간 경과",                   "min_minutes": 240, "max_minutes": 420},
]


class Simulation(_LocationMixin, _InfectionMixin, _MeetingMixin, _TargetsMixin, _EventsMixin, _TurnMixin, _StepMixin, _SystemMixin, _RunnerMixin):
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
        # {에이전트 key: {상대 key: 그 상대를 부르는 관계}}. 각 항목은 **그 에이전트
        # 시점**이다(김봉남→채민경="아내", 채민경→김봉남="남편"). 비었거나 생략되면
        # 관계 계약 블록이 아예 붙지 않는다 = 기능 미사용.
        agent_relationships: dict[str, dict[str, str]] | None = None,
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
        # 가변 시간 점프 상한 — LLM 분류기가 고른 카테고리의 랜덤 경과분을 엔진이
        # 벽시계·동석 상황 기준으로 결정론적으로 캡한다. 0 = 해당 캡 비활성.
        max_scene_jump_minutes:   int                  = 45,
        max_daytime_jump_minutes: int                  = 180,
        elapsed_minutes_init: int                      = 0,
        wave_base_init:   int                           = 0,
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

        # 이 run() 호출 이전까지 누적된 wave 수. fresh /start는 0이고, /continue·
        # /resume은 이전 run들의 (start_wave + total_waves)로 주입된다. run()의 루프
        # 카운터(per-run 0-based)와 더해 emit·영속화용 **표시 wave**(disp_wave)를 만든다.
        # 시간/감염/목표기간 계산은 이 값과 무관하게 per-run 카운터만 쓴다 —
        # 시계 연속성은 이미 `_elapsed_minutes`(elapsed_minutes_init) 복원이 담당한다.
        self._wave_base: int = max(0, int(wave_base_init))

        if initial_agents is not None:
            self.active_agents: set[str] = set(initial_agents) & set(agents.keys())
        else:
            self.active_agents = set(agents.keys())

        self._agent_groups: dict[str, list[str]] = agent_groups or {}
        self._visible_targets: dict[str, list[str]] = self._build_visible_targets(
            self._agent_groups
        )
        # 관계 지도. 실존하지 않는 상대 key(시나리오 편집 중 이름이 바뀌면 생긴다)와
        # 자기 자신 참조는 여기서 걸러내고, 사유는 _verify_engine_contract()가 경고로
        # 낸다. 이후 모든 소비 지점(계약 블록·<TARGETS>·[이 자리의 사람들]·knowledge
        # 시드)은 이 정제된 맵 하나만 본다.
        self._dangling_relationships: list[str] = []
        self._agent_relationships: dict[str, dict[str, str]] = self._sanitize_relationships(
            agent_relationships
        )

        self._summary_interval:    int        = max(0, summary_interval)
        # 재개 후 첫 요약 구간이 재개 지점(disp_wave 축)부터 측정되도록 base 기준으로 둔다.
        self._last_summarized_wave: int       = self._wave_base - 1
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
        # 가변 시간 점프 상한. _RunnerMixin._clamp_time_jump 가 소비한다.
        self._max_scene_jump_minutes:   int = max(0, int(max_scene_jump_minutes))
        self._max_daytime_jump_minutes: int = max(0, int(max_daytime_jump_minutes))
        self._elapsed_minutes: int = elapsed_minutes_init

        # 위치 그래프 (인접 리스트) + 외부 공간 집합 + 인지 구역(zone) 맵
        # 주의: 여기서의 zone은 **위치 기반 인지 범위**이며, _agent_groups(캐릭터 관계
        # 그룹)와는 완전히 별개의 개념이다. 같은 zone의 다른 장소에 있는 사람은 서로
        # 존재를 인지하지만, 대화는 여전히 같은 장소(노드)에 있어야만 가능하다.
        self._location_graph:    dict[str, list[str]] = {}
        self._exterior_locations: set[str]            = set()
        self._location_zone:     dict[str, str]       = {}
        # zone -> 기본 입구 노드명. 외부 노드의 connects_to 에 zone 참조가 있을 때
        # 진입 엣지(X -> 입구)를 세울 대상. zone당 첫 is_zone_entry 노드만 채택.
        self._zone_entry:        dict[str, str]       = {}
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
                        if node.get("is_zone_entry"):
                            if zone in self._zone_entry:
                                logger.warning(
                                    f"[zone 입구] '{zone}' 에 입구가 2개 이상 — "
                                    f"'{self._zone_entry[zone]}' 유지, '{name}' 무시"
                                )
                            else:
                                self._zone_entry[zone] = name
                    elif node.get("is_zone_entry"):
                        logger.warning(f"[zone 입구] '{name}' 은 zone 이 없어 is_zone_entry 무시")
            # zone 참조 엣지를 노드 레벨 엣지로 전개. 전개 후 _location_graph 는
            # 여전히 순수 노드 인접 리스트라 BFS/adjacency/인지 로직 전부 무변경.
            self._expand_zone_edges(location_graph)

        # 위치/시간 계약 블록의 주입은 감염 모델 설정을 읽은 **뒤**에 한 번에 한다
        # (`_apply_engine_contract()`). 예전엔 여기서 map_section/time_section을
        # 각각 `agent.system_prompt +=` 로 이어붙였는데, 그 방식은 (1) 사용자 소유
        # 프롬프트를 오염시키고 (2) 새 계약 블록이 생길 때마다 주입 지점이 흩어져
        # "코드엔 기능이 있는데 지시어가 없어 조용히 죽는" 버그를 만들었다.

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

        # ── 엔진 계약 층 주입 ────────────────────────────────────────────────
        # 위치 그래프/시간/감염 설정이 전부 파싱된 뒤에 한 번에 조립한다.
        # 저장하지 않고 매 실행마다 config에서 새로 만들기 때문에, 엔진을
        # 업그레이드하면 기존 시나리오도 DB 마이그레이션 없이 새 계약을 받는다.
        self._apply_engine_contract()

        self._agent_path: dict[str, list[str]] = {}

        # 만남 lock: {추격자 key: 목표 key}. `move_to`에 장소가 아니라 **사람**을
        # 지목했을 때 세워지고, 동석/다른 move_to/목표 이탈에서 풀린다. 상세는
        # meeting.py 참고. (기본은 빈 dict — 사람 지목이 없으면 전혀 관여하지 않는다.)
        self._meeting_intent: dict[str, str] = {}
        # lock이 풀린 사유를 한 wave 동안만 들고 있는 임시 버퍼 {추격자: 사유}.
        # runner가 meeting_update 이벤트를 만들 때 소비하고 비운다. **직렬화하지
        # 않는다** — 재개 시점에 복원할 의미가 없는 파생 정보다(상태는 여전히
        # _meeting_intent 하나뿐).
        self._meeting_break_log: dict[str, str] = {}

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
            # 관계를 명시한 상대는 groups 와 무관하게 **무조건 아는 사이**다.
            # 안 그러면 "아내"라고 계약에 써 놓고 정작 같은 방에서 만나면
            # stranger_1 로 보이는 모순이 생긴다. (관계는 그룹보다 강한 신호다.)
            self._agent_knowledge[key].update(self._agent_relationships.get(key, {}))

        os.makedirs(log_dir, exist_ok=True)
        self._save_shared_log()

        self._verify_engine_contract()

    # ── 관계 지도 ─────────────────────────────────────────────────────────────

    def _sanitize_relationships(
        self, raw: dict[str, dict[str, str]] | None
    ) -> dict[str, dict[str, str]]:
        """관계 지도에서 렌더 불가능한 항목을 걸러내고 사유를 기록한다.

        거르는 것 두 가지:
        - **dangling** — 상대 key 가 이 시뮬레이션의 에이전트가 아닌 경우. 시나리오
          편집기에서 에이전트 이름(key)을 바꾸면 다른 에이전트의 relationships 에
          옛 이름이 남는다. 그대로 렌더하면 모델에게 존재하지 않는 ID 를 지목하라고
          가르치는 셈이라, 계약에서도 knowledge 시드에서도 뺀다.
        - **자기 참조** — 자기 자신을 관계에 넣은 경우(무의미).

        raise 하지 않는다. 사유는 `_dangling_relationships` 에 모아두고
        `_verify_engine_contract()` 가 다른 계약 경고와 같은 자리에서 로그로 낸다.
        """
        cleaned: dict[str, dict[str, str]] = {}
        for key, rels in (raw or {}).items():
            if key not in self.agents or not rels:
                continue
            kept: dict[str, str] = {}
            for other, relation in rels.items():
                if other == key:
                    self._dangling_relationships.append(
                        f"{key}의 relationships 에 자기 자신이 들어 있습니다 — 무시합니다"
                    )
                    continue
                if other not in self.agents:
                    self._dangling_relationships.append(
                        f"{key}의 relationships 에 존재하지 않는 에이전트 "
                        f"{other!r}(관계: {relation!r})가 있습니다 — 계약에서 제외합니다"
                    )
                    continue
                kept[other] = relation
            if kept:
                cleaned[key] = kept

        # 단방향 관계 — a 는 b 를 관계로 적었는데 b 는 a 를 안 적은 경우.
        # "각자 자기 시점"이라 오류는 아니지만, b 는 a 를 낯선 이(stranger_N)로 보게
        # 되므로(관계 knowledge 시드가 화자 방향뿐) 대부분 config 실수다. 경고만.
        for key, kept in cleaned.items():
            for other in kept:
                if key not in cleaned.get(other, {}):
                    self._dangling_relationships.append(
                        f"{key}→{other} 관계는 있는데 {other}→{key} 가 없습니다 — "
                        f"{other}는 {key}를 낯선 이로 봅니다"
                    )
        return cleaned

    # ── 엔진 계약 층 ──────────────────────────────────────────────────────────

    def _time_enabled(self) -> bool:
        """시간 개념이 켜져 있는가 (fixed + time_per_wave>0, 또는 variable 모드)."""
        return self._time_mode == "variable" or self._time_per_wave > 0

    @property
    def cumulative_waves(self) -> int:
        """이 run 이전까지 누적 wave + 이번 run에서 완료한 wave 수(표시/리포팅용).

        `completed_waves`는 per-run 값(테스트·`_current_elapsed_minutes` 폴백 의존)이라
        그대로 두고, 재개 체인 전체에서 몇 wave가 진행됐는지는 이 프로퍼티로 읽는다.
        """
        return self._wave_base + self.completed_waves

    def contract_flags(self) -> dict:
        """계약 빌더/검증기에 넘길 현재 실행 설정의 feature 플래그."""
        return {
            "has_location_graph": bool(self._location_graph),
            "has_zone":           bool(self._location_zone),
            "time_enabled":       self._time_enabled(),
            "infection_enabled":  bool(self._infection_enabled),
        }

    def build_engine_world_contract(self) -> str:
        """이 실행의 정적 계약 블록(지도 + 시간 + 감염)."""
        return build_world_contract(
            location_graph     = self._location_graph,
            exterior_locations = self._exterior_locations,
            location_zone      = self._location_zone,
            zone_entry         = self._zone_entry,
            time_enabled       = self._time_enabled(),
            infection_enabled  = self._infection_enabled,
            disease_name       = self._infection_disease_name,
        )

    def _apply_engine_contract(self) -> None:
        """에이전트마다 정적 계약 블록과 feature 플래그를 건다.

        지도/시간/감염(`world`)은 시뮬레이션 전체가 공유하지만 관계 지도는 **화자
        시점**이라 에이전트마다 다르다. 그래서 공유 문자열 하나를 전원에게 거는
        대신 여기서 per-agent 로 조립한다. relationships 가 하나도 없는 시나리오는
        `build_relationship_contract` 가 `""` 를 돌려주므로 전원이 예전과 **글자
        단위로 같은** 계약을 받는다.
        """
        world = self.build_engine_world_contract()
        flags = self.contract_flags()
        for key, agent in self.agents.items():
            rels = self._agent_relationships.get(key, {})
            agent.set_engine_contract(
                world + build_relationship_contract(rels, self._key_to_alias),
                has_location_graph = flags["has_location_graph"],
                has_zone           = flags["has_zone"],
                relationships      = rels,
            )

    def _verify_engine_contract(self) -> list[str]:
        """활성 feature마다 필요한 지시어가 실제로 주입됐는지 확인해 경고를 남긴다.

        raise 하지 않는다 — 시뮬레이션은 계속 돌아야 한다. 이 어서션이 잡아내려는
        버그 클래스는 "코드엔 기능이 있는데 프롬프트에 지시어가 없어 조용히 죽는"
        경우다(예: 옛 프리즈 템플릿을 오버라이드로 들고 있어 move_to 스키마가
        빠진 시나리오). 반환값은 테스트용.
        """
        flags = self.contract_flags()
        seen: set[str] = set()
        problems: list[str] = []
        # 관계 지도의 dangling/자기참조는 계약 문자열에서는 이미 사라져 있어
        # verify_contract 로는 잡히지 않는다 — 초기화 때 기록해 둔 사유를 낸다.
        for problem in self._dangling_relationships:
            if problem in seen:
                continue
            seen.add(problem)
            problems.append(problem)
            logger.warning(f"[계약 검증] 관계 지도: {problem}")
        for key, agent in self.agents.items():
            assembled = agent.get_system_message([], self._key_to_alias)["content"]
            for problem in verify_contract(assembled, **flags):
                if problem in seen:
                    continue  # 같은 누락을 에이전트 수만큼 반복해서 찍지 않는다
                seen.add(problem)
                problems.append(problem)
                logger.warning(f"[계약 검증] {key}: {problem}")
        return problems

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
        """시나리오 시작부터 '지금'까지의 시뮬레이션 내 총 경과 분.

        에이전트에게 보여지는 시각 계산(`_assemble_agent_prompt`)과 목표 기간 판정
        (`run()`)이 쓰는 기준과 **정확히 같은 값**이어야 한다 — 감염 진행이 프롬프트
        속 시계와 어긋나면 "밤새 앓았는데 증상은 그대로"류의 모순이 생긴다.

        `self._elapsed_minutes`는 두 모드 모두에서 **이전 run들의 누적 경과**를 담는
        자리다(`elapsed_minutes_init`으로 복원되고, `/continue`는 리셋 직전에 이번
        run의 경과를 여기에 접어 넣는다). 그래서 어느 모드든 반환값은
        "이전 run들의 누적 + 이번 run의 wave 경과"다.

        - variable 모드: LLM 분류로 누적된 `self._elapsed_minutes`(wave 무관)
        - fixed 모드(time_per_wave > 0): `_elapsed_minutes + wave * time_per_wave`.
          `_elapsed_minutes`를 빼먹으면 `run()`이 호출마다 wave 0부터 다시 세므로
          `/continue`·`/resume` 첫 wave에서 시계가 시작 시각으로 되감긴다.
        - 시간 개념 비활성(fixed + time_per_wave == 0): `_elapsed_minutes`(보통 0)
          → 감염자는 첫 증상 단계에 머물고 자연 회복도 일어나지 않는다. 시간 기준
            모델에서 이는 버그가 아니라 "시간이 흐르지 않는 세계"의 정의다.
        """
        if self._time_mode == "variable":
            return self._elapsed_minutes
        if self._time_per_wave > 0:
            wave_idx = self.completed_waves if wave is None else int(wave)
            return self._elapsed_minutes + max(0, wave_idx) * self._time_per_wave
        return self._elapsed_minutes

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
                # 만남 lock. 빼먹으면 resume 직후 "누굴 만나러 가던 중"이라는 사실만
                # 사라지고 경로는 남아, 상대가 움직여도 더 이상 따라가지 않는다.
                "meeting_target": self._meeting_intent.get(key),
                # 감염 상태를 빼먹으면 resume/load 때 전원이 "S"로 되돌아가 유행이
                # 통째로 초기화된다 — 위치/외모와 정확히 같은 버그 클래스.
                "infection":    self._export_infection(key),
            }
        return state

    def _export_infection(self, key: str) -> dict:
        """감염 상태 직렬화.

        `infected_at_minutes`는 경과분 축의 **절대 앵커**지만, 그 축의 원점은
        재개 방식에 따라 달라질 수 있다(예: 구버전 스냅샷, 또는 저장된
        elapsed_minutes와 다른 `elapsed_minutes_init`으로 되살아나는 경우).
        그래서 앵커를 그대로 믿지 않고 저장 시점의 **감염 후 경과 분**을
        `elapsed_minutes_since_infection`으로 함께 남겨, 복원 쪽에서 새 run의
        원점 기준으로 다시 계산한다. `recover_at_minutes`는 절대값이 아니라 감염
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
                # 관계 지도는 **config 사실**이라 항상 knowledge 시드에 포함돼야 한다
                # (__init__ 의 시드와 같은 불변식). /load·/resume 은 얼려진 config_json
                # 에서 cfg 를 만들므로 관계를 쓰는 run 은 스냅샷 knowledge 에 이미 그
                # 시드가 들어 있어 이 합집합이 no-op 이지만, 손편집 스냅샷이나 관계
                # 시드보다 오래된 knowledge 가 들어와도 계약("아내")과 knowledge 가
                # 어긋나(stranger_N) 보이지 않도록 방어적으로 다시 넣는다.
                self._agent_knowledge[key] = (
                    {k for k in knowledge if k in self.agents}
                    | set(self._agent_relationships.get(key, {}))
                )
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
            meeting = st.get("meeting_target")
            if meeting and meeting in self.agents:
                self._meeting_intent[key] = meeting
            infection = st.get("infection")
            if isinstance(infection, dict) and infection.get("status") in ("S", "I", "R"):
                status     = infection["status"]
                rec        = infection.get("recovered_wave")
                rec_min    = infection.get("recovered_at_minutes")
                since      = None
                recover_at = None
                if status == "I":
                    # base = 재개 첫 wave의 '지금'. 두 모드 모두 elapsed_minutes_init
                    # (finalize_run이 저장한 총 경과)이 반영되므로, 저장된 감염 후
                    # 경과분만큼 과거로 앵커를 옮기면 증상 진행이 정확히 이어진다.
                    # fixed 모드도 이제 `_elapsed_minutes + wave*tpw`라 base가 올바른
                    # 원점이 된다 — 별도 보정 불필요.
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
