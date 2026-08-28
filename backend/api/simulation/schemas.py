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
    connects_to: list[str] = []
    is_exterior: bool      = False
    # 인지 구역. 같은 zone의 다른 장소에 있는 사람은 서로 존재를 인지하지만 대화는 불가.
    # 빈 문자열 = zone 없음(독립 노드). 위치 개념이며 AgentConfig.groups(캐릭터 관계 그룹)와 무관.
    zone:        str       = ""


class ExtraField(BaseModel):
    name:    str
    default: str = ""


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


class SymptomStage(BaseModel):
    """감염 후 경과 wave 구간별 증상 서사.

    ``min_waves <= (현재 wave - 감염된 wave) <= max_waves`` 인 구간의 ``symptom_text``가
    해당 에이전트의 상황 컨텍스트에 매 턴 주입된다. 시간 개념(time_per_wave)이 꺼진
    시나리오에서도 동작해야 하므로 분이 아니라 wave 단위로 정의한다.
    """
    id:           str
    label:        str
    min_waves:    int
    max_waves:    int
    symptom_text: str

    @field_validator("max_waves")
    @classmethod
    def _max_not_below_min(cls, v: int, info) -> int:
        min_v = info.data.get("min_waves")
        if min_v is not None and v < min_v:
            raise ValueError("max_waves must be >= min_waves")
        return v


class InfectionModelConfig(BaseModel):
    """결정론적 감염병 모델(SIR/SIS) 설정.

    감염 판정은 전적으로 엔진(순수 파이썬)이 수행한다. LLM은 status·확률 같은 raw 값을
    절대 보지 않고, 오직 ``symptom_stages``의 서사 텍스트만 상황 컨텍스트로 받는다.
    """
    enabled:                  bool  = False
    disease_name:             str   = ""
    # 감염자와 같은 wave·같은 장소에 있는 비감염자 1명당 이번 wave에 전염될 확률
    transmission_probability: float = Field(default=0.3, ge=0.0, le=1.0)
    # 감염(I) 상태 에이전트가 이번 wave에 회복할 확률
    recovery_probability:     float = Field(default=0.1, ge=0.0, le=1.0)
    symptom_stages:           list[SymptomStage] = []
    # True = SIR(회복 후 면역, 재감염 불가) / False = SIS(회복 후 S로 복귀, 재감염 가능)
    immune_after_recovery:    bool  = True


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
    extra_fields:           list[ExtraField] = [
        ExtraField(name="emotion",     default="neutral"),
        ExtraField(name="action",      default="speak"),
        ExtraField(name="action_note", default=""),
    ]
    events:                 list[ScenarioEvent] = []
    location_graph:         list[LocationNode]  = []
    lang_fix_enabled:       bool             = True
    lang_fix_retries:       int              = 2
    output_format_template: str              = ""
    summary_interval:       int              = 0   # 0 = 비활성, N = N웨이브마다 LLM 요약
    sim_start_time:         str              = "09:00"  # HH:MM, 시뮬레이션 내 시작 시각
    sim_start_weekday:      Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"  # 시뮬레이션 내 시작 요일. 자정 롤오버마다 자동 증가
    time_per_wave:          int              = 30        # wave당 경과 시간(분). 0 = 시간 개념 비활성
    time_mode:              Literal["fixed", "variable"] = "fixed"  # "fixed" = time_per_wave 고정, "variable" = wave 내용을 LLM이 분류해 가변 경과
    time_categories:        list[TimeCategory] = [
        TimeCategory(id="meal_or_brief",      label="식사·짧은 용무",      min_minutes=5,   max_minutes=10),
        TimeCategory(id="normal_scene",       label="일반적인 대화/활동",   min_minutes=15,  max_minutes=30),
        TimeCategory(id="alone_or_offscreen", label="혼자 있음/외출",      min_minutes=60,  max_minutes=120),
        TimeCategory(id="night_sleep",        label="취침/장시간 경과",     min_minutes=240, max_minutes=420),
    ]  # time_mode="variable"일 때 LLM이 wave 내용을 분류하는 카테고리 목록
    idle_minutes_schedule:  list[int]        = [60, 120, 180]  # 강제 침묵 재투입 시 경과 시간(분) 스케줄 — 침묵 회차가 늘수록 다음 값 사용, 끝에서 캡
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


class ScenarioSave(BaseModel):
    name:        str
    description: str = ""
    config:      SimStartConfig


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
