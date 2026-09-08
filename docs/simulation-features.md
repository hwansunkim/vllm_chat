# 시뮬레이션 기능별 상세

> 엔진의 각 기능이 무엇을 하고, 어떤 설정으로 켜지고, 어떤 이벤트를 내는가.
> 루프 순서·프롬프트 조립은 [`simulation-engine.md`](simulation-engine.md), 설정 UI는
> [`frontend.md`](frontend.md#61-설정-뷰). 각 절 형식: **목적 / 설정 / 엔진 동작 /
> 이벤트 / 계약 블록**.

전 기능 공통 원칙 하나: **비활성이면 코드 경로 자체가 죽는다.** `perception_mode`,
`infection_model.enabled`, `location_graph`, `system_agent.enabled`, `relationships`가
비어 있으면 관련 계약 블록도 안 붙고 관련 이벤트도 0건이라, 그 기능 도입 전과
프롬프트가 **글자 단위로 같다**.

---

## 1. 위치 그래프

**목적** — 에이전트가 오갈 수 있는 장소와 연결. 연결된 장소로만 이동, 같은 장소
사람끼리만 대화.

**설정** — 설정 뷰 "위치 그래프" 섹션. `location_graph[]` = `LocationNode`:

| 필드 | 의미 |
|---|---|
| `name` | 장소 노드명 |
| `connects_to[]` | 인접 노드명 (또는 zone명 — 아래) |
| `is_exterior` | 외부 공간 (완전 격리) |
| `zone` | 인지 구역 (§2) |
| `is_zone_entry` | 이 노드를 zone의 기본 입구로 (zone당 1개) |

에이전트별 `AgentConfig.location` = 초기 위치. 빈 값 = 위치 미설정 = 전체 노출 (레거시).

**엔진 동작** (`location.py`):

- `_find_path(start, goal)` — BFS 최단 경로 (시작 제외, 목표 포함). 이동은 **wave당
  한 칸**씩 `_agent_path`를 pop.
- 그래프 없음 → `[goal]` (직접 이동, 하위 호환). 지도 밖 목적지 → `[]` (이동 무시).
- **외부 공간** — `_compute_wave_targets`가 `[], []` 반환 (아무도 안 보임).
  `_resolve_targets`가 외부 화자에게 `[]` (아무에게도 전달 불가). 씬 메시지도 안 감.
- **zone 참조 엣지 전개** (`_expand_zone_edges`) — 외부 노드 X의 `connects_to`에 zone Z가
  있으면: 진입은 `X → 입구`, 탈출은 `Z의 모든 내부 노드 → X` (구역 안 어디서든 1홉
  탈출). 전개 후 `_location_graph`는 여전히 순수 노드 인접 리스트.

**이벤트** — `agent_move {wave, agent, from, to, to_exterior}`.

**계약 블록** — `build_map_contract` → `[위치 그래프 — 이동 가능한 경로]` + 이동 규칙
(그래프 내 장소명만) + 사람 지목 규칙 + 외부공간 규칙 + (zone 있으면) 구역 규칙.

---

## 2. zone (인지 구역)

**목적** — **대화 스코프 ≠ 인지 스코프.** 같은 zone의 다른 장소에 있는 사람은 "저기
있구나"까지 알지만, 말을 걸려면 그 장소로 가야 한다. (관계 지도와 **완전 별개** —
zone은 순수 위치 개념.)

**설정** — `LocationNode.zone` 문자열. 예: 안방·거실·부엌 → zone "우리집".

**엔진 동작**:

- `_compute_zone_awareness(agent_key)` → 같은 zone 다른 방의 `(known_elsewhere,
  strangers_elsewhere)`. `_build_situation_context`가 `[같은 구역(우리집)의 다른 곳]`
  블록으로 렌더하고, 각 줄에 `→ 만나려면 move_to: "<ID>"` 힌트를 인라인으로 붙인다.
- `<TARGETS>`·`visible_agents`에는 **절대 안 들어간다** — 인지만.

**계약 블록** — `_MAP_RULE_ZONE`: "같은 구역 안의 다른 장소에 있는 사람은 서로 존재를
인지하지만, 대화는 같은 장소에 있어야만".

