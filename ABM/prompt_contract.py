"""엔진 계약 층 (Engine Contract Layer) — LLM 프롬프트 중 **엔진이 소유하는 부분**.

프롬프트는 두 층으로 나뉜다:

| 층      | 소유자 | 내용                                                        | 저장   |
|---------|--------|-------------------------------------------------------------|--------|
| 서사 층 | 사용자 | 페르소나, 배경, 세계 설정, 감독 노트                        | 시나리오에 저장 |
| 계약 층 | 엔진   | JSON 출력 스키마, `move_to` 의미, target ID 규칙, 위치/zone/ | **저장 안 함** |
|         |        | 외부공간 규칙, 시간 인식 포맷, 감염 증상 포맷               | (실행 시 생성) |

계약 층은 **코드가 파싱·동작하는 방식에 의해 강제**되므로 사용자 편집 대상이 아니다.
DB에 스냅샷으로 얼려두면 엔진에 기능을 추가해도(예: `move_to` 사람 지목 →
rendezvous) 기존 시나리오는 옛 지시어를 그대로 들고 있어 기능이 **조용히 죽는다**.
그래서 이 모듈은 실행 시점의 config만 보고 계약 문자열을 새로 만든다 — 엔진을
업그레이드하면 기존 시나리오 전부가 DB 마이그레이션 없이 새 계약을 받는다.

### 왜 `ABM/simulation/` 이 아니라 여기인가
`ABM/agent.py`가 이 모듈을 써야 하는데, `ABM/simulation/__init__.py`는
`from .core import Simulation`을, `core.py`는 `from ..agent import Agent`를 한다.
계약 모듈을 `ABM/simulation/` 아래에 두면 `ABM/__init__.py`의
`from .agent import Agent` 시점에 순환 import가 난다. 이 모듈은 ABM 안의 어떤
모듈도 import 하지 않아(순수 문자열 빌더) 어느 쪽에서든 안전하게 쓸 수 있다.

### 조립 순서 (recency — 계약이 맨 뒤)
    [사용자 system_prompt: 페르소나 + 배경]
      + [엔진: 위치/zone/외부공간 규칙]   (위치 그래프 활성 시)
      + [엔진: 시간 인식 포맷]            (시간 개념 활성 시)
      + [엔진: 감염 증상 포맷]            (감염 모델 활성 시)
      + [엔진: 출력 계약 (스키마 + move_to + target)]  (항상, 단 인터뷰 모드 제외)

앞의 세 블록은 시뮬레이션 수명 동안 고정이라(`build_world_contract`) Agent에
한 번 붙여두고, 마지막 출력 계약은 매 턴 타깃이 달라지므로
(`build_output_contract`) `Agent.get_system_message()`가 호출 때마다 만든다.
"""
from __future__ import annotations

# ── 출력 계약 템플릿 ──────────────────────────────────────────────────────────
#
# `<...>` 자리표시자는 `build_output_contract()`가 채운다:
#   <FIELD_LINES>    extra_fields의 JSON 라인
#   <FIELD_HINTS>    extra_fields의 설명 라인
#   <MOVE_TO_HINT>   move_to 의미 (위치 그래프/zone 유무에 따라 조건부)
#   <TARGETS>        지목 가능한 시스템 ID 목록
#   <TARGETS_FOOTER> all/self/group 단축 표기
#
# 하위 호환: 사용자가 **명시적으로** 넘긴 오버라이드 템플릿에는 `<MOVE_TO_HINT>`가
# 없을 수 있다(구버전 프리즈 템플릿). 그 경우 치환이 그냥 no-op이 되고 템플릿에
# 하드코딩된 move_to 문구가 그대로 쓰인다.
DEFAULT_OUTPUT_FORMAT_TEMPLATE = """

[Important Output Format]
당신의 응답은 반드시 다음 JSON 형식이어야 합니다. 다른 텍스트는 출력하지 마세요.
{
    "content": "당신의 말이나 행동을 자신의 말투로 (반드시 한국어로만)",
    "action_note": "행동이나 생각, 상황 묘사. 텍스트로 서술. 예: '한숨을 쉰다', '눈을 흘김'",
<FIELD_LINES>
    "target": ["id1", "id2"] 또는 "all" 또는 "self",
    "move_to": null,
    "update_appearance": null
}

- content: 당신의 말, 대사. **반드시 한국어로만 작성. 중국어 한자·영어 등 외국어 절대 금지.**
- action_note: 행동이나 생각 묘사. 이 내용은 다른 에이전트에게 **시각적 정보**로 전달됨.
<FIELD_HINTS>
<MOVE_TO_HINT>
- update_appearance: 외모 변화가 있을 때 새 외모 전체 묘사 (없으면 null)
- target: 반드시 아래 시스템 ID만 사용 (표시 이름 절대 금지):
<TARGETS><TARGETS_FOOTER>
⚠ content 필드는 반드시 한국어로만 작성하십시오. 외국어·한자 사용 금지.
"""

