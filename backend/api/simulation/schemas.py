"""Pydantic schemas for simulation API."""
from __future__ import annotations

from pydantic import BaseModel


class AgentConfig(BaseModel):
    name:           str
    system_prompt:  str
    icon:           str       = "🤖"
    initial_active: bool      = True
    display_name:   str       = ""
    groups:         list[str] = []   # 소속 그룹 ID 목록. 빈 배열 = 전체 에이전트 노출 (하위 호환)


class ScenarioEvent(BaseModel):
    wave:    int
    type:    str             # "system_message" | "agent_enter" | "agent_exit"
    message: str       = ""
    targets: list[str] = ["all"]
    agent:   str       = ""  # agent_enter / agent_exit 전용


class ExtraField(BaseModel):
    name:    str
    default: str = ""


class SimStartConfig(BaseModel):
    scenario_id:            str | None       = None
    agents:                 list[AgentConfig]
    background:             str
    start_agent:            str
    max_waves:              int              = 10
    step_delay:             float            = 1.0
    token_limit:            int              = 8192
    extra_fields:           list[ExtraField] = [
        ExtraField(name="emotion",     default="neutral"),
        ExtraField(name="action",      default="speak"),
        ExtraField(name="action_note", default=""),
    ]
    events:                 list[ScenarioEvent] = []
    output_format_template: str              = ""
    summary_interval:       int              = 0   # 0 = 비활성, N = N웨이브마다 LLM 요약
    server_id:              str | None       = None  # None = DB default 서버, 미설정 시 env 폴백


class ScenarioSave(BaseModel):
    name:        str
    description: str = ""
    config:      SimStartConfig


class SimContinueConfig(BaseModel):
    start_agent: str
    max_waves:   int              = 10
    step_delay:  float            = 1.0
    events:      list[ScenarioEvent] = []
