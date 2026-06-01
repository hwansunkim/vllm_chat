from pydantic import BaseModel


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
