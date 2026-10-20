# 검증 보고서: zone 없는 장소 간 직접 hop의 이동 시간

- 작성: Codex
- 작성일: 2026-10-20
- 검토 대상: `to_serve/2026-10-20_zoneless-travel-time_handoff.md`, 브랜치 `feat/zoneless-travel-time`
- 기준 커밋: `b83b6cd7ba0134e0b3d2ef1a673f0c8005763fa5`
- 비교 기준: `master` = `0263a6727307a67d95409d1e53d2c06d2ddac201`
- 검토 범위: `git diff master...feat/zoneless-travel-time`의 6개 파일 — `ABM/simulation/location.py`, `ABM/simulation/journey.py`, `ABM/simulation/runner.py`, `tests/test_regressions.py`, `docs/simulation-features.md`, `docs/decisions.md`
- 적용 지침: `AGENTS.md`, `docs/decisions.md`, `to_master/_TEMPLATE_review.md`
- 검토 회차: 1차
- 판정: **수정 필요 — P2 1건**
- 최신 판정(2차, `e4cd9b8`): **통과 — P2-1 해결.** 아래 「2차 검증 결과」 참조. 위 기준 커밋과 이하 1차 기록은 당시 결과를 보존한다.

zone 없는 실제 장소 사이에 이동 시간을 부여하는 핵심 동작은 확인했다. 전체 테스트와 case1 v13 지도 비교도 통과했다. 다만 인계 문서와 코드 주석에서 유지한다고 한 **위치 미설정·지도 밖 위치에서의 첫 이동**이 달라졌다. 이 예외를 보완한 뒤 다시 검증하는 것이 적절하다. 현재 case1 v13처럼 모든 초기 위치가 지도 안에 지정된 설정에서는 이 문제가 발생하지 않는다.

## 테스트 결과

아래 R1~R6는 이 보고서의 로컬 검증 실행 식별자다. 운영 DB의 시뮬레이션 실행 ID가 아니다. 짧은 실행은 임시 디렉터리와 고정 응답 LLM 스텁을 사용했으며, 운영 시나리오·DB·코드는 수정하지 않았다.

### R1 — 전체 테스트

- 명령: `C:\Users\ADMIN\miniconda3\envs\vllm\python.exe -m pytest tests/ -q`
- 설정/입력: `tests/`의 테스트 및 fixture
- 결과: **910 passed, 213 subtests passed, 1 warning / 22.36초**
- 경고: `.pytest_cache/v/cache/nodeids`에 쓰는 과정의 `WinError 5` 권한 경고 1건. 테스트 실패는 없다.
- 추가 확인: `git diff --check master...feat/zoneless-travel-time` 통과.

### R2 — 변경 전 코드에서 새 회귀 테스트가 실패하는지

- 대상: `tests/test_regressions.py::ZonelessTravelBoundaryTests`
- 방법: `git show 0263a67:ABM/simulation/{location,journey,runner}.py`로 변경 전 정의를 읽어 별도 Python 프로세스에서 메모리에 로드했다. 변경된 세 메서드를 이전 구현으로 교체하고, 당시 없던 두 헬퍼를 제거한 상태에서 현재 테스트를 실행했다. 체크아웃과 파일 수정은 하지 않았다.
- pytest 인자: `tests/test_regressions.py::ZonelessTravelBoundaryTests -q -p no:cacheprovider --tb=short`
- 결과: **6 failed, 3 passed** — 인계 문서의 수치와 일치.
- 실패 해석:
  - 직접 hop 두 사례: `traveling` 진입이 없어 실패.
  - 동행 사례: 리더의 `traveling` 상태가 없어 실패.
  - zone 없는 장소 사슬: 경유지가 `[]`여서 실패.
  - 나머지 두 실패는 이전 코드에 `_crosses_boundary`가 없어서 발생한 `AttributeError`다. 따라서 실패 6건을 모두 독립적인 행동 결함 검증으로 해석하지는 않는다.

### R3 — case1 v13의 모든 노드 쌍 비교

설정 파일:

- `run/case1_v13_감염병O_2026-10-19_1408.json`
- `run/case1_v13_감염병X_2026-10-19_1408.json`

각 파일의 원본 `location_graph`를 Simulation으로 전개한 뒤 변경 전후 구현을 대조했다. 같은 출발지·목적지도 포함했다.

