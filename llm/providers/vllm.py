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


class _ContextLengthRetry(Exception):
    def __init__(self, reduced: int, model_len: int) -> None:
        self.reduced = reduced
        self.model_len = model_len  # 오류 메시지에서 추출한 실제 모델 한계


def _parse_context_error(body: str) -> tuple[int, int] | None:
    """컨텍스트 길이 초과 400 오류를 파싱해 (줄인_max_tokens, model_len)을 반환한다."""
    m = re.search(r'maximum context length is (\d+).*?prompt contains at least (\d+)', body, re.DOTALL)
    if not m:
        return None
    model_len = int(m.group(1))
    reduced = model_len - int(m.group(2))
    return (reduced, model_len) if reduced > 0 else None


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
        configured_max_len: int = 0,
    ) -> None:
        self.id = server_id
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enabled = enabled
        self.is_default = is_default
        self.thinking = thinking
        self.model_len: int = configured_max_len  # 설정값으로 초기화, API 조회 시 갱신
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
        effective_max = max_tokens
        _context_retried = False

        for _ in range(config.MAX_CONTINUATION_ROUNDS):
            try:
                response = await self._client.post(
                    self._chat_url(),
                    headers={"Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": current_messages,
                        "max_tokens": effective_max,
                        "temperature": temperature,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                body = e.response.text[:500]
                logger.error("[%s] HTTP %s: %s", self.name, e.response.status_code, body)
                if e.response.status_code == 400 and not _context_retried:
                    parsed = _parse_context_error(body)
                    if parsed is not None:
                        effective_max, detected_len = parsed
                        if detected_len and not self.model_len:
                            self.model_len = detected_len
                        _context_retried = True
                        logger.info("[%s] context limit (model_len=%d), retrying with max_tokens=%d",
                                    self.name, detected_len, effective_max)
                        continue
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
                    api_len = m.get("max_model_len", 0)
                    if api_len:
                        self.model_len = api_len  # API 값이 있으면 설정값 덮어씀
                    return self.model_len  # 없으면 설정값 유지
        except Exception:
            pass
        return self.model_len  # API 실패 시 설정값 반환

    async def stream_chat(
        self,
        messages: list,
        *,
        temperature: float = 0.7,
        max_tokens: int = config.MAX_COMPLETION_TOKENS,
        thinking: bool = False,
    ) -> AsyncGenerator[dict, None]:
        """SSE 스트리밍. thinking/answer/usage 딕셔너리를 순서대로 yield한다.

        방식 A — reasoning_content 필드 (vLLM 0.6+): 필드가 있으면 그대로 분리.
        방식 B — <think> 태그 (mlx_vlm): 상태 머신으로 content 스트림에서 실시간 감지.
        finish_reason == "length"이면 MAX_CONTINUATION_ROUNDS까지 자동 연속 생성한다.
        컨텍스트 초과(400) 시 max_tokens를 자동으로 줄여 1회 재시도한다.
        """
        effective_max    = max_tokens
        current_messages = list(messages)
        full_thinking    = ""
        full_answer      = ""
        total_completion_tokens = 0
        final_usage: dict = {}
        _context_retried  = False

        # <think> 태그 상태 머신 — 라운드 간 유지
        think_state = "pre"   # "pre" | "thinking" | "answer"

        for _round in range(config.MAX_CONTINUATION_ROUNDS):
            round_answer      = ""
            usage: dict       = {}
            finish_reason_end = "stop"
            buf               = ""

            for _attempt in range(2):
                got_context_error = False
                try:
                    async with self._client.stream(
                        "POST",
                        self._chat_url(),
                        headers={"Content-Type": "application/json"},
                        json={
                            "model":          self.model,
                            "messages":       current_messages,
                            "max_tokens":     effective_max,
                            "temperature":    temperature,
                            "stream":         True,
                            "stream_options": {"include_usage": True},
                            **({"chat_template_kwargs": {"enable_thinking": True}} if thinking else {}),
                        },
                    ) as response:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as e:
                            body = (await e.response.aread()).decode(errors="replace")[:500]
                            logger.error("[%s] stream HTTP %s: %s", self.name, e.response.status_code, body)
                            if e.response.status_code == 400 and not _context_retried:
                                parsed = _parse_context_error(body)
                                if parsed is not None:
                                    reduced, model_len = parsed
                                    effective_max = reduced
                                    if model_len and not self.model_len:
                                        self.model_len = model_len
                                    _context_retried = True
                                    logger.info("[%s] context limit (model_len=%d), retrying with max_tokens=%d",
                                                self.name, model_len, effective_max)
                                    got_context_error = True
                                    break  # inner attempt loop → retry
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
                            choice        = choices[0]
                            delta         = choice.get("delta") or {}
                            finish_reason = choice.get("finish_reason")
                            if finish_reason:
                                finish_reason_end = finish_reason

                            # ── 방식 A: reasoning_content 필드 ───────────────────────────
                            rc      = delta.get("reasoning_content") or ""
                            content = delta.get("content") or ""

                            if rc:
                                full_thinking += rc
                                yield {"type": "thinking", "chunk": rc}
                                if content:
                                    full_answer  += content
                                    round_answer += content
                                    yield {"type": "answer", "chunk": content}
                                continue  # 방식 A면 상태 머신 생략

                            # ── 방식 B: <think> 태그 상태 머신 ─────────────────────────
                            if content:
                                buf += content

                                while buf:
                                    if think_state == "pre":
                                        if buf.startswith("<think>"):
                                            think_state = "thinking"
                                            buf         = buf[7:]
                                        elif len(buf) >= 7 or finish_reason:
                                            think_state  = "answer"
                                            full_answer  += buf
                                            round_answer += buf
                                            yield {"type": "answer", "chunk": buf}
                                            buf = ""
                                        else:
                                            break  # 태그 경계 대기

                                    elif think_state == "thinking":
                                        close = buf.find("</think>")
                                        if close >= 0:
                                            chunk = buf[:close]
                                            if chunk:
                                                full_thinking += chunk
                                                yield {"type": "thinking", "chunk": chunk}
                                            think_state = "answer"
                                            buf         = buf[close + 8:].lstrip("\n")
                                        else:
                                            safe = max(0, len(buf) - 8)
                                            if safe:
                                                chunk = buf[:safe]
                                                full_thinking += chunk
                                                yield {"type": "thinking", "chunk": chunk}
                                                buf = buf[safe:]
                                            if finish_reason:
                                                full_thinking += buf
                                                yield {"type": "thinking", "chunk": buf}
                                                buf = ""
                                            break

                                    elif think_state == "answer":
                                        full_answer  += buf
                                        round_answer += buf
                                        yield {"type": "answer", "chunk": buf}
                                        buf = ""

                        # 잔여 버퍼 처리
                        if buf:
                            if think_state == "thinking":
                                full_thinking += buf
                                yield {"type": "thinking", "chunk": buf}
                            else:
                                full_answer  += buf
                                round_answer += buf
                                yield {"type": "answer", "chunk": buf}

                except httpx.TimeoutException as e:
                    logger.error("[%s] stream 타임아웃: %s", self.name, type(e).__name__)
                    raise RuntimeError(f"타임아웃 [{type(e).__name__}]") from e
                except httpx.HTTPError as e:
                    logger.error("[%s] stream 연결 오류 %s: %s", self.name, type(e).__name__, e)
                    raise RuntimeError(
                        f"연결 오류 [{type(e).__name__}]: {e or '(연결 종료)'}"
                    ) from e

                if got_context_error:
                    continue  # context 오류 재시도
                break  # 스트리밍 성공

            total_completion_tokens += usage.get("completion_tokens", 0)
            final_usage = usage

            if finish_reason_end != "length":
                break  # 정상 종료 또는 stop

            # ── 자동 연속 생성 ────────────────────────────────────────────────
            logger.info("[%s] finish_reason=length, 연속 생성 round %d/%d",
                        self.name, _round + 1, config.MAX_CONTINUATION_ROUNDS)
            current_messages = current_messages + [
                {"role": "assistant", "content": round_answer},
                {"role": "user",      "content": config.CONTINUE_PROMPT},
            ]

        merged = dict(final_usage)
        merged["completion_tokens"] = total_completion_tokens
        merged["thinking"]          = full_thinking
        merged["answer"]            = full_answer
        yield {"type": "usage", "data": merged}

    async def close(self) -> None:
        await self._client.aclose()
