"""에이전트 상태(수면·이동 등) — 발화/인지 가능 여부를 제어.

배경: 재투입(전원 침묵 처리)이 에이전트의 상태를 전혀 모른 채 무조건 다시 초대해서,
이미 잠든 에이전트가 매 침묵 사이클마다 억지로 잠꼬대를 반복하거나, zone 경계를
건너는 이동이 실제로는 시간이 걸려야 하는데 1 wave 만에 "순간이동"하는 문제가
있었다. 이 모듈은 그 간극을 메우는 최소한의 상태 계층이다.

상태는 두 갈래로 진입한다:
- **자기-선언형** (`sleep`/`busy` 등, `state_categories`) — 캐릭터의 선택이므로
  에이전트 스스로 `enter_state` 필드로 category id를 고른다(정확한 분은 고르지
  않는다 — LLM이 부르는 숫자를 그대로 믿지 않고, `time_categories`와 같은 원칙으로
  엔진이 그 카테고리의 min~max 범위에서 무작위로 뽑는다).
- **월드-부여형** (`traveling`) — zone 경계를 건너는 hop은 지리적 사실이지 캐릭터의
  판단이 아니므로, 캐릭터가 고르지 않고 엔진이 `zone_travel_min/max_minutes`
  범위에서 자동으로 부여한다(runner.py의 이동 적용 루프).

상태가 발화 제어에 개입하는 지점은 정확히 셋이다:
1. `_same_room`(targets.py) — **`traveling`에 한해서만** 대칭적으로 "같은 방 아님"
   처리한다(양방향 자동 해결 — 본인도 남을 못 보고, 남도 아직 도착 안 한 그를 못
   본다). `sleep`/`busy`는 물리적으로 그 자리에 있는 것이므로 같은 방 판정에는
   영향을 주지 않는다 — 같은 방 사람이 알면서 말을 걸면 정상 전달된다(직접
   타깃팅은 이미 co-location 게이트를 타므로 별도 처리가 필요 없다).
2. `wakeable` 재투입 후보(runner.py) — 상태 중인 에이전트는 제외한다. 직접
   타깃팅(누군가 실제로 말을 걸어 routed/scene_injections로 들어오는 경우)은
   이 재투입 경로와 무관하므로 영향받지 않는다.
3. "전원 재투입" 시간 점프(runner.py) — 활성 에이전트 **전원**이 상태에 묶여
   있을 때만 idle 스케줄의 랜덤값 대신 **가장 이른 상태 해제 시점까지 정확히
   점프**하고, 그 시점에 실제로 풀리는 에이전트 **한 명만** 다시 초대한다 —
   아직 안 풀린 나머지를 같이 깨우면 "아직 자는 중"이라는 거의 같은 대사를
   반복하게 될 뿐이다. 나머지는 각자의 해제 시점에 다음 wave 상단의 만료
   처리(`_expire_agent_states`)로 자연히 합류한다(아래 "자연 만료").
   "전원"이 실제로 강제돼야 한다 — 이 wave가 "전원 침묵"(아무도 서로를 못 닿음)으로 판정된 것과
   활성 에이전트 전원이 상태 중인 것은 별개다. 한 명만 상태 중이고 나머지는
   그냥 고립돼 있을 뿐인데 그 한 명의 해제 시점으로 점프하면, 아직 상태에
   안 묶인 나머지의 남은 시간까지 통째로 건너뛴다(실측: 밤 10시대에 한 명만
   자고 있었는데 자정 넘어 새벽까지 통째로 점프해 다른 한 명의 저녁 활동이
   증발함). 일부만 상태 중일 때는 반대로 idle 스케줄 점프가 그 사람의 해제
   시점을 **넘지 않도록** 해제 시점에서 자른다(runner.py idle 점프 클램프).

**자연 만료** — 진입은 에이전트의 선택이지만 지속 시간은 엔진이 정하므로, 끝나는
순간도 엔진이 본인에게 알려야 한다. runner.py가 wave **시작 시점**(LLM 호출 전)에
`_expire_agent_states`로 만료분을 걷어내고, 활성인 본인을 그 wave의 발화자로
편입하면서 incoming 맨 앞에 `_status_release_notice` 알림, 그 뒤에 이동 중 놓친
메시지(`_release_held_incoming`)를 붙인다. 도착지의 **다른 사람**에게 가는 도착
알림(`_deferred_arrival_scene_injections`)은 예전처럼 다음 wave로 간다. 개입으로
턴을 받아 `enter_state`를 비워 즉시 해제되는 경로는 이미 턴을 받은 것이라 알림
대상이 아니다(그 해제는 이 만료 처리를 거치지 않는다).

**자기 선언 상태 중 비직접 메시지 보류** (`sleep`·`busy`·사용자 정의 등 traveling
외 전부) — 실측(case1 v9 W108~114, spatial): 안방에서 먼저 잠든 아빠가 엄마의
"도착" 씬 하나로 턴을 받아 잠든 채 "(깊은 잠에 빠져 숨을 쉰다)"를 뱉고, 그 행동이
행동 관찰 씬으로 같은 방 엄마에게 가 엄마가 턴을 받고… 매 wave 핑퐁했다. next_wave
가 늘 차 있어 "전원 상태 잠금 → 해제 시점 점프"도 막혀 밤이 5~45분 단위로 갈렸다.
그래서 runner의 next_wave 조립에서:
- **턴을 주는 것**(기존대로): 그 사람을 **직접 타깃**한 말(1차 라우팅·1-wave 유예·
  이동 후 보강 배달), 예약 이벤트 알림(notified/entrant — 알람), 디렉터 개입,
  자연 만료(해제 알림). 이 중 앞의 것은 next_wave 조립에서, 나머지는 wave 시작에서
  current_wave에 직접 들어온다.
- **보류하는 것**: 직접 타깃이 아닌 모든 메시지(도착·이탈 씬, 엿듣기, 행동 관찰 씬,
  외모 변경 씬, 만남·여정 씬 등) — traveling과 같은 `held_incoming` 저장소에 쌓는다.
  같은 wave에 그 사람을 직접 타깃한 말이 하나라도 있으면 그 턴이 어차피 생기므로
  비직접 메시지도 함께(원래 순서대로) 배달한다.
- **보류분 전달**: 자연 만료면 wave 시작 블록의 해제 알림 뒤에, 상태 중에 다른
  이유로 턴을 받으면(직접 타깃·알람·디렉터·안전장치) runner의 턴 직전 관문에서
  incoming 맨 앞에 — 어느 쪽이든 `_summarize_held`로 상한·요약을 거친다. 그래서
  개입 턴에서 `enter_state`를 비워 즉시 해제돼도 보류분은 이미 그 턴에 전달된 뒤다.
또 `_route_spatial`은 상태를 **이어가는**(같은 상태 재선언) 화자의 혼잣말 행동을
방송하지 않는다 — 숨소리·잠꼬대가 깨어 있는 동거인에게 턴을 만들지 않게.
"""
import random

