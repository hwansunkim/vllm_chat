from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException

from api.schemas import AgentCreate, AgentUpdate
from database import get_db

router = APIRouter(prefix="/api/agents")


@router.get("")
def list_agents():
    conn = get_db()
    rows = conn.execute("SELECT * FROM agents ORDER BY updated_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
def create_agent(body: AgentCreate):
    conn = get_db()
    aid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    model = body.model.strip() if body.model else None
    conn.execute(
        """INSERT INTO agents
           (id, name, description, system_prompt, icon, model, temperature, max_tokens,
            role, goal, backstory, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, body.name, body.description, body.system_prompt,
         body.icon, model, body.temperature, body.max_tokens,
         body.role, body.goal, body.backstory, now, now),
    )
    conn.commit()
    agent = dict(conn.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone())
    conn.close()
    return agent


@router.get("/{agent_id}")
def get_agent(agent_id: str):
    conn = get_db()
    agent = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    conn.close()
    if not agent:
        raise HTTPException(404)
    return dict(agent)


@router.put("/{agent_id}")
def update_agent(agent_id: str, body: AgentUpdate):
    conn = get_db()
    agent = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not agent:
        conn.close()
        raise HTTPException(404)
    fields = body.model_dump(exclude_unset=True)
    if "model" in fields:
        fields["model"] = fields["model"].strip() if fields["model"] else None
    if not fields:
        conn.close()
        return dict(agent)
    now = datetime.now().isoformat()
    fields["updated_at"] = now
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE agents SET {set_clause} WHERE id=?", [*fields.values(), agent_id])
    conn.commit()
    agent = dict(conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
    conn.close()
    return agent


@router.delete("/{agent_id}", status_code=204)
def delete_agent(agent_id: str):
    conn = get_db()
    conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
    conn.commit()
    conn.close()