# ── move_to 의미 (조건부) ─────────────────────────────────────────────────────

# 위치 그래프가 없는 레거시 시나리오. `_MeetingMixin._is_location_name()`이
# 그래프 미설정 시 **항상 True**를 돌려주므로 사람 지목 해석 자체가 일어나지
# 않는다 — 여기서 사람 ID를 안내하면 동작하지 않는 기능을 광고하는 셈이다.
_MOVE_TO_LEGACY = '- move_to: 이동할 위치 이름 (이동 없으면 null)'

_MOVE_TO_GRAPH = (
    '- move_to: 위 [위치 그래프]의 장소명, **또는 만나러 갈 사람의 ID**. '
    '사람 ID를 넣으면 그 사람이 있는 곳까지 알아서 따라갑니다. 상대가 이동 중이면 '
    '그가 도착할 곳으로 가고, 상대도 당신을 만나러 오는 중이면 엇갈리지 않도록 '
    '중간 지점에서 만납니다. 다음 발화에서 다른 장소나 다른 사람을 넣으면 그 즉시 '
    '취소됩니다. 이동 없으면 null'
)

_MOVE_TO_ZONE_SUFFIX = (
    '\n  ※ 다른 장소에 있는 사람에게 말을 걸고 싶다면 move_to에 **그 사람의 ID**를 '
    '넣으세요(장소명이 아니라). 그래야 상대가 움직여도 놓치지 않습니다.'
)


def build_move_to_hint(*, has_location_graph: bool = False, has_zone: bool = False) -> str:
    """`move_to` 필드의 의미 설명 한 덩어리.

    - 그래프 없음 → 레거시("이동할 위치 이름")
    - 그래프 있음 → 장소명 또는 사람 ID(추격/랑데부 의미 포함)
    - zone 있음   → 위 + "다른 장소의 사람은 장소명이 아니라 그 사람 ID로 지목"
    """
    if not has_location_graph:
        return _MOVE_TO_LEGACY
    hint = _MOVE_TO_GRAPH
    if has_zone:
        hint += _MOVE_TO_ZONE_SUFFIX
    return hint


# ── 위치 / zone / 외부 공간 계약 ──────────────────────────────────────────────

_MAP_RULE_GRAPH_ONLY = (
    "※ move_to 필드에는 위 그래프에 있는 장소명을 사용할 것. "
    "그래프에 없는 장소로의 이동은 무시됩니다."
)
_MAP_RULE_PERSON = (
    "※ move_to에 **사람의 ID**를 넣으면 그 사람이 있는 곳까지 알아서 따라갑니다. "
    "상대가 이동 중이면 그가 도착할 곳으로 갑니다. 상대도 당신을 만나러 오는 중이라면 "
    "서로 엇갈리지 않도록 중간에서 만나게 됩니다. 마음이 바뀌면 다음 발화의 move_to에 "
    "다른 장소나 사람을 넣으세요 — 그 즉시 취소됩니다."
)
_MAP_RULE_EXTERIOR = (
    "※ [외부 공간]으로 표시된 장소는 시뮬레이션 경계 밖입니다. "
    "그곳에서는 다른 누구도 볼 수 없고, 누구도 당신을 볼 수 없습니다."
)
_MAP_RULE_ZONE = (
    "※ [구역: ...]은 같은 생활권을 뜻합니다. 같은 구역 안의 다른 장소에 있는 사람은 "
    "서로 존재를 인지하지만, 대화는 같은 장소에 있어야만 할 수 있습니다. "
    "말을 걸고 싶다면 move_to에 그 사람의 ID(또는 그 장소명)를 넣으세요."
)
_MAP_RULE_ZONE_EXIT = (
    "※ 구역(집·회사 등) 안에서는 어느 장소에서든 바깥으로 바로 나갈 수 있습니다"
    "(현관까지 걸어갈 필요 없음). 바깥에서 구역으로 들어올 때는 입구를 거칩니다."
)


