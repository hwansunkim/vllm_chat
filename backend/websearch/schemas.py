from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SearchResult:
    """검색 결과 한 건. provider 계층에서 정규화되어 나온다."""

    title: str
    url: str
    snippet: str
