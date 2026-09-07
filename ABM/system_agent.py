"""System agent — simulation narrator/director.

Runs at the start of a wave (configurable interval), receives the recent
activity digest, silent/isolated agents, repetition info, director_note, and the
accumulated director_memo, then outputs interventions (each addressed to one or
more agents), an updated director_memo, and a reason string.

The parsed result also carries a ``_meta`` dict (prompt_tokens / prompt_chars)
so the engine can surface director-call cost via the ``director_call`` event.
"""
from __future__ import annotations

import json
import logging

from .llm import LLMCall

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_AGENT_PROMPT = """\
당신은 멀티에이전트 시뮬레이션의 내레이터이자 진행자입니다.
주어진 시뮬레이션 요약, 침묵·반복·고립 에이전트 정보, 감독 노트를 분석하여
이야기가 감독 노트의 방향으로 흐르도록 필요한 개입을 결정하세요.

개입은 한 종류입니다 — 하나 이상의 에이전트에게 상황·자극 메시지를 전달합니다.
- 한 명에게만 보내면 그 캐릭터만 지각합니다 (사적인 촉발·감각·기억).
- 여럿 또는 전체에게 보내면 그들이 동시에 같은 것을 지각합니다 (공유된 소리·빛·분위기).

이야기가 자연스럽게 흐르고 있다면 interventions는 빈 배열로 반환하세요.\
"""

_USER_TEMPLATE = """\
[현재 Wave: {wave}]

{current_time_section}\
{director_note_section}\
{director_memo_section}\
{recent_activity_section}\
[활성 에이전트]
{agents}

[침묵 중인 에이전트 ({threshold}웨이브 이상 미발화)]
{silent}

[고립된 에이전트 (같은 장소에 대화 상대 없음)]
{isolated}

[반복 중인 에이전트 (최근 발언 유사도 {repeat_threshold}% 이상)]
{repetition}

반드시 아래 JSON 형식으로만 응답하세요:
{{
  "interventions": [
    {{"targets": ["에이전트_ID", ...], "message": "지목한 에이전트(들)에게 전달할 상황/자극 메시지"}}
  ],
  "director_memo": "이번 개입 결과와 다음 전략 메모 (간결하게 1~2줄)",
  "reason": "개입 이유 또는 판단 근거"
}}

규칙:
- 개입이 필요 없으면 interventions는 빈 배열 []
- targets: "all" / "group:그룹명" / 특정 에이전트 ID 목록 (표시 이름 절대 금지)
- 모든 텍스트는 한국어로 작성
- **[반복 중인 에이전트]는 거의 똑같은 문장만 잡아냅니다.** [최근 활동]을 직접
  읽고, 특정 에이전트가 여러 wave에 걸쳐 같은 화제·같은 욕구·같은 자리에서
  맴돌고 있으면(문구가 조금씩 달라도) 반복으로 간주하십시오. 이때는 상황을
  바꾸는 개입으로 장면을 전진시키십시오 — 같은 비트가 3wave 이상 이어지는 것은
  서사 정체입니다.
- **[고립된 에이전트]를 억지로 발화시키지 마십시오.** 혼자 있고(같은 장소에
  아무도 없음) 지금 할 수 있는 일이 정해진 것도 없는 에이전트에게 "계속 진행하라 /
  다음 단계로 넘어가라" 같은 추상적 독려를 보내면 같은 말의 반복만 낳습니다.
  이런 에이전트에게 필요하다면 ① 상황을 **실제로 바꾸는** 것(누가 찾아온다,
  연락이 온다, 자리를 뜰 이유가 생긴다 등 — 구체적 사건)만 주고, 그럴 계기가
  없다면 ② 그냥 두십시오. 고립 자체는 정체가 아니며, 시간이 흐르면 엔진이 알아서
  그 장면을 건너뜁니다.
- **시각·시계·시간을 임의로 지어내지 말 것.** 위 [현재 시각]만을 참조하십시오.
  [현재 시각] 섹션이 없다면 이 세계에는 시간 개념이 없는 것이므로 시각을 아예 언급하지 마십시오.
  "벽시계가 N시를 알린다" 같은 표현은 [현재 시각]과 정확히 일치할 때만 쓸 수 있습니다.
- **완료된 행동·없던 사물·물리적 상태 변화를 만들어내지 마십시오.** 당신이 주는 것은
  '무엇이 일어났다'가 아니라 '무엇이 느껴진다 / 보인다 / 들린다'입니다. 사물의 존재
  (없던 음식이 놓여 있다), 특정 인물의 완료된 행동(누가 요리를 마쳤다), 상태 변화는
  에이전트가 **행동으로** 만드는 것입니다. 여러 명에게 보내는 공유 자극일수록 특히
  엄격하게 — 뒤에 사람·행동이 따라와야 하는 자극(초인종·노크·전화)도 피하고,
  뒤끝 없는 순간적 자극(천둥·정전·사이렌·바람·냄새)만 쓰십시오.\
"""


