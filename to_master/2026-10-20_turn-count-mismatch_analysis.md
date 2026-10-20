# 분석 보고서: 실행 기록의 턴 수 불일치

- 작성: Codex
- 작성일: 2026-10-20
- 검토 대상: `to_serve/2026-10-20_turn-count-mismatch_handoff.md`, 브랜치 `fix/turn-count-mismatch`
- 기준 커밋: `f35b867700de0dd16f0a5083903d65bf34668f3c`
- 대상 파일: `ABM/simulation/{core,turn,step,runner,headless}.py`, `ABM/db/runs.py`, `ABM/cli.py`, `backend/api/simulation/runner.py`, `backend/api/simulation/runtime/{lifecycle,load,resume,feeds}.py`, Python·JS 마크다운 내보내기
- 분석 자료: `logs_graph/simulation.db`(읽기 전용), `logs_graph/shared_log.json`
- 대상 실행 ID: `5be80eb1-9ee5-473f-b230-3d72e694252f` (`case1_v12_감염병O`)
- 실행 설정 원본: `logs_graph/simulation.db` → `simulation_runs.config_json` → 위 `run_id`의 행. 다른 실행도 아래 표의 각 `run_id`에 저장된 같은 컬럼을 기준으로 한다.
- 관련 설정 파일: `run/case1_v12_감염병O_2026-10-13_0722.json`. 이 파일은 실행 당시 DB 스냅샷과 완전히 같지 않다. 파일의 `start_agent`는 `봉미선`, DB는 `신영식`이며, `scenario_id`와 `output_format_template`도 다르므로 분석에는 DB 스냅샷을 사용했다.
- 판정: **수정 필요 — 원인 확정. 결과 표에는 1979턴을 사용한다.**
- 작업 범위: 운영 코드·기존 DB 수정 및 커밋 없음. 아래 diff는 제안이며 적용하지 않았다. `git diff master...HEAD`는 비어 있다.

## 분석 결론

`1980 = 배경문 1개 + 에이전트 응답·행동 기록 1979개`다. `Simulation`은 `shared_log`에 배경문을 넣고 시작한다. GUI·CLI 종료 처리는 `len(shared_log)`를 `simulation_runs.total_turns`에 저장하지만, `simulation_log`와 마크다운은 에이전트 기록만 센다.

따라서 차이 1건은 특정 wave의 누락 턴이 아니라 **턴이 아닌 초기 배경문**이다. 대상 실행에서 이를 실제 파일과 DB의 대응으로 확인했다. 현재 DB의 12개 실행도 모두 같은 방향으로 정확히 1씩 과대 집계되어 있다.

## 테스트 및 분석 결과

- 전체 테스트: 지정한 `C:\Users\ADMIN\miniconda3\envs\vllm\python.exe`로 `pytest.main(['tests/', '-q', '-p', 'no:cacheprovider'])` 실행.
- 결과: **911 passed, 215 subtests passed in 31.63s**, 실패 없음.
- 실행 방식: `python.exe -B -X utf8 -`의 표준입력에서 `sqlite3.connect` 감사 훅을 설치한 뒤 위 `pytest.main`을 호출했다. 두 운영 DB(`logs_graph/simulation.db`, `memory.db`)는 `mode=ro`가 아닌 연결을 거부하도록 보호했다. 테스트 자체의 임시·메모리 DB는 허용했다.
- 별도 원인 재현: 실제 `Simulation`, `RunsMixin`, GUI `finalize_run`, CLI `_finish_db_run`을 사용해 **7개 관측값 assertion 통과**. SQLite는 `:memory:`, 에이전트 JSON 로그는 임시 폴더, LLM은 고정 응답이었다. 이는 현재 결함을 재현한 결과이며 수정 후 통과 판정이 아니다.
- 마크다운 재생성: 대상 DB 로그로 현재 `render_markdown`을 호출하면 `> **Wave** 1000 · **총 턴** 1979`가 나온다. 원본 내보내기 파일의 위치는 인수인계에 없으므로 그 파일 자체를 대조했다고 주장하지 않는다.
- 운영 DB 불변 확인: 전체 테스트 전후 두 파일의 크기·수정 시각·SHA-256이 모두 같았다. 분석 연결은 `mode=ro`와 `PRAGMA query_only=ON`을 사용했다.

| 파일 | 크기(byte) | 전후 동일한 SHA-256 |
| --- | ---: | --- |
| `logs_graph/simulation.db` | 18153472 | `aa064b755dc2dd779c826145c33040cfab40fa3c94980d45153c9dbe2b1d6fb9` |
| `memory.db` | 303104 | `4d073a90e7a27c68b95fcce08b42a561b2cc2fc714e52767e2e11fba045cefd3` |

