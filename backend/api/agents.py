from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException

from .schemas import AgentCreate, AgentUpdate
from ..db.database import get_db

router = APIRouter(prefix="/api/agents")

# 시뮬레이션(ABM) 에이전트와 공유되는 필드의 기본값.
# 구버전 row(컬럼이 NULL)를 읽을 때도 프론트가 그대로 시뮬레이션 AgentConfig 에
# 매핑할 수 있도록 응답에서 정규화한다.
_SIM_DEFAULTS = {
    "gender":             "auto",
    "location":           "",
    "visual_description": "",
    "display_name":       "",
}


def _dump_groups(groups: list[str] | None) -> str:
    """groups 리스트 -> DB 저장용 JSON 배열 문자열."""
    return json.dumps(groups or [], ensure_ascii=False)


def _load_groups(raw) -> list[str]:
    """DB 의 JSON 문자열 -> 리스트. 구버전 NULL/손상값은 빈 리스트."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _to_agent(row: sqlite3.Row | dict) -> dict:
    """DB row -> API 응답 dict. groups 역직렬화 + 시뮬레이션 필드 정규화."""
    agent = dict(row)
    agent["groups"] = _load_groups(agent.get("groups"))
    for key, default in _SIM_DEFAULTS.items():
        if agent.get(key) is None:
            agent[key] = default
    raw_active = agent.get("initial_active")
    agent["initial_active"] = True if raw_active is None else bool(raw_active)
    return agent


@router.get("")
def list_agents():
    conn = get_db()
    rows = conn.execute("SELECT * FROM agents ORDER BY updated_at DESC").fetchall()
    conn.close()
    return [_to_agent(r) for r in rows]


@router.post("", status_code=201)
def create_agent(body: AgentCreate):
    conn = get_db()
    aid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    model = body.model.strip() if body.model else None
    conn.execute(
        """INSERT INTO agents
           (id, name, description, system_prompt, icon, model, temperature, max_tokens,
            role, goal, backstory,
            gender, "groups", location, visual_description, display_name, initial_active,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, body.name, body.description, body.system_prompt,
         body.icon, model, body.temperature, body.max_tokens,
         body.role, body.goal, body.backstory,
         body.gender, _dump_groups(body.groups), body.location,
         body.visual_description, body.display_name, int(body.initial_active),
         now, now),
    )
    conn.commit()
    agent = _to_agent(conn.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone())
    conn.close()
    return agent


@router.get("/{agent_id}")
def get_agent(agent_id: str):
    conn = get_db()
    agent = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    conn.close()
    if not agent:
        raise HTTPException(404)
    return _to_agent(agent)


@router.put("/{agent_id}")
def update_agent(agent_id: str, body: AgentUpdate):
    conn = get_db()
    agent = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not agent:
        conn.close()
        raise HTTPException(404)
    # exclude_unset: 요청 본문에 없는 필드는 건드리지 않는다(부분 업데이트).
    fields = body.model_dump(exclude_unset=True)
    # 시뮬레이션 공유 필드는 "명시적 null = 초기화"라는 의미가 없다(model 같은 기존
    # 필드와 달리 null을 보내 지운다는 개념 자체가 정의된 적이 없다) — 왕복 보존이
    # 목적인 필드라 값을 아예 안 보내는 것과 null을 보내는 것을 구분할 이유가 없으므로,
    # null이 오면 "건드리지 않음"으로 취급해 실수로 gender="auto"/initial_active=True
    # 등 기본값으로 조용히 리셋되는 걸 막는다. `model`처럼 이미 null-clear 의미가
    # 정의된 필드는 그대로 둔다.
    _PRESERVE_ONLY_FIELDS = {
        "gender", "groups", "location", "visual_description",
        "display_name", "initial_active",
    }
    for key in _PRESERVE_ONLY_FIELDS:
        if fields.get(key) is None:
            fields.pop(key, None)
    if "model" in fields:
        fields["model"] = fields["model"].strip() if fields["model"] else None
    if "groups" in fields:
        fields["groups"] = _dump_groups(fields["groups"])
    if "initial_active" in fields and fields["initial_active"] is not None:
        fields["initial_active"] = int(fields["initial_active"])
    if not fields:
        conn.close()
        return _to_agent(agent)
    now = datetime.now().isoformat()
    fields["updated_at"] = now
    # "groups" 는 SQLite 예약어이므로 식별자를 인용한다.
    set_clause = ", ".join(f'"{k}"=?' for k in fields)
    conn.execute(f"UPDATE agents SET {set_clause} WHERE id=?", [*fields.values(), agent_id])
    conn.commit()
    agent = _to_agent(conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
    conn.close()
    return agent


@router.delete("/{agent_id}", status_code=204)
def delete_agent(agent_id: str):
    conn = get_db()
    conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
    conn.commit()
    conn.close()
