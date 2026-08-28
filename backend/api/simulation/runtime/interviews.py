"""사후 인터뷰 (post-simulation interview) 엔드포인트.

인터뷰는 끝난 run 을 대상으로 하는 읽기 전용 회고다. 여기서 오간 발화는
`interview_log` 에만 저장되며 `simulation_log` 나 에이전트 워킹 메모리에는
절대 들어가지 않는다 — /runs/{id}/log, /load, /resume 결과가 오염되지 않도록.

프롬프트 조립 자체는 이웃 모듈 ``..interview`` 가 담당하고, 여기서는 HTTP
계층(상태 검증 · 토큰 한도 판정 · LLM 호출 · 기록)만 다룬다.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from fastapi import APIRouter, HTTPException

from ..interview import (
    build_interview_messages,
    effective_token_limit,
    extract_answer,
    resolve_server_id,
    resolve_temperature,
)
from ..schemas import InterviewRecord, InterviewRequest, SimStartConfig
from ..state import get_sim_db
from .llm_config import _make_llm


logger = logging.getLogger(__name__)
router = APIRouter()


# 인터뷰를 허용할 run 상태. 'running' 은 아직 agent_snapshots 가 기록되지 않아
# (finalize_run 시점에 저장) 기억 상태를 신뢰할 수 없으므로 제외한다.
_INTERVIEWABLE_STATUS = ("done", "stopped", "error")


def _parse_run_config(run: dict) -> SimStartConfig:
    """run 의 config 스냅샷을 SimStartConfig 로 복원. 스냅샷이 없으면 400."""
    try:
        cfg_dict = json.loads(run.get("config_json") or "{}")
        if not cfg_dict.get("agents"):
            raise ValueError("empty config")
        return SimStartConfig(**cfg_dict)
    except Exception:
        raise HTTPException(400, "이 실행은 설정 스냅샷이 없어 인터뷰할 수 없습니다")


@router.post("/runs/{run_id}/agents/{name}/interview", response_model=InterviewRecord)
def create_agent_interview(run_id: str, name: str, body: InterviewRequest):
    """끝난 run 의 에이전트에게 질문하고 답변을 interview_log 에 저장한다."""
    db  = get_sim_db()
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    status = run.get("status") or ""
    if status not in _INTERVIEWABLE_STATUS:
        raise HTTPException(
            409,
            f"인터뷰는 종료된 시뮬레이션에서만 가능합니다 (현재 상태: {status or 'unknown'})",
        )

    cfg = _parse_run_config(run)
    if name not in {a.name for a in cfg.agents}:
        raise HTTPException(404, f"Agent '{name}' not found in run {run_id}")

    # 프롬프트 상한은 run 설정 token_limit, 단 모델 컨텍스트를 아는 경우 답변
    # max_tokens 만큼 더 조인다. 컨텍스트 초과 400 → 502 → 무의미한 재시도 방지.
    server_id   = resolve_server_id(cfg, name)
    # 인터뷰는 자체 온도를 갖지 않고 실행 당시 설정(에이전트 → 실행 기본값)을 그대로 쓴다.
    temperature = resolve_temperature(cfg, name)
    max_tokens = body.max_tokens or cfg.llm_max_tokens
    token_limit = effective_token_limit(cfg, server_id, max_tokens)

    try:
        messages = build_interview_messages(
            run_id, name, body.question, body.mode, cfg, db, token_limit=token_limit,
        )
    except KeyError:
        raise HTTPException(404, f"Agent '{name}' not found in run {run_id}")
    except Exception as e:
        logger.exception("인터뷰 컨텍스트 조립 실패 (run=%s, agent=%s)", run_id, name)
        raise HTTPException(500, f"인터뷰 컨텍스트 조립 실패: {e}")

    from ABM.agent import _estimate_tokens
    est_tokens = sum(_estimate_tokens(m.get("content", "")) + 4 for m in messages)
    if est_tokens > token_limit:
        # 기록·기억을 다 잘라내고도 넘친다 = 페르소나/배경 자체가 한도를 넘는다.
        # 재시도해도 결과가 같으므로 재시도 가능한 502 가 아니라 400 으로 알린다.
        logger.warning(
            "인터뷰 컨텍스트가 한도 초과 (run=%s, agent=%s, est=%d > %d)",
            run_id, name, est_tokens, token_limit,
        )
        raise HTTPException(
            400,
            f"인터뷰 컨텍스트가 모델 한도를 넘습니다 (추정 {est_tokens:,} > {token_limit:,} 토큰). "
            "에이전트 설정의 페르소나/배경이 너무 길어 재시도해도 결과가 같습니다.",
        )

    llm = _make_llm(server_id, temperature)
    try:
        content, _reasoning, usage = llm(messages, max_tokens=max_tokens)
    except Exception as e:
        logger.warning("인터뷰 LLM 호출 실패 (run=%s, agent=%s): %s", run_id, name, e)
        raise HTTPException(502, f"LLM 호출 실패: {e}")

    answer = extract_answer(content)
    if not answer:
        raise HTTPException(502, "LLM이 빈 답변을 반환했습니다")

    try:
        return db.log_interview(
            run_id, name, body.mode, body.question, answer,
            meta={
                "est_prompt_tokens": est_tokens,
                "token_limit":       token_limit,
                "context_messages":  len(messages),
                "server_id":         server_id,
                "temperature":       temperature,
                "usage":             usage or {},
            },
        )
    except sqlite3.IntegrityError:
        # interview_log.run_id 의 FK 위반 — LLM 호출 중에 run 이 삭제된 경우.
        raise HTTPException(404, "Run not found")


@router.get("/runs/{run_id}/agents/{name}/interview", response_model=list[InterviewRecord])
def list_agent_interviews(run_id: str, name: str):
    """해당 run/agent 의 인터뷰 기록을 오래된 순으로 반환."""
    db = get_sim_db()
    if db.get_run(run_id) is None:
        raise HTTPException(404, "Run not found")
    return db.get_interviews(run_id, name)