---

## 3. 공간 기반 인지 (`perception_mode = "spatial"`)

**목적** — 기본(`targeted`)은 발화가 `target` 지목 상대에게만 간다. `spatial`은 물리적
현실을 흉내낸다 — 같은 방에 있으면 못 들을 이유가 없다.

**설정** — 설정 뷰 "위치 그래프" 섹션의 "공간 기반 인지(엿듣기)" 토글. → `spatial` /
`targeted`. 심층: [`spatial-perception.md`](spatial-perception.md).

**엔진 동작** — `run()`의 발화 라우팅 블록이 `_route_spatial`로 분기, 화자의 **이동 전**
위치 기준 4갈래:

| 상황 | 전달 |
|---|---|
| 같은 방 + 직접 타깃 | 대사 + 행동 (`targeted`와 동일) |
| **같은 zone 다른 방** + 직접 타깃 (`_is_remote_target`) | **대사만** (`action_note` 제거), `speaker`에 `", 멀리서"` |
| 같은 방 + **제3자** (지목 안 됨) | 대사 + 행동을 `[화자→대상들]` 엿듣기 태그로 (관찰자 시점, 인지 관계 필터 없음 — 엿듣기는 물리적 사실) |
| 같은 방 + **독백** (`target=self/system`) | 대사 안 들림, **행동만** 씬 채널로 |

`_resolve_targets`도 `spatial`일 때만 직접 타깃(`<key>`/`stranger_N`)의 도달 범위를
`_reachable`로 "같은 zone 다른 방"까지 확장 (`all`은 여전히 같은 방).

**계약 블록** — 없음. `spatial`은 라우팅 동작만 바꾸고 프롬프트 문자열은 안 바꾼다
(그래서 `ContractPreviewRequest`에도 없음).

---

## 4. 관계 지도 (`relationships`)

**목적** — 페르소나에 "당신은 아빠다, 딸이 있다"라고만 쓰면 LLM은 그 "딸"이 `target`에
넣을 어떤 ID인지 모른다. 이름(key)과 관계어를 한 줄에 바인딩해야 지목이 성립한다.

**설정** — 에이전트 카드의 관계 편집기. `AgentConfig.relationships` = `{상대 key:
관계어}`. **각자 자기 시점** (김봉남→채민경 "아내", 채민경→김봉남 "남편") — 대칭 불필요.
소속·호칭·인지관계를 한 필드로 표현한다 (구 `groups`는 제거됨).

**엔진 동작**:

- `Simulation._sanitize_relationships()` — 초기화 시 1회 정제:
  - **dangling** (상대 key가 이 시뮬레이션에 없음 — 시나리오 편집 중 이름 변경) → 제외
  - **자기 참조** → 제외
  - **단방향** (A→B는 있는데 B→A 없음) → 경고만 (B는 A를 낯선 이로 봄)
  - 사유는 `_verify_engine_contract()`가 다른 계약 경고와 같이 로그로.
- **`_agent_knowledge` 시드를 결정한다** (계약 층과 같은 on/off):
  - 시나리오 전체가 비어 있으면(기능 미사용) → 전원이 서로 아는 사이. 옛 "그룹
    미설정 = 전원 인지" 기본값 그대로.
  - 하나라도 명시하면(기능 사용) → 각자 **자기가 명시한 상대만** 아는 사이. 관계
    목록에 없는 사람은 같은 방에서 만나도 `stranger_N`으로 보인다. 관계는 화자
    방향뿐이라 익명성을 대칭으로 두려면 양쪽 다 적어야 한다.
- `<TARGETS>` 목록·`[이 자리의 사람들]`에 관계어 라벨 (`채민경 (ID: "chaemin", 아내)`).

**계약 블록** — `build_relationship_contract` → `[아는 사람 (나와의 관계)]` (에이전트별,
화자 시점이라 사람마다 다름).

---

## 5. 만남 lock (`move_to`에 사람 지목)

