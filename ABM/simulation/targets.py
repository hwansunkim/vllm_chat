import logging

logger = logging.getLogger(__name__)


class _TargetsMixin:
    """에이전트 target 해석 및 가시성 관련 메서드."""

    def _build_visible_targets(self, agent_groups: dict[str, list[str]]) -> dict[str, list[str]]:
        """에이전트별 <TARGETS>에 노출할 다른 에이전트 key 목록 계산.

        groups가 빈 에이전트는 모든 에이전트를 볼 수 있음 (하위 호환).
        groups가 있으면 같은 그룹에 속한 에이전트만 노출.
        """
        all_keys = list(self.agents.keys())
        result: dict[str, list[str]] = {}
        for key in all_keys:
            groups = agent_groups.get(key, [])
            if not groups:
                result[key] = [k for k in all_keys if k != key]
            else:
                visible: set[str] = set()
                for gid in groups:
                    for other_key in all_keys:
                        if gid in agent_groups.get(other_key, []):
                            visible.add(other_key)
                visible.discard(key)
                result[key] = sorted(visible)
        return result

    def _normalize_target(self, t: str) -> str:
        """한국어 display_name → 시스템 key 정규화. 이미 key면 그대로 반환."""
        if t in self.agents:
            return t
        return self._alias_to_key.get(t, t)

    def _get_visible_sections(
        self, agent_key: str, visible_agents: list[str]
    ) -> list[tuple[str, list[str]]] | None:
        """그룹 소속 에이전트의 <TARGETS>를 그룹-섹션으로 분류.

        그룹 미설정 에이전트는 None 반환 (flat 목록 유지, 하위 호환).
        """
        my_groups = self._agent_groups.get(agent_key, [])
        if not my_groups:
            return None

        sections: list[tuple[str, list[str]]] = []
        seen: set[str] = set()
        for gid in my_groups:
            members = [
                k for k in visible_agents
                if k not in seen and gid in self._agent_groups.get(k, [])
            ]
            if members:
                sections.append((gid, members))
                seen.update(members)

        ungrouped = [k for k in visible_agents if k not in seen]
        if ungrouped:
            sections.append(("기타", ungrouped))

        return sections or None

    def _resolve_targets(self, targets: list[str], speaker_key: str) -> list[str]:
        """발화 target 해석 — active_agents 기준, 위치 기반 필터링 적용.

        지원 형식:
          "all"        → 화자와 같은 위치의 활성 에이전트
          "group:X"    → 그룹 X 소속 + 같은 위치 에이전트
          "stranger_N" → 해당 낯선 이 (real_key 변환 + knowledge 확장)
          "<key>"      → 특정 에이전트 (같은 위치일 때만)
        위치 미설정 에이전트는 기존 동작 유지 (하위 호환).
        외부 공간 에이전트는 아무에게도 메시지를 전달할 수 없음.

        ``perception_mode == "spatial"``이면 `<key>`/`stranger_N` **직접 타깃**에
        한해 "같은 zone의 다른 방"까지 도달 범위를 넓힌다(`_reachable`). 벽 너머로
        목소리는 들리기 때문이다. `all`/`group:X`는 이때도 **같은 방 한정**이다 —
        zone 전체를 향해 "모두"를 외치는 건 범위가 너무 넓다.

        **반환 타입은 언제나 플랫 `list[str]`이다.** runner.py(라우팅)와
        turn.py(`_apply_turn_result`의 관계 그래프 edge 생성)가 둘 다 이 형태를
        기대하므로 절대 바꾸지 말 것. "같은 방인지 원거리인지"의 구분이 필요한
        호출부는 `_is_remote_target()`으로 개별 조회한다.
        """
        speaker_loc = self._agent_location.get(speaker_key, "")
        if speaker_loc in self._exterior_locations:
            return []  # 외부 공간 화자 — 메시지 전달 불가

        resolved    = []
        visible_set = set(self._visible_targets.get(speaker_key, []))

        def _same_loc(key: str) -> bool:
            """위치 시스템 활성 시 같은 위치인지 확인. 위치 미설정이면 항상 True."""
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

        spatial = self._perception_mode == "spatial"

        def _can_address(key: str) -> bool:
            """직접 타깃(`<key>`/`stranger_N`) 전용 도달 판정.

            spatial 모드에서만 zone 완화(`_reachable`)를 먼저 본다. 순서가 중요하다 —
            `_same_loc`을 먼저 부르면 원거리로 도달 가능한 타깃에도 "location
            mismatch" 드롭 로그가 남아 추적을 흐린다.
            """
            if spatial and self._reachable(speaker_loc, key):
                return True
            return _same_loc(key)

        for t in targets:
            t_s = t.strip()
            if t_s.lower() in ("self", "system"):
                continue
            elif t_s.lower() == "all":
                my_groups = self._agent_groups.get(speaker_key, [])
                if my_groups:
                    candidates = (k for k in visible_set if k in self.active_agents)
                else:
                    candidates = (k for k in self.active_agents if k != speaker_key)
                resolved.extend(k for k in candidates if _same_loc(k))
            elif t_s.lower().startswith("group:"):
                gid = t_s[6:]
                resolved.extend(
                    k for k in visible_set
                    if k in self.active_agents
                    and gid in self._agent_groups.get(k, [])
                    and _same_loc(k)
                )
            elif t_s.startswith("stranger_"):
                # stranger_N ID는 같은 장소에서뿐 아니라 같은 zone의 다른 장소를
                # 인지할 때도 발급된다. targeted 모드의 대화 가능 범위는 어디까지나
                # "같은 장소"이므로 _same_loc()로 걸러야 zone 인지가 대화 채널로
                # 새지 않는다. spatial 모드에서만 _can_address()가 같은 zone의 다른
                # 방까지 열어준다(대사만 — 행동은 runner.py가 떼어낸다).
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

    # ── 공간 기반 인지 (perception_mode == "spatial") ──────────────────────────
    #
    # 아래 네 헬퍼는 `perception_mode == "spatial"`일 때만 의미가 있다.
    # `_reachable`/`_is_remote_target`은 모드 플래그를 스스로 확인하므로,
    # targeted 모드에서 실수로 불려도 기존 판정을 바꾸지 않는다.

    def _reachable(self, speaker_loc: str, key: str) -> bool:
        """spatial 모드에서 직접 타깃 도달 가능 여부.

        같은 방이거나(항상) 같은 zone의 다른 방(원거리)이면 True. zone이 없는 방,
        다른 zone, 외부 공간(exterior)은 False.

        `_same_loc`과 달리 로그를 남기지 않는다 — 이건 "완화 조건"이라 False가
        곧 드롭을 뜻하지 않고, 호출부(`_can_address`)가 `_same_loc`으로 한 번 더
        본다.
        """
        if not speaker_loc:
            return True                       # 위치 미설정 화자 — 하위 호환(_same_loc과 동일)
        if speaker_loc in self._exterior_locations:
            return False                      # 외부 공간은 완전 격리
        other_loc = self._agent_location.get(key, "")
        if not other_loc:
            return True                       # 위치 미설정 상대 — 하위 호환
        if other_loc in self._exterior_locations:
            return False
        if speaker_loc == other_loc:
            return True                       # 같은 방
        my_zone = self._location_zone.get(speaker_loc, "")
        if not my_zone:
            return False                      # zone 미설정 방끼리는 벽이 그대로다
        return self._location_zone.get(other_loc, "") == my_zone

    def _is_remote_target(self, speaker_key: str, target_key: str) -> bool:
        """해석된 타깃이 '같은 방'이 아니라 '같은 zone의 다른 방'(원거리)인지.

        `_resolve_targets`의 반환 타입을 바꾸지 않기 위한 짝 함수다 — runner.py가
        resolved 리스트를 순회하며 타깃마다 이걸 물어보고 전달 포맷을 나눈다
        (원거리는 대사만, 행동 없음). targeted 모드에서는 항상 False라 호출부의
        분기가 통째로 죽는다.
        """
        if self._perception_mode != "spatial":
            return False
        speaker_loc = self._agent_location.get(speaker_key, "")
        other_loc   = self._agent_location.get(target_key, "")
        if not speaker_loc or not other_loc or speaker_loc == other_loc:
            # 위치 미설정(레거시)은 "같은 방"으로 취급 — _same_loc/_reachable과 동일.
            return False
        if speaker_loc in self._exterior_locations or other_loc in self._exterior_locations:
            return False
        my_zone = self._location_zone.get(speaker_loc, "")
        return bool(my_zone) and self._location_zone.get(other_loc, "") == my_zone

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
            elif low.startswith("group:"):
                label = t_s[6:] or "모두"
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
                elif self._reachable(self._agent_location.get(observer_key, ""), real_key):
                    # 관찰자가 실제로 지각할 수 있는 범위(같은 방, 또는 같은 zone —
                    # `_reachable`은 direct-target 도달성과 같은 기준)에 있는 사람이면
                    # 새 stranger_N을 발급해도 안전하다 — 관찰자가 그 사람의 존재를
                    # 몰랐다면 이 순간 처음 알게 된 것이므로 `_meeting_label()`의
                    # 정상적인 발급 경로를 그대로 탄다.
                    label = self._meeting_label(observer_key, real_key)
                else:
                    # 관찰자의 지각 범위 밖(다른 zone 등)이면 ID를 새로 발급하지
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

        **하위 호환 설계 — 보수적 판정.** knowledge 검사를 무조건 걸면 위치/그룹
        시스템을 쓰지 않는 레거시 시나리오를 깨뜨릴 위험이 있으므로, "stranger_N ID가
        실제로 발급된 적 있는 상대"에게만 knowledge를 요구한다. ID는 관찰자가 그
        사람을 익명으로 본 적이 있을 때만(_compute_wave_targets / _compute_zone_awareness)
        발급되므로:

          - 그룹 미설정 시나리오: core.py가 knowledge에 전원을 넣어두므로 애초에 낯선
            이가 없고 ID도 발급되지 않는다 → 항상 False → 기존 동작 그대로.
          - 위치 미설정 + 그룹 설정 시나리오: 위치가 비어도 _compute_wave_targets는
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
          "group:X"  → 그룹 X 소속 활성 에이전트 전원
          "<key>"    → 특정 에이전트
        """
        if any(t.strip().lower() == "all" for t in targets):
            return list(self.active_agents)
        resolved = []
        for t in targets:
            t_s = t.strip()
            if t_s.lower().startswith("group:"):
                gid = t_s[6:]
                for key in self.active_agents:
                    if gid in self._agent_groups.get(key, []):
                        resolved.append(key)
            else:
                key = self._normalize_target(t_s)
                if key in self.active_agents:
                    resolved.append(key)
        return list(dict.fromkeys(resolved))