## 지적 사항

심각도: **P1** 결과·연구 해석을 바꾸는 오류, **P2** 근거·설계·검증 보완, **P3** 표현·가독성.

### [P1] 실행 메타데이터가 초기 배경문을 턴으로 센다

- 위치: `ABM/simulation/core.py:168`, `backend/api/simulation/runner.py:125`, `ABM/cli.py:441`, `ABM/db/runs.py:74`.
- 재현: 아래 **재현 A**를 저장소 루트 PowerShell에서 한 번 실행한다. 운영 DB는 읽기 전용이다.
- 실제: 대상 실행은 `total_turns=1980`, DB 기록 1979개, `shared_log` 1980개이며 그중 `speaker` 없는 배경문이 정확히 1개다.
- 기대: 실행 결과 표의 턴 수는 해당 실행에 기록된 에이전트 응답·행동 1979개다. 시나리오 배경문은 포함하지 않는다.
- 제안: 저장 시 `shared_log`의 전체 길이를 사용하지 않고 **해당 `run_id`의 `simulation_log` 행 수**를 집계한다. GUI·CLI·오류 종료를 함께 다룰 공통 위치는 `RunsMixin.finish_run`이다. 최소 diff는 아래에 제시한다.

### [P2] 같은 집계 경로는 이어서 실행·오류 종료에서도 실행별 기록 수를 보장하지 않는다

- 위치: `backend/api/simulation/runtime/lifecycle.py:168`, `backend/api/simulation/runtime/load.py:177`, `backend/api/simulation/runner.py:127`, `backend/api/simulation/runner.py:162`, `ABM/cli.py:534`; 별도 실시간 카운터는 `ABM/simulation/runner.py:547`, `ABM/simulation/runner.py:1313`.
- 재현: 아래 **재현 B**를 한 번 실행한다. `continue-2`, `parse-failure`, `error-after-turn` 출력을 확인한다.
- 실제: 1턴씩 두 번 실행하면 두 번째 실행은 DB 1행인데 메타데이터가 3이다(배경 1 + 이전 턴 1 + 현재 턴 1). 1턴 기록 후 오류 종료하면 메타데이터는 0이 된다. 파싱 실패 1회에서는 DB 0행인데 `simulation_end.total_turns`는 1이다.
- 기대: 실행 이력의 턴 수는 각 실행의 기록 수와 일치해야 한다. 시도 횟수와 기록 수를 서로 다른 지표로 명시해야 한다.
- 제안: 공통 DB 집계는 P1 제안으로 해결한다. 실시간·CLI 결과는 아래 정의에 맞춰 지표 이름과 소비처를 함께 정리한다. 단순히 `len(shared_log)-1`로 고치거나 `speaker` 필터만 넣으면 이전 실행 기록이 섞이는 문제를 해결하지 못한다.

이 경계 사례들은 현재 코드의 별도 재현에서 확인했다. **실제 DB의 12개 실행에서 관측된 +1의 원인으로 재개나 실패를 지목하는 것은 아니다.**

## 계산·저장 경로

| 경로 | 실제 계산·동작 | 의미 |
| --- | --- | --- |
| 새 실행 조립 | `ABM/simulation/headless.py:108`에서 배경문 1개 생성 | `{"role": "user", "content": "[배경] ..."}` |
| 엔진 초기화 | `ABM/simulation/core.py:168`의 `list(background_log)` | `shared_log`는 첫 턴 전부터 길이 1 |
| 에이전트 결과 기록 | `ABM/simulation/turn.py:90` append → `:121` `turn_complete` → `:143` DB 기록 호출 | 결과 파싱 후 응답·행동 기록을 추가 |
| DB 턴 기록 | `ABM/db/runs.py:137` INSERT, `:153` commit | 배경문 INSERT 경로 없음 |
| GUI 정상·중지 종료 | `backend/api/simulation/runner.py:127` | 배경·누적 기록을 포함한 `len(shared_log)` 전달 |
| CLI 정상·중지 종료 | `ABM/cli.py:442` | 동일한 `len(shared_log)` 전달 |
| DB 메타데이터 저장 | `ABM/db/runs.py:80` | 전달받은 `total_turns`를 재검산 없이 저장 |
| GUI 로그 내보내기 | `backend/api/simulation/runtime/feeds.py:31` | `speaker`가 있는 항목만 반환 |
| JS 마크다운 머리 | `frontend/js/sim/export/markdown.js:370` | 필터된 `log.length` |
| Python 마크다운 머리 | `ABM/export/markdown.py:407`, `:440` | `speaker` 필터 후 `len(log)` |
| 엔진 종료 이벤트 | `ABM/simulation/runner.py:149`, `:547`, `:1313` | 호출마다 0에서 시작해 `len(current_wave)`를 합산. 성공 여부와 무관한 배정·시도 수 |
| 실시간 UI·CLI 요약 | `frontend/js/sim/run/sse.js:201`, `ABM/simulation/headless.py:217`, `ABM/cli.py:559` | 위 종료 이벤트의 시도 수를 “턴”으로 표시 |

