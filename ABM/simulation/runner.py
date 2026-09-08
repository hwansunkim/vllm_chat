import random
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ._constants import _WEEKDAY_KEYS

logger = logging.getLogger(__name__)


def _parse_hhmm(at_time: str) -> int | None:
    """`"HH:MM"` → 자정 기준 분(0~1439). 형식이 틀리면 None."""
    try:
        hh, mm = str(at_time).strip().split(":")
        v = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None
    return v if 0 <= v < 1440 else None


def _beat_occurrence_after(
    hhmm: int, days: set[int] | None, after_elapsed: int,
    sim_start_minutes: int, start_weekday_idx: int,
) -> int | None:
    """시작 기준 경과분으로 표현한 `at_time` 이벤트의 다음 도래 시점.

    `after_elapsed` **보다 큰** 경과분 중 벽시계가 `hhmm`이고(요일 필터 `days`가
    있으면 그 요일인) 가장 이른 값. `days=None` 이면 매일. 14일치 시뮬레이션을
    커버하도록 최대 16일 앞까지 훑고, 못 찾으면 None.
    """
    base_wall = sim_start_minutes + after_elapsed
    base_day  = base_wall // 1440
    for day_off in range(0, 17):
        wall = (base_day + day_off) * 1440 + hhmm
        t = wall - sim_start_minutes
        if t <= after_elapsed:
            continue
        wd = (start_weekday_idx + wall // 1440) % 7
        if days is None or wd in days:
            return t
    return None


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
        target_duration_minutes: int | None = None,
    ):
        """Wave-based BFS + 시나리오 이벤트 실행.

        종료 조건: ``max_waves`` 도달 · ``target_duration_minutes`` 도달 · 활성
        에이전트 없음(``no_agents``) · 외부 중지(``stopped``) · **진행 불가**
        (``no_progress`` — 연속으로 성공한 발화가 하나도 없어 사실상 고장난 실행).

        대화가 시들해지는 것 자체로는 멈추지 않는다 — 시간을 크게 건너뛰고
        (``idle`` 시간 점프) 계속하며, 고립돼 혼잣말만 하는 에이전트는 밀집
        재투입에서 빠진다(휴면). 예전의 ``early_stop_enabled`` 플래그는 제거됐다:
        시간 개념이 있으면 "침묵"은 종료 신호가 아니라 "건너뛰기" 신호이고,
        시간 개념이 없는 순수 대화 시나리오는 더 이상 쓰이지 않는다.
        """
        # 이벤트 트리거는 wave 번호 또는 시계 시각(at_time) 둘 중 하나.
        # at_time 이벤트는 at_days 가 있으면 그 요일마다 반복 발동한다(하루 일과).
        events_by_wave: dict[int, list] = {}
        self._timed_events: list[dict] = []
        for e in (events or []):
            if not isinstance(e, dict):
                events_by_wave.setdefault(0, []).append(e)
                continue
            hhmm = _parse_hhmm(e.get("at_time", ""))
            if hhmm is not None:
                raw_days = [str(d).lower() for d in (e.get("at_days") or [])]
                days = {_WEEKDAY_KEYS.index(d) for d in raw_days
                        if d in _WEEKDAY_KEYS} or None
                te = {"hhmm": hhmm, "days": days, "event": e}
                # run 시작 시점보다 앞선 도래는 "이미 발동함"으로 본다(/continue·
                # /resume 로 넘겨 이어가는 경우 — 되돌릴 수 없다).
                te["next_at"] = _beat_occurrence_after(
                    hhmm, days, self._elapsed_minutes - 1,
                    self._sim_start_minutes, self._sim_start_weekday_idx,
                )
                self._timed_events.append(te)
            else:
                events_by_wave.setdefault(e.get("wave", 0), []).append(e)

        current_wave: dict[str, list] = resume_wave if resume_wave else {start_agent: []}
        turn_counter  = 0
        total_turns   = 0
        silence_count = 0
        # 연속으로 "성공한 발화가 0인" wave 수. 옛 early_stop 이 암묵적으로 하던
        # "LLM 이 전부 죽으면 max_waves 까지 두들기지 말고 멈춘다"를 대체하는
        # 명시적 백스톱. 짧은 말이라도 뱉으면(any success) 0 으로 리셋되므로
        # "대화가 시들해짐"이 아니라 "정말 아무것도 생성 못 함(고장)"만 잡는다.
        dead_waves = 0
        dead_wave_limit = max(6, max_silence_waves * 2)

        # ── 목표 기간(선택) ──────────────────────────────────────────────────
        # 시간 개념이 꺼져 있으면(fixed 모드 + time_per_wave=0) 목표 기간은 계산할
        # 기준 자체가 없으므로 조용히 무시한다 — 에러가 아니라 "사용 안 함".
        # variable 모드는 time_per_wave와 무관하게 경과 시간을 누적하므로 항상 유효.
        time_enabled   = self._time_mode == "variable" or self._time_per_wave > 0
        if self._timed_events and not time_enabled:
            logger.warning(
                f"at_time 이벤트 {len(self._timed_events)}건이 설정됐으나 시간 개념이 "
                f"비활성(time_mode=fixed, time_per_wave=0)이라 시계가 흐르지 않아 "
                f"영영 발동하지 않습니다."
            )
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

            wave_events = list(events_by_wave.get(run_wave, []))
            # 시계가 예정 시각에 도달한 at_time 이벤트도 이 wave에 발동한다.
            # 여러 도래를 건너뛴 점프는 한 번만(가장 최근 것) 발동하고 다음 도래를
            # 다시 계산한다 — 밀린 이벤트가 몰아치지 않는다.
            for te in self._timed_events:
                na = te["next_at"]
                if na is not None and self._elapsed_minutes >= na:
                    ev = dict(te["event"])
                    ev["wave"] = disp_wave     # infect_agent·피드 배치용
                    wave_events.append(ev)
                    logger.info(
                        f"[W{disp_wave}] at_time 이벤트 발동 "
                        f"({te['event'].get('at_time')}, 경과 {self._elapsed_minutes}분): "
                        f"{te['event'].get('type')}"
                    )
                    te["next_at"] = _beat_occurrence_after(
                        te["hhmm"], te["days"], self._elapsed_minutes,
                        self._sim_start_minutes, self._sim_start_weekday_idx,
                    )
            for event in wave_events:
                ev_result = self._execute_event(event)
                entrant   = ev_result.get("entrant")
                if entrant and entrant not in current_wave:
                    current_wave[entrant] = []

            if not current_wave:
                # 직전 루프가 종료 사유를 이미 세팅했으면(no_progress 등) 존중하고,
                # 아직 기본값이면 "활성 에이전트 없음"으로 본다 (초기 시나리오에
                # 에이전트가 없거나 전원 agent_exit 한 경우).
                if end_reason == "max_waves":
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
            # 전원 agent_exit 로 비워진 wave 를 되살려 no_agents 종료를 무력화한다.
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

            # as_completed()가 채운 `results`는 LLM 응답 지연에 따른 **완료 순서**라
            # 매 실행마다 다르다. 아래 라우팅·외모변경·이동 블록이 전부 이 dict를
            # 순회하므로, 여기서 키 순으로 한 번 정규화해 이벤트 emit 순서를
            # 결정론적으로 만든다(LLM 호출 자체의 병렬성은 그대로 유지).
            results = {k: results[k] for k in sorted(results)}

            turn_counter += len(current_wave)
            total_turns  += len(current_wave)
            self.completed_waves = run_wave + 1

            # 이번 wave **시작 시점**(이동 적용 전) 위치 스냅샷. 라우팅이 곧 이걸
            # 쓰고(지금 `_agent_location` 과 동일), 이번 wave 끝에서 다음 wave의
            # 1-wave 대화 유예 기준(`_prev_wave_start_location`)으로 넘긴다.
            wave_start_location = dict(self._agent_location)

            # ── 발화 라우팅 (이동 적용 *전* 위치 스냅샷 기준) ──────────────────
            # turn.py의 1차 _resolve_targets() 해석(엣지/피드/DB 기록)과 반드시 같은
            # 스냅샷을 써야 한다. 예전엔 이 블록이 이동 처리 뒤에 있어서, 같은 턴에
            # 발화하면서 move_to로 떠난 에이전트의 말이 "그래프엔 있는데 상대는 못 받는"
            # 유령 발화가 됐고, 그 타깃이 유일했다면 next_wave가 통째로 비어 시뮬레이션이
            # 즉시 침묵 종료됐다. 의미론: "말은 떠나기 전에 했으므로 그 자리에 있던
            # 사람은 듣는다."
            #
            # `perception_mode == "spatial"`이면 여기에 더해 엿듣기·독백 행동 관찰이
            # 붙는다(_route_spatial). 그 판정도 **같은 이동 전 스냅샷** 위에서
            # 일어나야 하므로 반드시 이 자리다.
            #
            # 씬 주입 버퍼는 원래 아래 외모·이동 블록에서 만들었지만, 독백 행동
            # 브로드캐스트가 같은 채널을 쓰므로 라우팅보다 앞으로 옮겼다. 빈 dict
            # 초기화라 targeted 모드에서는 동작이 완전히 동일하다.
            scene_injections: dict[str, list] = {}

            routed: dict[str, list] = {}
            # 이번 wave 에 발화한 에이전트가 실제로 누군가에게 말이 닿았는지
            # (순수 혼잣말이면 False). 아래 휴면(dormancy) 스트릭 갱신에 쓴다.
            reached_someone: dict[str, bool] = {}
            spatial = self._perception_mode == "spatial"
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                # _resolve_targets 는 "같은 방 + 1-wave 유예"까지 해석한다 —
                # 방금 자리를 뜬 상대에게 마지막 한마디가 닿는 경로.
                resolved = self._resolve_targets(result["targets"], speaker_key)
                reached_someone[speaker_key] = bool(resolved)
                for target_key in resolved:
                    routed.setdefault(target_key, []).append({
                        "speaker":     speaker_key,
                        "content":     result["clean_content"],
                        "action_note": result.get("action_note", ""),
                    })
                if spatial:
                    # 직접 배달 위에 얹히는 공간 효과(엿듣기·독백 행동 관찰)만.
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

            # ── 휴면(dormancy) 스트릭 갱신 ─────────────────────────────────────
            # 이동이 모두 반영된 뒤의 위치 기준으로, "이번 wave 에 발화했으나
            # 아무에게도 닿지 않았고(순수 혼잣말) 지금 같은 장소에 대화 상대도
            # 없는" 에이전트의 연속 카운트를 올린다. 누군가에게 닿았거나 곁에
            # 상대가 생기면 0으로 리셋(= 깨어남). 위치 미사용 레거시 시나리오는
            # `_has_reachable_partner` 가 항상 True 라 스트릭이 절대 쌓이지 않는다.
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                if reached_someone.get(speaker_key) or self._has_reachable_partner(speaker_key):
                    self._solo_streak[speaker_key] = 0
                else:
                    self._solo_streak[speaker_key] = self._solo_streak.get(speaker_key, 0) + 1

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

            # ── 침묵 처리 — 종료가 아니라 재투입/시간 점프 ────────────────────────
            organically_filled = bool(next_wave)
            forced_silence_reinject = False

            if not next_wave:
                # 이번 wave 에 아무도 서로를 target 하지 않았다. 종료하지 않는다:
                # max_waves 까지 계속 돈다. 단 **고립 독백을 max_silence_waves 회
                # 이상 연속한(휴면)** 에이전트는 밀집 재투입에서 뺀다 — 회사·학교로
                # 흩어져 서로 못 닿는 에이전트가 같은 독백을 무한 반복하는 것을
                # 막는다. 누군가 그에게 도달하면(이동·이벤트·디렉터 개입 →
                # routed/scene_injections) 위쪽 조립에서 이미 next_wave 에 들어가므로
                # 자동으로 깨어난다.
                dormant_cap = max(1, max_silence_waves)
                wakeable = sorted(
                    k for k in self.active_agents
                    if self._solo_streak.get(k, 0) < dormant_cap
                )
                if wakeable:
                    next_wave = {k: [] for k in wakeable}
                else:
                    # 전원 휴면 — 같은 독백을 재생하는 대신 시간을 크게 흘려보내고
                    # (가변 모드는 idle 스케줄) 전원을 한 번 깨운다. 새 시각을 보고
                    # 재회할지 각자 판단하게 한다. 이게 없으면 '모두 흩어진 하루'가
                    # 조각 점프로 max_waves 까지 갈린다.
                    silence_count += 1
                    forced_silence_reinject = True
                    logger.info(f"[W{disp_wave}] 전원 휴면 — 시간 점프 + 전원 재투입 #{silence_count}")
                    next_wave = {k: [] for k in sorted(self.active_agents)}
            else:
                silence_count = 0

            # ── 진행 불가 백스톱 ─────────────────────────────────────────────────
            # 이번 wave 에 성공한 발화가 하나라도 있었으면 리셋, 없으면 카운트.
            # 연속으로 아무 응답도 못 받으면(LLM 서버 다운, 전부 파싱 실패 등)
            # max_waves 까지 헛되이 두들기지 말고 멈춘다. 이건 옛 early_stop 이
            # 암묵적으로 하던 보호였다.
            if any(r.get("success") for r in results.values()):
                dead_waves = 0
            else:
                dead_waves += 1
                if dead_waves >= dead_wave_limit:
                    logger.warning(
                        f"[W{disp_wave}] 연속 {dead_waves} wave 동안 성공한 발화 0 — "
                        f"진행 불가로 종료(no_progress)"
                    )
                    end_reason = "no_progress"
                    current_wave = next_wave
                    break

            # ── 시간 누적 (가변 모드) ─────────────────────────────────────────
            # organically_filled(라우팅으로 next_wave가 자연스럽게 채워짐)가 아니어도,
            # 이번 wave에 성공한 발화가 있었다면(전원 재투입됐지만 에이전트들이 서로를
            # 타겟하지 않고 각자 행동하는 경우) 진짜 침묵이 아니므로 LLM 분류 대상에
            # 포함시킨다. forced_silence_reinject(전원 휴면 → 강제 재투입)만 결정적
            # idle 스케줄을 쓴다.
            if self._time_mode == "variable":
                has_content = any(r.get("success") for r in results.values())
                if forced_silence_reinject:
                    idx  = min(silence_count, len(self._idle_minutes_schedule)) - 1
                    jump = self._idle_minutes_schedule[idx]
                    # 관전 텔레메트리 — 전원 휴면 강제 재투입으로 시간이 크게 건너뛰는
                    # 것을 피드에서 볼 수 있게 한다. 예전엔 이 점프가 조용히 일어나
                    # "왜 갑자기 3시간이 지났지?"가 됐다.
                    self._emit("time_jump", {
                        "wave":           disp_wave,
                        "mode":           "idle",
                        "used_fallback":  False,
                        "category_id":    None,
                        "category_label": None,
                        "reason":         "전원 휴면(고립 독백)",
                        "raw_minutes":    jump,
                        "minutes":        jump,
                        "clamp_reason":   None,
                        "end_time_str":   self._format_time_str(
                            self._sim_start_minutes + self._elapsed_minutes + jump
                        ),
                    })
                    self._elapsed_minutes += jump
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

            # 다음 wave의 1-wave 대화 유예 기준. 이번 wave에 갈라선 상대에게
            # 다음 wave에 마지막 한마디가 닿게 한다("방금 자리를 뜬 사람에게 답").
            self._prev_wave_start_location = wave_start_location

            # ── 목표 기간 도달 체크 ───────────────────────────────────────────
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
            # "max_waves" | "target_duration" | "no_agents" | "no_progress" | "stopped"
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
        """한 화자의 발화의 **공간 부가 효과**를 배달한다 (spatial 모드 전용).

        직접 타깃(resolved) 배달 자체는 run()의 공통 라우팅 블록이 이미 끝냈다 —
        여기서는 그 위에 얹히는 것만 한다. ``perception_mode == "targeted"``에서는
        **호출조차 되지 않는다**.

        화자의 **이동 전** 위치를 기준으로:

        1. 같은 방 + 제3자(엿듣기) — 대사+행동을 `[화자→대상들]` 태그로. 같은
           공간이면 못 들을 이유가 없으므로 **인지 관계 필터를 적용하지 않는다**
           (관계는 "누구를 타깃할 수 있는가"의 서사적 장치이고, 엿듣기는 물리적
           사실이다). 태그는 관찰자 시점이라 bystander마다 다르다.
        2. 같은 방 + 독백(target=self/system) — 대사는 안 들리고 **행동만** 씬
           채널로 보인다.

        다른 방의 제3자에겐 아무것도 가지 않는다. 화자/수신자가 외부 공간
        (exterior)이면 전부 차단된다.
        """
        content = result.get("clean_content", "") or ""
        action  = (result.get("action_note", "") or "").strip()

        speaker_loc = self._agent_location.get(speaker_key, "")
        if speaker_loc in self._exterior_locations:
            # 외부 공간 화자는 아무도 보거나 들을 수 없다(_resolve_targets의 가드와
            # 대칭). resolved도 이미 비어 있다.
            return

        # (1)(2) 같은 방의 제3자. `_same_room`이 exterior 격리와 위치 미설정
        # (레거시 — 어느 한쪽이라도 비면 "같은 방") 하위 호환을 모두 처리한다.
        resolved_set = set(resolved)
        bystanders = [
            k for k in sorted(self.active_agents)
            if k != speaker_key
            and k not in resolved_set
            and self._same_room(speaker_key, k)
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
            beat = self._next_pending_beat()
            next_beat = ""
            if beat is not None:
                next_beat = f"{beat[1]} (지금부터 {beat[0] - self._elapsed_minutes}분 뒤)"
            category_id = classify_wave_time(
                entries, self._time_categories, self._llm,
                key_to_alias=self._key_to_alias,
                llm_max_tokens=min(self.llm_max_tokens, 256),
                current_time=now_str,
                placement=self._placement_summary(results),
                next_beat=next_beat,
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

    def _next_pending_beat(self) -> tuple[int, str] | None:
        """아직 발동하지 않은 at_time 이벤트 중 가장 이른 것 → ``(경과분, "HH:MM")``.

        시간 추론(카테고리/AI)과 `_clamp_time_jump`가 이 시각을 **넘겨** 점프하지
        않도록 하는 데 쓴다 — "짱구 태권도 16:00" 같은 예정 서사가 큰 시간 점프에
        통째로 스킵되는 것을 막는다. 없으면 None.
        """
        pending = [
            t for t in getattr(self, "_timed_events", [])
            if t["next_at"] is not None and t["next_at"] > self._elapsed_minutes
        ]
        if not pending:
            return None
        nxt = min(pending, key=lambda t: t["next_at"])
        return nxt["next_at"], str(nxt["event"].get("at_time", ""))

    def _placement_summary(self, results: dict) -> str:
        """이번 장면 화자들이 함께 있는지 흩어져 있는지 한 줄 요약 (시간 추론 프롬프트용).

        위치 미설정(레거시) 시나리오는 빈 문자열 → 프롬프트에서 블록 자체가 생략된다.
        """
        by_loc: dict[str, list[str]] = {}
        for k, r in results.items():
            if not (r.get("success") and (r.get("clean_content") or "").strip()):
                continue
            loc = self._agent_location.get(k, "")
            if not loc:
                continue
            by_loc.setdefault(loc, []).append(self._key_to_alias.get(k, k))
        if not by_loc:
            return ""
        if len(by_loc) == 1:
            loc, names = next(iter(by_loc.items()))
            if len(names) == 1:
                return f"{names[0]} 혼자 {loc}에 있음 (상호작용 없음 — 시간 압축 가능)"
            return f"{', '.join(names)} 모두 같은 곳({loc})에 함께 있음 (대화 중 — 압축 금지)"
        parts = [f"{loc} {len(names)}명" for loc, names in by_loc.items()]
        return f"여러 곳에 흩어져 있음 ({', '.join(parts)}) — 서로 상호작용 없으면 압축 가능"

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

            next_beat = ""
            beat = self._next_pending_beat()
            if beat is not None:
                beat_elapsed, beat_str = beat
                room = beat_elapsed - self._elapsed_minutes
                hi = min(hi, max(lo, room))
                next_beat = f"{beat_str} (지금부터 {room}분 뒤)"

            return estimate_wave_minutes(
                entries, self._llm,
                key_to_alias=self._key_to_alias,
                llm_max_tokens=min(self.llm_max_tokens, 256),
                current_time=now_str,
                lo=lo,
                hi=hi,
                placement=self._placement_summary(results),
                next_beat=next_beat,
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
        # (0) 예정된 at_time 이벤트를 넘기지 않는다 — 가장 강한 상한. 시간 추론
        #     프롬프트도 이 시각을 보지만(hi 캡 + next_beat), 카테고리 모드의 랜덤
        #     추출이나 AI 추론 실패 폴백은 프롬프트를 안 타므로 여기서 못 박는다.
        beat = self._next_pending_beat()
        if beat is not None:
            room = beat[0] - self._elapsed_minutes
            if room >= 0 and raw_jump > room:
                return room, f"예정 이벤트({beat[1]}) 전까지 {raw_jump}→{room}분"

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

