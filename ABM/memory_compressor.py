"""Delta memory compression for simulation agents.

Flow
----
1. Triggered when estimated prompt tokens reach 70 % of the agent's token limit.
2. Existing structured memory + new raw messages → LLM → updated structured memory.
3. DB is updated; raw messages are archived.
4. agent.memory is cleared.

시간 인식(2026-09 재설계)
------------------------
구버전은 원문에 시각 정보가 전혀 없어서(그냥 "[나] .../[수신] ...") 압축 LLM이
사건이 언제 있었던 일인지 알 도리가 없었다 — "몇 번째 wave"를 물으면 배치
전체에 같은 숫자를 반복해 찍거나(episodic_memory.wave 오염), "오늘"이 어느
요일인지 몰라 매일 바뀌는 것("오늘 저녁 메뉴는 X")까지 영구 사실처럼 쌓였다.

지금은:
  - 원문 각 줄에 요일/오전·오후 구획 헤더가 붙는다(`_format_messages`) — LLM이
    "화요일 저녁엔 이런 일이" 식으로 직접 요일을 적을 수 있다.
  - 사건/사실의 시각 앵커(`elapsed_minutes`, 절대 경과분)는 LLM에게 묻지 않고
    **코드가** 이 압축이 실제로 일어난 시점(`now_elapsed`)으로 못박는다
    (`ABM/simulation/step.py::_compress_agent`가 넘겨준다).
  - `build_memory_block()`이 "지금"(호출 시점의 `now_elapsed`) 기준으로 각
    사건이 얼마나 오래됐는지 계산해 "방금"/"며칠 전, 어렴풋한 기억" 두 버킷으로
    나눠 문체를 달리 렌더링한다 — 오래된 기억일수록 개수도 줄이고(뭉뚱그림)
    표현도 흐릿하게, 실제 기억의 감쇠를 흉내낸다. 이 함수는 캐싱하지 않고
    매 턴 새로 불러야 한다(`step.py::_fresh_memory_block`) — 그래야 "방금"이
    시간이 흐르면서 "며칠 전"으로 자연스럽게 넘어간다.
"""

import json
import logging

from .db import SimDB
from .llm import LLMCall
from .simulation._constants import format_sim_day_period

logger = logging.getLogger(__name__)

_COMPRESSION_SYSTEM = (
    "당신은 기억 정리 도우미입니다. "
    "주어진 에이전트의 관점에서 대화를 분석하고 구조화된 기억을 추출하세요. "
    "반드시 JSON만 출력하고 다른 텍스트는 절대 출력하지 마세요."
)

_COMPRESSION_PROMPT = """\
{agent_name}의 관점에서 아래 대화를 분석하여 기억을 업데이트하세요.

[기존 구조화 기억]
{existing_memory}

[새로 경험한 대화 — "---" 구획은 그 대화가 있었던 일차·요일·시간대입니다]
{messages_text}

반드시 아래 JSON 형식으로만 응답하세요:
{{
  "episodes": [
    {{"event": "<사건 요약>", "participants": ["<에이전트키>"], "importance": <1-5>}}
  ],
  "facts": [
    {{"fact": "<사실/믿음>", "confidence": <0.0-1.0>, "prev_fact": "<변경 전 사실, 변경 없으면 생략>", "prev_confidence": <float, 변경 없으면 생략>}}
  ],
  "relationships": [
    {{"target": "<에이전트키>", "stance": "<trust|neutral|suspect|hostile>", "reason": "<이유>"}}
  ],
  "self_state": "<{agent_name}의 현재 동기·감정 상태를 한 문장으로>"
}}

규칙:
- episodes: 이번 대화에서 새로 경험한 사건만 (기존과 중복 제외). "몇 번째 wave"인지는
  적지 마세요 — 시스템이 실제 시각을 따로 기록합니다.
- facts: 시간이 지나도 안 바뀌는 "계속 참인 것"만 — 성격·취향·습관·지속되는 관계.
  오늘 하루만 해당하는 일(오늘 메뉴, 오늘 기분, 숙제를 끝냈는지처럼 매일 달라지는
  것)은 facts가 아니라 episodes로 적으세요. facts에 매번 다른 값이 쌓이면
  "화요일엔 X, 수요일엔 Y" 식으로 모순돼 보입니다.
- facts/episodes에 "오늘"·"어제"·"이번 주" 같은 상대적 시간 표현을 쓰지 마세요.
  위 구획 헤더의 일차·요일을 보고 "9일차 화요일 저녁 메뉴는..." 처럼 **일차를
  포함해서** 적으세요. 요일만 적으면(예: "화요일") 몇 주가 지나면 어느 화요일인지
  구분이 안 돼 기억이 혼선됩니다.
- relationships: 변화가 있는 관계만 (변화 없으면 제외)
- self_state: 항상 포함 (기존과 동일해도)
"""


