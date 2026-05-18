from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import state
import llm.client as llm_client
from api import agents, conversations, memories, servers
from database import get_db, init_tables, migrate_db, seed_default_agents, seed_default_servers
from llm.registry import get_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_db()
    init_tables(conn)
    migrate_db(conn)
    seed_default_agents(conn)
    seed_default_servers(conn)           # servers.json 초기 시드
    await llm_client.setup(conn)         # DB → ServerRegistry 로드
    conn.close()

    state.max_model_len = await llm_client.async_get_model_context_limit()

    yield

    await llm_client.teardown()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(conversations.router)
app.include_router(agents.router)
app.include_router(memories.router)
app.include_router(servers.router)


@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/api/model/status")
def model_status():
    registry = get_registry()
    default = registry.get_default()
    servers = [
        {
            "id": p.id,
            "name": p.name,
            "model": p.model,
            "model_len": p.model_len,
            "is_default": p.is_default,
            "enabled": p.enabled,
        }
        for p in registry.list_providers()
    ]
    return {
        "model": default.model if default else None,
        "base_url": default.base_url if default else None,
        "max_model_len": default.model_len if default else state.max_model_len,
        "current_server": {
            "id": default.id,
            "name": default.name,
        } if default else None,
        "servers": servers,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8888, reload=True)
