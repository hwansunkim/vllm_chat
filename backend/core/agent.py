from __future__ import annotations

import logging
import re
import sqlite3

from ..llm.client import async_llm

logger = logging.getLogger(__name__)

RoutingMethod = str


def build_agent_system_prompt(agent: dict) -> str:
    parts = []
    if agent.get("role"):
        parts.append(f"당신은 {agent['role']}입니다.")
    if agent.get("goal"):
        parts.append(f"목표: {agent['goal']}")
    if agent.get("backstory"):
        parts.append(f"배경: {agent['backstory']}")
    return "\n".join(parts) if parts else (agent.get("system_prompt") or "")


def resolve_agent_mention(content: str, conn: sqlite3.Connection) -> tuple[str, dict | None]:
    m = re.match(r"^@(\S+)\s*(.*)", content.strip(), re.DOTALL)
    if not m:
        return content, None
    name, rest = m.group(1), m.group(2).strip()
    row = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    if row:
        return rest, dict(row)
    return content, None


async def async_route_agent(
    user_input: str,
    agents: list[dict],
) -> tuple[dict | None, RoutingMethod]:
    if not agents:
        return None, "fallback"

    descriptions = "\n".join(
        f"- {a['name']}: {a.get('description') or a.get('role') or '설명 없음'}"
        for a in agents
    )
    names  = ", ".join(a["name"] for a in agents)
    prompt = f"""\
다음 전문 에이전트 중 사용자 요청에 가장 적합한 에이전트를 하나 선택하세요.

[에이전트 목록]
{descriptions}

[사용자 요청]
{user_input}

규칙: 에이전트의 이름만 정확히 출력하세요. 다른 설명은 절대 하지 마세요.
선택 가능한 이름: [{names}]"""

    try:
        raw      = (await async_llm(prompt, max_tokens=30, temperature=0)).strip()
        selected = raw.split()[0].strip('.,!?\'"')
        for a in agents:
            if a["name"] == selected:
                return a, "router"
    except Exception as e:
        logger.error("[route_agent] LLM 오류: %s", e)

    return agents[0], "fallback"