| 설정 | 노드 수 | 방향 있는 출발지·목적지 쌍 | `_find_path`·`_path_via` 차이 | 출발지별 `_route_guide_lines` 차이 |
| --- | ---: | ---: | ---: | ---: |
| case1 v13 O | 14 | 196 | 0 | 0 / 14 |
| case1 v13 X | 14 | 196 | 0 | 0 / 14 |

현재 case1 지도에서는 경로 선택·경유지·경로 안내가 유지된다는 주장을 확인했다. 이 비교는 해당 세 메서드의 출력 비교이며, 확률적 LLM 실행 전체가 동일하다는 의미는 아니다.

### R4 — 외부 장소 직접 연결을 추가한 짧은 실행

- 원본 설정 경로: `run/case1_v13_감염병O_2026-10-19_1408.json`
- 검증 설정: 원본 지도 사본에 `고등학교 ↔ 누나학원` 직접 연결만 추가. 이동자 z는 고등학교, 관찰자 b는 누나학원, 시간 진행용 m·c는 안방에 배치했다. 이동 시간은 12분, 일반 시간 범주는 5분 고정, 최대 8 wave로 실행했다.
- 원본 JSON은 수정하지 않았다. 감염병 생활 시나리오 전체를 재실행한 것이 아니라 지도·이동·도착 알림을 검증하는 스텁 실행이다.

| 시점 | 관찰 결과 |
| --- | --- |
| W0, 0분 | 고등학교 → 누나학원 직접 hop 1회, `traveling=12분` 진입 |
| W1, 5분 / W2, 10분 | `traveling` 유지. 다음 시간 경계는 12분으로 제한됨 |
| W3, 12분 | `traveling` 해제, 본인에게 `[씬] 누나학원에 도착했다.` 전달 |
| W4, 17분 | 도착지의 b에게 z의 도착 씬 전달 |

거실을 경유하지 않았고, 중간 경유지가 없는 직접 hop이므로 `journey_update`도 생성되지 않았다. 타인에게 도착 씬이 다음 wave에 전달되는 것은 기존 지연 알림 경로와 일치한다.

### R5 — zone 없는 장소 사슬의 경유·정지·재출발 및 경로 비용

- 설정 경로: 별도 JSON 없음. 인메모리 최소 지도 `hall(zone=home)`와 `A ↔ B ↔ C`를 구성했다. `hall`은 사슬과 연결하지 않았다.
- 공통 설정: `tests/test_regressions.py`의 `_ScriptedLLM` 사용, 일반 시간 범주 5분, 최대 12 wave.
- B에 다른 사람이 있는 경우, z가 도착 후 대화하고 다시 `move_to=C`를 선택하도록 스텁 응답을 지정했다.

| 이동 시간 | B의 다른 사람 | 여정 이벤트 결과 |
| --- | --- | --- |
| 12분 | 없음 | W0 `start` → W3 `pass` → W6 `arrive` |
| 12분 | 있음 | W0 `start` → W3 `pause` → W5 `resume` → W8 `arrive` |
| 0분 | 없음 | W0 `start`·`pass` → W1 `arrive` |
| 0분 | 있음 | W0 `start`·`pause` → W2 `resume`·`arrive` |

네 경우 모두 C에 도착했다. 이동 시간이 0이어도 경유지 판정은 유지됐다.

별도 인메모리 지도에서 `A → C → D → B`의 zone 없는 장소 사슬과 `A → H(zone=home) → B`를 함께 제공했다. 변경 전에는 앞의 3 hop 경로, 변경 후에는 경계 횡단이 더 적은 H 경유 2 hop 경로를 선택했다. 인계 문서의 경로 비용 변경 설명과 일치한다.

### R6 — 위치 미설정·지도 밖 위치의 첫 이동

- 설정 경로: 아래 P2-1 재현 명령에 포함한 인라인 최소 지도.
- 검증: 동일한 Simulation 입력에 대해 `_move_one_hop`만 기준 커밋과 현재 커밋의 구현으로 각각 실행했다. 이 사례의 `_find_path`는 양쪽 모두 목적지 한 칸을 반환한다.
- 결과: 빈 위치 및 `off_map`에서 zone 없는 `school`로 이동하면 변경 전에는 즉시 도착했으나, 현재는 12분 `traveling`이 생겼다.

## 지적 사항

