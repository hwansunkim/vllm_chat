from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator

import httpx

from ... import config
from .base import HealthReport, LLMHTTPError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
THINKING_BUDGET_TOKENS = 10000
# Claude 계열은 API 로 컨텍스트 길이를 노출하지 않는다. 설정값이 없을 때의 보수적 기본치.
DEFAULT_CONTEXT_LEN = 200_000


def _split_system(messages: list) -> tuple[str, list[dict]]:
    """role=="system" 을 top-level system 으로 분리하고 messages 를 Anthropic 규격에 맞춘다.

    Anthropic Messages API 제약:
      - messages 배열에 system 롤을 허용하지 않는다 → 뽑아서 개행으로 병합
      - user/assistant 가 반드시 교대해야 한다 → 연속 같은 role 은 하나로 합침
      - 첫 메시지는 user 여야 한다 → 앞쪽 assistant 는 버림
      - 빈 content 를 거부한다 → 제거

    연속 role 병합은 특히 중요하다. 스트림이 한 번 실패하면 conversations.py 가
    assistant 턴을 저장하지 않아 turns 에 짝 없는 user 턴이 남고, 다음 요청이
    [user, assistant, user, user] 형태가 된다. 병합하지 않으면 그 대화는
    "roles must alternate" 400 으로 영구히 복구 불가능해진다.
    """
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue
        if not content:
            continue  # Anthropic 은 빈 content 를 거부한다
        role = "assistant" if role == "assistant" else "user"
        if rest and rest[-1]["role"] == role:
            rest[-1]["content"] = f"{rest[-1]['content']}\n\n{content}"
        else:
            rest.append({"role": role, "content": content})

    # 첫 메시지는 반드시 user 여야 한다
    while rest and rest[0]["role"] != "user":
        rest.pop(0)

    return "\n\n".join(system_parts), rest


