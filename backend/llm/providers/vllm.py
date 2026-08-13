from __future__ import annotations

from .openai_compatible import OpenAICompatibleProvider, _extract_reply  # noqa: F401  (하위호환 re-export)


class VLLMProvider(OpenAICompatibleProvider):
    """로컬/사내 vLLM OpenAI 호환 서버.

    - base_url 은 필수 (기본값 없음). 예: http://172.17.3.135:8000
    - 생존 확인은 vLLM 전용 GET /health
    - thinking 서버면 chat_template_kwargs 로 enable_thinking 을 전달
    """

    DEFAULT_BASE_URL = ""
    DEFAULT_API_KEY_ATTR = "VLLM_API_KEY"
    HEALTH_PATH = "/health"

    def _thinking_body(self, thinking: bool) -> dict:
        if not self.thinking:
            return {}
        return {"chat_template_kwargs": {"enable_thinking": thinking}}
