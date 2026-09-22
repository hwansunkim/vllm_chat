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

### 대화 도달성 · 1-wave 유예

`_resolve_targets`가 적용, **`perception_mode`와 무관**하게 두 모드 공통.

- **대화는 같은 방이어야 성립한다.** 다른 방(같은 zone 포함)·외부 공간 상대에게
  한 말은 닿지 않는다. (옛 zone 원거리 대화 `[화자, 멀리서]`는 제거됨 — 벽 너머
  또렷한 대화가 어색하고 상황 컨텍스트와 모순됐다.)
- **직접 타깃(`<key>`/`stranger_N`)엔 1-wave 유예** (`_recently_co_located`): 지금은
  다른 방이어도 **직전 wave 시작 시점에 같은 방**이었으면 한 번 더 배달된다.
  한 wave 안에서 대사·이동을 병렬로 한꺼번에 정하다 보니 "나에게 온 질문을 못 본
  채 자리를 뜨는" 일이 생기는데, 이 유예가 그 답·작별 한마디를 건네게 해준다.
  그 다음 wave엔 직전 스냅샷도 "다른 방"이라 유예가 닫힌다.
- `"all"`엔 유예 없음. 외부 공간은 유예로도 못 뚫는다.
- 기준 스냅샷 `_prev_wave_start_location`은 `run()`이 매 wave 끝에서 갱신. 재개
  스냅샷엔 안 들어가므로 **재개 직후 첫 wave는 유예 없이 시작**.

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

**엔진 동작** — 대화 도달성(직접 배달)은 두 모드가 **같은 방**으로 동일하다
(§1의 "대화 도달성 · 1-wave 유예" 참고). `spatial`은 `run()`의 라우팅 블록에서
`_route_spatial`로 그 위에 부가 효과만 얹는다 (화자의 **이동 전** 위치 기준):

| 상황 | 전달 |
|---|---|
| 같은 방 + **제3자** (지목 안 됨) | 대사 + 행동을 `[화자→대상들]` 엿듣기 태그로 (관찰자 시점, 인지 관계 필터 없음 — 엿듣기는 물리적 사실) |
| 같은 방 + **독백** (`target=self/system`) | 대사 안 들림, **행동만** 씬 채널로 |

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
- **variable — category 모드**: wave 끝에 `classify_wave_time`(LLM)이 카테고리 하나
  선택 → 그 범위에서 `random.randint`. 실패 → 폴백(아래).
- **variable — ai 모드**: `estimate_wave_minutes`(LLM)가 경과 분을 직접 추론 →
  `time_categories` 전체의 min~max로 clamp. 실패 → 폴백(아래).
- **카테고리 id는 순수 내부 키다** — 설정 화면(`time-categories.js`)은 label·
  min·max만 편집하게 하고 id는 아예 보여주지 않는다. `_resolve_time_category`는
  알 수 없는 id를 받으면(분류 실패·미설정 등) **항상 목록의 첫 번째 카테고리**로
  폴백한다 — `"normal_scene"`이라는 특정 id를 찾아가는 특별 취급은 없다(id 값이
  뭐든, 사용자가 라벨을 어떻게 바꾸든 무관). 설정 화면에도 "맨 위 카테고리가
  기본값" 힌트가 있다.
- **시간 추론 프롬프트가 받는 컨텍스트** (`time_classifier.py`): `[현재 시각]`,
  `[인물 배치]`(화자들이 함께 있나 흩어졌나 — `_placement_summary`),
  `[다음 예정 시각]`(가장 이른 미발동 `at_time` 이벤트 — §9), 방법론 블록
  (`_METHOD_BLOCK`), `reason` 먼저 `minutes` 나중.
  - **2026-09 재보정** — 예전엔 "대사는 거의 실시간·의심스러우면 짧게"로
    앵커가 강하게 짧은 쪽으로 쏠려, wave당 평균 2~5분씩만 흘러 하루를
    지나는 데 수백 wave가 필요했다. 대사만 문자 그대로 읽는 속도가 아니라
    사이사이의 화면 밖 동작(차리기·치우기·오가기 등)까지 포함한 "장면 전체
    길이"로 옮기고(한두 마디 2~5분, 보통 장면 15~20분), "의심스러우면
    짧게"는 "의심스러우면 장면 성격에 맞는 중간값으로"로 완화했다. 예정된
    시각을 넘기는 점프는 이 문구와 무관하게 아래 (0) 가드가 항상 별도로
    막고 있어서, 판단 자체를 느슨하게 풀어도 일정을 건너뛰는 사고는
    재발하지 않는다.
