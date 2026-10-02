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
import math
from collections import Counter
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
    {{"fact": "<사실/믿음>", "confidence": <0.0-1.0>, "matches_id": <후보 id 또는 null>, "judgment": "<신규|동일_의미|변경|보류>", "prev_fact": "<변경 전 사실, 변경 없으면 생략>", "prev_confidence": <float, 변경 없으면 생략>}}
  ],
  "relationships": [
    {{"target": "<에이전트키>", "stance": "<trust|neutral|suspect|hostile>", "reason": "<이유>"}}
  ],
  "self_state": "<{agent_name}의 현재 동기·감정 상태를 한 문장으로>"
}}

규칙:
- episodes: 이번 대화에서 새로 경험한 사건만 (기존과 중복 제외). "몇 번째 wave"인지는
  적지 마세요 — 시스템이 실제 시각을 따로 기록합니다.
- episodes의 사건 문장에는 다음을 우선 담으세요: 무엇을 원했는지(요구), 무엇을
  했는지(행동), 상대가 어떻게 반응했는지(반응), 실제로 무엇이 바뀌었는지(변화),
  아직 해결되지 않은 요구나 지키지 않은 약속이 있는지(미해결). 단, "여러 번
  실패했으니 앞으로는 포기한다" 같은 앞으로의 행동 방침까지 추론해서 지어내지는
  마세요 — 그건 사건 요약의 범위를 넘는 추론입니다.
- facts: 시간이 지나도 안 바뀌는 "계속 참인 것"만 — 성격·취향·습관·지속되는 관계.
  오늘 하루만 해당하는 일(오늘 메뉴, 오늘 기분, 숙제를 끝냈는지처럼 매일 달라지는
  것)은 facts가 아니라 episodes로 적으세요. facts에 매번 다른 값이 쌓이면
  "화요일엔 X, 수요일엔 Y" 식으로 모순돼 보입니다.
- facts/episodes에 "오늘"·"어제"·"이번 주" 같은 상대적 시간 표현을 쓰지 마세요.
  위 구획 헤더의 일차·요일을 보고 "9일차 화요일 저녁 메뉴는..." 처럼 **일차를
  포함해서** 적으세요. 요일만 적으면(예: "화요일") 몇 주가 지나면 어느 화요일인지
  구분이 안 돼 기억이 혼선됩니다.
- facts의 judgment/matches_id: 위 [기존 구조화 기억]의 "알고 있는 사실" 후보 목록의 id와
  비교하세요 — 완전히 새로운 내용이면 "신규", 후보 중 하나와 같은 의미를 다른 말로
  재진술한 것뿐이면 그 id를 matches_id에 적고 "동일_의미", 후보와 같은 사실이 이어지지만
  기간·시점·정도가 갱신됐거나(예: "6일차까지 …" → "7일차까지 …") 내용이 달라졌거나
  모순되면 그 id를 적고 "변경", 비슷한 것 같지만 확실하지 않으면 "보류"(matches_id는
  비움)로 적으세요. 신규·보류는 평소처럼 새 사실로 저장됩니다. 후보 목록에 없는 id는
  적지 마세요.
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
# 후보 사실 검색 (기억 메커니즘 2~3단계) — 압축 LLM 이 id 로 지칭할 대상
# ---------------------------------------------------------------------------
#
# 예전 "기존 기억"은 확신 상위 15개 사실을 **id 없이** 보여줬다. 긴 실행에서는 사실이
# 수백 개라 이번 배치와 관련된 기존 사실이 상위 15개 밖에 있으면 LLM 이 그 존재를 몰라
# 같은 사실을 새로 쓰고, 날짜만 바뀐 같은 사실이 문자열이 달라 새 행으로 쌓였다. 이제
# 전체 사실(`db.get_all_facts`) 중 이번 배치 원문과 겹치는 것을 코드가 먼저 추리고(후보
# 검색 — 프롬프트 크기 제한), LLM 은 그 후보 id 로 동일/변경을 판정하며, DB 계층이 id 의
# 소속을 검증한다(`ABM/db/semantic.py`).
#
# 유사도는 **IDF 가중 문자 바이그램 포함률** — 사실 문장의 문자 2-gram 중 이번 배치
# 원문에도 나오는 것의 (IDF 가중) 비율. IDF 는 그 에이전트의 사실 전체 기준이라 어미·이름
# 같은 흔한 조각은 신호에서 빠진다(`_select_candidate_facts`). 한국어는 띄어쓰기·조사 변화가 커서 단어 단위 비교가 잘 안 맞고,
# `difflib.SequenceMatcher`를 짧은 사실 vs 긴 배치 원문에 그대로 쓰면 길이 차이 때문에
# 점수가 거의 0 으로 눌린다. 포함률은 사실 길이로 정규화돼 짧은 사실도 공정하다.