# ---------------------------------------------------------------------------
# 사건 recency 버킷 — build_memory_block()과 인터뷰(interview.py)가 공유한다
# ---------------------------------------------------------------------------

_RECENT_EPISODE_DAYS     = 1    # 이 안(며칠)이면 "방금"(자세히), 넘으면 "예전"(뭉뚱그림)
_RECENT_EPISODE_SHOW_MAX = 10   # "방금" 버킷 표시 상한
_OLD_EPISODE_SHOW_MAX    = 3    # "예전" 버킷에서 실제로 보여줄 개수(중요도 상위) — 나머지는 개수만


def _days_ago(now_elapsed: int, episode_elapsed: int | None) -> int | None:
    """None이면 시각 정보가 없는 옛 행(이 기능 도입 전 압축분) — 무조건 '예전' 취급."""
    if episode_elapsed is None:
        return None
    return max(0, (now_elapsed - episode_elapsed) // 1440)


def _episode_line(ep: dict, *, with_importance: bool = True) -> str:
    if with_importance:
        return f"    - {ep['event']} [중요도 {ep['importance']}]"
    return f"    - {ep['event']}"


def bucket_episodes(
    episodes: list[dict], now_elapsed: int, *, cap: bool = True, with_importance: bool = True,
) -> tuple[list[str], list[str], int]:
    """사건들을 "방금"/"예전" 두 버킷으로 나눠 렌더링 줄을 만든다.

    "방금"(오늘~하루 전)은 있는 그대로, "예전"(그 이상 또는 시각 정보 없는 옛
    행)은 `cap=True`(기본, 라이브 시뮬레이션 경로)면 중요도 상위 몇 개만 보여주고
    나머지는 개수만 알린다 — 실제 기억처럼 오래될수록 세부가 흐려지고
    뭉뚱그려지는 걸 흉내낸다. `cap=False`(인터뷰 경로 — 회고는 완전성이
    목적이라 항목을 버리지 않는다)면 두 버킷으로 나누기만 하고 전부 보여준다.

    `with_importance=False`는 압축 프롬프트의 "기존 구조화 기억" 재진술
    (`_format_existing`) 전용이다 — 사용자에게 보이는 최종 블록에는 항상
    `[중요도 N]` 접미사를 붙이지만(with_importance=True, 기본값), 그 접미사가
    붙은 문장을 압축 LLM에게 "기존 기억"으로 다시 보여주면 LLM이 새로 쓰는
    `event` 문장에도 똑같은 "[중요도 N]" 텍스트를 그대로 따라 적는 사례가
    관찰됐다(실측 — 두 번째 압축부터 이벤트 문장 끝에 중요도 태그가 중복으로
    박힘). 압축 프롬프트의 "기존과 중복 제외" 판단에는 중요도 숫자가 필요
    없으므로, 그 입력에서만 아예 빼서 흉내낼 텍스트를 안 보여준다.

    Returns (recent_lines, distant_lines, distant_omitted_count).
    """
    recent: list[dict] = []
    distant: list[dict] = []
    for ep in episodes:
        d = _days_ago(now_elapsed, ep.get("elapsed_minutes"))
        (distant if d is None or d > _RECENT_EPISODE_DAYS else recent).append(ep)

    if not cap:
        return (
            [_episode_line(e, with_importance=with_importance) for e in recent],
            [_episode_line(e, with_importance=with_importance) for e in distant],
            0,
        )

    recent = recent[-_RECENT_EPISODE_SHOW_MAX:]

    distant_sorted = sorted(distant, key=lambda e: e.get("importance", 3), reverse=True)
    shown    = distant_sorted[:_OLD_EPISODE_SHOW_MAX]
    omitted  = max(0, len(distant_sorted) - len(shown))
    shown.sort(key=lambda e: e.get("elapsed_minutes") if e.get("elapsed_minutes") is not None else -1)

    return (
        [_episode_line(e, with_importance=with_importance) for e in recent],
        [_episode_line(e, with_importance=with_importance) for e in shown],
        omitted,
    )


def _render_episode_section(
    episodes: list[dict], now_elapsed: int, *, with_importance: bool = True,
) -> list[str]:
    if not episodes:
        return []
    recent_lines, distant_lines, omitted = bucket_episodes(
        episodes, now_elapsed, with_importance=with_importance,
    )
    if not (recent_lines or distant_lines or omitted):
        return []
    lines = ["■ 경험한 사건:"]
    if recent_lines:
        lines.append("  방금 있었던 일:")
        lines.extend(recent_lines)
    if distant_lines or omitted:
        lines.append("  며칠 전, 어렴풋한 기억:")
        lines.extend(distant_lines)
        if omitted:
            lines.append(f"    - (그 밖에도 며칠 전 사소한 일 {omitted}건은 가물가물하다)")
    return lines


# ---------------------------------------------------------------------------
# 사실(facts) 표시 상한 — episodes와 달리 기존엔 상한이 전혀 없었다
# ---------------------------------------------------------------------------

_FACT_SHOW_MAX = 15   # 확신 높은 순 상위 개수. 나머지는 개수만 알린다.


def _fact_lines(facts: list[dict]) -> list[str]:
    """확신 높은 순으로 최대 `_FACT_SHOW_MAX`개만 줄로 만들고, 나머지는 생략 안내.

    `facts`는 `SimDB.get_facts()`가 이미 confidence DESC로 정렬해 돌려준다 —
    episodic_memory(사건)는 recency 버킷으로 상한이 있었지만(방금 최대 10개,
    예전 최대 3개) semantic_memory(사실)는 상한이 아예 없었다. 압축 사이클이
    쌓일수록(장기 시뮬레이션) 사실 목록이 끝없이 자라 `[나의 기억 요약]`이
    무한정 커지고, 그만큼 실제 대화를 담을 자리(`agent.memory`)가 줄어든다 —
    심하면 `trim_to_token_limit`이 대화 원문을 전부 비워도 사실 목록 하나만으로
    token_limit을 넘겨 LLM 호출 자체가 실패할 수 있었다.
    """
    shown   = facts[:_FACT_SHOW_MAX]
    omitted = max(0, len(facts) - len(shown))
    lines = [
        f"  - {f['fact']} (확신 {int(float(f['confidence']) * 100)}%)"
        for f in shown
    ]
    if omitted:
        lines.append(f"  - (그 밖에 확신이 흐릿한 사실 {omitted}건은 생략)")
    return lines


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_existing(
    episodes: list, facts: list, relationships: list, self_state: str | None,
    now_elapsed: int,
) -> str:
    lines: list[str] = []

    if self_state:
        lines.append(f"현재 상태: {self_state}")

    if facts:
        lines.append("알고 있는 사실:")
        lines.extend(_fact_lines(facts))

    if relationships:
        lines.append("인물 관계:")
        for r in relationships:
            lines.append(f"  - {r['target_key']}: {r['stance']} — {r['reason']}")

    # with_importance=False — 이 텍스트는 압축 LLM에게 "기존 기억"으로 보여주는
    # 입력이다. 접미사를 붙여서 보여주면 LLM이 새로 쓰는 event 문장에도 같은
    # "[중요도 N]" 텍스트를 그대로 따라 적는다(bucket_episodes 문서 참고).
    lines.extend(_render_episode_section(episodes, now_elapsed, with_importance=False))

    return "\n".join(lines) if lines else "없음 (첫 번째 압축)"


def _format_messages(messages: list[dict], sim_start_minutes: int, start_weekday_idx: int) -> str:
    """원문을 일차·요일·오전/오후 구획으로 나눠 나열한다.

    분 단위까지 보이면 거의 매 줄 헤더가 바뀌어 구획이 무의미해지므로
    `format_sim_day_period`(일차 + 요일 + 오전/오후만)로 굵게 묶는다 — 압축 LLM이
    "이 대화가 대략 언제였는지" 판단하는 데는 이 정도 해상도면 충분하다. 일차를
    포함하는 이유는 요일만으로는 일주일이 지나면 반복돼("화요일"이 이번 주인지
    저번 주인지 구분 불가) 압축 LLM이 그 모호한 라벨을 그대로 옮겨 적으면
    몇 주 뒤 같은 요일 문구가 다시 나와 기억이 혼선되기 때문이다.
    `elapsed_minutes`가 없는 메시지(이 기능 이전 경로·레거시)는 헤더 없이 그냥 나열.
    """
    lines: list[str] = []
    last_period: str | None = None
    for m in messages:
        em = m.get("elapsed_minutes")
        if em is not None:
            period = format_sim_day_period(sim_start_minutes + em, start_weekday_idx)
            if period != last_period:
                lines.append(f"--- {period} ---")
                last_period = period
        prefix = "나" if m["role"] == "assistant" else "수신"
        lines.append(f"[{prefix}] {m['content']}")
    return "\n".join(lines)


def _parse_compression_result(raw: str) -> dict:
    """Parse LLM output; strip optional markdown code fence."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_memory_block(
    sim_id: str,
    agent_key: str,
    db: SimDB,
    key_to_alias: dict | None = None,
    now_elapsed: int = 0,
) -> str | None:
    """Format the agent's structured memory as a prompt injection block.

    Returns None when no memory exists yet (first cycles before any compression).

    캐싱하지 말 것 — `now_elapsed`가 "지금"이어야 "방금"/"며칠 전" 버킷이
    맞다. 압축 시점에 한 번 렌더링해 두면 시간이 흐른 뒤에도 그 라벨이
    그대로 굳어버린다(`ABM/simulation/step.py::_fresh_memory_block` 참고).
    """
    episodes      = db.get_episodes(sim_id, agent_key)
    facts         = db.get_facts(sim_id, agent_key)
    relationships = db.get_relationships(sim_id, agent_key)
    self_state    = db.get_self_state(sim_id, agent_key)

    if not any([episodes, facts, relationships, self_state]):
        return None

    alias  = key_to_alias or {}
    lines  = ["[나의 기억 요약]"]

    if self_state:
        lines.append(f"■ 현재 상태: {self_state}")

    if facts:
        lines.append("■ 알고 있는 사실:")
        lines.extend(_fact_lines(facts))

    if relationships:
        lines.append("■ 인물 관계:")
        for r in relationships:
            display = alias.get(r["target_key"], r["target_key"])
            lines.append(f"  - {display}: {r['stance']} — {r['reason']}")

    lines.extend(_render_episode_section(episodes, now_elapsed))

    return "\n".join(lines)


def compress(
    agent_name: str,
    agent_key: str,
    sim_id: str,
    messages: list[dict],
    wave: int,
    db: SimDB,
    llm: LLMCall,
    key_to_alias: dict | None = None,
    llm_max_tokens: int = 16384,
    sim_start_minutes: int = 0,
    start_weekday_idx: int = 0,
    now_elapsed: int = 0,
) -> str | None:
    """Run delta compression for one agent.

    Archives raw messages to DB, updates structured memory tables,
    and returns the refreshed memory block string (or None on failure).

    `now_elapsed`(이 압축이 일어난 시점의 절대 경과분)를 이번 배치에서 뽑아낸
    모든 episode/fact의 시점으로 못박는다 — LLM이 준 값(있어도)은 무시한다.
    """
    if not messages:
        return build_memory_block(sim_id, agent_key, db, key_to_alias, now_elapsed)

    episodes      = db.get_episodes(sim_id, agent_key)
    facts         = db.get_facts(sim_id, agent_key)
    relationships = db.get_relationships(sim_id, agent_key)
    self_state    = db.get_self_state(sim_id, agent_key)

    existing_text = _format_existing(episodes, facts, relationships, self_state, now_elapsed)
    messages_text = _format_messages(messages, sim_start_minutes, start_weekday_idx)

    prompt = _COMPRESSION_PROMPT.format(
        agent_name=agent_name,
        existing_memory=existing_text,
        messages_text=messages_text,
    )

    try:
        raw, _, _ = llm(
            [
                {"role": "system", "content": _COMPRESSION_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=llm_max_tokens,
        )
        data = _parse_compression_result(raw)
    except Exception as exc:
        logger.error(f"[{agent_key}] 압축 LLM 실패: {exc}")
        return None

    # Archive raw messages before clearing them.
    db.save_messages(sim_id, agent_key, messages, wave)
    db.log_compression(sim_id, agent_key, len(messages), wave)

    if data.get("episodes"):
        db.upsert_episodes(sim_id, agent_key, data["episodes"], wave, elapsed_minutes=now_elapsed)
    if data.get("facts"):
        db.upsert_facts(sim_id, agent_key, data["facts"], wave, elapsed_minutes=now_elapsed)
    if data.get("relationships"):
        db.upsert_relationships(sim_id, agent_key, data["relationships"], wave)
    if data.get("self_state"):
        db.upsert_self_state(sim_id, agent_key, data["self_state"], wave)

    logger.info(
        f"[{agent_key}] 압축 완료 — "
        f"msgs={len(messages)}, "
        f"episodes+={len(data.get('episodes', []))}, "
        f"facts+={len(data.get('facts', []))}, "
        f"rels+={len(data.get('relationships', []))}"
    )

    return build_memory_block(sim_id, agent_key, db, key_to_alias, now_elapsed)


# ---------------------------------------------------------------------------
# 2차 기억 정리(consolidation) — "반복되면 깊어지고, 한 번뿐이면 옅어진다"
# ---------------------------------------------------------------------------
#
# 1차 압축(compress())은 새 대화 조각 하나만 보고 매번 독립적으로 판단하므로
# "이 사실이 전체 기억에서 몇 번째 반복인지"를 알 방법이 없다 — 그래서 표현만
# 바뀐 같은 이야기가 별개 행으로 계속 쌓였다(실측: "고등학생이며 수학 학원" /
# "학생이며 수학 학원"). 2차 정리는 그 에이전트의 전체 사실을 다시 조망하며:
#   - 여러 사실이 사실상 같은 이야기를 반복/뒷받침하면 확신을 높이고(강화 —
#     사람이 반복 노출된 정보를 더 오래 기억하는 것과 같은 원리)
#   - 한 번만 언급되고 이후 전혀 뒷받침되지 않은 사소한 사실은 확신을 낮춘다
#     (쇠퇴).
# 행을 지우거나 병합하지 않는다 — 점수(confidence)만 바꾸고, 이미 있는 표시
# 상한(`_fact_lines`, 상위 `_FACT_SHOW_MAX`개)이 낮아진 확신을 보고 자연히
# 걸러내게 둔다. 그래서 되돌릴 수 있고(다음 정리 때 다시 오를 수 있음), 잘못
# 병합해 원문을 잃어버릴 위험이 없다. 트리거는
# `ABM/simulation/step.py::_maybe_consolidate_facts`(1차 압축 N번마다 한 번).

_CONSOLIDATION_SYSTEM = (
    "당신은 기억 정리 도우미입니다. 쌓인 사실들을 검토해 확신도를 재평가하세요. "
    "반드시 JSON만 출력하고 다른 텍스트는 절대 출력하지 마세요."
)

_CONSOLIDATION_PROMPT = """\
{agent_name}에 대해 지금까지 쌓인 사실 목록입니다. 각 줄 앞의 id로 지칭하세요.

{facts_text}

이 목록을 검토해 확신도를 재평가하세요:
- 서로 다른 표현이지만 같은 내용을 반복하거나 뒷받침하는 사실이 여럿 있으면
  ("반복 확인된 사실") — 그 사실들 각각의 확신을 원래보다 높이세요(0.1~0.2 상향,
  최대 1.0).
- 딱 한 번만 언급되고 다른 어떤 사실로도 뒷받침되지 않는 사소한 사실은 —
  확신을 낮추세요(0.1~0.2 하향, 최소 0.1).
- 이미 충분히 높거나(0.9 이상) 이미 낮은(0.3 이하) 사실, 또는 바꿀 필요가
  없는 사실은 결과에 포함하지 마세요.
- 사실 문장 자체는 바꾸지 마세요 — 이번엔 확신도만 재평가합니다.

반드시 아래 JSON 형식으로만 응답하세요:
{{
  "adjustments": [
    {{"id": <int>, "new_confidence": <0.0-1.0>, "reason": "<한 줄 이유>"}}
  ]
}}
바꿀 사실이 없으면 "adjustments": [] 로 응답하세요.
"""

_CONSOLIDATION_MIN_FACTS = 4   # 이보다 적으면 정리할 게 없다고 보고 건너뛴다


def consolidate_facts(
    agent_name: str,
    agent_key: str,
    sim_id: str,
    db: SimDB,
    llm: LLMCall,
    llm_max_tokens: int = 8192,
) -> int:
    """전체 사실을 다시 훑어 확신도를 강화/쇠퇴시킨다(2차 정리).

    행을 지우거나 합치지 않는다 — 점수만 바꾸고 나머지는 `_fact_lines`의
    표시 상한에 맡긴다. 반환값은 실제로 반영된 조정 건수(관전/테스트용).
    """
    facts = db.get_all_facts(sim_id, agent_key)
    if len(facts) < _CONSOLIDATION_MIN_FACTS:
        return 0

    facts_text = "\n".join(
        f"[id={f['id']}] {f['fact']} (확신 {int(float(f['confidence']) * 100)}%)"
        for f in facts
    )
    prompt = _CONSOLIDATION_PROMPT.format(agent_name=agent_name, facts_text=facts_text)

    try:
        raw, _, _ = llm(
            [
                {"role": "system", "content": _CONSOLIDATION_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=llm_max_tokens,
        )
        data = _parse_compression_result(raw)
    except Exception as exc:
        logger.error(f"[{agent_key}] 2차 기억 정리 LLM 실패: {exc}")
        return 0

    valid_ids = {f["id"] for f in facts}
    applied = 0
    for adj in (data.get("adjustments") or []):
        try:
            fid  = int(adj["id"])
            conf = max(0.0, min(1.0, float(adj["new_confidence"])))
        except (KeyError, TypeError, ValueError):
            continue
        if fid not in valid_ids:
            continue
        db.update_fact_confidence(fid, conf)
        applied += 1

    logger.info(
        f"[{agent_key}] 2차 기억 정리 완료 — 사실 {len(facts)}개 중 {applied}건 확신 재평가"
    )
    return applied
