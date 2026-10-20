# 인수인계: zone 없는 장소 간 직접 hop에 이동 시간 부여

- 작성: Claude Code
- 작성일: 2026-10-20
- 브랜치: `feat/zoneless-travel-time`
- 기준 커밋: `0263a67`(master) → `b83b6cd`(feat/zoneless-travel-time HEAD). 검토 범위: `git diff master...feat/zoneless-travel-time`
- 관련 결정: `docs/decisions.md` 2026-10-20 "zone 없는 장소는 각자 독립 구역으로 경계 판정"(이번 변경에서 추가)

## 바꾼 것
- `ABM/simulation/location.py`: 헬퍼 `_travel_zone(node)`(zone 있으면 그 zone, 없으면 노드별 고유 키 `"\0place:<이름>"`, 지도 밖·빈 위치는 `''`)와 `_crosses_boundary(a, b)`(zone 안 쓰는 지도는 항상 False) 추가. `_find_path_zoned`의 경계 횡단 비용을 헬퍼로 교체.
- `ABM/simulation/journey.py`: `_is_zone_boundary`가 `_crosses_boundary`를 호출(경유지 판정).
- `ABM/simulation/runner.py`: `_move_one_hop`의 `crossing_zone`(traveling 부여)을 `_crosses_boundary`로 교체.
- `tests/test_regressions.py`: `ZonelessTravelBoundaryTests` 7개(JourneyTests 뒤).
- `docs/simulation-features.md` §1: "경계 판정 규칙" 문단. `docs/decisions.md`: 결정 한 줄.

## 의도한 동작
- 수정 전: 경계 판정이 zone 문자열 비교라 zone 없는 장소끼리(둘 다 `''`) 직접 hop하면 경계가 아니라고 보고 즉시 도착(순간이동)했다. 현재 case1 지도는 외부 장소가 모두 `우리집`에만 연결돼 드러나지 않았고, 실측 실행 근거는 없다(지도에 외부 장소 간 직접 연결을 추가하면 생기는 잠재 결함).
- 수정 후 (zone을 하나라도 쓰는 지도):
  - zone 없는 장소 A → zone 없는 장소 B 직접 hop: 경계 → `traveling`(`zone_travel_min~max` 분).
  - 우리집(zone) ↔ 외부 장소: 기존과 같이 경계.
  - 같은 zone 안 방끼리: 기존과 같이 즉시.
  - 경로: 직접 A→B(경계 1회)가 A→거실→B(경계 2회)보다 우선. zone 없는 장소 사슬(A→C→B)은 hop마다 경계라, 집을 거치는 더 적은 경계 경로가 있으면 그쪽을 고른다.
  - 경유지: zone 없는 장소 사슬의 중간 노드가 경유지가 된다.
- zone을 전혀 안 쓰는 지도: 변화 없음(경계 없음, 즉시 이동).
- 인지(같은 zone 다른 방 사람을 앎)는 바뀌지 않는다. zone 없는 장소끼리는 서로를 모른다.

## 검증 요청
1. 전체 테스트: `C:\Users\ADMIN\miniconda3\envs\vllm\python.exe -m pytest tests/ -q`
   - Claude 쪽에서는 일부만 실행: `-k "ZonelessTravelBoundary"` 7 passed / `-k "Zone or zone or Travel or travel or Journey or journey or BringAlong or bring or Path or path or Meeting or Move or move or Location or location or Spatial or Exterior or exterior"` 236 passed, 0 failed.
2. 변경 전 코드에서 `ZonelessTravelBoundaryTests`가 실패하는지(Claude 확인: 6 failed / 3 passed — 통과 3개는 경로 선택·인지 불변식).
3. case1 지도(외부 장소가 `우리집`에만 연결)로 `_find_path`·`_path_via`·`_route_guide_lines` 결과가 변경 전과 같은지(모든 노드 쌍 비교 권장).
4. 외부 장소 간 직접 연결(예: 고등학교↔누나학원)을 추가한 지도로 짧게 시뮬레이션해 traveling·도착 씬·여정 이벤트가 자연스러운지(선택).