def run_system_agent(
    *,
    system_prompt: str,
    wave: int,
    active_agents: dict[str, str],       # key → display_name
    silent_agents: list[str],             # agent keys
    silence_threshold: int,
    isolated_agents: list[str] | None = None,   # 같은 장소에 대화 상대가 없는 agent keys
    repetition_info: dict[str, float],    # key → similarity score
    director_note: str,
    director_memo: str,
    key_to_alias: dict[str, str] | None,
    llm: LLMCall,
    llm_max_tokens: int = 16384,
    current_time_str: str = "",
    recent_activity: str = "",
    repeat_threshold_pct: int = 65,
) -> dict | None:
    """Run the system agent LLM call.

    Returns a dict with ``interventions``, ``director_memo``, ``reason``, and
    ``_meta`` (``prompt_tokens`` / ``prompt_chars``), or None on failure.

    ``current_time_str`` 은 에이전트들이 보는 것과 **같은** 시각 문자열
    (`Simulation._format_time_str(...)`, 요일 포함)이다. 비어 있으면 [현재 시각]
    섹션 자체를 생략한다 — 시간 개념이 꺼진 시뮬레이션에서 없는 시계를 만들지
    않기 위해서다. 이 인자가 없던 시절 디렉터는 시각을 전혀 못 받아 개입에
    엉뚱한 시각("벽시계가 8시를 친다")을 지어냈다.

    ``recent_activity`` 는 `_recent_activity_digest(...)` 결과 — 마지막 몇 wave의
    발화를 wave별로 나열한 문자열이다. 어휘 유사도(`repetition_info`)로는 못 잡는
    "표현만 바꿔 같은 화제를 맴도는" 주제 반복을 디렉터가 직접 읽고 판단하게 한다.
    비어 있으면 섹션을 생략한다.
    """
    alias = key_to_alias or {}

    agent_lines = "\n".join(
        f'  - ID: "{k}"  ({alias.get(k, k)})' if alias.get(k) else f'  - ID: "{k}"'
        for k in active_agents
    )
    silent_lines = "\n".join(
        f'  - ID: "{k}"  ({alias.get(k, k)})' for k in silent_agents
    ) if silent_agents else "  없음"
    isolated_lines = "\n".join(
        f'  - ID: "{k}"  ({alias.get(k, k)})' for k in (isolated_agents or [])
    ) if isolated_agents else "  없음"

    if repetition_info:
        repetition_lines = "\n".join(
            f'  - ID: "{k}"  ({alias.get(k, k)})  유사도: {int(v * 100)}%'
            for k, v in repetition_info.items()
        )
    else:
        repetition_lines = "  없음"

    director_note_section = (
        f"[감독 노트 — 서사 목표]\n{director_note}\n\n"
        if director_note.strip() else ""
    )
    director_memo_section = (
        f"[진행 기록 — 이전 개입 누적]\n{director_memo}\n\n"
        if director_memo.strip() else ""
    )

    current_time_section = (
        f"[현재 시각]\n{current_time_str.strip()}\n\n"
        if (current_time_str or "").strip() else ""
    )
    recent_activity_section = (
        f"[최근 활동 — 마지막 몇 wave의 발화]\n{recent_activity.strip()}\n\n"
        if (recent_activity or "").strip() else ""
    )

    user_msg = _USER_TEMPLATE.format(
        wave                    = wave,
        current_time_section    = current_time_section,
        recent_activity_section = recent_activity_section,
        director_note_section   = director_note_section,
        director_memo_section   = director_memo_section,
        agents                  = agent_lines,
        silent                  = silent_lines,
        isolated                = isolated_lines,
        threshold               = silence_threshold,
        repetition              = repetition_lines,
        repeat_threshold        = repeat_threshold_pct,
    )

    sys_msg  = system_prompt or DEFAULT_SYSTEM_AGENT_PROMPT
    messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user",   "content": user_msg},
    ]

    try:
        content, _, usage = llm(messages, max_tokens=llm_max_tokens)
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed["_meta"] = {
                "prompt_tokens": (usage or {}).get("prompt_tokens"),
                "prompt_chars":  len(sys_msg) + len(user_msg),
            }
        return parsed
    except Exception as exc:
        logger.warning(f"[system_agent] LLM 호출 실패 (W{wave}): {exc}")
        return None
