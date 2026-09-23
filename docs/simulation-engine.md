# 시뮬레이션 엔진 내부

> `ABM/simulation/`의 `Simulation` 클래스가 wave를 어떻게 돌리는지 — `run()` 루프의
> 처리 순서, 프롬프트 조립, 계약 층, 메모리 압축. 기능별(위치·시간·감염·디렉터)
> 상세는 [`simulation-features.md`](simulation-features.md), 웹 경계는
> [`simulation-overview.md`](simulation-overview.md).

---

## 1. `Simulation` = 믹스인 10종 조합

`ABM/simulation/core.py`:

```python
class Simulation(_LocationMixin, _InfectionMixin, _MeetingMixin, _TargetsMixin,
                 _StatusMixin, _EventsMixin, _TurnMixin, _StepMixin, _SystemMixin,
                 _RunnerMixin):
```

`core.py` 자체가 보유하는 것: `__init__`(모든 config 파싱), 상태 dict들, `_emit`,
`_current_elapsed_minutes`(시각 계산 단일 진실 원천), `export_agent_state` /
`restore_agent_state`(재개 스냅샷), `_apply_engine_contract`(계약 주입),
`_sanitize_relationships`, `_format_time_str`.

| 믹스인 | 파일 | 핵심 메서드 | 담당 |
|---|---|---|---|
| `_RunnerMixin` | `runner.py` | `run()` | Wave 루프. §2 |
| `_StepMixin` | `step.py` | `_step_agent`, `_assemble_agent_prompt` | 단일 턴. §3 |
| `_TurnMixin` | `turn.py` | `_apply_turn_result` | 응답 파싱 → 상태 반영. §4 |
| `_TargetsMixin` | `targets.py` | `_resolve_targets`, `_compute_wave_targets`(→ location.py) | 발화 대상 해석. §5 |
| `_StatusMixin` | `status.py` | `_enter_state`, `_agent_unavailable`, `_agent_traveling`, `_expire_agent_states` | 에이전트 상태(수면·이동). features |
| `_LocationMixin` | `location.py` | `_compute_zone_awareness`, `_build_situation_context`, `_get_or_assign_stranger_id` | 위치·zone·낯선 이·씬 메시지. features |
| `_MeetingMixin` | `meeting.py` | `_apply_move_intents`, `_update_meeting_paths` | 만남 lock. features |
| `_InfectionMixin` | `infection.py` | `_apply_infection_wave`, `_build_symptom_context` | 결정론적 SEIR/SEIRS. features |
| `_EventsMixin` | `events.py` | `_execute_event` | 시나리오 이벤트. features |
| `_SystemMixin` | `system.py` | `_run_system_agent` | 디렉터. features |

---

## 2. `run()` 루프 — wave 1회의 처리 순서

**순서가 곧 정확성이다.** 발화 라우팅·외모·이동을 이동 *전* 위치 스냅샷 위에서 하고,
감염은 이동 *후*에 한다. 이 순서를 바꾸면 "떠나기 전에 한 말을 그 자리 사람이 못 듣는"
유령 발화, "밤새 앓았는데 증상은 그대로"류 모순이 생긴다 (`runner.py` 주석이 각각의
과거 버그를 기록).

`for run_wave in range(max_waves)` 안에서:

```
disp_wave   = _wave_base + run_wave
now_elapsed = _current_elapsed_minutes(run_wave)   ← 이번 wave 시작 벽시계.
              fixed 모드에서 _elapsed_minutes 는 run 내내 고정이므로 at_time
              판정·이벤트 감염 앵커는 반드시 이 값을 써야 시계가 흐른다.

 1. stop_event 확인 → "stopped"
 2. 이번 wave의 시나리오 이벤트 실행 (_execute_event):
       - wave 트리거 + 시계가 next_at 에 도달한 at_time 트리거를 모은다
         (now_elapsed 로 판정; disp_wave·now_elapsed 를 event 에 스탬프)
       - agent_enter면 current_wave에 추가(entrant), system_message면 알림
         받은 대상도 강제 추가(notified) — 알림이 memory에는 들어가도
         current_wave(지난 wave 라우팅으로 이미 확정)에 없으면 그 wave엔
         반응 없이 넘어가고, 자연히 다시 초대될 때까지 미뤄질 수 있었다.
         "정해진 시각에 정해진 사람이 반응한다"를 보장하려면 알림 자체가
         이번 wave 발화 기회를 같이 만들어야 한다
       - 이벤트 실행 후 current_wave 를 active_agents 로 필터 — agent_exit 가
         이번 wave 참가자를 비활성으로 만들면 그 인물은 이 wave 에 발화하지 않는다
 3. current_wave 비었으면 종료 (직전 루프가 no_progress 세팅했으면 존중, 아니면 "no_agents")
 4. 디렉터 (disp_wave > 0 이고 disp_wave % interval == 0)
       _run_system_agent(disp_wave, current_wave) → 개입/세계사건을 current_wave에 주입
       ※ wave 루프 상단에서 돈다 — emit·반응 wave·표시 시각이 일치하도록
 5. _emit("wave_start", {wave, agents})
 6. self._turn_executor.submit(...) × len(current_wave):
       각 (agent_key, incoming) → _step_agent(agent_key, run_wave, disp_wave, turn, incoming)
       as_completed 순서로 results 수집 (LLM 지연에 따라 매번 다름)
       ※ _turn_executor 는 run() 진입 시 한 번만 만든 ThreadPoolExecutor
         (max_workers=len(self.agents), 로스터 상한) 를 wave 마다 재사용한다.
         예전엔 wave 마다 `with ThreadPoolExecutor(...) as executor:` 로 매번
         새 스레드를 띄웠는데, ABM/db/conn.py 가 스레드별 sqlite 커넥션을 캐싱만
         하고 절대 닫지 않아 장기 실행(수백~수천 wave)에서 파일 디스크립터가
         서서히 새다가 "unable to open database file"/"Too many open files"로
         죽는 원인이었다. run() 정상 종료 시 루프 뒤에서 명시적으로 shutdown 하고,
         run() 이 예외로 빠져나가는 경로는 finalize_run()
         (backend/api/simulation/runner.py) 이 `sim._turn_executor` 를
         getattr 로 방어적으로 한 번 더 닫아 대비한다.
 7. stop_event 확인 → "stopped"
 8. results = {k: results[k] for k in sorted(results)}   ← 키순 정규화 (이벤트 emit 결정론)
 9. completed_waves = run_wave + 1

── 발화 라우팅 (이동 전 위치 스냅샷) ──
10. scene_injections = {}   (씬 메시지 버퍼)
    wave_start_location = dict(_agent_location)   ← 이번 wave 시작 스냅샷
11. 각 성공한 speaker:
       resolved = _resolve_targets(result.targets, speaker)   ← 같은 방 + 1-wave 유예
       routed[target] += {speaker, content, action_note}       ← 두 모드 공통
       spatial 모드  → _route_spatial(...) : 그 위에 엿듣기·독백 행동 관찰만 얹음
    ...
    (wave 끝) _prev_wave_start_location = wave_start_location   ← 다음 wave 유예 기준

── 외모 변경 (이동 전 스냅샷) ──
12. 각 update_appearance:
       _agent_visual[speaker] = update_appearance
       _emit("appearance_update")
       같은 장소 사람들에게 "[씬] ..." scene_injection (외부 공간이면 생략)

── 이동 (이동 전 스냅샷 기준으로 의도 해석) ──
13. meeting_before = dict(_meeting_intent)
    _apply_move_intents(results)      : move_to를 장소이동/사람추격으로 분류
    _update_meeting_paths(scene_injections)  : 추격/랑데부/집결 경로 계산
    _emit_meeting_updates(disp_wave, meeting_before)
14. 각 agent의 _agent_path에서 next_loc pop → _agent_location 갱신
       _emit("agent_move")
       도착지/출발지 사람들에게 "[씬] 도착/이탈" scene_injection
       (아는 사이면 실명, 아니면 "낯선 이가 나타났다: <외모>")

── 감염 (이동 후 위치 기준) ──
15. _apply_infection_wave(run_wave, disp_wave)   : 같은 wave·같은 장소 접촉 → 확률 전염

── 휴면 스트릭 갱신 ──
16. 이번 wave에 발화한 각 에이전트: 아무에게도 안 닿았고(순수 혼잣말) 지금 곁에
    대화 상대도 없으면 _solo_streak[k] += 1, 아니면 0으로 리셋
    (_has_reachable_partner — 위치 미사용 시나리오는 항상 True → 스트릭 안 쌓임)

── next_wave 조립 ──
17. next_wave = scene_injections + routed   (active_agents인 것만)

── 침묵 처리 — 종료가 아니라 재투입/시간 점프 ──
18. next_wave 비었으면:
       _solo_streak < max_silence_waves 인 에이전트만 재투입(wakeable)
       전원 휴면(wakeable 없음) → forced_silence_reinject + 전원 재투입
    next_wave 있으면 silence_count = 0

── 진행 불가 백스톱 ──
19. 이번 wave에 성공한 발화 있으면 dead_waves=0, 없으면 dead_waves+1
    dead_waves >= max(6, max_silence_waves×2) → end_reason="no_progress", break

── 시간 누적 (variable 모드만) ──
20. forced_silence_reinject → idle_minutes_schedule[silence_count], _emit("time_jump", mode="idle")
    아니면 → _classify_wave_time / _estimate_wave_minutes → _clamp_time_jump
            _emit("time_jump", {...}) → _elapsed_minutes += jump

21. current_wave = next_wave

── 목표 기간 체크 ──
22. target_minutes > 0 이고 (_current_elapsed_minutes(run_wave+1) - baseline) >= target
       → end_reason = "target_duration", break

23. step_delay 만큼 sleep (stop_event 확인하며)

── 루프 종료 후 ──
_pending_wave = current_wave;  _save_edges()
_emit("simulation_end", {total_turns, edges_count, log_count, end_reason})
```