- **시간 점프 클램프** (`_clamp_time_jump`) — LLM이 고른 raw 경과 분을 엔진이
  **결정론적으로** 상한 (약한 모델 방어):
  - (0) 아직 발동 안 한 `at_time` 이벤트 시각 → 그 전까지만 (§9 예정 서사 앵커)
  - (1) 실내 한 곳에 2명+ 이 **서로 말을 주고받는 중**(`any_reached`) →
    `max_scene_jump_minutes`. 각자 독백만 하면(취침 등) 보호할 대화가 없으므로
    미적용 — 온 가족이 잠든 밤이 45분씩 갈리지 않도록.
  - (2) 밤(22~06시) 아니고 집에 남은 사람 있음 → `max_daytime_jump_minutes` (학원·저녁
    재집결 장면 안 건너뜀). 집이 완전히 비면 미적용.
- **강제 재투입 시간 점프** (`mode: "idle"`) — **전원이 고립 독백 중(휴면)**이면,
  LLM 분류 대신 `idle_minutes_schedule`로 시간을 크게 건너뛴다 (회차가 늘수록 다음
  값, 끝에서 고정). **이 점프도 (0) 예정 이벤트 시각은 넘기지 않는다** — "가족이
  각자 나간 낮"에 하교·학원이 통째로 스킵되던 버그. "흩어진 하루"가 15~30분 조각
  점프로 `max_waves`까지 갈리지도 않는다.
- **`_placement_summary`** — 시간 추론 프롬프트의 `[인물 배치]` 한 줄. 화자들이
  같은 방인지 흩어졌는지 + **이번 wave에 아무도 서로 말을 안 걸었으면**(각자
  독백·조용함) 그 사실을 실어, 추론이 "함께 있으니 무조건 실시간"으로 오판하지
  않게 한다.

**이벤트** — `time_jump {wave, mode, used_fallback, category_id, category_label, reason,
raw_minutes, minutes, clamp_reason, end_time_str}`. `mode`는 `category` / `ai` / `idle`.
fixed 모드엔 안 나옴. `turn_complete`·로그의 `time_str`이 실제 시각.

### 고립 에이전트 휴면 (dormancy)

대화가 비면 전원을 재투입해 `max_waves`까지 계속 돈다 (구 `early_stop_enabled`
플래그는 제거 — "대화가 시들해짐"으로는 더 이상 멈추지 않는다). 단 **실제 소통 없이
혼잣말만 `max_silence_waves`회 연속**한 에이전트는 "휴면"으로 보고 밀집 재투입에서
뺀다 (`_solo_streak` — `runner.py`). 스트릭이 0으로 리셋(= 깨어남)되는 경우:

- 내가 누군가에게 말이 닿았다 / 이번 wave 에 누군가의 말·씬을 받았다
- 곁에 상대가 있다(`_has_reachable_partner`) — 단 **위치를 쓰는 시나리오**면
  이번 wave 에 방 어딘가에서 실제로 대화가 오갔을 때만. 곁에 사람이 있어도
  **아무도 서로 말을 안 걸면**(각자 독백·취침) 스트릭이 쌓인다 — "한 방에서
  잠든 부부"가 재투입에서 안 빠져 밤이 45분씩 갈리던 버그.

누군가 이동·이벤트·디렉터 개입으로 그에게 도달하면 깨어난다. 전원 휴면이면 위
`idle` 시간 점프(예정 이벤트 시각은 넘기지 않음) + 전원 재투입. 위치 미사용
시나리오는 `_has_reachable_partner`가 항상 True라 예전처럼 이 로직이 발동하지 않는다.

