from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator

import httpx

import config

logger = logging.getLogger(__name__)


def _extract_reply(message: dict) -> tuple[str, str]:
    """(answer, thinking) 반환.

    방식 A: reasoning_content 별도 필드 (vLLM 0.6+, Qwen3 등)
    방식 B: content 안의 <think>...</think> 태그 (구버전/일부 모델)
    두 방식 모두 자동 감지한다.
    """
    thinking = (message.get("reasoning_content") or "").strip()
    content  = (message.get("content") or "").strip()

    if not thinking:
        m = re.match(r"<think>(.*?)</think>\s*(.*)", content, re.DOTALL)
        if m:
            thinking = m.group(1).strip()
            content  = m.group(2).strip()

    return content, thinking


class VLLMProvider:
    def __init__(
        self,
        server_id: str,
        name: str,
        base_url: str,
        model: str,
        *,
        enabled: bool = True,
        is_default: bool = False,
        thinking: bool = False,
    ) -> None:
        self.id = server_id
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enabled = enabled
        self.is_default = is_default
        self.thinking = thinking
        self.model_len: int = 0
        # thinking 모드는 추론 토큰이 수백~수천 개이므로 타임아웃을 길게 설정
        timeout = httpx.Timeout(300.0) if thinking else httpx.Timeout(60.0)
        self._client = httpx.AsyncClient(timeout=timeout)

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────────────

    def _chat_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    # ── LLMProvider 인터페이스 ────────────────────────────────────────────────

    async def llm(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> str:
        response = await self._client.post(
            self._chat_url(),
            headers={"Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        response.raise_for_status()
        content, _ = _extract_reply(response.json()["choices"][0]["message"])
        return content

    async def chat(
        self,
        messages: list,
        *,
        temperature: float = 0.7,
        max_tokens: int = config.MAX_COMPLETION_TOKENS,
    ) -> tuple[str, dict]:
        full_reply = ""
        full_thinking = ""
        total_completion_tokens = 0
        final_usage: dict = {}
        current_messages = list(messages)

        for _ in range(config.MAX_CONTINUATION_ROUNDS):
            try:
                response = await self._client.post(
                    self._chat_url(),
                    headers={"Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": current_messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                body = e.response.text[:500]
                logger.error("[%s] HTTP %s: %s", self.name, e.response.status_code, body)
                raise RuntimeError(
                    f"HTTP {e.response.status_code} from {self.name}: {body}"
                ) from e
            except httpx.TimeoutException as e:
                logger.error("[%s] 타임아웃 (%ss): %s", self.name, type(e).__name__, e)
                raise RuntimeError(
                    f"타임아웃 — {self.name} 서버가 {type(e).__name__}를 발생시켰습니다"
                ) from e
            except httpx.HTTPError as e:
                logger.error("[%s] 연결 오류 %s: %s", self.name, type(e).__name__, e)
                raise RuntimeError(
                    f"연결 오류 [{type(e).__name__}]: {e or '(서버가 응답 없이 연결을 종료)'}"
                ) from e

            try:
                data = response.json()
            except Exception as e:
                preview = response.text[:200]
                logger.error("[%s] JSON 파싱 실패. 응답: %r", self.name, preview)
                raise RuntimeError(
                    f"JSON 파싱 실패 — 서버가 비표준 응답을 반환했습니다: {preview!r}"
                ) from e

            if "choices" not in data or not data["choices"]:
                logger.error("[%s] 응답에 choices 없음: %s", self.name, data)
                raise RuntimeError(f"응답에 choices 없음: {data}")

            choice = data["choices"][0]
            if "message" not in choice:
                logger.error("[%s] choice에 message 없음: %s", self.name, choice)
                raise RuntimeError(f"choice에 message 없음 (스트리밍 응답인지 확인): {choice}")

            partial, thinking = _extract_reply(choice["message"])
            finish_reason: str = choice.get("finish_reason", "stop")
            usage: dict = data.get("usage", {})

            full_reply    += partial
            full_thinking += thinking
            total_completion_tokens += usage.get("completion_tokens", 0)
            final_usage = usage

            if finish_reason != "length":
                break

            # thinking은 assistant 메시지에서 제외해야 연속 생성이 안전함
            current_messages = current_messages + [
                {"role": "assistant", "content": partial},
                {"role": "user", "content": config.CONTINUE_PROMPT},
            ]

        merged = dict(final_usage)
        merged["completion_tokens"] = total_completion_tokens
        merged["thinking"] = full_thinking
        return full_reply, merged

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get(
                f"{self.base_url}/health",
                timeout=httpx.Timeout(5.0),
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def fetch_model_len(self) -> int:
        try:
            response = await self._client.get(
                f"{self.base_url}/v1/models",
                timeout=httpx.Timeout(5.0),
            )
            for m in response.json().get("data", []):
                if m["id"] == self.model:
                    self.model_len = m.get("max_model_len", 0)
                    return self.model_len
        except Exception:
            pass
        return 0

    async def stream_chat(
        self,
        messages: list,
        *,
        temperature: float = 0.7,
        max_tokens: int = config.MAX_COMPLETION_TOKENS,
    ) -> AsyncGenerator[dict, None]:
        """SSE 스트리밍. thinking/answer/usage 딕셔너리를 순서대로 yield한다.

        방식 A — reasoning_content 필드 (vLLM 0.6+): 필드가 있으면 그대로 분리.
        방식 B — <think> 태그 (mlx_vlm): 상태 머신으로 content 스트림에서 실시간 감지.
        """
        full_thinking = ""
        full_answer   = ""
        usage: dict   = {}

        # <think> 태그 상태 머신
        state = "pre"   # "pre" | "thinking" | "answer"
        buf   = ""

        try:
            async with self._client.stream(
                "POST",
                self._chat_url(),
                headers={"Content-Type": "application/json"},
                json={
                    "model":       self.model,
                    "messages":    messages,
                    "max_tokens":  max_tokens,
                    "temperature": temperature,
                    "stream":      True,
                },
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    body = (await e.response.aread()).decode(errors="replace")[:500]
                    logger.error("[%s] stream HTTP %s: %s", self.name, e.response.status_code, body)
                    raise RuntimeError(f"HTTP {e.response.status_code}: {body}") from e

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        break
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if data.get("usage"):
                        usage = data["usage"]

                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    choice       = choices[0]
                    delta        = choice.get("delta") or {}
                    finish_reason = choice.get("finish_reason")

                    # ── 방식 A: reasoning_content 필드 ───────────────────────────
                    rc      = delta.get("reasoning_content") or ""
                    content = delta.get("content") or ""

                    if rc:
                        full_thinking += rc
                        yield {"type": "thinking", "chunk": rc}
                        if content:          # 같은 청크에 answer도 있을 때 (드묾)
                            full_answer += content
                            yield {"type": "answer", "chunk": content}
                        if finish_reason:
                            break
                        continue            # 방식 A면 상태 머신 생략

                    # ── 방식 B: <think> 태그 상태 머신 ─────────────────────────
                    if content:
                        buf += content

                        while buf:
                            if state == "pre":
                                if buf.startswith("<think>"):
                                    state = "thinking"
                                    buf   = buf[7:]
                                elif len(buf) >= 7 or finish_reason:
                                    # <think> 없음 → 전부 answer
                                    state = "answer"
                                    full_answer += buf
                                    yield {"type": "answer", "chunk": buf}
                                    buf = ""
                                else:
                                    break   # 태그 경계 대기

                            elif state == "thinking":
                                close = buf.find("</think>")
                                if close >= 0:
                                    chunk = buf[:close]
                                    if chunk:
                                        full_thinking += chunk
                                        yield {"type": "thinking", "chunk": chunk}
                                    state = "answer"
                                    buf   = buf[close + 8:].lstrip("\n")
                                else:
                                    # </think> 경계 보호: 마지막 8자 버퍼링
                                    safe = max(0, len(buf) - 8)
                                    if safe:
                                        chunk = buf[:safe]
                                        full_thinking += chunk
                                        yield {"type": "thinking", "chunk": chunk}
                                        buf = buf[safe:]
                                    if finish_reason:
                                        # EOF인데 </think> 없음 (비정상)
                                        full_thinking += buf
                                        yield {"type": "thinking", "chunk": buf}
                                        buf = ""
                                    break   # 더 많은 데이터 대기

                            elif state == "answer":
                                full_answer += buf
                                yield {"type": "answer", "chunk": buf}
                                buf = ""

                    if finish_reason:
                        break

                # 잔여 버퍼 처리
                if buf:
                    if state == "thinking":
                        full_thinking += buf
                        yield {"type": "thinking", "chunk": buf}
                    else:
                        full_answer += buf
                        yield {"type": "answer", "chunk": buf}

        except httpx.TimeoutException as e:
            logger.error("[%s] stream 타임아웃: %s", self.name, type(e).__name__)
            raise RuntimeError(f"타임아웃 [{type(e).__name__}]") from e
        except httpx.HTTPError as e:
            logger.error("[%s] stream 연결 오류 %s: %s", self.name, type(e).__name__, e)
            raise RuntimeError(
                f"연결 오류 [{type(e).__name__}]: {e or '(연결 종료)'}"
            ) from e

        yield {"type": "usage", "data": {**usage, "thinking": full_thinking, "answer": full_answer}}

    async def close(self) -> None:
        await self._client.aclose()
