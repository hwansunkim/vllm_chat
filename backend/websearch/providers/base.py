from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schemas import SearchResult


@runtime_checkable
class SearchProvider(Protocol):
    """검색 백엔드 어댑터 인터페이스.

    구현체는 네트워크 I/O + 결과 정규화만 담당한다. 프롬프트 포맷팅 금지.
    검색 실패(429/차단/파싱 실패)는 예외를 raise 하고, service 계층이 잡는다.
    """

    async def search(self, query: str, k: int) -> list[SearchResult]:
        ...

    async def close(self) -> None:
        ...