초기 배경문은 `turn_complete`나 `simulation_log`를 거치지 않는다. 시나리오 알림은 이벤트와 incoming으로 처리되고, 알림을 받은 에이전트가 응답하면 그 응답이 일반 턴으로 기록된다. 말 없이 행동하거나 상태에 진입한 결과도 파싱·기록 경로를 통과하면 턴에 포함된다.

`turn_complete`는 DB INSERT보다 먼저 발생한다. 또한 shared log append 이후 예외가 발생할 수 있다(`step.py:406`). 따라서 `turn_complete` 수나 `success=True` 결과 수를 영속 기록 수와 무조건 같다고 가정해서도 안 된다.

## 대상 실행에서 확인한 차이 1건

| 확인 항목 | 결과 |
| --- | --- |
| `simulation_runs.total_turns` | 1980 |
| `simulation_log` 행 수 | 1979 |
| 턴 번호 | 0~1978, distinct 1979, 중간 결번 0 |
| wave 번호 | 0~1000, distinct 1001 |
| DB 행 ID | 8322~10300 |
| `logs_graph/shared_log.json` | 1980항목: 배경문 1 + 에이전트 기록 1979 |
| 추가된 항목 | 배열 인덱스 0, `role=user`, `content="[배경] 지금은 월요일 새벽이다. 대한민국의 한 아파트에 4인 가족이 살고 있다.…"` |
| 배경문 대조 | 저장된 `config_json.background`에 `[배경] `를 붙인 문자열과 완전 일치 |
| 추가 항목의 wave·화자·이벤트 | 모두 없음. `role`, `content` 두 키만 있는 초기 입력 |
| 첫 실제 턴 | DB id 8322, wave 0, turn 0, 신영식, 월요일 오전 5시 50분. 대사는 비어 있고 침대에서 몸을 일으키는 행동 기록 |
| 마지막 실제 턴 | DB id 10300, wave 1000, turn 1978, 신영식. 화장실 불을 끄고 안방으로 돌아가는 행동 기록 |
| 대사가 빈 기록 | 184개. 턴 수에 포함되며 누락되지 않음 |

shared log의 1979개 에이전트 기록과 DB의 1979행은 `wave`, `speaker`, `content`, `action_note`, `time_str`, `location`, `is_exterior`, `infection_status`, `elapsed_minutes`, `meta`, `targets`를 대조했을 때 모두 대응했다. 비교는 병렬 실행의 완료 순서에 의존하지 않는 집합 대조이며, `(wave, speaker)` 중복도 없었다. `timestamp`는 두 기록 지점에서 따로 생성되므로 동등성 키에서 제외했다(관측 차이 0~0.236초).

wave 0에는 신짱구의 `infect_agent`에 대응하는 `scene_event`(event id 6807)와 `infection_update`(6806)가 별도로 존재한다. 이것이 추가 1턴은 아니다. 에이전트 기록 전체가 대응하고 배경문이 정확히 1개임을 직접 확인했으므로, 대상 불일치를 실패 턴·상태 진입·알림·마지막 wave의 누락으로 설명할 필요가 없다.

단, **기록 수가 맞는다는 사실은 각 턴의 이후 처리까지 모두 성공했다는 뜻은 아니다.** 이 분석은 턴 집계의 정의와 대응을 검증한 것이다.

## 다른 실행의 집계

날짜는 시작 시각의 KST 날짜다. 차이 Δ는 `total_turns - COUNT(simulation_log)`이며, 설정 출처는 각 행의 `simulation_runs.config_json`이다. 현재 DB에 남아 있는 전체 12개 실행을 조회했으며 모두 `done`, `start_wave=0`이다.