**목적** — A와 B가 서로를 만나려고 각자 상대의 "현재 위치"로 `move_to`하면 위치를
맞바꾸며 영원히 엇갈린다. 위치가 아니라 **의도**를 신호로 받는다.

**설정** — 별도 설정 없음. `move_to` 값이 위치 그래프 노드가 **아니면** 사람 지목
(key / alias / `stranger_N`)으로 해석. (위치 그래프가 있어야 의미 있음 — 없으면
`_is_location_name`이 항상 True.)

**엔진 동작** (`meeting.py`):

- `_meeting_intent` = `{추격자 key: 목표 key}` lock. `_apply_move_intents`가 세우고,
  동석·다른 `move_to`·목표 이탈에서 해제.
- 매 wave `_update_meeting_paths`가 목적 노드를 **결정론적으로** 다시 계산:
  - `weak_components` — 의도 방향 그래프의 약한 연결 컴포넌트 (A→B, C→B면 세 명이 한
    자리에). key 사전순 고정.
  - `gathering_node` — 참가자들이 **지금 서 있는** 노드 중 총 이동 홉 합이 최소인 곳.
    동점이면 key 사전순. 외부 공간 참가자는 호출 전 제외.
- `_build_situation_context`에 "채민경을(를) 만나러 이동 중 (현재 채민경는 부엌에 있음)"
  + "생각이 바뀌면 move_to에 다른 장소나 사람" 안내.

**이벤트** — `meeting_update {chaser, target, status}` — `start` / `arrived` /
`cancelled`(reason `gone`/그 외). 문구는 프론트 `state.js:meetingNarration` + 파이썬
`labels.py`가 공유.

**계약 블록** — `build_move_to_hint`가 그래프 있을 때 `move_to` 설명에 "만나러 갈
사람의 ID. 상대가 이동 중이면 도착할 곳으로, 서로 만나러 오면 중간 지점에서".

---

## 6. 시간 모델

**목적** — 시뮬레이션 안에서 시간이 흐르게 해 시간대에 맞는 행동, 목표 기간 종료,
감염 진행을 가능하게 한다.

**설정** — 설정 뷰 "시간 설정" 섹션.

| 필드 | 의미 |
|---|---|
| `sim_start_weekday` / `sim_start_time` | 시뮬레이션 내 시작 요일·시각. 자정 롤오버 시 요일 자동 증가 |
| `time_per_wave` | wave당 경과 분. **0 = 시간 개념 비활성** |
| `time_mode` | `fixed` (wave당 고정) / `variable` (wave 내용을 LLM이 분류해 가변) |
| `time_categories[]` | `variable` 분류 대상 `{id, label, min_minutes, max_minutes}` (기본 4종) |
| `time_estimation_mode` | `variable` 내에서: `category` (LLM이 카테고리 선택 → 범위 내 랜덤) / `ai` (LLM이 경과 분 직접 추론) |
| `max_scene_jump_minutes` / `max_daytime_jump_minutes` | 시간 점프 상한 (45 / 180). 0 = 비활성 |
| `idle_minutes_schedule[]` | 강제 침묵 재투입 시 경과 분 (`[60,120,180]` — 침묵 회차가 늘수록 다음 값) |

**"시간 개념 활성"** = `time_mode == "variable"` **또는** (`fixed` + `time_per_wave > 0`).

**엔진 동작** (`runner.py` + `time_classifier.py`):