**`end_reason`**: `max_waves` / `target_duration` / `no_agents` / `no_progress` / `stopped`.
(구 `early_stop_enabled` 플래그 제거 — 대화가 시들해지는 것으로는 멈추지 않고
시간을 건너뛰며 계속한다. `no_progress`는 연속으로 아무 응답도 못 받은 경우만.)

---

## 3. `_step_agent` + `_assemble_agent_prompt`

### `_assemble_agent_prompt(agent_key, wave)` — 프롬프트 조립의 단일 진실 원천

실행 경로(`_step_agent`)와 읽기 전용 조회(`GET /agents/{name}/context`)가 **반드시 같은
규칙**을 쓰도록 한곳에 모았다. **부작용 없음** (이벤트 emit은 호출부 책임).

```
known, strangers  = _compute_wave_targets(agent_key)     # 같은 장소 + 관계 지도(knowledge)
zone_awareness    = _compute_zone_awareness(agent_key)   # 같은 zone 다른 방 (인지만)

ephemeral_msgs (메모리에 저장 안 함, 매 턴 재계산):
  · [현재 시각: {요일} {오전|오후} N시 M분]   (시간 개념 활성 시)
      → _current_elapsed_minutes(wave) 로 계산 (감염 진행과 같은 값)
  · _build_situation_context(...)  → "[현재 상황]" (이 자리의 사람들 + 낯선 이 + zone 인지)
  · _build_symptom_context(...)    → "[몸 상태]"  (감염 중일 때만)

visible_agents = known + [stranger ID들]
target_sections:
  · known/strangers 있음  → [아는 사람]/[처음 보는 사람] 섹션
  · location_mode(내 위치 설정됨)인데 아무도 없음 → <TARGETS> "(없음)"
  · 위치 미사용(레거시)   → 전역 폴백 (활성 에이전트 전원, 섹션 없이 flat)
```

### `_step_agent(agent_key, run_wave, disp_wave, turn, incoming)`

```
1. _inject_incoming(agent, incoming)        → agent.memory에 "[화자] 내용\n(행동)" 추가
2. ctx = _assemble_agent_prompt(agent_key, run_wave)
3. situation_text 있으면 _emit("turn_situation")  (감염 중이면 증상 카드가 한 번 더)
4. _maybe_compress(...)   : est_tokens / token_limit >= 0.70 이고 memory >= 4개 → 압축
5. agent.trim_to_token_limit(...)   : 초과하면 오래된 memory부터 pop
6. _emit("turn_start", {turn, wave, speaker, memory_size, est_tokens, token_limit})
7. call_messages = agent.build_messages(...)
8. content, reasoning, usage, error = _call_llm_for_agent_msgs(...)   ← 브릿지 동기 콜러블
      실패 → _emit("turn_error"), _rollback_incoming, return {success: False}
9. 외국어 감지(_has_foreign_chars) + lang_fix_enabled → _retry_language_fix (최대 N회)
10. extras = parse_json_extras(content)   → move_to, update_appearance
11. result = _apply_turn_result(...)   (§4)
12. _consume_recovery_notice(agent_key)
```

`_llm_for(agent_key)` — 에이전트에 `server_id` 오버라이드가 있으면 그 콜러블, 없으면
시뮬레이션 기본. 시뮬레이션 레벨 호출(시간 분류, 디렉터)은 항상 `self._llm`.

---

## 4. `_apply_turn_result` — 응답 → 상태 반영

```
clean_content, meta, parsed_targets = parse_json_response(raw_content, agent._extra_fields)

agent.add_to_memory({role: assistant, content: raw_content})   ← 원본 JSON 그대로 저장
agent.add_to_log(content, reasoning, extra=meta, targets)      ← logs_graph/{name}.json
_last_spoke_wave[agent.name] = wave

shared_log.append({speaker, content, meta, action_note, targets, wave, timestamp,
                   time_str, location, is_exterior})           ← 이동 전 위치 스냅샷

각 _resolve_targets(parsed_targets, agent_key):
    edges.append({source, target, emotion, meta, content, timestamp})

_emit("turn_complete", {turn, wave, speaker, targets, content, action_note, meta,
                        memory_size, prompt_tokens, token_limit, reasoning_preview,
                        new_edges, is_exterior, time_str})

db.log_turn(...)   → simulation_log

return {success: True, clean_content, action_note, targets}
```

`location`/`is_exterior`는 **이 wave의 이동이 적용되기 전** 값 — 그 턴에 이 에이전트가
실제로 있던 장소이고, 접촉 분석이 쓰는 값이다. `shared_log`·`turn_complete`·DB 세 곳이
같은 스냅샷을 공유한다.