_CANDIDATE_FACT_LIMIT = 12


def _char_bigrams(text: str) -> set[str]:
    t = "".join((text or "").split())     # 공백 제거 — 띄어쓰기 차이를 무시한다
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _select_candidate_facts(
    all_facts: list[dict], messages_text: str, limit: int = _CANDIDATE_FACT_LIMIT,
) -> list[dict]:
    """이번 배치와 관련된 기존 사실 후보(최대 `limit`개, id 포함).

    1) IDF 가중 문자 바이그램 포함률(위 설명)이 0 보다 큰 사실을 점수 내림차순으로 — 동점이면
       확신 높은 것, 그다음 최근 행(id 큰 것).
    2) 자리가 남으면 아직 안 고른 사실 중 확신 높은 것으로 채운다 — 관련도가 낮아도
       에이전트의 핵심 사실을 LLM 이 계속 알고 있게(예전 "확신 상위 N개" 표시의 역할).
    반환 순서는 1)의 순서 뒤에 2)의 순서다.
    """
    if not all_facts or limit <= 0:
        return []
    msg_grams = _char_bigrams(messages_text)
    fact_grams = {f["id"]: _char_bigrams(f.get("fact", "")) for f in all_facts}

    # IDF 가중치 — 그 에이전트의 사실 전체에서 흔한 바이그램("있다"·"는다"·"짱구"·"엄마"
    # 같은 어미·이름 조각)은 관련성 신호가 아니다. 가중치가 없으면 이런 조각을 많이 공유하는
    # 무관한 일상 사실이 진짜 관련 사실(희귀한 "냄새"·"코로"를 가진)보다 점수가 높아져 상한
    # 밖으로 밀어낸다(QA 실측: 19개 중 코로나 사실이 16위 → LLM 이 id 로 지칭 못 해 중복 재발).
    # 모든 사실에 나오는 바이그램은 가중치 0 이 된다.
    n = len(all_facts)
    df = Counter(g for grams in fact_grams.values() for g in grams)

    def idf(g: str) -> float:
        return math.log((1 + n) / (1 + df[g]))

    def score(f: dict) -> float:
        grams = fact_grams[f["id"]]
        total = sum(idf(g) for g in grams)
        return sum(idf(g) for g in grams & msg_grams) / total if total > 0 else 0.0

    scored = [(score(f), f) for f in all_facts]
    related = sorted(
        (sf for sf in scored if sf[0] > 0),
        key=lambda sf: (-sf[0], -float(sf[1].get("confidence", 0)), -int(sf[1].get("id", 0))),
    )
    picked = [f for _, f in related[:limit]]
    if len(picked) < limit:
        chosen = {f["id"] for f in picked}
        rest = sorted(
            (f for f in all_facts if f["id"] not in chosen),
            key=lambda f: (-float(f.get("confidence", 0)), -int(f.get("id", 0))),
        )
        picked.extend(rest[:limit - len(picked)])
    return picked


def _candidate_fact_lines(candidates: list[dict], total: int) -> list[str]:
    lines = [
        f"  - [id={f['id']}] {f['fact']} (확신 {int(float(f['confidence']) * 100)}%)"
        for f in candidates
    ]
    if total > len(candidates):
        lines.append(f"  - (이번 대화와 관련이 적은 다른 사실 {total - len(candidates)}건은 생략)")
    return lines


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_existing(
    episodes: list, facts: list, relationships: list, self_state: str | None,
    now_elapsed: int, *, fact_total: int | None = None,
) -> str:
    """압축 LLM 에게 보여줄 "기존 구조화 기억". `facts`는 후보 사실(id 포함 —
    `_select_candidate_facts`)이고, `fact_total`은 전체 사실 수(생략 안내용)다."""
    lines: list[str] = []

    if self_state:
        lines.append(f"현재 상태: {self_state}")

    if facts:
        lines.append("알고 있는 사실 (후보 — 새 사실이 이 중 하나와 같거나 바뀐 것이면 id로 지칭):")
        lines.extend(_candidate_fact_lines(facts, fact_total if fact_total is not None else len(facts)))

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


