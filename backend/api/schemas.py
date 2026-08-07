from typing import Literal

from pydantic import BaseModel, ConfigDict


class NewConversation(BaseModel):
    system_prompt: str = ""
    title: str = "새 대화"
    agent_id: str | None = None
    router_mode: bool = False


class ChatMessage(BaseModel):
    content: str
    thinking: bool = False


class UpdateTitle(BaseModel):
    title: str


class AgentCreate(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    icon: str = "🤖"
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 1024
    role: str = ""
    goal: str = ""
    backstory: str = ""


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    icon: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    role: str | None = None
    goal: str | None = None
    backstory: str | None = None


class ServerCreate(BaseModel):
    name: str
    base_url: str
    model: str
    api_key: str = ""
    weight: int = 1
    is_default: bool = False
    thinking: bool = False
    max_model_len: int = 0  # 0 = 자동 감지


class ServerHealth(BaseModel):
    """서버 연결 확인 결과.

    status:
      - "ok"            : 연결 정상 + 설정된 모델이 서버에 존재
      - "model_missing" : 서버는 살아있으나 모델을 확인할 수 없음
      - "unreachable"   : 서버에 연결 실패
      - "disabled"      : 서버가 비활성화되어 확인하지 않음
    """

    # model_ok 가 pydantic 의 "model_" 보호 네임스페이스와 충돌하는 것을 허용
    model_config = ConfigDict(protected_namespaces=())

    server_id: str
    name: str
    model: str
    status: Literal["ok", "model_missing", "unreachable", "disabled"]
    healthy: bool           # status == "ok" 일 때만 True
    reachable: bool         # /health 200 여부
    model_ok: bool          # /v1/models 에서 모델 확인 여부
    detail: str             # 사용자에게 보여줄 한국어 설명
    available_models: list[str] = []


class ServerUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    weight: int | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    thinking: bool | None = None
    max_model_len: int | None = None