def build_map_contract(
    *,
    location_graph:     dict[str, list[str]] | None = None,
    exterior_locations: set[str] | None             = None,
    location_zone:      dict[str, str] | None       = None,
    zone_entry:         dict[str, str] | None       = None,
) -> str:
    """[위치 그래프] 블록 + 이동/외부공간/구역 규칙. 그래프가 없으면 빈 문자열.

    `zone_entry` (zone -> 입구 노드명)가 주어지면 입구 노드에 ", 입구" 표기를 붙이고
    구역 밖으로 바로 나갈 수 있다는 규칙 한 줄을 추가한다. 탈출 엣지 자체는 이미
    전개된 `location_graph` 에 들어 있어 [위치 그래프] 블록에 자동 노출된다.
    """
    if not location_graph:
        return ""
    exterior = exterior_locations or set()
    zones    = location_zone or {}
    entries  = zone_entry or {}

    lines = ["\n\n[위치 그래프 — 이동 가능한 경로]"]
    for loc, conns in location_graph.items():
        conn_str      = ", ".join(conns) if conns else "(연결 없음)"
        exterior_mark = " [외부 공간]" if loc in exterior else ""
        zone_name     = zones.get(loc, "")
        if zone_name:
            is_entry  = entries.get(zone_name) == loc
            zone_mark = f" [구역: {zone_name}{', 입구' if is_entry else ''}]"
        else:
            zone_mark = ""
        lines.append(f"  {loc}{exterior_mark}{zone_mark}: {conn_str}")
    lines.append(_MAP_RULE_GRAPH_ONLY)
    lines.append(_MAP_RULE_PERSON)
    if exterior:
        lines.append(_MAP_RULE_EXTERIOR)
    if zones:
        lines.append(_MAP_RULE_ZONE)
    if entries:
        lines.append(_MAP_RULE_ZONE_EXIT)
    return "\n".join(lines)


# ── 시간 인식 계약 ────────────────────────────────────────────────────────────

_TIME_CONTRACT = (
    "\n\n[시간 인식]\n"
    "매 대화 맥락에 [현재 시각: 요일 + 오전/오후 시각] 정보가 제공됩니다. "
    "이를 자연스럽게 인지하고 시간대에 맞는 행동을 하세요. "
    "예) 점심 시간엔 식사를 제안하거나, 퇴근 시간이 다가오면 마무리 행동을 취하는 등.\n"
    "요일도 함께 고려하세요. 평일(월~금)과 주말(토·일)의 일상은 다릅니다. "
    "예) 평일 아침엔 출근·등교를 준비하고, 주말엔 늦잠을 자거나 여가·약속 위주로 움직이는 등. "
    "자정을 넘기면 요일이 자동으로 다음 날로 바뀝니다."
)


def build_time_contract(*, time_enabled: bool = False) -> str:
    """[시간 인식] 블록. 시간 개념이 꺼져 있으면 빈 문자열."""
    return _TIME_CONTRACT if time_enabled else ""


# ── 감염 증상 계약 ────────────────────────────────────────────────────────────


def build_infection_contract(
    *, infection_enabled: bool = False, disease_name: str = ""
) -> str:
    """[몸 상태 인식] 블록. 감염 모델이 꺼져 있으면 빈 문자열.

    감염 상태(S/I/R)·확률·경과 시간 같은 raw 값은 **절대** 프롬프트에 넣지 않는다
    (`_build_symptom_context` 참고). 여기서 알려주는 것은 "[몸 상태] 블록이 오면
    그게 네 몸이고, 안 오면 너는 멀쩡하다"는 읽는 법뿐이다 — 이 안내가 없으면
    모델이 유행 설정만 보고 증상을 지어낸다.
    """
    if not infection_enabled:
        return ""
    disease = (disease_name or "").strip()
    what    = f'"{disease}"이(가) ' if disease else "전염병이 "
    return (
        "\n\n[몸 상태 인식]\n"
        f"이 세계에는 {what}돌고 있습니다. 당신이 아플 때에 한해 대화 맥락에 "
        "[몸 상태] 블록이 함께 주어집니다.\n"
        "- [몸 상태]에 적힌 증상만을 지금 당신이 실제로 느끼는 것으로 여기고, "
        "말과 행동에 자연스럽게 드러내세요.\n"
        "- [몸 상태] 블록이 없으면 당신은 지금 아무 증상도 느끼지 않습니다. "
        "증상이나 감염 여부를 임의로 지어내지 마세요.\n"
        "- 자신의 감염 여부를 수치나 상태값으로 알 수는 없습니다. 몸으로 느끼는 것만 압니다."
    )


