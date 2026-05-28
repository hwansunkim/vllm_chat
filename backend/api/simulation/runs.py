"""Endpoints for managing past simulation runs stored in simulation.db."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .state import get_sim_db


router = APIRouter()


@router.get("/runs")
def list_runs(scenario_id: str | None = None):
    db = get_sim_db()
    return db.get_runs(scenario_id)


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    db = get_sim_db()
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/runs/{run_id}/log")
def get_run_log(run_id: str):
    db = get_sim_db()
    return db.get_run_log(run_id)


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(run_id: str):
    db = get_sim_db()
    db.delete_run(run_id)
