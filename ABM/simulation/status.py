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
   처리(`_expire_agent_states`)로 자연히 합류한다. "전원"이 실제로 강제돼야
   한다 — 이 wave가 "전원 침묵"(아무도 서로를 못 닿음)으로 판정된 것과
   활성 에이전트 전원이 상태 중인 것은 별개다. 한 명만 상태 중이고 나머지는
   그냥 고립돼 있을 뿐인데 그 한 명의 해제 시점으로 점프하면, 아직 상태에
   안 묶인 나머지의 남은 시간까지 통째로 건너뛴다(실측: 밤 10시대에 한 명만
   자고 있었는데 자정 넘어 새벽까지 통째로 점프해 다른 한 명의 저녁 활동이
   증발함).
"""
import random


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
        self._agent_status[key] = {
            "state": state,
            "until_elapsed": now_elapsed + minutes,
            **extra,
        }
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

    def _hold_incoming(self, key: str, msgs: list[dict]) -> None:
        """key가 지금 이동 중이라 받을 수 없는 메시지를 상태에 보관한다.

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
                released[key] = held
        return released

    # ── 만료 ─────────────────────────────────────────────────────────────────

    def _expire_agent_states(self, now_elapsed: int) -> dict[str, dict]:
        """만료된(지금 시점 기준) 상태를 제거하고, 제거된 항목들을 반환한다.

        반환값 `{key: status_dict}`은 호출부(runner.py)가 부수효과(예: traveling
        만료 시 지연된 "도착했다" 씬 알림)를 처리하는 데 쓴다 — 이 함수 자체는
        상태 dict를 지우는 것 말고는 아무 부수효과가 없다.
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
                injections.setdefault(other_key, []).append({
                    "speaker": "씬", "content": scene_msg, "action_note": "",
                })
        return injections