# ── 관계 지도 계약 ────────────────────────────────────────────────────────────
#
# `relationships` 는 **에이전트마다 다른** 유일한 계약 블록이다. 나머지 세계 계약
# (지도/시간/감염)은 시뮬레이션 전체가 공유하지만, "채민경은 나의 아내"는 김봉남의
# 시점에서만 참이다. 그래서 `build_world_contract` 안에 넣지 않고 별도 빌더로 두고
# `core._apply_engine_contract` 가 에이전트별로 이어붙인다.
#
# 왜 프로즈가 아니라 구조 데이터인가: 페르소나가 "당신은 아빠다. 딸이 하나 있다"
# 라고만 쓰면 LLM 은 그 "딸"이 `target` 에 넣어야 할 어떤 ID 인지 모른다. 이름(key)과
# 관계어를 한 줄에 바인딩해야 지목이 성립한다.

# 헤더가 `[아는 사람]` 이 아닌 이유: `build_targets_block` 의 섹션 라벨도 `[아는 사람]`
# 이라(step.py 가 만든다) 위치 미사용 시나리오에서는 한 프롬프트에 같은 헤더가 두 번
# 나온다 — 하나는 "내 관계 로스터", 하나는 "이번 턴에 지목 가능한 목록"으로 뜻이 다르다.
# 섹션 라벨 쪽을 고치면 관계를 안 쓰는 기존 시나리오의 프롬프트까지 바뀌므로(회귀),
# 관계를 쓸 때만 붙는 이쪽 헤더를 구체화했다.
_RELATIONSHIP_HEADER = (
    "\n\n[아는 사람 (나와의 관계)]\n"
    "당신이 아는 사람들입니다. 이들에게 말을 걸 때 target 필드에 아래 ID를 씁니다."
)


def build_relationship_contract(
    relationships: dict[str, str],
    key_to_alias:  dict[str, str] | None = None,
) -> str:
    """[아는 사람] 블록. `relationships` 가 비면 빈 문자열.

    Parameters
    ----------
    relationships
        ``{상대 agent key: 내가 그를 부르는 관계}``. **화자 한 명의 시점**이다
        (김봉남의 `{"채민경": "아내"}` 와 채민경의 `{"김봉남": "남편"}` 은 별개).
        dict 삽입 순서를 그대로 렌더 순서로 쓴다.
    key_to_alias
        agent key → 표시 이름. 없으면 key 를 그대로 표시명으로 쓴다.

    실존하지 않는 key(dangling)를 거르는 것은 **호출자 책임**이다 — 이 모듈은
    에이전트 명부를 모른다(순수 문자열 빌더). 엔진 경로에서는
    `Simulation._sanitize_relationships()` 가 초기화 시 한 번 걸러낸다.
    """
    if not relationships:
        return ""
    alias = key_to_alias or {}
    lines = [_RELATIONSHIP_HEADER]
    for key, relation in relationships.items():
        name = alias.get(key) or key
        rel  = (relation or "").strip()
        # 관계어가 비어도 "이 사람을 안다"는 사실 자체는 유효하므로 줄은 남긴다.
        suffix = f" — 당신의 {rel}" if rel else ""
        lines.append(f'  - {name} (ID: "{key}"){suffix}')
    return "\n".join(lines)


# ── 정적(세계) 계약 조립 ──────────────────────────────────────────────────────


