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


def _stamp_event(e, disp_wave: int, at_minutes: int):
    """시나리오 이벤트에 발동 wave·경과분을 새겨 넣는다.

    ``wave`` (disp_wave) 는 emit·영속화용 — 안 넣으면 `_emit` 이 0 으로 기록해
    이벤트가 전부 Wave 0 에 몰린다. ``at_minutes`` 는 이벤트 감염의 시각 앵커
    전용 — disp_wave 를 `_current_elapsed_minutes` 에 넘기면 재개 후 fixed
    모드에서 누적분을 두 번 세어 앵커가 미래로 밀린다. dict 가 아닌 이벤트
    (레거시)는 그대로 통과.
    """
    if not isinstance(e, dict):
        return e
    return {**e, "wave": disp_wave, "at_minutes": at_minutes}


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
                    hhmm, days, self._current_elapsed_minutes(0) - 1,
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
        # _clamp_time_jump가 다른 캡들과 함께 min()으로 참조할 수 있도록 절대
        # 경과분(마감 시각)으로 저장해둔다 — target_minutes(상대값)는 이 함수
        # 지역 변수라 그 밖에서 못 본다.
        self._target_deadline_elapsed = (
            elapsed_baseline + target_minutes if target_minutes > 0 else None
        )
        end_reason = "max_waves"

        # 이번 run() 호출 전체가 재사용하는 스레드풀. 예전엔 wave마다
        # `with ThreadPoolExecutor(...) as executor:`로 새로 만들어 매번 새 OS
        # 스레드를 띄웠는데, `ABM/db/conn.py`가 스레드마다 sqlite 커넥션을 캐싱해두고
        # 절대 닫지 않는 구조라 그 새 스레드들이 죽어도 커넥션(=파일 디스크립터)이
        # 조용히 새고 있었다. 수백~수천 wave가 누적되면 결국 프로세스의 FD 한도를
        # 넘겨 "unable to open database file"/"Too many open files"로 죽는다
        # (에이전트 로그 json open()까지 실패할 정도로 — sqlite만의 문제가 아니라
        # 프로세스 전체 FD 고갈). 에이전트 로스터는 run() 내내 고정이므로 그 크기를
        # 상한으로 풀 하나만 만들어 재사용한다 — 어떤 wave도 활성 에이전트 수보다
        # 많은 동시 작업을 submit하지 않는다.
        self._turn_executor = ThreadPoolExecutor(max_workers=max(1, len(self.agents)))

        for run_wave in range(max_waves):
            # per-run 카운터(run_wave)는 시간/감염/목표기간 계산 전용이다.
            # emit·영속화(피드 뱃지, DB wave 컬럼, 요약 구간)에는 이전 run들의 누적을
            # 더한 disp_wave를 쓴다 — /continue·/resume 후에도 wave 번호가 이어지도록.
            disp_wave = self._wave_base + run_wave
            if self._stop_event.is_set():
                end_reason = "stopped"
                break

            # 이번 wave **시작 시점**의 실제 경과 분(벽시계). variable 모드는
            # `_elapsed_minutes`(누적)와 같고, fixed 모드는
            # `_elapsed_minutes + run_wave*time_per_wave` 로 wave 마다 흐른다.
            # `_elapsed_minutes` 는 fixed 모드에서 run 내내 고정이라, at_time
            # 이벤트 판정·이벤트 감염 시각 앵커에 직접 쓰면 시계가 안 흐른다.
            now_elapsed = self._current_elapsed_minutes(run_wave)

            # 이벤트에 발동 wave(disp_wave)·경과분(now_elapsed)을 새긴다 — 자세한
            # 이유는 _stamp_event docstring.
            wave_events = [_stamp_event(e, disp_wave, now_elapsed)
                           for e in events_by_wave.get(run_wave, [])]
            # 시계가 예정 시각에 도달한 at_time 이벤트도 이 wave에 발동한다.
            # 여러 도래를 건너뛴 점프는 한 번만(가장 최근 것) 발동하고 다음 도래를
            # 다시 계산한다 — 밀린 이벤트가 몰아치지 않는다.
            for te in self._timed_events:
                na = te["next_at"]
                if na is not None and now_elapsed >= na:
                    wave_events.append(_stamp_event(te["event"], disp_wave, now_elapsed))
                    logger.info(
                        f"[W{disp_wave}] at_time 이벤트 발동 "
                        f"({te['event'].get('at_time')}, 경과 {now_elapsed}분): "
                        f"{te['event'].get('type')}"
                    )
                    te["next_at"] = _beat_occurrence_after(
                        te["hhmm"], te["days"], now_elapsed,
                        self._sim_start_minutes, self._sim_start_weekday_idx,
                    )
            for event in wave_events:
                ev_result = self._execute_event(event)
                entrant   = ev_result.get("entrant")
                if entrant and entrant not in current_wave:
                    current_wave[entrant] = []
                # system_message로 알림을 받은 사람은 이번 wave에 발화 후보로
                # 강제 편입한다 — 안 그러면 "16:30. 학원 갈 시간이다" 가
                # memory에는 들어갔지만 current_wave(지난 wave 라우팅으로 이미
                # 확정됨)에 없어서, 자연히 다시 초대될 때까지 반응이 미뤄진다.
                # 이미 라우팅으로 받은 incoming이 있으면 그대로 유지(setdefault).
                for notified_key in ev_result.get("notified", []):
                    current_wave.setdefault(notified_key, [])

            # 퇴장(agent_exit)이 이번 wave 참가자를 비활성으로 만들었으면 이번 wave
            # 발화 목록에서도 뺀다 — active_agents/_pending_wave 에서만 지우고
            # current_wave 를 그대로 두면 이미 나간 인물이 이 wave 에 계속 발화해
            # 로그·다른 인물 맥락에 남는다. 전원 퇴장이면 아래 no_agents 로 종료.
            current_wave = {k: v for k, v in current_wave.items()
                            if k in self.active_agents}

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

            # ── 이동 중 강제 편입 차단 — 공통 관문 ────────────────────────────
            # 방금 위 두 블록(예약 이벤트의 entrant/notified, 디렉터 개입)은 둘 다
            # traveling 체크 없이 current_wave에 직접 꽂는다 — next_wave 구성
            # 시점의 hold 처리(아래, scene_injections/routed → next_wave)로는 못
            # 막는 구멍이었다(외부 리뷰 Finding 1/6: 이동 중인데 "18:00 학원
            # 끝났다" 알림이 그대로 꽂혀, 그 wave에 아직 도착 안 한 집에서 활동
            # 하는 서사가 실제로 나옴 — W82 재현). 새 주입 경로가 또 생겨도 이
            # 한 곳만 지키면 되도록, 실제 LLM 호출(turn_executor.submit) 바로
            # 직전에 한 번 더 공통으로 거른다. 메시지 자체는 이미
            # `_execute_event`/디렉터가 각자 memory에 직접 적어뒀으므로(이 필터와
            # 무관하게 항상 전달됨), 여기서 막는 건 오직 "이번 wave에 턴을 받아
            # 즉시 반응하는 것"뿐이다 — 도착 후 자연스러운 계기(직접 부름·휴면
            # 재투입 등)에 반응한다.
            travelers = {k for k in current_wave if self._agent_traveling(k, now_elapsed)}
            if travelers and len(travelers) < len(current_wave):
                for key in travelers:
                    self._hold_incoming(key, current_wave.pop(key))
            elif travelers:
                # 활성 에이전트 전원이 하필 이 순간 동시에 이동 중인 극단적
                # 경우 — current_wave를 완전히 비우면 no_agents 오판정
                # (`if not current_wave` 가드는 이미 위에서 지나간 뒤라 여기선
                # 안 걸리지만, 다음 wave 조립이 빈 채로 시작해 no_progress
                # 오탐지로 이어질 수 있다). 이 희귀 케이스는 옛 동작(그대로
                # 진행)으로 폴백한다 — 완벽히 막기보다 진행 불가를 피한다.
                logger.warning(
                    f"[W{disp_wave}] 활성 에이전트 전원이 이동 중 — traveling "
                    f"필터를 건너뜀(전체 보류 시 진행 불가 위험): {sorted(travelers)}"
                )

            self._emit("wave_start", {
                "wave":   disp_wave,
                "agents": list(current_wave.keys()),
            })

            # 이번 wave 에 실제 incoming(누가 나에게 한 말/씬)을 받은 에이전트.
            # 빈 리스트로 재투입된 경우는 제외 — 휴면 스트릭 리셋 판정에 쓴다.
            got_incoming = {k for k, inc in current_wave.items() if inc}

            results: dict[str, dict] = {}
            # 풀 자체는 위에서 run() 전체용으로 한 번만 만든 걸 재사용한다(wave마다
            # 새로 만들지 않음 — 이유는 위 주석).
            future_map = {
                self._turn_executor.submit(
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

            # 이번 wave 시작 시점에 자연 해제된 상태(수면·이동 등) 정리. traveling이
            # 막 풀린 사람의 "도착했다" 알림은 이동 시작 시점이 아니라 **지금** 그
            # 자리 사람들을 기준으로 새로 만든다(status.py 모듈 docstring 참고) —
            # 그래서 라우팅보다 앞, wave_start_location 스냅샷 직후에 처리한다.
            # 같은 만료 배치에서 본인이 이동 중 놓친 메시지(held_incoming)도
            # 함께 돌려준다 — 도착 알림(남에게)과 밀린 메시지(본인에게)는 방향이
            # 반대라 서로 다른 dict에 쌓이지만, 최종적으로 next_wave에 합쳐지는
            # 경로는 같다.
            expired_statuses = self._expire_agent_states(now_elapsed)
            for other_key, msgs in self._deferred_arrival_scene_injections(expired_statuses).items():
                scene_injections.setdefault(other_key, []).extend(msgs)
            for key, msgs in self._release_held_incoming(expired_statuses).items():
                scene_injections.setdefault(key, []).extend(msgs)
            # 상태 해제도 진입과 대칭으로 관전 텔레메트리를 남긴다 — 이게 없으면
            # (리뷰에서 지적된 대로) "언제 잠들어서 언제 깼는지"를 Markdown
            # 내보내기·DB sim_events 어디서도 감사할 수 없었다.
            for key, st in expired_statuses.items():
                self._emit("agent_status_change", {
                    "wave":         disp_wave,
                    "agent":        key,
                    "display_name": self._key_to_alias.get(key, key),
                    "action":       "clear",
                    "state":        st.get("state"),
                })

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

            # ── 상태 선언 처리 (enter_state, 이동 적용과 무관) ──────────────────
            # 자기-선언형 상태(수면·개인 용무 등)는 "지금부터"라 이동/외모 처리
            # 같은 스냅샷 시점 제약이 없다 — 이번 wave 시작 시점(now_elapsed)
            # 기준으로 바로 건다. zone-crossing 이동(traveling)은 에이전트가
            # 선택하지 않으므로 여기서 다루지 않고, 아래 이동 루프에서 엔진이
            # 자동으로 건다.
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                category_id = result.get("enter_state")
                if not category_id:
                    # enter_state가 없다 = "이 상태를 유지하지 않겠다"는 뜻으로
                    # 본다. 지금 자기-선언형(수면·개인 용무) 상태 중이었다면
                    # 그 자리에서 바로 해제한다 — 사용자 설계 결정: 잠긴
                    # 에이전트가 턴을 받는 경로는 직접 지목·예약 이벤트·디렉터
                    # 개입뿐이라(순수 휴면 재투입에서는 이미 제외됨), 턴을
                    # 받았다는 것 자체가 이미 "누군가/뭔가 개입했다"는 뜻이고,
                    # 그때 상태를 유지할지는 그 순간의 판단에 맡기는 게 자연
                    # 스럽다(예: 엄마가 깨우면 실제로 일어나 세수하러 감).
                    # traveling은 에이전트가 선택하지 않는 상태라 이 판단과
                    # 무관하다(원래도 여기서 안 건드림). 애초에 상태가 없던
                    # 평범한 에이전트(가장 흔한 경우)는 조용히 넘어간다.
                    st = self._agent_active_status(speaker_key, now_elapsed)
                    if st is not None and st.get("state") != "traveling":
                        del self._agent_status[speaker_key]
                        logger.info(
                            f"[W{disp_wave}] {speaker_key} 상태 해제(재선언 없음): "
                            f"{st.get('state')}"
                        )
                        self._emit("agent_status_change", {
                            "wave":         disp_wave,
                            "agent":        speaker_key,
                            "display_name": self._key_to_alias.get(speaker_key, speaker_key),
                            "action":       "clear",
                            "state":        st.get("state"),
                        })
                    continue
                minutes = self._enter_state(speaker_key, now_elapsed, category_id=category_id)
                if minutes is not None:
                    st    = self._agent_status.get(speaker_key, {})
                    state = st.get("state", category_id)
                    cat   = self._resolve_state_category(state)
                    logger.info(f"[W{disp_wave}] {speaker_key} 상태 진입: {state} ({minutes}분)")
                    self._emit("agent_status_change", {
                        "wave":         disp_wave,
                        "agent":        speaker_key,
                        "display_name": self._key_to_alias.get(speaker_key, speaker_key),
                        "action":       "enter",
                        "state":        state,
                        "label":        (cat or {}).get("label"),
                        "minutes":      minutes,
                        "until_time_str": self._format_time_str(
                            self._sim_start_minutes + now_elapsed + minutes
                        ),
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
                # zone 경계를 건너는 hop인가 — 어느 방에서 출발했든 구역을 나가는
                # 쪽은 무조건 1홉이라(location.py::_expand_zone_edges "구역 안
                # 어디서든 1홉 탈출") raw 위치가 이 hop 하나로 바로 바뀐다. 지리적
                # 사실이라 에이전트가 아니라 엔진이 판단한다(0이면 기능 꺼짐).
                # 외부 노드는 zone이 없어(zone='') 내부 zone과 자연히 구분된다.
                crossing_zone = (
                    self._zone_travel_max_minutes > 0
                    and self._location_zone.get(old_loc, "") != self._location_zone.get(next_loc, "")
                )
                if crossing_zone:
                    lo, hi = self._zone_travel_min_minutes, self._zone_travel_max_minutes
                    travel_minutes = random.randint(lo, hi) if lo < hi else lo
                    self._enter_state(
                        agent_key, now_elapsed, minutes=travel_minutes, state="traveling",
                        arrival_location=next_loc, pending_arrival_announcement=True,
                    )
                    self._emit("agent_status_change", {
                        "wave":         disp_wave,
                        "agent":        agent_key,
                        "display_name": display,
                        "action":       "enter",
                        "state":        "traveling",
                        "label":        f"{next_loc}(으)로 이동 중",
                        "minutes":      travel_minutes,
                        "until_time_str": self._format_time_str(
                            self._sim_start_minutes + now_elapsed + travel_minutes
                        ),
                    })
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
                            if crossing_zone:
                                # zone 경계를 건넜다 — 실제 도착 알림은 이동 시간이
                                # 다 찬 뒤에야 낸다(status.py
                                # _deferred_arrival_scene_injections). 여기서 바로
                                # 내면 raw 위치만 바뀐 시점에 "도착했다"가 나가버려
                                # 아직 이동 중인데 말을 걸 수 있는 것처럼 보인다.
                                continue
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
            # "이번 wave 에 발화했으나 실제 소통이 없었던(순수 혼잣말)" 에이전트의
            # 연속 카운트를 올린다. 리셋(= 깨어남) 조건 중 하나라도:
            #   - 내가 누군가에게 말이 닿았다 (reached_someone)
            #   - 내가 이번 wave 에 누군가의 말/씬을 받았다 (got_incoming)
            #   - 내 곁에 상대가 있다 (_has_reachable_partner) — 단 **위치를 쓰는**
            #     시나리오라면 **이번 wave 에 방 어딘가에서 대화가 오갔을 때만**
            #     (any_reached). 곁에 사람이 있어도 아무도 서로 말을 안 걸면 스트릭이
            #     쌓인다 — 잠든 부부가 같은 방에 있다는 이유로 영영 안 깨어 max_waves
            #     까지 45분씩 갈리던 버그. 순수 독백 wave 만 해당하므로 실제 대화가
            #     오가는 장면(식사·수다)에는 영향이 없다.
            # 위치 미사용(레거시) 시나리오는 `_has_reachable_partner` 가 항상 True 라
            # 예전처럼 스트릭이 절대 쌓이지 않는다.
            any_reached = any(reached_someone.values())
            for speaker_key, result in results.items():
                if not result.get("success"):
                    continue
                partner = self._has_reachable_partner(speaker_key)
                if self._agent_location.get(speaker_key):
                    partner = partner and any_reached
                if (reached_someone.get(speaker_key)
                        or speaker_key in got_incoming or partner):
                    self._solo_streak[speaker_key] = 0
                else:
                    self._solo_streak[speaker_key] = self._solo_streak.get(speaker_key, 0) + 1

            # ── next_wave 구성 ────────────────────────────────────────────────
            # 조립 자체는 이동이 끝난 뒤에 한다(도착/이탈 씬 메시지가 필요하므로).
            # "누가 무엇을 듣는지" 판정만 위에서 이동 전 스냅샷으로 이미 끝났다.
            #
            # 대상이 **지금(이동 적용 후) 여전히 traveling**이면 여기서 한 번 더
            # 막는다 — `_same_room`/`_resolve_targets`의 traveling 가드는 "새로
            # 타깃을 해석할 때"만 적용되고, 이미 routed/scene_injections에 실려
            # 여기 도달한 메시지는 그 필터를 다시 통과하지 않는다. 그대로 두면
            # 이동 중인 사람이 다음 wave에 정상 턴을 받아 "이미 도착한 것처럼"
            # 서술할 수 있다(리뷰에서 코드 재현으로 확인된 구멍). 막는 대신
            # 버리지 않고 상태에 보관했다가(`_hold_incoming`) 실제 도착 시점에
            # 본인에게 돌려준다(`_release_held_incoming`, 위쪽 만료 처리 참고).
            next_wave: dict[str, list] = {}
            for agent_key, msgs in scene_injections.items():
                if agent_key not in self.active_agents:
                    continue
                if self._agent_traveling(agent_key, now_elapsed):
                    self._hold_incoming(agent_key, msgs)
                    continue
                next_wave.setdefault(agent_key, []).extend(msgs)

            for agent_key, msgs in routed.items():
                if agent_key not in self.active_agents:
                    continue
                if self._agent_traveling(agent_key, now_elapsed):
                    self._hold_incoming(agent_key, msgs)
                    continue
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
                # 상태(수면·이동 등) 중인 에이전트는 재투입 후보에서 뺀다 — 직접
                # 타깃팅(누군가 실제로 말을 걸어 routed/scene_injections로 들어오는
                # 경우)은 이 경로와 무관하므로 영향받지 않는다.
                dormant_cap = max(1, max_silence_waves)
                wakeable = sorted(
                    k for k in self.active_agents
                    if self._solo_streak.get(k, 0) < dormant_cap
                    and not self._agent_unavailable(k, now_elapsed)
                )
                if wakeable:
                    next_wave = {k: [] for k in wakeable}
                else:
                    # 전원 휴면 — 같은 독백을 재생하는 대신 시간을 크게 흘려보내고
                    # (가변 모드는 idle 스케줄) 다시 초대한다. 새 시각을 보고 재회할지
                    # 각자 판단하게 한다. 이게 없으면 '모두 흩어진 하루'가 조각 점프로
                    # max_waves 까지 갈린다.
                    #
                    # 예전엔 여기서 활성 에이전트 **전원**을 무조건 재투입했는데,
                    # 그중 상태(수면·이동 등)로 묶여 아직 해제 시점이 안 된 에이전트
                    # 까지 매번 강제로 턴을 받아 거의 똑같은 잠꼬대를 반복하는 게
                    # 실측으로 확인됐다(v11 실행 재현 — 신짱구/신짱아가 같은 밤
                    # 3~4번 연속으로 "드르렁... 슛... 골!!!"류를 재선언). 상태로
                    # 묶여 있다는 건 아직 시간이 덜 지났다는 뜻이라 지금 깨워도 얻을
                    # 게 없다 — 그중 **가장 먼저 풀리는 한 명만** 다시 초대해 그
                    # 행동으로 자연스럽게 이어지는지 지켜보고, 나머지는 각자의 해제
                    # 시점에 다음 wave 상단의 만료 처리(`_expire_agent_states`)로
                    # 자연히 합류하게 둔다. 상태 없이 순수하게 고립된(대화 상대가
                    # 없어 dormant_cap을 넘긴) 에이전트는 이 문제와 무관하므로
                    # 그대로 재투입한다 — state_categories 자체를 안 쓰는 시나리오는
                    # state_locked가 항상 빈 집합이라 기존 동작과 완전히 같다.
                    silence_count += 1
                    forced_silence_reinject = True
                    state_locked = {
                        k for k in self.active_agents
                        if self._agent_active_status(k, now_elapsed) is not None
                    }
                    reinject = sorted(self.active_agents - state_locked)
                    if state_locked:
                        wake_key = min(
                            state_locked,
                            key=lambda k: self._agent_active_status(k, now_elapsed)["until_elapsed"],
                        )
                        reinject.append(wake_key)
                        logger.info(
                            f"[W{disp_wave}] 전원 휴면(상태 {len(state_locked)}명 묶임) — "
                            f"시간 점프 + {wake_key} 우선 재투입 #{silence_count}"
                        )
                    else:
                        logger.info(f"[W{disp_wave}] 전원 휴면 — 시간 점프 + 전원 재투입 #{silence_count}")
                    next_wave = {k: [] for k in reinject}
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
                    raw_jump    = self._idle_minutes_schedule[idx]
                    jump_reason = "전원 휴면(고립 독백)"
                    # 활성 에이전트 전원이 상태(수면·이동 등)에 묶여 있다면, idle
                    # 스케줄의 랜덤값 대신 **가장 이른 상태 해제 시점까지 정확히**
                    # 점프한다 — 몇 분 뒤 깨어날지 이미 아는데 굳이 60/120/180분
                    # 임의 조각으로 나눠 깨울 이유가 없다(0번 클램프는 아래에서
                    # 그대로 적용된다).
                    state_wake_at = self._earliest_status_clear(
                        self.active_agents, self._elapsed_minutes,
                    )
                    if state_wake_at is not None:
                        raw_jump    = max(1, state_wake_at - self._elapsed_minutes)
                        jump_reason = "전원 상태(수면·이동 등) 해제 대기"
                    # 결정적 idle 점프도 예정 이벤트(at_time) 시각은 넘기지 않는다 —
                    # "가족이 각자 나가 있는 낮"에 15:00 하교·16:30 학원이 통째로
                    # 건너뛰어지던 버그. _clamp_time_jump 는 LLM 경로 전용이라 여기서
                    # 따로 못 박는다.
                    idle_clamp_reason: str | None = None
                    jump = raw_jump
                    beat = self._next_pending_beat()
                    if beat is not None:
                        room = beat[0] - self._elapsed_minutes
                        if 0 <= room < jump:
                            jump = room
                            idle_clamp_reason = f"예정 이벤트({beat[1]}) 전까지 {raw_jump}→{room}분"
                    # 관전 텔레메트리 — 전원 휴면 강제 재투입으로 시간이 크게 건너뛰는
                    # 것을 피드에서 볼 수 있게 한다. 예전엔 이 점프가 조용히 일어나
                    # "왜 갑자기 3시간이 지났지?"가 됐다.
                    self._emit("time_jump", {
                        "wave":           disp_wave,
                        "mode":           "idle",
                        "used_fallback":  False,
                        "category_id":    None,
                        "category_label": None,
                        "reason":         jump_reason,
                        "raw_minutes":    raw_jump,
                        "minutes":        jump,
                        "clamp_reason":   idle_clamp_reason,
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
                    # `any_reached`(이번 wave에 실제로 누군가 누군가에게 말이 닿았나)는
                    # 위 휴면 스트릭 블록에서 이미 계산됐다. 다들 각자 독백이면
                    # "진행 중인 대화 장면"이 아니므로 시간 추론이 장면 보호 캡을
                    # 걸지 않는다 — 온 가족이 잠든 밤에도 45분씩만 흐르던 버그.
                    if self._time_estimation_mode == "ai":
                        ai_result = self._estimate_wave_minutes(disp_wave, results, any_reached)
                        if ai_result is None:
                            # AI 추론 실패 — 카테고리 모드로 조용히 폴백. "normal_scene"은
                            # 있으면 그리로, 없으면(사용자가 라벨을 바꿨거나 지웠으면)
                            # _resolve_time_category가 알아서 첫 카테고리로 떨어뜨린다.
                            used_fallback = True
                            category_id = "normal_scene"
                            raw_jump    = self._random_minutes_for_category(category_id)
                            jump_source = "ai→카테고리 폴백"
                            logger.info(
                                f"[W{disp_wave}] 시간 추론 모드=ai 실패 — "
                                f"카테고리 방식으로 폴백, {raw_jump}분"
                            )
                        else:
                            raw_jump, ai_reason = ai_result
                            jump_source = "ai"
                            logger.info(f"[W{disp_wave}] 시간 추론 모드=ai — {raw_jump}분")
                    else:
                        category_id = self._classify_wave_time(disp_wave, results, any_reached)
                        raw_jump    = self._random_minutes_for_category(category_id)
                        jump_source = category_id
                    jump, clamp_reason = self._clamp_time_jump(raw_jump, results, any_reached)
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

        # 정상 종료 경로 — 여기 도착 못 하고 run()이 예외로 빠져나가는 경우의
        # 대비책은 finalize_run()의 방어적 shutdown(backend/api/simulation/runner.py).
        self._turn_executor.shutdown(wait=True)

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

    def _classify_wave_time(self, wave_num: int, results: dict,
                            any_reached: bool = True) -> str:
        """이번 wave의 발화 결과를 LLM으로 분류해 시간 경과 카테고리 id를 반환.

        절대 예외를 밖으로 던지지 않음 — 실패 시 ``"normal_scene"``을 반환하지만,
        이 문자열 자체는 이제 아무 의미가 없다(그 id를 가진 카테고리가 실제로
        있는지는 `_resolve_time_category`가 신경 쓰지 않는다) — 있으면 그리로,
        없으면 목록의 첫 카테고리로 조용히 떨어진다.
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
                placement=self._placement_summary(results, any_reached),
                next_beat=next_beat,
            )
            valid_ids = {c["id"] for c in self._time_categories}
            if category_id is None or category_id not in valid_ids:
                logger.warning(f"[W{wave_num}] 시간 분류 실패/알수없는 카테고리({category_id!r}) — 첫 카테고리로 폴백")
                return "normal_scene"
            return category_id
        except Exception as e:
            logger.warning(f"[W{wave_num}] 시간 분류 예외 — 첫 카테고리로 폴백: {e}")
            return "normal_scene"

    def _resolve_time_category(self, category_id: str | None) -> dict | None:
        """category id → 실제로 사용될 카테고리 dict.

        알 수 없는 id(또는 None)면 항상 목록의 **첫 번째 카테고리**로 폴백한다.
        카테고리가 하나도 설정되지 않았으면 ``None``.

        예전엔 ``id == "normal_scene"``인 항목을 특별 취급해 그리로 폴백했는데,
        설정 화면은 카테고리의 label·min·max만 편집하게 하고 id는 아예 보여주지
        않는다(`renderTimeCategories()`, frontend) — 즉 id는 사용자가 절대 못
        보는 순수 내부 키인데 코드 한 군데(`_resolve_time_category`)만 그 값에
        의미를 두고 있었다. 사용자가 그 항목의 라벨을 다른 뜻으로 바꾸거나
        지워버리면(둘 다 UI에서 자유롭게 가능) 이 특별 취급은 그냥 조용히
        `cats[0]`으로 떨어지던 것과 동작이 같았으므로, "id는 완전히 불투명한
        키이고 목록의 첫 항목이 곧 기본값"이라는 규칙 하나로 정리했다 —
        설정 화면에도 이 규칙을 안내한다(index.html의 카테고리 목록 힌트).

        `time_jump` 이벤트가 "실제로 쓰인" 카테고리의 id/label을 싣기 위해 분리했다
        — 폴백이 걸렸을 때 요청된 id를 그대로 보여주면 사용자가 라벨/범위를
        미세조정할 때 엉뚱한 카테고리를 고치게 된다.
        """
        cats = self._time_categories or []
        if not cats:
            return None
        return next((c for c in cats if c["id"] == category_id), None) or cats[0]

    def _random_minutes_for_category(self, category_id: str | None) -> int:
        """카테고리 id의 min~max 범위에서 경과 분을 뽑는다 (카테고리 모드의 원래 로직).

        알 수 없는 id(또는 None)면 목록의 첫 카테고리로 폴백한다.
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

    def _placement_summary(self, results: dict, any_reached: bool = True) -> str:
        """이번 장면 화자들의 위치·상호작용 한 줄 요약 (시간 추론 프롬프트용).

        ``any_reached`` = 이번 wave에 누군가 누군가에게 말이 닿았는가. 같은 방에
        있어도 아무도 말을 안 걸었으면(각자 독백·취침) "진행 중인 대화"가 아니므로
        압축 여지를 열어준다. 위치 미설정(레거시)은 빈 문자열 → 블록 생략.
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
        quiet = " — 아무도 서로 말을 걸지 않았다(각자 독백/조용함). 장면 내용에 따라 압축 가능" \
            if not any_reached else ""
        if len(by_loc) == 1:
            loc, names = next(iter(by_loc.items()))
            if len(names) == 1:
                return f"{names[0]} 혼자 {loc}에 있음 (상호작용 없음 — 시간 압축 가능)"
            base = f"{', '.join(names)} 모두 같은 곳({loc})에 함께 있음"
            return base + (quiet or " (대화가 오가는 중이면 압축 금지)")
        parts = [f"{loc} {len(names)}명" for loc, names in by_loc.items()]
        return f"여러 곳에 흩어져 있음 ({', '.join(parts)}) — 서로 상호작용 없으면 압축 가능"

    def _estimate_wave_minutes(self, wave_num: int, results: dict,
                               any_reached: bool = True) -> tuple[int, str] | None:
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
                placement=self._placement_summary(results, any_reached),
                next_beat=next_beat,
            )
        except Exception as e:
            logger.warning(f"[W{wave_num}] AI 시간 추론 예외 — 카테고리 폴백: {e}")
            return None

    def _clamp_time_jump(self, raw_jump: int, results: dict,
                         any_reached: bool = True) -> tuple[int, str | None]:
        """가변 시간 점프(분)를 벽시계·동석 상황 기준으로 결정론적으로 캡한다.

        LLM 분류기는 '장면의 질감'만 정하고, 실제 경과 분의 상한은 여기서 엔진이
        강제한다 — 약한 모델이 오후 한복판에서 최대 범위(예: 480분)를 골라 학원·
        퇴근·저녁 식사 같은 재집결 장면을 통째로 건너뛰는 것을 막는다.

        ``any_reached`` = 이번 wave에 누군가 누군가에게 말이 닿았는가. 같은 방에
        있어도 아무도 말을 안 걸었으면(각자 독백·취침) "진행 중인 대화 장면"이
        아니므로 동석 캡을 걸지 않는다 — 온 가족이 잠든 밤에 45분씩만 흐르던 버그.

        적용 가능한 상한을 전부 (분, 사유) 후보로 모아 마지막에 **한 번에**
        min()을 취한다 — 예전엔 우선순위 순서(예정 이벤트 → 상태 해제 → 동석 →
        주간)로 **먼저 걸리는 조건에서 바로 반환**했는데, 그러면 뒤에 있는 캡이
        실제로 더 짧아도 무시된다. 실제 실행에서 확인된 버그(외부 리뷰 Finding 4):
        상태 해제가 10분 뒤인데 예정 이벤트가 60분 뒤라 예정 이벤트 캡이 먼저
        걸려 60분이 그대로 통과, 정작 더 급한 상태 해제 시점을 넘겨버렸다.

        반환: ``(clamped_jump, 사유_문자열 or None)``. 사유가 None이면 캡 미적용.
        """
        limits: list[tuple[int, str]] = []

        # 예정된 at_time 이벤트를 넘기지 않는다. 시간 추론 프롬프트도 이 시각을
        # 보지만(hi 캡 + next_beat), 카테고리 모드의 랜덤 추출이나 AI 추론 실패
        # 폴백은 프롬프트를 안 타므로 여기서 못 박는다.
        beat = self._next_pending_beat()
        if beat is not None:
            room = beat[0] - self._elapsed_minutes
            if room >= 0:
                limits.append((room, f"예정 이벤트({beat[1]})"))

        # 가장 이른 상태(수면·이동 등) 해제 시점도 같은 원칙으로 넘기지 않는다.
        # 짧은 개인 용무(예: busy 10~30분)가 곧 끝나 돌아와야 하는 사람이
        # 있는데, 다른 누군가의 "깊은 잠에 빠졌다" 같은 무성 독백 하나만 보고
        # LLM이 몇 시간을 통째로 점프시키면 그 활동 완료·복귀 서사가 그대로
        # 묻힌다 — 실제 실행에서 확인된 버그(리뷰 3번: 씻으러 간 사람이 자기
        # 방으로 돌아오지 못한 채 밤을 넘김). `forced_silence_reinject` 경로
        # (전원 침묵)는 이 함수를 안 타므로 run()이 `_earliest_status_clear`를
        # 직접 쓰지만, 이 경로(LLM이 실제로 뭔가 판단한 경우)는 여기서 막아야 한다.
        state_wake_at = self._earliest_status_clear(self.active_agents, self._elapsed_minutes)
        if state_wake_at is not None:
            room = state_wake_at - self._elapsed_minutes
            if room >= 0:
                limits.append((room, "상태 해제 시점"))

        # 목표 기간(target_duration_minutes) 마감도 같은 방식으로 넘기지 않는다
        # — 예전엔 이 캡이 아예 없어서, 목표를 이미 다 채운 뒤에도 자유 점프가
        # 목표를 몇 시간 더 지나쳐야 다음 wave의 "목표 기간 도달" 체크가 뒤늦게
        # 걸렸다(실제 실행에서 2,880분 목표가 2,995분에서야 종료).
        if self._target_deadline_elapsed is not None:
            room = self._target_deadline_elapsed - self._elapsed_minutes
            if room >= 0:
                limits.append((room, "목표 기간 종료"))

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

        # 실내 한 곳에 2명 이상이 **서로 말을 주고받는 중** = 진행 중인 장면.
        # 강하게 캡. 각자 독백만 하고 있으면(any_reached=False) 같은 방이어도
        # 보호할 대화가 없다 — 취침 장면이 45분씩 갈리지 않도록.
        scene_cap = self._max_scene_jump_minutes
        if (scene_cap > 0 and any_reached
                and len(interior_locs) != len(set(interior_locs))):
            limits.append((scene_cap, "동석 장면(실내 2인+)"))

        # 밤(22~06시)이 아니고 집에 남아 있는 사람이 있으면 주간 상한 적용.
        # 모두 외부(회사·학교·학원)로 나가 집이 완전히 빈 낮은 캡하지 않는다
        # — 그때는 건너뛸 재집결 장면 자체가 없다.
        daytime_cap = self._max_daytime_jump_minutes
        now_hour = ((self._sim_start_minutes + self._elapsed_minutes) % 1440) // 60
        is_night = now_hour >= 22 or now_hour < 6
        if daytime_cap > 0 and not is_night and interior_locs:
            limits.append((daytime_cap, "주간·재실자 있음"))

        if not limits:
            return raw_jump, None

        cap_minutes, cap_reason = min(limits, key=lambda t: t[0])
        if raw_jump > cap_minutes:
            return cap_minutes, f"{cap_reason} {raw_jump}→{cap_minutes}분"
        return raw_jump, None

