from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    id: str
    name: str
    base_url: str
    model: str
    enabled: bool
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

    async def health_check(self) -> bool: ...

    async def fetch_model_len(self) -> int: ...

    async def close(self) -> None: ...
