from __future__ import annotations

from llm.client import _parse_json, async_llm


async def async_extract_keywords(text: str) -> list[str]:
    prompt = f"""\
다음 텍스트에서 핵심 키워드를 추출하세요.
명사, 고유명사, 기술 용어 위주로 최대 7개 이내로 추출합니다.
JSON 배열로만 응답하세요. 다른 텍스트 없이 JSON만 출력하세요.

텍스트: {text}

예시: ["서버", "포트", "모델이름"]"""

    try:
        result = _parse_json(await async_llm(prompt), array=True)
        return result if isinstance(result, list) else []
    except Exception:
        return []


async def async_extract_memories_from_turns(turns: list[dict]) -> list[dict]:
    conversation = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in turns
    )
    prompt = f"""\
다음 대화에서 나중에 참조할 만한 새로운 정보를 추출하세요.

[대화]
{conversation}

type 종류:
- fact: 사실, 수치, 설정값, 고유명사
- decision: 결정된 사항
- pending: 미결 또는 진행 중인 항목

keywords는 이 항목을 나중에 검색할 때 쓸 핵심 단어 (최대 5개).
새로운 정보가 없으면 빈 배열 []을 반환하세요.

JSON 배열로만 응답하세요:
[
  {{"type": "fact", "content": "...", "keywords": ["...", "..."]}},
  {{"type": "decision", "content": "...", "keywords": ["...", "..."]}}
]"""

    try:
        result = _parse_json(await async_llm(prompt, max_tokens=1024), array=True)
        return result if isinstance(result, list) else []
    except Exception:
        return []


def build_messages(
    system_prompt: str,
    retrieved: list[dict],
    recent_turns: list,
) -> list:
    parts = [system_prompt] if system_prompt else []

    if retrieved:
        mem_lines = "\n".join(f"[{m['type']}] {m['content']}" for m in retrieved)
        parts.append(f"[관련 메모리]\n{mem_lines}")

    msgs = []
    if parts:
        msgs.append({"role": "system", "content": "\n\n".join(parts)})
    msgs.extend(recent_turns)
    return msgs
