from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException

from api.schemas import ServerCreate, ServerUpdate
from database import get_db
from llm.registry import get_registry

router = APIRouter(prefix="/api/servers")


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["is_default"] = bool(d["is_default"])
    d["thinking"] = bool(d.get("thinking", False))
    d["max_model_len"] = d.get("max_model_len", 0)
    # 런타임 model_len: API 조회값 우선, 없으면 설정값
    provider = get_registry().get_provider(d["id"])
    d["model_len"] = provider.model_len if provider else d["max_model_len"]
    return d


@router.get("")
def list_servers():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM servers ORDER BY is_default DESC, created_at"
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


@router.post("", status_code=201)
async def create_server(body: ServerCreate):
    conn = get_db()
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()

    # is_default 설정 시 기존 default 해제
    if body.is_default:
        conn.execute("UPDATE servers SET is_default=0")

    conn.execute(
        """INSERT INTO servers (id, name, base_url, model, weight, enabled, is_default, thinking, max_model_len, created_at)
           VALUES (?,?,?,?,?,1,?,?,?,?)""",
        (sid, body.name, body.base_url.rstrip("/"), body.model,
         body.weight, int(body.is_default), int(body.thinking), body.max_model_len, now),
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone())
    conn.close()

    # 레지스트리에 즉시 등록 후 model_len 조회
    provider = await get_registry().register(row)
    if provider:
        await provider.fetch_model_len()
    return _row_to_dict(row)


@router.get("/{server_id}")
def get_server(server_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    return _row_to_dict(row)


@router.put("/{server_id}")
async def update_server(server_id: str, body: ServerUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404)

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        conn.close()
        return _row_to_dict(existing)

    # is_default 설정 시 기존 default 해제
    if fields.get("is_default"):
        conn.execute("UPDATE servers SET is_default=0 WHERE id!=?", (server_id,))

    if "base_url" in fields:
        fields["base_url"] = fields["base_url"].rstrip("/")

    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE servers SET {set_clause} WHERE id=?",
        [*fields.values(), server_id],
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone())
    conn.close()

    # 레지스트리 반영 (enabled=False면 unregister, 그 외엔 재등록)
    if not row.get("enabled", True):
        await get_registry().unregister(server_id)
    else:
        provider = await get_registry().register(row)
        if provider:
            await provider.fetch_model_len()
    return _row_to_dict(row)


@router.delete("/{server_id}", status_code=204)
async def delete_server(server_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404)
    conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
    conn.commit()
    conn.close()
    await get_registry().unregister(server_id)


@router.get("/{server_id}/health")
async def server_health(server_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    provider = get_registry().get_provider(server_id)
    if provider is None:
        return {"healthy": False, "reason": "서버가 비활성화 상태입니다."}
    healthy = await provider.health_check()
    return {"healthy": healthy, "server_id": server_id, "name": row["name"]}