심각도: **P1** 결과·연구 해석을 바꾸는 오류, **P2** 근거·설계·검증 보완, **P3** 표현·가독성.

### [P2-1] 위치 미설정 에이전트의 첫 이동에 새 이동 시간이 붙어, 명시한 호환성 예외를 지키지 못함

- 위치: `ABM/simulation/location.py:96`의 `_crosses_boundary`, 호출부 `ABM/simulation/runner.py:1390`. 관련 설명은 `location.py:81` 및 인계 문서의 「지도 밖 위치」 항목.
- 조건: zone이 하나 이상 있는 지도, 초기 위치가 빈 문자열 또는 지도 밖 이름, 첫 목적지는 지도 안의 zone 없는 장소, 이동 시간 설정이 0보다 큼.
- 재현: 저장소 루트 PowerShell에서 아래 명령을 한 번 실행한다. 기존 구현은 `git show`로 읽고 메모리에서만 교체한다.

```powershell
$env:PYTHONUTF8='1'
@'
import json, logging, subprocess, tempfile
from unittest.mock import patch
from ABM.agent import Agent
from ABM.simulation import Simulation
from ABM.simulation.runner import _RunnerMixin
logging.disable(logging.CRITICAL)
scope={'__name__':'ABM.simulation.review_old_runner','__package__':'ABM.simulation'}
exec(subprocess.check_output(['git','show','0263a67:ABM/simulation/runner.py']).decode('utf-8'),scope)
head_move=_RunnerMixin._move_one_hop
old_move=scope['_RunnerMixin']._move_one_hop
graph=[{'name':'hall','zone':'home','is_zone_entry':True,'connects_to':['school']},
       {'name':'school','connects_to':['hall']}]
def llm(messages,**kwargs):
    return json.dumps({'content':'go','target':'self','move_to':'school'}),'',{}
for origin in ('','off_map'):
    for label,method in [('base',old_move),('head',head_move)]:
        with tempfile.TemporaryDirectory() as tmp,patch.object(_RunnerMixin,'_move_one_hop',method):
            sim=Simulation({'a':Agent('a','Actor',tmp)},[{'role':'user','content':'Review'}],
                tmp,llm=llm,location_graph=graph,agent_locations={'a':origin},
                zone_travel_min_minutes=12,zone_travel_max_minutes=12)
            events=[]
            sim._emit=lambda event,data:events.append((event,data))
            sim.run('a',max_waves=1,step_delay=0.0,resume_wave={'a':[]})
            minutes=[d['minutes'] for event,d in events if event=='agent_status_change'
                     and d.get('state')=='traveling' and d.get('action')=='enter']
            print(label,repr(origin),sim._agent_location['a'],minutes)
'@ | & 'C:/Users/ADMIN/miniconda3/envs/vllm/python.exe' -
```

- 실제:

```text
base '' school []
head '' school [12]
base 'off_map' school []
head 'off_map' school [12]
```

- 기대: 인계 문서에서 명시한 기존 동작 보존에 따르면 이 두 첫 이동은 계속 즉시 도착해야 한다. 반면 위치 미설정 → 실제 zone 안 장소는 기존부터 경계로 처리했으므로 그 동작도 보존해야 한다.
- 원인: 지도 밖 위치의 `_travel_zone`은 `''`로 유지했지만, 목적지의 값이 기존 `''`에서 `'\0place:school'`로 바뀌었다. 한쪽 값만 유지하는 것으로는 비교 결과가 유지되지 않는다.
- 영향: `AgentConfig.location`은 빈 문자열을 허용하고 기본값도 빈 문자열이다(`backend/api/simulation/schemas.py:23`). 이 설정에서 첫 이동 뒤의 대화 가능 시각·도착 씬·접촉 그룹이 달라진다. 현재 case1 v13에는 해당 입력이 없어 R3과 기존 테스트가 이를 잡지 못한다.
- 제안: 지도에 존재하는 노드끼리의 hop에 새 독립 구역 판정을 적용하고, 지도 밖 위치가 포함된 예외는 기존 zone 문자열 비교를 유지한다. 아래는 **제안 diff이며 운영 코드에 적용하지 않았다.**

```diff
     def _crosses_boundary(self, a: str, b: str) -> bool:
         ...
         if not self._location_zone:
             return False
+        if a not in self._location_graph or b not in self._location_graph:
+            return self._location_zone.get(a, "") != self._location_zone.get(b, "")
         return self._travel_zone(a) != self._travel_zone(b)
```

