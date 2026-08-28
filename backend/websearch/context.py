from __future__ import annotations

from .schemas import SearchResult

_HEADER = (
    "다음은 사용자 질문과 관련된 웹 검색 결과입니다. "
    "답변에 활용하고, 인용 시 [번호] 형식으로 출처를 표기하세요."
)


def format_search_context(results: list[SearchResult]) -> str:
    """SearchResult[] → LLM system 프롬프트에 넣을 텍스트 블록. 순수 함수(네트워크 호출 금지)."""
    if not results:
        return ""
    lines = [
        f"[{i}] {r.title}\n{r.snippet}\n출처: {r.url}"
        for i, r in enumerate(results, 1)
    ]
    return _HEADER + "\n\n" + "\n\n".join(lines)