**진행 불가 백스톱**: 연속으로 성공한 발화가 하나도 없는 wave가 `max(6, max_silence_waves×2)`회
이어지면(LLM 서버 다운, 전부 파싱 실패 등) `end_reason="no_progress"`로 종료 —
`max_waves`까지 헛되이 두들기지 않는다. 구 `early_stop`이 암묵적으로 하던 보호다.

**계약 블록** — `build_time_contract` → `[시간 인식]` (`[현재 시각]` 읽는 법, 평일/주말
행동). 에이전트에겐 ephemeral `[현재 시각: 월요일 오전 9시 00분]`.

---

## 7. 에이전트 상태 (수면·이동) — `status.py`

**배경** — 재투입(위 dormancy)이 에이전트의 상태를 전혀 모른 채 무조건 다시 초대해서,
① 이미 잠든 에이전트가 침묵 사이클마다 잠꼬대를 반복하거나 ② zone 경계를 건너는
이동(`location.py::_expand_zone_edges`의 "구역 안 어디서든 1홉 탈출" 설계상 대부분
1 wave에 끝난다)이 순간이동처럼 보이는 문제가 있었다. `_StatusMixin`이 최소한의
상태 계층으로 이 둘을 메운다.

**상태 진입 — 출처가 둘로 갈린다:**
- **자기-선언형**(`state_categories`) — 수면·개인 용무 등 캐릭터의 선택. 에이전트는
  출력 계약의 `enter_state` 필드에 **분이 아니라 카테고리 id**를 고른다 — 정확한
  지속 시간은 `time_categories`와 같은 원칙으로 그 카테고리의 min~max 범위에서
  엔진이 `random.randint`로 뽑는다(`_enter_state`). id는 화면에 노출되지 않는 순수
  내부 키다(`_resolve_state_category` — 알 수 없는 id는 `_resolve_time_category`와
  똑같이 목록의 첫 카테고리로 폴백). `state_categories: []`(빈 리스트, 생략과 다름)면
  기능 자체가 꺼지고 `enter_state` 안내도 계약에서 빠진다.
- **월드-부여형**(`traveling`) — zone 경계를 건너는 hop은 지리적 사실이지 캐릭터의
  판단이 아니므로, 캐릭터가 고르지 않고 이동 적용 루프(`runner.py`)가 hop 적용 시
  자동으로 건다. 지속 시간은 `zone_travel_min_minutes`~`zone_travel_max_minutes`
  범위에서 무작위(둘 다 0이면 기능 비활성 — 다른 점프 캡과 같은 관례). 같은 zone
  안의 이동(방→방)에는 적용되지 않는다(기존처럼 즉시).

**상태가 개입하는 지점은 정확히 셋:**
1. **`_same_room`(targets.py)** — 둘 중 누구라도 지금 `traveling`이면 무조건
   같은 방 아님. raw `_agent_location`은 hop 적용 즉시 목적지를 가리키지만
   (맵 표시는 그대로), 실제로는 아직 그 방에 없는 것과 같다. 한 가드로 양방향이
   같이 풀린다 — 이동 중인 쪽은 남을 못 보고(엿듣기·인지 억제), 이미 그 방에
   있는 쪽도 raw 위치만 보고 "이미 도착했다"고 오인해 타깃하지 않는다(예: 딸이
   하교 중이라 `_agent_location`은 이미 "거실"이어도, 실제 도착 전까지는 거실의
   가족이 그녀를 타깃할 수 없다). `_resolve_targets`의 `_same_loc`, `_compute_wave_targets`
   (location.py, `[현재 상황]`의 아는 사람/타깃 목록)도 같은 원칙의 별도 구현이다.
   `sleep`/`busy`는 물리적으로 그 자리에 있는 것이므로 이 가드와 무관 — **같은 방
   사람이 상태를 알면서도 말을 걸면 정상 전달된다**(direct routing은 재투입과
   무관한 별개 경로라 손댈 필요가 없다).
