"""엔진 프롬프트 계약(prompt contract) 읽기 전용 프리뷰.

계약 층 — 출력 JSON 스키마, `move_to` 의미, target ID 규칙, 위치/zone/외부공간
규칙, 시간 인식, 감염 증상 읽는 법 — 은 **저장되지 않는다.** 엔진이 소유하고
실행 시점에 config 에서 생성한다(`ABM/prompt_contract.py`). 그래야 엔진을
업그레이드했을 때 기존 시나리오도 DB 마이그레이션 없이 새 계약을 받는다.

그 대신 설정 UI 가 "지금 설정이면 무엇이 주입되는가"를 보여줄 수 있어야 해서,
같은 빌더를 그대로 노출한다. `Simulation` 인스턴스를 만들지 않으므로 LLM 호출도,
DB 쓰기도, 에이전트 로그 파일 생성도 없는 순수 함수 호출이다.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .schemas import (
    ContractFlags,
    ContractPreviewRequest,
    ContractPreviewResponse,
)


router = APIRouter()

# 실제 실행에서 <TARGETS> 는 매 턴 "같은 자리에 있는 사람들"로 채워진다. 프리뷰는
# 그 자리가 어떤 모양인지만 보여주면 되므로 자리표시자 하나를 쓴다.
_PLACEHOLDER_TARGET = "agent_id"
_PLACEHOLDER_ALIAS  = "표시 이름"


@router.post("/contract-preview", response_model=ContractPreviewResponse)
def preview_engine_contract(body: ContractPreviewRequest):
    """현재 설정으로 엔진이 만들 계약 블록을 그대로 돌려준다(읽기 전용)."""
    from ABM.prompt_contract import (
        build_engine_contract, build_world_contract, verify_contract,
    )

    # LocationNode 리스트 → 빌더가 받는 인접 리스트/집합/맵 (abm_done §6-C3)
    graph    = {n.name: list(n.connects_to) for n in body.location_graph}
    exterior = {n.name for n in body.location_graph if n.is_exterior}
    zones    = {n.name: n.zone for n in body.location_graph if n.zone}

    targets = list(body.available_targets) or [_PLACEHOLDER_TARGET]
    aliases = dict(body.key_to_alias) or {_PLACEHOLDER_TARGET: _PLACEHOLDER_ALIAS}

    kwargs = dict(
        extra_fields           = [f.model_dump() for f in body.extra_fields],
        available_targets      = targets,
        key_to_alias           = aliases,
        situation_targets      = body.situation_targets,
        location_graph         = graph or None,
        exterior_locations     = exterior or None,
        location_zone          = zones or None,
        time_enabled           = body.time_enabled(),
        infection_enabled      = bool(body.infection_model.enabled),
        disease_name           = body.infection_model.disease_name,
        output_format_override = body.output_format_override or None,
    )

    try:
        world = build_world_contract(
            location_graph     = kwargs["location_graph"],
            exterior_locations = kwargs["exterior_locations"],
            location_zone      = kwargs["location_zone"],
            time_enabled       = kwargs["time_enabled"],
            infection_enabled  = kwargs["infection_enabled"],
            disease_name       = kwargs["disease_name"],
        )
        contract = build_engine_contract(
            include_output_schema=body.include_output_schema, **kwargs,
        )
    except Exception as e:                        # 빌더는 순수 함수라 여기 오면 설정 문제다
        raise HTTPException(400, f"계약을 생성할 수 없습니다: {e}")

    # build_engine_contract 는 world + output 을 이어 붙인다. 잘라내서 두 조각을
    # 따로 보여주되, 합친 문자열은 항상 실제 주입본과 글자 단위로 같게 유지한다.
    output = contract[len(world):] if contract.startswith(world) else ""

    flags = ContractFlags(
        has_location_graph    = bool(graph),
        has_zone              = bool(zones),
        time_enabled          = kwargs["time_enabled"],
        infection_enabled     = kwargs["infection_enabled"],
        include_output_schema = body.include_output_schema,
    )
    warnings = verify_contract(contract, **flags.model_dump())

    return ContractPreviewResponse(
        contract        = contract,
        world_contract  = world,
        output_contract = output,
        flags           = flags,
        warnings        = warnings,
    )
