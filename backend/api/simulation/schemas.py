"""Pydantic schemas for simulation API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentConfig(BaseModel):
    name:               str
    system_prompt:      str
    icon:               str       = "🤖"
    gender:             str       = "auto"  # "auto" | "male" | "female" | "unknown"
    initial_active:     bool      = True
    display_name:       str       = ""
    # 관계 지도 {상대 agent key(= AgentConfig.name): 내가 그를 부르는 관계}.
    # 예) 김봉남: {"채민경": "아내", "김미경": "큰딸"} / 채민경: {"김봉남": "남편"}.
    # **각자 자기 시점**이라 서로 대칭일 필요가 없다. location 처럼 ABM 엔진이
    # 해석하는 필드로, 엔진은 이 값으로 (1) [아는 사람] 계약 블록을 에이전트별로 만들고
    # (2) <TARGETS>·[이 자리의 사람들]에 관계어 라벨을 붙이고 (3) 서로를 known 으로 시드한다.
    # 빈 dict = 관계 기능 미사용(계약 블록이 붙지 않고 나머지 동작은 완전히 동일).
    relationships:      dict[str, str] = {}
    location:           str       = ""  # 초기 위치 (빈값이면 위치 미설정 = 전체 노출)
    visual_description: str       = ""  # 모르는 사람에게 보이는 외모 묘사
    server_id:          str | None = None  # 이 에이전트만 사용할 LLM 서버. None/빈값 = 시뮬레이션 기본 서버(SimStartConfig.server_id)
    # 이 에이전트만 사용할 샘플링 온도. None = 시뮬레이션 기본값(SimStartConfig.temperature)
    temperature:        float | None = Field(default=None, ge=0.0, le=2.0)
    # "역할형" — 관찰 대상(주역)이 아니라 특정 역할만 수행하는 에이전트(예: 병원 의사).
    # 기본값 False(기존 동작 그대로, 하위호환). True면:
    #   - 소외 재투입(_starved_agents) 대상에서 제외
    #   - 전원 침묵 강제 호출(_empty_wave_fallback)의 "가용 전원"에서 제외
    #     (단 후보가 역할형뿐이면 그들이라도 부른다)
    #   - 디렉터(시스템 에이전트)의 침묵/고립 감지·active_agents 표시 대상에서 제외
    #   - move_to가 항상 무시됨 — 절대 이동하지 않고 늘 같은 자리
    # 그 외(직접 지목·예약 이벤트로 턴을 받는 것, 같은 방 라우팅 등)는 일반 에이전트와 동일.
    role_type:          bool      = False
    # ── 채팅 에이전트(backend/db agents 테이블)와 공유되는 필드 ────────────────
    # ABM 엔진과 프롬프트 조립은 이 값들을 전혀 해석하지 않는다. 채팅 -> 시뮬레이션
    # 가져오기 시 값이 유실되지 않도록 시나리오 JSON에 보존만 하며, 다시 채팅으로
    # 내보낼 때 사용된다. model 은 채팅의 모델명 문자열로, 시뮬레이션의 server_id
    # (서버 인스턴스 지정)와 개념이 달라 자동 변환하지 않고 원본 그대로 왕복시킨다.
    role:               str | None = None
    goal:               str | None = None
    backstory:          str | None = None
    description:        str | None = None
    model:              str | None = None
    max_tokens:         int | None = None


_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScenarioEvent(BaseModel):
    # 트리거는 둘 중 하나:
    #   - wave: N        — N번째 wave 시작 시 발동 (기본)
    #   - at_time: "HH:MM" — 시뮬레이션 시계가 그 시각에 도달한 첫 wave에 발동.
    #     시간 모드가 켜져 있을 때만 의미가 있고, 이때 시간 추론은 이 시각을 넘겨
    #     점프하지 않는다("짱구 태권도 16:30" 같은 예정 서사가 큰 시간 점프에
    #     통째로 스킵되는 것을 막는다). at_time 이 있으면 wave 는 무시된다.
    #   - at_days: 요일 목록(["mon","wed","fri"] 등). 비었으면 매일. 있으면 해당
    #     요일마다 반복 발동한다(하루 일과·학원 스케줄용). 여러 날 건너뛴 점프는
    #     한 번만(가장 최근에 놓친 것) 발동한다 — 밀린 이벤트가 몰아치지 않는다.
    wave:    int       = 0
    at_time: str       = ""
    at_days: list[str] = []
    # "system_message" | "agent_enter" | "agent_exit" | "update_appearance" | "infect_agent"
    type:    str
    message: str       = ""
    targets: list[str] = ["all"]
    # agent_enter / agent_exit / update_appearance / infect_agent 전용.
    # infect_agent: 해당 에이전트를 이 wave에서 감염(기본 E, start_status로 P/I 선택) 상태로
    #               전이시키는 "환자 0번" 시드.
    #               message는 관전용 이벤트 피드에만 쓰이고 에이전트 메모리에는 주입되지 않는다
    #               (LLM은 증상 서사 텍스트로만 감염을 인지한다).
    agent:   str       = ""
    # infect_agent 전용. 환자 0번이 어느 상태로 바로 시작할지. 기본 "E"(기존 동작과 동일).
    # "P"/"I"면 건너뛴 앞 구간(E, 또는 E+P)을 실제로 추첨한 뒤 그만큼 노출 시점을 과거로
    # 앵커링한다(ABM/simulation/infection.py::_set_infected) — 증상 서사가 시작부터 그
    # 단계의 경과분에 맞게 보인다.
    start_status: Literal["E", "P", "I"] = "E"

    @field_validator("at_time")
    @classmethod
    def _valid_at_time(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return ""
        try:
            hh, mm = v.split(":")
            h, m = int(hh), int(mm)
        except (ValueError, AttributeError):
            raise ValueError(f"at_time 은 'HH:MM' 형식이어야 합니다: {v!r}")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"at_time 시각 범위 오류: {v!r}")
        return f"{h:02d}:{m:02d}"

    @field_validator("at_days")
    @classmethod
    def _valid_at_days(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for d in (v or []):
            d = str(d).strip().lower()
            if d not in _WEEKDAY_KEYS:
                raise ValueError(
                    f"at_days 는 {_WEEKDAY_KEYS} 중에서 골라야 합니다: {d!r}"
                )
            if d not in seen:
                seen.append(d)
        return sorted(seen, key=_WEEKDAY_KEYS.index)


class LocationNode(BaseModel):
    name:        str
    # 노드명 또는 zone명(그 zone에 입구 노드가 있을 때). zone 참조는 파싱 직후
    # 엔진 컴파일 단계에서 노드 레벨 엣지로 전개된다(ABM/simulation/core._expand_zone_edges).
    # 스키마는 zone 참조를 reject 하지 않는다 — import/구버전 시나리오 하위 호환.
    connects_to: list[str] = []
    is_exterior: bool      = False
    # 인지 구역. 같은 zone의 다른 장소에 있는 사람은 서로 존재를 인지하지만 대화는 불가.
    # 빈 문자열 = zone 없음(독립 노드). 위치 개념이며 관계 지도와 무관.
    zone:        str       = ""
    # 이 노드를 zone의 기본 입구로 지정. zone당 1개(중복 시 첫 번째만 채택 + warning).
    # 외부 노드가 connects_to에 zone명을 넣으면: 진입은 이 입구를 거치고, 탈출은
    # zone 내부 어느 노드에서든 1홉. zone 없는 노드에 붙으면 엔진이 무시(로그).
    is_zone_entry: bool    = False


class ExtraField(BaseModel):
    name:    str
    default: str = ""


# 출력 JSON 스키마에 기본으로 실리는 추가 필드. SimStartConfig 와 계약 프리뷰가
# 같은 기본값을 보도록 한곳에 둔다 (pydantic v2 는 모델 기본값을 deep-copy 한다).
DEFAULT_EXTRA_FIELDS: list[ExtraField] = [
    ExtraField(name="emotion",     default="neutral"),
    ExtraField(name="action",      default="speak"),
    ExtraField(name="action_note", default=""),
]

# wave당 경과 시간(분). SimStartConfig 와 계약 프리뷰가 같은 기본값을 봐야, 필드를
# 생략한 프리뷰 요청이 실제 실행과 다른 "시간 개념 OFF" 를 보여주지 않는다.
DEFAULT_TIME_PER_WAVE = 30


class TimeCategory(BaseModel):
    id:           str
    label:        str
    min_minutes:  int
    max_minutes:  int

    @field_validator("max_minutes")
    @classmethod
    def _max_not_below_min(cls, v: int, info) -> int:
        min_v = info.data.get("min_minutes")
        if min_v is not None and v < min_v:
            raise ValueError("max_minutes must be >= min_minutes")
        return v


# 에이전트가 스스로 선택하는 상태(수면·개인 용무 등) 카테고리. 시간 범위 구조는
# TimeCategory와 같아(id/label/min_minutes/max_minutes, id는 화면에 안 보이는 순수
# 내부 키) 그대로 상속하지만, 쓰이는 목적이 다르므로(시간 판정 vs 발화 억제) 이름을
# 분리해 계약・설정 코드가 각자의 개념으로 읽히게 한다. 별칭이 아니라 서브클래스인
# 이유는 아래 completion_style — 상태에만 있는 개념이라 시간 분류 쪽에 얹을 수 없다.
# `_max_not_below_min` validator는 상속으로 그대로 동작한다.
# 이동(zone 경계) 상태는 에이전트가 고르는 게 아니라 엔진이 자동으로 적용하므로
# 여기 포함되지 않는다 — 별도 zone_travel_min/max_minutes로 설정한다.
class StateCategory(TimeCategory):
    # 상태가 자연 만료될 때 본인에게 주는 알림 문구 분기 — ABM/simulation/status.py
    # ::_status_release_notice 참고. "completive"(기본, 하위 호환) = 완결 단정
    # ("하던 일을 마쳤다: ..."), "ongoing" = 비완결("잠깐 정신이 들었다. 여전히
    # ... 중") — 카테고리가 대표하는 게 스스로 끝나는 자기완결형 일(씻기 등)이
    # 아니라, 실제 종료는 다른 신호(예정 이벤트 등)가 담당하는 지속형 상황(학업·
    # 업무 등)일 때 사용자가 직접 켠다. 자동 추정 없음.
    #
    # 실측 버그: "집 밖에서 수행하는 고유 업무(학업)" 카테고리의 지속 시간은
    # min~max 무작위로 뽑히는데 실제 학원 종료는 별도 예정 이벤트(21:30)가 정한다.
    # 무작위 타이머가 먼저 만료돼 "하던 일을 마쳤다: 학업."을 받은 에이전트가
    # "학원이 끝났다"로 오해해 20:12에 조퇴 서사를 만들어냈다.
    #
    # `sleep`/`traveling`은 `_status_release_notice`에서 이 분기보다 **먼저** 전용
    # 문구로 갈라지므로 이 필드의 영향을 받지 않는다 — 기본 수면 카테고리에 UI
    # 체크박스가 보여도 실질적으로 무시된다(막지 않고 사실만 남긴다).
    completion_style: Literal["completive", "ongoing"] = "completive"

# ABM/simulation/core.py::_DEFAULT_STATE_CATEGORIES 와 같은 기본값이어야 한다 —
# 생략된 계약 프리뷰 요청이 실제 실행과 다른 모습을 보여주면 오도한다.
DEFAULT_STATE_CATEGORIES: list[StateCategory] = [
    StateCategory(id="sleep", label="수면 — 상대가 알고도 말 걸지 않는 한 반응 없음",
                  min_minutes=300, max_minutes=540),
    StateCategory(id="busy",  label="자리를 비우고 하는 개인적인 일(씻기 등)",
                  min_minutes=10, max_minutes=30),
]
DEFAULT_ZONE_TRAVEL_MIN_MINUTES = 10
DEFAULT_ZONE_TRAVEL_MAX_MINUTES = 20


# 분 단위 입력의 공통 상한(= 100년). 프론트의 MAX_TARGET_DURATION_MINUTES와 같은 값으로,
# 목표 기간·증상 단계·회복 시간 등 모든 "시뮬레이션 내 분" 입력에 함께 적용한다.
MAX_DURATION_MINUTES = 52560000


class SymptomStage(BaseModel):
    """상태(E/P/I)별 증상 서사.

    엔진은 **지금 실제 상태**와 **그 상태에 머문 비율**(상태 진입 후 경과 ÷ 그 상태의
    뽑힌 길이)만으로 문구를 고른다(``ABM/simulation/infection.py::_find_symptom_stage``).
    같은 status로 여러 항목을 등록할 수 있고, 등록 순서가 곧 그 상태 안의 진행 순서다
    (2개면 앞 절반/뒤 절반). 어떤 상태에 항목이 없으면 그 상태에선 증상 카드가 안 뜬다.

    예전의 "노출 후 경과분" 구간(``min_minutes``/``max_minutes``)은 제거됐다 — E/P/I
    길이가 확률분포로 뽑히는 구조에서는 그 별도 시간 축이 실제 상태와 항상 어긋날 수
    있었다(I로 즉시 시작했는데 잠복기 문구가 나오는 등).
    """
    status:       Literal["E", "P", "I"]
    symptom_text: str


MAX_DURATION_DAYS = 36500  # = MAX_DURATION_MINUTES / 1440 (100년)


class DurationSpec(BaseModel):
    """며칠(day) 단위 지속시간을 뽑는 확률분포 설정 — E(잠복기)/P(무증상 전염기)/I(감염기) 공통.

    kind에 따라 shape/scale(gamma) 또는 mean/stddev(gaussian)만 의미가 있다 — uniform은
    min_days~max_days 자체가 전체 지지집합이라 별도 파라미터가 없다. min_days~max_days는
    어느 kind든 항상 절단 범위로 적용된다(감마·가우시안이 그 범위 밖으로 나오면 재추첨).
    min_days == max_days면 kind와 무관하게 그 값으로 고정된다.
    엔진 쪽 샘플러: ``ABM/simulation/infection.py::_sample_duration_minutes``.

    NaN/±inf는 거부한다 — shape=inf면 감마 표본이 전부 inf라 거부 표집이 상한까지
    헛돌고, mean=NaN이면 런 도중 반올림에서 크래시한다(엔진도 이중 방어를 한다).
    """
    model_config = ConfigDict(allow_inf_nan=False)

    kind:     Literal["uniform", "gamma", "gaussian"] = "uniform"
    shape:    float = Field(default=2.0, gt=0)   # gamma 전용 (k)
    scale:    float = Field(default=1.0, gt=0)   # gamma 전용 (θ)
    mean:     float = Field(default=5.0)         # gaussian 전용 (μ)
    stddev:   float = Field(default=2.0, gt=0)   # gaussian 전용 (σ)
    min_days: float = Field(default=1.0, ge=0, le=MAX_DURATION_DAYS)
    max_days: float = Field(default=10.0, le=MAX_DURATION_DAYS)

    @field_validator("max_days")
    @classmethod
    def _max_not_below_min(cls, v, info):
        min_v = info.data.get("min_days")
        if min_v is not None and v < min_v:
            raise ValueError("max_days must be >= min_days")
        return v


class InfectionModelConfig(BaseModel):
    """결정론적 감염병 모델(SEPIR/SEPIRS) 설정.

    감염 판정은 전적으로 엔진(순수 파이썬)이 수행한다. LLM은 status·확률·경과 시간 같은
    raw 값을 절대 보지 않고, 오직 ``symptom_stages``의 서사 텍스트만 상황 컨텍스트로 받는다.

    상태는 S(감염 가능) → E(잠복기, 비전염) → P(무증상 전염기, 전염 가능) →
    I(감염기, 전염 가능) → R(회복) 순으로 전이한다. E/P/I 각 구간의 지속 시간은
    ``DurationSpec``(분포 종류 + 파라미터 + min/max 절단 범위)으로 설정한다. 기본값은
    SEPIR 도입 전의 연구 스펙 상수(E = 절단 감마 k=1.926/θ=1.775/[1,10]일, P = 0일,
    I = 8일 고정)를 그대로 재현한다 — P가 0일이면 P 단계는 건너뛰어 기존 SEIR과
    같은 시점에 E→I가 일어난다(``ABM/simulation/infection.py`` 상단 docstring 참고).

    시간 축이 둘로 나뉜다: **전염은 wave·접촉 기준 Monte Carlo 확률**(λ=β×t_d,
    P=1-exp(-λ)), **E/P/I 진행은 시뮬레이션 내 경과 시간(분) 기준**이다.
    """
    # `model_type` 필드명이 pydantic의 보호 네임스페이스("model_")와 겹쳐 경고가 난다 —
    # BaseModel 메서드와 실제 충돌은 없으므로 보호를 끈다.
    model_config = ConfigDict(protected_namespaces=())
    enabled:                  bool  = False
    disease_name:             str   = ""
    # 전염 확률의 β(λ=β×t_d, P=1-exp(-λ)). t_d는 직전 판정 이후 실제로 경과한
    # 시간(일 단위)이다 — infection.py::_apply_infection_wave 참고.
    beta:                     float = Field(default=0.04, ge=0.0)
    symptom_stages:           list[SymptomStage] = []
    # 설정 화면의 모델 선택(UI 전용). 엔진은 읽지 않는다 — S→E→P→I→R 하나의 구조에서
    # E/P 지속을 0으로 두면 SIR/SEIR이 된다. 프론트가 sir/seir 선택 시 숨긴 구간의
    # 지속을 0으로 강제한다(frontend/js/sim/state.js::applyModelType).
    model_type:               Literal["sir", "seir", "sepir"] = "sepir"
    # True = 영구 면역(회복 후 다시 걸리지 않음) / False = 재감염 가능(회복 후 S로 복귀 — SEPIRS)
    immune_after_recovery:    bool  = True
    # E/P/I 지속 시간 분포 — 기본값은 SEPIR 도입 전 동작과 100% 동일한 결과를 낸다.
    exposed_duration:        DurationSpec = DurationSpec(
        kind="gamma", shape=1.926, scale=1.775, min_days=1, max_days=10)
    presymptomatic_duration: DurationSpec = DurationSpec(
        kind="uniform", min_days=0, max_days=0)
    infectious_duration:     DurationSpec = DurationSpec(
        kind="uniform", min_days=8, max_days=8)

    @field_validator("symptom_stages", mode="before")
    @classmethod
    def _drop_legacy_stages(cls, v):
        """status가 없거나 E/P/I가 아닌 항목(구버전 min/max 형식)은 조용히 버린다.

        구조가 바뀌어 옛 항목을 새 의미로 옮길 방법이 없다. 버리지 않으면 옛 시나리오·
        저장된 run의 config_json을 불러오거나 이어하기할 때 422/ValidationError로
        아예 열리지 않는다 — 증상 문구만 빠진 채 여는 편이 낫다.
        """
        if not isinstance(v, list):
            return v
        return [s for s in v if not isinstance(s, dict) or s.get("status") in ("E", "P", "I")]


DEFAULT_SYSTEM_AGENT_PROMPT = (
    "당신은 멀티에이전트 시뮬레이션의 내레이터이자 진행자입니다.\n"
    "주어진 시뮬레이션 요약과 침묵 중인 에이전트 목록을 분석하여\n"
    "이야기 흐름을 자연스럽게 이어가기 위한 개입이 필요한지 판단하세요.\n\n"
    "개입이 필요한 경우, 해당 에이전트에게 이야기 흐름에 맞는\n"
    "상황 묘사·사건·주변 변화·대화 유도 등의 메시지를 전달하세요.\n"
    "개입이 불필요한 경우(이야기가 자연스럽게 흐르고 있다면) interventions를 빈 배열로 반환하세요."
)


class SystemAgentConfig(BaseModel):
    enabled:               bool  = False
    icon:                  str   = "🎬"
    display_name:          str   = "내레이터"
    system_prompt:         str   = DEFAULT_SYSTEM_AGENT_PROMPT
    intervention_interval: int   = 1   # N웨이브마다 실행
    silence_threshold:     int   = 3   # N웨이브 미발화 = 침묵
    director_note:         str   = ""  # 시뮬레이션 서사 목표 (불변 나침반)
    # 디렉터가 개입 판단 시 원문으로 되짚는 최근 wave 수. 엔진에서 [2, 20] clamp,
    # 총 라인은 _DIGEST_MAX_LINES(120)로 별도 캡. 크게 잡으면 장기 흐름을 보지만
    # 디렉터 프롬프트가 커진다 — director_call 이벤트의 prompt_tokens/elapsed_ms로
    # 비용을 관측하며 조절한다.
    digest_waves:          int   = 6


class SimStartConfig(BaseModel):
    scenario_id:            str | None       = None
    agents:                 list[AgentConfig]
    background:             str
    start_agent:            str
    max_waves:              int              = 10
    step_delay:             float            = 1.0
    token_limit:            int              = 8192
    llm_max_tokens:         int              = 16384
    extra_fields:           list[ExtraField] = DEFAULT_EXTRA_FIELDS
    events:                 list[ScenarioEvent] = []
    location_graph:         list[LocationNode]  = []
    # 공간 기반 인지 모드. location_graph(특히 zone)와 짝이라 바로 옆에 둔다.
    #   "targeted" (기본) — 기존 동작 100% 그대로. 대사는 해석된 직접 타깃에게만 간다.
    #   "spatial"        — 같은 방 제3자 엿듣기 + 같은 zone 다른 방으로의 원거리
    #                      대사 전달(직접 타깃 한정, 행동 줄 없음) + 독백의 행동을
    #                      같은 방 전원에게 씬으로 브로드캐스트.
    # 알 수 없는 값은 엔진(Simulation.__init__)이 조용히 "targeted"로 폴백한다
    # (time_estimation_mode와 동일한 패턴 — 여기서 예외를 던지지 않는다).
    # 계약 문자열(ABM/prompt_contract.py)은 이 값에 전혀 의존하지 않으므로
    # ContractPreviewRequest에는 의도적으로 넣지 않았다.
    perception_mode:        Literal["targeted", "spatial"] = "targeted"
    lang_fix_enabled:       bool             = True
    lang_fix_retries:       int              = 2
    # ── 출력 계약(프롬프트 계약 층) ────────────────────────────────────────────
    # 출력 JSON 스키마·move_to 의미·target ID 규칙은 **엔진이 소유**하며
    # (`ABM/prompt_contract.py`) 실행 시점에 현재 설정으로 생성된다. 따라서 아래
    # 두 필드가 **비어 있는 것이 정상 경로**이고, 그래야 엔진을 업그레이드했을 때
    # 기존 시나리오도 자동으로 새 계약을 받는다.
    #
    # output_format_override — 고급 사용자용 opt-in 오버라이드. 값이 있을 때만
    #   출력 계약 템플릿을 통째로 대체한다. 이후 엔진 업데이트가 이 시나리오의
    #   **출력 계약에만** 자동 반영되지 않는다(지도/시간/감염 계약은 계속 최신).
    #   동기화는 사용자 책임.
    output_format_override: str              = ""
    # output_format_template — (구) 시나리오 저장 시점에 전체 템플릿이 통째로
    #   스냅샷되던 필드. 옛 config_json 을 그대로 파싱하기 위해 필드만 남긴다.
    #   **런타임에서 무조건 무시**되며(= 엔진이 항상 재생성), 저장 시에도
    #   기록하지 않는다. 새 코드는 output_format_override 만 볼 것.
    output_format_template: str              = ""
    sim_start_time:         str              = "09:00"  # HH:MM, 시뮬레이션 내 시작 시각
    sim_start_weekday:      Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"  # 시뮬레이션 내 시작 요일. 자정 롤오버마다 자동 증가
    time_per_wave:          int              = DEFAULT_TIME_PER_WAVE  # wave당 경과 시간(분). 0 = 시간 개념 비활성
    time_mode:              Literal["fixed", "variable"] = "fixed"  # "fixed" = time_per_wave 고정, "variable" = wave 내용을 LLM이 분류해 가변 경과
    time_categories:        list[TimeCategory] = [
        TimeCategory(id="meal_or_brief",      label="식사·짧은 용무",      min_minutes=5,   max_minutes=10),
        TimeCategory(id="normal_scene",       label="일반적인 대화/활동",   min_minutes=15,  max_minutes=30),
        TimeCategory(id="alone_or_offscreen", label="혼자 있음/외출",      min_minutes=60,  max_minutes=120),
        TimeCategory(id="night_sleep",        label="취침/장시간 경과",     min_minutes=240, max_minutes=420),
    ]  # time_mode="variable"일 때 LLM이 wave 내용을 분류하는 카테고리 목록
    # 가변 시간의 경과분을 정하는 방식. time_mode="variable"일 때만 의미가 있고
    # "fixed"에서는 완전히 무시된다(엔진 분기가 variable 안에만 있음).
    #   "category" (기본) — LLM이 time_categories 중 하나를 고르고 그 범위에서 랜덤 추출.
    #   "ai"             — LLM이 카테고리 대신 경과분을 직접 추론. 실패하면 조용히
    #                      normal_scene 카테고리로 폴백하고, 추론된 분은 time_categories
    #                      전체의 min(min_minutes)~max(max_minutes)로 clamp된다.
    # 어느 모드든 최종 분은 max_scene_jump_minutes / max_daytime_jump_minutes 상한을
    # 동일하게 통과한다. 알 수 없는 값은 엔진이 "category"로 폴백한다.
    time_estimation_mode:   Literal["category", "ai"] = "category"
    idle_minutes_schedule:  list[int]        = [60, 120, 180]  # 강제 침묵 재투입 시 경과 시간(분) 스케줄 — 침묵 회차가 늘수록 다음 값 사용, 끝에서 캡
    # ── 가변 시간 점프 상한 (time_mode="variable" 전용) ────────────────────────
    # LLM 분류기는 "이 장면의 질감"만 정하고, 실제 경과 분의 **상한은 엔진이
    # 결정론적으로 강제**한다. 약한 모델이 오후 한복판에서 최대 범위 카테고리를
    # 골라 학원·퇴근·저녁 같은 재집결 장면을 통째로 건너뛰는 것을 막는다.
    # 두 캡 모두 0 = 해당 캡 비활성(순수 카테고리 랜덤값 사용).
    max_scene_jump_minutes:   int            = 45   # 실내 한 곳에 2명+ 동석 발화 중일 때의 점프 상한
    max_daytime_jump_minutes: int            = 180  # 밤(22~06시)이 아니고 집에 남은 사람이 있을 때의 점프 상한
    # ── 압축 전(raw) 메모리의 시간 앵커 ────────────────────────────────────────
    # 압축된 구조화 기억은 이미 `format_sim_day_period` 헤더로 시간 인식이 되지만,
    # 아직 압축되지 않은 최근 대화에는 시간 정보가 전혀 없다(elapsed_minutes 는
    # 엔진 내부용이고 LLM 호출 직전에 벗겨진다). 켜면 문턱값 이상 시간이 점프했을
    # 때, 또는 자정(날짜) 경계를 넘었을 때는 문턱값과 무관하게 항상, 라벨 한 줄
    # ("[시간] 3일차 수요일 오후 7시 20분" / "[날짜 변경] ...")을 그 에이전트의
    # raw 메모리에 영구 기록한다. 매 메시지에 타임스탬프를 붙이지 않는 이유:
    # 엔진이 매 wave 끝에 시계를 갱신하므로 "바뀌면 기록"이 곧 "매번 기록"이 된다.
    # 문구가 문장이 아니라 라벨인 이유: 현재형 서술("지금은 ~입니다")은 그 줄이
    # 과거가 된 뒤 다시 읽힐 때 시제가 꼬인다. 기본 문턱값 45 =
    # max_scene_jump_minutes 와 같은 값(엔진이 이미 장면 연속성이 끊긴다고 보는 지점).
    # 프롬프트 계약 문자열에는 전혀 영향이 없어 ContractPreviewRequest 에는 없고,
    # /continue 는 최초 실행 설정을 그대로 이어받아 SimContinueConfig 에도 없다.
    memory_time_anchor_enabled:           bool = False
    memory_time_anchor_threshold_minutes: int  = Field(default=45, ge=0)
    # ── 에이전트 상태(수면·이동 등) ────────────────────────────────────────────
    # 재투입(전원 침묵 처리)이 에이전트의 상태를 전혀 모른 채 무조건 다시 초대해서,
    # 이미 잠든 에이전트가 매 침묵 사이클마다 잠꼬대를 반복하거나 zone 경계를
    # 건너는 이동이 1 wave 만에 "순간이동"하는 문제를 막는다(ABM/simulation/status.py).
    # []면(빈 리스트, 생략과 다름) 자기-선언형 상태 기능 자체가 꺼진다 — enter_state
    # 힌트가 계약에서 빠지고 발화 억제도 일어나지 않는다.
    state_categories: list[StateCategory] = list(DEFAULT_STATE_CATEGORIES)
    # zone 경계를 건너는 이동(예: 집→학교)에 걸리는 시간(분) — 에이전트가 고르는
    # 게 아니라 엔진이 hop 적용 시 자동으로 부여한다. 같은 zone 안의 이동(예:
    # 거실→안방)에는 적용되지 않는다(기존처럼 즉시). 둘 다 0이면 기능 비활성.
    zone_travel_min_minutes: int = DEFAULT_ZONE_TRAVEL_MIN_MINUTES
    zone_travel_max_minutes: int = DEFAULT_ZONE_TRAVEL_MAX_MINUTES
    # 소외 재투입 간격(wave). 다른 에이전트들이 대화 중이어도, 마지막으로 턴을
    # 받은 지 이 wave 수 이상 지난(상태에 묶이지 않은) 에이전트를 빈 incoming 으로
    # 재투입한다 — 혼자 다른 방에 간 에이전트가 영영 턴을 못 받는 starvation 방지.
    # 진행 불가 백스톱 한도 max(6, 2×이 값) 에도 쓰인다. 구 이름 max_silence_waves
    # (고립 휴면 기준)는 아래 validator 가 옮겨 읽는다. (구 early_stop_enabled
    # 플래그는 제거됐다 — 대화가 시들해지는 것으로는 더 이상 실행을 종료하지 않고,
    # 시간을 건너뛰며 계속한다. 종료는 max_waves / target_duration_minutes /
    # no_agents / no_progress / stopped.)
    starvation_waves:       int              = 3
    # 목표 기간(분). None = 미사용. 설정 시 "시뮬레이션 내 경과 시간이 이 값에 도달"이
    # 주 종료 신호가 되고, max_waves는 상한 안전장치로 남는다 — 둘 중 먼저 도달하는
    # 쪽에서 정상 종료. 시간 개념이 비활성(time_mode="fixed" AND time_per_wave=0)이면
    # 이 값은 조용히 무시된다.
    target_duration_minutes: int | None      = Field(default=None, ge=1)
    server_id:              str | None       = None  # None = DB default 서버, 미설정 시 env 폴백
    # 시뮬레이션 전체 기본 샘플링 온도. AgentConfig.temperature 로 에이전트별 오버라이드 가능.
    temperature:            float            = Field(default=0.7, ge=0.0, le=2.0)
    system_agent:           SystemAgentConfig = SystemAgentConfig()
    # 결정론적 감염병 모델. enabled=False(기본)면 상태 갱신도 프롬프트 주입도 전혀 일어나지 않는다.
    infection_model:        InfectionModelConfig = InfectionModelConfig()

    @model_validator(mode="before")
    @classmethod
    def _migrate_max_silence_waves(cls, data):
        """하위 호환: 옛 시나리오/DB config_json 의 `max_silence_waves`(고립 휴면
        기준)를 `starvation_waves` 로 옮겨 읽는다. 둘 다 있으면 새 이름이 이긴다.
        옛 키는 버린다 — 모델에 없는 필드라 어차피 무시되지만 명시적으로 정리."""
        if isinstance(data, dict) and "max_silence_waves" in data:
            data = dict(data)
            legacy = data.pop("max_silence_waves")
            if data.get("starvation_waves") is None and legacy is not None:
                data["starvation_waves"] = legacy
        return data

    @field_validator("starvation_waves")
    @classmethod
    def _clamp_starvation_waves(cls, v: int) -> int:
        # 엔진도 max(1, …)로 막지만, 저장된 옛 값(0 등)으로 로드가 실패하지 않도록
        # 거부 대신 clamp 한다.
        return max(1, int(v))

    @field_validator("time_categories")
    @classmethod
    def _non_empty_categories(cls, v):
        if not v:
            raise ValueError("time_categories must not be empty")
        return v

    @field_validator("idle_minutes_schedule")
    @classmethod
    def _non_empty_schedule(cls, v):
        if not v:
            raise ValueError("idle_minutes_schedule must not be empty")
        return v

    # ── 계약 층 헬퍼 ──────────────────────────────────────────────────────────

    def effective_output_format_override(self) -> str | None:
        """`Agent(output_format_template=...)` 에 넘길 값.

        오버라이드가 **실제로 설정됐을 때만** 문자열을, 아니면 `None`(= 엔진이
        실행 시점에 현재 설정으로 생성)을 돌려준다. 구 `output_format_template`
        (프리즈 스냅샷)은 여기서 의도적으로 보지 않는다 — 옛 시나리오도 로드하면
        최신 엔진 계약을 받아야 하기 때문이다.
        """
        return self.output_format_override or None


class ScenarioSave(BaseModel):
    name:        str
    description: str = ""
    config:      SimStartConfig


# ── 엔진 프롬프트 계약 프리뷰 ─────────────────────────────────────────────────

class ContractPreviewRequest(BaseModel):
    """계약 문자열에 영향을 주는 config 조각만 받는다.

    `SimStartConfig` 의 부분집합이라 프론트는 편집 중인 설정 객체를 그대로 잘라
    보내면 된다. 여기 없는 필드는 계약을 바꾸지 않는다.
    """
    location_graph:         list[LocationNode]  = []
    time_mode:              Literal["fixed", "variable"] = "fixed"
    # SimStartConfig.time_per_wave 와 같은 기본값이어야 한다 — 생략된 프리뷰 요청이
    # 실제 실행과 다른 "시간 개념 OFF" 를 보여주면 오도한다.
    time_per_wave:          int                 = DEFAULT_TIME_PER_WAVE
    infection_model:        InfectionModelConfig = InfectionModelConfig()
    extra_fields:           list[ExtraField]    = DEFAULT_EXTRA_FIELDS
    output_format_override: str                 = ""
    # 인터뷰 모드처럼 출력 스키마를 빼는 경로를 미리 보고 싶을 때 False.
    include_output_schema:  bool                = True
    # 위치 그래프가 있는 시나리오는 실행 중 <TARGETS> 자리에 flat ID 목록이 아니라
    # "([현재 상황] 컨텍스트에서 …)" 안내가 들어간다(step.py 의 sit_targets). 프론트는
    # location_graph 가 비어있지 않으면 이 값을 True 로 보내 프리뷰가 실제 주입본과
    # 같은 target 블록을 그리게 한다.
    situation_targets:      bool                = False
    # 프리뷰용 더미 타깃(선택). 비우면 자리표시자 ID 하나로 렌더한다 — 실제 실행에서는
    # 매 턴 같은 자리에 있는 사람들로 채워진다.
    available_targets:      list[str]           = []
    key_to_alias:           dict[str, str]      = {}
    # 프리뷰는 "에이전트 한 명이 보는 계약"을 그린다. relationships 는 per-agent 라서
    # 프론트가 **지금 편집 중인 에이전트의** AgentConfig.relationships 를 그대로 보낸다
    # (비우면 [아는 사람] 블록도, <TARGETS> 관계 라벨도 렌더되지 않는다 = 미사용 상태).
    # 여기 실린 key 는 실존 검증을 하지 않는다 — 프리뷰는 편집 중 상태를 보여주는 거울이고,
    # dangling 필터링은 실행 시점(Simulation._sanitize_relationships)의 책임이다.
    relationships:          dict[str, str]      = {}
    # SimStartConfig 와 같은 기본값이어야 한다 — 생략된 프리뷰 요청이 실제 실행과
    # 다른 "상태 기능 OFF"를 보여주면 오도한다.
    state_categories:       list[StateCategory] = list(DEFAULT_STATE_CATEGORIES)

    def time_enabled(self) -> bool:
        return self.time_mode == "variable" or self.time_per_wave > 0


class ContractFlags(BaseModel):
    """이 설정에서 켜진 계약 feature. 프론트가 "무엇 때문에 이 블록이 붙었는지" 표시용."""
    has_location_graph:    bool
    has_zone:              bool
    time_enabled:          bool
    infection_enabled:     bool
    include_output_schema: bool


class ContractPreviewResponse(BaseModel):
    # world_contract + output_contract. 실제 주입되는 순서/문자열 그대로.
    contract:        str
    # 지도/시간/감염 정적 블록 (= Agent.engine_contract). 오버라이드와 무관하게 항상 엔진 소유.
    world_contract:  str
    # 출력 JSON 스키마 + move_to 의미 + target ID 규칙. output_format_override 가 대체하는 부분.
    output_contract: str
    flags:           ContractFlags
    # verify_contract 진단. 정상 설정이면 빈 배열. 오버라이드가 필수 지시어를 빠뜨리면 채워진다.
    warnings:        list[str] = []


class SimContinueConfig(BaseModel):
    start_agent: str
    max_waves:   int              = 10
    step_delay:  float            = 1.0
    events:      list[ScenarioEvent] = []
    # max_waves와 같은 성격의 "이번 이어서 실행" 예산. None = 목표 기간 미사용.
    target_duration_minutes: int | None = Field(default=None, ge=1)


# ── 사후 인터뷰 ────────────────────────────────────────────────────────────────

InterviewMode = Literal["memory_only", "full_log"]


class InterviewRequest(BaseModel):
    question: str
    mode:     InterviewMode = "memory_only"
    # 답변 길이 상한. None이면 실행 설정(config_json)의 llm_max_tokens를 따른다.
    max_tokens: int | None = Field(default=None, gt=0)

    @field_validator("question")
    @classmethod
    def _non_empty_question(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be empty")
        return v


class InterviewRecord(BaseModel):
    id:         int
    run_id:     str
    agent_key:  str
    mode:       str
    question:   str
    answer:     str
    created_at: float
    meta:       dict = {}