**롤백** — LLM 실패 시 `_rollback_incoming`이 방금 주입한 incoming 메시지를 memory
꼬리에서 pop. 재시도 때 memory가 깨끗하도록.

---

## 5. `_resolve_targets` — 발화 대상 해석

`targets.py`. 반환은 **언제나 플랫 `list[str]`** (runner 라우팅과 turn.py 엣지 생성이
이 형태를 기대).

| 입력 | 해석 |
|---|---|
| `"self"` / `"system"` | 건너뜀 (혼잣말) |
| `"all"` | 화자와 **같은 방**의 활성 에이전트 전원 (아는 사이·낯선 이 구분 없이 — 같은 방이면 목소리가 닿는다). **유예 없음** |
| `"stranger_N"` | 화자 사전의 낯선 이 → 실제 key 변환 + 양방향 knowledge 갱신 ("이름은 만나서 안다"). 직접 타깃이므로 1-wave 유예 적용 |
| `"<key>"` / display_name | 정규화 후 `_can_address`(같은 방 OR 1-wave 유예)일 때만. 화자가 아직 "낯선 이"로만 아는 상대를 실명으로 부르면 폐기 (`_is_anonymous_to`) |

- **외부 공간(exterior) 화자** → 항상 `[]` (아무에게도 전달 불가). 유예도 exterior는 못 뚫는다.
- **위치 미설정** → 항상 같은 방으로 취급 (하위 호환).
- **1-wave 대화 유예** (`_recently_co_located`) → 직접 타깃(`<key>`/`stranger_N`)에 한해,
  지금은 다른 방이어도 **직전 wave 시작 시점에 같은 방**이었으면 한 번 더 배달.
  방금 자리를 뜬 상대에게 답·작별을 건네는 경로. 그 다음 wave엔 유예가 닫힌다.
  `perception_mode`와 무관하게 두 모드 공통. `_prev_wave_start_location`이 기준
  스냅샷 (runner가 매 wave 끝에서 갱신, 재개 스냅샷엔 안 들어감 → 재개 첫 wave는 유예 없음).
- 위치 불일치로 폐기되는 경로는 `logger.debug`로 추적 (무성 폐기가 wave를 통째로
  비우는 원인).

---

## 6. 메모리 압축 — 시간 인식 재설계(2026-09)

에이전트 `memory`(개인 컨텍스트)가 커지면 **구조화 DB 메모리**로 델타 압축한다.
구버전(시간·공간 모델 이전)은 원문에 시각 정보가 전혀 없어 압축 LLM이 "언제 있던
일인지" 알 방법이 없었다 — 그 결과 (1) 한 압축 배치의 모든 사건이 같은 wave
번호로 뭉개지고, (2) "오늘 저녁 메뉴는 X" 처럼 매일 바뀌는 것까지 영구 사실로
쌓이고, (3) `[나의 기억 요약]`이 방금 일어난 일과 며칠 전 일을 구분 없이 같은
글머리로 나열해 에이전트에게 전부 "지금 생생한 지식"처럼 읽혔다. 지금은 3층
구조로 시간 거리를 명시적으로 다룬다.

### 층 1 — 원천에 절대 시간 새기기

`Agent.add_to_memory(message, elapsed_minutes=None)` — 메시지가 memory에 들어가는
**모든** 지점(`_inject_incoming`, 본인 응답 append, 시나리오 이벤트 주입)이 그
순간의 `_current_elapsed_minutes(run_wave)`를 같이 새긴다. wave 번호가 아니라
절대 경과분을 쓰는 이유는 이벤트 감염 앵커(§9)와 같다 — wave는 fixed 모드·재개
후 사후 환산이 꼬이지만 절대 경과분은 그럴 일이 없다. `elapsed_minutes`는
`Agent.memory`의 내부 표현에만 있고, `build_messages()`가 LLM 호출 직전에
`role`/`content`만 남기고 벗겨낸다(OpenAI 호환 API에 알 수 없는 필드가 안 나가게).

### 층 2 — 압축: 일차·요일 구획 + 결정론적 시각 앵커 + 사실/사건 경계