| 시작일(KST) | 실행 ID | 시나리오 | 저장 턴 수 | 로그 행 수 | Δ |
| --- | --- | --- | ---: | ---: | ---: |
| 2026-08-27 | `02d3c43c-09f5-42c4-9038-7b53fb0a8de8` | 4인가족(엄마아빠누나동생)_v3 | 109 | 108 | +1 |
| 2026-09-02 | `8e475431-8a14-4019-919a-623a8e1fc305` | 4인가족(엄마아빠누나동생)_v3 | 639 | 638 | +1 |
| 2026-09-04 | `ca2e1faf-cee8-452b-b496-4ee33b449c7d` | 4인가족(엄마아빠누나동생)_v3 | 193 | 192 | +1 |
| 2026-09-05 | `dc819c87-5bb7-47da-beee-35a6ee753100` | 4인가족(엄마아빠누나동생)_v3 | 447 | 446 | +1 |
| 2026-09-05 | `e9861c88-6978-4a6e-a4e1-9140ed10c9e0` | 4인가족(엄마아빠누나동생)_v3 | 85 | 84 | +1 |
| 2026-09-05 | `5bae785a-1951-4457-bd53-7ee7df1ffc4f` | 4인가족(엄마아빠누나동생)_v3 | 531 | 530 | +1 |
| 2026-09-06 | `eae5ced0-ee7f-4d68-8298-33f58aefc6a7` | 4인가족(엄마아빠누나동생)_v3 | 457 | 456 | +1 |
| 2026-09-06 | `5e44d4cc-2af6-4fd1-8273-a57812ae00b8` | 4인가족(엄마아빠누나동생)_v3 | 483 | 482 | +1 |
| 2026-09-06 | `5ba47908-e30f-4cac-a9f4-73b602645bf6` | 4인가족(엄마아빠누나동생)_v3 | 500 | 499 | +1 |
| 2026-09-06 | `22268cac-051d-441f-b9e0-242e4985a5b3` | 4인가족(엄마아빠누나동생)_v3 | 1800 | 1799 | +1 |
| 2026-09-08 | `793c0c25-a02c-4e01-a62e-9f32e4bf0337` | 4인가족(엄마아빠누나동생)_v3 | 889 | 888 | +1 |
| 2026-10-13 | `5be80eb1-9ee5-473f-b230-3d72e694252f` | case1_v12_감염병O | 1980 | 1979 | +1 |
| 합계 | 12개 실행 | | 8113 | 8101 | +12 |

12개 실행 모두 턴 번호는 0부터 `행 수 - 1`까지 연속이고 중복·결번이 없다. 차이가 실행 길이와 무관하게 +1인 패턴은 배경문 포함 경로와 일치한다. 과거 11개 실행의 개별 shared log 원본까지 보존되어 있다고 가정하지는 않았다.

## 권장 정의와 수정 방향

**실행 결과 표의 “턴 수”는 “해당 `run_id`의 `simulation_log`에 영속화된 에이전트 응답·행동 기록 수”로 정의할 것을 권한다.** 대상 값은 1979다. 빈 대사·독백·상태 진입 결과를 포함하고, 초기 배경·시나리오 알림 자체·아직 기록되지 않은 실패 시도는 제외한다. “성공적으로 모든 후속 처리를 마친 턴 수”라는 더 강한 의미는 현재 테이블만으로 보장하지 못한다.

별도 지표가 필요하면 현재 엔진 카운터를 `attempted_turns` 또는 “배정된 턴 수”로 명명한다. 이는 wave에 배정된 수이므로 실제 HTTP 요청 횟수와도 다를 수 있다(호출 전 중지, 언어 재시도 등). 실시간 UI와 CLI 요약의 `total_turns` 의미를 바꿀 때는 `RunResult`와 소비처를 함께 갱신한다. DB 비사용 실행에서는 해당 `run()` 시작 시점 이후의 에이전트 기록 증가분을 사용할 수 있지만, 이를 DB에 영속화된 수라고 부르지는 않는다.

다음은 공통 DB 저장 위치에 대한 **최소 diff 제안**이다. 동일 UPDATE 안에서 해당 실행의 행 수를 구하므로 정상·중지·오류 종료의 호출자가 무엇을 넘기든 저장값의 기준이 같다.

```diff
--- a/ABM/db/runs.py
+++ b/ABM/db/runs.py
@@ -74,10 +74,11 @@
         conn.execute(
             "UPDATE simulation_runs "
-            "SET status=?, total_waves=?, total_turns=?, finished_at=?, "
+            "SET status=?, total_waves=?, "
+            "total_turns=(SELECT COUNT(*) FROM simulation_log WHERE run_id=?), finished_at=?, "
             "active_agents_json=?, pending_wave_json=?, elapsed_minutes=? "
             "WHERE run_id=?",
             (
-                status, total_waves, total_turns, time.time(),
+                status, total_waves, run_id, time.time(),
                 json.dumps(list(active_agents), ensure_ascii=False) if active_agents is not None else None,
                 json.dumps(pending_wave, ensure_ascii=False) if pending_wave is not None else None,
                 elapsed_minutes,
```

