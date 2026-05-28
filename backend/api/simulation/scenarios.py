"""Scenario CRUD endpoints + default output-format helper."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter

from ...db.database import get_db
from .schemas import ScenarioSave


router = APIRouter()


@router.get("/default-output-format")
def get_default_output_format():
    from ABM.agent import DEFAULT_OUTPUT_FORMAT_TEMPLATE
    return {"template": DEFAULT_OUTPUT_FORMAT_TEMPLATE}


@router.get("/scenarios")
def list_scenarios():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, description, config_json, created_at, updated_at "
        "FROM simulation_scenarios ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [
        {**dict(r), "config": json.loads(r["config_json"])}
        for r in rows
    ]


@router.post("/scenarios", status_code=201)
def create_scenario(body: ScenarioSave):
    conn = get_db()
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO simulation_scenarios "
        "(id, name, description, config_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, body.name, body.description, body.config.model_dump_json(), now, now),
    )
    conn.commit()
    conn.close()
    return {"id": sid, "name": body.name}


@router.put("/scenarios/{sid}")
def update_scenario(sid: str, body: ScenarioSave):
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE simulation_scenarios "
        "SET name=?, description=?, config_json=?, updated_at=? WHERE id=?",
        (body.name, body.description, body.config.model_dump_json(), now, sid),
    )
    conn.commit()
    conn.close()
    return {"id": sid}


@router.delete("/scenarios/{sid}", status_code=204)
def delete_scenario(sid: str):
    conn = get_db()
    conn.execute("DELETE FROM simulation_scenarios WHERE id=?", (sid,))
    conn.commit()
    conn.close()
