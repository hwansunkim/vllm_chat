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


class _RetryStream(Exception):
    """스트림 요청을 같은 round 에서 다시 보내라는 내부 신호(컨텍스트 초과·파라미터 거부 조정 후)."""


def _estimate_prompt_tokens(messages: list) -> int:
    """프롬프트 토큰 수의 **보수적(과대) 추정** — 컨텍스트 클램프 전용. 한국어는 1~2자당 1토큰이라
    글자 수 / 2 + 메시지당 8 로 잡는다(조금 크게 잡을수록 클램프가 안전 쪽)."""
    chars = 0
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else ""
        if isinstance(c, list):            # 멀티파트 content
            c = " ".join(str(part.get("text", "")) for part in c if isinstance(part, dict))
        chars += len(str(c or ""))
    return chars // 2 + 8 * len(messages or [])


def capabilities_from_row(row: dict) -> dict:
    """servers 행 → 프로바이더 capabilities dict (registry·API 공용). NULL/없음 = None(미확인)."""
    def _tri(v):
        return None if v is None or v == "" else bool(v)
    return {
        "reasoning_override":        (row.get("reasoning_override") or "").strip().lower(),
        "is_reasoning":              _tri(row.get("cap_is_reasoning")),
        "supports_reasoning_effort": _tri(row.get("cap_supports_reasoning_effort")),
        "supports_temperature":      _tri(row.get("cap_supports_temperature")),
        "max_model_len":             int(row.get("cap_max_model_len") or 0),
        "probed_at":                 row.get("cap_probed_at") or "",
        "probe_error":               row.get("cap_probe_error") or "",
    }


_RE_MAX_OUT = re.compile(r"(?:at most|maximum(?: value)?(?: is| of)?|less than or equal to|<=)\s*(\d{3,7})", re.I)


_REJECT_WORDS = ("unsupported", "not support", "unrecognized", "unknown parameter", "unknown field",
                 "only the default", "not allowed", "extra inputs", "does not support", "invalid value",
                 "invalid parameter", "not permitted")


def _error_message(body: str) -> str:
    """400 본문에서 사람이 읽는 메시지만 — JSON 이면 error.message(또는 message/detail), 아니면 본문 그대로.
    오류 **종류** 필드("invalid_request_error")나 요청 에코가 파라미터 거부로 오인되지 않게 한다."""
    try:
        data = json.loads(body)
    except Exception:
        return body
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        for k in ("message", "detail"):
            if isinstance(data.get(k), str):
                return data[k]
    return body


def _mentions_param(body: str, *names: str) -> bool:
    """오류 메시지가 이 파라미터(들) 중 하나를 **거부한다**고 말하는가 — 메시지에 이름과 거부 표현이 함께 있어야 한다."""
    low = _error_message(body).lower()
    return any(n in low for n in names) and any(k in low for k in _REJECT_WORDS)