이 diff는 기존 인자를 호환 목적으로 남긴 최소 제안이다. 실제 구현에서는 불필요해진 `total_turns` 인자를 호출부·테스트와 함께 정리하거나, 호환 인자임을 명시해야 한다.

이 저장 경로를 고쳐도 기존에 저장된 1980은 자동으로 바뀌지 않는다. 이번 결과 표에는 읽기 전용 COUNT를 사용한다. 과거 실행의 화면 표시도 바로잡으려면 읽기 API가 실행별 COUNT를 계산해 반환하는 방법이나 별도 감사 가능한 보정 절차를 검토한다. 이번에는 어느 방법도 적용하지 않았다.

## 최소 재현과 회귀 테스트 제안

### 재현 A — 운영 DB·JSON 읽기만 수행

저장소 루트의 PowerShell에서 다음을 실행한다. 배경문 키·내용 일치, 전체 실행의 차이, 대상 턴 번호, 1979건의 대응을 한 번에 확인한다.

```powershell
@'
import json, sqlite3
from collections import Counter
from pathlib import Path

rid = "5be80eb1-9ee5-473f-b230-3d72e694252f"
c = sqlite3.connect("file:logs_graph/simulation.db?mode=ro", uri=True)
c.execute("PRAGMA query_only=ON")
c.row_factory = sqlite3.Row
for r in c.execute("""
    SELECT r.run_id, r.total_turns, COUNT(l.id) AS logs,
           r.total_turns-COUNT(l.id) AS delta
    FROM simulation_runs r LEFT JOIN simulation_log l ON l.run_id=r.run_id
    GROUP BY r.run_id ORDER BY r.started_at
"""):
    print(dict(r))
run = c.execute("SELECT * FROM simulation_runs WHERE run_id=?", (rid,)).fetchone()
cfg = json.loads(run["config_json"])
rows = [dict(r) for r in c.execute(
    "SELECT * FROM simulation_log WHERE run_id=?", (rid,))]
shared = json.loads(Path("logs_graph/shared_log.json").read_text(encoding="utf-8"))
background = [e for e in shared if "speaker" not in e]
spoken = [e for e in shared if "speaker" in e]
print("counts:", run["total_turns"], len(rows), len(shared), len(background))
print("background keys:", list(background[0]))
print("background equals config:",
      background[0]["content"] == "[\ubc30\uacbd] " + cfg["background"])
turns = {r["turn"] for r in rows}
print("turn range, unique, gaps:",
      min(turns), max(turns), len(turns),
      sorted(set(range(min(turns), max(turns)+1)) - turns))
fields = ("wave", "speaker", "content", "action_note", "time_str",
          "location", "is_exterior", "infection_status", "elapsed_minutes")
def signature(e, db=False):
    meta = json.loads(e["meta_json"]) if db else e.get("meta")
    targets = json.loads(e["targets_json"]) if db else e.get("targets")
    return (tuple(e.get(k) for k in fields),
            json.dumps([meta, targets], sort_keys=True, ensure_ascii=False))
print("all agent records match:",
      Counter(signature(e) for e in spoken) ==
      Counter(signature(e, True) for e in rows))
c.close()
'@ | & 'C:\Users\ADMIN\miniconda3\envs\vllm\python.exe' -B -X utf8 -
```

### 재현 B — 메모리 DB와 고정 LLM 응답

운영 DB는 열지 않는다. 설정 파일 없이 아래 인라인 설정(에이전트 1명, 기본 배경 1개, fixed 1분, 1 wave, 언어 재시도 꺼짐)을 사용한다. 실행 ID는 코드에 지정한 이름이다.

