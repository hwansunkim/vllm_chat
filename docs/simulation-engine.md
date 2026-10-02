# 시뮬레이션 엔진 내부

> `ABM/simulation/`의 `Simulation` 클래스가 wave를 어떻게 돌리는지 — `run()` 루프의
> 처리 순서, 프롬프트 조립, 계약 층, 메모리 압축. 기능별(위치·시간·감염·디렉터)
> 상세는 [`simulation-features.md`](simulation-features.md), 웹 경계는
> [`simulation-overview.md`](simulation-overview.md).

---

## 1. `Simulation` = 믹스인 11종 조합

`ABM/simulation/core.py`:

```python
class Simulation(_LocationMixin, _InfectionMixin, _MeetingMixin, _JourneyMixin,
                 _TargetsMixin, _StatusMixin, _EventsMixin, _TurnMixin, _StepMixin,
                 _SystemMixin, _RunnerMixin):
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
| `_StatusMixin` | `status.py` | `_enter_state`, `_agent_unavailable`, `_agent_traveling`, `_expire_agent_states`, `_status_release_notice` | 에이전트 상태(수면·이동). features |
| `_LocationMixin` | `location.py` | `_compute_zone_awareness`, `_build_situation_context`, `_get_or_assign_stranger_id` | 위치·zone·낯선 이·씬 메시지. features |
| `_MeetingMixin` | `meeting.py` | `_apply_move_intents`, `_update_meeting_paths` | `move_to` 해석(장소/사람/머무름) + 만남 lock. features |
| `_JourneyMixin` | `journey.py` | `_journey_plan`, `_journey_on_via_arrival`, `_route_guide_lines`, `_record_move_resolution` | 여정(먼 목적지로 가는 길) · 경유지 멈춤/통과 · 가는 길 안내 · move_to 로깅. features §1 |
| `_InfectionMixin` | `infection.py` | `_apply_infection_wave`, `_build_symptom_context` | 결정론적 SEPIR/SEPIRS. features |
| `_EventsMixin` | `events.py` | `_execute_event` | 시나리오 이벤트. features |
| `_SystemMixin` | `system.py` | `_run_system_agent` | 디렉터. features |

---

## 2. `run()` 루프 — wave 1회의 처리 순서

**순서가 곧 정확성이다.** 발화 라우팅·외모·이동을 이동 *전* 위치 스냅샷 위에서 하고,
감염은 이동 *후*에 한다. 이 순서를 바꾸면 "떠나기 전에 한 말을 그 자리 사람이 못 듣는"
유령 발화, "밤새 앓았는데 증상은 그대로"류 모순이 생긴다 (`runner.py` 주석이 각각의
과거 버그를 기록).

루프 진입 전 `current_wave = resume_wave if resume_wave is not None else {start_agent: []}`
— `None`(새 시작)만 start_agent 로 시작한다. 빈 dict `{}`는 "직전 run 의 다음 wave 가
비어 있었다"는 명시적 재개 입력이라(연속 전원 침묵 2회째+ 에서 흔하다, 18번) 첫 wave 의
2b·3번(자연 만료·예정 이벤트 편입 → 빈 wave 안전장치)을 그대로 탄다 — `/resume` 이
저장된 pending `"{}"` 를 넘기는 경우. `/continue` 는 백엔드가 빈 pending 을 `None` 으로
바꿔 넘기므로 사용자가 고른 start_agent 로 시작한다(lifecycle.py).

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
         (agent_exit 는 그 인물의 여정도 취소 — journey_update cancel/exit, at_wave_start)
 2b. 상태 자연 만료 (wave 시작 시점, LLM 호출 전):
       expired = _expire_agent_states(now_elapsed)
       각 만료 → _emit("agent_status_change", action="clear", wave=disp_wave)
                 (그래서 이 wave 의 wave_start 보다 먼저 나간다)
       _journey_on_status_expiry(expired)   ← traveling 해제 = 도착. 여정 중이면:
                 목적지 → 여정 해제(arrive) / 경유지 + 가용한 다른 사람 있음 → 멈춤
                 (남은 경로 삭제, paused) / 없음 → 통과(pass, 경로 유지 → 14번에서 다음 hop).
                 본인 알림이 이 결과를 읽으므로 반드시 알림 조립 **앞**
       arrival_injections = _deferred_arrival_scene_injections(expired)
                 → 도착지 **다른 사람**의 "[씬] X이(가) 이곳에 도착했다" — 10번
                   scene_injections 의 초기값이 되어 **다음 wave** 에 전달
       활성인 만료자 본인 → current_wave[key] =
                 [해제 알림(_status_release_notice), *_summarize_held(held_incoming), *기존 incoming]
                 → 이번 wave 에 바로 턴 ("[씬] 고등학교에 도착했다." /
                   "[씬] 잠에서 깼다. (누나방)" / "[씬] 하던 일을 마쳤다: 씻기. (안방화장실)")
                   여정 경유지면 "[씬] 거실에 도착했다. (동네로 가는 길에 들름, 잠시 멈춤)"
                   / "(…, 그대로 지나가는 중)"
       ※ 위치 이유: 자연 만료는 at_time 이벤트의 notified 처럼 "엔진이 시간으로
         부르는 참가자"라 이벤트·활성 필터 뒤(방금 퇴장한 사람은 편입 안 함),
         3번 가드 앞(사유가 있는 사람이 정말 아무도 없을 때만 빈 wave 안전장치가
         발동하도록 — 활성만 넣으므로 "전원 퇴장" 종료는 그대로), 디렉터 앞(개입이
         알림 뒤에 붙는다), traveling 관문 앞(방금 도착한 사람은 통과). 예전엔 LLM 호출 **뒤**에 있어 풀린 본인은 그
         wave 에 턴도 알림도 없었다(case1: 23:09 에 씻기가 끝난 아빠가 01:46 에야 턴).
       ※ 개입으로 턴을 받아 enter_state 를 비워 즉시 해제되는 경로(12b)는 이
         만료를 거치지 않으므로 알림 대상이 아니다.
 3. current_wave 비었으면:
       활성 에이전트가 있으면 → **빈 wave 안전장치** (_empty_wave_fallback, logger.info)
         가용(상태 미잠금) 활성 에이전트 전원을 빈 incoming [] 으로. 가용자가 없고
         전원 상태 잠금이면 가장 먼저 풀리는 한 명(18번 wake_key 와 같은 규칙,
         동률은 key 사전순). 시간 점프는 하지 않는다.
         ※ 정상 흐름에서 여기 오는 건 18번이 연속 전원 침묵 2회째+ 에 소외 해당자가
           없어 next_wave 를 비웠고, 그 뒤 idle 점프가 클램프 없이 크게(60/120/180분)
           흘러 2·2b 의 편입 대상(일정 알림·해제)도 없는 경우다 — 오랜 시간이
           지났으니 전원 행동이 자연스럽다. 클램프된 짧은 wave 는 2·2b 가 사유가
           있는 사람만 채워 여기 안 걸린다.
         ※ 이번 wave 참가자만 agent_exit 하고 다른 활성 에이전트가 남은 경우도 여기로
           온다(예전엔 no_agents 종료).
       활성 에이전트가 하나도 없으면 → 종료 (직전 루프가 no_progress 세팅했으면
         존중, 아니면 "no_agents")
 4. 디렉터 (disp_wave > 0 이고 disp_wave % interval == 0)
       _run_system_agent(disp_wave, current_wave) → 개입/세계사건을 current_wave에 주입
       ※ wave 루프 상단에서 돈다 — emit·반응 wave·표시 시각이 일치하도록
 4b. traveling 공통 관문 — current_wave 에 남은 이동 중 에이전트를 _hold_incoming 으로
       보류하고 제외(예약 이벤트·디렉터가 traveling 체크 없이 꽂은 경우 차단)
       참가자 **전원**이 이동 중이면: 이동 중이 아닌 가용 활성 에이전트가 있으면 참가자를
         보류하고 그들 전원으로 채운다(3번 안전장치와 같은 규칙 — 18번 축소 이후 "일정
         알림 대상이 하필 이동 중인 한 명뿐"인 wave 가 흔해져서). 가용자도 없는 극단적
         경우만 옛 폴백(그대로 진행 + warning).
 4c. 자기 선언 상태 중 턴 — 보류분 먼저: current_wave 의 각 key 가 아직 자기 선언
       상태(sleep·busy·사용자 정의, traveling 외)인데 턴을 받으면(직접 타깃·예약 이벤트
       알람·디렉터·안전장치 wake_key) _pop_held_for_turn 으로 held_incoming 을 꺼내
       요약(_summarize_held)해 incoming **맨 앞**에 붙인다. 상태는 유지 — 12b 에서
       재선언하면 계속, 비우면 즉시 해제(보류분은 이미 여기서 전달돼 누락 없음).
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
 7. stop_event 확인 → "stopped"  (2b 의 arrival_injections 는 current_wave 에 실어
       _pending_wave 로 넘긴다 — 상태는 이미 지워져 재개 때 다시 만들어지지 않으므로)
 8. results = {k: results[k] for k in sorted(results)}   ← 키순 정규화 (이벤트 emit 결정론)
 9. completed_waves = run_wave + 1

── 발화 라우팅 (이동 전 위치 스냅샷) ──
10. scene_injections = arrival_injections   (씬 메시지 버퍼 — 2b 의 다음-wave 도착 알림으로 시작)
    wave_start_location = dict(_agent_location)   ← 이번 wave 시작 스냅샷
11. 각 성공한 speaker:
       resolved = _resolve_targets(result.targets, speaker)   ← 같은 방 + 1-wave 유예
       routed[target] += {speaker, content, action_note}       ← 두 모드 공통
       spatial 모드  → _route_spatial(...) : 그 위에 엿듣기·독백 행동 관찰만 얹음
                       (각 항목에 kind=overheard/action_scene 표식 — 17번 보류 판정·요약용.
                        화자가 자기 선언 상태를 **이어가는** 턴(같은 상태 재선언)이면
                        독백 행동 방송은 생략 — 숨소리 핑퐁 차단. 상태에 드는 행동·
                        소리 낸 대사의 엿듣기는 그대로)
    ...
    (wave 끝) _prev_wave_start_location = wave_start_location   ← 다음 wave 유예 기준

── 외모 변경 (이동 전 스냅샷) ──
12. 각 update_appearance:
       _agent_visual[speaker] = update_appearance
       _emit("appearance_update")
       같은 장소 사람들에게 "[씬] ..." scene_injection (외부 공간이면 생략)

── 상태 선언 ──
12b. 각 성공한 speaker의 enter_state:
       있으면 _enter_state(category_id) → 새로 걸렸을 때만 _emit("agent_status_change", action="enter")
       없는데 자기-선언형 상태 중이면 → 즉시 해제(개입으로 턴을 받았다는 뜻) +
         _emit(action="clear"). 이미 턴을 받은 것이라 2b 의 해제 알림은 없다.
       sleep 에 새로 들면 여정 취소(reason sleep) + 그 여정의 남은 경로도 버린다.
       (zone 경계 이동의 traveling 은 14번 이동 루프가 엔진 판단으로 건다)

── 이동 (이동 전 스냅샷 기준으로 의도 해석) ──
13. meeting_before = dict(_meeting_intent)
    _apply_move_intents(results, disp_wave) : move_to를 장소이동/사람추격/머무름으로 분류
       - 지금 위치 = 머무름: 남은 경로·여정·만남 lock 버림(옛 "lock 없으면 무시" 폐기)
       - 장소 → 경로 + _journey_plan (경유지 있으면 여정 start, 같은 목적지면
         resume/continue, 다른 곳이면 기존 여정 cancel)
       - 원본·해석 → _record_move_resolution (에이전트 로그 주석 + move_intent 이벤트)
    _update_meeting_paths(scene_injections)  : 추격/랑데부/집결 경로 계산
    _emit_meeting_updates(disp_wave, meeting_before)
14. 각 agent의 _agent_path에서 next_loc pop → _agent_location 갱신
       단, 지금(now_elapsed) traveling 중이면 pop 하지 않고 경로를 **보존**한다 —
         다음 hop 을 꺼내면 traveling 을 덮어써 순간이동이 되고 중간 노드 사람의
         도착 알림(pending_arrival_announcement)도 사라진다(case1 v9: 17:00 학교→
         [거실, 동네] 첫 hop 17분 이동 중 17:01 wave 에서 거실→동네로 넘어감).
         13번이 이 wave 에 경로를 새로 깔았어도(만남 재계산 등) 보류만 한다. 도착 wave
         에는 2b 가 상태를 지우고 알림 + 턴을 주므로 여기서 다음 hop 이 정상 진행된다
         (그 턴 move_to 가 null 이면 이어서, 다른 곳이면 13번이 이미 경로를 교체).
       _emit("agent_move")
       도착지/출발지 사람들에게 "[씬] 도착/이탈" scene_injection
       (아는 사이면 실명, 아니면 "낯선 이가 나타났다: <외모>")
       이동 시간 없는(zone_travel_max=0) 경계 hop 으로 여정의 경유지에 들어선 사람은
       instant_via_arrivals 에 모은다(판정은 14a — 루프 순회 순서에 흔들리지 않게)
14a. _journey_after_moves(instant_via_arrivals, ...) : 이동 후 위치 기준
       - 즉시 경유지 도착자 멈춤/통과 판정 (멈춤이면 본인에게 다음 wave 씬 알림)
       - 구역 안 hop 으로 목적지에 들어선(traveling 아님) 여정 해제(arrive)

── 이동 후 보강 배달 (이동 후 위치 기준, 직접 타깃 한정) ──
14b. _deliver_post_move(...)  : 이번 wave에 위치가 바뀐 화자의 직접 타깃 중 1차에서
       못 받은 사람이 이제 같은 방(둘 다 traveling 아님, 인지 규칙 준수)이면 routed에 추가
       + edge + _emit("post_move_delivery"). "걸어가며 한 말은 도착지에서도 들린다".
       1차(이동 전) 라우팅은 그대로 — 출발지 사람도 계속 듣는다.
       보강 배달 대상은 first_resolved 에도 합류한다(17번 직접 타깃 집합).

── 감염 (이동 후 위치 기준) ──
15. _apply_infection_wave(run_wave, disp_wave)   : 같은 wave·같은 장소 접촉 → 확률 전염

── 도달 여부 집계 ──
16. any_reached = any(reached_someone.values())  (시간 추정의 장면 보호 캡용)
    (턴을 받은 에이전트는 결과 수집 직후 _last_turn_wave[key] = disp_wave 로 기록 —
     성공 여부 무관. 소외 재투입의 기준)

── next_wave 조립 ──
17. next_wave = scene_injections + routed   (active_agents인 것만, kind 표식은 뗀다)
       수신자가 traveling      → 전부 _hold_incoming (기존)
       수신자가 자기 선언 상태 → 이번 wave 에 그를 **직접 타깃**한 말(first_resolved —
         1차 해석·1-wave 유예·14b 보강 배달)이 있으면 전부 배달(비직접 씬·엿듣기도 그
         턴에 함께), 없으면 전부 _hold_incoming (도착·이탈·외모·만남·여정 씬, 엿듣기,
         행동 관찰 씬). 보류분은 2b(자연 만료) 또는 4c(상태 중 턴)에서 요약돼 전달.
       ※ 보류로 next_wave 가 비면 18번 전원 침묵 처리가 그대로 돈다 — 전원 상태 잠금이면
         wake_key + 해제 시점 점프(case1 v9 W108~114 의 숨소리 핑퐁이 막던 밤 점프 복원).
       ※ reached_someone/any_reached 는 11·14b 의 직접 해석으로만 정해져 보류와 무관
         (보류분은 직접 타깃이 아니므로 "닿음"이 아니다).

── 침묵 처리 — 종료가 아니라 재투입/시간 점프 ──
18. next_wave 비었으면 (전원 침묵): silence_count += 1
       가용(sleep·busy·traveling 에 안 묶인) 활성 에이전트가 있으면:
         silence_count == 1 → 가용 전원 재투입 (시간은 일반 경로)
         silence_count >= 2 → **소외 기준 해당자만** 재투입(_starved_agents — 아래 소외
           재투입과 같은 판정, 있으면 _emit("starvation_reinject")) + idle_jump.
           해당자가 없으면 next_wave 는 **빈 채로** 둔다 — 해제 알림 대상(2b)·예정 이벤트
           알림 대상(2 notified)은 다음 wave 시작에 자동 편입되고, 아무도 없으면 3번
           빈 wave 안전장치가 가용 전원을 부른다.
           (의도: idle 점프가 남의 상태 해제·예정 이벤트 시점에 클램프된 1~16분짜리
            짧은 wave 에서는 사유가 있는 사람만 턴을 받는다. case1 v9 실측: 낮 W31~47
            턴 59개 중 50개가 매번 전원 재투입된 반복 독백이었다.)
       전원 상태 잠금이면 → 가장 먼저 풀리는 한 명(wake_key)만 재투입 + idle_jump
         (다음 wave 2b 에서 wake_key 의 [] 앞에 해제 알림이 한 번 붙는다 — 같은 키라 중복 없음)
    next_wave 있으면: silence_count = 0, 그리고 소외 재투입(starvation reinject) —
       _starved_agents: 활성 · next_wave에 없음 · 상태 아님 ·
       disp_wave - _last_turn_wave[k] >= starvation_waves 인 에이전트를 빈 incoming [] 으로
       추가, _emit("starvation_reinject")
       (한 번도 턴을 안 받은 에이전트는 run 시작 wave(_wave_base)를 기준으로 간주)

── 진행 불가 백스톱 ──
19. 이번 wave에 성공한 발화 있으면 dead_waves=0, 없으면 dead_waves+1
    dead_waves >= max(6, starvation_waves×2) → end_reason="no_progress", break

── 시간 누적 (variable 모드만) ──
20. idle_jump → idle_minutes_schedule[min(silence_count-1, len)-1] (2회째에 첫 값, 끝에서 포화;
       전원 상태 잠금이면 가장 이른 해제 시점까지. 그다음 min(예정 이벤트 시각,
       가장 이른 상태 해제 시점)에서 클램프 — 일부만 상태 중이어도 누군가의 해제
       시점을 넘지 않는다(실측: 23:09 해제인데 22:46→23:46 점프). 동률이면 예정 이벤트,
       최소 1분. clamp_reason "상태 해제 시점 전까지 60→29분" / "예정 이벤트(HH:MM) 전까지 …"),
       _emit("time_jump", mode="idle")
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
    사실 후보 검색 (_select_candidate_facts, 2026-10 2~3단계) — db.get_all_facts()
      (상한 없는 전체) 중 이번 배치 원문과 문자 바이그램이 겹치는 사실을 포함률 순으로
      최대 _CANDIDATE_FACT_LIMIT(12)개, 남는 자리는 확신 높은 순. "기존 기억"의 사실
      줄은 이 후보를 [id=N] 과 함께 보여준다(예전: 확신 상위 15개, id 없음)
    db.save_messages (raw 아카이브) → 삽입 id 목록 = 이 배치 사실들의 원문 근거
      ← **LLM 호출 전**(근거 id 를 미리 확보). 호출·파싱 실패 시 db.delete_messages
        로 방금 넣은 행만 지운다 — "실패한 배치는 보관 안 함" 보장 유지
    기존 구조화 메모리 + 위 원문 → LLM (system: "기억 정리 도우미", JSON만)
      → { episodes[], facts[], relationships[], self_state }
      - facts 각 항목: judgment(신규|동일_의미|변경|보류) + matches_id(후보 id).
        _parse_compression_result 가 정규화 — 모르는 값·필드 없음·id 없는 동일/변경
        → 신규(예전과 같은 새 행 경로)
      - episodes에 "wave"를 묻지 않는다 — LLM이 준 값(있어도)은 무시
      - facts에는 "계속 참인 것"만(성격·취향·습관·지속 관계), 그날 한정 정보는
        episodes로 적으라고 명시 지시 (반복되는 "오늘 메뉴" 류가 fact로 승격돼
        모순되게 쌓이던 문제의 원인 차단)
      - "오늘"/"어제" 금지, 구획 헤더의 **일차·요일**을 직접 적으라고 지시
        (예: "9일차 화요일 저녁 메뉴는...")
    db.upsert_facts(source_message_ids=배치 id, candidate_ids=이번 후보)  ← 쓰기 단계 맨 앞
    db.log_compression
    db.upsert_episodes
                                       ← elapsed_minutes = now_elapsed(이 압축이
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
개수만 알린다. 압축 프롬프트의 "기존 기억" 재진술(`_format_existing`)도 상한이
있다 — 2~3단계부터는 확신 상위가 아니라 아래 후보 검색의 `_CANDIDATE_FACT_LIMIT`(12)개
(안 그러면 압축 프롬프트 자체가 끝없이 커진다).

### 사실 중복 차단 — 후보 검색 · ID 판정 · 원문 근거 (2026-10, 기억 메커니즘 2~3단계)

**문제(실측, 일주일 실행):** 날짜만 다른 같은 사실("6일차 토요일 오후까지 코로나19로
지속적으로 맛과 냄새를 느끼지 못하고 있다" / "7일차 …")이 한 에이전트에 6개 이상 쌓였고
전부 확신 100%였다. `upsert_facts`가 문장이 **완전히 같을 때만** 기존 행과 합쳤기 때문이다.
1단계(2차 정리의 상향 제거)는 이미 100%로 생긴 중복엔 효과가 없다 — 생성을 막아야 한다.

**구조(설계 문서 `_workspace/2026-10-01_memory_mechanism_proposal_review_response.md`
§2·§3):**
1. **후보 검색(코드)** — `_select_candidate_facts`가 전체 사실 중 이번 배치와 관련된 것만
   추려 `[id=N]`과 함께 보여준다. 유사도는 **IDF 가중** 문자 바이그램 포함률(사실의 2-gram 중
   원문에도 나오는 것의 가중 비율) — 한국어는 띄어쓰기·조사 변화가 커서 단어 비교가 잘 안
   맞고, `SequenceMatcher`를 짧은 사실 vs 긴 원문에 쓰면 길이 차이로 점수가 눌린다. IDF 는
   그 에이전트의 사실 전체 기준(`log((1+N)/(1+df))`)이라 어미·이름("는다"·"있다"·"짱구"·"엄마")
   처럼 흔한 조각은 신호에서 빠진다 — 가중치가 없으면 그런 조각을 많이 공유하는 무관한 일상
   사실이 진짜 관련 사실을 상한 밖으로 밀어내 LLM 이 id 로 지칭 못 하고 중복이 다시 생겼다
   (QA 재현: 19개 중 "코로나19로 맛과 냄새를 느끼지 못한다"가 14~16위 → IDF 후 4위).
2. **판정(LLM)** — 새 사실마다 `judgment` ∈ {신규, 동일_의미, 변경, 보류} + `matches_id`.
   표현만 다른 재진술은 동일_의미, 같은 사실이 이어지지만 기간·시점·정도가 갱신된 것
   ("6일차까지 …" → "7일차까지 …")은 변경으로 지칭하라고 지시한다 — 둘 다 행은 1개로
   유지되고, 변경이면 문장이 최신으로 바뀌고 직전 문장이 `prev_fact`에 남는다(동일_의미로
   묶으면 첫 문장 "1일차까지 …"가 그대로 남아 날짜가 낡는다).
3. **검증·반영(코드, `ABM/db/semantic.py::upsert_facts`)** — `matches_id`는 (a) 같은
   `sim_id`+`agent_key`의 행이고 (b) **이번 호출에서 LLM 에게 실제로 보여준 후보 id**
   (`compress()`가 넘기는 `candidate_ids`)여야 한다. 둘 중 하나라도 아니면 거부하고 신규로
   폴백한다 — 남의 기억도, LLM 이 보지도 못한 기억도 덮지 않는다. `candidate_ids=None`(직접
   호출·구 경로)이면 ID 매칭을 **끈다**(누가 무엇을 보고 판정했는지 모르므로 안전 쪽).
   - 동일_의미 → **새 행 없음**. 그 행의 `source_message_ids`에 이번 배치 근거만 누적.
     확신도·`updated_at`·`elapsed_minutes`는 **건드리지 않는다** — 재진술을 새 발생으로 치지
     않는다(병합 시각을 최근 시각으로 쓰면 오래된 사실이 요약될 때마다 새 사실이 된다).
   - 변경 → 바뀌기 직전 버전(문장·확신도·근거·시각)을 `semantic_memory_history`에 남기고
     (reason=`changed`) 행을 새 문장·확신도로 갱신한다. 현재 행의 근거는 **이번 배치로 리셋**
     — 이전 문장(반대 내용일 수도 있다)을 지지했던 근거가 현재 문장의 지지 횟수로 섞이지 않게
     (2차 정리의 "K번의 대화에서 확인"). `prev_fact`/`prev_confidence`는 하위 호환으로 직전 값.
     A→B→C 뒤에도 A·B 를 `SimDB.get_fact_history()`로 조회한다(원문 근거가 NULL 인 옛 행도 최초
     구조화 문장이 이력에 남는다). 문장이 그대로인 "변경"은 내용 변화가 아니므로 **동일_의미와
     완전히 같은 경로**다(근거만 추가, 확신도·시각·prev 불변, 이력 없음 — 확신도를 덮어쓰면 이력
     없이 0.95→0.2 같은 변화가 남아 복원할 수 없다).
   - 변경 후 문장이 같은 sim/agent 의 **다른 행**과 같아지면 대표 하나로 통합한다: LLM 이 지칭한
     행이 대표, 다른 행은 이력에 reason=`merged`·`merged_into`=대표로 보존한 뒤 삭제. 그 행의 근거는
     현재 문장을 지지했으므로 대표 근거에 합치고, 확신도는 **max**. 이번 호출에서 지운 id 를 같은
     배치에서 다시 지칭하면 대표로 이어진다. 예전 호출에서 지운 id 면 이력의 `merged_into`를
     따르되 그 대표도 **이번 후보 집합에 있어야** 받아들인다(리다이렉트로 후보 검사를 우회하지
     않게). 옛 문장을 다른 중복 행이 공유하고 있었다면 같은 배치의 문자열 매칭은 그 행으로 잇는다.
   - 반영 도중 하나라도 실패하면 `upsert_facts` 호출 전체를 롤백한다(이력·삭제·갱신이 반쯤
     남거나, 열린 트랜잭션이 다른 커넥션을 잠그지 않게). 압축 응답의 `confidence`는 파서가 미리
     정규화한다(숫자 아님·NaN → 1.0, 0~1 클램프). `compress()`는 쓰기 단계에서 사실을 **맨 앞**에
     반영한다. 쓰기가 실패하면 먼저 열린 트랜잭션을 롤백하고(반쯤 쓴 관계 등이 뒤따르는 commit에
     섞여 저장되지 않게), **사실 반영 전**의 실패면 미리 저장한 원문(messages)도 지운다. 사실이
     이미 커밋된 뒤의 실패면 원문은 남긴다 — 커밋된 사실의 근거 id 가 그 원문을 가리키기 때문이다.
     이 경우 이미 커밋된 사실·압축 로그·에피소드는 그대로 남고(단일 트랜잭션 아님), 예외는 그대로
     올라가 다음 압축에서 같은 메시지가 다시 보관·처리될 수 있다(같은 문장의 "변경"은 동일_의미
     경로라 이력이 중복되지 않는다).
   - 신규·보류·필드 없음·검증 실패 → 예전과 똑같은 문자열 매칭 경로(새 행).
4. **원문 근거** — `save_messages()`가 삽입 id 를 돌려주고, 그 배치에서 나온 사실은 그 id 들을
   `source_message_ids`(JSON 배열)로 갖는다(배치 단위 — 문장별 근거는 알 수 없다). 현재 행의
   근거는 **현재 문장을 지지하는 배치**만, 이전 버전·통합된 행의 근거는 이력 행에 있다 — 그래서
   잘못된 변경·병합을 문장·확신도·근거 단위로 추적·되돌릴 수 있다(코드 리뷰
   `to_master/2026-10-02_memory_id_matching_code_review.md` 4건 반영).

**2차 정리와의 상호작용:** `consolidate_facts`의 사실 목록에 근거 **배치** 수를 함께 보여준다
(`[id=42] … (확신 80%, 6번의 대화에서 확인)` — `SimDB.count_source_batches`, 근거 메시지들의
서로 다른 `created_at` 수). 안 그러면 여러 번 재진술돼 1행으로 병합된 사실이 확신도만 보여
"딱 한 번 언급된 사실"처럼 쇠퇴 대상이 될 수 있다. 메시지 수가 아니라 배치 수인 이유: 한
배치에서 한 번 나온 사실도 메시지 수로는 "근거 30건"처럼 보인다. 근거를 모르는 옛 행은 표시
안 함.

**범위·보류:** 이번엔 **사실만**이다. 사건(episodes)의 대표 통합과 반복 횟수·발생 시각
집계는 **4단계로 계속 보류**(실제 사건을 구분할 체계가 먼저 필요 — 응답 문서 §4). 동일 의미
병합이 생겼어도 확신도 상향("진짜 반복 강화")은 재도입하지 않았고, 2차 정리의 "상향 무시"
가드도 그대로다(별도 논의). 이 컬럼 도입 전 행의 `source_message_ids`는 NULL 그대로다.

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

**raw 구간의 시간 인식 갭** — 위 3층은 **압축된** 기억에만 적용된다. 아직 압축되지
않은 `agent.memory`(최근 대화)는 층 1이 새긴 `elapsed_minutes`를 갖고 있지만
`build_messages()`가 LLM 호출 직전에 그걸 벗겨내므로, 최근 대화만 남아 있는 동안은
에이전트가 "그게 언제 있었던 일인지" 알 방법이 없었다. 이 갭은 **시간 앵커**
(`location.py::_time_anchor_notice`, `memory_time_anchor_enabled`)가 채운다 — 매
메시지에 타임스탬프를 붙이는 대신, 문턱값 이상 시간이 점프했을 때 또는 자정 경계를
넘었을 때만 `[시간] 3일차 수요일 오후 7시 20분` / `[날짜 변경] ...` 라벨 한 줄을 그
에이전트의 raw 메모리에 영구 기록한다. 일차 계산은 압축 헤더(`format_sim_day_period`)와
같은 공식을 쓰고 문구도 같은 라벨 스타일이라, 그 줄이 나중에 압축될 때 층 2의 구획
헤더와 충돌하지 않는다. 상세는
[`simulation-features.md §6 시간 앵커`](simulation-features.md#시간-앵커--압축-전raw-메모리의-시간-인식-locationpy).

구조화 메모리 테이블 (`episodic_memory`, `semantic_memory`, `relationship_memory` +
`relationship_history`, `agent_self_state`, `compression_log`) → [`database.md`](database.md).
`GET /agents/{name}/memory`가 `db.get_full_memory(sim_id, agent_key)`로 4개를 묶어 반환.

### 2차 기억 정리(consolidation) — 한 번뿐이면 옅어진다 (강화는 임시 제거)

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
      - 한 번만 언급되고 안 뒷받침된 사소한 사실 → 확신 하향(쇠퇴)
      - 사실 문장 자체는 안 바꾼다 — 이번엔 확신도만 재평가
    유효한 id만(존재하지 않는 id는 무시) db.update_fact_confidence(id, conf)
      — conf는 [0,1]로 클램프. 현재 값보다 큰 conf(상향)는 반영하지 않는다
```

