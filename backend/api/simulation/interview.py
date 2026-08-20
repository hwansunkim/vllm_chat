"""사후 인터뷰(post-simulation interview) 컨텍스트 조립.

끝난 run 하나를 골라 특정 에이전트에게 질문을 던지는 기능의 프롬프트 조립부.
시뮬레이션 실행 경로와 완전히 분리돼 있다 — 여기서 만든 메시지는 어떤
에이전트의 워킹 메모리에도 추가되지 않고, 답변은 `simulation_log`가 아니라
`interview_log`에만 저장된다(리플레이 오염 방지).

프롬프트 조립 순서는 `ABM.agent.Agent.build_messages()`를 그대로 재사용한다:
    [system] + background_log + [memory_block] + agent.memory + [ephemeral]
다만 system 메시지만 인터뷰 전용으로 교체한다. 평소 system 메시지에는 JSON
액션 출력 포맷(`_build_output_format`)이 붙는데, 인터뷰 답변은 자연어 산문이어야
하기 때문이다.
"""
from __future__ import annotations

import logging
import os
from typing import NamedTuple

from .schemas import SimStartConfig


logger = logging.getLogger(__name__)


# full_log 모드에서 대화록에 허용할 **토큰** 예산의 기본값.
# 문자 수 예산은 한국어에서 토큰 상한 구실을 전혀 못 한다(UTF-8 3바이트/자 → 120,000자가
# 6만 토큰 이상). 실제로는 build_interview_messages() 가 run 설정의 token_limit 에서
# 나머지 메시지 토큰을 뺀 값을 넘겨주므로, 아래 값은 직접 호출용 폴백일 뿐이다.
_FULL_LOG_TOKEN_BUDGET = 4096

# 대화록에 최소한 남겨줄 토큰(이보다 작아지면 기록이 사실상 사라진다).
_TRANSCRIPT_MIN_TOKEN_BUDGET = 512

# 헤더/생략 마커 + system 프롬프트에 덧붙는 절삭 안내문 등 사후에 늘어나는 분량 여유.
_TRANSCRIPT_OVERHEAD_TOKENS = 64
_INTERVIEW_RESERVE_TOKENS   = 256

# 남는 자리 중 [나의 기억 요약]에 최대 몇 %까지 줄지. 평상시 한도(8k~16k)에서는
# 걸리지 않고, 한도가 빡빡할 때만 발동해 워킹 메모리/대화록 자리를 남긴다.
_MEMORY_BLOCK_SHARE = {"memory_only": 0.7, "full_log": 0.4}


_INTERVIEW_COMMON = (
    "[인터뷰 안내]\n"
    "지금은 시뮬레이션이 모두 끝난 뒤이며, 당신은 인터뷰어와 1:1로 마주 앉아 "
    "질문을 받고 있습니다.\n"
    "- 위 설정의 인물({name})로서, 그 인물의 말투와 성격을 유지한 채 답하십시오.\n"
    "- 답변은 JSON이 아니라 자연스러운 한국어 산문으로 작성하십시오. "
    "action_note·target 같은 필드나 코드블록을 쓰지 마십시오.\n"
    "- 이 인터뷰는 시뮬레이션 이야기의 일부가 아닙니다. 새로운 사건을 만들거나 "
    "다른 인물의 대사를 지어내지 마십시오.\n"
)

_INTERVIEW_MEMORY_ONLY = (
    "- 오직 당신이 직접 겪고 기억하는 것(위의 기억 요약과 당신이 주고받은 대화)에만 "
    "근거해 답하십시오.\n"
    "- 당신이 그 자리에 없었거나 듣지 못한 일은 모른다고 말하십시오. 추측은 추측이라고 "
    "분명히 밝히십시오.\n"
)

_INTERVIEW_FULL_LOG = (
    "- 아래 시뮬레이션 기록에는 당신이 직접 보지 못한 장면까지 포함된 "
    "모든 참여자의 발화가 시간순으로 담겨 있습니다.\n"
    "- 인물의 목소리는 유지하되, 이 기록을 다시 읽어본 사람으로서 메타적으로 "
    "회고하며 답하십시오. 필요하면 당시에는 몰랐던 사실을 지금 알게 됐다고 말해도 "
    "됩니다.\n"
)

