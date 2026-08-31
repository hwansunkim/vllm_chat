from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException

from .schemas import (
    ModelProbeRequest,
    ModelProbeResponse,
    ProbedModel,
    ServerCreate,
    ServerHealth,
    ServerUpdate,
)
from .. import config
from ..db.database import get_db
from ..llm.registry import get_provider_class, get_registry

router = APIRouter(prefix="/api/servers")


def _mask_api_key(key: str | None) -> str:
    """응답 직렬화용 마스킹. DB 에는 항상 평문이 저장된다.

    - 빈 값        → "" (프론트에서 '키 없음' 으로 표시)
    - 8자 미만     → "***"
    - 8자 이상     → 앞 4자 + "..." + 뒤 4자   (예: sk-a...b3f2)
    """
    if not key:
        return ""
    if len(key) < 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def _row_thinking_level(row: dict) -> str:
    """행에서 사고 수준을 읽는다. thinking_level 이 소스 오브 트루스이고,
    컬럼이 없거나 값이 비정상인 구 DB 는 구 `thinking` bool 로 폴백한다."""
    return config.normalize_thinking_level(
        row.get("thinking_level"),
        default=config.normalize_thinking_level(row.get("thinking")),
    )


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["is_default"] = bool(d["is_default"])
    d["thinking_level"] = _row_thinking_level(d)
    # 하위호환: 구 프론트/외부 소비자를 위해 bool 파생값을 계속 내보낸다.
    d["thinking"] = d["thinking_level"] != "off"
    d["max_model_len"] = d.get("max_model_len", 0)
    d["provider_type"] = d.get("provider_type") or "vllm"
    d["api_key"] = _mask_api_key(d.get("api_key"))
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

    if body.is_default:
        conn.execute("UPDATE servers SET is_default=0")

    conn.execute(
        """INSERT INTO servers (id, name, base_url, model, provider_type, api_key, weight, enabled, is_default, thinking, thinking_level, max_model_len, created_at)
           VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?)""",
        (sid, body.name, body.base_url.rstrip("/"), body.model, body.provider_type, body.api_key,
         body.weight, int(body.is_default),
         int(body.thinking_level != "off"), body.thinking_level,
         body.max_model_len, now),
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone())
    conn.close()

    provider = await get_registry().register(row)
    if provider:
        await provider.fetch_model_len()
    return _row_to_dict(row)


@router.post("/probe-models", response_model=ModelProbeResponse)
async def probe_models(body: ModelProbeRequest):
    """저장 전 서버 설정으로 사용 가능한 모델 목록을 조회한다 (DB 미변경).

    provider 를 임시 인스턴스화해서 목록만 읽고 즉시 닫는다. registry 에는
    등록하지 않으므로 이 호출은 실행 중인 서버 풀에 아무 영향을 주지 않는다.
    반환 목록은 필터링·정렬 없이 API 원본 순서 그대로다(선별은 프론트 몫).
    """
    cls = get_provider_class(body.provider_type)
    if cls is None:
        raise HTTPException(400, detail=f"알 수 없는 provider_type: {body.provider_type!r}")

    api_key = body.api_key.strip()
    if not api_key and body.server_id:
        conn = get_db()
        existing = conn.execute(
            "SELECT api_key FROM servers WHERE id=?", (body.server_id,)
        ).fetchone()
        conn.close()
        if existing and existing["api_key"]:
            api_key = existing["api_key"]

    provider = cls(
        server_id="__probe__",
        name=f"probe:{body.provider_type}",
        base_url=body.base_url.strip().rstrip("/"),
        model="",
        api_key=api_key,
        enabled=False,
        is_default=False,
        thinking_level="off",
        configured_max_len=0,
    )
    try:
        # vLLM 은 기본 주소가 없다. 빈 주소로 조회하면 상대 URL 이 되어
        # 일반적인 "조회 실패" 로 뭉개지므로 원인을 분명히 알려준다.
        effective_base_url = provider.base_url
        if not effective_base_url:
            raise HTTPException(400, detail="서버 주소(base_url)를 입력하세요.")
        entries = await provider.list_models()
    finally:
        await provider.close()

    if entries is None:
        raise HTTPException(
            400,
            detail="모델 목록을 가져오지 못했습니다. API 키와 주소를 확인하세요.",
        )

    models = [
        ProbedModel(id=e["id"], context_length=e.get("max_model_len") or None)
        for e in entries
        if isinstance(e.get("id"), str) and e["id"]
    ]
    return ModelProbeResponse(
        provider_type=body.provider_type,
        base_url=effective_base_url,
        models=models,
    )


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

    # 사고 수준: thinking_level 이 소스 오브 트루스. 구 `thinking` bool 은
    # 스키마 validator 가 이미 thinking_level 로 승격했으므로 여기서는 파생 컬럼을
    # 동기화하기만 한다(NOT NULL 컬럼에 None 이 흘러들지 않도록 방어).
    if fields.get("thinking_level") is None:
        fields.pop("thinking_level", None)
        fields.pop("thinking", None)
    else:
        fields["thinking"] = int(fields["thinking_level"] != "off")

    # api_key 는 GET 응답에서 마스킹되므로, 프론트가 되돌려 보낸 값을 그대로 저장하면 안 된다.
    # 계약: 빈 문자열 = "변경 안 함"(기존 키 유지). 실제 새 키를 넣었을 때만 갱신된다.
    # 방어적으로, 기존 키의 마스킹 결과와 똑같은 값이 들어와도 "변경 안 함"으로 본다.
    if "api_key" in fields:
        submitted = fields["api_key"]
        if not submitted or submitted == _mask_api_key(existing["api_key"] if "api_key" in existing.keys() else ""):
            fields.pop("api_key")

    if not fields:
        conn.close()
        return _row_to_dict(existing)

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


@router.get("/{server_id}/health", response_model=ServerHealth)
async def server_health(server_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)

    base = {"server_id": server_id, "name": row["name"], "model": row["model"]}

    provider = get_registry().get_provider(server_id)
    if provider is None:
        return ServerHealth(
            **base,
            status="disabled",
            healthy=False,
            reachable=False,
            model_ok=False,
            detail="서버가 비활성화 상태입니다.",
        )

    report = await provider.health_status()
    return ServerHealth(**base, **report.to_dict())
