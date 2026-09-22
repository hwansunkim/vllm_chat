import logging

logger = logging.getLogger(__name__)


class _TargetsMixin:
    """에이전트 target 해석 및 가시성 관련 메서드."""

    def _normalize_target(self, t: str) -> str:
        """한국어 display_name → 시스템 key 정규화. 이미 key면 그대로 반환."""
        if t in self.agents:
            return t
        return self._alias_to_key.get(t, t)

    def _resolve_targets(self, targets: list[str], speaker_key: str) -> list[str]:
        """발화 target 해석 — active_agents 기준, 위치 기반 필터링 적용.

        지원 형식:
          "all"        → 화자와 **같은 방**의 활성 에이전트 (유예 없음)
          "stranger_N" → 해당 낯선 이 (real_key 변환 + knowledge 확장)
          "<key>"      → 특정 에이전트

        대화는 **같은 방**이어야 성립한다(zone 전체가 아니다 — 벽 너머로 또렷이
        대화하는 건 공간 개념을 무너뜨린다). 단 직접 타깃(`<key>`/`stranger_N`)에
        한해 **1-wave 유예**를 둔다: 지금은 다른 방이지만 **직전 wave 시작 시점에
        같은 방이었으면** 한 번 더 배달한다. 방금 자리를 뜬 상대에게 마지막 한마디를
        건네는 경우(질문에 대한 답, 작별 인사)를 위한 것으로, 그 다음 wave엔 다시
        같은 방이어야 한다. 외부 공간(exterior) 격리는 유예로도 못 뚫는다.

        위치 미설정 에이전트는 기존 동작 유지(항상 같은 방 취급 — 하위 호환).
        외부 공간 화자는 아무에게도 전달할 수 없다.

        **반환 타입은 언제나 플랫 `list[str]`이다.** runner.py(라우팅)와
        turn.py(`_apply_turn_result`의 관계 그래프 edge 생성)가 둘 다 이 형태를
        기대하므로 절대 바꾸지 말 것.
        """
        now         = self._current_elapsed_minutes(self.completed_waves)
        speaker_loc = self._agent_location.get(speaker_key, "")
        if speaker_loc in self._exterior_locations or self._agent_traveling(speaker_key, now):
            return []  # 외부 공간 화자, 또는 화자 본인이 이동 중 — 메시지 전달 불가

        resolved    = []

        def _same_loc(key: str) -> bool:
            """위치 시스템 활성 시 같은 위치인지 확인. 위치 미설정이면 항상 True.

            `traveling` 중인 상대는 raw 위치가 이미 목적지를 가리켜도 아직 그
            방에 없는 것과 같다 — `_same_room`과 같은 원칙(둘 중 하나라도 이동
            중이면 같은 방 아님)의 화자 시점 버전이다.
            """
            if self._agent_traveling(key, now):
                return False
            if not speaker_loc:
                return True
            other_loc = self._agent_location.get(key, "")
            if not other_loc:
                return True
            if speaker_loc == other_loc:
                return True
            # 위치 불일치로 타깃이 폐기되는 경로. 무성 폐기(silent drop)는 웨이브가
            # 통째로 비는 원인이 되므로 추적 가능하도록 남긴다.
            logger.debug(
                "target drop (location mismatch): speaker=%s@%s target=%s@%s",
                speaker_key, speaker_loc, key, other_loc,
            )
            return False

        def _can_address(key: str) -> bool:
            """직접 타깃(`<key>`/`stranger_N`) 전용 도달 판정.

            1-wave 유예(`_recently_co_located`)를 먼저 본다. 순서가 중요하다 —
            `_same_loc`을 먼저 부르면 유예로 살아남는 타깃에도 "location mismatch"
            드롭 로그가 남아 추적을 흐린다.
            """
            if self._recently_co_located(speaker_key, key):
                return True
            return _same_loc(key)

        for t in targets:
            t_s = t.strip()
            if t_s.lower() in ("self", "system"):
                continue
            elif t_s.lower() == "all":
                # "모두"는 유예 없이 **같은 방 한정**이다 — 방금 나간 사람에게까지
                # 방송을 밀어 넣을 이유가 없다(직접 타깃만 마지막 한마디를 받는다).
                #
                # 정책: 수면·개인 용무(sleep/busy) 중인 사람은 **방송에서도** 뺀다.
                # "같은 방 사람이 상태를 알고도 의도적으로 말을 건다"는 예외는
                # 그 사람을 콕 집어 부르는 직접 타깃(_can_address, 아래 else 분기)
                # 에만 해당한다 — 전체를 향한 말은 그 상태의 상대를 겨냥한 게
                # 아니라서 "알고도 깨웠다"는 전제가 성립하지 않는다. traveling은
                # 이미 _same_loc()이 걸러주므로 여기서는 sleep/busy만 추가로 본다.
                candidates = (k for k in self.active_agents if k != speaker_key)
                resolved.extend(
                    k for k in candidates
                    if _same_loc(k) and not self._agent_unavailable(k, now)
                )
            elif t_s.startswith("stranger_"):
                # stranger_N ID는 같은 장소에서뿐 아니라 같은 zone의 다른 장소를
                # 인지할 때도 발급된다. 대화 가능 범위는 어디까지나 "같은 방"이므로
                # _can_address()(같은 방 + 1-wave 유예)로 걸러야 zone 인지가 대화
                # 채널로 새지 않는다.
                real_key = self._stranger_map.get(speaker_key, {}).get(t_s)
                if real_key and real_key in self.active_agents and _can_address(real_key):
                    self._agent_knowledge.setdefault(speaker_key, set()).add(real_key)
                    self._agent_knowledge.setdefault(real_key, set()).add(speaker_key)
                    resolved.append(real_key)
            else:
                key = self._normalize_target(t_s)
                if key in self.active_agents and key != speaker_key and _can_address(key):
                    if self._is_anonymous_to(speaker_key, key):
                        # 화자가 아직 '낯선 이'로만 인지하는 상대를 실명/key로 부른
                        # 경우. stranger_N 핸드셰이크를 건너뛴 것이므로 폐기한다.
                        logger.debug(
                            "target drop (not acquainted): speaker=%s target=%s",
                            speaker_key, key,
                        )
                        continue
                    resolved.append(key)
        return list(dict.fromkeys(resolved))

    # ── 대화 도달성 ───────────────────────────────────────────────────────────

    def _same_room(self, a: str, b: str) -> bool:
        """a, b가 **지금** 같은 방에 있는가.

        위치 미설정(레거시)은 어느 한쪽이라도 비어 있으면 "같은 방"으로 본다 —
        `_resolve_targets._same_loc`과 같은 하위 호환 규칙. 외부 공간(exterior)이
        한쪽이라도 끼면 격리가 우선이라 False.

        둘 중 누구라도 지금 `traveling`(zone 경계 이동 중) 상태면 무조건 False —
        raw 위치(`_agent_location`)는 hop 적용 즉시 목적지로 바뀌지만, 실제로는
        아직 이동 시간이 남아 있어 그 방에 없는 것과 같다. 이 가드 하나로 양방향이
        같이 풀린다: 이동 중인 쪽은 남을 못 보고(엿듣기·인지 억제), 이미 그 방에
        있는 쪽도 raw 위치만 보고 그를 "도착한 것"으로 오인해 타깃하지 않는다 —
        예: 딸이 하교 중이라 `_agent_location`은 이미 "거실"이어도, 실제로 도착할
        때까지는 거실에 있는 다른 가족이 그녀를 타깃할 수 없다. `sleep`/`busy`는
        물리적으로 그 자리에 있는 것이므로 이 가드와 무관하다 — 같은 방 사람이
        (자고 있음을) 알면서 말을 걸면 정상적으로 전달돼야 한다.
        """
        now = self._current_elapsed_minutes(self.completed_waves)
        if self._agent_traveling(a, now) or self._agent_traveling(b, now):
            return False
        la = self._agent_location.get(a, "")
        lb = self._agent_location.get(b, "")
        if la in self._exterior_locations or lb in self._exterior_locations:
            return la == lb and la not in self._exterior_locations
        return (not la) or (not lb) or (la == lb)

    def _recently_co_located(self, a: str, b: str) -> bool:
        """직전 wave **시작 시점**에 a, b가 같은 방이었는가 (1-wave 대화 유예).

        지금 같은 방이면 이 함수와 무관하게 배달되므로(호출부가 `_same_loc`을 따로
        본다), 여기서 참이 의미를 갖는 건 **방금 갈라선** 경우뿐이다. 그 상황에서
        직접 타깃에게 마지막 한마디(질문의 답·작별)를 한 번 더 배달한다. 그 다음
        wave엔 직전 스냅샷도 '다른 방'이 되므로 유예가 자동으로 닫힌다.

        외부 공간(exterior)은 **지금이든 직전이든** 한쪽이라도 끼면 격리가 우선이라
        False. 직전 위치를 모르면(재개 첫 wave 등) 유예 없음.

        **`traveling` 우선 검사** — `_can_address()`가 이 함수를 `_same_loc()`보다
        먼저 확인하므로(순서는 그 함수 docstring 참고), 여기서 걸러주지 않으면
        `_same_loc()`의 traveling 가드가 아예 호출되지 않고 그냥 통과해버린다.
        정확히 "직전 wave에 같은 방이었다가 방금 zone 경계를 넘어 이동을 시작한"
        경우가 이 유예의 트리거 조건과 겹쳐서, 막 이동을 시작한 상대에게도 마지막
        한마디가 전달되는 구멍이 있었다(리뷰에서 코드 재현으로 확인됨).
        """
        now = self._current_elapsed_minutes(self.completed_waves)
        if self._agent_traveling(a, now) or self._agent_traveling(b, now):
            return False
        if (self._agent_location.get(a, "") in self._exterior_locations
                or self._agent_location.get(b, "") in self._exterior_locations):
            return False
        prev = self._prev_wave_start_location
        if not prev:
            return False
        la = prev.get(a, "")
        lb = prev.get(b, "")
        if not la or not lb:
            return False
        if la in self._exterior_locations or lb in self._exterior_locations:
            return False
        return la == lb

    def _is_monologue_targets(self, targets: list[str] | None) -> bool:
        """이 턴이 '혼잣말'인지 — 원본 targets 필드 기준.

        **해석 결과가 아니라 의도 기준**이다. `["self"]`/`["system"]`(또는 빈
        목록)뿐이면 소리 내어 말한 게 아니므로 같은 방 제3자에게 대사가 가지 않고
        행동만 보인다. 반대로 누군가를 불렀다면 — 그 상대가 이미 자리에 없어
        `_resolve_targets`가 전부 폐기했더라도 — "소리 내어 말한 것"이므로 같은
        방의 제3자는 엿듣는다.
        """
        for t in (targets or []):
            if str(t).strip().lower() not in ("", "self", "system"):
                return False
        return True

    def _eavesdrop_tag(
        self, observer_key: str, speaker_key: str, targets: list[str] | None
    ) -> str:
        """엿듣는 사람에게 보일 `화자→대상1, 대상2` 태그 문자열.

        이름은 전부 **관찰자(observer) 시점**으로 계산한다 — 아는 사이면 표시
        이름, 아직 모르면 `stranger_N`. 규칙은 `_meeting_label()`을 그대로 재사용
        하므로 씬 메시지·상황 블록과 ID가 어긋나지 않는다. 관찰자마다 아는 범위가
        다르므로 **이 문자열은 bystander별로 다르다 — 캐시하지 말 것.**

        대상 목록은 `_resolve_targets`의 해석 결과가 아니라 **원본 targets 참조**를
        정규화해서 쓴다. 실제로 전달됐는지와 무관하게 "누구를 불렀는가"가 엿듣는
        쪽에 정보 가치가 있기 때문이다(부른 상대가 이미 없었다는 사실 자체가 단서).
        """
        speaker_label = self._meeting_label(observer_key, speaker_key)
        labels: list[str] = []
        for t in (targets or []):
            t_s = str(t).strip()
            low = t_s.lower()
            if not t_s or low in ("self", "system"):
                continue
            if low == "all":
                label = "모두"
            else:
                if t_s.startswith("stranger_"):
                    # stranger_N은 **화자**의 사전에서만 의미가 있다. 관찰자에게는
                    # 자기 사전의 ID(또는 실명)로 다시 번역해 줘야 한다.
                    real_key = self._stranger_map.get(speaker_key, {}).get(t_s)
                else:
                    real_key = self._normalize_target(t_s)
                    if real_key not in self.agents:
                        real_key = None
                if real_key is None:
                    label = t_s          # 해석 불가(환각 이름 등) — 원문 그대로
                elif real_key == observer_key:
                    # 관찰자 본인을 불렀는데 해석에서 빠진 경우(예: 미지의 상대를
                    # 실명으로 부름). 자기 자신에게 stranger_N을 발급하면 안 된다.
                    label = "당신"
                elif real_key in self._agent_knowledge.get(observer_key, set()):
                    label = self._key_to_alias.get(real_key, real_key)
                elif self._same_room(observer_key, real_key):
                    # 관찰자와 **같은 방**에 있는 사람이면 새 stranger_N을 발급해도
                    # 안전하다 — 관찰자가 그 사람의 존재를 몰랐다면 이 순간 처음
                    # 알게 된 것이므로 `_meeting_label()`의 정상적인 발급 경로를 탄다.
                    label = self._meeting_label(observer_key, real_key)
                else:
                    # 관찰자와 다른 방이면 ID를 새로 발급하지
                    # 않는다(읽기 전용 조회). 대화에 이름만 등장했을 뿐 관찰자가
                    # 실제로 마주친 적 없는 사람에게 `_get_or_assign_stranger_id`로
                    # ID를 선점시키면 (1) 나중에 실제로 만난 사람의 번호가 밀리고,
                    # (2) `_is_anonymous_to()`가 그 사람을 곧바로 "낯선 이"로 간주해
                    # 관찰자가 실명으로는 다시 부를 수 없게 막아버린다(조용한 드롭).
                    # 이미 발급된 ID가 있으면(=과거에 지각 범위 안에서 마주친 적
                    # 있으면) 그것만 재사용한다.
                    sid = self._stranger_rmap.get(observer_key, {}).get(real_key)
                    label = f'낯선 이(ID: "{sid}")' if sid else "누군가"
            if label not in labels:
                labels.append(label)
        if not labels:
            return speaker_label
        return f"{speaker_label}→{', '.join(labels)}"

    def _is_anonymous_to(self, observer_key: str, target_key: str) -> bool:
        """observer가 target을 아직 '낯선 이'로만 인지 중인지.

        `stranger_N` 분기(위)는 해석과 동시에 _agent_knowledge를 양방향으로 갱신해
        "이름은 만나서 알아내는 것"이라는 전제를 지킨다. 반면 일반 key 분기는
        knowledge를 전혀 보지 않아, 어떤 경로로든(과거의 외모변경 씬 메시지 누출,
        시나리오 배경 텍스트 등) 이름을 알게 되면 핸드셰이크 없이 곧바로 말을 걸 수
        있었다. 그러면 memory엔 실명이 있는데 `[현재 상황]` 블록은 계속 stranger_N을
        보여주는 상태 불일치가 고착된다.

        **하위 호환 설계 — 보수적 판정.** knowledge 검사를 무조건 걸면 위치/관계
        시스템을 쓰지 않는 레거시 시나리오를 깨뜨릴 위험이 있으므로, "stranger_N ID가
        실제로 발급된 적 있는 상대"에게만 knowledge를 요구한다. ID는 관찰자가 그
        사람을 익명으로 본 적이 있을 때만(_compute_wave_targets / _compute_zone_awareness)
        발급되므로:

          - 관계 미설정 시나리오: core.py가 knowledge에 전원을 넣어두므로 애초에 낯선
            이가 없고 ID도 발급되지 않는다 → 항상 False → 기존 동작 그대로.
          - 위치 미설정 + 관계 설정 시나리오: 위치가 비어도 _compute_wave_targets는
            동작하므로 ID가 정상 발급된다 → 익명화가 그대로 적용된다.

        _resolve_targets는 언제나 화자 자신의 턴 **직후**에 불리고, 그 턴의 프롬프트
        조립(_assemble_agent_prompt → _compute_wave_targets)이 이미 동석한 모든 미지의
        상대에게 ID를 발급해둔 뒤다. 따라서 실전 경로에서는 이 보수적 판정이 무조건적
        knowledge 검사와 동일하게 동작하면서, 레거시 경로만 안전하게 비켜간다.
        """
        if target_key in self._agent_knowledge.get(observer_key, set()):
            return False
        return target_key in self._stranger_rmap.get(observer_key, {})

    def _resolve_event_targets(self, targets: list[str]) -> list[str]:
        """이벤트 알림 대상 해석 — active_agents 기준 (speaker 제한 없음).

        지원 형식:
          "all"      → 모든 활성 에이전트
          "<key>"    → 특정 에이전트
        """
        if any(t.strip().lower() == "all" for t in targets):
            return list(self.active_agents)
        resolved = []
        for t in targets:
            key = self._normalize_target(t.strip())
            if key in self.active_agents:
                resolved.append(key)
        return list(dict.fromkeys(resolved))