# 분량 제한으로 기록 앞부분이 잘렸을 때만 덧붙이는 경고. 이게 없으면 모델이
# "모든 발화가 담겨 있다"는 위 문장을 믿고 없는 초반부를 지어낸다.
_INTERVIEW_TRUNCATED = (
    "- 단, 아래 기록은 분량 제한 때문에 **앞부분 {omitted}개 항목이 생략된 상태**입니다. "
    "당신이 지금 읽고 있는 것은 시뮬레이션의 뒷부분뿐입니다.\n"
    "- 생략된 앞부분에 대해서는 당신의 [나의 기억 요약]에 남아 있는 범위 안에서만 말하고, "
    "그 밖의 일은 지어내지 말고 \"기록이 남아 있지 않아 정확히는 모르겠다\"고 답하십시오.\n"
)


def build_interview_system_prompt(
    system_prompt: str,
    name: str,
    mode: str,
    truncated: bool = False,
    omitted: int = 0,
) -> str:
    """원래 페르소나 system_prompt + 인터뷰 전용 지시. JSON 출력 포맷은 붙이지 않는다.

    `truncated=True` 면 기록 앞부분이 잘렸다는 사실을 지시문에 명시한다
    (전체를 보고 있다고 착각한 채 회고를 지어내는 것 방지).
    """
    if mode == "memory_only":
        tail = _INTERVIEW_MEMORY_ONLY
    else:
        tail = _INTERVIEW_FULL_LOG
        if truncated:
            tail += _INTERVIEW_TRUNCATED.format(omitted=omitted)
    return f"{system_prompt}\n\n{_INTERVIEW_COMMON.format(name=name)}{tail}"


def format_full_memory(
    memory: dict,
    key_to_alias: dict[str, str] | None = None,
    token_budget: int | None = None,
) -> str | None:
    """`SimDB.get_full_memory()` 결과를 프롬프트용 블록으로 포맷.

    `ABM.memory_compressor.build_memory_block()`과 같은 레이아웃이지만, 인터뷰는
    회고가 목적이라 에피소드를 최근 10건으로 자르지 않고 전부 싣는다.

    `token_budget` 을 주면 그 안에 들어가도록 오래된 에피소드 → 오래된 사실
    순으로 덜어낸다(자기 상태·인물 관계는 짧고 회고 가치가 높아 유지).
    긴 run 에서는 이 블록만으로 수천 토큰이 되므로 상한이 필요하다.
    """
    from ABM.agent import _estimate_tokens

    episodes      = memory.get("episodes") or []
    facts         = memory.get("facts") or []
    relationships = memory.get("relationships") or []
    self_state    = memory.get("self_state") or ""

    if not any([episodes, facts, relationships, self_state]):
        return None

    alias = key_to_alias or {}

    fact_lines = [
        f"  - {f['fact']} (확신 {int(float(f['confidence']) * 100)}%)" for f in facts
    ]
    ep_lines = [
        f"  - (Wave {ep['wave']}) {ep['event']} [중요도 {ep['importance']}]" for ep in episodes
    ]

    def render(f_lines: list[str], e_lines: list[str],
               f_omitted: int = 0, e_omitted: int = 0) -> str:
        lines = ["[나의 기억 요약]"]
        if self_state:
            lines.append(f"■ 현재 상태: {self_state}")
        if f_lines or f_omitted:
            lines.append("■ 알고 있는 사실:")
            if f_omitted:
                lines.append(f"  - ... (오래된 사실 {f_omitted}건 생략) ...")
            lines.extend(f_lines)
        if relationships:
            lines.append("■ 인물 관계:")
            for r in relationships:
                display = alias.get(r["target_key"], r["target_key"])
                lines.append(f"  - {display}: {r['stance']} — {r['reason']}")
        if e_lines or e_omitted:
            lines.append("■ 경험한 사건:")
            if e_omitted:
                lines.append(f"  - ... (오래된 사건 {e_omitted}건 생략) ...")
            lines.extend(e_lines)
        return "\n".join(lines)

    text = render(fact_lines, ep_lines)
    if token_budget is None or _estimate_tokens(text) <= token_budget:
        return text

    # 예산 초과 — 오래된 에피소드부터, 그래도 넘치면 오래된 사실부터 덜어낸다.
    e_start, f_start = 0, 0
    while e_start < len(ep_lines):
        e_start += 1
        text = render(fact_lines, ep_lines[e_start:], 0, e_start)
        if _estimate_tokens(text) <= token_budget:
            break
    while _estimate_tokens(text) > token_budget and f_start < len(fact_lines):
        f_start += 1
        text = render(fact_lines[f_start:], ep_lines[e_start:], f_start, e_start)

    logger.warning(
        "인터뷰 기억 요약이 토큰 예산(%d)을 초과해 사건 %d/%d건·사실 %d/%d건을 생략했습니다.",
        token_budget, e_start, len(ep_lines), f_start, len(fact_lines),
    )
    return text