2. **재투입 `wakeable` 후보**(runner.py) — 상태 중인 에이전트는 제외.
3. **"전원 재투입" 시간 점프**(runner.py) — 활성 에이전트 전원이 상태에 묶여
   있으면 idle 스케줄의 랜덤값 대신 `_earliest_status_clear`로 **가장 이른 상태
   해제 시점까지 정확히** 점프한다(`_next_pending_beat`가 예정 이벤트를 넘기지
   않는 것과 대칭 — 이건 반대로 "그 시점까지는 확실히 건너뛴다").

이동 상태가 자연 해제될 때(hop 적용 시점이 아니라!) "도착했다" 씬 알림도 그제서야
**지금** 그 자리에 있는 사람들 기준으로 새로 만든다(`_deferred_arrival_scene_injections`)
— 이동 시작 시점 기준으로 미리 만들어두면 그 사이 룸메이트가 바뀐 경우 틀린다.

**재개(export/restore)** — `until_elapsed`(절대 앵커)는 감염 상태 복원과 같은 이유로
그대로 믿지 않는다. 저장 시점 기준 **남은 분**(`remaining_minutes`)만 넘기고,
복원 쪽이 새 run의 원점(`elapsed_minutes_init`) 기준으로 다시 앵커를 잡는다
(`export_agent_state`/`restore_agent_state`, core.py).

**계약 블록** — `build_state_hint(state_categories)` → 출력 계약의 `enter_state`
필드 설명 + 카테고리 id/label 목록. `state_categories`가 비면 빈 문자열(동작하지
않는 필드를 광고하지 않는다 — `move_to`와 같은 원칙).

**리뷰 기반 보완 (4건)** — 실제 실행 로그(v11) 재현으로 발견된, 위 "정확히 셋"
설계가 놓친 우회로들. 전부 회귀 테스트로 고정돼 있다(`AgentStatusUnitTests`,
`ZoneTravelStateTests`, `SelfDeclaredStateTests`).

1. **이동 중에도 이미 예약된 턴이 실행됨** — 상대가 말을 건 *그 wave*에 발신자가
   막 이동을 시작해도, `next_wave` 큐 구성 시점에는 traveling 체크가 없어 다음
   wave에 그대로 턴이 돌았다(도착 전인데 답장하는 꼴). 고쳐서 `next_wave`
   구성 시 수신자가 `traveling`이면 큐에 넣는 대신 `_hold_incoming`으로 보류하고,
   상태가 풀리는 wave에 도착 알림과 함께 되돌려준다(`_release_held_incoming`).
2. **직전 wave 동석 유예가 이동 중 차단을 우회함** — `_can_address()`는
   `_recently_co_located()`(방금 전까지 같은 방이었으면 한 wave 봐준다)를
   `_same_loc()`보다 먼저 검사해 즉시 리턴하므로, "방금 막 출발"한 경우 `_same_room`
   traveling 가드를 그대로 건너뛰었다. `_recently_co_located()` 자체의 첫 검사로
   같은 가드를 옮겨 막았다.
3. **시간 점프가 상태 해제 시점을 모름** — `_earliest_status_clear` 기반 캡은
   "전원 재투입"(완전 침묵) 분기에만 있었다. 그런데 짧은 독백(`...` 하나)만
   있어도 일반 시간판정 경로(`_clamp_time_jump`)를 타므로, 화장실행(`busy`
   10~30분) 도중 무관한 대형 AI 점프(예: "다들 잠들었으니 455분")가 그 위를
   덮어써 다음 등장까지 화장실 복귀 장면이 통째로 생략되는 사고가 실측됐다
   (v11 Wave 114→123). `_clamp_time_jump`에도 같은 캡을 추가해 상태 해제
   시점을 넘는 점프는 그 시점까지로 잘린다.
