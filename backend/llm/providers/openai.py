from __future__ import annotations

from .openai_compatible import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI 공식 API (api.openai.com).

    - base_url 이 비어있으면 https://api.openai.com/v1 사용
      (프록시/게이트웨이를 쓰는 경우에만 직접 입력)
    - /health 엔드포인트가 없으므로 생존 확인은 GET /v1/models 로 대체
      → API 키가 잘못되면 401 이 나고 "unreachable" 로 보고된다
    - chat_template_kwargs 는 OpenAI 가 이해하지 못하므로 전송하지 않음
    """

    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_API_KEY_ATTR = "OPENAI_API_KEY"
    HEALTH_PATH = None  # /v1/models 조회로 생존 확인
    # gpt-5.1+ 등 최신 모델은 max_tokens 를 거부하고 max_completion_tokens 를 요구한다.
    # gpt-4.1 이하는 max_completion_tokens 도 받아들이므로 모델별 분기 없이 통일한다.
    MAX_TOKENS_PARAM = "max_completion_tokens"

    # gpt-5 / o-계열 reasoning 모델은 temperature 를 기본값(1) 외의 값으로 주면
    # "Unsupported value ... temperature" 400 을 낸다. 이 경우 파라미터 자체를 생략한다.
    _NO_TEMPERATURE_PREFIXES = ("gpt-5", "o1", "o3", "o4")

    def _temperature_body(self, temperature: float) -> dict:
        if self.model.startswith(self._NO_TEMPERATURE_PREFIXES):
            return {}
        return {"temperature": temperature}
