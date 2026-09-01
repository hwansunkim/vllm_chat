"""Simulation API package.

Public surface intentionally mirrors the legacy ``backend.api.simulation``
module: ``router`` is the only symbol ``backend.main`` imports.
"""
from __future__ import annotations

from fastapi import APIRouter

from .contract import router as contract_router
from .runs import router as runs_router
from .runtime import router as runtime_router
from .scenarios import router as scenarios_router
from .sse import router as sse_router


router = APIRouter(prefix="/api/simulation")
router.include_router(runtime_router)
router.include_router(sse_router)
router.include_router(runs_router)
router.include_router(scenarios_router)
router.include_router(contract_router)


__all__ = ["router"]