```
트리거 (_maybe_compress, step.py):
    _db 있음  AND  _sim_id 있음  AND  len(memory) >= _COMPRESSION_MIN_MSGS (4)
    AND  est_tokens / token_limit >= _COMPRESSION_THRESHOLD (0.70)
    (est_tokens는 이번 턴의 memory_block도 포함해서 잰다 — _fresh_memory_block)

compress() (ABM/memory_compressor.py):
    원문을 일차·요일·오전/오후 구획으로 묶어 나열 (_format_messages, 각 메시지의
      elapsed_minutes로 판정 — "--- 9일차 화요일 오후 ---" 같은 헤더).
      요일만 쓰면(_constants.format_sim_day_period) 일주일이 지난 뒤 같은 요일이
      똑같은 라벨이 돼(예: 1일차 화요일과 8일차 화요일이 둘 다 "화요일") 압축
      LLM이 그 라벨을 그대로 옮겨 적을 때 몇 주 뒤 같은 문구가 다시 나와 기억이
      혼선된다 — 시뮬레이션 시작일부터 센 절대 일차를 앞에 붙여 절대 안 겹치게
      한다. 요일은 그대로 남긴다(이 세계의 일과가 요일 단위라 맥락 유지용).
    기존 구조화 메모리 재진술 (_format_existing) — episode에 [중요도 N] 접미사를
      **안 붙인다**(with_importance=False). 최종 사용자 블록엔 붙이는데 그 텍스트를
      그대로 "기존 기억"으로 LLM에게 다시 보여주면 LLM이 새로 쓰는 event 문장
      끝에도 똑같은 "[중요도 N]"을 따라 적는 사례가 실측됐다(두 번째 압축부터
      중복 태그). "기존과 중복 제외" 판단에는 중요도 숫자가 필요 없어 이 입력
      에서만 뺀다.
    기존 구조화 메모리 + 위 원문 → LLM (system: "기억 정리 도우미", JSON만)
      → { episodes[], facts[], relationships[], self_state }
      - episodes에 "wave"를 묻지 않는다 — LLM이 준 값(있어도)은 무시
      - facts에는 "계속 참인 것"만(성격·취향·습관·지속 관계), 그날 한정 정보는
        episodes로 적으라고 명시 지시 (반복되는 "오늘 메뉴" 류가 fact로 승격돼
        모순되게 쌓이던 문제의 원인 차단)
      - "오늘"/"어제" 금지, 구획 헤더의 **일차·요일**을 직접 적으라고 지시
        (예: "9일차 화요일 저녁 메뉴는...")
    db.save_messages (raw 아카이브) + db.log_compression
    db.upsert_episodes / upsert_facts  ← elapsed_minutes = now_elapsed(이 압축이
      일어난 시점, **코드가 못박음** — 배치 전체가 같은 값이지만 최소 "실제
      이 근처에 있었던 일"이라는 정확도는 보장된다. 배치 단위 오차(±압축 주기)는
      recency 버킷(층 3) 판정에는 충분)
    db.upsert_relationships / upsert_self_state (wave 그대로, 시각 라벨 없음)
    → agent.memory.clear()

실패 시 → 압축 안 하고 trim_to_token_limit 폴백 (오래된 memory부터 pop)
```

### 층 3 — 렌더링: "지금" 기준 recency 버킷 (캐시 안 함)

`build_memory_block(sim_id, agent_key, db, now_elapsed=...)`가 매 턴 **새로**
호출된다(`step.py::_fresh_memory_block`) — 압축 시점에 캐싱하면 "방금"이 시간이
흘러도 영원히 "방금"으로 굳는다. 호출 시점의 `now_elapsed`와 각 사건의
`elapsed_minutes` 차이(`_days_ago`, 1440분=1일)로 사건을 두 버킷으로 나눈다:

```
■ 경험한 사건:
  방금 있었던 일:              ← 0~1일 전, 있는 그대로(_RECENT_EPISODE_SHOW_MAX=10)
    - ...
  며칠 전, 어렴풋한 기억:       ← 2일 이상 전 (또는 elapsed_minutes 없는 옛 행)
    - ...                     ← 중요도 상위 _OLD_EPISODE_SHOW_MAX(3)개만
    - (그 밖에도 며칠 전 사소한 일 N건은 가물가물하다)   ← 나머지는 개수만
```

같은 사건도 시간이 지나면 "방금" → "며칠 전"으로 자연스럽게 넘어간다. facts/
relationships/self_state는 "계속 참인 것"이라 recency 라벨을 안 붙인다(층 2가
그날 한정 정보를 애초에 episodes로 유도하므로).

**사실(facts)도 표시 상한이 있다** (`_fact_lines`, `_FACT_SHOW_MAX=15`) — episodes와
달리 처음엔 상한이 전혀 없었다. 장기 시뮬레이션에서 압축 사이클이 쌓일수록
"■ 알고 있는 사실:" 목록이 끝없이 자라 `[나의 기억 요약]`이 무한정 커지고, 그만큼
실제 대화(`agent.memory`)가 들어갈 자리가 줄어드는 문제가 실측됐다 — 심하면
`trim_to_token_limit`이 대화 원문을 전부 비워도 사실 목록 하나만으로 token_limit을
넘겨 LLM 호출 자체가 실패할 수 있었다. `get_facts()`가 이미 confidence 내림차순으로
주므로 상위 `_FACT_SHOW_MAX`개만 보여주고 나머지는 "확신이 흐릿한 사실 N건은 생략"으로
개수만 알린다. 압축 프롬프트의 "기존 기억" 재진술(`_format_existing`)도 같은
헬퍼를 써서 상한을 공유한다(안 그러면 압축 프롬프트 자체가 끝없이 커진다).