**행을 지우거나 병합하지 않는다** — 점수(confidence)만 바꾸고, 이미 있는 표시
상한(`_fact_lines`, 상위 `_FACT_SHOW_MAX`개)이 낮아진 확신을 보고 자연히
걸러내게 둔다. 그래서 되돌릴 수 있고(다음 정리 때 다시 오를 수 있음), LLM이
실수로 판단을 잘못해도 원문(raw messages 아카이브)이나 다른 사실을 잃어버릴
위험이 없다 — 사람의 기억 응고(consolidation)가 세부를 지우는 게 아니라
덜 중요한 것을 옅어지게만 하는 것과 같은 원칙.

**중복 강화 제거 (2026-10-01, 1단계 — 임시 조치).** 예전엔 "서로 다른 표현이지만
반복/뒷받침되는 사실은 확신 상향(강화)"도 했다. 그런데 `db.upsert_facts`
(`ABM/db/semantic.py`)는 문자열이 **완전히 같을 때만** 병합하므로, 표현만 바뀐 같은
이야기("괴물"→"블랙홀"→"은하계")가 별개 행으로 쌓이고, 2차 정리는 그 중복 행들을
"서로 뒷받침하는 독립 증거"로 오인해 전부 끌어올렸다 — 반복된 주제가 기억에서 점점
"중요한 사실"로 격상되는 자기강화 루프(실측: case1 v11에서 "햄버거"가 전체 wave의
48%). 그래서 `_CONSOLIDATION_PROMPT`에서 강화 규칙을 지우고, 모델이 습관적으로 상향
조정을 내도 `consolidate_facts`가 반영하지 않게 했다(쇠퇴는 그대로). 같은 단계에서
1차 압축 프롬프트(`_COMPRESSION_PROMPT`)에 **결과 중심 요약** 지시를 추가했다 —
사건 문장에 요구·행동·반응·변화·미해결(지키지 않은 약속 포함)을 우선 담고, "앞으로
포기한다" 같은 행동 방침은 지어내지 않는다. 이건 반복 행동의 원인이 입증됐다는 뜻이
아니며, 실제 영향은 비교 실험으로 검증해야 한다. 강화를 영구히 포기한 것도 아니다 —
설계가 더 필요한 **2~4단계는 보류 중**이다: (2) 기억 ID·원문 근거 연결 + 제한된 후보
검색, (3) 원본을 보존하는 대표 기억 통합, (4) 실제 사건을 구분할 수 있을 때 반복
횟수·발생 시각 집계. 중복과 실제 반복을 구분할 수 있게 되면 "진짜 반복" 강화를 다시
넣는다. 설계 문서: `_workspace/2026-10-01_memory_mechanism_proposal*.md`.

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
| `build_move_to_hint` | 그래프 없음 → "이동할 위치 이름" / 있음 → "장소명 또는 사람 ID(추격/랑데부)" + "지금 있는 곳 = 머묾" / zone 있음 → + "다른 방 사람은 ID로 지목" + "먼 곳은 안내된 길, 경유지에 깨어 있는 사람이 있으면 멈춤 — 같은 목적지로 재출발" | — |

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
| `has_zone` | `[구역:`, `경유지` (세계 계약 `_MAP_RULE_JOURNEY` — 옛 프리즈 출력 템플릿에도 들어간다) |
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
