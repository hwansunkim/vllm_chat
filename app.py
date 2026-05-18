from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
import state
import llm.client as llm_client
from api import agents, conversations, memories
from database import get_db, init_tables, migrate_db, seed_default_agents


@asynccontextmanager
async def lifespan(app: FastAPI):
    # HTTP 클라이언트 초기화 (앱 전체 공유)
    llm_client.setup_client()

    conn = get_db()
    init_tables(conn)
    migrate_db(conn)
    seed_default_agents(conn)
    conn.close()

    state.max_model_len = await llm_client.async_get_model_context_limit()

    yield

    # 종료 시 HTTP 클라이언트 정리
    await llm_client.teardown_client()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(conversations.router)
app.include_router(agents.router)
app.include_router(memories.router)


@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/api/model/status")
def model_status():
    return {
        "model": config.MODEL,
        "base_url": config.BASE_URL,
        "max_model_len": state.max_model_len,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8888, reload=True)
