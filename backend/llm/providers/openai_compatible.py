from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator

import httpx

from ... import config
from .base import HealthReport, LLMHTTPError

logger = logging.getLogger(__name__)


def _extract_reply(message: dict) -> tuple[str, str]:
    """(answer, thinking) 반환.

    방식 A: reasoning_content 별도 필드 (vLLM 0.6+, Qwen3 등)
    방식 B: content 안의 <think>...</think> 태그 (구버전/일부 모델)
    """
    thinking = (message.get("reasoning_content") or "").strip()
    content  = (message.get("content") or "").strip()

    if not thinking:
        m = re.match(r"\s*<think>(.*?)</think>\s*(.*)", content, re.DOTALL)
        if m:
            thinking = m.group(1).strip()
            content  = m.group(2).strip()

    return content, thinking


def _parse_context_error(body: str) -> tuple[int, int] | None:
    m = re.search(r'maximum context length is (\d+).*?prompt contains at least (\d+)', body, re.DOTALL)
    if not m:
        return None
    model_len = int(m.group(1))
    reduced   = model_len - int(m.group(2))
    return (reduced, model_len) if reduced > 0 else None


class OpenAICompatibleProvider:
    """OpenAI 호환 REST API(`/v1/chat/completions`) 기반 프로바이더 공통 구현.

    vLLM / OpenAI 등 OpenAI 스펙을 따르는 백엔드가 이 클래스를 상속한다.
    서브클래스 훅:
      - DEFAULT_BASE_URL : row 의 base_url 이 비어있을 때 사용할 기본 주소
      - DEFAULT_API_KEY_ATTR : config 모듈에서 읽어올 환경변수 기반 API 키 속성명
      - HEALTH_PATH  : 생존 확인용 경로 (None 이면 /v1/models 조회로 대체)
      - _thinking_body() : 요청 바디에 추가할 벤더 전용 thinking 파라미터
                           (인자는 이미 해석된 effective level 문자열)
    """

    DEFAULT_BASE_URL: str = ""
    DEFAULT_API_KEY_ATTR: str = "VLLM_API_KEY"
    HEALTH_PATH: str | None = "/health"
    # OpenAI 는 gpt-5.1+ 등 최신 모델에서 max_tokens 를 거부하고
    # max_completion_tokens 를 요구한다("Unsupported parameter" 400).
    # vLLM 등 순수 OpenAI 호환 서버는 여전히 max_tokens 를 쓰므로 서브클래스 훅으로 둔다.
    MAX_TOKENS_PARAM: str = "max_tokens"

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
        thinking_level: str = "off",
        configured_max_len: int = 0,
    ) -> None:
        self.id        = server_id
        self.name      = name
        self.base_url  = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model     = model
        self.enabled   = enabled
        self.is_default = is_default
        # 서버 기본 사고 수준. 요청이 level 을 주지 않으면 이 값이 쓰인다.
        self.thinking_level = config.normalize_thinking_level(thinking_level)
        # 하위호환 파생값 — 구 코드의 `provider.thinking` 참조를 깨지 않는다.
        self.thinking  = self.thinking_level != "off"
        self.model_len: int = configured_max_len
        timeout = httpx.Timeout(300.0) if self.thinking else httpx.Timeout(60.0)
        self._client = httpx.AsyncClient(timeout=timeout)
        effective_key = api_key or getattr(config, self.DEFAULT_API_KEY_ATTR, "")
        self._headers = {"Content-Type": "application/json"}
        if effective_key:
            self._headers["Authorization"] = f"Bearer {effective_key}"

    # ── URL 구성 ──────────────────────────────────────────────────────

    def _api_base(self) -> str:
        """`/v1` 까지 포함한 API 루트. base_url 이 이미 /v1 로 끝나면 중복 부착 금지."""
        return self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

    def _chat_url(self) -> str:
        return f"{self._api_base()}/chat/completions"

    def _models_url(self) -> str:
        return f"{self._api_base()}/models"

    # ── 벤더 전용 훅 ──────────────────────────────────────────────────

    def _effective_level(self, thinking_level: str | None) -> str:
        """요청이 준 level 과 서버 기본값을 합쳐 실제로 적용할 level 을 정한다.

        `None`(미지정) 이면 서버 기본값(`self.thinking_level`)을 쓴다. 명시값은
        서버 기본값을 오버라이드한다 — 채팅 🧠 컨트롤이 메시지별로 끄거나 올릴 수 있다.
        """
        if thinking_level is None:
            return self.thinking_level
        return config.normalize_thinking_level(thinking_level)

    def _thinking_body(self, level: str) -> dict:
        """요청 바디에 병합할 벤더 전용 thinking 파라미터. 기본은 없음.

        `level` 은 `_effective_level()` 로 이미 해석된 "off"|"low"|"medium"|"high".
        """
        return {}

    def needs_thinking_headroom(self, thinking_level: str | None = None) -> bool:
        """이 요청이 reasoning 토큰을 소비하므로 max_tokens 상향이 필요한가.

        사고가 켜지면 생성 예산의 상당 부분을 reasoning 이 먼저 먹는다. 예산을
        올려두지 않으면 답변이 나오기도 전에 `finish_reason == "length"` 로 끊겨
        continuation 루프가 빈 왕복만 반복한다(ABM 의 `max_tokens=256` 분류 호출이
        정확히 이 함정에 빠졌다). 벤더별로 판정이 다르므로 훅으로 둔다.
        """
        return self._effective_level(thinking_level) != "off"

    def _with_thinking_headroom(self, max_tokens: int, thinking_level: str | None) -> int:
        if self.needs_thinking_headroom(thinking_level):
            return max(max_tokens, config.MAX_COMPLETION_TOKENS_THINKING)
        return max_tokens

    def _temperature_body(self, temperature: float) -> dict:
        """요청 바디에 병합할 temperature 파라미터.

        일부 벤더는 특정 모델에서 temperature 커스텀 값을 400 으로 거부한다
        (OpenAI gpt-5/o-계열 reasoning 모델). 그런 서브클래스는 빈 dict 를
        반환해 파라미터 자체를 생략한다.
        """
        return {"temperature": temperature}

    # ── 호출 ─────────────────────────────────────────────────────────

    async def llm(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> str:
        response = await self._client.post(
            self._chat_url(),
            headers=self._headers,
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                self.MAX_TOKENS_PARAM: max_tokens,
                **self._temperature_body(temperature),
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
        timeout: float | None = None,
        thinking_level: str | None = None,
    ) -> tuple[str, dict]:
        # timeout 미지정 시 생성자에서 설정한 클라이언트 기본값(60s/300s)을 그대로 쓴다.
        req_timeout = httpx.Timeout(timeout) if timeout else httpx.USE_CLIENT_DEFAULT
        # 비스트리밍 chat() 의 유일한 호출자는 시뮬레이션(bridge.make_sync_chat)이다.
        # level 을 주지 않으면 서버 기본값을 상속한다 — 시뮬레이션 전용 사고 UI 없이
        # 선택된 서버의 설정을 그대로 따르게 하기 위함.
        level       = self._effective_level(thinking_level)
        full_reply  = ""
        full_thinking = ""
        total_completion_tokens = 0
        final_usage: dict = {}
        current_messages = list(messages)
        # 사고가 실리는 요청은 예산을 올린다. 시뮬레이션의 짧은 유틸 호출
        # (classify_wave_time 은 max_tokens=256)이 reasoning 에 예산을 다 쓰고
        # 빈 응답으로 continuation 5회를 왕복하는 것을 막는다.
        effective_max    = self._with_thinking_headroom(max_tokens, thinking_level)
        _context_retried = False

        for _ in range(config.MAX_CONTINUATION_ROUNDS):
            try:
                response = await self._client.post(
                    self._chat_url(),
                    headers=self._headers,
                    json={
                        "model":       self.model,
                        "messages":    current_messages,
                        self.MAX_TOKENS_PARAM: effective_max,
                        **self._temperature_body(temperature),
                        **self._thinking_body(level),
                    },
                    timeout=req_timeout,
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
                        continue
                raise LLMHTTPError(
                    e.response.status_code,
                    f"HTTP {e.response.status_code} from {self.name}: {body}",
                ) from e
            except httpx.TimeoutException as e:
                raise RuntimeError(f"타임아웃 — {self.name}: {type(e).__name__}") from e
            except httpx.HTTPError as e:
                raise RuntimeError(f"연결 오류 [{type(e).__name__}]: {e or '(연결 종료)'}") from e

            try:
                data = response.json()
            except Exception as e:
                preview = response.text[:200]
                raise RuntimeError(f"JSON 파싱 실패: {preview!r}") from e

            if "choices" not in data or not data["choices"]:
                raise RuntimeError(f"응답에 choices 없음: {data}")

            choice = data["choices"][0]
            if "message" not in choice:
                raise RuntimeError(f"choice에 message 없음: {choice}")

            partial, thinking  = _extract_reply(choice["message"])
            finish_reason: str = choice.get("finish_reason", "stop")
            usage: dict        = data.get("usage", {})

            full_reply    += partial
            full_thinking += thinking
            total_completion_tokens += usage.get("completion_tokens", 0)
            final_usage = usage

            if finish_reason != "length":
                break

            current_messages = current_messages + [
                {"role": "assistant", "content": partial},
                {"role": "user", "content": config.CONTINUE_PROMPT},
            ]

        merged = dict(final_usage)
        merged["completion_tokens"] = total_completion_tokens
        merged["thinking"] = full_thinking
        return full_reply, merged

    # ── 연결/모델 확인 ────────────────────────────────────────────────

    async def _probe_liveness(self) -> tuple[bool, str]:
        """생존 확인. (살아있음, 실패 사유) 반환.

        HEALTH_PATH 가 있으면 해당 경로를 GET, 없으면 /v1/models 조회로 대체.
        """
        if self.HEALTH_PATH is None:
            entries = await self._fetch_model_entries()
            if entries is None:
                return False, f"{self._models_url()} 인증/조회에 실패했습니다. API 키와 주소를 확인하세요."
            return True, ""
        try:
            resp = await self._client.get(
                f"{self.base_url}{self.HEALTH_PATH}",
                headers=self._headers,
                timeout=httpx.Timeout(5.0),
            )
            if resp.status_code == 200:
                return True, ""
            return False, f"{self.HEALTH_PATH} 가 HTTP {resp.status_code} 를 반환했습니다."
        except Exception as e:
            return False, f"{self.base_url} 에 연결할 수 없습니다 [{type(e).__name__}]: {e or '(연결 종료)'}"

    async def _fetch_model_entries(self) -> list[dict] | None:
        """GET /v1/models 의 data 배열. 조회 실패 시 None (빈 목록과 구분)."""
        try:
            response = await self._client.get(
                self._models_url(),
                headers=self._headers,
                timeout=httpx.Timeout(10.0),
            )
            response.raise_for_status()
            data = response.json().get("data")
            if not isinstance(data, list):
                return None
            return [m for m in data if isinstance(m, dict)]
        except Exception:
            return None

    async def list_models(self) -> list[dict] | None:
        """서버가 제공하는 모델 엔트리 목록. 조회 실패 시 None (빈 목록과 구분).

        `_fetch_model_entries()` 의 공개 래퍼. 서버 등록 전 모델 선택지를
        미리 조회하는 API 레이어가 private 메서드에 의존하지 않게 한다.
        """
        return await self._fetch_model_entries()

    def _match_model(self, entries: list[dict]) -> dict | None:
        """설정된 모델명과 id 가 정확히 일치(exact match)하는 엔트리."""
        for m in entries:
            if m.get("id") == self.model:
                return m
        return None

    def _apply_model_len(self, entry: dict) -> None:
        api_len = entry.get("max_model_len", 0)
        if api_len:
            self.model_len = api_len

    async def health_check(self) -> bool:
        """프로세스 생존 여부만 확인 (모델 존재는 보지 않음)."""
        alive, _ = await self._probe_liveness()
        return alive

    async def health_status(self) -> HealthReport:
        """연결 + 모델 존재까지 확인한 3단계 상태를 반환."""
        alive, reason = await self._probe_liveness()
        if not alive:
            return HealthReport(
                status="unreachable", reachable=False, model_ok=False, detail=reason
            )

        entries = await self._fetch_model_entries()
        if entries is None:
            return HealthReport(
                status="model_missing",
                reachable=True,
                model_ok=False,
                detail="서버는 응답하지만 모델 목록(/v1/models)을 조회하지 못했습니다.",
            )

        available = [m["id"] for m in entries if isinstance(m.get("id"), str) and m["id"]]
        entry = self._match_model(entries)
        if entry is None:
            listed = ", ".join(available[:5]) or "(없음)"
            if len(available) > 5:
                listed += f" 외 {len(available) - 5}개"
            return HealthReport(
                status="model_missing",
                reachable=True,
                model_ok=False,
                detail=f"모델 '{self.model}' 이(가) 서버에 없습니다. 서버가 제공하는 모델: {listed}",
                available_models=available,
            )

        self._apply_model_len(entry)
        return HealthReport(
            status="ok",
            reachable=True,
            model_ok=True,
            detail=f"모델 '{self.model}' 확인됨.",
            available_models=available,
        )

    async def fetch_model_len(self) -> int:
        entries = await self._fetch_model_entries()
        if entries is None:
            return self.model_len
        entry = self._match_model(entries)
        if entry is not None:
            self._apply_model_len(entry)
        return self.model_len

    async def stream_chat(
        self,
        messages: list,
        *,
        temperature: float = 0.7,
        max_tokens: int = config.MAX_COMPLETION_TOKENS,
        thinking_level: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        level            = self._effective_level(thinking_level)
        effective_max    = self._with_thinking_headroom(max_tokens, thinking_level)
        current_messages = list(messages)
        full_thinking    = ""
        full_answer      = ""
        total_completion_tokens = 0
        final_usage: dict = {}
        _context_retried  = False
        think_state = "pre"

        for _round in range(config.MAX_CONTINUATION_ROUNDS):
            round_answer      = ""
            round_thinking    = ""
            usage: dict       = {}
            finish_reason_end = "stop"
            buf               = ""

            for _attempt in range(2):
                got_context_error = False
                try:
                    async with self._client.stream(
                        "POST",
                        self._chat_url(),
                        headers=self._headers,
                        json={
                            "model":          self.model,
                            "messages":       current_messages,
                            self.MAX_TOKENS_PARAM: effective_max,
                            **self._temperature_body(temperature),
                            "stream":         True,
                            "stream_options": {"include_usage": True},
                            **self._thinking_body(level),
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
                                    got_context_error = True
                                    break
                            raise LLMHTTPError(
                                e.response.status_code,
                                f"HTTP {e.response.status_code}: {body}",
                            ) from e

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

                            # 방식 A: reasoning_content 필드 (구버전은 "reasoning")
                            rc      = delta.get("reasoning_content") or delta.get("reasoning") or ""
                            content = delta.get("content") or ""

                            if rc:
                                full_thinking  += rc
                                round_thinking += rc
                                yield {"type": "thinking", "chunk": rc}
                                if content:
                                    full_answer  += content
                                    round_answer += content
                                    yield {"type": "answer", "chunk": content}
                                continue

                            # 방식 B: <think> 태그 상태 머신
                            if content:
                                buf += content
                                while buf:
                                    if think_state == "pre":
                                        stripped = buf.lstrip()
                                        if not stripped:
                                            break
                                        if stripped.startswith("<think>"):
                                            think_state = "thinking"
                                            buf         = stripped[7:]
                                        elif "<think>".startswith(stripped) and not finish_reason:
                                            break
                                        elif len(stripped) >= 7 or finish_reason:
                                            think_state  = "answer"
                                            full_answer  += buf
                                            round_answer += buf
                                            yield {"type": "answer", "chunk": buf}
                                            buf = ""
                                        else:
                                            break

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

                        if buf:
                            if think_state == "thinking":
                                full_thinking += buf
                                yield {"type": "thinking", "chunk": buf}
                            else:
                                full_answer  += buf
                                round_answer += buf
                                yield {"type": "answer", "chunk": buf}

                except httpx.TimeoutException as e:
                    raise RuntimeError(f"타임아웃 [{type(e).__name__}]") from e
                except httpx.HTTPError as e:
                    raise RuntimeError(f"연결 오류 [{type(e).__name__}]: {e or '(연결 종료)'}") from e

                if got_context_error:
                    continue
                break

            total_completion_tokens += usage.get("completion_tokens", 0)
            final_usage = usage

            if finish_reason_end != "length":
                break

            logger.info("[%s] finish_reason=length, 연속 생성 round %d/%d",
                        self.name, _round + 1, config.MAX_CONTINUATION_ROUNDS)
            if not round_answer and round_thinking:
                # thinking 도중 끊긴 경우: <think> 블록을 열어둔 채로 전달
                assistant_content = f"<think>{round_thinking}"
                continue_prompt   = config.CONTINUE_PROMPT_THINKING
            else:
                assistant_content = round_answer
                continue_prompt   = config.CONTINUE_PROMPT
            current_messages = current_messages + [
                {"role": "assistant", "content": assistant_content},
                {"role": "user",      "content": continue_prompt},
            ]

        # content가 비어있고 thinking만 있으면 thinking을 answer로 승격
        # (Qwen3.6 등 always-thinking 모델이 <think> 안에 최종 답변을 쓰는 경우)
        if not full_answer and full_thinking:
            full_answer = full_thinking
            full_thinking = ""
            yield {"type": "answer", "chunk": full_answer}

        merged = dict(final_usage)
        merged["completion_tokens"] = total_completion_tokens
        merged["thinking"]          = full_thinking
        merged["answer"]            = full_answer
        yield {"type": "usage", "data": merged}

    async def close(self) -> None:
        await self._client.aclose()
