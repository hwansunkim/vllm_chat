"""워커 스레드 → 메인 이벤트 루프 브릿지.

ABM 시뮬레이션은 FastAPI 이벤트 루프 밖의 순수 OS 스레드에서 돈다.
provider 의 httpx.AsyncClient 는 lifespan() 이 실행된 메인 루프에 바인딩되므로,
워커 스레드에서 새 루프(asyncio.run)를 만들어 호출하면
"attached to a different loop" 로 깨진다. 반드시 원래 루프로 되돌려 실행한다.
"""
from __future__ import annotations

import asyncio
import logging

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .. import config, state
from . import client
from .providers.base import LLMHTTPError
from .registry import NoProviderError

logger = logging.getLogger(__name__)


class LoopNotReadyError(RuntimeError):
    """메인 이벤트 루프 참조가 없을 때(백엔드 lifespan 밖에서 호출)."""


# 재시도해도 결과가 달라지지 않는 영구 실패들.
_PERMANENT = (NoProviderError, LoopNotReadyError)


def run_sync(coro, *, timeout: float):
    """메인 이벤트 루프에서 코루틴을 실행하고 결과를 기다린다. 워커 스레드 전용.

    이벤트 루프 안에서 호출하면 그 루프를 블로킹해 데드락이 되므로 호출하지 말 것.
    """
    loop = state.event_loop
    if loop is None:
        coro.close()  # 스케줄되지 않은 코루틴 경고 방지
        raise LoopNotReadyError(
            "이벤트 루프가 설정되지 않았습니다 — 백엔드 lifespan 안에서만 호출할 수 있습니다."
        )
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        # 가드 타임아웃. 매달린 요청이 커넥션을 물고 있지 않도록 취소한다.
        future.cancel()
        raise


def _is_retryable(e: BaseException) -> bool:
    if isinstance(e, _PERMANENT):
        return False
    if isinstance(e, LLMHTTPError):
        # 400(파라미터 거부 등)은 재시도해도 같은 결과다.
        return e.status_code in (408, 429) or e.status_code >= 500
    # provider 는 연결 오류/타임아웃도 RuntimeError 로 정규화해 던진다.
    return isinstance(e, RuntimeError)


def make_sync_chat(
    *,
    server_id: str | None = None,
    timeout: int = 120,
    temperature: float = 0.7,
    thinking_level: str | None = None,
):
    """(messages, *, max_tokens) -> (content, reasoning, usage) 동기 콜러블을 만든다.

    ABM 처럼 이벤트 루프가 없는 스레드에서 provider 계층을 쓰기 위한 어댑터.
    provider 는 호출 시점마다 registry 에서 lazy select 되므로, 실행 중
    서버 설정이 바뀌어도 닫힌 클라이언트를 붙들지 않는다.

    `temperature` 는 이 콜러블이 내는 모든 요청에 고정으로 붙는다(기본 0.7 =
    기존 하드코딩 값). 샘플링 온도를 다르게 쓰려면 서로 다른 콜러블을 만든다.
    reasoning 계열 모델처럼 temperature 를 받지 않는 provider 는 각자
    `_temperature_body()` 에서 이 값을 무시한다.

    `thinking_level` 은 기본이 None 이며, 이는 "선택된 서버의 기본 사고 수준을
    그대로 상속"을 뜻한다. 시뮬레이션에는 별도 사고 UI 가 없고 서버 설정을
    공유한다는 계약이 여기서 성립한다 — ABM 코드는 이 값을 알 필요가 없다.
    """
    # provider.chat() 은 finish_reason=="length" 일 때 최대 MAX_CONTINUATION_ROUNDS 회
    # 연속 생성하므로, 스레드 쪽 가드는 per-request 타임아웃의 그 배수보다 커야 한다.
    guard = timeout * config.MAX_CONTINUATION_ROUNDS + 30

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable),
        reraise=True,  # RetryError 로 감싸지 말고 원본 예외를 그대로 올린다
    )
    def _chat(messages: list, *, max_tokens: int = 16384) -> tuple[str, str, dict]:
        content, usage = run_sync(
            client.async_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                server_id=server_id,
                thinking_level=thinking_level,
            ),
            timeout=guard,
        )
        return content, usage.pop("thinking", ""), usage

    return _chat