_FACT_JUDGMENTS = ("신규", "동일_의미", "변경", "보류")


def _normalize_fact_judgment(f: dict) -> dict:
    """사실 하나의 `judgment`/`matches_id`를 안전하게 정규화한다.

    - 모르는 값·필드 없음(구버전 응답 등) → "신규" (안전한 폴백 — 예전과 같은 새 행 경로).
    - matches_id 는 정수만(bool·문자열 숫자 아님은 None). 숫자 문자열("42")은 정수로 읽는다.
    - 동일_의미·변경인데 matches_id 가 없으면 지칭 대상이 없으므로 "신규".
    - 신규·보류의 matches_id 는 버린다(보류는 "matches_id 비움"이 규칙).
    소속 검증(같은 sim_id·agent_key 인가)은 DB 계층(`upsert_facts`)이 한다.
    """
    j = f.get("judgment")
    if j not in _FACT_JUDGMENTS:
        j = "신규"
    mid = f.get("matches_id")
    if isinstance(mid, bool):
        mid = None
    elif isinstance(mid, str) and mid.strip().isdigit():
        mid = int(mid.strip())
    elif not isinstance(mid, int):
        mid = None
    if j in ("동일_의미", "변경") and mid is None:
        j = "신규"
    if j in ("신규", "보류"):
        mid = None
    # confidence: 숫자로 못 읽거나(None·문자열)·NaN 이면 기본값 1.0(`upsert_facts`의 기본값과
    # 같음), 범위 밖은 0~1 로 자른다 — 반영 루프에서 float() 가 터져 트랜잭션이 깨지지 않게.
    try:
        conf = float(f.get("confidence", 1.0))
        if conf != conf:      # NaN
            raise ValueError
    except (TypeError, ValueError):
        conf = 1.0
    conf = max(0.0, min(1.0, conf))
    return {**f, "judgment": j, "matches_id": mid, "confidence": conf}


