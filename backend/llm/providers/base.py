from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

HealthStatus = Literal["ok", "model_missing", "unreachable"]


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
    thinking: bool
    model_len: int

    async def chat(
        self,
        messages: list,
        *,
        temperature: float,
        max_tokens: int,
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
        thinking: bool = False,
    ) -> AsyncGenerator[dict, None]: ...

    async def health_check(self) -> bool: ...

    async def health_status(self) -> HealthReport: ...

    async def fetch_model_len(self) -> int: ...

    async def close(self) -> None: ...