def build_world_contract(
    *,
    location_graph:     dict[str, list[str]] | None = None,
    exterior_locations: set[str] | None             = None,
    location_zone:      dict[str, str] | None       = None,
    zone_entry:         dict[str, str] | None       = None,
    time_enabled:       bool                        = False,
    infection_enabled:  bool                        = False,
    disease_name:       str                         = "",
) -> str:
    """시뮬레이션 수명 동안 고정인 계약 블록들(지도 + 시간 + 감염)을 순서대로 이어붙인다.

    반환 문자열은 사용자 `system_prompt` **뒤에**, 출력 계약 **앞에** 붙는다.
    활성화된 feature가 하나도 없으면 빈 문자열.
    """
    return (
        build_map_contract(
            location_graph     = location_graph,
            exterior_locations = exterior_locations,
            location_zone      = location_zone,
            zone_entry         = zone_entry,
        )
        + build_time_contract(time_enabled=time_enabled)
        + build_infection_contract(
            infection_enabled = infection_enabled,
            disease_name      = disease_name,
        )
    )


# ── 출력 계약 (스키마 + move_to + target) ─────────────────────────────────────

_FIELD_DESCS: dict[str, str] = {
    "action_note": "행동이나 생각 묘사. 이 내용은 다른 에이전트에게 **시각적 정보**로 전달됨.",
}


def _field_line(f: dict) -> str:
    if f["name"] == "action_note":
        return '    "action_note": "행동이나 생각, 상황 묘사. 텍스트로 서술. 예: \'한숨을 쉰다\', \'눈을 흘김\'",'
    return f'    "{f["name"]}": "{f["default"]}",'


def _field_hint(f: dict) -> str:
    desc = _FIELD_DESCS.get(f["name"])
    if desc:
        return f'- {f["name"]}: {desc}'
    return f'- {f["name"]}: 적절한 값 (기본값 예시: "{f["default"]}")'


def _target_label(
    key:           str,
    key_to_alias:  dict[str, str] | None,
    relationships: dict[str, str] | None,
) -> str:
    """`- ID: "<key>"` 뒤에 붙는 괄호 라벨. 없으면 빈 문자열.

    - 관계 있음: ``  (채민경 · 아내)``  — 표시명이 없으면 key 를 표시명 자리에 쓴다
    - 관계 없음: ``  (표시명)``          — 기존 동작 그대로
    """
    alias = (key_to_alias or {}).get(key, "")
    rel   = ((relationships or {}).get(key) or "").strip()
    if rel:
        return f'  ({alias or key} · {rel})'
    return f'  ({alias})' if alias else ""


def build_targets_block(
    available_targets: list[str],
    key_to_alias:      dict[str, str] | None                = None,
    target_sections:   list[tuple[str, list[str]]] | None   = None,
    situation_targets: bool                                 = False,
    *,
    speaker_relationships: dict[str, str] | None            = None,
) -> tuple[str, str]:
    """(targets_block, targets_footer) — `<TARGETS>` / `<TARGETS_FOOTER>` 치환값.

    `speaker_relationships` 는 **이 블록을 읽는 화자 시점**의 관계 지도다. 목록에
    있는 ID 가 거기 있으면 표시명 옆에 관계어를 붙여, 모델이 `[아는 사람]` 블록과
    `<TARGETS>` 를 같은 사람으로 묶을 수 있게 한다(낯선 이 `stranger_N` ID 는
    관계 지도에 없으므로 자동으로 라벨이 붙지 않는다).
    """
    if situation_targets:
        # 위치 기반 모드: 대화 상대는 상황 컨텍스트에서 ID 포함하여 제공됨
        targets_block = "  ([현재 상황] 컨텍스트에서 대화 상대와 ID를 확인하세요)\n"
    elif target_sections:
        parts: list[str] = []
        for section_label, members in target_sections:
            parts.append(f"[{section_label}]")
            for t in members:
                parts.append(f'  - ID: "{t}"' + _target_label(t, key_to_alias, speaker_relationships))
        targets_block = ("\n".join(parts) + "\n") if parts else "  (없음)\n"
    else:
        lines: list[str] = []
        for t in available_targets:
            lines.append(f'  - ID: "{t}"' + _target_label(t, key_to_alias, speaker_relationships))
        targets_block = ("\n".join(lines) + "\n") if lines else "  (없음)\n"

    # 그룹이 2개 이상일 때 그룹별 단축 표기 추가 (브릿지 에이전트용)
    named_sections = [
        label for label, _ in (target_sections or [])
        if label != "기타"
    ]
    if len(named_sections) >= 2:
        group_shortcuts = " / ".join(
            f'[{label}] 전체: "group:{label}"' for label in named_sections
        )
        targets_footer = f'  {group_shortcuts} / 모두에게: "all" / 혼잣말·독백·탄식 등: "self"\n'
    else:
        targets_footer = '  전체에게: "all" / 혼잣말·독백·탄식 등: "self"\n'

    return targets_block, targets_footer


