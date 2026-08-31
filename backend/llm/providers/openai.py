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

    # gpt-5 / o-계열 추론 모델. 두 가지 분기에 같은 판별을 쓴다:
    #   1) temperature 를 기본값(1) 외의 값으로 주면 400 → 파라미터 자체를 생략
    #   2) reasoning_effort 를 이해하는 유일한 모델군 → 그 외에는 전송 금지
    _REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
    # 하위호환 별칭 (구 이름을 참조하는 코드/테스트용)
    _NO_TEMPERATURE_PREFIXES = _REASONING_PREFIXES

    # 추론 모델이지만 `reasoning_effort` 파라미터는 거부하는 예외들.
    # o1-mini / o1-preview 는 reasoning 을 하면서도 이 파라미터에 400 을 낸다.
    # (bare "o1" prefix 가 이 둘을 함께 잡으므로 _thinking_body 에서만 따로 뺀다.
    #  _temperature_body 는 이 둘도 temperature 를 거부하므로 prefix 그대로가 맞다.)
    _NO_REASONING_EFFORT = ("o1-mini", "o1-preview")

    def _is_reasoning_model(self) -> bool:
        return self.model.startswith(self._REASONING_PREFIXES)

    def _temperature_body(self, temperature: float) -> dict:
        if self._is_reasoning_model():
            return {}
        return {"temperature": temperature}

    def _thinking_body(self, level: str) -> dict:
        """추론 모델에만 `reasoning_effort` 를 전송한다.

        일반 모델(gpt-4o 등)은 이 파라미터를 400 으로 거부하므로 level 이 조용한
        no-op 이 된다 — 프론트에서 수준을 바꿔도 요청 바디는 변하지 않는다.

        `off` 는 추론 모델에서 완전한 비활성이 불가능하다(OpenAI 는 reasoning 을
        끄는 스위치를 제공하지 않는다). 파라미터를 생략해 서버 기본값(medium)에
        맡기며, 이는 의도된 동작이다.
        """
        if level == "off" or not self._is_reasoning_model():
            return {}
        if self.model.startswith(self._NO_REASONING_EFFORT):
            return {}
        return {"reasoning_effort": level}

    def needs_thinking_headroom(self, thinking_level: str | None = None) -> bool:
        """추론 모델은 level 과 무관하게 항상 reasoning 토큰 헤드룸이 필요하다.

        `off` 여도 OpenAI 는 reasoning 을 끌 수 없다(파라미터를 생략해 서버 기본값
        medium 에 맡긴다). 이때 max_tokens 를 올려두지 않으면 4096 을 reasoning 이
        대부분 소비해 답변이 비어서 돌아온다.

        반대로 일반 모델(gpt-4o 등)은 4단계 전부 요청 바디가 변하지 않으므로
        reasoning 토큰을 쓰지 않는다 — 헤드룸이 필요 없다.
        """
        return self._is_reasoning_model()
