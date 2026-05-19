from __future__ import annotations

import sqlite3

import config
from llm.registry import get_registry


# ── 앱 생명주기 ───────────────────────────────────────────────────────────────

async def setup(conn: sqlite3.Connection) -> None:
    """앱 시작 시 DB에서 서버 목록을 로드해 레지스트리를 초기화한다."""
    await get_registry().load_from_db(conn)


async def teardown() -> None:
    """앱 종료 시 모든 서버의 HTTP 클라이언트를 닫는다."""
    await get_registry().close_all()


# ── LLM 호출 퍼블릭 API ───────────────────────────────────────────────────────

async def async_llm(prompt: str, max_tokens: int = 512, temperature: float = 0.1) -> str:
    """내부 파이프라인용 단순 LLM 호출. 기본 서버를 사용한다."""
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
    """스트리밍 대화 호출. thinking/answer/usage 딕셔너리를 yield한다."""
    provider = get_registry().select(model=model, server_id=server_id)
    async for event in provider.stream_chat(
        messages, temperature=temperature, max_tokens=max_tokens, thinking=thinking
    ):
        if event["type"] == "usage":
            event["data"]["max_model_len"] = provider.model_len
        yield event


async def async_get_model_context_limit() -> int:
    """기본 서버의 컨텍스트 길이를 조회하고 provider에 캐시한다."""
    provider = get_registry().get_default()
    if provider is None:
        return 0
    return await provider.fetch_model_len()
