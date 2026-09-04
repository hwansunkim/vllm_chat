"""Pydantic schemas for simulation API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AgentConfig(BaseModel):
    name:               str
    system_prompt:      str
    icon:               str       = "🤖"
    gender:             str       = "auto"  # "auto" | "male" | "female" | "unknown"
    initial_active:     bool      = True
    display_name:       str       = ""
    groups:             list[str] = []   # 소속 그룹 ID 목록. 빈 배열 = 전체 에이전트 노출 (하위 호환)
    # 관계 지도 {상대 agent key(= AgentConfig.name): 내가 그를 부르는 관계}.
    # 예) 김봉남: {"채민경": "아내", "김미경": "큰딸"} / 채민경: {"김봉남": "남편"}.
    # **각자 자기 시점**이라 서로 대칭일 필요가 없다. location/groups 처럼 ABM 엔진이
    # 해석하는 필드로, 엔진은 이 값으로 (1) [아는 사람] 계약 블록을 에이전트별로 만들고
    # (2) <TARGETS>·[이 자리의 사람들]에 관계어 라벨을 붙이고 (3) 서로를 known 으로 시드한다.
    # 빈 dict = 관계 기능 미사용(계약 블록이 붙지 않고 나머지 동작은 완전히 동일).
    relationships:      dict[str, str] = {}
    location:           str       = ""  # 초기 위치 (빈값이면 위치 미설정 = 전체 노출)
    visual_description: str       = ""  # 모르는 사람에게 보이는 외모 묘사
    server_id:          str | None = None  # 이 에이전트만 사용할 LLM 서버. None/빈값 = 시뮬레이션 기본 서버(SimStartConfig.server_id)
    # 이 에이전트만 사용할 샘플링 온도. None = 시뮬레이션 기본값(SimStartConfig.temperature)
    temperature:        float | None = Field(default=None, ge=0.0, le=2.0)
    # ── 채팅 에이전트(backend/db agents 테이블)와 공유되는 필드 ────────────────
    # ABM 엔진과 프롬프트 조립은 이 값들을 전혀 해석하지 않는다. 채팅 -> 시뮬레이션
    # 가져오기 시 값이 유실되지 않도록 시나리오 JSON에 보존만 하며, 다시 채팅으로
    # 내보낼 때 사용된다. model 은 채팅의 모델명 문자열로, 시뮬레이션의 server_id
    # (서버 인스턴스 지정)와 개념이 달라 자동 변환하지 않고 원본 그대로 왕복시킨다.
    role:               str | None = None
    goal:               str | None = None
    backstory:          str | None = None
    description:        str | None = None
    model:              str | None = None
    max_tokens:         int | None = None


class ScenarioEvent(BaseModel):
    wave:    int
    # "system_message" | "agent_enter" | "agent_exit" | "update_appearance" | "infect_agent"
    type:    str
    message: str       = ""
    targets: list[str] = ["all"]
    # agent_enter / agent_exit / update_appearance / infect_agent 전용.
    # infect_agent: 해당 에이전트를 이 wave에서 즉시 감염(I) 상태로 전이시키는 "환자 0번" 시드.
    #               message는 관전용 이벤트 피드에만 쓰이고 에이전트 메모리에는 주입되지 않는다
    #               (LLM은 증상 서사 텍스트로만 감염을 인지한다).
    agent:   str       = ""


class LocationNode(BaseModel):
    name:        str
    # 노드명 또는 zone명(그 zone에 입구 노드가 있을 때). zone 참조는 파싱 직후
    # 엔진 컴파일 단계에서 노드 레벨 엣지로 전개된다(ABM/simulation/core._expand_zone_edges).
    # 스키마는 zone 참조를 reject 하지 않는다 — import/구버전 시나리오 하위 호환.
    connects_to: list[str] = []
    is_exterior: bool      = False
    # 인지 구역. 같은 zone의 다른 장소에 있는 사람은 서로 존재를 인지하지만 대화는 불가.
    # 빈 문자열 = zone 없음(독립 노드). 위치 개념이며 AgentConfig.groups(캐릭터 관계 그룹)와 무관.
    zone:        str       = ""
    # 이 노드를 zone의 기본 입구로 지정. zone당 1개(중복 시 첫 번째만 채택 + warning).
    # 외부 노드가 connects_to에 zone명을 넣으면: 진입은 이 입구를 거치고, 탈출은
    # zone 내부 어느 노드에서든 1홉. zone 없는 노드에 붙으면 엔진이 무시(로그).
    is_zone_entry: bool    = False


class ExtraField(BaseModel):
    name:    str
    default: str = ""


# 출력 JSON 스키마에 기본으로 실리는 추가 필드. SimStartConfig 와 계약 프리뷰가
# 같은 기본값을 보도록 한곳에 둔다 (pydantic v2 는 모델 기본값을 deep-copy 한다).
DEFAULT_EXTRA_FIELDS: list[ExtraField] = [
    ExtraField(name="emotion",     default="neutral"),
    ExtraField(name="action",      default="speak"),
    ExtraField(name="action_note", default=""),
]

# wave당 경과 시간(분). SimStartConfig 와 계약 프리뷰가 같은 기본값을 봐야, 필드를
# 생략한 프리뷰 요청이 실제 실행과 다른 "시간 개념 OFF" 를 보여주지 않는다.
DEFAULT_TIME_PER_WAVE = 30


class TimeCategory(BaseModel):
    id:           str
    label:        str
    min_minutes:  int
    max_minutes:  int

    @field_validator("max_minutes")
    @classmethod
    def _max_not_below_min(cls, v: int, info) -> int:
        min_v = info.data.get("min_minutes")
        if min_v is not None and v < min_v:
            raise ValueError("max_minutes must be >= min_minutes")
        return v


# 분 단위 입력의 공통 상한(= 100년). 프론트의 MAX_TARGET_DURATION_MINUTES와 같은 값으로,
# 목표 기간·증상 단계·회복 시간 등 모든 "시뮬레이션 내 분" 입력에 함께 적용한다.
MAX_DURATION_MINUTES = 52560000


class SymptomStage(BaseModel):
    """감염 후 **경과 시간(분)** 구간별 증상 서사.

    ``min_minutes <= (지금의 경과분 - 감염 시점의 경과분) <= max_minutes`` 인 첫 구간의
    ``symptom_text``가 해당 에이전트의 상황 컨텍스트에 매 턴 주입된다. 정의된 최대 구간을
    넘어서면 가장 늦은 단계를 계속 유지한다.

    wave가 아니라 분으로 정의하는 이유: variable 시간 모드에서는 wave 길이가 5분~7시간까지
    들쭉날쭉해 "N wave 경과"가 병의 진행을 전혀 대표하지 못한다. 프론트는 이 값을
    (일 + 시간) 복합 입력으로 받아 분으로 변환해 보낸다.

    주의: 시간 개념이 꺼진 시나리오(``time_mode="fixed"`` AND ``time_per_wave == 0``)에서는
    경과분이 항상 0이라 모든 감염자가 첫 단계에 머물고 자연 회복도 일어나지 않는다.
    """
    id:           str
    label:        str
    min_minutes:  int = Field(default=0, ge=0, le=MAX_DURATION_MINUTES)
    max_minutes:  int = Field(default=0, ge=0, le=MAX_DURATION_MINUTES)
    symptom_text: str

    @field_validator("max_minutes")
    @classmethod
    def _max_not_below_min(cls, v: int, info) -> int:
        min_v = info.data.get("min_minutes")
        if min_v is not None and v < min_v:
            raise ValueError("max_minutes must be >= min_minutes")
        return v


class InfectionModelConfig(BaseModel):
    """결정론적 감염병 모델(SIR/SIS) 설정.

    감염 판정은 전적으로 엔진(순수 파이썬)이 수행한다. LLM은 status·확률·경과 시간 같은
    raw 값을 절대 보지 않고, 오직 ``symptom_stages``의 서사 텍스트만 상황 컨텍스트로 받는다.

    시간 축이 둘로 나뉜다: **전염은 wave·접촉 기준 확률**, **증상 진행과 회복은
    시뮬레이션 내 경과 시간(분) 기준**이다.
    """
    enabled:                  bool  = False
    disease_name:             str   = ""
    # 감염자와 같은 wave·같은 장소에 있는 비감염자 1명당 이번 wave에 전염될 확률
    transmission_probability: float = Field(default=0.3, ge=0.0, le=1.0)
    symptom_stages:           list[SymptomStage] = []
    # 회복까지 걸리는 시간(분) 구간. 감염 시점에 [min, max]에서 한 번 균등 샘플하고,
    # 감염 후 경과분이 그 값에 도달하면 회복시킨다(wave당 주사위를 굴리던
    # recovery_probability 모델은 폐기됨 — wave 길이에 따라 이환 기간이 달라졌다).
    # recovery_max_minutes == 0 이면 자연 회복 없음(만성).
    recovery_min_minutes:     int   = Field(default=7200,  ge=0, le=MAX_DURATION_MINUTES)   # 5일
    recovery_max_minutes:     int   = Field(default=14400, ge=0, le=MAX_DURATION_MINUTES)   # 10일
    # True = SIR(회복 후 면역, 재감염 불가) / False = SIS(회복 후 S로 복귀, 재감염 가능)
    immune_after_recovery:    bool  = True

    @field_validator("recovery_max_minutes")
    @classmethod
    def _recovery_max_not_below_min(cls, v: int, info) -> int:
        # v == 0 은 "자연 회복 없음(만성)"이라는 별도 의미라서 min과 비교하지 않는다.
        min_v = info.data.get("recovery_min_minutes")
        if min_v is not None and 0 < v < min_v:
            raise ValueError("recovery_max_minutes must be >= recovery_min_minutes")
        return v


DEFAULT_SYSTEM_AGENT_PROMPT = (
    "당신은 멀티에이전트 시뮬레이션의 내레이터이자 진행자입니다.\n"
    "주어진 시뮬레이션 요약과 침묵 중인 에이전트 목록을 분석하여\n"
    "이야기 흐름을 자연스럽게 이어가기 위한 개입이 필요한지 판단하세요.\n\n"
    "개입이 필요한 경우, 해당 에이전트에게 이야기 흐름에 맞는\n"
    "상황 묘사·사건·주변 변화·대화 유도 등의 메시지를 전달하세요.\n"
    "개입이 불필요한 경우(이야기가 자연스럽게 흐르고 있다면) interventions를 빈 배열로 반환하세요."
)


class SystemAgentConfig(BaseModel):
    enabled:               bool  = False
    icon:                  str   = "🎬"
    display_name:          str   = "내레이터"
    system_prompt:         str   = DEFAULT_SYSTEM_AGENT_PROMPT
    intervention_interval: int   = 1   # N웨이브마다 실행
    silence_threshold:     int   = 3   # N웨이브 미발화 = 침묵
    director_note:         str   = ""  # 시뮬레이션 서사 목표 (불변 나침반)


class SimStartConfig(BaseModel):
    scenario_id:            str | None       = None
    agents:                 list[AgentConfig]
    background:             str
    start_agent:            str
    max_waves:              int              = 10
    step_delay:             float            = 1.0
    token_limit:            int              = 8192
    llm_max_tokens:         int              = 16384
    extra_fields:           list[ExtraField] = DEFAULT_EXTRA_FIELDS
    events:                 list[ScenarioEvent] = []
    location_graph:         list[LocationNode]  = []
    lang_fix_enabled:       bool             = True
    lang_fix_retries:       int              = 2
    # ── 출력 계약(프롬프트 계약 층) ────────────────────────────────────────────
    # 출력 JSON 스키마·move_to 의미·target ID 규칙은 **엔진이 소유**하며
    # (`ABM/prompt_contract.py`) 실행 시점에 현재 설정으로 생성된다. 따라서 아래
    # 두 필드가 **비어 있는 것이 정상 경로**이고, 그래야 엔진을 업그레이드했을 때
    # 기존 시나리오도 자동으로 새 계약을 받는다.
    #
    # output_format_override — 고급 사용자용 opt-in 오버라이드. 값이 있을 때만
    #   출력 계약 템플릿을 통째로 대체한다. 이후 엔진 업데이트가 이 시나리오의
    #   **출력 계약에만** 자동 반영되지 않는다(지도/시간/감염 계약은 계속 최신).
    #   동기화는 사용자 책임.
    output_format_override: str              = ""
    # output_format_template — (구) 시나리오 저장 시점에 전체 템플릿이 통째로
    #   스냅샷되던 필드. 옛 config_json 을 그대로 파싱하기 위해 필드만 남긴다.
    #   **런타임에서 무조건 무시**되며(= 엔진이 항상 재생성), 저장 시에도
    #   기록하지 않는다. 새 코드는 output_format_override 만 볼 것.
    output_format_template: str              = ""
    # 0 = 비활성, N = N웨이브마다 LLM 요약. 요약은 디렉터(system_agent)의 유일한
    # 장거리 서사 신호다 — 0이면 디렉터가 "이 장면이 몇 wave째 맴돌고 있나"를
    # (최근 활동 다이제스트로만) 알 수 있어 주제 반복을 놓치기 쉽다. 기본값 5.
    summary_interval:       int              = 5
    sim_start_time:         str              = "09:00"  # HH:MM, 시뮬레이션 내 시작 시각
    sim_start_weekday:      Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"  # 시뮬레이션 내 시작 요일. 자정 롤오버마다 자동 증가
    time_per_wave:          int              = DEFAULT_TIME_PER_WAVE  # wave당 경과 시간(분). 0 = 시간 개념 비활성
    time_mode:              Literal["fixed", "variable"] = "fixed"  # "fixed" = time_per_wave 고정, "variable" = wave 내용을 LLM이 분류해 가변 경과
    time_categories:        list[TimeCategory] = [
        TimeCategory(id="meal_or_brief",      label="식사·짧은 용무",      min_minutes=5,   max_minutes=10),
        TimeCategory(id="normal_scene",       label="일반적인 대화/활동",   min_minutes=15,  max_minutes=30),
        TimeCategory(id="alone_or_offscreen", label="혼자 있음/외출",      min_minutes=60,  max_minutes=120),
        TimeCategory(id="night_sleep",        label="취침/장시간 경과",     min_minutes=240, max_minutes=420),
    ]  # time_mode="variable"일 때 LLM이 wave 내용을 분류하는 카테고리 목록
    idle_minutes_schedule:  list[int]        = [60, 120, 180]  # 강제 침묵 재투입 시 경과 시간(분) 스케줄 — 침묵 회차가 늘수록 다음 값 사용, 끝에서 캡
    # ── 가변 시간 점프 상한 (time_mode="variable" 전용) ────────────────────────
    # LLM 분류기는 "이 장면의 질감"만 정하고, 실제 경과 분의 **상한은 엔진이
    # 결정론적으로 강제**한다. 약한 모델이 오후 한복판에서 최대 범위 카테고리를
    # 골라 학원·퇴근·저녁 같은 재집결 장면을 통째로 건너뛰는 것을 막는다.
    # 두 캡 모두 0 = 해당 캡 비활성(순수 카테고리 랜덤값 사용).
    max_scene_jump_minutes:   int            = 45   # 실내 한 곳에 2명+ 동석 발화 중일 때의 점프 상한
    max_daytime_jump_minutes: int            = 180  # 밤(22~06시)이 아니고 집에 남은 사람이 있을 때의 점프 상한
    max_silence_waves:      int              = 3         # 연속 침묵 허용 wave 수 (early_stop_enabled + time_per_wave > 0일 때 활성)
    early_stop_enabled:     bool             = True      # False = 조기 종료 비활성 (max_waves까지 항상 실행)
    # 목표 기간(분). None = 미사용(기존 동작: max_waves + 침묵 조기종료만).
    # 설정 시 "시뮬레이션 내 경과 시간이 이 값에 도달"이 주 종료 신호가 되고,
    # max_waves는 상한 안전장치로 남는다 — 둘 중 먼저 도달하는 쪽에서 정상 종료.
    # 시간 개념이 비활성(time_mode="fixed" AND time_per_wave=0)이면 이 값은 조용히 무시된다.
    target_duration_minutes: int | None      = Field(default=None, ge=1)
    server_id:              str | None       = None  # None = DB default 서버, 미설정 시 env 폴백
    # 시뮬레이션 전체 기본 샘플링 온도. AgentConfig.temperature 로 에이전트별 오버라이드 가능.
    temperature:            float            = Field(default=0.7, ge=0.0, le=2.0)
    system_agent:           SystemAgentConfig = SystemAgentConfig()
    # 결정론적 감염병 모델. enabled=False(기본)면 상태 갱신도 프롬프트 주입도 전혀 일어나지 않는다.
    infection_model:        InfectionModelConfig = InfectionModelConfig()

    @field_validator("time_categories")
    @classmethod
    def _non_empty_categories(cls, v):
        if not v:
            raise ValueError("time_categories must not be empty")
        return v

    @field_validator("idle_minutes_schedule")
    @classmethod
    def _non_empty_schedule(cls, v):
        if not v:
            raise ValueError("idle_minutes_schedule must not be empty")
        return v

    # ── 계약 층 헬퍼 ──────────────────────────────────────────────────────────

    def effective_output_format_override(self) -> str | None:
        """`Agent(output_format_template=...)` 에 넘길 값.

        오버라이드가 **실제로 설정됐을 때만** 문자열을, 아니면 `None`(= 엔진이
        실행 시점에 현재 설정으로 생성)을 돌려준다. 구 `output_format_template`
        (프리즈 스냅샷)은 여기서 의도적으로 보지 않는다 — 옛 시나리오도 로드하면
        최신 엔진 계약을 받아야 하기 때문이다.
        """
        return self.output_format_override or None


class ScenarioSave(BaseModel):
    name:        str
    description: str = ""
    config:      SimStartConfig


# ── 엔진 프롬프트 계약 프리뷰 ─────────────────────────────────────────────────

class ContractPreviewRequest(BaseModel):
    """계약 문자열에 영향을 주는 config 조각만 받는다.

    `SimStartConfig` 의 부분집합이라 프론트는 편집 중인 설정 객체를 그대로 잘라
    보내면 된다. 여기 없는 필드는 계약을 바꾸지 않는다.
    """
    location_graph:         list[LocationNode]  = []
    time_mode:              Literal["fixed", "variable"] = "fixed"
    # SimStartConfig.time_per_wave 와 같은 기본값이어야 한다 — 생략된 프리뷰 요청이
    # 실제 실행과 다른 "시간 개념 OFF" 를 보여주면 오도한다.
    time_per_wave:          int                 = DEFAULT_TIME_PER_WAVE
    infection_model:        InfectionModelConfig = InfectionModelConfig()
    extra_fields:           list[ExtraField]    = DEFAULT_EXTRA_FIELDS
    output_format_override: str                 = ""
    # 인터뷰 모드처럼 출력 스키마를 빼는 경로를 미리 보고 싶을 때 False.
    include_output_schema:  bool                = True
    # 위치 그래프가 있는 시나리오는 실행 중 <TARGETS> 자리에 flat ID 목록이 아니라
    # "([현재 상황] 컨텍스트에서 …)" 안내가 들어간다(step.py 의 sit_targets). 프론트는
    # location_graph 가 비어있지 않으면 이 값을 True 로 보내 프리뷰가 실제 주입본과
    # 같은 target 블록을 그리게 한다.
    situation_targets:      bool                = False
    # 프리뷰용 더미 타깃(선택). 비우면 자리표시자 ID 하나로 렌더한다 — 실제 실행에서는
    # 매 턴 같은 자리에 있는 사람들로 채워진다.
    available_targets:      list[str]           = []
    key_to_alias:           dict[str, str]      = {}
    # 프리뷰는 "에이전트 한 명이 보는 계약"을 그린다. relationships 는 per-agent 라서
    # 프론트가 **지금 편집 중인 에이전트의** AgentConfig.relationships 를 그대로 보낸다
    # (비우면 [아는 사람] 블록도, <TARGETS> 관계 라벨도 렌더되지 않는다 = 미사용 상태).
    # 여기 실린 key 는 실존 검증을 하지 않는다 — 프리뷰는 편집 중 상태를 보여주는 거울이고,
    # dangling 필터링은 실행 시점(Simulation._sanitize_relationships)의 책임이다.
    relationships:          dict[str, str]      = {}

    def time_enabled(self) -> bool:
        return self.time_mode == "variable" or self.time_per_wave > 0


class ContractFlags(BaseModel):
    """이 설정에서 켜진 계약 feature. 프론트가 "무엇 때문에 이 블록이 붙었는지" 표시용."""
    has_location_graph:    bool
    has_zone:              bool
    time_enabled:          bool
    infection_enabled:     bool
    include_output_schema: bool


class ContractPreviewResponse(BaseModel):
    # world_contract + output_contract. 실제 주입되는 순서/문자열 그대로.
    contract:        str
    # 지도/시간/감염 정적 블록 (= Agent.engine_contract). 오버라이드와 무관하게 항상 엔진 소유.
    world_contract:  str
    # 출력 JSON 스키마 + move_to 의미 + target ID 규칙. output_format_override 가 대체하는 부분.
    output_contract: str
    flags:           ContractFlags
    # verify_contract 진단. 정상 설정이면 빈 배열. 오버라이드가 필수 지시어를 빠뜨리면 채워진다.
    warnings:        list[str] = []


class SimContinueConfig(BaseModel):
    start_agent: str
    max_waves:   int              = 10
    step_delay:  float            = 1.0
    events:      list[ScenarioEvent] = []
    # max_waves와 같은 성격의 "이번 이어서 실행" 예산. None = 목표 기간 미사용.
    target_duration_minutes: int | None = Field(default=None, ge=1)


# ── 사후 인터뷰 ────────────────────────────────────────────────────────────────

InterviewMode = Literal["memory_only", "full_log"]


class InterviewRequest(BaseModel):
    question: str
    mode:     InterviewMode = "memory_only"
    # 답변 길이 상한. None이면 실행 설정(config_json)의 llm_max_tokens를 따른다.
    max_tokens: int | None = Field(default=None, gt=0)

    @field_validator("question")
    @classmethod
    def _non_empty_question(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be empty")
        return v


class InterviewRecord(BaseModel):
    id:         int
    run_id:     str
    agent_key:  str
    mode:       str
    question:   str
    answer:     str
    created_at: float
    meta:       dict = {}
