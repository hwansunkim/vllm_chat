from __future__ import annotations

import logging

from ..llm.client import async_llm

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 200


async def async_build_search_query(user_msg: str, recent_turns: list[dict]) -> str:
    """대화 맥락을 반영해 검색 엔진에 넣을 짧은 질의를 만든다.

    실패 시 user_msg 를 그대로 반환한다 (검색이 답변을 막지 않는다).
    """
    user_msg = (user_msg or "").strip()
    if not user_msg:
        return user_msg

    context_lines = []
    for turn in (recent_turns or [])[-3:]:
        role = turn.get("role", "")
        content = (turn.get("content") or "").strip().replace("\n", " ")
        if content:
            context_lines.append(f"{role}: {content[:300]}")
    context_block = "\n".join(context_lines) if context_lines else "(이전 대화 없음)"

    prompt = f"""\
아래 대화 맥락과 사용자의 마지막 메시지를 보고, 검색 엔진에 넣을 검색어 한 줄을 만드세요.
- 대화체 표현·군더더기는 빼고 핵심 키워드 위주로.
- 지시대명사("그거", "이 회사")는 맥락을 보고 실제 대상으로 치환.
- 따옴표·접두어 없이 검색어만 한 줄로 출력.

[이전 대화]
{context_block}

[사용자 마지막 메시지]
{user_msg}

검색어:"""

    try:
        raw = await async_llm(prompt, max_tokens=60, temperature=0.0)
        query = (raw or "").strip().splitlines()[0].strip().strip('"').strip()
        if query.startswith("검색어:"):
            query = query[len("검색어:"):].strip()
        return query[:_MAX_QUERY_CHARS] if query else user_msg
    except Exception as e:
        logger.warning("async_build_search_query 실패, user_msg 폴백: %s", e)
        return user_msg