class AnthropicProvider:
    """Anthropic Claude Messages API 프로바이더.

    OpenAI 호환이 아니므로 OpenAICompatibleProvider 를 상속하지 않고
    LLMProvider Protocol 을 직접 구현한다.
    """

    def __init__(
        self,
        server_id: str,
        name: str,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        enabled: bool = True,
        is_default: bool = False,
        thinking: bool = False,
        configured_max_len: int = 0,
    ) -> None:
        self.id         = server_id
        self.name       = name
        self.base_url   = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model      = model
        self.enabled    = enabled
        self.is_default = is_default
        self.thinking   = thinking
        self.model_len: int = configured_max_len or DEFAULT_CONTEXT_LEN
        timeout = httpx.Timeout(300.0) if thinking else httpx.Timeout(120.0)
        self._client = httpx.AsyncClient(timeout=timeout)
        effective_key = api_key or getattr(config, "ANTHROPIC_API_KEY", "")
        self._headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if effective_key:
            self._headers["x-api-key"] = effective_key

    # ── URL ──────────────────────────────────────────────────────────

    def _api_base(self) -> str:
        return self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

    def _messages_url(self) -> str:
        return f"{self._api_base()}/messages"

    def _models_url(self) -> str:
        return f"{self._api_base()}/models"

    # ── 요청 바디 ─────────────────────────────────────────────────────

    def _build_body(
        self,
        messages: list,
        *,
        temperature: float,
        max_tokens: int,
        thinking: bool,
        stream: bool,
    ) -> dict:
        system, converted = _split_system(messages)
        use_thinking = bool(self.thinking and thinking)

        # extended thinking 은 max_tokens > budget_tokens 를 요구하고,
        # temperature 는 1 이외의 값을 허용하지 않는다.
        if use_thinking and max_tokens <= THINKING_BUDGET_TOKENS:
            max_tokens = THINKING_BUDGET_TOKENS + config.MAX_COMPLETION_TOKENS

        body: dict = {
            "model":      self.model,
            "messages":   converted,
            "max_tokens": max_tokens,
        }
        if system:
            body["system"] = system
        if use_thinking:
            body["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS}
            body["temperature"] = 1
        else:
            body["temperature"] = temperature
        if stream:
            body["stream"] = True
        return body

    @staticmethod
    def _usage(raw: dict) -> dict:
        return {
            "prompt_tokens":     raw.get("input_tokens", 0),
            "completion_tokens": raw.get("output_tokens", 0),
        }

    def _raise_http(self, status: int, body: str) -> None:
        logger.error("[%s] Anthropic HTTP %s: %s", self.name, status, body)
        raise LLMHTTPError(status, f"HTTP {status} from {self.name}: {body}")

    # ── 호출 ─────────────────────────────────────────────────────────

    async def llm(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> str:
        answer, _ = await self.chat(
            [{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return answer

    async def chat(
        self,
        messages: list,
        *,
        temperature: float = 0.7,
        max_tokens: int = config.MAX_COMPLETION_TOKENS,
        timeout: float | None = None,
    ) -> tuple[str, dict]:
        body = self._build_body(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            # 비스트리밍 chat()/llm() 은 메모리 키워드 추출·에이전트 라우팅 같은
            # 내부 유틸 호출 경로다. extended thinking 을 켜면 temperature 가 1 로
            # 강제되고 max_tokens 가 14k 로 부풀어 짧고 결정적인 출력이 오염된다.
            # (OpenAICompatibleProvider.chat() 도 _thinking_body() 를 쓰지 않는다)
            thinking=False,
            stream=False,
        )
        try:
            response = await self._client.post(
                self._messages_url(),
                headers=self._headers,
                json=body,
                # 미지정 시 생성자에서 설정한 클라이언트 기본값(120s/300s)을 유지한다.
                timeout=httpx.Timeout(timeout) if timeout else httpx.USE_CLIENT_DEFAULT,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            self._raise_http(e.response.status_code, e.response.text[:500])
        except httpx.TimeoutException as e:
            raise RuntimeError(f"타임아웃 — {self.name}: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise RuntimeError(f"연결 오류 [{type(e).__name__}]: {e or '(연결 종료)'}") from e

        try:
            data = response.json()
        except Exception as e:
            raise RuntimeError(f"JSON 파싱 실패: {response.text[:200]!r}") from e

        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise RuntimeError(f"응답에 content 배열 없음: {data}")

        answer_parts:   list[str] = []
        thinking_parts: list[str] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                answer_parts.append(b.get("text") or "")
            elif b.get("type") == "thinking":
                thinking_parts.append(b.get("thinking") or "")

        usage = self._usage(data.get("usage") or {})
        usage["thinking"] = "".join(thinking_parts).strip()
        return "".join(answer_parts).strip(), usage

    async def stream_chat(
        self,
        messages: list,
        *,
        temperature: float = 0.7,
        max_tokens: int = config.MAX_COMPLETION_TOKENS,
        thinking: bool = False,
    ) -> AsyncGenerator[dict, None]:
        body = self._build_body(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=thinking,
            stream=True,
        )

        full_answer   = ""
        full_thinking = ""
        usage: dict   = {"prompt_tokens": 0, "completion_tokens": 0}

        try:
            async with self._client.stream(
                "POST", self._messages_url(), headers=self._headers, json=body
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    raw = (await e.response.aread()).decode(errors="replace")[:500]
                    self._raise_http(e.response.status_code, raw)

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue  # "event: ..." 줄은 JSON 의 type 필드로 대체 가능
                    raw = line[6:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    etype = evt.get("type")

                    if etype == "message_start":
                        u = (evt.get("message") or {}).get("usage") or {}
                        usage.update(self._usage(u))

                    elif etype == "content_block_delta":
                        delta = evt.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            chunk = delta.get("text") or ""
                            if chunk:
                                full_answer += chunk
                                yield {"type": "answer", "chunk": chunk}
                        elif dtype == "thinking_delta":
                            chunk = delta.get("thinking") or ""
                            if chunk:
                                full_thinking += chunk
                                yield {"type": "thinking", "chunk": chunk}

                    elif etype == "message_delta":
                        u = evt.get("usage") or {}
                        if u.get("output_tokens") is not None:
                            usage["completion_tokens"] = u["output_tokens"]
                        if u.get("input_tokens") is not None:
                            usage["prompt_tokens"] = u["input_tokens"]

                    elif etype == "error":
                        err = evt.get("error") or {}
                        raise RuntimeError(
                            f"Anthropic 오류 [{err.get('type', 'unknown')}]: {err.get('message', '')}"
                        )

                    elif etype == "message_stop":
                        break

        except httpx.TimeoutException as e:
            raise RuntimeError(f"타임아웃 [{type(e).__name__}]") from e
        except httpx.HTTPError as e:
            raise RuntimeError(f"연결 오류 [{type(e).__name__}]: {e or '(연결 종료)'}") from e

        # thinking 만 나오고 answer 가 비어있으면 thinking 을 답변으로 승격
        if not full_answer and full_thinking:
            full_answer   = full_thinking
            full_thinking = ""
            yield {"type": "answer", "chunk": full_answer}

        usage["thinking"] = full_thinking
        usage["answer"]   = full_answer
        yield {"type": "usage", "data": usage}

    # ── 연결/모델 확인 ────────────────────────────────────────────────

    async def _fetch_model_entries(self) -> list[dict] | None:
        """GET /v1/models. 실패 시 None."""
        try:
            response = await self._client.get(
                self._models_url(), headers=self._headers, timeout=httpx.Timeout(10.0)
            )
            response.raise_for_status()
            data = response.json().get("data")
            if not isinstance(data, list):
                return None
            return [m for m in data if isinstance(m, dict)]
        except Exception:
            return None

    async def list_models(self) -> list[dict] | None:
        """계정에서 사용 가능한 모델 엔트리 목록. 조회 실패 시 None.

        `_fetch_model_entries()` 의 공개 래퍼. Anthropic 은 컨텍스트 길이를
        노출하지 않으므로 엔트리에 max_model_len 이 없다.
        """
        return await self._fetch_model_entries()

    async def health_check(self) -> bool:
        return await self._fetch_model_entries() is not None

    async def health_status(self) -> HealthReport:
        if "x-api-key" not in self._headers:
            return HealthReport(
                status="unreachable",
                reachable=False,
                model_ok=False,
                detail="Anthropic API 키가 설정되지 않았습니다.",
            )

        entries = await self._fetch_model_entries()
        if entries is None:
            return HealthReport(
                status="unreachable",
                reachable=False,
                model_ok=False,
                detail=f"{self._models_url()} 인증/조회에 실패했습니다. API 키와 주소를 확인하세요.",
            )

        available = [m["id"] for m in entries if isinstance(m.get("id"), str) and m["id"]]
        if self.model not in available:
            listed = ", ".join(available[:5]) or "(없음)"
            if len(available) > 5:
                listed += f" 외 {len(available) - 5}개"
            return HealthReport(
                status="model_missing",
                reachable=True,
                model_ok=False,
                detail=f"모델 '{self.model}' 이(가) 계정에서 사용 가능한 목록에 없습니다. 사용 가능: {listed}",
                available_models=available,
            )

        return HealthReport(
            status="ok",
            reachable=True,
            model_ok=True,
            detail=f"모델 '{self.model}' 확인됨.",
            available_models=available,
        )

    async def fetch_model_len(self) -> int:
        # Anthropic 은 컨텍스트 길이를 API 로 노출하지 않는다. 설정값/기본값을 그대로 사용.
        return self.model_len

    async def close(self) -> None:
        await self._client.aclose()
