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

    # gpt-5 / o-계열 추론 모델 — 이제 **최후 폴백**이다(한 번도 감지하지 않은 서버용). 판정 우선순위:
    # 수동 설정 > 서버 설정 시 감지(probe_capabilities) > 서버의 사고 수준(thinking) > 이 이름 목록
    # (`OpenAICompatibleProvider.is_reasoning`). 실측: 게이트웨이 모델 "gpt-6-astra"는 이름 목록에 없어
    # 사고형인데도 헤드룸·reasoning_effort 없이 512 상한으로 호출돼 기억 압축이 수백 번 실패했다.
    _REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
    # 하위호환 별칭 (구 이름을 참조하는 코드/테스트용)
    _NO_TEMPERATURE_PREFIXES = _REASONING_PREFIXES
    # 감지 요청에서 reasoning_effort 를 받아들였다 = 추론 모델(일반 모델은 400 으로 거부한다).
    _PROBE_PARAM_ACCEPT_IMPLIES_REASONING = True

    # 추론 모델이지만 `reasoning_effort` 파라미터는 거부하는 예외들(감지 전 폴백 전용).
    # o1-mini / o1-preview 는 reasoning 을 하면서도 이 파라미터에 400 을 낸다.
    _NO_REASONING_EFFORT = ("o1-mini", "o1-preview")

    def _is_reasoning_model(self) -> bool:
        """하위호환 이름 — 이제 우선순위 판정(`is_reasoning`)."""
        return self.is_reasoning()

    def _default_send_temperature(self) -> bool:
        # 감지 전: 이름 목록·수동 설정으로 추론 모델이면 temperature 커스텀 값을 400 으로 거부하므로 생략한다.
        # 서버 사고 수준(thinking)만으로 사고형이 된 경우는 생략하지 않는다 — 사고 수준은 "사고를 원한다"는 뜻일 뿐
        # 모델이 temperature 를 거부한다는 증거가 아니다. 거부하면 실행 중 400 조정이 빼고 다시 보낸다.
        # 이름 목록은 사고 수준이 켜져 있어 근거가 "thinking_level" 로 잡혀도 여전히 증거다(gpt-5 + high).
        return not (self.is_reasoning()
                    and (self.reasoning_source() in ("override", "model_name") or self._name_says_reasoning()))

    def _default_send_reasoning_effort(self) -> bool:
        return not self.model.startswith(self._NO_REASONING_EFFORT)

    def _probe_reasoning_body(self) -> dict:
        return {"reasoning_effort": "low"}

    def _thinking_body(self, level: str) -> dict:
        """사고형으로 판정된 모델에만 `reasoning_effort` 를 전송한다.

        일반 모델(gpt-4o 등)은 이 파라미터를 400 으로 거부하므로 level 이 조용한
        no-op 이 된다. 감지 결과가 "reasoning_effort 미지원"이면 사고형이어도 보내지 않는다.

        `off` 는 추론 모델에서 완전한 비활성이 불가능하다(OpenAI 는 reasoning 을
        끄는 스위치를 제공하지 않는다). 파라미터를 생략해 서버 기본값(medium)에
        맡기며, 이는 의도된 동작이다.
        """
        if level == "off" or not self.is_reasoning() or not self._send_reasoning_effort():
            return {}
        return {"reasoning_effort": level}

    def needs_thinking_headroom(self, thinking_level: str | None = None) -> bool:
        """추론 모델은 level 과 무관하게 항상 reasoning 토큰 헤드룸이 필요하다(off 여도 OpenAI 는 reasoning 을
        끌 수 없다). 일반 모델은 필요 없다. (생성 예산 자체는 이제 모든 호출이 넉넉하게 받는다 — `_budget`.)"""
        return self.is_reasoning()