- `_current_elapsed_minutes` — 시각 계산 단일 진실 원천 ([`simulation-engine.md §10`](simulation-engine.md#10-시각-계산--_current_elapsed_minutes-단일-진실-원천)).
- **variable — category 모드**: wave 끝에 `classify_wave_time`(LLM)이 대화 내용 + 현재
  시각을 보고 카테고리 하나 선택 → 그 범위에서 `random.randint`. 실패 → `normal_scene`.
- **variable — ai 모드**: `estimate_wave_minutes`(LLM)가 경과 분을 직접 추론 →
  `time_categories` 전체의 min~max로 clamp. 실패 → `normal_scene` 카테고리 폴백.
- **시간 점프 클램프** (`_clamp_time_jump`) — LLM이 고른 raw 경과 분을 엔진이
  **결정론적으로** 상한:
  - 실내 한 곳에 2명+ 동석 발화 중 → `max_scene_jump_minutes` (진행 중 장면 안 잘림)
  - 밤(22~06시) 아니고 집에 남은 사람 있음 → `max_daytime_jump_minutes` (학원·저녁
    재집결 장면 안 건너뜀). 집이 완전히 비면 미적용.
- **강제 재투입 시간 점프** (`mode: "idle"`) — **전원이 고립 독백 중(휴면)**이면,
  LLM 분류 대신 `idle_minutes_schedule`로 시간을 크게 건너뛴다 (회차가 늘수록 다음
  값, 끝에서 고정). "가족이 각자 회사·학교로 흩어진 하루"가 15~30분 조각 점프로
  `max_waves`까지 갈리지 않도록.

**이벤트** — `time_jump {wave, mode, used_fallback, category_id, category_label, reason,
raw_minutes, minutes, clamp_reason, end_time_str}`. `mode`는 `category` / `ai` / `idle`.
fixed 모드엔 안 나옴. `turn_complete`·로그의 `time_str`이 실제 시각.

### 고립 에이전트 휴면 (dormancy)

대화가 비면 전원을 재투입해 `max_waves`까지 계속 돈다 (구 `early_stop_enabled`
플래그는 제거 — "대화가 시들해짐"으로는 더 이상 멈추지 않는다). 단 대화 상대가 없고
아무에게도 닿지 않은 채 **혼잣말만 `max_silence_waves`회 연속**한 에이전트는 "휴면"으로
보고 밀집 재투입에서 뺀다 (`_solo_streak` — `runner.py`). 누군가 이동·이벤트·디렉터
개입으로 그에게 도달하면 스트릭이 0으로 리셋되어 깨어난다. 전원 휴면이면 위 `idle`
시간 점프 + 전원 재투입(새 시각을 보고 재회 판단). 위치 미사용 시나리오는
`_has_reachable_partner`가 항상 True라 이 로직이 절대 발동하지 않는다 (완전 하위 호환).

**진행 불가 백스톱**: 연속으로 성공한 발화가 하나도 없는 wave가 `max(6, max_silence_waves×2)`회
이어지면(LLM 서버 다운, 전부 파싱 실패 등) `end_reason="no_progress"`로 종료 —
`max_waves`까지 헛되이 두들기지 않는다. 구 `early_stop`이 암묵적으로 하던 보호다.

**계약 블록** — `build_time_contract` → `[시간 인식]` (`[현재 시각]` 읽는 법, 평일/주말
행동). 에이전트에겐 ephemeral `[현재 시각: 월요일 오전 9시 00분]`.

---

## 7. 디렉터 (system 에이전트)

**목적** — 이야기가 정체·반복되면 개입해 흐름을 되살리는 내레이터. (= system
에이전트 = 내레이터, 셋 다 같은 것. 내부 식별자 `system`.)

**설정** — 설정 뷰 "system 에이전트" 섹션. `SystemAgentConfig`:

| 필드 | 의미 |
|---|---|
| `enabled` | 활성화 |
| `icon` / `display_name` | 타임라인 표시 (기본 🎬 / "내레이터") |
| `system_prompt` | 페르소나 (비우면 `DEFAULT_SYSTEM_AGENT_PROMPT`) |
| `intervention_interval` | N wave마다 디렉터 LLM 호출 (1 = 매 wave). **디렉터가 도는 유일한 게이트** |
| `silence_threshold` | N wave 미발화면 `[침묵 중인 에이전트]` 목록에 올림 — **강제 발화가 아니라 디렉터에게 주는 정보** |
| `digest_waves` | 개입 판단 시 원문으로 되짚는 최근 wave 수 (엔진 clamp [2,20], 총 라인 120 캡) |
| `director_note` | **불변** 서사 목표·결말 조건 — 매 개입마다 참조 (페르소나보다 우선) |

**엔진 동작** (`system.py` + `system_agent.py`) — wave 루프 **상단**에서 실행.
디렉터는 아래 정보를 받아 **개입 여부를 스스로 판단**한다 (아무것도 안 하면
`interventions: []`). 어떤 신호도 기계적으로 발화를 강제하지 않는다.

- **침묵 감지** — `(wave-1) - _last_spoke_wave[key] >= silence_threshold` → `[침묵 중인 에이전트]` 목록. (독백도 발화라 매 wave 혼잣말하는 에이전트는 침묵으로 안 잡힘)
- **고립 감지** — `not _has_reachable_partner(key)` → `[고립된 에이전트]` 목록.
  프롬프트 규칙: 고립돼서 할 게 없는 에이전트에게 "계속 진행하라" 류 추상적 독려를
  보내지 말 것 — 같은 말 반복만 낳는다. 상황을 실제로 바꾸는 개입을 쓰거나,
  아무것도 하지 말 것.
- **반복 감지 D1** — 최근 4발언(`_REPEAT_WINDOW`)의 어휘 유사도 `>= 0.65`
  (`_REPEAT_THRESHOLD`). 대사 우선, 없으면 행동 묘사.
- **반복 감지 D2** — 디렉터가 `_recent_activity_digest`(최근 `digest_waves` wave 원문)를
  직접 읽고 표현을 바꿔가며 같은 화제를 맴도는 주제 반복 판단. 심층:
  [`director-repetition-detection.md`](director-repetition-detection.md).
- LLM 출력: `{ interventions[], director_memo, reason }`.
  - `interventions[]` — 각 항목 `{targets: [...], message}`. `_resolve_event_targets`로
    `all`/ID 해석 → 대상 전원의 `current_wave`에 `[내레이터] {message}` 주입.
    대상 1명이면 사적 촉발, 여럿이면 공유 자극. **구 `world_event`(별도 세계 사건 채널)는
    여기로 통합됨** — 디렉터가 채널을 고르는 분기가 사라지고 대상 수로 표현이 유도된다.
  - `director_memo` → 스스로 갱신하는 진행 메모 (최근 12줄, `_MEMO_MAX_LINES`).
- **개입 규칙** — 완료된 행동·없던 사물·상태 변화 authoring 금지 (그건 에이전트가 행동으로).
  뒤에 사람·행동이 따라와야 하는 자극(초인종·노크·전화)도 피하고 뒤끝 없는 순간적
  자극(천둥·정전·사이렌·바람·냄새)만. `[현재 시각]` 외 시각 지어내기 금지.

**이벤트**:
- `director_call {wave, digest_waves, prompt_tokens, prompt_chars, elapsed_ms,
  intervened, n_interventions, failed, icon, display_name}` — 돈 사실 + 비용. 개입
  여부와 무관하게 매번 (성능 관측용).
- `system_intervention {wave, targets[], target_aliases[], target_label, message, reason, icon, display_name}` — `target_label`은 전원이면 `"전체"`, 아니면 이름 join.
- `world_event {...}` — **레거시 실행 재생·재출력에서만**. 신규 실행은 안 냄.

**계약 블록** — 없음 (디렉터는 자체 프롬프트로 별도 LLM 호출).

---

## 8. 감염병 모델 (`infection_model`)

**목적** — 결정론적 SIR/SIS 확산. **LLM은 감염 여부를 절대 판단하지 않는다** — 엔진이
접촉만 보고 상태를 계산하고, 결과를 오직 "증상 서사 텍스트"로만 알린다.

**설정** — 설정 뷰 "감염병 모델" 섹션. `InfectionModelConfig`:

| 필드 | 의미 |
|---|---|
| `enabled` | 활성화 |
| `disease_name` | 질병명 (안내 문구용) |
| `transmission_probability` | 같은 wave·같은 장소 감염자 1명당 전염 확률 (0~1) |
| `symptom_stages[]` | `{min_minutes, max_minutes, symptom_text}` — **감염 후 경과 분** 구간별 서사 |
| `recovery_min/max_minutes` | 회복까지 걸리는 시간 구간. 감염 시점에 [min,max]분에서 1회 균등 샘플. `max == 0` = 만성 |
| `immune_after_recovery` | `true` = SIR (회복 후 면역) / `false` = SIS (재감염 가능) |

환자 0번은 `ScenarioEvent` `infect_agent` (설정 뷰에서 "환자 0번" 체크 → 해당 wave에
`infect_agent` 이벤트 생성).

**엔진 동작** (`infection.py`) — **시간 축이 둘**:

- **전염 = wave·접촉 기준** — `_apply_infection_wave`가 **이동 반영 후** 위치로
  `_compute_contact_groups`(같은 장소 2명+, 외부 공간 제외). 감염자 × 비감염자 쌍마다
  `random.random() < transmission_probability` 독립 판정.
- **증상 진행·회복 = 경과 분 기준** — 감염 시점에 `_sample_recovery_minutes()`로 회복
  목표를 1회 뽑고, `now - infected_at_minutes >= recover_at_minutes`면 회복.
  `_find_symptom_stage(elapsed)`가 경과 분이 속한 단계의 `symptom_text` 반환 (범위 밖은
  마지막 단계 유지, 첫 단계가 0에서 시작 안 하면 그 전엔 증상 없음).
- **재개 시 앵커 복원** — `infected_at_minutes`는 경과분 축의 절대 앵커. 재개 방식에
  따라 원점이 달라질 수 있어 `elapsed_minutes_since_infection`(저장 시점의 감염 후
  경과분)을 함께 저장하고 복원 쪽에서 새 원점 기준으로 재계산. `/continue`는
  `rebase_infection_anchors(now=before)` (정상 시 no-op, 접기 누락 회귀 흡수).
- 시간 개념 비활성 → 경과분 항상 0 → 감염자 첫 단계 고정, 자연 회복 없음.

**이벤트** — `infection_update {wave, elapsed_minutes, agent, status, cause,
disease_name}` — `cause` = `event`(시드) / `transmission`(전파) / `recovery`.

**계약 블록** — `build_infection_contract` → `[몸 상태 인식]`: "[몸 상태] 블록이 오면
그게 네 몸이고, 안 오면 멀쩡하다. 수치나 상태값으로는 알 수 없다". 에이전트에겐
감염 중일 때만 ephemeral `[몸 상태]\n{symptom_text}`.

---

## 9. 시나리오 이벤트 (`events`)

**목적** — 지정한 wave에 자동 발생하는 사건. 실행 도중 상황 변경.

**설정** — 설정 뷰 "시나리오 이벤트" 섹션. `ScenarioEvent {wave, type, message, targets, agent}`.

**엔진 동작** (`events.py: _execute_event`) — wave 루프 상단에서 실행:

| type | 동작 |
|---|---|
| `system_message` | `targets`의 memory에 `[시스템] {message}` 주입 |
| `agent_enter` | `active_agents.add(agent)`, 알림 주입, `current_wave`에 추가 (`entrant`) |
| `agent_exit` | `active_agents.discard(agent)`, `_pending_wave`에서 제거, 알림 주입 |
| `infect_agent` | `_set_infected(agent, wave, "event")` — 환자 0번 시드 (감염 모델 꺼져 있으면 무시). `message`는 관전용, memory엔 안 감 |
| `update_appearance` | `_agent_visual[agent] = message`, 같은 장소 사람들에게 씬 메시지 (아는 사이면 실명, 아니면 `stranger_N`) |

**이벤트** — `scene_event {event_type, message, targets, agent, observer_only}`.

---

## 10. 언어 교잡 수정

**목적** — 응답 `content`에 한국어 아닌 문자(한자·영어)가 섞이면 재생성.

**설정** — 설정 뷰 "생성 옵션" 섹션. `lang_fix_enabled` / `lang_fix_retries` (기본 2).

**엔진 동작** (`step.py: _retry_language_fix`) — `_has_foreign_chars(content)`면
`turn_language_fix` emit 후, `[방금 응답에 한국어 아닌 문자... content를 한국어로만
다시]` 지시로 최대 N회 재시도. 다 실패하면 마지막 결과를 그대로 씀 (경고 로그).

**이벤트** — `turn_language_fix {speaker, wave, turn}`.