class RunTranscript(NamedTuple):
    """`format_run_transcript()` 결과. 절삭 여부를 호출부(system 프롬프트)로 전달한다."""
    text:    str
    omitted: int   # 예산 때문에 앞에서 잘라낸 항목 수
    total:   int   # 원래 항목 수

    @property
    def truncated(self) -> bool:
        return self.omitted > 0


def format_run_transcript(
    log_entries: list[dict],
    key_to_alias: dict[str, str] | None = None,
    focus_agent: str = "",
    token_budget: int = _FULL_LOG_TOKEN_BUDGET,
) -> RunTranscript:
    """`simulation_log` 전체를 시간순 대화록 텍스트로 변환한다.

    `token_budget`(추정 토큰) 을 넘으면 오래된 항목부터 잘라내고, 잘린 개수를
    함께 돌려준다. 예산 단위가 문자가 아니라 토큰인 것이 중요하다 — 한국어는
    문자당 토큰 비율이 높아 문자 예산으로는 컨텍스트 초과를 못 막는다.
    """
    from ABM.agent import _estimate_tokens

    alias = key_to_alias or {}
    rows: list[str] = []
    for e in log_entries:
        speaker = e.get("speaker", "")
        display = alias.get(speaker, speaker)
        if speaker and speaker == focus_agent:
            display = f"{display} (나)"
        stamp = f"Wave {e.get('wave', 0)}"
        if e.get("time_str"):
            stamp = f"{stamp} · {e['time_str']}"
        line = f"[{stamp}] {display}: {(e.get('content') or '').strip()}"
        note = (e.get("action_note") or "").strip()
        if note:
            line += f"  ({note})"
        targets = [t for t in (e.get("targets") or []) if t not in ("all", "self")]
        if targets:
            line += "  → " + ", ".join(alias.get(t, t) for t in targets)
        rows.append(line)

    total  = len(rows)
    budget = max(_TRANSCRIPT_MIN_TOKEN_BUDGET, int(token_budget)) - _TRANSCRIPT_OVERHEAD_TOKENS

    # 항목별 비용을 한 번만 계산하고 앞에서부터 커서를 밀어 O(n) 으로 절삭한다
    # (매번 전체 합을 다시 구하면 수천 턴짜리 run 에서 O(n²) 로 터진다).
    costs = [_estimate_tokens(r) + 1 for r in rows]
    used  = sum(costs)
    start = 0
    while start < total and used > budget:
        used  -= costs[start]
        start += 1

    omitted = start
    kept    = rows[start:]
    if omitted:
        logger.warning(
            "인터뷰 기록이 토큰 예산(%d)을 초과해 앞부분 %d/%d개 항목을 생략했습니다.",
            budget, omitted, total,
        )
        header = (
            f"[시뮬레이션 기록 (뒷부분만)] (모든 참여자의 발화, 시간순 · "
            f"분량 제한으로 앞부분 {omitted}/{total}개 항목 생략)"
        )
        kept.insert(0, f"... (앞부분 {omitted}개 항목 생략) ...")
    else:
        header = "[시뮬레이션 전체 기록] (모든 참여자의 발화, 시간순)"

    body = "\n".join(kept) if kept else "(기록 없음)"
    return RunTranscript(f"{header}\n{body}", omitted, total)


def resolve_server_id(cfg: SimStartConfig, agent_name: str) -> str | None:
    """에이전트별 server_id → 실행 기본 server_id 순으로 해석.

    `runtime._make_agent_llm_map()`과 같은 규칙: 삭제/비활성 server_id는 무시하고
    실행 기본 서버로 폴백한다(엉뚱한 유료 서버로 새는 것 방지).
    """
    agent_sid = next((a.server_id for a in cfg.agents if a.name == agent_name), None)
    if not agent_sid:
        return cfg.server_id
    from ...llm.registry import get_registry
    if get_registry().get_provider(agent_sid) is None:
        logger.warning(
            "[%s] 인터뷰: server_id=%r 를 찾을 수 없어 실행 기본 서버로 폴백합니다.",
            agent_name, agent_sid,
        )
        return cfg.server_id
    return agent_sid


