"""Delta memory compression for simulation agents.

Flow
----
1. Triggered when estimated prompt tokens reach 70 % of the agent's token limit.
2. Existing structured memory + new raw messages → LLM → updated structured memory.
3. DB is updated; raw messages are archived.
4. agent.memory is cleared; agent._memory_block is refreshed.
"""

import json
import logging

from .db import SimDB
from .llm import chat_response

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

[새로 경험한 대화 (Wave {wave})]
{messages_text}

반드시 아래 JSON 형식으로만 응답하세요:
{{
  "episodes": [
    {{"wave": <int>, "event": "<사건 요약>", "participants": ["<에이전트키>"], "importance": <1-5>}}
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
- episodes: 이번 대화에서 새로 경험한 사건만 (기존과 중복 제외)
- facts: 새로 알게 됐거나 변경된 사실만 (기존과 동일하면 제외)
- relationships: 변화가 있는 관계만 (변화 없으면 제외)
- self_state: 항상 포함 (기존과 동일해도)
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_existing(episodes: list, facts: list, relationships: list, self_state: str | None) -> str:
    lines: list[str] = []

    if self_state:
        lines.append(f"현재 상태: {self_state}")

    if facts:
        lines.append("알고 있는 사실:")
        for f in facts:
            pct = int(float(f["confidence"]) * 100)
            lines.append(f"  - {f['fact']} (확신 {pct}%)")

    if relationships:
        lines.append("인물 관계:")
        for r in relationships:
            lines.append(f"  - {r['target_key']}: {r['stance']} — {r['reason']}")

    if episodes:
        lines.append("경험한 사건 (최근 10개):")
        for ep in episodes[-10:]:
            lines.append(f"  - (Wave {ep['wave']}) {ep['event']} [중요도 {ep['importance']}]")

    return "\n".join(lines) if lines else "없음 (첫 번째 압축)"


def _format_messages(messages: list[dict]) -> str:
    lines: list[str] = []
    for m in messages:
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
) -> str | None:
    """Format the agent's structured memory as a prompt injection block.

    Returns None when no memory exists yet (first cycles before any compression).
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
        for f in facts:
            pct = int(float(f["confidence"]) * 100)
            lines.append(f"  - {f['fact']} (확신 {pct}%)")

    if relationships:
        lines.append("■ 인물 관계:")
        for r in relationships:
            display = alias.get(r["target_key"], r["target_key"])
            lines.append(f"  - {display}: {r['stance']} — {r['reason']}")

    if episodes:
        lines.append("■ 경험한 사건:")
        for ep in episodes[-10:]:
            lines.append(f"  - (Wave {ep['wave']}) {ep['event']} [중요도 {ep['importance']}]")

    return "\n".join(lines)


def compress(
    agent_name: str,
    agent_key: str,
    sim_id: str,
    messages: list[dict],
    wave: int,
    db: SimDB,
    model: str,
    base_url: str,
    api_timeout: int,
    key_to_alias: dict | None = None,
    llm_max_tokens: int = 16384,
) -> str | None:
    """Run delta compression for one agent.

    Archives raw messages to DB, updates structured memory tables,
    and returns the refreshed memory block string (or None on failure).
    """
    if not messages:
        return build_memory_block(sim_id, agent_key, db, key_to_alias)

    episodes      = db.get_episodes(sim_id, agent_key)
    facts         = db.get_facts(sim_id, agent_key)
    relationships = db.get_relationships(sim_id, agent_key)
    self_state    = db.get_self_state(sim_id, agent_key)

    existing_text = _format_existing(episodes, facts, relationships, self_state)
    messages_text = _format_messages(messages)

    prompt = _COMPRESSION_PROMPT.format(
        agent_name=agent_name,
        existing_memory=existing_text,
        messages_text=messages_text,
        wave=wave,
    )

    try:
        raw, _, _ = chat_response(
            [
                {"role": "system", "content": _COMPRESSION_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            model, base_url, api_timeout,
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
        db.upsert_episodes(sim_id, agent_key, data["episodes"], wave)
    if data.get("facts"):
        db.upsert_facts(sim_id, agent_key, data["facts"], wave)
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

    return build_memory_block(sim_id, agent_key, db, key_to_alias)