def build_output_contract(
    available_targets: list[str],
    extra_fields:      list[dict],
    key_to_alias:      dict[str, str] | None              = None,
    template:          str | None                         = None,
    target_sections:   list[tuple[str, list[str]]] | None = None,
    location_name:     str                                = "",
    situation_targets: bool                               = False,
    *,
    has_location_graph: bool = False,
    has_zone:           bool = False,
    speaker_relationships: dict[str, str] | None = None,
) -> str:
    """출력 JSON 스키마 + move_to 의미 + target ID 규칙을 담은 계약 블록.

    `template`이 **명시적으로** 주어졌을 때만 사용자 오버라이드로 취급하고, 그렇지
    않으면 항상 엔진 기본 템플릿을 쓴다(= 엔진 업그레이드가 자동 반영된다).
    `location_name`은 시그니처 호환을 위해 남겨둔 미사용 인자다.
    `speaker_relationships`는 `<TARGETS>` 목록에 관계어를 붙이는 화자 시점 관계 지도.
    """
    targets_block, targets_footer = build_targets_block(
        available_targets, key_to_alias, target_sections, situation_targets,
        speaker_relationships=speaker_relationships,
    )

    tmpl = template if template is not None else DEFAULT_OUTPUT_FORMAT_TEMPLATE
    # 템플릿에 action_note가 이미 하드코딩돼 있으면 <FIELD_LINES>/<FIELD_HINTS>에서 제외 (중복 방지)
    hardcoded = {name for name in ("action_note",) if f'"{name}"' in tmpl}
    active_fields = [f for f in extra_fields if f["name"] not in hardcoded]

    field_lines = "\n".join(_field_line(f) for f in active_fields)
    field_hints = "\n".join(_field_hint(f) for f in active_fields)
    move_to_hint = build_move_to_hint(
        has_location_graph=has_location_graph, has_zone=has_zone,
    )

    return (
        tmpl
        .replace("<FIELD_LINES>", field_lines)
        .replace("<FIELD_HINTS>", field_hints)
        .replace("<MOVE_TO_HINT>", move_to_hint)
        .replace("<TARGETS>", targets_block)
        .replace("<TARGETS_FOOTER>", targets_footer)
    )


def build_engine_contract(
    *,
    extra_fields:       list[dict],
    available_targets:  list[str] | None                   = None,
    key_to_alias:       dict[str, str] | None              = None,
    target_sections:    list[tuple[str, list[str]]] | None = None,
    situation_targets:  bool                               = False,
    location_graph:     dict[str, list[str]] | None        = None,
    exterior_locations: set[str] | None                    = None,
    location_zone:      dict[str, str] | None              = None,
    zone_entry:         dict[str, str] | None              = None,
    time_enabled:       bool                               = False,
    infection_enabled:  bool                               = False,
    disease_name:       str                                = "",
    include_output_schema: bool                            = True,
    output_format_override: str | None                     = None,
    relationships:      dict[str, str] | None              = None,
) -> str:
    """계약 층 전체(세계 계약 + 관계 지도 + 출력 계약)를 한 번에 만든다.

    실행 경로는 두 조각을 따로 쓰지만(정적 블록은 Agent에 한 번, 출력 계약은 매 턴),
    **계약 프리뷰 엔드포인트**나 테스트처럼 "지금 설정이면 무엇이 주입되는가"를
    통째로 보고 싶은 호출자를 위한 단일 진입점이다.

    `relationships` 는 **한 명의 화자 시점** 관계 지도다. 주어지면 [아는 사람]
    블록으로 렌더되고, 동시에 `<TARGETS>` 목록의 관계어 라벨로도 쓰인다 — 실행
    경로(`core._apply_engine_contract` + `Agent.get_system_message`)가 같은 dict 를
    두 자리에 쓰는 것과 정확히 같다.

    `include_output_schema=False`면 출력 스키마를 뺀다 — 인터뷰 모드처럼 자연어
    산문 답변을 받아야 하는 경로용 carve-out이다.
    """
    world = build_world_contract(
        location_graph     = location_graph,
        exterior_locations = exterior_locations,
        location_zone      = location_zone,
        zone_entry         = zone_entry,
        time_enabled       = time_enabled,
        infection_enabled  = infection_enabled,
        disease_name       = disease_name,
    ) + build_relationship_contract(relationships or {}, key_to_alias)
    if not include_output_schema:
        return world
    return world + build_output_contract(
        available_targets or [],
        extra_fields,
        key_to_alias,
        template           = output_format_override,
        target_sections    = target_sections,
        situation_targets  = situation_targets,
        has_location_graph = bool(location_graph),
        has_zone           = bool(location_zone),
        speaker_relationships = relationships or None,
    )


