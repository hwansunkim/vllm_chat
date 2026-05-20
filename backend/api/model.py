from __future__ import annotations

from fastapi import APIRouter

from .. import state
from ..llm.registry import get_registry

router = APIRouter()


@router.get("/api/model/status")
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
            "thinking": p.thinking,
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
            "thinking": default.thinking,
        } if default else None,
        "servers": servers,
    }