# 보류 메시지 표식 — `_route_spatial`이 만든 항목에만 붙는다(요약 우선순위용).
# 소비처(`_inject_incoming`)는 speaker/content/action_note만 읽으므로 추가 키로
# 깨지지 않지만, 정상 배달·해제 시에는 떼어 내 기존 shape을 유지한다.
HELD_KIND_KEY      = "kind"
HELD_KIND_OVERHEARD = "overheard"     # 엿듣기(`_eavesdrop_tag` 경로)
HELD_KIND_ACTION    = "action_scene"  # 독백 행동 관찰(`_action_scene_msg` 경로)
# 해제 시 상한: 행동 관찰 씬은 최근 N개만, 전체는 M개. 넘친 건 한 줄 요약.
HELD_ACTION_KEEP = 2
HELD_TOTAL_CAP   = 8


def strip_held_kind(msgs: list[dict]) -> list[dict]:
    """`kind` 표식을 뗀 사본 — 소비처에 기존 shape(speaker/content/action_note)만."""
    return [
        {k: v for k, v in m.items() if k != HELD_KIND_KEY} if HELD_KIND_KEY in m else m
        for m in msgs
    ]


class _StatusMixin:
    """에이전트 상태 저장·조회·진입·만료 처리."""

    # ── 카테고리 해석 ────────────────────────────────────────────────────────

    def _resolve_state_category(self, category_id: str | None) -> dict | None:
        """category id → 실제로 쓰일 상태 카테고리 dict.

        `_resolve_time_category`(runner.py)와 정확히 같은 규칙 — 알 수 없는
        id(또는 None)는 항상 목록의 **첫 번째 카테고리**로 폴백한다. id는 설정
        화면에 노출되지 않는 순수 내부 키라 특정 이름에 의미를 두지 않는다.
        카테고리가 하나도 설정되지 않았으면(`state_categories: []` = 기능 off) None.
        """
        cats = self._state_categories or []
        if not cats:
            return None
        return next((c for c in cats if c["id"] == category_id), None) or cats[0]

    def _status_display_label(self, st: dict | None) -> str | None:
        """상태 dict → 사람이 읽는 표시용 라벨(컨텍스트 배너·상태 진입 이벤트 공용).

        `_resolve_state_category`를 쓰면 안 된다 — 그 첫-카테고리 폴백은
        `_enter_state` 자기-선언 경로(요청 id와 실제 적용 범위를 일치시키기)를
        위한 규칙이라, 표시에 쓰면 카테고리 목록에 없는 엔진 부여 상태
        `traveling`이 "수면"으로 둔갑한다(실측: 고등학교로 이동 중인데 배너가
        🚶 아이콘 + 수면 라벨). 그래서 여기서는 폴백 없이:
        - `traveling` → 도착지 기준 "…(으)로 이동 중"(도착지가 없으면 "이동 중").
          runner.py의 이동 시작 emit도 이 함수를 써서 문구 정본이 한 곳이다.
        - 그 외 → id가 **정확히 일치**하는 카테고리의 label, 없으면 state id 그대로.
        상태가 없으면 None.
        """
        if not st or not st.get("state"):
            return None
        state = st["state"]
        if state == "traveling":
            dest = st.get("arrival_location") or ""
            return f"{dest}(으)로 이동 중" if dest else "이동 중"
        cat = next((c for c in (self._state_categories or []) if c.get("id") == state), None)
        return (cat or {}).get("label") or state

    def _status_release_notice(self, key: str, st: dict) -> str:
        """상태가 **자연 만료**로 풀린 본인에게 주는 한 줄 씬 알림.

        왜 필요한가: 상태에 들어가는 건 에이전트의 선택(`enter_state`·zone 경계
        이동)이지만 끝나는 시점은 엔진이 정한다. 예전엔 만료돼도 본인에게 아무
        신호가 없어서, 누가 말을 걸거나 소외/전원 침묵 재투입에 걸릴 때까지
        (그것도 빈 incoming으로) 움직이지 않았다 — 실측(case1): 23:09에 씻기가
        끝난 아빠가 01:46에야 "씻고 나와 수건으로 닦는다"로 턴을 받음. 그래서
        runner가 만료 wave에 턴을 주면서 incoming 맨 앞에 이 알림을 넣는다.

        문구 규칙(도착·이탈 씬 알림 "[씬] X이(가) 이곳에 도착했다."와 같은 톤 —
        짧은 과거형 평서문, 무엇이 끝났고 지금 어디인지만):
        - `traveling` → "[씬] {도착지}에 도착했다." 도착지 자체가 내용이라 괄호
          위치를 따로 붙이지 않는다. 외부 공간이어도 본인 알림은 준다(남에게
          가는 도착 알림 `_deferred_arrival_scene_injections`만 외부를 건너뛴다).
          여정(journey.py)의 경유지 도착이면 뒤에 "(동네로 가는 길에 들름, 잠시
          멈춤)" / "(…, 그대로 지나가는 중)"이 붙는다.
        - `sleep` → "[씬] 잠에서 깼다. (현재 위치)". 기본 카테고리 id `sleep`에만
          전용 문구를 준다 — "하던 일을 마쳤다: 수면"은 어색하다. id를 바꾼 사용자
          정의 수면은 아래 일반 문구로 떨어질 뿐 틀린 말은 아니다.
        - 그 외(`busy`·사용자 정의 카테고리) → "[씬] 하던 일을 마쳤다: {라벨}.
          (현재 위치)". 라벨은 배너·상태 진입 이벤트와 같은 정본
          `_status_display_label`.
        위치가 없는 레거시(위치 미사용) 시나리오는 괄호를 생략한다.
        """
        state = st.get("state")
        here  = self._agent_location.get(key, "") or ""
        if state == "traveling":
            dest = st.get("arrival_location") or here
            if not dest:
                return "[씬] 목적지에 도착했다."
            # 여정의 경유지면 "(동네로 가는 길에 들름, 잠시 멈춤 / 그대로 지나가는
            # 중)"을 붙인다 — 멈춤/통과 판정은 runner 가 이 알림을 만들기 **전에**
            # `_journey_on_status_expiry` 로 끝내 둔다. 목적지 도착·여정 없음이면 불변.
            return f"[씬] {dest}에 도착했다.{self._journey_notice_suffix(key, dest)}"
        where = f" ({here})" if here else ""
        if state == "sleep":
            return f"[씬] 잠에서 깼다.{where}"
        label = (self._status_display_label(st) or "").rstrip(" .") or "하던 일"
        return f"[씬] 하던 일을 마쳤다: {label}.{where}"

    # ── 조회 ─────────────────────────────────────────────────────────────────

    def _agent_active_status(self, key: str, now_elapsed: int) -> dict | None:
        """key가 지금(now_elapsed) 유효한 상태를 갖고 있으면 그 dict, 없으면 None.

        만료된 상태를 여기서 지우지는 않는다 — 정리·부수효과(도착 알림 등)는
        `_expire_agent_states`가 명시적으로 담당한다. 이 함수는 순수 조회용.
        """
        st = self._agent_status.get(key)
        if not st or now_elapsed >= st.get("until_elapsed", 0):
            return None
        return st

    def _agent_unavailable(self, key: str, now_elapsed: int) -> bool:
        """key가 지금 어떤 상태로든(수면·이동 등) 묶여 있어 자연스러운 재투입
        대상이 아닌지. 재투입(wakeable) 필터 전용 — 직접 타깃팅에는 안 쓴다."""
        return self._agent_active_status(key, now_elapsed) is not None

    def _agent_self_state(self, key: str, now_elapsed: int) -> dict | None:
        """key가 지금 **자기 선언 상태**(traveling 외 — sleep·busy·사용자 정의)면
        그 dict, 아니면 None. 비직접 메시지 보류·혼잣말 행동 방송 억제의 판정."""
        st = self._agent_active_status(key, now_elapsed)
        if st is None or st.get("state") == "traveling":
            return None
        return st

    def _agent_traveling(self, key: str, now_elapsed: int) -> bool:
        """key가 지금 zone 경계를 건너는 중(traveling)인지.

        `_same_room`이 이 함수만 참조한다 — `sleep`/`busy`는 "그 자리에 있지만
        하는 일이 있는" 상태라 같은 방 판정과 무관하고, `traveling`만 "물리적으로
        아직 도착/이탈 중이라 지금 이 방에 없는 것과 같다"는 의미이기 때문이다.
        """
        st = self._agent_active_status(key, now_elapsed)
        return bool(st and st.get("state") == "traveling")

    # ── 진입 ─────────────────────────────────────────────────────────────────

    def _enter_state(
        self, key: str, now_elapsed: int,
        *, minutes: int | None = None, category_id: str | None = None,
        state: str | None = None, **extra,
    ) -> int | None:
        """key를 상태로 전환하고 `until_elapsed`를 새로 잡는다.

        두 가지 호출 형태:
        - **자기-선언형**: `category_id`만 준다. `_resolve_state_category`로 그
          카테고리를 찾아 min~max 범위에서 `random.randint`로 분을 뽑고, 저장되는
          `state` 라벨은 **그 카테고리의 실제 id**다 — 요청한 category_id가 목록에
          없어 폴백이 걸려도(`_resolve_time_category`와 같은 무의미 id 규칙) 라벨과
          실제 적용된 범위가 항상 일치한다. 카테고리가 하나도 없으면(기능 off) None.
        - **월드-부여형**: `minutes`와 `state`를 직접 준다(예: `state="traveling"`).
          캐릭터의 판단이 아니라 엔진이 계산한 값이라 카테고리를 거치지 않는다.

        `**extra`는 상태 dict에 그대로 병합된다(예: traveling의 `arrival_location`).
        반환값은 실제로 부여된 지속 분(디버그/테스트 편의용) — 호출부가 굳이
        쓰지 않아도 된다. **재선언은 타이머를 갱신하지 않는다** — 아직 안 끝난
        같은 상태를 다시 선언했다면(개입으로 턴을 받았지만 "계속 유지"를
        선택한 경우) 이미 진행 중인 타이머를 그대로 이어간다(사용자 설계:
        "이전 타이머가 남아 있는 상태에서... 타이머 변경 없이 이어서 진행").
        타이머가 실제로 끝난 뒤(또는 애초에 상태가 없었을 때) 다시 진입하는
        경우에만 새로 뽑는다 — 이 경우도 반환값은 None(변경 없음)이라 호출부가
        "enter" 이벤트를 새로 emit하지 않는다(진짜 변화가 없으므로).
        """
        if category_id is not None:
            cat = self._resolve_state_category(category_id)
            if cat is None:
                return None
            current = self._agent_active_status(key, now_elapsed)
            if current is not None and current.get("state") == cat["id"]:
                return None
            lo, hi = int(cat["min_minutes"]), int(cat["max_minutes"])
            minutes = random.randint(lo, hi) if lo < hi else lo
            state = cat["id"]
        if minutes is None or state is None:
            return None
        minutes = max(1, int(minutes))
        # 상태가 바뀌어도(예: busy → sleep, 또는 만료 대기 중인 옛 dict 위에 새
        # 진입) 아직 전달 안 된 보류분은 새 상태로 넘긴다 — dict 통째 교체로
        # 조용히 사라지지 않게.
        prev_held = (self._agent_status.get(key) or {}).get("held_incoming")
        self._agent_status[key] = {
            "state": state,
            "until_elapsed": now_elapsed + minutes,
            **extra,
        }
        if prev_held:
            self._agent_status[key]["held_incoming"] = list(prev_held)
        return minutes

    # ── 이동 중 수신 보류 ────────────────────────────────────────────────────
    #
    # 리뷰에서 확인된 구멍: `_same_room`/`_resolve_targets`가 "traveling 중이면
    # 대상팅 불가"를 막아도, 그건 **새로 타깃을 해석할 때만** 적용된다. 이미
    # `routed`/`scene_injections`에 실려 `next_wave`에 들어간 사람은 그 필터를
    # 다시 통과하지 않고 다음 wave에 정상 턴을 받아 "이동 중인데 이미 도착한
    # 것처럼" 서술할 수 있었다. 그래서 next_wave를 구성하는 시점에 한 번 더
    # traveling 여부를 확인해, 대상이 이동 중이면 배달을 보류(hold)했다가
    # 실제로 도착한 순간(_expire_agent_states) 본인에게 돌려준다.

    #
    # 같은 저장소를 자기 선언 상태(수면·busy 등)의 **비직접 메시지** 보류에도 쓴다
    # (모듈 docstring "자기 선언 상태 중 비직접 메시지 보류").

    def _hold_incoming(self, key: str, msgs: list[dict]) -> None:
        """key가 지금 이동 중이거나(모든 메시지) 자기 선언 상태라(비직접 메시지)
        지금 턴을 줄 수 없는 메시지를 상태에 보관한다.

        상태 자체가 없으면(예: 이미 만료돼 다음 턴에 정리 대기 중인 극히
        드문 순간) 아무 것도 하지 않는다 — 그 경우는 정상 배달 경로로 이미
        처리됐어야 한다.
        """
        st = self._agent_status.get(key)
        if st is None or not msgs:
            return
        st.setdefault("held_incoming", []).extend(msgs)

    def _release_held_incoming(self, expired: dict[str, dict]) -> dict[str, list]:
        """방금 만료된 상태에 쌓여 있던 held_incoming을 본인에게 돌려줄 형태로.

        도착 알림(`_deferred_arrival_scene_injections`, 남에게 보내는 메시지)과
        방향이 반대다 — 이건 **본인**이 이동 중 놓친 것을 받는 쪽이다.
        """
        released: dict[str, list] = {}
        for key, st in expired.items():
            held = st.get("held_incoming")
            if held:
                released[key] = self._summarize_held(held, st.get("state"))
        return released

    def _pop_held_for_turn(self, key: str, now_elapsed: int) -> list[dict]:
        """자기 선언 상태 중인 key가 이번 wave에 **다른 이유로 턴을 받을 때**(직접
        타깃·예약 이벤트 알람·디렉터·안전장치) 보류분을 꺼내 요약해 돌려준다.
        상태 자체는 유지된다(턴에서 재선언하면 계속 잔다). 없으면 []."""
        st = self._agent_self_state(key, now_elapsed)
        if st is None:
            return []
        held = st.pop("held_incoming", None)
        return self._summarize_held(held, st.get("state")) if held else []

    @staticmethod
    def _held_overflow_line(state: str | None, n: int) -> str:
        when = {"sleep": "자는 동안", "traveling": "이동하는 동안"}.get(state or "", "하던 일 중에")
        return f"[씬] ({when}) 그 밖에 {n}건의 일이 있었다."

    def _summarize_held(self, msgs: list[dict], state: str | None) -> list[dict]:
        """보류분 → 해제 시 실제로 건넬 목록(상한·요약). 순서는 원래 도착 순서.

        규칙(밤새 쌓인 보류분이 수십 개가 되지 않게):
        1. 행동 관찰 씬(`kind=action_scene` — 옆 사람의 숨소리·뒤척임)은 **최근
           `HELD_ACTION_KEEP`개**만.
        2. 그래도 `HELD_TOTAL_CAP`개를 넘으면 우선순위대로 채운다 — ① 표식 없는 항목
           (도착·이탈·외모 변경 씬, 만남·여정 씬, traveling 중 보류된 직접 대사 등
           **사실 정보**) ② 엿듣기(`kind=overheard`) ③ 행동 관찰 씬. 같은 등급 안에서는
           최근 것부터.
        3. 버려진 게 있으면 맨 끝에 "[씬] (자는 동안/이동하는 동안/하던 일 중에) 그
           밖에 N건의 일이 있었다." 한 줄.
        상한 이하·행동 씬 2개 이하면 원본 그대로(표식만 뗌) — traveling의 기존
        보류 동작은 그래서 사실상 바뀌지 않는다(공통 적용).
        """
        if not msgs:
            return []
        idx_action = [i for i, m in enumerate(msgs) if m.get(HELD_KIND_KEY) == HELD_KIND_ACTION]
        keep = set(range(len(msgs))) - set(idx_action[:-HELD_ACTION_KEEP] if HELD_ACTION_KEEP else idx_action)
        if len(keep) > HELD_TOTAL_CAP:
            def tier(i: int) -> int:
                kind = msgs[i].get(HELD_KIND_KEY)
                return 2 if kind == HELD_KIND_ACTION else 1 if kind == HELD_KIND_OVERHEARD else 0
            # 등급 오름차순, 같은 등급은 최근(인덱스 큰) 것 먼저.
            ranked = sorted(keep, key=lambda i: (tier(i), -i))
            keep = set(ranked[:HELD_TOTAL_CAP])
        out = strip_held_kind([msgs[i] for i in sorted(keep)])
        dropped = len(msgs) - len(keep)
        if dropped:
            out.append({"speaker": "씬", "action_note": "",
                        "content": self._held_overflow_line(state, dropped)})
        return out

    # ── 만료 ─────────────────────────────────────────────────────────────────

    def _expire_agent_states(self, now_elapsed: int) -> dict[str, dict]:
        """만료된(지금 시점 기준) 상태를 제거하고, 제거된 항목들을 반환한다.

        반환값 `{key: status_dict}`은 호출부(runner.py, wave 시작 시점)가
        부수효과(clear 이벤트, 본인 해제 알림 + 이번 wave 턴, held_incoming 반환,
        traveling 만료 시 지연된 "도착했다" 씬 알림)를 처리하는 데 쓴다 — 이 함수
        자체는 상태 dict를 지우는 것 말고는 아무 부수효과가 없다.
        """
        expired: dict[str, dict] = {}
        for key in list(self._agent_status.keys()):
            st = self._agent_status[key]
            if now_elapsed >= st.get("until_elapsed", 0):
                expired[key] = st
                del self._agent_status[key]
        return expired

    def _earliest_status_clear(self, keys, now_elapsed: int) -> int | None:
        """주어진 키들 중 지금 상태 중인 에이전트의 가장 이른 해제 시점(절대 경과분).

        상태 중인 에이전트가 하나도 없으면 None — 호출부가 idle 스케줄 등 기존
        폴백을 쓰게 한다.
        """
        candidates = [
            st["until_elapsed"]
            for k in keys
            if (st := self._agent_active_status(k, now_elapsed)) is not None
        ]
        return min(candidates) if candidates else None

    def _deferred_arrival_scene_injections(self, expired: dict[str, dict]) -> dict[str, list]:
        """방금(이번 wave 시작 시점) 자연 해제된 `traveling` 상태의 "도착했다" 씬
        알림을 **지금** 시점 기준으로 새로 만든다.

        이동이 시작된 시점이 아니라 이동에 걸린 시간이 다 찬 지금 그 자리에 있는
        사람들을 기준으로 계산한다 — 그 사이 룸메이트가 바뀌었을 수 있어서다.
        도착지가 외부 공간(격리)이면 애초에 바라볼 사람이 없어 자연히 빈 dict.
        일반 이동(zone 안)의 즉시 도착 알림(runner.py 이동 루프)과 정확히 같은
        문구 규칙(아는 사이면 실명, 아니면 외모 묘사)을 쓴다.
        """
        injections: dict[str, list] = {}
        for agent_key, st in expired.items():
            if st.get("state") != "traveling" or not st.get("pending_arrival_announcement"):
                continue
            next_loc = st.get("arrival_location", "")
            if not next_loc or next_loc in self._exterior_locations:
                continue
            display      = self._key_to_alias.get(agent_key, agent_key)
            mover_visual = self._agent_visual.get(agent_key, "") or display
            for other_key in self.active_agents:
                if other_key == agent_key:
                    continue
                if self._agent_location.get(other_key, "") != next_loc:
                    continue
                if agent_key in self._agent_knowledge.get(other_key, set()):
                    scene_msg = f"[씬] {display}이(가) 이곳에 도착했다."
                else:
                    scene_msg = (
                        f"[씬] 낯선 이가 나타났다: {mover_visual}"
                        if mover_visual else "[씬] 낯선 이가 나타났다."
                    )
                    # 외모가 알림에 실렸다 = 인지함(재회 알림 기준, runner 이동 루프와 같음).
                    self._mark_seen(other_key, agent_key, self._agent_visual.get(agent_key, ""))
                injections.setdefault(other_key, []).append({
                    "speaker": "씬", "content": scene_msg, "action_note": "",
                })
        return injections