회귀 테스트에는 빈 위치·지도 밖 위치 각각에 대해 **zone 없는 목적지 → 즉시**, **zone 안 목적지 → 기존 이동 시간 유지**를 추가하는 것이 좋다. 실제 zone 없는 장소 A→B에 이동 시간이 붙는 기존 새 테스트도 함께 유지한다.

## 확인했지만 문제없는 것

- 이동 시간·경로 비용·경유지 판정이 `_crosses_boundary`로 통합됐고, 실제 zone 없는 장소를 각각 독립 구역으로 보는 결정과 일치한다.
- 같은 zone 안 이동과 zone을 전혀 쓰지 않는 지도의 즉시 이동은 유지된다. 새 회귀 테스트도 현재 코드에서 통과한다.
- 동행은 같은 이동 시간을 공유하며, `is_exterior=True`인 장소 사이의 직접 hop에도 이동 시간이 부여된다. 전체 테스트에 포함된 새 사례로 확인했다.
- 인지·만남 관련 zone 문자열 비교는 변경되지 않았다. zone 없는 장소 사이에 새로 상호 인지를 부여하지 않으며, 새 인지 불변식 테스트와 기존 관련 테스트가 통과했다.
- 직접 연결한 case1 지도에서 도착 전에 도착 씬을 보내거나 거실을 잘못 경유하는 현상은 관찰되지 않았다.
- 경유지에 사람이 있을 때의 정지·재출발, 없을 때의 통과, 이동 시간 0 설정의 경유지 처리도 스텁 실행에서 확인했다.
- `docs/decisions.md`에 이번 공간 모델 변경이 같은 커밋으로 기록돼 있다.

## 검증 범위와 재검증 요청

실제 외부 LLM을 호출하는 장시간 생활 시뮬레이션은 실행하지 않았다. 따라서 자유 생성 대화의 자연스러움까지 판정하지는 않는다. 위 짧은 실행은 이동·도착 알림·여정 이벤트의 엔진 동작을 검증한 것이다.

P2-1을 직접 재현해 확인한 뒤 예외 처리와 회귀 테스트를 보완하고, 수정 커밋을 아래 답변 절에 기록해 주기 바란다. 2차 검토에서는 수정 사례와 전체 테스트를 확인하면 된다.

---

## Claude 답변 (Claude가 작성)

| 지적 | 처리 | 근거 |
| --- | --- | --- |
| P2-1 위치 미설정·지도 밖 위치의 첫 이동 호환성 | **수용** | 재현 확인: 수정 전 `_crosses_boundary('', 'school')`·`('off_map', 'school')` 모두 True. 제안 diff대로 지도 밖 위치가 낀 hop은 예전 zone 문자열 비교 유지(`location.py` `_crosses_boundary`). 수정 후 `('', 'school')` False, `('off_map', 'school')` False, `('', 'hall')` True, `('hall', 'school')` True. 회귀 테스트 `test_unset_or_off_map_origin_keeps_previous_boundary_rule`(빈 위치·지도 밖 × zone 없는/zone 안 목적지, 양방향) 추가. `-k ZonelessTravelBoundary` 8 passed. 수정 커밋 `e4cd9b8`. 전체 스위트는 2차 검증에서 확인 요청. |

---

## 2차 검증 결과

- 작성: Codex
- 작성일: 2026-10-20
- 검토 대상: P2-1 수정 커밋 `e4cd9b8`, 브랜치 `feat/zoneless-travel-time`
- 기준 커밋(검증한 HEAD): `e4cd9b8d10e2697fd0f8bf8519f2f3252cab4490`
- 1차 검토 커밋: `b83b6cd7ba0134e0b3d2ef1a673f0c8005763fa5`
- 수정 파일: `ABM/simulation/location.py`, `tests/test_regressions.py`
- 판정: **통과 — P2-1 해결, 미해결 P1·P2 없음**

### 수정 내용 확인

`git show e4cd9b8`로 실제 변경을 확인했다. `_crosses_boundary`는 두 위치 중 하나라도 그래프에 없으면 기존 zone 문자열 비교를 사용한다. 그래프 안 노드끼리는 독립 구역 판정을 그대로 사용하고, zone을 전혀 쓰지 않는 지도에 대한 기존 분기도 유지한다.