4. **브로드캐스트가 수면/개인용무 중에도 도달**(정책) — `target: "all"`은
   `_same_room` 가드(traveling만 막음)만 거쳤을 뿐 `sleep`/`busy`는 걸러지지
   않아, "다 같이 들으라고 한 말"이 자는 사람에게도 그대로 전달됐다. 논의된
   설계 의도(직접 지목 + 상대가 상태를 인지하고 말 걸면 전달 O, 무차별
   브로드캐스트는 X)에 맞춰 `"all"` 해석에 `_agent_unavailable` 체크를
   추가했다 — 직접 주소(`_can_address`)는 그대로라 "자는 걸 알면서 콕 집어
   말 걸기"는 여전히 전달된다.

**관측성(UI)** — 도입 당시엔 상태가 서버 콘솔 로그 한 줄 외엔 어디서도 안 보였다
("잠꼬대는 없어졌는데 왜 잠들었는지 화면에서 확인할 방법이 없다"는 지적). 지금은
상태 진입/해제마다 영속 SSE 이벤트 `agent_status_change`
(`{wave, agent, display_name, action: "enter"|"clear", state, label, minutes,
until_time_str}`, label/minutes/until_time_str는 enter에만)를 emit하고, 이걸
네 곳에서 소비한다:
- **에이전트 카드 뱃지** (`cards.js::updateAgentStatus`) — 💤/🚶 아이콘 + 라벨을
  카드에 바로 표시, 해제되면 사라짐.
- **피드 카드** (`feed.js::addStatusCard`) — enter/clear 각각 별도 카드로 실행
  이력에 영구 기록.
- **컨텍스트 패널 배너** (`context.js`) — `GET /agents/{name}/context`가 함께
  내려주는 `status`(`_agent_active_status`로 그 순간 기준 새로 계산, 만료됐으면
  자연히 `null`)를 패널 상단에 배너로.
- **마크다운 내보내기** (`export/markdown.js` + 파이썬 쌍둥이
  `ABM/export/markdown.py`) — 다운로드한 시나리오 기록만으로 상태 진입/해제
  시각·사유를 감사할 수 있도록, 토글과 무관하게 항상 포함(`scene_event`와
  같은 원칙). `infection_update`와 같은 이중 emit 패턴이라(해제=대사 전,
  진입=대사 후) `_stream_phase`도 `action` 필드로 갈라 처리한다.

---

