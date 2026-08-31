from __future__ import annotations

from .openai_compatible import OpenAICompatibleProvider, _extract_reply  # noqa: F401  (하위호환 re-export)


class VLLMProvider(OpenAICompatibleProvider):
    """로컬/사내 vLLM OpenAI 호환 서버.

    - base_url 은 필수 (기본값 없음). 예: http://172.17.3.135:8000
    - 생존 확인은 vLLM 전용 GET /health
    - 사고가 켜져 있으면 chat_template_kwargs 로 enable_thinking 을 전달
    """

    DEFAULT_BASE_URL = ""
    DEFAULT_API_KEY_ATTR = "VLLM_API_KEY"
    HEALTH_PATH = "/health"

    def _thinking_body(self, level: str) -> dict:
        """low/medium/high 는 전부 동일하게 `enable_thinking: true` 로 전송된다.

        대부분의 chat template 은 사고 강도를 세분하지 못하므로 on/off 만 의미가 있다.
        강도를 이해하는 템플릿을 위해 `reasoning_effort` 를 함께 실어 보낸다 —
        Jinja 템플릿은 모르는 키를 조용히 무시하므로 구 모델에서도 안전하다.
        """
        if level == "off":
            return {}
        return {"chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": level}}
