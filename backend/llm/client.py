from __future__ import annotations

import sqlite3

from .. import config
from .registry import get_registry


async def setup(conn: sqlite3.Connection) -> None:
    await get_registry().load_from_db(conn)


async def teardown() -> None:
    await get_registry().close_all()


async def async_llm(prompt: str, max_tokens: int = 512, temperature: float = 0.1) -> str:
    provider = get_registry().select()
    return await provider.llm(prompt, max_tokens=max_tokens, temperature=temperature)


async def async_stream_chat(
    messages: list,
    *,
    temperature: float = 0.7,
    max_tokens: int = config.MAX_COMPLETION_TOKENS,
    model: str | None = None,
    server_id: str | None = None,
    thinking: bool = False,
):
    provider = get_registry().select(model=model, server_id=server_id)
    async for event in provider.stream_chat(
        messages, temperature=temperature, max_tokens=max_tokens, thinking=thinking
    ):
        if event["type"] == "usage":
            event["data"]["max_model_len"] = provider.model_len
        yield event


async def async_chat(
    messages: list,
    *,
    temperature: float = 0.7,
    max_tokens: int = config.MAX_COMPLETION_TOKENS,
    timeout: float | None = None,
    model: str | None = None,
    server_id: str | None = None,
) -> tuple[str, dict]:
    """비스트리밍 멀티턴 호출. 반환 usage 에는 "thinking" 키가 포함된다."""
    provider = get_registry().select(model=model, server_id=server_id)
    return await provider.chat(
        messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout
    )


async def async_get_model_context_limit() -> int:
    provider = get_registry().get_default()
    if provider is None:
        return 0
    return await provider.fetch_model_len()
