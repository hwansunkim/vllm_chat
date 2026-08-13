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
