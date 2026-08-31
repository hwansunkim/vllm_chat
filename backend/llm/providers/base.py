from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

HealthStatus = Literal["ok", "model_missing", "unreachable"]


class LLMHTTPError(RuntimeError):
    """LLM 서버가 비-2xx 를 반환했을 때 상태코드를 보존하는 예외.

    `RuntimeError` 서브클래스이므로 기존 `except RuntimeError` 경로
    (conversations.py 등)는 그대로 동작한다. 상태코드를 보존하는 이유는
    호출자(특히 bridge 의 재시도 predicate)가 408/429/5xx 같은 일시적
    실패와 400(파라미터 거부 등 영구 실패)을 구분하기 위해서다.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class HealthReport:
    """서버 연결 확인 결과.

    3가지 상태를 구분한다:
      - "ok"            : /health 200 이고 설정된 모델이 /v1/models 목록에 존재
      - "model_missing" : /health 는 200 이지만 모델을 확인하지 못함
                          (목록에 없거나 /v1/models 조회 자체가 실패)
      - "unreachable"   : /health 가 실패 (타임아웃, 커넥션 리셋, 비-200 등)
    """

    status: HealthStatus
    reachable: bool
    model_ok: bool
    detail: str = ""
    available_models: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """완전 정상(연결 + 모델 확인)일 때만 True."""
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "healthy": self.healthy,
            "reachable": self.reachable,
            "model_ok": self.model_ok,
            "detail": self.detail,
            "available_models": self.available_models,
        }


@runtime_checkable
class LLMProvider(Protocol):
    id: str
    name: str
    base_url: str
    model: str
    enabled: bool
    is_default: bool
    # 서버 기본 사고 수준 ("off"|"low"|"medium"|"high"). 요청이 값을 주지 않으면 이게 쓰인다.
    thinking_level: str
    # 하위호환 파생값: thinking_level != "off"
    thinking: bool
    model_len: int

    async def chat(
        self,
        messages: list,
        *,
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
        thinking_level: str | None = None,
    ) -> tuple[str, dict]: ...

    async def llm(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> str: ...

    async def stream_chat(
        self,
        messages: list,
        *,
        temperature: float,
        max_tokens: int,
        thinking_level: str | None = None,
    ) -> AsyncGenerator[dict, None]: ...

    def needs_thinking_headroom(self, thinking_level: str | None = None) -> bool:
        """이 요청이 reasoning 토큰을 소비하므로 max_tokens 상향이 필요한가.

        보통은 `effective level != "off"` 이지만, OpenAI 추론 모델은 `off` 여도
        reasoning 을 끌 수 없어 항상 True 다.
        """
        ...

    async def health_check(self) -> bool: ...

    async def health_status(self) -> HealthReport: ...

    async def list_models(self) -> list[dict] | None: ...

    async def fetch_model_len(self) -> int: ...

    async def close(self) -> None: ...
