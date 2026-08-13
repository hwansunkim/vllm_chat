import asyncio

max_model_len: int = 0

# FastAPI 메인 이벤트 루프. lifespan()이 설정한다.
# 워커 스레드(ABM 시뮬레이션 등)가 provider 코루틴을 이 루프에 위탁 실행하는 데 쓴다.
event_loop: asyncio.AbstractEventLoop | None = None
