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
    web_search: bool = False


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
    # ── 시뮬레이션(ABM) 에이전트와 공유되는 필드 ──────────────────────────────
    # 채팅 파이프라인은 이 값들을 해석하지 않는다. 시뮬레이션 -> 채팅 가져오기 시
    # 데이터가 유실되지 않도록 보존만 하며, 다시 시뮬레이션으로 내보낼 때 사용된다.
    gender: str = "auto"
    groups: list[str] = []
    location: str = ""
    visual_description: str = ""
    display_name: str = ""
    initial_active: bool = True


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
    # 시뮬레이션 공유 필드. exclude_unset 기반 부분 업데이트이므로 요청에 없으면 보존된다.
    gender: str | None = None
    groups: list[str] | None = None
    location: str | None = None
    visual_description: str | None = None
    display_name: str | None = None
    initial_active: bool | None = None


class ServerCreate(BaseModel):
    name: str
    base_url: str
    model: str
    provider_type: Literal["vllm", "openai", "anthropic"] = "vllm"
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


class ModelProbeRequest(BaseModel):
    """아직 저장되지 않은 서버 설정으로 사용 가능한 모델 목록을 조회하는 요청.

    DB 를 전혀 건드리지 않는 순수 조회용이므로 model 필드가 없다(그걸 알아내는 게 목적).
    base_url 이 비어있으면 provider 의 기본 주소를 사용한다.
    """

    provider_type: Literal["vllm", "openai", "anthropic"] = "vllm"
    base_url: str = ""
    api_key: str = ""
    # 편집 모드에서 API Key 입력란은 항상 비어서 시작한다(마스킹 값 prefill 금지).
    # api_key 가 비어있고 이 값이 있으면, DB 에 저장된 해당 서버의 평문 키로 대신 조회한다.
    server_id: str | None = None


class ProbedModel(BaseModel):
    """조회된 모델 하나. context_length 는 API 가 알려주지 않으면 None."""

    id: str
    context_length: int | None = None


class ModelProbeResponse(BaseModel):
    provider_type: Literal["vllm", "openai", "anthropic"]
    base_url: str                # 실제로 조회에 사용된 주소(기본값 적용 후)
    models: list[ProbedModel] = []


class ServerUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    provider_type: Literal["vllm", "openai", "anthropic"] | None = None
    api_key: str | None = None
    weight: int | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    thinking: bool | None = None
    max_model_len: int | None = None
