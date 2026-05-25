from ..agent import Agent
from ..simulation import Simulation
from ..config import LOG_DIR, MODEL, BASE_URL, API_TIMEOUT


def run():
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
    sim = Simulation(agents, background_log, LOG_DIR, MODEL, BASE_URL, API_TIMEOUT)
    sim.run(start_agent="boss", max_waves=8)
