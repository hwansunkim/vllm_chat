"""ABM 이 사용하는 LLM 호출 계약.

ABM 은 LLM 전송 방식을 직접 알지 않는다. Simulation 생성자에 아래 형태의
콜러블을 주입받고 그것만 호출한다. 실제 구현은 backend/llm/bridge.py 의
make_sync_chat() 이 만들며, backend/llm/providers/* 계층으로 위임한다.
"""
from __future__ import annotations

from collections.abc import Callable

# (messages, *, max_tokens) -> (content, reasoning, usage)
# usage 키: prompt_tokens / completion_tokens / total_tokens
LLMCall = Callable[..., tuple[str, str, dict]]


# ── 실패 진단 헬퍼 ─────────────────────────────────────────────────────────────
# LLM 출력이 파싱·검증에 실패했을 때 "원문·요청 한도·usage·종료 사유"를 한 줄
# 경고에 담기 위한 순수 함수들. 원문이 없으면 실패 원인(토큰 잘림 vs 형식 오류
# vs 타입 위반)을 사후에 구분할 수 없다(analysis/case1_v12 보고서).

RAW_PREVIEW_CHARS = 200


def preview_raw(text, limit: int = RAW_PREVIEW_CHARS) -> str:
    """원문 앞부분을 한 줄로 — 개행·탭을 이스케이프해 로그 한 줄이 깨지지 않게 한다.

    잘렸으면 끝에 ``…(+N자)`` 를 붙여 원문 전체 길이를 알 수 있게 한다.
    문자열이 아니면(None 등) ``repr`` 로 표기한다.
    """
    if not isinstance(text, str):
        return repr(text)
    head = text[:limit]
    head = head.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    rest = len(text) - limit
    return head + (f"…(+{rest}자)" if rest > 0 else "")


# usage 에서 진단에 쓸 키. 종료 사유 키(finish_reason/stop_reason)는 현재 provider
# 가 usage 에 싣지 않지만, 실리게 되면(provider 보강) 자동으로 로그에 나타난다.
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens",
               "finish_reason", "stop_reason")


def summarize_usage(usage) -> str:
    """usage dict → ``completion_tokens=256 prompt_tokens=…`` 형태의 요약.

    값이 있는 키만 싣는다. ``completion_tokens_details.reasoning_tokens``(OpenAI
    호환 경로)가 있으면 ``reasoning_tokens`` 로 함께 싣는다. 종료 사유가 없으면
    ``finish_reason=미제공`` 을 명시해 "정상 종료로 확인됨"과 구분한다.
    """
    if not isinstance(usage, dict) or not usage:
        return "usage=미제공 finish_reason=미제공"
    parts = [f"{k}={usage[k]}" for k in _USAGE_KEYS if usage.get(k) not in (None, "")]
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens"):
        parts.append(f"reasoning_tokens={details['reasoning_tokens']}")
    if not any(usage.get(k) for k in ("finish_reason", "stop_reason")):
        parts.append("finish_reason=미제공")
    return " ".join(parts) if parts else "usage=미제공"
