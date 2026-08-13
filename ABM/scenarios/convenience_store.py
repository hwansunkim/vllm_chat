"""편의점 시나리오 — 로컬 스모크테스트용 standalone 스크립트.

이 파일은 백엔드 DB/registry 없이 ABM.config 환경변수만으로 vLLM 에 직접 붙는다.
Simulation 은 llm 콜러블을 주입받으므로, 여기서는 이 스크립트 전용의 최소
동기 HTTP 콜러블(_direct_chat)을 만들어 넘긴다. 서버 등록/프로바이더 선택이
필요한 실사용 경로는 backend/llm/bridge.py 의 make_sync_chat() 을 쓴다.
"""
import logging

import requests

from ..agent import Agent
from ..config import LOG_DIR, MODEL, BASE_URL, API_KEY, API_TIMEOUT
from ..llm import LLMCall
from ..simulation import Simulation

logger = logging.getLogger(__name__)


def _direct_chat(messages: list, *, max_tokens: int = 16384) -> tuple[str, str, dict]:
    """ABM.config 환경변수로 vLLM 에 직접 요청하는 최소 LLMCall 구현.

    로컬 스모크테스트 전용이라 재시도는 두지 않는다.
    """
    payload = {
        "model":       MODEL,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": 0.7,
        "stream":      False,
    }
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    r = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=API_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if "choices" not in data or not data["choices"]:
        logger.error(f"응답 구조 오류: {data}")
        raise ValueError("Invalid response structure")
    message_obj = data["choices"][0].get("message", {})
    content   = message_obj.get("content", "")
    reasoning = message_obj.get("reasoning", "") or message_obj.get("reasoning_content", "")
    usage     = data.get("usage", {})
    return content, reasoning, usage


def run(llm: LLMCall | None = None):
    agents = {
        "boss": Agent(
            "boss",
            "너는 편의점 사장님 '김민태'야. 47세 남자. 욕심이 많고 고지식함. 말투가 거칠고 불평이 많음.",
            LOG_DIR,
        ),
        "lee": Agent(
            "lee",
            "너는 편의점 알바생 '이상민'이야. 29세 남자. 아이돌 지망생. 착실하고 씩씩하며 친절한 말투.",
            LOG_DIR,
        ),
        "park": Agent(
            "park",
            "너는 편의점 알바생 '박슬기'야. 21세 여자. 게으르고 냉소적임. 말투가 거칠고 반항적임.",
            LOG_DIR,
        ),
        "customer": Agent(
            "customer",
            "너는 편의점 손님 '정용진'이야. 55세 남자. 불만이 많고 괴팍함. 말투가 거칠고 막무가내임.",
            LOG_DIR,
        ),
    }

    background_log = [
        {
            "role": "user",
            "content": (
                "[배경] 편의점 아침 출근 시간. 김민태 사장님이 도착하여 문을 열었다. "
                "이상민과 박슬기 알바생이 출근했다. 정용진 손님이 들어왔다."
            ),
        }
    ]

    print("🏪 편의점 시뮬레이션 시작\n" + "=" * 50)
    sim = Simulation(agents, background_log, LOG_DIR, llm=llm or _direct_chat)
    sim.run(start_agent="boss", max_waves=8)
