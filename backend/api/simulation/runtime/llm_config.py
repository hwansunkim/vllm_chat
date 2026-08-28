"""시뮬레이션 실행에 쓰이는 LLM 콜러블 조립.

``_make_llm`` 은 server_id/temperature 를 고정한 동기 콜러블을 만들고,
``_make_agent_llm_map`` 은 시뮬레이션 기본값과 다른 설정을 가진 에이전트만
이름 → 콜러블로 매핑한다. lifecycle(start/continue) · load · resume ·
interviews 가 모두 이 두 함수를 공유한다.
"""
from __future__ import annotations

import logging

from ..schemas import SimStartConfig


logger = logging.getLogger(__name__)


def _make_llm(server_id: str | None, temperature: float = 0.7):
    """server_id/temperature를 고정한 동기 LLM 콜러블을 만든다.

    server_id가 None이면 registry의 기본 서버가 선택된다. DB에 사용 가능한
    서버가 없으면 select()가 즉시 RuntimeError를 던지며, 이는 이 함수 호출
    시점이 아니라 반환된 콜러블을 실제로 호출하는 첫 LLM 요청 시점에 발생해
    기존처럼 _run()의 except 절 → finalize_run(error=)로 UI에 표시된다.

    temperature 기본값 0.7은 이 옵션이 생기기 전 bridge에 하드코딩돼 있던 값이다.
    """
    from ABM.config import API_TIMEOUT
    from ....llm.bridge import make_sync_chat
    return make_sync_chat(server_id=server_id, timeout=API_TIMEOUT, temperature=temperature)


def _make_agent_llm_map(cfg: SimStartConfig) -> dict:
    """시뮬레이션 기본값과 다른 LLM 설정을 가진 에이전트만 이름→콜러블로 매핑한다.

    기준이 되는 축은 두 개다.
      - server_id:  에이전트 값이 없으면(None/빈 문자열) 시뮬레이션 기본값
                    (cfg.server_id)을 쓴다.
      - temperature: 에이전트 값이 None이면 시뮬레이션 기본값(cfg.temperature)을 쓴다.

    두 축을 합친 유효 설정이 시뮬레이션 기본값 `(cfg.server_id, cfg.temperature)`와
    같으면 매핑에서 빠지고, 기본 콜러블 `_make_llm(cfg.server_id, cfg.temperature)`을
    그대로 쓴다(ABM 쪽 `self._agent_llm.get(k) or self._llm`). 한 축만 달라도
    매핑에 포함되므로 "서버만 다름"/"온도만 다름"/"둘 다 다름"이 모두 잡힌다.
    구버전 시나리오처럼 필드 자체가 없으면 Pydantic 기본값(None)이 적용돼 제외된다.

    존재하지 않거나 비활성화된 server_id는 시뮬레이션 기본 서버로 되돌린다 —
    registry.select()가 그런 id를 조용히 "registry 전역 기본 서버"로 폴백시키는데,
    이는 시뮬레이션이 설정한 기본 서버(cfg.server_id)와 다를 수 있다(예: 로컬 vLLM
    시나리오인데 삭제된 서버를 가리키던 에이전트가 유료 OpenAI로 새는 경우).
    이때 temperature 오버라이드는 그대로 살아남는다(서버만 기본값으로 되돌림).
    """
    from ....llm.registry import get_registry
    registry = get_registry()
    cache: dict[tuple[str | None, float], object] = {}
    result = {}
    for a in cfg.agents:
        server_id = a.server_id or cfg.server_id
        if a.server_id and registry.get_provider(a.server_id) is None:
            logger.warning(
                "[%s] server_id=%r 를 찾을 수 없습니다(삭제/비활성) — "
                "시뮬레이션 기본 서버로 대신 동작합니다.",
                a.name, a.server_id,
            )
            server_id = cfg.server_id
        temperature = cfg.temperature if a.temperature is None else a.temperature

        if server_id == cfg.server_id and temperature == cfg.temperature:
            continue  # 기본 콜러블과 동일 — 매핑에 넣을 필요 없음
        key = (server_id, temperature)
        if key not in cache:
            cache[key] = _make_llm(server_id, temperature)
        result[a.name] = cache[key]
    return result