def _parse_context_error(body: str) -> tuple[int, int] | None:
    """컨텍스트 초과 400 → (허용되는 출력 토큰, 컨텍스트 길이). 모르는 형식이면 None.

    세 형식(숫자에서 허용 출력 = 컨텍스트 − 입력):
      - vLLM 신형: "... maximum context length is 8192 tokens and your request has 120 input tokens (16384 > 8192 - 120)"
      - OpenAI/vLLM 구형: "maximum context length is 8192 tokens. However, you requested 16504 tokens
        (120 in the messages, 16384 in the completion)"
      - "maximum context length is 8192 ... prompt contains at least 120 input tokens"
    """
    m = re.search(r'\((\d+)\s*>\s*(\d+)\s*-\s*(\d+)\)', body)
    if m:
        model_len, prompt = int(m.group(2)), int(m.group(3))
    else:
        m = (re.search(r'maximum context length is (\d+).*?(\d+) in the messages', body, re.DOTALL)
             or re.search(r'maximum context length is (\d+).*?prompt contains at least (\d+)', body, re.DOTALL))
        if not m:
            return None
        model_len, prompt = int(m.group(1)), int(m.group(2))
    reduced = model_len - prompt
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
        capabilities: dict | None = None,
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
        # 서버 설정 시점에 감지·저장한 능력(probe_capabilities → servers 테이블). 실행 중에는 탐지하지 않는다.
        # 400 으로 거부된 파라미터만 이 인스턴스 안에서 학습해 빼고 재시도한다(_adapt_to_400 — DB 에는 안 씀).
        self._learned_no_temperature = False
        self._learned_no_effort      = False
        self._learned_max_out        = 0
        self.apply_capabilities(capabilities or {})
        timeout = httpx.Timeout(300.0) if (self.thinking or self.is_reasoning()) else httpx.Timeout(60.0)
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

    # ── 능력(서버 설정 시 감지) · 우선순위 ────────────────────────────────

    # 이름 기반 추론 모델 판별(최후 폴백 — 한 번도 감지하지 않은 서버용). 서브클래스가 채운다.
    _REASONING_PREFIXES: tuple = ()
    # 감지 요청에서 "사고 파라미터를 받아들였다"를 사고형의 증거로 칠지(OpenAI: 일반 모델은 reasoning_effort 를
    # 400 으로 거부하므로 받아들였다 = 추론 모델). vLLM 은 chat_template_kwargs 를 늘 받으므로 False.
    _PROBE_PARAM_ACCEPT_IMPLIES_REASONING: bool = False

    def apply_capabilities(self, caps: dict) -> None:
        """저장된 능력·수동 설정을 인스턴스에 반영한다(생성 시·감지 직후)."""
        ov = (caps.get("reasoning_override") or "").strip().lower()
        self.reasoning_override = ov if ov in ("on", "off") else ""
        self.cap_is_reasoning              = caps.get("is_reasoning")
        self.cap_supports_reasoning_effort = caps.get("supports_reasoning_effort")
        self.cap_supports_temperature      = caps.get("supports_temperature")
        self.cap_probed_at                 = caps.get("probed_at") or ""
        self.cap_probe_error               = caps.get("probe_error") or ""
        cap_len = int(caps.get("max_model_len") or 0)
        if cap_len and not self.model_len:
            self.model_len = cap_len

    def _name_says_reasoning(self) -> bool:
        return bool(self._REASONING_PREFIXES) and self.model.startswith(self._REASONING_PREFIXES)

    def reasoning_source(self) -> str:
        """사고형 판정의 근거. 우선순위: 수동 설정 > 감지 결과 > 서버의 사고 수준(thinking) > 모델명."""
        if self.reasoning_override:
            return "override"
        if self.cap_is_reasoning is not None:
            return "probe"
        if self.thinking_level != "off":
            return "thinking_level"
        if self._name_says_reasoning():
            return "model_name"
        return "none"

    def is_reasoning(self) -> bool:
        src = self.reasoning_source()
        if src == "override":
            return self.reasoning_override == "on"
        if src == "probe":
            return bool(self.cap_is_reasoning)
        return src in ("thinking_level", "model_name")

    def _send_temperature(self) -> bool:
        if self._learned_no_temperature or self.cap_supports_temperature is False:
            return False
        if self.cap_supports_temperature is True:
            return True
        return self._default_send_temperature()

    def _default_send_temperature(self) -> bool:
        """감지 전 기본값. vLLM 등은 항상 보낸다."""
        return True

    def _send_reasoning_effort(self) -> bool:
        if self._learned_no_effort or self.cap_supports_reasoning_effort is False:
            return False
        if self.cap_supports_reasoning_effort is True:
            return True
        return self._default_send_reasoning_effort()

    def _default_send_reasoning_effort(self) -> bool:
        return True

    # ── 생성 예산 ──────────────────────────────────────────────────────

    def _budget(self, max_tokens: int, messages: list | None = None) -> int:
        """실제로 보낼 max_tokens — 넉넉한 기본(config.GENEROUS_COMPLETION_TOKENS) 이상, 호출자 값이 더 크면 그것.
        모델이 출력 상한을 알려줬으면(400 학습) 그 이하, 컨텍스트 길이를 알면 (길이 − 프롬프트 추정 − 여유) 이하."""
        budget = max(int(max_tokens or 0), config.GENEROUS_COMPLETION_TOKENS)
        if self._learned_max_out:
            budget = min(budget, self._learned_max_out)
        if self.model_len and messages is not None:
            room = self.model_len - _estimate_prompt_tokens(messages) - config.BUDGET_MARGIN_TOKENS
            budget = min(budget, max(room, config.BUDGET_MIN_TOKENS))
        return budget

    def _adapt_to_400(self, body: str, sent: dict) -> bool:
        """400 이 우리가 보낸 파라미터를 거부한 것이면 이 인스턴스에서 빼고(또는 줄이고) True — 호출자가 재시도.
        탐지가 아니라 방어다(감지를 안 한 서버·감지 뒤 서버가 바뀐 경우). DB 는 바꾸지 않는다."""
        if "temperature" in sent and not self._learned_no_temperature and _mentions_param(body, "temperature"):
            self._learned_no_temperature = True
            logger.warning("[%s] temperature 거부 — 이후 생략합니다", self.name)
            return True
        if (("reasoning_effort" in sent or "chat_template_kwargs" in sent) and not self._learned_no_effort
                and _mentions_param(body, "reasoning_effort", "chat_template_kwargs", "reasoning")):
            self._learned_no_effort = True
            logger.warning("[%s] reasoning_effort 거부 — 이후 생략합니다", self.name)
            return True
        low = body.lower()
        if self.MAX_TOKENS_PARAM in sent and ("max_tokens" in low or "max_completion_tokens" in low
                                               or "completion tokens" in low or "output tokens" in low):
            m = _RE_MAX_OUT.search(body)
            if m:
                cap = int(m.group(1))
                if 0 < cap < int(sent[self.MAX_TOKENS_PARAM]):
                    self._learned_max_out = cap
                    logger.warning("[%s] 출력 상한 %d 학습 — 이후 그 이하로 요청합니다", self.name, cap)
                    return True
        return False

    def _request_body(self, messages: list, effective_max: int, temperature: float, level: str,
                      **extra) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            self.MAX_TOKENS_PARAM: min(effective_max, self._learned_max_out) if self._learned_max_out else effective_max,
            **self._temperature_body(temperature),
            **extra,
            **self._thinking_body(level),
        }

    # ── 서버 설정 시 능력 감지 ──────────────────────────────────────────

    _PROBE_PROMPT = "2+3은? 숫자 하나만 답하세요."

    def _probe_reasoning_body(self) -> dict:
        """감지 요청에 실을 사고 파라미터(받아들이는지 시험). 기본: 없음."""
        return {}

    @staticmethod
    def _reasoning_signals(data: dict) -> bool:
        """응답에 사고의 흔적이 있나 — reasoning_tokens>0 · message.reasoning(_content) · <think>."""
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        if isinstance(details, dict) and (details.get("reasoning_tokens") or 0) > 0:
            return True
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return False
        for key in ("reasoning_content", "reasoning"):
            v = msg.get(key)
            if (isinstance(v, str) and v.strip()) or (v and not isinstance(v, str)):
                return True
        return "<think>" in (msg.get("content") or "")

    async def probe_capabilities(self) -> dict:
        """서버 등록·수정·연결 테스트 때 한 번 — 작은 요청으로 능력을 감지해 dict 로 돌려준다(저장은 호출자).

        - max_model_len: /v1/models 가 알려주면(vLLM)
        - supports_temperature / supports_reasoning_effort: 커스텀 temperature 와 사고 파라미터를 실어 보내고,
          400 이 그 파라미터를 거부하면 빼고 다시 보내 False 로 기록
        - is_reasoning: 응답의 사고 흔적(reasoning_tokens·reasoning(_content)·<think>), OpenAI 는 추가로
          "사고 파라미터를 받아들였다"·"temperature 를 거부했다"도 증거로 친다
        실패하면 probe_error 에 사유를 적고 나머지는 None(미확인)으로 둔다.
        """
        from datetime import datetime
        caps: dict = {"is_reasoning": None, "supports_reasoning_effort": None, "supports_temperature": None,
                      "max_model_len": 0, "probe_error": "", "probed_at": datetime.now().isoformat()}
        entries = await self._fetch_model_entries()
        if entries:
            entry = self._match_model(entries)
            if entry and entry.get("max_model_len"):
                caps["max_model_len"] = int(entry["max_model_len"])
        send_temp, send_effort = True, bool(self._probe_reasoning_body())
        temp_rejected = False
        data = None
        for _ in range(4):
            body = {"model": self.model, "messages": [{"role": "user", "content": self._PROBE_PROMPT}],
                    self.MAX_TOKENS_PARAM: 2048}
            if send_temp:
                body["temperature"] = 0.3
            if send_effort:
                body.update(self._probe_reasoning_body())
            try:
                resp = await self._client.post(self._chat_url(), headers=self._headers, json=body,
                                               timeout=httpx.Timeout(90.0))
            except Exception as e:
                caps["probe_error"] = f"감지 요청 실패 [{type(e).__name__}]: {e or '(연결 종료)'}"
                return caps
            if resp.status_code == 400:
                text = resp.text[:500]
                if send_temp and _mentions_param(text, "temperature"):
                    send_temp, temp_rejected = False, True
                    caps["supports_temperature"] = False
                    continue
                if send_effort and _mentions_param(text, "reasoning_effort", "chat_template_kwargs", "reasoning"):
                    send_effort = False
                    caps["supports_reasoning_effort"] = False
                    continue
                caps["probe_error"] = f"HTTP 400: {text}"
                return caps
            if resp.status_code >= 300:
                caps["probe_error"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
                return caps
            try:
                data = resp.json()
            except Exception:
                caps["probe_error"] = f"JSON 파싱 실패: {resp.text[:200]!r}"
                return caps
            break
        if data is None:
            caps["probe_error"] = "파라미터를 바꿔 여러 번 시도했지만 성공하지 못했습니다."
            return caps
        if caps["supports_temperature"] is None:
            caps["supports_temperature"] = True
        if send_effort and caps["supports_reasoning_effort"] is None:
            caps["supports_reasoning_effort"] = True
        signals = self._reasoning_signals(data)
        if self._PROBE_PARAM_ACCEPT_IMPLIES_REASONING:
            signals = signals or temp_rejected or caps["supports_reasoning_effort"] is True
        caps["is_reasoning"] = bool(signals)
        return caps

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
        return self._effective_level(thinking_level) != "off" or (
            self.reasoning_source() in ("override", "probe") and self.is_reasoning())

    def _with_thinking_headroom(self, max_tokens: int, thinking_level: str | None) -> int:
        """하위호환 — 이제 모든 호출이 넉넉한 예산(`_budget`)을 받는다(사고 여부와 무관)."""
        return self._budget(max_tokens)

    def _temperature_body(self, temperature: float) -> dict:
        """요청 바디에 병합할 temperature 파라미터.

        일부 벤더는 특정 모델에서 temperature 커스텀 값을 400 으로 거부한다
        (OpenAI gpt-5/o-계열 reasoning 모델). 그런 서브클래스는 빈 dict 를
        반환해 파라미터 자체를 생략한다. 이제 판정은 `_send_temperature()`(감지 결과 > 이름 폴백 > 400 학습).
        """
        return {"temperature": temperature} if self._send_temperature() else {}

    # ── 호출 ─────────────────────────────────────────────────────────

    async def llm(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> str:
        messages = [{"role": "user", "content": prompt}]
        budget = self._budget(max_tokens, messages)
        context_retried = False
        for _ in range(4):                      # 파라미터 조정·컨텍스트 재시도 합쳐 상한
            body = {
                "model": self.model,
                "messages": messages,
                self.MAX_TOKENS_PARAM: min(budget, self._learned_max_out) if self._learned_max_out else budget,
                **self._temperature_body(temperature),
            }
            response = await self._client.post(self._chat_url(), headers=self._headers, json=body)
            if response.status_code == 400:
                text = response.text[:800]
                if self._adapt_to_400(text, body):
                    continue
                parsed = None if context_retried else _parse_context_error(text)
                if parsed is not None:
                    budget, detected_len = parsed
                    if detected_len and not self.model_len:
                        self.model_len = detected_len
                    context_retried = True
                    continue
            break
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
        effective_max    = self._budget(max_tokens, current_messages)
        _context_retried = False
        _adapt_left      = 3

        rounds = 0
        while rounds < config.MAX_CONTINUATION_ROUNDS:
            rounds += 1
            sent = self._request_body(current_messages, effective_max, temperature, level)
            try:
                response = await self._client.post(
                    self._chat_url(),
                    headers=self._headers,
                    json=sent,
                    timeout=req_timeout,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                body = e.response.text[:800]
                logger.error("[%s] HTTP %s: %s", self.name, e.response.status_code, body)
                if e.response.status_code == 400 and _adapt_left and self._adapt_to_400(body, sent):
                    _adapt_left -= 1
                    rounds -= 1            # 파라미터 조정 재시도는 이어 쓰기 round 로 세지 않는다
                    continue
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
        current_messages = list(messages)
        effective_max    = self._budget(max_tokens, current_messages)
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

            for _attempt in range(5):
                got_context_error = False
                sent = self._request_body(current_messages, effective_max, temperature, level,
                                          stream=True, stream_options={"include_usage": True})
                try:
                    async with self._client.stream(
                        "POST",
                        self._chat_url(),
                        headers=self._headers,
                        json=sent,
                    ) as response:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as e:
                            body = (await e.response.aread()).decode(errors="replace")[:800]
                            logger.error("[%s] stream HTTP %s: %s", self.name, e.response.status_code, body)
                            if e.response.status_code == 400 and self._adapt_to_400(body, sent):
                                raise _RetryStream()           # 파라미터 조정 — 같은 round 재시도
                            if e.response.status_code == 400 and not _context_retried:
                                parsed = _parse_context_error(body)
                                if parsed is not None:
                                    reduced, model_len = parsed
                                    effective_max = reduced
                                    if model_len and not self.model_len:
                                        self.model_len = model_len
                                    _context_retried = True
                                    # `break` 는 async with 를 빠져 _attempt 루프까지 끝내 재시도가 안 됐다
                                    # (빈 답으로 끝남) — 예외로 빠져나와 아래에서 재시도한다.
                                    raise _RetryStream()
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

                except _RetryStream:
                    got_context_error = True
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
