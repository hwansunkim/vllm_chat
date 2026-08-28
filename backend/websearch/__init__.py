"""웹 검색 모듈.

레이어 분리:
  - service / providers : 네트워크 I/O + 결과 정규화 (프롬프트 포맷팅 금지)
  - context             : SearchResult[] → 프롬프트 텍스트 (순수 함수, 네트워크 금지)
  - query               : 대화체 메시지 → 검색어 (LLM 호출)
"""
from __future__ import annotations

from .context import format_search_context
from .query import async_build_search_query
from .schemas import SearchResult
from .service import web_search

__all__ = [
    "web_search",
    "format_search_context",
    "async_build_search_query",
    "SearchResult",
]
