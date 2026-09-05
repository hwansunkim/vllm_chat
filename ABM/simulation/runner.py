import random
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


class _RunnerMixin:
    """시뮬레이션 실행 루프, 웨이브 요약, system 에이전트."""

    def run(
        self,
        start_agent:       str,
        max_waves:         int         = 10,
        step_delay:        float       = 1.0,
        events:            list        = None,
        resume_wave:       dict | None = None,
        max_silence_waves: int         = 3,
        early_stop_enabled: bool       = True,
        target_duration_minutes: int | None = None,
    ):
        """Wave-based BFS + 시나리오 이벤트 실행.

        ``target_duration_minutes``가 주어지면 시뮬레이션 내 경과 시간이 그 값에
        도달하는 시점에서도 정상 종료한다. ``max_waves``는 그대로 상한(안전장치)으로
        남으며, 둘 중 먼저 도달하는 조건에서 멈춘다.
        """
        events_by_wave: dict[int, list] = {}
        for e in (events or []):
            w = e.get("wave", 0) if isinstance(e, dict) else 0
            events_by_wave.setdefault(w, []).append(e)

        current_wave: dict[str, list] = resume_wave if resume_wave else {start_agent: []}
        turn_counter  = 0
        total_turns   = 0
        silence_count = 0

        # ── 목표 기간(선택) ──────────────────────────────────────────────────
        # 시간 개념이 꺼져 있으면(fixed 모드 + time_per_wave=0) 목표 기간은 계산할
        # 기준 자체가 없으므로 조용히 무시한다 — 에러가 아니라 "사용 안 함".
        # variable 모드는 time_per_wave와 무관하게 경과 시간을 누적하므로 항상 유효.
        time_enabled   = self._time_mode == "variable" or self._time_per_wave > 0
        target_minutes = int(target_duration_minutes or 0)
        if target_minutes > 0 and not time_enabled:
            logger.info(
                f"목표 기간({target_minutes}분)이 설정됐으나 시간 개념이 비활성"
                f"(time_mode=fixed, time_per_wave=0)이라 무시합니다."
            )
            target_minutes = 0
        # 목표 기간은 '이번 run() 호출 이후' 경과분 기준이다. max_waves가 실행마다
        # 새로 주어지는 예산인 것과 동일한 성격 — resume/continue도 목표 기간만큼
        # 더 진행한다(누적 경과가 이미 목표를 넘었다고 즉시 멈추지 않는다).
        # `_elapsed_minutes`는 두 모드 모두 '이전 run들의 누적'이므로 이게 곧
        # 이번 run 시작 시점의 총 경과다(fixed는 wave 0 = 아직 0분 진행).
        elapsed_baseline = self._elapsed_minutes
        end_reason = "max_waves"

        for run_wave in range(max_waves):
            # per-run 카운터(run_wave)는 시간/감염/목표기간 계산 전용이다.
            # emit·영속화(피드 뱃지, DB wave 컬럼, 요약 구간)에는 이전 run들의 누적을
            # 더한 disp_wave를 쓴다 — /continue·/resume 후에도 wave 번호가 이어지도록.
            disp_wave = self._wave_base + run_wave
            if self._stop_event.is_set():
                end_reason = "stopped"
                break

            for event in events_by_wave.get(run_wave, []):
                ev_result = self._execute_event(event)
                entrant   = ev_result.get("entrant")
                if entrant and entrant not in current_wave:
                    current_wave[entrant] = []

            if not current_wave:
                # "silence"는 직전 루프에서 이미 원인을 표시해뒀다(침묵 조기종료).
                # 그 외(예: 초기 시나리오에 에이전트가 아예 없는 경우)에만 no_agents.
                if end_reason != "silence":
                    end_reason = "no_agents"
                break

            # ── system 에이전트 (디렉터) — wave **시작** 시점 ────────────────────
            # 예전엔 루프 **끝**에서 돌며 다음 wave의 current_wave에 개입을 꽂았다.
            # 그 배치의 문제: emit의 `wave` 값은 방금 끝난 wave인데 개입은 다음
            # wave에서 소비되고, 디렉터가 참조하는 시각도 한 wave 어긋났다
            # ("6:45인데 벽시계가 8시를 친다"류 환각의 직접 원인).
            # 이제 이번 wave의 current_wave에 바로 주입하므로 emit의 wave, 반응
            # wave, 표시 시각이 셋 다 일치한다.
            # 배치는 `if not current_wave` 가드 **뒤**다 — 앞이면 디렉터의 개입이
            # 빈 wave를 되살려 침묵 조기종료(early_stop)를 무력화한다.
            # 판정식도 `(wave_num+1) % interval` → `wave_num % interval`로 바뀌지만
            # 실제로 개입이 꽂히는 wave 번호는 예전과 동일하다(wave 0만 스킵).
            if self._sys_enabled and disp_wave > 0 and not self._stop_event.is_set():
                if disp_wave % self._sys_interval == 0:
                    logger.info(f"[W{disp_wave}] system 에이전트 호출 시작, current_wave={list(current_wave.keys())}")
                    try:
                        current_wave = self._run_system_agent(disp_wave, current_wave)
                        logger.info(f"[W{disp_wave}] system 에이전트 완료, current_wave={list(current_wave.keys())}")
                    except Exception as e:
                        logger.error(f"[W{disp_wave}] system 에이전트 예외: {e}", exc_info=True)

            self._emit("wave_start", {
                "wave":   disp_wave,
                "agents": list(current_wave.keys()),
            })

            results: dict[str, dict] = {}
            with ThreadPoolExecutor(max_workers=len(current_wave)) as executor:
                future_map = {
                    executor.submit(
                        self._step_agent, agent_key, run_wave, disp_wave,
                        turn_counter + i, incoming,
                    ): agent_key
                    for i, (agent_key, incoming) in enumerate(current_wave.items())
                }
                for future in as_completed(future_map):
                    agent_key = future_map[future]
                    try:
                        results[agent_key] = future.result()
                    except Exception as e:
                        logger.error(f"Wave {disp_wave} agent {agent_key} 예외: {e}")
                        results[agent_key] = {"success": False, "agent_key": agent_key}
                    if self._stop_event.is_set():
                        break

            if self._stop_event.is_set():
                end_reason = "stopped"
                break

            turn_counter += len(current_wave)
            total_turns  += len(current_wave)
            self.completed_waves = run_wave + 1

            # ── 발화 라우팅 (이동 적용 *전* 위치 스냅샷 기준) ──────────────────
            # turn.py의 1차 _resolve_targets() 해석(엣지/피드/DB 기록)과 반드시 같은
            # 스냅샷을 써야 한다. 예전엔 이 블록이 이동 처리 뒤에 있어서, 같은 턴에
            # 발화하면서 move_to로 떠난 에이전트의 말이 "그래프엔 있는데 상대는 못 받는"
            # 유령 발화가 됐고, 그 타깃이 유일했다면 next_wave가 통째로 비어 시뮬레이션이
            # 즉시 침묵 종료됐다. 의미론: "말은 떠나기 전에 했으므로 그 자리에 있던
            # 사람은 듣는다."
            #
            # `perception_mode == "spatial"`이면 여기에 더해 엿듣기·원거리 전달·
            # 독백 행동 관찰이 붙는다(_route_spatial). 그 판정도 **같은 이동 전
            # 스냅샷** 위에서 일어나야 하므로 반드시 이 자리다.
            #
            # 씬 주입 버퍼는 원래 아래 외모·이동 블록에서 만들었지만, 독백 행동
            # 브로드캐스트가 같은 채널을 쓰므로 라우팅보다 앞으로 옮겼다. 빈 dict
            # 초기화라 targeted 모드에서는 동작이 완전히 동일하다.
            scene_injections: dict[str, list] = {}

            routed: dict[str, list] = {}
            spatial = self._perception_mode == "spatial"
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                resolved = self._resolve_targets(result["targets"], speaker_key)
                if not spatial:
                    # ── targeted (기본) — 기존 코드 그대로 ──
                    for target_key in resolved:
                        routed.setdefault(target_key, []).append({
                            "speaker":     speaker_key,
                            "content":     result["clean_content"],
                            "action_note": result.get("action_note", ""),
                        })
                    continue
                self._route_spatial(
                    speaker_key, result, resolved, routed, scene_injections,
                )

            # ── 외모·이동 처리 ────────────────────────────────────────────────

            # ── 외모 변경 (이동 적용 *전* 위치 스냅샷 기준) ────────────────────
            # 발화 라우팅과 같은 원칙이다: "옷은 떠나기 전에 갈아입었으므로 그 자리에
            # 있던 사람이 본다." 이 블록이 예전처럼 이동 처리 **뒤에** 있으면 두 가지가
            # 동시에 깨진다.
            #   1) my_loc이 이동 *후* 위치가 되어, 실제 목격자(출발지 동석자)는 알림을
            #      못 받고 그 자리에 없던 도착지 사람이 대신 받는다. 출발지 사람에겐
            #      나중에 다시 만났을 때 아무 설명 없이 외모만 바뀌어 보인다.
            #   2) 아래 이동 루프의 mover_visual이 아직 갱신되지 않은 **옛** 외모를 읽어
            #      도착 알림("낯선 이가 나타났다: 검은 코트")과 뒤이은 외모 알림
            #      ("...빨간 코트")이 서로 모순되는 두 줄로 도착지에 함께 꽂힌다.
            # 따라서 _agent_visual 갱신 자체가 반드시 이동 루프보다 먼저 일어나야 한다.
            # 순서를 옮기면 도착지 사람은 외모 알림을 받지 않는데, 이는 정상이다 —
            # 그들은 도착 알림에서 이미 갱신된 새 외모를 본다.
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                update_appearance = result.get("update_appearance")
                if not update_appearance:
                    continue
                self._agent_visual[speaker_key] = update_appearance
                display = self._key_to_alias.get(speaker_key, speaker_key)
                self._emit("appearance_update", {
                    "wave": disp_wave, "agent": speaker_key,
                    "display_name": display,
                    "description":  update_appearance,
                })
                my_loc = self._agent_location.get(speaker_key, "")
                if my_loc in self._exterior_locations:
                    # 외부 공간은 완전 격리 — 아무도 그를 볼 수 없으므로 씬 브로드캐스트
                    # 자체를 하지 않는다 (_resolve_targets의 "외부 화자는 전달 불가"와
                    # 대칭). _agent_visual 갱신과 emit은 그대로 두어, 내부로 돌아왔을 때
                    # 새 외모가 보이도록 한다.
                    continue
                for other_key in self.active_agents:
                    if other_key == speaker_key:
                        continue
                    other_loc = self._agent_location.get(other_key, "")
                    if other_loc in self._exterior_locations:
                        continue  # 외부 공간의 에이전트에게는 씬 메시지 전달 안 함
                    if my_loc and other_loc and my_loc != other_loc:
                        continue
                    scene_injections.setdefault(other_key, []).append({
                        "speaker": "씬",
                        "content": self._appearance_scene_msg(
                            speaker_key, other_key, display, update_appearance
                        ),
                        "action_note": "",
                    })

            # ── 이동 의도 해석 (이동 적용 *전* 위치 스냅샷 기준) ────────────────
            # 1) 이번 wave의 move_to를 장소 이동 / "사람을 만나러 간다"로 분류
            # 2) 살아 있는 만남 의도를 실제 경로로 환산 (추격 / 랑데부 / 집결)
            # 두 단계 모두 아래 이동 루프가 돌기 **전**, 즉 발화 라우팅·외모 처리와
            # 같은 스냅샷 위에서 계산돼야 한다 — 이동 후 위치로 목표를 잡으면 이번
            # wave에 이미 한 칸 움직인 결과를 근거로 다음 목표를 정하게 되어, 서로
            # 다가가는 두 사람의 랑데부 지점이 매 wave 흔들린다.
            # 스냅샷은 두 단계 **앞**에서 뜬다. _apply_move_intents가 새 lock을
            # 세우고(start) 어떤 lock은 그 자리에서 취소하므로(new_move_to/staying),
            # _update_meeting_paths 직전에 뜨면 그 변화들이 이미 스냅샷에 녹아
            # meeting_update가 영영 안 나간다. diff 기준은 "지난 wave 종료 시점"이다.
            meeting_before = dict(self._meeting_intent)
            self._apply_move_intents(results)
            self._update_meeting_paths(scene_injections)
            self._emit_meeting_updates(disp_wave, meeting_before)

            for agent_key in list(self.active_agents):
                path = self._agent_path.get(agent_key)
                if not path:
                    continue
                next_loc = path.pop(0)
                if not path:
                    self._agent_path.pop(agent_key, None)
                old_loc = self._agent_location.get(agent_key, "")
                if old_loc == next_loc:
                    continue
                self._agent_location[agent_key] = next_loc
                display          = self._key_to_alias.get(agent_key, agent_key)
                to_exterior      = next_loc in self._exterior_locations
                from_exterior    = old_loc  in self._exterior_locations
                self._emit("agent_move", {
                    "wave": disp_wave, "agent": agent_key,
                    "display_name": display,
                    "from": old_loc, "to": next_loc,
                    "to_exterior": to_exterior,
                })
                mover_visual = self._agent_visual.get(agent_key, "") or display
                for other_key in self.active_agents:
                    if other_key == agent_key:
                        continue
                    other_loc = self._agent_location.get(other_key, "")
                    if other_loc in self._exterior_locations:
                        continue  # 외부 공간의 에이전트에게는 씬 메시지 전달 안 함
                    if to_exterior:
                        # 내부에서 외부로 나갔을 때 — 출발지 사람들에게만 알림
                        if other_loc == old_loc and not from_exterior:
                            if agent_key in self._agent_knowledge.get(other_key, set()):
                                scene_msg = f"[씬] {display}이(가) 자리를 떠났다."
                            else:
                                scene_msg = "[씬] 낯선 이가 자리를 떠났다."
                            scene_injections.setdefault(other_key, []).append({
                                "speaker": "씬", "content": scene_msg, "action_note": ""
                            })
                    else:
                        # 일반 이동 (내부 → 내부, 외부 → 내부) — 도착지 사람들에게 알림
                        if other_loc == next_loc:
                            if agent_key in self._agent_knowledge.get(other_key, set()):
                                scene_msg = f"[씬] {display}이(가) 이곳에 도착했다."
                            else:
                                scene_msg = (
                                    f"[씬] 낯선 이가 나타났다: {mover_visual}"
                                    if mover_visual else "[씬] 낯선 이가 나타났다."
                                )
                            scene_injections.setdefault(other_key, []).append({
                                "speaker": "씬", "content": scene_msg, "action_note": ""
                            })
                        elif other_loc == old_loc and old_loc:
                            # 출발지에 남은 사람들에게도 이탈을 알린다. 이게 없으면
                            # 남은 쪽 memory의 마지막 대화가 여전히 "진행 중"이라
                            # 떠난 상대에게 계속 말을 거는 무성 발화가 반복된다.
                            if agent_key in self._agent_knowledge.get(other_key, set()):
                                scene_msg = f"[씬] {display}이(가) 자리를 떠났다."
                            else:
                                scene_msg = "[씬] 낯선 이가 자리를 떠났다."
                            scene_injections.setdefault(other_key, []).append({
                                "speaker": "씬", "content": scene_msg, "action_note": ""
                            })

            # ── 감염 모델 ────────────────────────────────────────────────────
            # 이동이 모두 반영된 뒤의 위치를 기준으로 접촉을 계산한다 — "이번 wave가
            # 끝난 시점에 같은 장소에 함께 있었는가". 모델이 꺼져 있으면 즉시 반환한다.
            # (외모 변경 처리 자체는 위(124~174행)에서 이동 *전* 스냅샷 기준으로 이미
            # 끝났다 — 예전엔 여기 이동 이후에 중복으로 처리했었는데, 그 버전은 위치
            # 스냅샷이 틀리고 이름 노출·외부공간 격리도 안 됐던 구버전이라 제거했다.)
            self._apply_infection_wave(run_wave, disp_wave)

            # ── next_wave 구성 ────────────────────────────────────────────────
            # 조립 자체는 이동이 끝난 뒤에 한다(도착/이탈 씬 메시지가 필요하므로).
            # "누가 무엇을 듣는지" 판정만 위에서 이동 전 스냅샷으로 이미 끝났다.
            next_wave: dict[str, list] = {}
            for agent_key, msgs in scene_injections.items():
                if agent_key in self.active_agents:
                    next_wave.setdefault(agent_key, []).extend(msgs)

            for agent_key, msgs in routed.items():
                if agent_key in self.active_agents:
                    next_wave.setdefault(agent_key, []).extend(msgs)

            # ── 조기 종료 / 시간 주도형 루프 ─────────────────────────────────────
            organically_filled = bool(next_wave)
            forced_silence_reinject = False

            if not next_wave:
                if not early_stop_enabled:
                    # 조기 종료 OFF: 항상 모든 active 에이전트 재투입 (max_waves까지 실행)
                    next_wave = {key: [] for key in self.active_agents}
                elif self._time_per_wave > 0 or self._time_mode == "variable":
                    # 시간 주도형 + 조기 종료 ON: max_silence_waves 초과 시 종료
                    silence_count += 1
                    forced_silence_reinject = True
                    logger.info(f"[W{disp_wave}] 침묵 #{silence_count}/{max_silence_waves}")
                    if silence_count < max_silence_waves:
                        next_wave = {key: [] for key in self.active_agents}
                    else:
                        # next_wave가 빈 채로 남아 다음 루프 선두의 `if not current_wave:`
                        # 가드에 걸리는데, 그 가드는 무조건 "no_agents"를 붙인다. 침묵으로
                        # 멈춘 것을 여기서 먼저 표시해 그 덮어쓰기를 막는다.
                        end_reason = "silence"
                else:
                    # time_per_wave=0, early_stop=ON → 즉시 종료 (원래 동작)
                    end_reason = "silence"
            elif next_wave:
                silence_count = 0

            # ── 시간 누적 (가변 모드) ─────────────────────────────────────────
            # organically_filled(라우팅으로 next_wave가 자연스럽게 채워짐)가 아니어도,
            # 이번 wave에 성공한 발화가 있었다면(예: early_stop_enabled=False로 강제
            # 전원 재투입됐지만 에이전트들이 서로를 타겟하지 않고 각자 행동하는 경우)
            # 진짜 침묵이 아니므로 LLM 분류 대상에 포함시킨다. forced_silence_reinject
            # (정말 아무도 응답하지 않아 강제 재투입된 경우)만 결정적 idle 스케줄을 쓴다.
            if self._time_mode == "variable":
                has_content = any(r.get("success") for r in results.values())
                if forced_silence_reinject:
                    idx = min(silence_count, len(self._idle_minutes_schedule)) - 1
                    self._elapsed_minutes += self._idle_minutes_schedule[idx]
                elif organically_filled or has_content:
                    # raw_jump(이번 wave의 경과 분)를 정하는 방식만 모드별로 갈린다.
                    # 이후의 _clamp_time_jump()는 모드 무관 공통 경로다 — 그 함수는
                    # 카테고리가 아니라 최종 분 숫자만 보고 동작한다.
                    # 아래 세 값이 곧 `time_jump` 이벤트의 판정 근거다. category
                    # 경로(직접/폴백)를 탔을 때만 category_id 가 채워지고, ai 성공
                    # 시에만 ai_reason 이 채워진다.
                    category_id:   str | None = None
                    ai_reason:     str | None = None
                    used_fallback: bool       = False
                    if self._time_estimation_mode == "ai":
                        ai_result = self._estimate_wave_minutes(disp_wave, results)
                        if ai_result is None:
                            # AI 추론 실패 — 카테고리 모드(normal_scene)로 조용히 폴백.
                            used_fallback = True
                            category_id = "normal_scene"
                            raw_jump    = self._random_minutes_for_category(category_id)
                            jump_source = "ai→normal_scene 폴백"
                            logger.info(
                                f"[W{disp_wave}] 시간 추론 모드=ai 실패 — "
                                f"normal_scene 카테고리로 폴백, {raw_jump}분"
                            )
                        else:
                            raw_jump, ai_reason = ai_result
                            jump_source = "ai"
                            logger.info(f"[W{disp_wave}] 시간 추론 모드=ai — {raw_jump}분")
                    else:
                        category_id = self._classify_wave_time(disp_wave, results)
                        raw_jump    = self._random_minutes_for_category(category_id)
                        jump_source = category_id
                    jump, clamp_reason = self._clamp_time_jump(raw_jump, results)
                    if clamp_reason:
                        logger.info(f"[W{disp_wave}] 시간 점프 클램프({jump_source}): {clamp_reason}")
                    # 판정 결과를 관전용 텔레메트리로 노출한다(director_call 과 같은
                    # 성격 — 대사가 아니고 어떤 에이전트 메모리에도 들어가지 않는다).
                    # 사용자가 카테고리 라벨/범위를 미세조정하려면 "이번 wave 가 어느
                    # 카테고리로 판정됐는지"를 화면에서 볼 수 있어야 한다.
                    resolved_cat = (
                        self._resolve_time_category(category_id)
                        if category_id is not None else None
                    ) or {}
                    # 이 wave 의 **종료 시각**(delta 적용 후 절대 시각). 아래
                    # `self._elapsed_minutes += jump` 가 emit *다음*에 실행되므로
                    # 여기서는 아직 이번 wave 가 반영되지 않은 `_elapsed_minutes`
                    # 에 `jump` 를 직접 더해야 한다. CSV 내보내기가 "다음 wave 의
                    # 시작 시각 훔쳐보기" 대신 이 값을 쓰면 마지막 wave 도 종료
                    # 시각이 채워진다(다음 wave 가 없어도 됨).
                    end_time_str = self._format_time_str(
                        self._sim_start_minutes + self._elapsed_minutes + jump
                    )
                    self._emit("time_jump", {
                        "wave":           disp_wave,
                        "mode":           self._time_estimation_mode,
                        "used_fallback":  used_fallback,
                        "category_id":    resolved_cat.get("id"),
                        "category_label": resolved_cat.get("label") or None,
                        "reason":         ai_reason or None,
                        "raw_minutes":    raw_jump,
                        "minutes":        jump,
                        "clamp_reason":   clamp_reason,
                        "end_time_str":   end_time_str,
                    })
                    self._elapsed_minutes += jump
                # else: 이번 wave에 성공한 발화가 전혀 없음 — 시간 미누적

            current_wave = next_wave
            logger.info(f"[W{disp_wave}] next_wave: {list(current_wave.keys())}")

            # ── 목표 기간 도달 체크 ───────────────────────────────────────────
            # 침묵 조기종료(early_stop_enabled/max_silence_waves)와 독립적으로,
            # 이번 wave까지의 경과 시간이 목표에 도달하면 정상 종료한다.
            # 경과 시간 기준은 에이전트에게 보여지는 시각 계산(step.py) 및 감염
            # 진행과 동일한 `_current_elapsed_minutes`로 단일화한다 — 모드별로
            # 따로 계산하면 한쪽만 고칠 때 시계와 종료 조건이 어긋난다.
            # baseline을 빼므로 값은 "이번 run() 이후 경과"다: variable은 누적분의
            # 증가량, fixed는 (wave_num + 1) * time_per_wave (이전 누적은 상쇄).
            if target_minutes > 0:
                elapsed_since_start = (
                    self._current_elapsed_minutes(run_wave + 1) - elapsed_baseline
                )
                if elapsed_since_start >= target_minutes:
                    logger.info(
                        f"[W{disp_wave}] 목표 기간 도달 — 경과 {elapsed_since_start}분 "
                        f">= 목표 {target_minutes}분, 정상 종료"
                    )
                    end_reason = "target_duration"
                    break

            if current_wave and not self._stop_event.is_set():
                elapsed  = 0.0
                interval = 0.1
                while elapsed < step_delay and not self._stop_event.is_set():
                    time.sleep(interval)
                    elapsed += interval

        self._pending_wave = current_wave
        self._save_edges()

        self._emit("simulation_end", {
            "total_turns": total_turns,
            "edges_count": len(self.edges),
            "log_count":   len(self.shared_log),
            # 종료 사유 (추가 필드 — 모르는 소비자는 무시해도 기존과 동일하게 동작):
            # "max_waves" | "target_duration" | "silence" | "no_agents" | "stopped"
            "end_reason":  end_reason,
        })

    # ── 공간 기반 인지 라우팅 (perception_mode == "spatial") ────────────────────

    def _route_spatial(
        self,
        speaker_key:      str,
        result:           dict,
        resolved:         list[str],
        routed:           dict[str, list],
        scene_injections: dict[str, list],
    ) -> None:
        """한 화자의 발화를 공간 규칙에 따라 배달한다 (spatial 모드 전용).

        ``perception_mode == "targeted"``에서는 **호출조차 되지 않는다** —
        run()의 라우팅 블록이 모드 분기로 통째로 건너뛴다.

        화자의 **이동 전** 위치를 기준으로 네 갈래로 나뉜다:

        1. 같은 방 + 직접 타깃 — 대사+행동. 기존과 완전히 동일한 딕셔너리.
        2. 같은 zone 다른 방 + 직접 타깃 — **대사만**. 벽 너머로 목소리는 들려도
           뭘 하는지는 안 보이므로 `action_note`를 지우고, `speaker`에
           `"…, 멀리서"`를 넣어 `_inject_incoming`의 `[{speaker}] {content}`
           포매팅을 그대로 재사용한다(step.py는 손대지 않는다).
        3. 같은 방 + 제3자(엿듣기) — 대사+행동을 `[화자→대상들]` 태그로. 같은
           공간이면 못 들을 이유가 없으므로 **그룹 필터를 적용하지 않는다**
           (그룹은 "누구를 타깃할 수 있는가"의 서사적 장치이고, 엿듣기는 물리적
           사실이다). 태그는 관찰자 시점이라 bystander마다 다르다.
        4. 같은 방 + 독백(target=self/system) — 대사는 안 들리고 **행동만** 씬
           채널로 보인다.

        같은 zone 다른 방의 제3자에겐 아무것도 가지 않는다 — zone 인지
        (`_compute_zone_awareness`)는 "저기 누가 있다"까지이고 대화 내용은 새지
        않는다. 화자/수신자가 외부 공간(exterior)이면 전부 차단된다.
        """
        content = result.get("clean_content", "") or ""
        action  = (result.get("action_note", "") or "").strip()

        # (1)(2) 직접 타깃 — 같은 방 / 원거리 포맷 분기
        for target_key in resolved:
            if self._is_remote_target(speaker_key, target_key):
                routed.setdefault(target_key, []).append({
                    "speaker":     f"{speaker_key}, 멀리서",
                    "content":     content,
                    "action_note": "",     # 벽 너머 — 행동은 보이지 않는다
                })
            else:
                routed.setdefault(target_key, []).append({
                    "speaker":     speaker_key,
                    "content":     content,
                    "action_note": action,
                })

        speaker_loc = self._agent_location.get(speaker_key, "")
        if speaker_loc in self._exterior_locations:
            # 외부 공간 화자는 아무도 보거나 들을 수 없다(_resolve_targets의 가드와
            # 대칭). resolved도 이미 비어 있다.
            return

        # (3)(4) 같은 방의 제3자. **정확히 같은 방**만 본다 — 원거리 제3자는 제외.
        # 위치 미설정(레거시)은 `_same_loc`/`_reachable`과 같은 하위 호환 규칙을
        # 쓴다 — 화자나 상대 어느 한쪽이라도 위치가 비어 있으면 "같은 방"으로 본다.
        # 정확 일치만 썼다면 위치를 일부만 지정한 혼합 시나리오에서 "말은 걸 수
        # 있는데(all) 엿듣지는 못하는" 비대칭이 생긴다 — `_reachable`은 위치 미설정
        # 상대를 항상 통과시키지만 이 목록은 그렇지 않았기 때문. exterior는 위치가
        # 비어 있을 수 없는 실제 장소명이라 이 완화와 무관하게 항상 제외한다.
        def _co_located(k: str) -> bool:
            other = self._agent_location.get(k, "")
            if other in self._exterior_locations:
                return False
            return (not speaker_loc) or (not other) or (other == speaker_loc)

        resolved_set = set(resolved)
        bystanders = [
            k for k in sorted(self.active_agents)
            if k != speaker_key
            and k not in resolved_set
            and _co_located(k)
        ]
        if not bystanders:
            return

        if not self._is_monologue_targets(result.get("targets")) and content.strip():
            # 소리 내어 말한 턴 — 부른 상대가 자리에 없어 resolved가 비었더라도
            # 같은 방 사람은 듣는다. 태그는 관찰자별로 계산한다(캐시 금지).
            for bystander in bystanders:
                routed.setdefault(bystander, []).append({
                    "speaker":     self._eavesdrop_tag(
                        bystander, speaker_key, result.get("targets"),
                    ),
                    "content":     content,
                    "action_note": action,
                })
        elif action:
            # 혼잣말이거나 대사가 비었지만 몸으로 한 일이 있는 턴 — 행동만 보인다.
            display = self._key_to_alias.get(speaker_key, speaker_key)
            for bystander in bystanders:
                scene_injections.setdefault(bystander, []).append({
                    "speaker":     "씬",
                    "content":     self._action_scene_msg(
                        speaker_key, bystander, display, action,
                    ),
                    "action_note": "",
                })

    def _classify_wave_time(self, wave_num: int, results: dict) -> str:
        """이번 wave의 발화 결과를 LLM으로 분류해 시간 경과 카테고리 id를 반환.

        절대 예외를 밖으로 던지지 않음 — 실패 시 "normal_scene"으로 폴백.
        """
        try:
            from ..time_classifier import classify_wave_time

            entries = [
                {
                    "speaker":     speaker_key,
                    "content":     result.get("clean_content", ""),
                    "action_note": result.get("action_note", ""),
                }
                for speaker_key, result in results.items()
                if result.get("success")
            ]
            # Layer 0 — 분류기에 현재 시각을 준다. "이 시각 이후 다른 구성원이
            # 귀가·등장하거나 함께 모이는 장면을 건너뛸 수 있으니 큰 카테고리는
            # 확실히 한적/야간일 때만" 이라는 판단을 LLM이 하도록.
            now_str = self._format_time_str(self._sim_start_minutes + self._elapsed_minutes)
            category_id = classify_wave_time(
                entries, self._time_categories, self._llm,
                key_to_alias=self._key_to_alias,
                llm_max_tokens=min(self.llm_max_tokens, 256),
                current_time=now_str,
            )
            valid_ids = {c["id"] for c in self._time_categories}
            if category_id is None or category_id not in valid_ids:
                logger.warning(f"[W{wave_num}] 시간 분류 실패/알수없는 카테고리({category_id!r}) — normal_scene으로 폴백")
                return "normal_scene"
            return category_id
        except Exception as e:
            logger.warning(f"[W{wave_num}] 시간 분류 예외 — normal_scene으로 폴백: {e}")
            return "normal_scene"

    def _resolve_time_category(self, category_id: str | None) -> dict | None:
        """category id → 실제로 사용될 카테고리 dict.

        알 수 없는 id면 ``normal_scene``, 그것도 없으면 첫 카테고리로 폴백한다
        (``_random_minutes_for_category``의 원래 폴백 규칙 그대로). 카테고리가
        하나도 설정되지 않았으면 ``None``.

        `time_jump` 이벤트가 "실제로 쓰인" 카테고리의 id/label을 싣기 위해 분리했다
        — 폴백이 걸렸을 때 요청된 id를 그대로 보여주면 사용자가 라벨/범위를
        미세조정할 때 엉뚱한 카테고리를 고치게 된다.
        """
        cats = self._time_categories or []
        if not cats:
            return None
        return (
            next((c for c in cats if c["id"] == category_id), None)
            or next((c for c in cats if c["id"] == "normal_scene"), cats[0])
        )

    def _random_minutes_for_category(self, category_id: str | None) -> int:
        """카테고리 id의 min~max 범위에서 경과 분을 뽑는다 (카테고리 모드의 원래 로직).

        알 수 없는 id면 ``normal_scene``, 그것도 없으면 첫 카테고리로 폴백한다.
        """
        cat = self._resolve_time_category(category_id)
        if cat is None:
            # 카테고리가 하나도 없는 설정. 시간을 정할 근거가 없으니 0분으로 본다
            # (시뮬레이션 자체는 계속 돌아야 한다).
            return 0
        lo, hi = cat["min_minutes"], cat["max_minutes"]
        if lo > hi:
            lo, hi = hi, lo
        return random.randint(lo, hi)

    def _estimate_wave_minutes(self, wave_num: int, results: dict) -> tuple[int, str] | None:
        """AI 모드 — LLM에게 이번 wave의 경과 분을 직접 추론시킨다.

        sanity 범위는 두 모드가 같은 설정을 공유하도록 ``_time_categories`` 전체의
        min(min_minutes) ~ max(max_minutes)를 쓴다.

        반환: ``(minutes, reason)`` — reason은 LLM이 준 한 줄 이유(빈 문자열일 수
        있음)로, 호출부가 `time_jump` 이벤트에 실어 사용자에게 보여준다.
        절대 예외를 밖으로 던지지 않음 — 실패 시 ``None``(호출부가 카테고리 폴백).
        """
        try:
            from ..time_classifier import estimate_wave_minutes

            entries = [
                {
                    "speaker":     speaker_key,
                    "content":     result.get("clean_content", ""),
                    "action_note": result.get("action_note", ""),
                }
                for speaker_key, result in results.items()
                if result.get("success")
            ]
            cats = self._time_categories or []
            lo = min((int(c["min_minutes"]) for c in cats), default=1)
            hi = max((int(c["max_minutes"]) for c in cats), default=480)
            now_str = self._format_time_str(self._sim_start_minutes + self._elapsed_minutes)
            return estimate_wave_minutes(
                entries, self._llm,
                key_to_alias=self._key_to_alias,
                llm_max_tokens=min(self.llm_max_tokens, 256),
                current_time=now_str,
                lo=lo,
                hi=hi,
            )
        except Exception as e:
            logger.warning(f"[W{wave_num}] AI 시간 추론 예외 — 카테고리 폴백: {e}")
            return None

    def _clamp_time_jump(self, raw_jump: int, results: dict) -> tuple[int, str | None]:
        """가변 시간 점프(분)를 벽시계·동석 상황 기준으로 결정론적으로 캡한다.

        LLM 분류기는 '장면의 질감'만 정하고, 실제 경과 분의 상한은 여기서 엔진이
        강제한다 — 약한 모델이 오후 한복판에서 최대 범위(예: 480분)를 골라 학원·
        퇴근·저녁 식사 같은 재집결 장면을 통째로 건너뛰는 것을 막는다.

        반환: ``(clamped_jump, 사유_문자열 or None)``. 사유가 None이면 캡 미적용.
        """
        # 이번 wave에 실제 내용 있는 발화를 한 에이전트들의 현재(이동 반영 후) 위치.
        speaker_locs = [
            self._agent_location.get(k, "")
            for k, r in results.items()
            if r.get("success") and (r.get("clean_content") or "").strip()
        ]
        interior_locs = [
            loc for loc in speaker_locs
            if loc and loc not in self._exterior_locations
        ]

        # (1) 실내 한 곳에 2명 이상이 함께 발화 중 = 진행 중인 장면. 강하게 캡.
        scene_cap = self._max_scene_jump_minutes
        if scene_cap > 0 and len(interior_locs) != len(set(interior_locs)):
            if raw_jump > scene_cap:
                return scene_cap, f"동석 장면(실내 2인+) {raw_jump}→{scene_cap}분"

        # (2) 밤(22~06시)이 아니고 집에 남아 있는 사람이 있으면 주간 상한 적용.
        #     모두 외부(회사·학교·학원)로 나가 집이 완전히 빈 낮은 캡하지 않는다
        #     — 그때는 건너뛸 재집결 장면 자체가 없다.
        daytime_cap = self._max_daytime_jump_minutes
        now_hour = ((self._sim_start_minutes + self._elapsed_minutes) % 1440) // 60
        is_night = now_hour >= 22 or now_hour < 6
        if daytime_cap > 0 and not is_night and interior_locs:
            if raw_jump > daytime_cap:
                return daytime_cap, f"주간·재실자 있음 {raw_jump}→{daytime_cap}분"

        return raw_jump, None