```powershell
@'
import queue, sqlite3, tempfile, threading
from pathlib import Path
from ABM.agent import Agent
from ABM.simulation import Simulation
from ABM.db import SimDB
from ABM.db.schema import SCHEMA, migrate
from ABM.cli import _finish_db_run
from backend.api.simulation.runner import finalize_run, fold_elapsed_and_reset_waves

def make_db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    migrate(c)
    db = SimDB.__new__(SimDB)
    db._conn = lambda: c
    return db, c

def show(db, sim, rid, emitted):
    end = [d for t, d in emitted if t == "simulation_end"][-1]
    print(rid, "stored=", db.get_run(rid)["total_turns"],
          "rows=", len(db.get_run_log(rid)), "shared=", len(sim.shared_log),
          "simulation_end=", end["total_turns"])

with tempfile.TemporaryDirectory() as tmp:
    for name in ("fresh-gui", "fresh-cli", "background-two",
                 "parse-failure", "continue", "error-after-turn"):
        db, c = make_db()
        rid = name
        db.create_run(rid, None, name, "{}")
        folder = str(Path(tmp) / name)
        raw = "not json" if name == "parse-failure" else '{"content":"hello","target":"self"}'
        background = [{"role": "user", "content": "[background] test"}] * (
            2 if name == "background-two" else 1)
        sim = Simulation(
            {"a": Agent("a", "Act as a.", folder, token_limit=4096)},
            background, folder, llm=lambda *a, **k: (raw, "", {}),
            db=db, sim_id=rid, lang_fix_enabled=False,
            time_mode="fixed", time_per_wave=1)
        emitted = []
        original_emit = sim._emit
        def emit(t, d):
            emitted.append((t, d))
            original_emit(t, d)
        sim._emit = emit
        sim.run("a", max_waves=1, step_delay=0)
        if name == "fresh-cli":
            _finish_db_run(db, rid, sim, "done")
        else:
            error = RuntimeError("injected") if name == "error-after-turn" else None
            finalize_run(db, rid, threading.Event(), sim, queue.Queue(), error=error)
        show(db, sim, rid, emitted)
        if name == "continue":
            rid = "continue-2"
            sim._sim_id = rid
            fold_elapsed_and_reset_waves(sim)
            db.create_run(rid, None, name, "{}", start_wave=sim._wave_base)
            emitted.clear()
            sim.run("a", max_waves=1, step_delay=0)
            finalize_run(db, rid, threading.Event(), sim, queue.Queue())
            show(db, sim, rid, emitted)
        c.close()
'@ | & 'C:\Users\ADMIN\miniconda3\envs\vllm\python.exe' -B -X utf8 -
```

관측 결과(위와 같은 조건으로 실행. 분석 시 ID에는 `probe-` 접두사를 붙였음):

| 조건 | 메타데이터 | DB 행 수 | shared log | 종료 이벤트의 total_turns |
| --- | ---: | ---: | ---: | ---: |
| GUI, 새 실행 1턴 | 2 | 1 | 2 | 1 |
| CLI, 새 실행 1턴 | 2 | 1 | 2 | 1 |
| 배경 2개, 새 실행 1턴 | 3 | 1 | 3 | 1 |
| 파싱 실패 1회 | 1 | 0 | 1 | 1 |
| 이어서 실행의 첫 번째 실행 | 2 | 1 | 2 | 1 |
| 같은 객체의 두 번째 실행 | 3 | 1 | 3 | 1 |
| 1턴 저장 후 오류 종료 주입 | 0 | 1 | 2 | 1 |

수정 후 회귀 테스트는 위의 `stored == rows`를 GUI·CLI 양쪽에서 보장하도록 한다. 추가 경계는 `/load` 후 `/continue`, 새 객체로 시작하는 `/resume`, 배경 0개, 턴 0개, 중지, 빈 `content`와 유효한 `action_note`, DB 저장 전 예외다. DB 저장 전 예외에서는 먼저 발행된 `turn_complete`를 영속 기록의 증거로 삼지 않는지도 검증한다.

기존 테스트 `tests/test_regressions.py:12432`는 이미 “5턴·shared log 6항목”을 확인하지만, 배경이 포함된 객체를 종료 처리에 넘겼을 때 DB 메타데이터가 일치하는지까지 검증하지 않는다. 이 두 경로를 연결하는 테스트가 이번 결함을 직접 잡는다.

## 확인했지만 문제없는 것

- 대상 실행의 DB 로그 1979건과 shared log 에이전트 기록 1979건의 대응, 연속 턴 번호, 마지막 wave의 실제 기록.
- 배경을 제외하는 GUI 피드와 Python 마크다운, 필터된 로그 길이를 표시하는 JS 마크다운의 집계 방향.
- 대사 없는 행동도 턴 기록에서 제외되지 않는다. 대상 실행의 빈 `content` 184건을 확인했다.
- 메타데이터 `total_waves=1001`은 wave 0~1000의 1001개와 같고, `elapsed_minutes=10085`는 마지막 `time_jump.end_elapsed_minutes`와 같다. 대상에서는 같은 배경 혼입 원인에 따른 이 두 값의 불일치가 없었다.
- 연구 가정·엔진 동작·기존 DB를 변경하지 않았다. 결정 기록도 변경하지 않았다.