# ── 시작 시 검증 ──────────────────────────────────────────────────────────────

# (feature 이름, 조립된 프롬프트에 반드시 있어야 하는 토큰, 없을 때의 진단 문구)
_CONTRACT_ASSERTIONS: list[tuple[str, str, str, str]] = [
    ("location_graph", "has_location_graph", "move_to",
     "위치 그래프가 활성인데 프롬프트에 move_to 지시어가 없습니다 — 에이전트가 이동/추격을 하지 못합니다"),
    ("location_graph", "has_location_graph", "[위치 그래프",
     "위치 그래프가 활성인데 프롬프트에 [위치 그래프] 블록이 없습니다 — 에이전트가 지도를 모릅니다"),
    ("zone", "has_zone", "[구역:",
     "구역(zone)이 설정됐는데 프롬프트에 구역 표기가 없습니다 — 다른 방의 사람을 만나러 갈 방법을 모릅니다"),
    ("time", "time_enabled", "[시간 인식]",
     "시간 개념이 활성인데 프롬프트에 [시간 인식] 안내가 없습니다 — 에이전트가 시각을 무시합니다"),
    ("infection", "infection_enabled", "[몸 상태",
     "감염 모델이 활성인데 프롬프트에 [몸 상태] 읽는 법 안내가 없습니다 — 증상 환각이 늘어납니다"),
    ("output_schema", "include_output_schema", '"target"',
     "출력 계약이 필요한데 프롬프트에 target 스키마가 없습니다 — 발화 라우팅이 전부 실패합니다"),
    ("output_schema", "include_output_schema", '"move_to"',
     "출력 계약이 필요한데 프롬프트에 move_to 스키마가 없습니다 — 이동이 전혀 일어나지 않습니다"),
    ("output_schema", "include_output_schema", "update_appearance",
     "출력 계약이 필요한데 프롬프트에 update_appearance 스키마가 없습니다 — 외모 변경이 전달되지 않습니다"),
]


def verify_contract(
    prompt: str,
    *,
    has_location_graph:    bool = False,
    has_zone:              bool = False,
    time_enabled:          bool = False,
    infection_enabled:     bool = False,
    include_output_schema: bool = True,
) -> list[str]:
    """활성 feature마다 필요한 지시어가 조립된 프롬프트에 실제로 들어 있는지 확인.

    문제를 발견하면 사람이 읽을 수 있는 경고 문자열 목록을 돌려준다(예외를 던지지
    않는다 — 시뮬레이션은 계속 돌아야 한다). 사용자가 옛 프리즈 템플릿을
    오버라이드로 들고 있거나, 계약 블록 주입 경로가 리팩터링 중에 끊겼을 때
    **실행 전에** 잡아내는 안전망이다.
    """
    flags = {
        "has_location_graph":    bool(has_location_graph),
        "has_zone":              bool(has_zone),
        "time_enabled":          bool(time_enabled),
        "infection_enabled":     bool(infection_enabled),
        "include_output_schema": bool(include_output_schema),
    }
    problems: list[str] = []
    for _feature, flag, token, message in _CONTRACT_ASSERTIONS:
        if flags[flag] and token not in prompt:
            problems.append(f"{message} (누락 토큰: {token!r})")
    return problems
