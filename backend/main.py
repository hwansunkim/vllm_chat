from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import state
from .llm import client as llm_client
from .api import agents, conversations, memories, servers, model, simulation
from .db.database import get_db, init_tables, migrate_db, seed_default_agents, seed_default_servers


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_db()
    init_tables(conn)
    migrate_db(conn)
    seed_default_agents(conn)
    seed_default_servers(conn)
    await llm_client.setup(conn)
    conn.close()

    state.max_model_len = await llm_client.async_get_model_context_limit()

    yield

    await llm_client.teardown()


app = FastAPI(lifespan=lifespan)

app.include_router(model.router)
app.include_router(conversations.router)
app.include_router(agents.router)
app.include_router(memories.router)
app.include_router(servers.router)
app.include_router(simulation.router)

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