---

## Claude 답변 (Claude가 작성)

| 지적 | 처리 | 근거 |
| --- | --- | --- |
| P1 배경문을 턴 수에 포함 | **수용** | 직접 확인: 읽기 전용 DB에서 12개 실행, `total_turns - 로그 행 수` 합계 12(전부 +1). `core.py:168`이 `shared_log`를 배경 로그로 시작함을 확인. 제안 diff대로 `RunsMixin.finish_run`이 같은 UPDATE에서 `SELECT COUNT(*) FROM simulation_log WHERE run_id=?`를 저장. `total_turns` 인자는 호환용으로 남기고 무시함을 docstring에 명시. 회귀 테스트 `RunTurnCountTests`(새 실행·배경 2개·파싱 실패·CLI) — 수정 전 실패, 수정 후 통과. 커밋 `dcbbf15`. |
| P2 실행별 집계와 시도 수의 경계 | **부분 수용 / 일부 보류** | 수용: 저장 메타데이터는 P1 수정으로 실행별 행 수가 되어 이어서 실행(예전 3 → 1)·오류 종료(예전 0 → 1)·파싱 실패(0)가 모두 로그와 일치. 같은 테스트 클래스에 두 경계 추가(`dcbbf15`). 보류: 실시간 UI·CLI 요약의 `simulation_end.total_turns`(배정·시도 수)를 `attempted_turns`로 이름을 바꾸는 일은 프론트엔드·`RunResult`·소비처를 함께 바꾸는 별도 작업이라 이번 범위에서 제외. 과거 저장값 보정도 하지 않음 — 결과 표는 로그 행 수 COUNT를 쓴다. |


---

## 2차 검증 결과

- 작성: Codex
- 작성일: 2026-10-20
- 기준 커밋: `dcbbf159b421a46d0c4241bcc0103b61c8b82722` (`fix/turn-count-mismatch`)
- 검토 대상: `git diff master...HEAD`의 `ABM/db/runs.py`, `tests/test_regressions.py`, 위 Claude 답변과 원래 인수인계 메모. 변경은 두 파일, 77줄 추가·2줄 삭제다.
- 관련 경로 확인: GUI·CLI 종료 처리, 엔진 종료 이벤트, `RunResult`, 프론트엔드 SSE 표시. `AGENTS.md`와 `docs/decisions.md`를 먼저 확인했다.
- 작업 범위: 보고서와 아래 사용자 인계 문서 작성. 운영 코드·테스트 코드·운영 DB·결정 기록 수정 및 커밋 없음. 아래 최신 판정이 문서 앞부분의 1차 판정 **수정 필요**를 대체한다.

### 전체 테스트 및 불변 확인

- 전체 테스트 실행 ID: 도구 실행 세션 `42810`.
- 실행: 지정 환경의 `C:\Users\ADMIN\miniconda3\envs\vllm\python.exe -B -X utf8 -`에서 `pytest.main(['tests/', '-q', '-p', 'no:cacheprovider'])` 호출. 전체 `tests/`를 실행했으며 선택·제외 옵션은 없다.
- 결과: **914 passed, 218 subtests passed in 24.97s**, 종료 코드 0. 새로 추가한 `RunTurnCountTests`의 테스트 3개와 GUI 조건별 subtest 3개를 포함한다.
- 운영 DB 보호: 실행 전 `sqlite3.connect` 감사 훅을 설치해 `logs_graph/simulation.db`와 `memory.db`의 `mode=ro`가 아닌 연결을 거부했다. 임시·메모리 DB는 허용했으며, 차단된 운영 DB 연결 시도는 0건이다.
- 두 운영 DB의 크기·수정 시각·SHA-256은 테스트 전후 모두 동일했다. `logs_graph/simulation.db`: 18153472 bytes, `aa064b755dc2dd779c826145c33040cfab40fa3c94980d45153c9dbe2b1d6fb9`. `memory.db`: 303104 bytes, `4d073a90e7a27c68b95fcce08b42a561b2cc2fc714e52767e2e11fba045cefd3`.
- `git diff --check master...HEAD` 통과. 연구 가정·실행 시뮬레이션 로직 변경이 없어 결정 기록 추가 대상은 아니다.

회귀 테스트 설정은 별도 JSON 파일 없이 `tests/test_regressions.py:12433`의 `RunTurnCountTests._run`에 정의되어 있다. 에이전트 1명, 고정 LLM 응답, 언어 재시도 꺼짐, fixed 1분·1 wave, 임시 폴더의 SQLite DB를 사용한다. 아래 값은 전체 테스트에서 실행된 assertion으로 확인했다.

