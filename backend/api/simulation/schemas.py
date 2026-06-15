"""Pydantic schemas for simulation API."""
from __future__ import annotations

from pydantic import BaseModel


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


class ScenarioEvent(BaseModel):
    wave:    int
    type:    str             # "system_message" | "agent_enter" | "agent_exit" | "update_appearance"
    message: str       = ""
    targets: list[str] = ["all"]
    agent:   str       = ""  # agent_enter / agent_exit / update_appearance 전용


class LocationNode(BaseModel):
    name:        str
    connects_to: list[str] = []


class ExtraField(BaseModel):
    name:    str
    default: str = ""


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
    server_id:              str | None       = None  # None = DB default 서버, 미설정 시 env 폴백
    system_agent:           SystemAgentConfig = SystemAgentConfig()


class ScenarioSave(BaseModel):
    name:        str
    description: str = ""
    config:      SimStartConfig


class SimContinueConfig(BaseModel):
    start_agent: str
    max_waves:   int              = 10
    step_delay:  float            = 1.0
    events:      list[ScenarioEvent] = []
