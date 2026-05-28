"""SSE streaming endpoint and helpers."""
from __future__ import annotations

import asyncio
import json
import queue

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .state import _sim


router = APIRouter()


def _blocking_get(q: queue.Queue) -> dict | None:
    """SSE generator's blocking queue get (30s timeout returns a ping)."""
    try:
        return q.get(timeout=30)
    except queue.Empty:
        return {"type": "ping", "data": {}}


@router.get("/stream")
async def stream_events():
    q = _sim.get("event_queue")

    async def _gen():
        if q is None:
            yield 'event: error\ndata: {"message":"no active simulation"}\n\n'
            return
        loop = asyncio.get_running_loop()
        while True:
            item = await loop.run_in_executor(None, _blocking_get, q)
            if item is None:  # sentinel
                yield "event: simulation_end\ndata: {}\n\n"
                break
            yield (
                f"event: {item['type']}\n"
                f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