def resolve_temperature(cfg: SimStartConfig, agent_name: str) -> float:
    """에이전트별 temperature → 실행 기본 temperature 순으로 해석.

    인터뷰는 자체 온도를 갖지 않는다 — 실행 때 그 인물을 연기하던 설정을 그대로
    쓰는 편이 말투가 일관된다. `runtime._make_agent_llm_map()`과 같은 규칙:
    에이전트 값이 None이면 실행 기본값(cfg.temperature)을 쓴다.
    """
    agent_cfg = next((a for a in cfg.agents if a.name == agent_name), None)
    if agent_cfg is None or agent_cfg.temperature is None:
        return cfg.temperature
    return agent_cfg.temperature


def effective_token_limit(
    cfg: SimStartConfig, server_id: str | None, max_tokens: int
) -> int:
    """인터뷰 프롬프트가 실제로 지켜야 할 토큰 상한.

    기본은 run 설정의 `token_limit` 이지만, provider 가 모델 컨텍스트 길이를 알고
    있으면 `model_len - 답변 max_tokens - 여유` 로 한 번 더 조인다. 프롬프트가
    한도 안이어도 프롬프트+답변이 컨텍스트를 넘으면 provider 가 400을 뱉기 때문.
    """
    limit = int(cfg.token_limit)
    model_len = 0
    try:
        from ...llm.registry import get_registry
        registry = get_registry()
        provider = registry.get_provider(server_id) if server_id else None
        if provider is None:
            provider = registry.get_default()
        model_len = int(getattr(provider, "model_len", 0) or 0)
    except Exception:  # registry 미초기화 등 — 조용히 run 설정값만 쓴다
        logger.debug("인터뷰 토큰 상한 계산 중 registry 조회 실패", exc_info=True)
    if model_len > 0:
        limit = min(limit, model_len - int(max_tokens or 0) - _INTERVIEW_RESERVE_TOKENS)
    return max(2 * _TRANSCRIPT_MIN_TOKEN_BUDGET, limit)


def _assemble(
    agent,
    system_content: str,
    background_log: list[dict],
    key_to_alias: dict[str, str],
    ephemeral: list[dict],
) -> list[dict]:
    """`Agent.build_messages()` 로 조립하고 system 메시지만 인터뷰용으로 교체."""
    msgs = agent.build_messages(background_log, [], key_to_alias, None, ephemeral_msgs=ephemeral)
    msgs[0] = {"role": "system", "content": system_content}
    return msgs


