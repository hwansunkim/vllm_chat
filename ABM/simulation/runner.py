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
    ):
        """Wave-based BFS + 시나리오 이벤트 실행."""
        events_by_wave: dict[int, list] = {}
        for e in (events or []):
            w = e.get("wave", 0) if isinstance(e, dict) else 0
            events_by_wave.setdefault(w, []).append(e)

        current_wave: dict[str, list] = resume_wave if resume_wave else {start_agent: []}
        turn_counter  = 0
        total_turns   = 0
        silence_count = 0

        for wave_num in range(max_waves):
            if self._stop_event.is_set():
                break

            for event in events_by_wave.get(wave_num, []):
                ev_result = self._execute_event(event)
                entrant   = ev_result.get("entrant")
                if entrant and entrant not in current_wave:
                    current_wave[entrant] = []

            if not current_wave:
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
                break

            turn_counter += len(current_wave)
            total_turns  += len(current_wave)
            self.completed_waves = wave_num + 1

            # ── 이동·외모 처리 ────────────────────────────────────────────────
            scene_injections: dict[str, list] = {}

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

            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                update_appearance = result.get("update_appearance")
                if update_appearance:
                    self._agent_visual[speaker_key] = update_appearance
                    display = self._key_to_alias.get(speaker_key, speaker_key)
                    self._emit("appearance_update", {
                        "wave": wave_num, "agent": speaker_key,
                        "display_name": display,
                        "description":  update_appearance,
                    })
                    my_loc = self._agent_location.get(speaker_key, "")
                    for other_key in self.active_agents:
                        if other_key == speaker_key:
                            continue
                        other_loc = self._agent_location.get(other_key, "")
                        if my_loc and other_loc and my_loc != other_loc:
                            continue
                        scene_msg = f"[씬] {display}의 외모가 변했다: {update_appearance}"
                        scene_injections.setdefault(other_key, []).append({
                            "speaker": "씬", "content": scene_msg, "action_note": ""
                        })

            # ── next_wave 구성 ────────────────────────────────────────────────
            next_wave: dict[str, list] = {}
            for agent_key, msgs in scene_injections.items():
                if agent_key in self.active_agents:
                    next_wave.setdefault(agent_key, []).extend(msgs)

            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                for target_key in self._resolve_targets(result["targets"], speaker_key):
                    if target_key not in next_wave:
                        next_wave[target_key] = []
                    next_wave[target_key].append({
                        "speaker":     speaker_key,
                        "content":     result["clean_content"],
                        "action_note": result.get("action_note", ""),
                    })

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
                # else: time_per_wave=0, early_stop=ON → 즉시 종료 (원래 동작)
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