| 조건 / 테스트 실행 ID (`run_id`) | 저장 턴 수 | 해당 실행 로그 행 수 | 검증 근거 |
| --- | ---: | ---: | --- |
| GUI 새 실행 / `fresh` | 1 | 1 | 배경 1개가 집계에서 제외됨 |
| GUI 배경 2개 / `two-backgrounds` | 1 | 1 | 배경 수와 무관하게 로그 행 수 사용 |
| GUI 파싱 실패 / `parse-failure` | 0 | 0 | 기록되지 않은 시도를 저장 턴 수에서 제외 |
| CLI 정상 종료 / `cli` | 1 | 1 | `_finish_db_run`을 거쳐 같은 기준으로 저장 |
| 1턴 기록 후 오류 종료 주입 / `error` | 1 | 1 | 오류 경로의 전달값 0을 저장하지 않음 |
| 같은 객체로 이어서 실행 / `first` → `second` | 1 | 1 | 표의 값은 `second`; 이전 실행 기록을 포함하지 않음 |

### 지적별 재판정과 보류 사유

| 지적 / 처리 범위 | 2차 판단 | 근거 |
| --- | --- | --- |
| P1: 초기 배경문을 저장 턴 수에 포함 | **해결** | `ABM/db/runs.py:86`의 COUNT 하위 질의와 바깥 UPDATE 모두 동일한 `run_id`를 바인딩한다. 호출부의 `len(shared_log)`를 무시하므로 배경 수와 무관하며, GUI·CLI 회귀 테스트가 이를 확인한다. 호환 인자를 저장에 사용하지 않는다는 설명도 docstring에 있다. |
| P2 수용 부분: 실행별 집계·오류 종료 | **해결** | 같은 UPDATE에서 해당 실행의 영속 로그만 집계한다. 이어서 실행·오류 종료·파싱 실패 assertion이 통과했다. `status`, `total_waves`, 종료 시각, 활성 에이전트, pending wave, 경과 시간의 SQL 바인딩은 기존과 같은 순서·값을 유지한다. |
| P2 보류 부분: 실시간 UI·CLI의 배정·시도 수 명칭과 소비처 정리 | **보류 사유 타당 — 비차단 후속 과제** | `ABM/simulation/runner.py:547,1313`의 카운터는 여전히 wave 배정 수이며, `ABM/simulation/headless.py:43,217`의 `RunResult`, `frontend/js/sim/run/sse.js:201`, `ABM/cli.py:559,565`가 이를 소비한다. 명칭·의미 변경은 이 계약과 소비처를 함께 다뤄야 한다. 이번 저장 집계는 이 값을 사용하지 않아 독립적으로 수정 완료를 판정할 수 있다. 파싱 실패 등에서는 실시간 표시와 영속 기록 수가 계속 다를 수 있으므로, 이 부분까지 해결됐다고 보지는 않는다. 보류 사유만 검토했으며 추가 변경은 요구하지 않는다. |
| 과거 저장값 보정 미실시 | **보류 사유 타당** | 수정은 `finish_run` 호출 시 해당 실행만 갱신하며 과거 행을 일괄 보정하지 않는다. 기존 결과 표에 로그 행 수 COUNT를 쓰는 방법은 원인 분석과 일치하고 운영 DB 무수정 범위에도 맞는다. 대상 과거 실행의 결과 표는 계속 1979를 사용한다. 과거 화면의 저장값까지 자동으로 고쳐졌다는 의미는 아니다. 별도 신규 P1·P2로 추가하지 않는다. |

회귀 테스트와 수정 diff에서 새로운 P1·P2는 발견하지 않았다. `/load`·`/resume` 전체 HTTP 흐름 각각을 이번에 신규 종단 간 테스트로 검증했다는 뜻은 아니며, 이번 결론은 공통 저장 경로와 위 명시한 회귀 조건에 대한 것이다.

### 최신 판정

**통과 — P1과 P2의 수용 범위는 해결됐고, 나머지 P2의 보류 사유는 타당하다.**

- 미해결 P1: **없음**.
- 미해결 P2: **1건 — 실시간 UI·CLI의 배정·시도 수 지표 정리(보류 인정, 이번 수정의 통과를 막지 않음)**.
- 검증 반복은 여기서 종료한다. 남은 P2는 `AGENTS.md` 3절에 따라 [사용자 인계 요약](../to_human/2026-10-20_turn-count-mismatch_summary.md)에 남겼으며, 추가 리뷰 반복이나 운영 코드·DB 수정은 하지 않았다.