def _parse_compression_result(raw: str) -> dict:
    """Parse LLM output; strip optional markdown code fence.

    `facts` 항목의 판정 필드(`judgment`/`matches_id`)는 `_normalize_fact_judgment`로
    정규화한다(2차 정리 응답처럼 facts 가 없는 결과는 그대로).
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        # 배열·문자열 등 — 호출부의 `data.get(...)`이 try 밖에서 AttributeError 로 터져
        # "실패하면 저장한 메시지를 지운다" 안전장치를 비켜가지 않도록 여기서 실패로 만든다.
        raise ValueError(f"압축 응답이 JSON 객체가 아님: {type(data).__name__}")
    if isinstance(data.get("facts"), list):
        data["facts"] = [
            _normalize_fact_judgment(f) for f in data["facts"]
            if isinstance(f, dict) and isinstance(f.get("fact"), str) and f["fact"].strip()
        ]
    return data


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
    all_facts     = db.get_all_facts(sim_id, agent_key)
    relationships = db.get_relationships(sim_id, agent_key)
    self_state    = db.get_self_state(sim_id, agent_key)

    messages_text = _format_messages(messages, sim_start_minutes, start_weekday_idx)
    # 후보 사실(id 포함) — 이번 배치와 겹치는 기존 사실을 코드가 먼저 추린다.
    candidates    = _select_candidate_facts(all_facts, messages_text)
    existing_text = _format_existing(
        episodes, candidates, relationships, self_state, now_elapsed, fact_total=len(all_facts),
    )

    prompt = _COMPRESSION_PROMPT.format(
        agent_name=agent_name,
        existing_memory=existing_text,
        messages_text=messages_text,
    )

    # 원문을 LLM 호출 **전에** 보관해 근거 id 를 확보한다(사실의 source_message_ids).
    # 호출·파싱이 실패하면 방금 넣은 행만 지워 "실패한 배치는 보관하지 않는다"는 예전
    # 보장을 유지한다(`SimDB.delete_messages` 참고 — 트랜잭션을 호출 동안 열어 두지 않는다).
    source_ids = db.save_messages(sim_id, agent_key, messages, wave)

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
        db.delete_messages(source_ids)
        return None

    # 쓰기 단계. 실패하면 먼저 저장한 원문도 지운다(LLM 실패와 같은 정리 — QA 실측: upsert
    # 실패 후 메시지가 남았다). 사실 반영을 **맨 앞**에 둔다 — 가장 복잡한 쓰기라 실패하면
    # 그 트랜잭션만 롤백되고(semantic.upsert_facts) 아직 아무것도 안 쓴 상태로 정리된다.
    # 예외는 예전처럼 그대로 올린다(호출부 동작 불변).
    # 단, 사실 반영이 이미 커밋된 뒤의 실패라면 원문을 지우지 않는다 — 커밋된 사실의
    # source_message_ids 가 그 원문을 가리키므로, 지우면 근거가 허공을 가리킨다(QA 재검증).
    facts_done = False
    try:
        if data.get("facts"):
            # candidate_ids — 이번 호출에서 LLM 에게 실제로 보여준 후보 id. 이 밖의 matches_id 는
            # 받아들이지 않는다(보지도 못한 기억을 덮지 않게 — ABM/db/semantic.py).
            db.upsert_facts(sim_id, agent_key, data["facts"], wave, elapsed_minutes=now_elapsed,
                            source_message_ids=source_ids,
                            candidate_ids=[f["id"] for f in candidates])
            facts_done = True
        db.log_compression(sim_id, agent_key, len(messages), wave)
        if data.get("episodes"):
            db.upsert_episodes(sim_id, agent_key, data["episodes"], wave, elapsed_minutes=now_elapsed)
        if data.get("relationships"):
            db.upsert_relationships(sim_id, agent_key, data["relationships"], wave)
        if data.get("self_state"):
            db.upsert_self_state(sim_id, agent_key, data["self_state"], wave)
    except BaseException:
        # 실패한 쓰기(예: upsert_relationships 중간)의 반쯤 열린 트랜잭션을 먼저 버린다 —
        # 그렇지 않으면 아래 delete_messages 의 commit 이 그 반쪽 쓰기까지 함께 커밋한다.
        db._conn().rollback()
        if facts_done:
            logger.error(f"[{agent_key}] 압축 결과 저장 실패(사실 반영 후) — 사실이 원문을 참조하므로 원문은 유지")
        else:
            logger.error(f"[{agent_key}] 압축 결과 저장 실패 — 이번 배치 원문 보관을 되돌린다")
            db.delete_messages(source_ids)
        raise

    logger.info(
        f"[{agent_key}] 압축 완료 — "
        f"msgs={len(messages)}, "
        f"episodes+={len(data.get('episodes', []))}, "
        f"facts+={len(data.get('facts', []))}, "
        f"rels+={len(data.get('relationships', []))}"
    )

    return build_memory_block(sim_id, agent_key, db, key_to_alias, now_elapsed)


# ---------------------------------------------------------------------------
# 2차 기억 정리(consolidation) — "한 번뿐이면 옅어진다" (쇠퇴만; 강화는 임시 제거)
# ---------------------------------------------------------------------------
#
# 1차 압축(compress())은 새 대화 조각 하나만 보고 매번 독립적으로 판단하므로
# "이 사실이 전체 기억에서 몇 번째 반복인지"를 알 방법이 없다 — 그래서 표현만
# 바뀐 같은 이야기가 별개 행으로 계속 쌓였다(실측: "고등학생이며 수학 학원" /
# "학생이며 수학 학원"). 2차 정리는 그 에이전트의 전체 사실을 다시 조망하며
# 한 번만 언급되고 이후 전혀 뒷받침되지 않은 사소한 사실의 확신을 낮춘다(쇠퇴).
#
# ⚠ 예전엔 "여러 사실이 같은 이야기를 반복/뒷받침하면 확신을 높인다(강화)"도
# 했다. 그런데 `db.upsert_facts`는 문자열이 완전히 같을 때만 병합하므로, 표현만
# 다른 **중복 행**("괴물"→"블랙홀"→"은하계")을 LLM이 "독립적으로 뒷받침하는
# 사실"로 오인해 전부 끌어올렸다 — 반복된 주제가 기억에서 점점 "중요한 사실"로
# 격상되는 자기강화 루프(실측: "햄버거" 전체 wave의 48%, `_workspace/2026-10-01_
# memory_mechanism_proposal*.md`). 그래서 1단계 조치로 강화를 **임시로** 뺐다 —
# 프롬프트에서 규칙을 지우고, `consolidate_facts`가 확신을 올리는 조정은 코드에서도
# 반영하지 않는다. 포기한 게 아니다: 2~3단계(기억 ID·원문 근거 연결, 후보 검색,
# 원본 보존 대표 기억 통합)로 중복과 실제 반복을 구분할 수 있게 되면 "진짜 반복"
# 강화를 다시 넣을 수 있다.
#
# 행을 지우거나 병합하지 않는다 — 점수(confidence)만 바꾸고, 이미 있는 표시
# 상한(`_fact_lines`, 상위 `_FACT_SHOW_MAX`개)이 낮아진 확신을 보고 자연히
# 걸러내게 둔다. 그래서 되돌릴 수 있고(다음 정리 때 다시 오를 수 있음), 잘못
# 병합해 원문을 잃어버릴 위험이 없다. 트리거는
# `ABM/simulation/step.py::_maybe_consolidate_facts`(1차 압축 N번마다 한 번).

_CONSOLIDATION_SYSTEM = (
    "당신은 기억 정리 도우미입니다. 쌓인 사실들을 검토해 확신도를 재평가하세요. "
    "반드시 JSON만 출력하고 다른 텍스트는 절대 출력하지 마세요."
)

# "반복 확인된 사실의 확신을 높이세요" 규칙은 삭제됐다 — 표현만 다른 중복 행을 독립
# 증거로 오인해 자기강화 루프를 만들었다(위 블록 주석). 2~3단계에서 중복과 실제 반복을
# 구분할 수 있게 되면 "진짜 반복" 강화를 다시 넣는다(임시 제거).
_CONSOLIDATION_PROMPT = """\
{agent_name}에 대해 지금까지 쌓인 사실 목록입니다. 각 줄 앞의 id로 지칭하세요.