def build_interview_messages(
    run_id: str,
    agent_key: str,
    question: str,
    mode: str,
    cfg: SimStartConfig,
    db,
    token_limit: int | None = None,
) -> list[dict]:
    """인터뷰 프롬프트 메시지 배열을 조립한다.

    memory_only: [interview system] + background + [기억 요약] + 본인 워킹 메모리 + [질문]
    full_log:    [interview system] + background + [기억 요약] + [전체 대화록] + [질문]

    full_log 에서 본인 워킹 메모리를 빼는 이유 — 전체 대화록이 그 내용을 포함하는
    상위집합이라 그대로 두면 같은 발화가 두 번 들어가고 컨텍스트만 커진다.

    `token_limit` 을 주면 그 값을, 없으면 `cfg.token_limit` 을 프롬프트 상한으로
    삼는다. 어느 모드든 상한을 실제로 지키도록 오래된 항목부터 잘라낸다.
    """
    from ABM.agent import Agent, _msg_tokens
    from ABM.config import LOG_DIR

    agent_cfg = next((a for a in cfg.agents if a.name == agent_key), None)
    if agent_cfg is None:
        raise KeyError(agent_key)

    key_to_alias = {a.name: a.display_name for a in cfg.agents if a.display_name.strip()}
    display_name = key_to_alias.get(agent_key, agent_key)
    limit = int(token_limit if token_limit is not None else cfg.token_limit)

    # Agent.__init__ 은 log_dir/{name}.json 을 빈 배열로 덮어쓴다. 실제 실행 로그를
    # 날리지 않도록 인터뷰 전용 스크래치 디렉터리를 쓴다.
    scratch_dir = os.path.join(LOG_DIR, "_interview")
    agent = Agent(
        agent_cfg.name,
        agent_cfg.system_prompt,
        scratch_dir,
        token_limit=limit,
        extra_fields=[f.model_dump() for f in cfg.extra_fields],
        output_format_template=cfg.output_format_template or None,
    )

    question_msg = {
        "role": "user",
        "content": f"[인터뷰어의 질문]\n{question}\n\n({display_name}로서 한국어 산문으로 답하십시오.)",
    }
    background_log = [{"role": "user", "content": f"[배경] {cfg.background}"}]

    # 기억 요약을 넣기 전(system+배경+질문)의 고정 비용부터 잰다. 남는 자리 안에서만
    # 기억 요약을 싣고, full_log 는 그 뒤에 남은 자리를 대화록에 준다.
    agent._memory_block = None
    probe_system = build_interview_system_prompt(
        agent_cfg.system_prompt, display_name, mode, truncated=(mode != "memory_only"), omitted=0,
    )
    fixed_tokens = sum(
        _msg_tokens(m)
        for m in _assemble(agent, probe_system, background_log, key_to_alias, [question_msg])
    )
    room = max(0, limit - fixed_tokens - _INTERVIEW_RESERVE_TOKENS)
    memory_budget = int(room * _MEMORY_BLOCK_SHARE.get(mode, 0.4))

    # 구조화 RAG 기억 (semantic / episodic / relationship / self_state).
    # 메모리 테이블은 sim_id 로 키가 잡히고, 실행에서는 sim_id == run_id 다.
    agent._memory_block = format_full_memory(
        db.get_full_memory(run_id, agent_key), key_to_alias, token_budget=memory_budget,
    ) if memory_budget > 0 else None

    if mode == "memory_only":
        # 종료 시점 워킹 메모리 복원 (finalize_run 이 저장한 agent_snapshots).
        snapshot = db.get_agent_snapshots(run_id).get(agent_key) or []
        agent.memory = list(snapshot)
        # 실행 경로와 동일하게 token_limit 을 실제로 적용한다(오래된 발화부터 제거).
        agent.trim_to_token_limit(
            background_log, [], key_to_alias, None, ephemeral_msgs=[question_msg],
        )
        if agent._trimmed_count:
            logger.warning(
                "[%s] 인터뷰(memory_only): token_limit(%d) 때문에 워킹 메모리 %d개를 잘랐습니다.",
                agent_key, limit, agent._trimmed_count,
            )
        system_content = build_interview_system_prompt(agent_cfg.system_prompt, display_name, mode)
        ephemeral: list[dict] = [question_msg]
    else:  # full_log
        agent.memory = []
        # 대화록을 뺀 나머지(system·배경·기억 요약·질문)를 다시 재고 남은 만큼만 배정한다.
        # 절삭 안내가 붙은 쪽이 system 이 더 길므로 probe_system 으로 보수적으로 잰다.
        base_tokens = sum(
            _msg_tokens(m)
            for m in _assemble(agent, probe_system, background_log, key_to_alias, [question_msg])
        )
        budget = limit - base_tokens - _INTERVIEW_RESERVE_TOKENS
        if budget < _TRANSCRIPT_MIN_TOKEN_BUDGET:
            logger.warning(
                "[%s] 인터뷰(full_log): 대화록을 빼고도 %d 토큰이라 token_limit(%d)에 여유가 "
                "거의 없습니다. 대화록은 최소 예산(%d)만 싣습니다.",
                agent_key, base_tokens, limit, _TRANSCRIPT_MIN_TOKEN_BUDGET,
            )
            budget = _TRANSCRIPT_MIN_TOKEN_BUDGET

        transcript = format_run_transcript(
            db.get_run_log(run_id), key_to_alias, focus_agent=agent_key, token_budget=budget,
        )
        # H-2: 실제로 잘렸으면 system 지시에도 그 사실을 명시한다.
        system_content = build_interview_system_prompt(
            agent_cfg.system_prompt, display_name, mode,
            truncated=transcript.truncated, omitted=transcript.omitted,
        )
        ephemeral = [{"role": "user", "content": transcript.text}, question_msg]

    # 조립 순서는 build_messages()에 위임하고, system 메시지만 인터뷰용으로 교체한다.
    msgs = _assemble(agent, system_content, background_log, key_to_alias, ephemeral)

    est = sum(_msg_tokens(m) for m in msgs)
    if est > limit:
        logger.warning(
            "[%s] 인터뷰 프롬프트가 token_limit 을 초과합니다 (%d > %d, mode=%s). "
            "system_prompt/배경/기억 요약이 이미 한도를 넘었을 수 있습니다.",
            agent_key, est, limit, mode,
        )
    return msgs


def extract_answer(content: str) -> str:
    """페르소나 프롬프트 습관으로 JSON을 뱉은 경우 content 필드만 뽑아낸다."""
    text = (content or "").strip()
    if not text.startswith(("{", "```")):
        return text
    from ...llm.utils import parse_json
    data = parse_json(text)
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        answer = data["content"].strip()
        note   = (data.get("action_note") or "").strip() if isinstance(data.get("action_note"), str) else ""
        return f"{answer}\n\n({note})" if note else answer
    return text