## 위험 지점
- **동행(bring_along)**: 리더의 `(crossing_zone, travel_minutes)`를 동행에 그대로 넘기는 경로(runner 826~835)는 그대로다. zone 없는 hop에서도 같은 이동 시간을 공유하는지 테스트했다(`test_companion_shares_leader_travel_time_on_zoneless_hop`).
- **여정·경유지**: `_path_via`가 새 기준을 따르므로 zone 없는 장소 사슬의 중간 노드가 경유지가 된다. 이동 시간 0(`zone_travel_max_minutes=0`)일 때도 `instant_via_arrivals`(runner 838~840)로 경유지 판정이 일어난다. 이전에는 이런 경로가 여정이 아니었다.
- **도착 씬·도착 알림**: 새 분기 없음. `crossing_zone`이면 즉시 도착 알림을 건너뛰고 status.py 지연 도착 경로를 탄다(기존 우리집↔외부 장소와 같은 경로).
- **접촉 그룹**: traveling 상태 기반이라 판정 결과만 따른다(이동 중엔 동행끼리만). zone을 직접 비교하는 코드 없음.
- **외부 공간(is_exterior)끼리 직접 hop**(동네→공원): 이제 traveling. 외부 공간 사람에게는 씬이 원래 안 가고 본인 도착 씬만 간다(우리집→동네와 같음). 테스트 subTest로 확인.
- **경로 비용 변화**: zone 없는 장소를 2개 이상 이어 지나는 경로가 있는 지도에서는 경로 선택이 바뀔 수 있다(경계가 적은 쪽 우선). 현재 case1 지도에는 해당 경로가 없다.
- **지도 밖 위치**: `_travel_zone`은 그래프에 없는 노드(빈 위치 포함)를 `''`로 둔다 — 위치 미설정 에이전트의 첫 이동 판정은 예전과 같다. 이 예외가 맞는지 확인 부탁.
- **인지 판정 분류(바꾸지 않음)**:
  - location.py `_compute_zone_awareness`(433/448): 인지 — 유지.
  - location.py 545~550 `(구역 밖)` 표시: 내가 zone 안에 있을 때의 표시 — 유지.
  - location.py 594~598 `[같은 구역의 다른 곳]`: 인지 출력 — 유지.
  - journey.py `_route_guide_lines` 그룹핑(107~113): 표시. `zone or first`로 이미 zone 없는 노드를 개별 그룹으로 묶음 — 유지.
  - meeting.py 154~156 `_resolve_meet_target`: 인지 — 유지.
  - meeting.py 284~287 `_leaves_own_zone`: **인지로 분류해 유지.** docstring이 "zone은 인지 범위의 벽, `_meeting_break_reason()`과 대칭"이라고 명시. 대칭 쌍(316~318)과 기준이 갈리면 안 된다. 차이가 생길 수 있는 경우는 대상이 zone 없는 노드에 있을 때뿐이고, 그때 추격자는 같은 노드에 있어야 추격이 성립하므로 동작은 flat 지도와 같다.
  - meeting.py 316~318 `_meeting_break_reason`: 인지 — 유지.

## 범위 밖
- 계약 문구: `_route_guide_lines` 꼬리 "(구역을 넘을 때마다 약 N~M분)"과 `_MAP_RULE_JOURNEY`의 "다른 구역" 표현은 그대로 둠(zone 없는 장소도 독립 구역이라 의미상 맞다고 판단). 표현 개선은 P3.
- 대상이 zone 없는 노드에서 다른 zone 없는 장소로 갈 때 추격자가 목적지를 따라갈지(`_meeting_goal_node`)는 별도 설계 질문으로 남김.
- 이동 시간을 거리별로 다르게 주는 기능은 하지 않음(모든 경계 hop이 같은 범위).