## 8. 디렉터 (system 에이전트)

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
- **위치 인지** — `_director_placement_summary()`가 활성 에이전트 전원의 "누가
  어디 있는지"를 장소별로 그룹핑한 한 줄 요약(`거실: 봉미선, 신영식` / `동네
  (외부): 신짱아`)을 `[에이전트 위치]` 섹션으로 넘긴다. 개별 에이전트의 사적
  기억·관계까지는 **의도적으로 주지 않는다** — 디렉터는 "관찰 가능한 것"만
  아는 외부 서술자여야 하고(정보 누출 방지), 에이전트 수만큼 압축 메모리를
  매 개입마다 붙이면 비용도 감당이 안 된다. 프롬프트 규칙이 이 위치 정보를
  근거로 "대상이 실제로 있는 곳에서 지각 가능한 자극만" 보내도록 강제한다 —
  이게 없으면 동네에 나가 있는 에이전트에게 "찌개 냄새가 방 안까지 스며든다"
  같은 실내 전용 자극이 가는 사고가 생긴다(실제 관측됨). 위치 미사용(레거시)
  시나리오는 요약이 빈 문자열이라 섹션 자체가 생략되고 이 제약도 적용 안 됨.
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

## 9. 감염병 모델 (`infection_model`)

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

## 10. 시나리오 이벤트 (`events`)

**목적** — 지정한 시점에 자동 발생하는 사건. 실행 도중 상황 변경.

**설정** — 설정 뷰 "시나리오 이벤트" 섹션. `ScenarioEvent {wave | at_time, type, message, targets, agent}`.

**트리거** — 둘 중 하나:

| 트리거 | 발동 |
|---|---|
| `wave: N` (기본) | N번째 wave 시작 시 |
| `at_time: "HH:MM"` (+ `at_days`) | 시뮬레이션 **시계**가 그 시각에 도달한 첫 wave (`now_elapsed = _current_elapsed_minutes(run_wave)` 로 판정 — fixed·variable 모두). `at_days`(`["mon","wed","fri"]` 등)가 있으면 그 요일마다 **반복** 발동(하루 일과·학원 스케줄), 비었으면 매일. 여러 도래를 건너뛴 큰 점프는 **한 번만**(가장 최근 놓친 것) 발동. **시간 모드가 켜져 있어야** 의미 있음 |

**`at_time` — 예정 서사 앵커.** 시간 추론(카테고리/AI)과 `_clamp_time_jump`가
아직 발동하지 않은 `at_time` 이벤트의 시각을 **넘겨 점프하지 않는다** — "짱구
태권도 16:00" 같은 예정된 장면이 "다들 흩어졌으니 3시간 점프" 판단에 통째로
스킵되는 것을 막는다. 추론 프롬프트에도 `[다음 예정 시각]`으로 노출되고,
`estimate_wave_minutes`의 `hi`가 그 시각까지로 캡된다. `_next_pending_beat()`.
이번 run 시작 시점에 이미 지난 시각은 "발동함"으로 처리 → `/continue`·`/resume`
에서도 안전하게 재전달된다(wave 트리거 이벤트는 재생 안 됨).

**엔진 동작** (`events.py: _execute_event`) — wave 루프 상단에서 실행:

| type | 동작 |
|---|---|
| `system_message` | `targets`의 memory에 `[시스템] {message}` 주입 + **이번 wave의 `current_wave`에 강제 편입**(`notified`). 안 그러면 "16:30. 학원 갈 시간이다" 가 memory에는 들어가도 지난 wave 라우팅으로 이미 정해진 이번 wave 발화 후보 목록엔 없어서, 그 사람이 자연히 다시 초대될 때까지(누가 부르거나 전원 휴면 강제 재투입) 반응이 미뤄질 수 있었다 — 예정 알림은 그 즉시 반응 기회를 보장해야 실제 발화 시점이 예정 시각 근처에 머문다 |
| `agent_enter` | `active_agents.add(agent)`, 알림 주입, `current_wave`에 추가 (`entrant`) — `system_message`의 `notified`와 같은 강제 편입 메커니즘 |
| `agent_exit` | `active_agents.discard(agent)`, `_pending_wave`에서 제거, 알림 주입. 이벤트 실행 후 `current_wave`를 `active_agents`로 필터 → 나간 인물은 **그 wave부터** 발화 안 함 |
| `infect_agent` | `_set_infected(agent, wave, "event", at_minutes=…)` — 환자 0번 시드 (감염 모델 꺼져 있으면 무시). 시각 앵커는 runner가 스탬프한 `at_minutes`(= `_current_elapsed_minutes(run_wave)`); disp_wave를 환산하면 재개 후 fixed 모드에서 두 번 세어 밀린다. `message`는 관전용, memory엔 안 감 |
| `update_appearance` | `_agent_visual[agent] = message`, 같은 장소 사람들에게 씬 메시지 (아는 사이면 실명, 아니면 `stranger_N`) |

**이벤트** — `scene_event {event_type, message, targets, agent, observer_only}`.

---

## 11. 언어 교잡 수정

**목적** — 응답 `content`에 한국어 아닌 문자(한자·영어)가 섞이면 재생성.

**설정** — 설정 뷰 "생성 옵션" 섹션. `lang_fix_enabled` / `lang_fix_retries` (기본 2).

**엔진 동작** (`step.py: _retry_language_fix`) — `_has_foreign_chars(content)`면
`turn_language_fix` emit 후, `[방금 응답에 한국어 아닌 문자... content를 한국어로만
다시]` 지시로 최대 N회 재시도. 다 실패하면 마지막 결과를 그대로 씀 (경고 로그).

**이벤트** — `turn_language_fix {speaker, wave, turn}`.