인터뷰(`backend/api/simulation/interview.py::format_full_memory`)는 같은
`bucket_episodes()`를 쓰되 `cap=False`로 — 회고는 완전성이 목적이라 "예전" 버킷도
개수 제한 없이 전부 보여준다("마지막 무렵 있었던 일" / "그보다 며칠 전, 어렴풋한
기억"). `now_elapsed`는 run 종료 시점의 총 경과분(`simulation_runs.elapsed_minutes`).

`/resume`·`/load`는 옛 run의 `sim_id`로 `build_memory_block()`을 한 번 호출해
`agent._memory_block`에 캐시해 둔다 — 새 run_id 아래 첫 압축이 일어나기 전까지의
다리 역할(구조화 메모리는 sim_id별로 격리돼 있어, 새 run_id로는 옛 기억을 못
읽는다). `build_messages(..., memory_block=None)`이면 이 캐시로 자동 폴백한다.

`agent.build_messages()` 순서:
`[system] + background_log + [memory_block?] + agent.memory + [ephemeral_msgs]`
(`memory_block` 인자를 주면 그걸, 생략하면 `self._memory_block`으로 폴백)

구조화 메모리 테이블 (`episodic_memory`, `semantic_memory`, `relationship_memory` +
`relationship_history`, `agent_self_state`, `compression_log`) → [`database.md`](database.md).
`GET /agents/{name}/memory`가 `db.get_full_memory(sim_id, agent_key)`로 4개를 묶어 반환.

### 2차 기억 정리(consolidation) — "반복되면 깊어지고, 한 번뿐이면 옅어진다"

1차 압축은 새 대화 조각 하나만 보고 매번 독립적으로 판단해서 "이 사실이 전체
기억에서 몇 번째 반복인지" 알 방법이 없다 — 표현만 바뀐 같은 이야기가 별개
행으로 계속 쌓이는 원인이었다(실측: "고등학생이며 수학 학원" / "학생이며 수학
학원"). 2차 정리는 그 에이전트의 **전체 사실**을 다시 조망해:

```
트리거 (_maybe_consolidate_facts, step.py):
    1차 압축이 성공할 때마다 db.count_compressions(sim_id, agent_key)로 누적
    압축 횟수를 세고, _CONSOLIDATION_EVERY_N_COMPRESSIONS(3)의 배수일 때만
    한 번 더 실행

consolidate_facts() (ABM/memory_compressor.py):
    db.get_all_facts(sim_id, agent_key)  ← 상한 없이 id 포함 전체 조회
    len < _CONSOLIDATION_MIN_FACTS(4)면 스킵(정리할 게 없음)
    id를 매긴 전체 목록 → LLM (system: "기억 정리 도우미")
      → { adjustments: [{id, new_confidence, reason}] }
      - 서로 다른 표현이지만 반복/뒷받침되는 사실 → 확신 상향(강화)
      - 한 번만 언급되고 안 뒷받침된 사소한 사실 → 확신 하향(쇠퇴)
      - 사실 문장 자체는 안 바꾼다 — 이번엔 확신도만 재평가
    유효한 id만(존재하지 않는 id는 무시) db.update_fact_confidence(id, conf)
      — conf는 [0,1]로 클램프
```

**행을 지우거나 병합하지 않는다** — 점수(confidence)만 바꾸고, 이미 있는 표시
상한(`_fact_lines`, 상위 `_FACT_SHOW_MAX`개)이 낮아진 확신을 보고 자연히
걸러내게 둔다. 그래서 되돌릴 수 있고(다음 정리 때 다시 오를 수 있음), LLM이
실수로 판단을 잘못해도 원문(raw messages 아카이브)이나 다른 사실을 잃어버릴
위험이 없다 — 사람의 기억 응고(consolidation)가 세부를 지우는 게 아니라
중요한 것의 흔적을 강화하고 그렇지 않은 것을 옅어지게만 하는 것과 같은 원칙.

`consolidation_start`/`consolidation_done` 이벤트가 emit된다(`compression_start`/
`compression_done`과 같은 성격 — 관전용 로그, 피드·마크다운 내보내기엔 안 나감).

---

## 7. `disp_wave` vs `run_wave`

`run()`은 **호출마다 wave 0부터** 센다. `/continue`·`/resume` 후에도 피드 뱃지·DB
`wave` 컬럼·이벤트 라벨이 이어지도록 두 축을 분리한다:

| 축 | 값 | 쓰는 곳 |
|---|---|---|
| `run_wave` | 이번 `run()`의 0-based 카운터 | 시간 계산(`_current_elapsed_minutes`), 감염 진행, 목표 기간 |
| `disp_wave` | `_wave_base + run_wave` | `_emit` 이벤트 `wave`, DB `simulation_log.wave` / `sim_events.wave`, 요약 구간, 디렉터 프롬프트 "Wave N" |

`_wave_base` 세팅:
- fresh `/start` → 0
- `/continue` → `fold_elapsed_and_reset_waves`가 `_wave_base += completed_waves`
- `/resume` → `wave_base_init = (직전 run.start_wave) + (직전 run.total_waves)`

**감염 앵커 재기준화** — `/continue`는 `_elapsed_minutes`를 접기 전의 '지금'(`before`)을
붙잡아 `rebase_infection_anchors(now=before)`로 넘긴다. 접은 뒤 인자 없이 부르면
`now`가 접힌 값을 한 번 더 세어 앵커가 두 배로 밀린다.

---

## 8. 계약 층 (`ABM/prompt_contract.py`)

프롬프트는 두 층:

| 층 | 소유자 | 내용 | 저장 |
|---|---|---|---|
| **서사 층** | 사용자 | 페르소나, 배경, 세계 설정, 감독 노트 | 시나리오에 저장 |
| **계약 층** | 엔진 | JSON 출력 스키마, `move_to` 의미, target ID 규칙, 위치/zone/외부공간 규칙, 시간 인식 포맷, 감염 증상 포맷 | **저장 안 함 — 실행 시 생성** |

계약을 저장하지 않는 이유: DB에 얼려두면 엔진에 기능을 추가해도(예: `move_to` 사람 지목
→ 랑데부) 기존 시나리오가 옛 지시어를 들고 있어 **기능이 조용히 죽는다**. 실행 시점
config만 보고 매번 새로 만들면 엔진 업그레이드가 DB 마이그레이션 없이 전파된다.

### 조립 (recency — 계약이 맨 뒤)

```
[사용자 system_prompt: 페르소나 + 배경]
  + build_map_contract()          (위치 그래프 활성 시)   ┐
  + build_time_contract()         (시간 개념 활성 시)     ├ world_contract — 수명 동안 고정
  + build_infection_contract()    (감염 모델 활성 시)     ┘   (Agent에 1회 주입)
  + build_relationship_contract() (relationships 있을 때) — 에이전트별 (화자 시점)
  + build_output_contract()       (항상, 인터뷰 모드 제외) — 매 턴 재생성 (<TARGETS>가 매번 다름)
```

| 빌더 | 만드는 것 | 조건부 |
|---|---|---|
| `build_map_contract` | `[위치 그래프 — 이동 가능한 경로]` + 이동/외부공간/구역 규칙 | `location_graph` 있을 때만 |
| `build_time_contract` | `[시간 인식]` — `[현재 시각]` 읽는 법, 요일·시간대 행동 | `time_enabled` |
| `build_infection_contract` | `[몸 상태 인식]` — `[몸 상태]` 블록 읽는 법 (status/확률은 절대 안 알려줌) | `infection_enabled` |
| `build_relationship_contract` | `[아는 사람 (나와의 관계)]` — `- 채민경 (ID: "chaemin") — 당신의 아내` | `relationships` 비어있지 않을 때 |
| `build_output_contract` | 출력 JSON 스키마 + `<MOVE_TO_HINT>` + `<TARGETS>` 목록 + `<TARGETS_FOOTER>` | 항상 (인터뷰 `include_output_schema=False` 제외) |
| `build_move_to_hint` | 그래프 없음 → "이동할 위치 이름" / 있음 → "장소명 또는 사람 ID(추격/랑데부)" / zone 있음 → + "다른 방 사람은 ID로 지목" | — |

`build_engine_contract(...)` — 전체를 한 번에 만드는 단일 진입점 (계약 프리뷰
엔드포인트·테스트용). 실행 경로는 world를 `core._apply_engine_contract`로 1회,
output을 `Agent.get_system_message`로 매 턴.

**부재 상상 금지 규칙** — `DEFAULT_OUTPUT_FORMAT_TEMPLATE`의 하단 경고문
(항상 켜짐, 인터뷰 모드 제외 없음)에 "`[현재 상황]`의 '이 자리의 사람들'에
없는 사람이 방금 왔다거나 지금 곁에 있다고 상상해서 서술하지 마십시오"를
추가했다. 실측된 문제: 이동 중(traveling)이라 아직 도착 안 한 가족을 다른
에이전트가 순서상 자연스러운 서사 흐름만으로 "왔다"고 서술하고 몇 wave 연속
그 전제로 대화를 이어간 사례(2026-09-23) — `_compute_wave_targets`가
`[이 자리의 사람들]`에서 그 사람을 정확히 뺐는데도, 그 사람의 부재 자체를
지켜야 한다는 제약이 계약에 없어 발생했다. 디렉터의 "완료된 행동을 지어내지
말라" 규칙과 같은 종류지만, 특정 에이전트 성격과 무관한 보편 규칙이라 계약
층(모든 에이전트, 설정과 무관하게 항상)에 넣었다.

### `verify_contract` — 시작 시 안전망

`Simulation._verify_engine_contract()`가 조립된 프롬프트에 활성 feature별 필수 토큰이
실제로 있는지 검사한다. 예: `has_location_graph`인데 프롬프트에 `move_to`가 없으면
경고 (raise 안 함 — 시뮬레이션은 계속 돈다). 옛 프리즈 템플릿을 오버라이드로 들고
있거나 주입 경로가 리팩터링 중 끊겼을 때 잡는다.

| feature | 필수 토큰 |
|---|---|
| `has_location_graph` | `move_to`, `[위치 그래프` |
| `has_zone` | `[구역:` |
| `time_enabled` | `[시간 인식]` |
| `infection_enabled` | `[몸 상태` |
| `include_output_schema` | `"target"`, `"move_to"`, `update_appearance` |

관계 지도의 dangling/자기참조/단방향은 `_sanitize_relationships`가 초기화 때 걸러내고
같은 경고 채널로 낸다.

### `_agent_knowledge` 시드 — 누가 누구를 아는가

`__init__`에서 관계 지도로 결정된다 (구 `groups`는 제거됨). 계약 층과 같은 on/off 규칙:

- **시나리오 어디에도 관계가 없으면** (기능 미사용) → 전원이 서로 아는 사이.
  옛 "그룹 미설정 = 전원 인지" 기본값 그대로. `stranger_N` 체계가 발동하지 않는다.
- **누군가 관계를 하나라도 명시하면** (기능 사용) → 각 에이전트는 **자기가 명시한
  상대만** 아는 사이. 관계 목록에 없는 사람은 같은 방에서 만나도 `stranger_N`으로
  보인다. 관계는 화자 방향뿐이라(`a→b`만 적으면 `b`는 `a`를 낯선 이로 봄) 익명성을
  대칭으로 두려면 양쪽 다 적어야 한다.

---

## 9. `Agent` (`ABM/agent.py`)

| 속성 | 내용 |
|---|---|
| `system_prompt` | **서사 층** (사용자 소유 — 페르소나 + 배경). 순수하게 유지 |
| `engine_contract` | **계약 층 정적 부분** (world + relationship). `set_engine_contract()`가 **대입** (`+=` 아님 — 재초기화해도 중복 안 됨) |
| `memory` | 개인 대화 컨텍스트 (list of `{role, content}`) |
| `_memory_block` | 압축된 구조화 메모리 블록 문자열 |
| `relationships` | 화자 시점 `{상대 key: 관계어}` (`<TARGETS>` 라벨용) |
| `_extra_fields` | 출력 JSON 추가 필드 |
| `_token_limit` | 프롬프트 토큰 상한 |
| `log_file` | `logs_graph/{name}.json` |

`get_system_message()` = `system_prompt + engine_contract + build_output_contract(...)`
(출력 계약이 맨 뒤, 매 호출 재생성).

`trim_to_token_limit()` — `_estimate_tokens`(UTF-8 bytes / 4)로 추정해 한도 초과 시
`memory.pop(0)` 반복. system+background만으로 초과하면 경고 (더는 못 줄임).

---

## 10. 시각 계산 — `_current_elapsed_minutes` (단일 진실 원천)

에이전트 프롬프트의 `[현재 시각]`, 감염 진행 판정, 목표 기간 판정, **`at_time`
이벤트 발동 판정, 이벤트 감염(`infect_agent`)의 시각 앵커**가 **모두 이 함수**를
쓴다 — 갈라지면 "프롬프트 시계와 병의 진행이 어긋난다".

```
variable 모드          → _elapsed_minutes                       (LLM 분류 누적)
fixed + time_per_wave>0 → _elapsed_minutes + wave * time_per_wave
시간 개념 비활성         → _elapsed_minutes (보통 0)             → 감염자 첫 단계 고정
```

`wave` 인자는 **per-run 카운터(`run_wave`)** 여야 한다. `disp_wave`(= `_wave_base +
run_wave`)를 넘기면 `/continue`·`/resume` 후 fixed 모드에서 이미 `_elapsed_minutes`
에 접힌 이전 run 경과를 `_wave_base * time_per_wave` 만큼 **두 번** 세어 시계가
미래로 밀린다. 이벤트 감염이 disp_wave 를 넘겨 증상·회복이 지연되던 버그가 이것
(runner 가 이벤트에 `at_minutes = _current_elapsed_minutes(run_wave)` 를 스탬프해
`_set_infected` 에 직접 넘기는 것으로 수정).

`_elapsed_minutes`는 두 모드 모두 **이전 run들의 누적 경과**를 담는 자리
(`elapsed_minutes_init`으로 복원, `/continue`가 리셋 전에 이번 run 경과를 접어 넣음).

`_format_time_str(total_min)` — `총 분 // 1440`만큼 시작 요일에서 날짜가 넘어간 것으로
보고 `{요일} {오전|오후} {시}시 {분:02d}분` 반환. fixed·variable 모두 같은 "총 경과 분"을
넘기므로 이 계산 하나가 양쪽을 커버.
