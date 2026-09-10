from __future__ import annotations

from .client import async_llm
from .utils import parse_json


async def async_extract_keywords(text: str) -> list[str]:
    prompt = f"""\
다음 텍스트에서 핵심 키워드를 추출하세요.
명사, 고유명사, 기술 용어 위주로 최대 7개 이내로 추출합니다.
JSON 배열로만 응답하세요. 다른 텍스트 없이 JSON만 출력하세요.

텍스트: {text}

예시: ["서버", "포트", "모델이름"]"""

    try:
        result = parse_json(await async_llm(prompt), array=True)
        return result if isinstance(result, list) else []
    except Exception:
        return []


class MemoryExtractionError(RuntimeError):
    """메모리 추출 LLM 호출 또는 응답 파싱이 실패했음을 알린다.

    "새 정보 없음"(정상적인 빈 결과)과 반드시 구분해야 한다 — 호출부가 이 예외를
    보면 아카이브를 보류하고 원문을 활성 상태로 남겨 다음 기회에 재시도한다.
    실패를 조용히 `[]` 로 삼키면 원문이 아카이브되면서 그 맥락이 이후 LLM 입력·
    RAG 검색에서 영영 빠진다(장애가 기억 손실처럼 보인다).
    """


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
        raw = await async_llm(prompt, max_tokens=1024)
    except Exception as e:  # LLM 서버 다운·타임아웃 등 — 조용히 삼키지 않는다.
        raise MemoryExtractionError(f"메모리 추출 LLM 호출 실패: {e}") from e

    result = parse_json(raw, array=True)
    if not isinstance(result, list):
        # 모델이 배열이 아닌 것을 반환 — 형식 위반이므로 실패로 취급.
        raise MemoryExtractionError(f"메모리 추출 응답이 JSON 배열이 아닙니다: {raw[:200]!r}")
    if not result and "[" in raw and "]" not in raw:
        # 열린 대괄호만 있고 닫히지 않음 = max_tokens 로 잘린 응답. parse_json 은
        # 이때 조용히 []( fallback )를 주므로 여기서 잡아야 한다.
        raise MemoryExtractionError(f"메모리 추출 응답이 불완전합니다(잘림): {raw[:200]!r}")
    return result


def build_messages(
    system_prompt: str,
    retrieved: list[dict],
    recent_turns: list,
    web_context: str = "",
) -> list:
    parts = [system_prompt] if system_prompt else []

    if retrieved:
        mem_lines = "\n".join(f"[{m['type']}] {m['content']}" for m in retrieved)
        parts.append(f"[관련 메모리]\n{mem_lines}")

    if web_context:
        parts.append(web_context)

    msgs = []
    if parts:
        msgs.append({"role": "system", "content": "\n\n".join(parts)})
    msgs.extend(recent_turns)
    return msgs
