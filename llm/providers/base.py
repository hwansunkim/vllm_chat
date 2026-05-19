from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol, runtime_checkable


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

    async def fetch_model_len(self) -> int: ...

    async def close(self) -> None: ...
