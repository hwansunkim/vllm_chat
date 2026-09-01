"""Scenario CRUD endpoints + default output-format helper."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter

from ...db.database import get_db
from .schemas import ScenarioSave, SimStartConfig


router = APIRouter()


def _config_json(cfg: SimStartConfig) -> str:
    """시나리오 저장용 config JSON. 구 프리즈 필드는 기록하지 않는다.

    `output_format_template` 은 예전에 시나리오 생성 시점의 전체 출력 템플릿을
    통째로 스냅샷하던 필드다. 이제 계약 층은 엔진이 실행 시점에 만들고 이 값은
    런타임에서 무조건 무시되므로, 새로 저장할 때 죽은 지시어를 남길 이유가 없다.
    (오버라이드는 `output_format_override` 로만 저장된다.)
    """
    return cfg.model_copy(update={"output_format_template": ""}).model_dump_json()


@router.get("/default-output-format", deprecated=True)
def get_default_output_format():
    """(구) 출력 템플릿 스냅샷 제공 엔드포인트 — 이제 빈 값만 돌려준다.

    출력 계약을 시나리오에 프리즈하면 엔진에 기능을 추가해도 기존 시나리오는 옛
    지시어에 묶여 그 기능이 조용히 죽는다. 게다가 현재 템플릿에는
    `<MOVE_TO_HINT>` 같은 자리표시자가 들어 있어 그대로 프리즈하면 미치환
    문자열이 저장된다. 기본값은 **빈 문자열**이며(= 엔진이 매 실행 생성),
    계약을 눈으로 보려면 `POST /api/simulation/contract-preview` 를 쓸 것.

    라우트 자체는 옛 프론트가 404 를 만나지 않도록 남겨 둔다.
    """
    return {"template": ""}


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
        (sid, body.name, body.description, _config_json(body.config), now, now),
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
        (body.name, body.description, _config_json(body.config), now, sid),
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
