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
        elapsed_baseline = self._elapsed_minutes
        end_reason = "max_waves"

        for wave_num in range(max_waves):
            if self._stop_event.is_set():
                end_reason = "stopped"
                break

            for event in events_by_wave.get(wave_num, []):
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

            self._emit("wave_start", {
                "wave":   wave_num,
                "agents": list(current_wave.keys()),
            })

            results: dict[str, dict] = {}
            with ThreadPoolExecutor(max_workers=len(current_wave)) as executor:
                future_map = {
                    executor.submit(
                        self._step_agent, agent_key, wave_num, turn_counter + i, incoming
                    ): agent_key
                    for i, (agent_key, incoming) in enumerate(current_wave.items())
                }
                for future in as_completed(future_map):
                    agent_key = future_map[future]
                    try:
                        results[agent_key] = future.result()
                    except Exception as e:
                        logger.error(f"Wave {wave_num} agent {agent_key} 예외: {e}")
                        results[agent_key] = {"success": False, "agent_key": agent_key}
                    if self._stop_event.is_set():
                        break

            if self._stop_event.is_set():
                end_reason = "stopped"
                break

            turn_counter += len(current_wave)
            total_turns  += len(current_wave)
            self.completed_waves = wave_num + 1

            # ── 발화 라우팅 (이동 적용 *전* 위치 스냅샷 기준) ──────────────────
            # turn.py의 1차 _resolve_targets() 해석(엣지/피드/DB 기록)과 반드시 같은
            # 스냅샷을 써야 한다. 예전엔 이 블록이 이동 처리 뒤에 있어서, 같은 턴에
            # 발화하면서 move_to로 떠난 에이전트의 말이 "그래프엔 있는데 상대는 못 받는"
            # 유령 발화가 됐고, 그 타깃이 유일했다면 next_wave가 통째로 비어 시뮬레이션이
            # 즉시 침묵 종료됐다. 의미론: "말은 떠나기 전에 했으므로 그 자리에 있던
            # 사람은 듣는다."
            routed: dict[str, list] = {}
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                for target_key in self._resolve_targets(result["targets"], speaker_key):
                    routed.setdefault(target_key, []).append({
                        "speaker":     speaker_key,
                        "content":     result["clean_content"],
                        "action_note": result.get("action_note", ""),
                    })

            # ── 외모·이동 처리 ────────────────────────────────────────────────
            scene_injections: dict[str, list] = {}

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
                    "wave": wave_num, "agent": speaker_key,
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

            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                move_to_raw = result.get("move_to")
                if move_to_raw and isinstance(move_to_raw, str):
                    dest = move_to_raw.strip()
                    if dest:
                        current_loc = self._agent_location.get(speaker_key, "")
                        if dest != current_loc:
                            self._agent_path[speaker_key] = self._find_path(current_loc, dest)

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
                    "wave": wave_num, "agent": agent_key,
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
                    logger.info(f"[W{wave_num}] 침묵 #{silence_count}/{max_silence_waves}")
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
                    category_id = self._classify_wave_time(wave_num, results)
                    cat = next((c for c in self._time_categories if c["id"] == category_id), None) \
                          or next((c for c in self._time_categories if c["id"] == "normal_scene"), self._time_categories[0])
                    lo, hi = cat["min_minutes"], cat["max_minutes"]
                    if lo > hi:
                        lo, hi = hi, lo
                    self._elapsed_minutes += random.randint(lo, hi)
                # else: 이번 wave에 성공한 발화가 전혀 없음 — 시간 미누적

            current_wave = next_wave
            logger.info(f"[W{wave_num}] next_wave: {list(current_wave.keys())}")

            if self._summary_interval > 0 and not self._stop_event.is_set():
                waves_since = wave_num - self._last_summarized_wave
                if waves_since >= self._summary_interval:
                    logger.info(f"[W{wave_num}] 요약 에이전트 호출 시작")
                    try:
                        self._run_wave_summary(self._last_summarized_wave + 1, wave_num)
                        self._last_summarized_wave = wave_num
                        logger.info(f"[W{wave_num}] 요약 에이전트 완료")
                    except Exception as e:
                        logger.error(f"[W{wave_num}] 요약 에이전트 예외: {e}", exc_info=True)

            if self._sys_enabled and not self._stop_event.is_set():
                if (wave_num + 1) % self._sys_interval == 0:
                    logger.info(f"[W{wave_num}] system 에이전트 호출 시작, current_wave={list(current_wave.keys())}")
                    try:
                        current_wave = self._run_system_agent(wave_num, current_wave)
                        logger.info(f"[W{wave_num}] system 에이전트 완료, current_wave={list(current_wave.keys())}")
                    except Exception as e:
                        logger.error(f"[W{wave_num}] system 에이전트 예외: {e}", exc_info=True)

            # ── 목표 기간 도달 체크 ───────────────────────────────────────────
            # 침묵 조기종료(early_stop_enabled/max_silence_waves)와 독립적으로,
            # 이번 wave까지의 경과 시간이 목표에 도달하면 정상 종료한다.
            # 경과 시간 기준은 에이전트에게 보여지는 시각 계산(step.py)과 동일하게 둔다:
            #   - variable: 누적된 self._elapsed_minutes
            #   - fixed:    (wave_num + 1) * time_per_wave  (결정론적)
            if target_minutes > 0:
                if self._time_mode == "variable":
                    elapsed_since_start = self._elapsed_minutes - elapsed_baseline
                else:
                    elapsed_since_start = (wave_num + 1) * self._time_per_wave
                if elapsed_since_start >= target_minutes:
                    logger.info(
                        f"[W{wave_num}] 목표 기간 도달 — 경과 {elapsed_since_start}분 "
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
            category_id = classify_wave_time(
                entries, self._time_categories, self._llm,
                key_to_alias=self._key_to_alias,
                llm_max_tokens=min(self.llm_max_tokens, 256),
            )
            valid_ids = {c["id"] for c in self._time_categories}
            if category_id is None or category_id not in valid_ids:
                logger.warning(f"[W{wave_num}] 시간 분류 실패/알수없는 카테고리({category_id!r}) — normal_scene으로 폴백")
                return "normal_scene"
            return category_id
        except Exception as e:
            logger.warning(f"[W{wave_num}] 시간 분류 예외 — normal_scene으로 폴백: {e}")
            return "normal_scene"

    def _run_wave_summary(self, wave_start: int, wave_end: int) -> None:
        """shared_log에서 해당 웨이브 구간 엔트리를 추출해 LLM 요약 후 이벤트를 emit."""
        if self._stop_event.is_set():
            return
        from ..summarizer import summarize_waves
        entries = [
            e for e in self.shared_log
            if isinstance(e.get("wave"), int)
            and wave_start <= e["wave"] <= wave_end
        ]
        bg_text = ""
        if self.background_log:
            first   = self.background_log[0].get("content", "")
            bg_text = first.removeprefix("[배경]").strip()

        result = summarize_waves(
            entries        = entries,
            background     = bg_text,
            wave_start     = wave_start,
            wave_end       = wave_end,
            llm            = self._llm,
            key_to_alias   = self._key_to_alias,
            llm_max_tokens = self.llm_max_tokens,
        )
        if result:
            self._last_summary = result
            self._emit("wave_summary", {
                "wave_start": wave_start,
                "wave_end":   wave_end,
                "summary":    result.get("summary", ""),
                "key_events": result.get("key_events", []),
                "mood":       result.get("mood", ""),
            })