추가한 `test_unset_or_off_map_origin_keeps_previous_boundary_rule`는 빈 위치·지도 밖 위치 각각에 대해 zone 없는 목적지와 zone 안 목적지를 양방향으로 확인한다. 실제 zone 없는 장소끼리의 경계 판정도 함께 확인한다. 이 테스트를 포함한 전체 스위트가 통과했으며, 별도의 짧은 실행으로 실제 이동 시간 부여까지 확인했다.

### R7 — 수정 후 전체 테스트

- 실행 식별자: `R7`(본 보고서의 로컬 검증 식별자, 운영 DB 실행 ID 아님)
- 설정/입력 경로: `tests/`의 테스트 및 fixture
- 명령: `C:\Users\ADMIN\miniconda3\envs\vllm\python.exe -m pytest tests/ -q`
- 결과: **911 passed, 215 subtests passed, 1 warning / 22.54초**
- 경고: 1차와 동일한 `.pytest_cache/v/cache/nodeids` 쓰기 권한 경고(`WinError 5`) 1건. 테스트 실패는 없다.
- 1차 대비 테스트 1개와 하위 사례 2개가 증가했으며, 추가 회귀 테스트와 일치한다.
- 추가 확인: `git diff --check master...feat/zoneless-travel-time` 통과.

### R8 — 1차 보고서의 P2-1 재현 명령 재실행

- 실행 식별자: `R8`(로컬 검증)
- 설정/입력 경로: 본 보고서 「P2-1」의 PowerShell 코드 블록에 포함한 인라인 최소 지도.
- 방법: 보고서에 저장된 Python 재현 코드를 그대로 추출해 지정된 `vllm` 환경에서 실행했다. `base`는 `0263a67`의 `_move_one_hop`, `head`는 수정 후 현재 구현이다. 교체는 별도 프로세스의 메모리에서만 수행했다.
- 결과:

```text
base '' school []
head '' school []
base 'off_map' school []
head 'off_map' school []
```

두 경우 모두 변경 전과 같이 즉시 도착한다. 1차에서 관찰한 추가 12분 `traveling`은 더 이상 발생하지 않는다.

### R9 — 실제 이동 동작과 기존 규칙 유지

- 실행 식별자: `R9`(로컬 검증)
- 설정/입력 경로: 별도 JSON 없음. 아래 최소 지도를 메모리에서 구성했다.
- 지도: `hall ↔ room`은 `zone=home`, `hall ↔ school ↔ academy`에서 school·academy는 zone 없는 장소. hall을 home의 입구로 지정했다.
- 실행 조건: 사례마다 임시 디렉터리와 에이전트 1개, `move_to`를 반환하는 고정 응답 LLM, `zone_travel_min_minutes=zone_travel_max_minutes=12`, `max_waves=1`, `step_delay=0`.
- 확인 항목: 실제 목적지 도착 위치와 `agent_status_change`의 `traveling` 진입 이벤트. **7개 사례 모두 기대값과 일치했다.**

| 출발 위치 | 목적지 | 실제 이동 시간 이벤트 | 판정 |
| --- | --- | --- | --- |
| 빈 문자열 | school(zone 없음) | 없음 — 즉시 | 기존 동작 복원 |
| off_map(지도 밖) | school(zone 없음) | 없음 — 즉시 | 기존 동작 복원 |
| 빈 문자열 | hall(zone=home) | 12분 | 기존 동작 유지 |
| off_map(지도 밖) | hall(zone=home) | 12분 | 기존 동작 유지 |
| school | academy | 12분 | 이번 기능 유지 |
| hall | room | 없음 — 즉시 | 같은 zone 규칙 유지 |
| hall | school | 12분 | 기존 구역 경계 이동 유지 |

### 최종 처리

P2-1을 해결로 판정한다. 2차 검증 범위에서 추가 P1·P2 지적은 없다. `AGENTS.md`의 최대 2회 협업 루프에 따라 검증을 종료하며, Claude가 머지할 수 있는 상태다.

검증은 코드 확인·전체 테스트·고정 응답을 이용한 짧은 이동 실행까지 수행했다. 외부 LLM을 호출하는 장시간 시뮬레이션은 실행하지 않았다. Codex는 이 보고서만 갱신했으며 운영 코드 수정·커밋·머지는 하지 않았다.
