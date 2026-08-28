from __future__ import annotations

import logging

from .. import config
from .providers.base import SearchProvider
from .providers.duckduckgo import DuckDuckGoProvider
from .schemas import SearchResult

logger = logging.getLogger(__name__)

_provider: SearchProvider | None = None


def get_provider() -> SearchProvider:
    """config.WEB_SEARCH_PROVIDER 로 분기해 provider 싱글턴을 반환한다.

    새 백엔드(tavily/searxng 등)를 추가하려면 providers/ 에 어댑터를 만들고
    여기에 분기 한 줄만 넣으면 된다.
    """
    global _provider
    if _provider is None:
        name = config.WEB_SEARCH_PROVIDER
        if name == "duckduckgo":
            _provider = DuckDuckGoProvider()
        else:
            raise ValueError(f"알 수 없는 WEB_SEARCH_PROVIDER: {name!r}")
    return _provider


async def web_search(query: str) -> list[SearchResult]:
    """검색 실행. 실패 시 빈 리스트 반환 (답변 생성을 절대 막지 않는다)."""
    try:
        provider = get_provider()
        return await provider.search(query, config.WEB_SEARCH_MAX_RESULTS)
    except Exception as e:
        logger.warning("web_search 실패 (query=%r): %s", query, e)
        return []


async def close_provider() -> None:
    global _provider
    if _provider is not None:
        try:
            await _provider.close()
        finally:
            _provider = None