{facts_text}

이 목록을 검토해 확신도를 재평가하세요:
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
    """전체 사실을 다시 훑어 확신도를 **낮춘다**(2차 정리 — 쇠퇴만).

    강화(확신 상향)는 임시로 뺐다 — 표현만 다른 중복 행을 독립 증거로 오인해
    자기강화 루프를 만들었기 때문이다(위 블록 주석). 프롬프트에서 규칙을 지웠고,
    그래도 LLM이 올리는 조정을 내면 여기서 **반영하지 않는다**(현재 값보다 큰
    `new_confidence`는 무시). 2~3단계에서 진짜 반복을 구분할 수 있게 되면 되살린다.

    행을 지우거나 합치지 않는다 — 점수만 바꾸고 나머지는 `_fact_lines`의
    표시 상한에 맡긴다. 반환값은 실제로 반영된 조정 건수(관전/테스트용).
    """
    facts = db.get_all_facts(sim_id, agent_key)
    if len(facts) < _CONSOLIDATION_MIN_FACTS:
        return 0

    # 근거 배치 수를 함께 보여준다 — 여러 번 재진술돼 한 행으로 병합된 사실(2~3단계)이
    # 확신도만 보이면 "딱 한 번 언급된 사실"과 구분이 안 돼 쇠퇴 대상이 될 수 있다(병합의
    # 이점을 2차 정리가 거꾸로 깎는 상호작용). 근거를 모르는 옛 행은 표시하지 않는다.
    def _fact_line(f: dict) -> str:
        n = db.count_source_batches(f.get("source_message_ids"))
        support = f", {n}번의 대화에서 확인" if n else ""
        return f"[id={f['id']}] {f['fact']} (확신 {int(float(f['confidence']) * 100)}%{support})"

    facts_text = "\n".join(_fact_line(f) for f in facts)
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

    current = {f["id"]: float(f["confidence"]) for f in facts}
    applied = 0
    for adj in (data.get("adjustments") or []):
        try:
            fid  = int(adj["id"])
            conf = max(0.0, min(1.0, float(adj["new_confidence"])))
        except (KeyError, TypeError, ValueError):
            continue
        if fid not in current:
            continue
        if conf > current[fid]:
            # 강화는 임시 제거(1단계) — 프롬프트 규칙을 지워도 모델이 습관적으로
            # "반복 확인"을 올릴 수 있어 코드에서도 막는다.
            logger.info(
                f"[{agent_key}] 2차 정리: 확신 상향 무시 (id={fid}, "
                f"{current[fid]:.2f}→{conf:.2f}) — 중복 강화 임시 제거"
            )
            continue
        db.update_fact_confidence(fid, conf)
        applied += 1

    logger.info(
        f"[{agent_key}] 2차 기억 정리 완료 — 사실 {len(facts)}개 중 {applied}건 확신 재평가"
    )
    return applied
