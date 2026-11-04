# 검증 보고서: CLI resume 및 실패 wave 시계 정지

- 작성: Codex
- 작성일: 2026-11-02
- 검토 대상: `to_serve/2026-11-02_cli-resume_handoff.md`, 브랜치 `cli-resume`의 `git diff HEAD` 전체와 미추적 `tests/test_cli_resume.py`
- 기준 커밋: `e90cd19dcce78dde6ddf05af28c73dd01fe881af` + 작업 트리
- 대상 파일: ABM/cli.py, ABM/db/{runs,schema}.py, ABM/simulation/{headless,runner}.py, backend/api/simulation/runtime/resume.py, tests/{test_cli_resume,test_regressions}.py 및 변경 문서
- 판정: **수정 필요**

## 테스트 결과

- 명령: `/Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider`
- 결과: **966 passed in 15.03s**. 실패 없음. pytest-asyncio의 기본 fixture loop scope 미지정 경고 1건.
- 실행 ID: `cli-resume-review-20261102-suite` (도구 세션 4358). 설정: 저장소 테스트 fixture, 별도 시나리오 설정 파일 없음.
- 추가 실행 ID: `cli-resume-review-20261102-probe` (최종 도구 세션 31121). 임시 스크립트 `/private/tmp/cli_resume_review_probe.py`, 로그 `/private/tmp/cli_resume_review_probe.log`. 아래 부록에 스크립트 전문을 보존한다. 실제 LLM 대신 `_ScriptedLLM` 및 mock만 사용했다.
- 추가 실행 설정 원본: `tests/test_cli_resume.py::_SCENARIO`, `tests/test_regressions.py::RosterCsvParityTests`; 메모리의 SimStartConfig로 구성하며 영구 시나리오 파일을 쓰지 않았다. 임시 DB의 run ID는 `event1/event2`, `mem0/mem1/mem2`, `state0/state1`이다.
- 운영 코드·테스트·시나리오·기존 DB 수정 및 커밋 없음. 임시 DB만 생성했고 `logs_graph/*.db`는 열지 않았다. 검증 후 기존 작업 트리의 변경 파일 목록은 시작 시점과 동일했다(이 보고서 추가 제외).

## 지적 사항

심각도: **P1** 결과·연구 해석을 바꾸는 오류, **P2** 근거·설계·검증 보완, **P3** 표현·가독성.

### [P1] 재개 시각과 같은 예정 이벤트가 다시 발동한다

- 위치: `ABM/simulation/headless.py:233`, `ABM/simulation/runner.py:133` (`_current_elapsed_minutes(0) - 1`)
- 재현: 부록 스크립트를 저장하고 `/Users/wskim/anaconda/anaconda3/envs/coffee/bin/python /private/tmp/cli_resume_review_probe.py` 실행. `EVENT_REPLAY` 확인.
- 실제: 시작 시각 06:00의 `UNIQUE_ALERT`를 발동한 뒤 전부 실패하여 경과 0분으로 저장한 `event1`을 재개하면, `event2` 첫 wave에서 같은 알림이 재발동한다. 출력 `EVENT_REPLAY 0 1 1 2`: 첫 실행 scene_event 1건, 재개 실행도 1건, 최종 메모리에 알림 2건.
- 기대: 이미 발동했고 메모리에도 저장된 알림은 다시 주입하지 않는다. 실패로 시계가 멈추는 이번 변경 때문에 이 경계가 쉽게 발생한다. system_message 이외 agent_enter/update_appearance 등도 재실행 대상이 될 수 있다.
- 제안: 이벤트의 다음 도래/발동 여부를 저장·복원한다. 단순히 재개 시각의 이벤트를 모두 건너뛰면, 직전 정상 wave가 그 시각에 막 도달해 아직 발동하지 않은 이벤트까지 유실되므로 피해야 한다. 정확히 경계 시각에서 '이미 실행한 이벤트'와 '아직 실행하지 않은 이벤트' 양쪽 회귀 테스트가 필요하다. GUI의 같은 기존 경계 문제도 별도로 인지할 것.

### [P1] 구조화 기억이 새 run에 계승되지 않아 두 번째 재개에서 사라진다

- 위치: `ABM/simulation/headless.py:116–125`; 관련 `ABM/simulation/step.py:81`, `ABM/cli.py:534`
- 재현: 같은 부록 실행의 `MEMORY_CHAIN` 확인. mem0에 `UNIQUE_OLD_MEMORY` episode를 저장한 뒤 mem0 → mem1 → mem2로 각각 재개한다.
- 실제: `MEMORY_CHAIN True False 0`: mem1의 agent 캐시에는 이전 요약이 있으나 mem1 DB에는 episode가 0건이고, mem2는 mem1만 읽어서 캐시마저 비게 된다. `save_agent_snapshots`는 raw memory/state만 저장한다. 첫 재개에서도 새 run에 압축 결과가 생기면 `_fresh_memory_block`이 그 결과를 우선 사용하여 이전 run의 사실·사건·관계를 제외한다.
- 기대: 재개 횟수와 압축 여부에 관계없이 저장된 구조화 기억이 지속되고, 현재 시각에 맞춰 다시 렌더링된다.
- 제안: 새 run 생성 시 이전 구조화 기억을 출처/시점과 함께 계승하거나, 읽기·압축·갱신 경로 모두 동일한 계보를 조회하도록 설계한다. 캐시 문자열만 복사하면 recency와 다음 재개 문제가 남는다. 두 번 재개 및 재개 후 첫 압축 회귀 테스트를 추가한다. GUI에서 빌려 온 캐시 방식의 한계이며 새 CLI 구현에서도 그대로 노출된다.

### [P2] 디렉터 활성 시 실패 재시도마다 개입이 더해져 입력이 달라진다

- 위치: `ABM/simulation/runner.py:406–410`, `ABM/simulation/runner.py:978–986`
- 재현: 같은 부록 실행의 `DIRECTOR_RETRY` 확인. 디렉터가 항상 a에게 `UNIQUE_DIRECTOR`를 주도록 mock하고 에이전트 턴은 모두 실패시켜 3 wave 실행한다.
- 실제: 시계는 0분에 머무르지만 pending 입력에 같은 개입이 2건 쌓인다. 실패 wave의 current_wave를 보존한 후 다음 wave의 디렉터를 다시 실행하기 때문이다. 새 대상 개입이 있으면 재시도 참가자도 달라질 수 있다.
- 기대: 실패한 wave는 같은 입력으로 재시도하거나, 디렉터 사용 시 입력 변경이 의도된 예외라는 명시적 정책이 있어야 한다.
- 제안: 실패 재시도임을 추적하여 해당 재시도에서 디렉터 재실행을 건너뛰고 원래 개입을 보존한다. 실제 연구에서는 디렉터를 쓰지 않아 현재 연구 영향은 제한되지만 요청 2의 지원 경로에는 해당한다.

### [P3] 실패 wave의 시계 정지 설명에 가변 시간 모드 조건 누락 — `docs/glossary.md:418`, `docs/simulation-engine.md:272`

결정 표에는 fixed 예외가 있지만 두 설명은 무조건 경과 0분으로 읽힌다. variable 한정임을 적어 결정 표 및 실제 fixed 동작과 맞출 것.

## 확인했지만 문제없는 것

1. **전체 테스트(요청 1):** 미추적 새 테스트 5개 및 로컬 household 테스트까지 포함하여 966개 통과. 다만 새 `test_wave_with_some_successes_is_not_a_failed_wave`는 참가자별 성공/실패 조합을 직접 단언하지 않아 추가 probe로 2명 중 1명 성공을 확인했다(`PARTIAL_SUCCESS 1 <양수 경과분>`).
2. **실패 경로(요청 2):** variable의 실패 wave는 침묵 분기와 시간 추정을 건너뛰며 마지막 no_progress wave도 0분 명단을 남긴다. stop은 dead_wave 분기 전에 빠지고 pending을 보존한다(`STOP_FAILED 0 0`, KEEP_ON_STOP 유지). `_pop_held_for_turn`의 메시지는 current_wave에 합쳐진 뒤 실패 시 raw 메모리에서 롤백되고 같은 current_wave로 재전달된다. 실제 probe에서 UNIQUE_HELD는 성공 후 메모리에 1건이었다. 같은 run 내 예정 이벤트는 next_at을 먼저 갱신하므로 단순 실패 재시도만으로 중복되지 않는다(위 P1은 재개 경계 문제).
3. **이동 보류·빈 wave·전원 잠금(요청 2):** 해당 기존 회귀 테스트가 전체 suite에서 통과했다. 코드상 travelers 필터/상태 만료/empty fallback은 실제 턴 제출 이전에 실행되고, dead_wave는 제출 결과가 있을 때만 판정된다. 전원 잠금에서 성공 턴이 있는 기존 idle jump는 유지된다. 디렉터는 위 P2 예외다. 이 세 경로 각각과 실패가 동시에 발생하는 모든 조합을 새로 동적 검증한 것은 아니다.
4. **CLI 재개(요청 3):** 새 CLI 통합 테스트는 pending에 든 이전 발화의 재개, 새 ID, `resumed_from`, 누적 wave와 원래 목표까지 남은 시간 실행을 통과했다. 추가 probe는 `until_elapsed=99`, `UNIQUE_PHONE` 보류분, `infected_at_minutes=-10` 복원을 확인했다. infection 진행의 기존 stop/resume 접촉시간 테스트도 통과했다. 상태 fixture의 infection anchor 검사는 복원만 검증하며, 전체 활성 질병 진행의 모든 경우를 별도 검증했다는 뜻은 아니다.
5. **CSV·rebeta(요청 3):** `_resume_history`로 모은 실제 DB 이력 CSV는 wave `[0,1,2,3]`로 중복/누락 없이 이어지고 시작·끝 분도 연속이다. 별도로 두 엔진 실행의 명단 CSV를 합쳐 `rebeta`에 넣은 결정적 fixture에서 엔진 전파 시각 `{'b':30}`과 도구 `{'b':30}`이 같았다(`REBETA_CHAIN ... True`). 일반 확률 실행은 난수열이 다르므로 매 실행의 감염 결과까지 같다는 주장은 하지 않는다.
6. **GUI와 구 DB(요청 4):** GUI resume.py의 diff는 create_run에 `resumed_from=run_id` 추가 한 줄뿐이다. 기존의 목표 기간만큼 더 진행하는 인자도 유지된다. 실제 GUI/LLM 세션은 실행하지 않았다. 임시 legacy DB에서 resumed_from 없는 기존 행을 두고 migrate를 두 번 실행해 `[('legacy', None)]` 확인: 기존 행 보존 및 멱등성 정상. 전체 suite의 구 스키마 회귀 테스트도 통과했다.
7. **고정 시간 모드 판단(요청 5):** `FIXED_FAILURE 6 60`으로 실패 6 wave, 10분/wave에서 60분 경과함을 확인했다. fixed의 wave→시간 대응을 유지하는 명시적 호환성 경계로 이번 범위에 두는 것은 타당하다. 다만 fixed에서 '연결 실패 동안 가상 시간이 흐르지 않는다'는 보장은 성립하지 않으며 variable과 동일한 장애 복구 의미로 설명하면 안 된다. 결정 표에 예외가 있으므로 새 연구 가정 결정을 추가로 요구하지 않는다. 문서의 조건 누락은 위 P3.

---

## Claude 답변 (Claude가 작성)

| 지적 | 처리 | 근거 |
| --- | --- | --- |
| P1 예정 이벤트 경계 중복 | 수용·수정 | `runner.py:133`의 `now-1` 계산을 직접 확인했다. 실패 wave로 시계가 선 시각 T에 이미 발동한 T 이벤트가 "아직 안 발동"과 구별되지 않았다. 수정: `Simulation.export_timed_events()`(각 at_time 이벤트의 `next_at`)를 `finish_run(timed_events=)`가 새 컬럼 `simulation_runs.timed_events_json`에 저장한다(CLI `_finish_db_run`·백엔드 `finalize_run`). `run(timed_events_state=)`가 같은 목록(길이·순서·at_time 일치)일 때만 저장값을 쓰고 아니면 계산값을 쓴다. 저장값이 없으면 같은 객체의 직전 목록을 쓴다(/continue). CLI·GUI `/resume` 모두 전달한다. 테스트: 이미 발동한 경우는 재발동하지 않고(`test_event_fired_at_the_frozen_time_is_not_fired_again_on_resume`), 다음 도래가 지금 시각이면 발동한다(`..._due_but_not_yet_fired_...`). |
| P1 구조화 기억 계승 | 수용·수정 | `_fresh_memory_block`이 `self._sim_id`로만 읽는 것을 확인했다(GUI `/resume`에도 원래 있던 문제). 수정: `SimDB.copy_structured_memory(src, dst)`가 `episodic_memory`·`semantic_memory`·`relationship_memory`·`agent_self_state`를 새 run_id로 복사한다(id 제외, sim_id 교체). headless 재개와 GUI `/resume` 시작 시 호출한다. GUI는 실패해도 재개를 막지 않고 경고만 남긴다. 원문 근거 `messages`와 이력 테이블은 원래 run에 남긴다. 테스트: `test_structured_memory_survives_repeated_resumes`(m0→m1→m2, 각 run의 episode와 m2의 `_fresh_memory_block`). |
| P2 디렉터 재개입 | 수용·수정 | 실패 wave 다음 wave(재시도)는 디렉터를 호출하지 않는다(`retrying_failed_wave`). 원래 개입은 재시도 입력에 들어 있다. 테스트: `test_failed_wave_retry_does_not_call_the_director_again`(W1 개입 → W1·W2 실패 → pending에 개입 1건). 4개 테스트는 수정을 되돌리면 모두 실패하는 것을 확인했다. |
| P3 variable 조건 명시 | 수용·수정 | `docs/glossary.md`, `docs/simulation-engine.md` 18a·20에 가변 시간 모드 한정과 고정 시간 모드 동작을 적었다. 디렉터 재호출 없음과 재개 시 이벤트 상태 복원, 기억 복사는 `docs/decisions.md` 2026-11-02 행에도 반영했다. |

관련 부분집합 테스트: 274 passed(`-k "resume or no_progress or Cli or silence or idle or starv or dead or create_run or migrat or Roster or alert or timed or at_time or event or director or system_agent or finish_run or finalize or continue or memory"`), `tests/test_cli_resume.py` 9 passed. 남은 한계: `/load` 뒤 `/continue`는 새로 만든 객체라 저장된 이벤트 상태를 쓰지 않는다(이번 범위 밖, 기존 동작).

## 부록: 추가 검증 재현 스크립트

저장소 루트에서 아래 내용을 `/private/tmp/cli_resume_review_probe.py`로 저장하고 위 재현 명령으로 실행한다. 임시 DB는 종료 시 삭제된다. 실행 경과분 일부는 category 시간 추첨 때문에 달라지지만 P1/P2 출력의 건수·진릿값과 결정적 rebeta 결과는 동일하다.

```python
import sys, tempfile, os, json, importlib.util, sqlite3
sys.path.insert(0, os.getcwd())
from ABM.db import SimDB
from ABM.cli import _finish_db_run, _resume_history
from ABM.simulation.headless import run_config
from backend.api.simulation.schemas import SimStartConfig
from ABM.export.csv import render_location_csv
from ABM.tools import rebeta
spec=importlib.util.spec_from_file_location('fixture','tests/test_cli_resume.py'); f=importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

def run(db,tmp,cfg,rid,prior=None,llm=None,ready=None):
    row=db.get_run(prior) if prior else None
    db.create_run(rid,None,'probe',cfg.model_dump_json(),start_wave=(row['start_wave']+row['total_waves']) if row else 0,resumed_from=prior)
    res=run_config(cfg,llm=llm or f._ScriptedLLM(),log_dir=os.path.join(tmp,rid),db=db,sim_id=rid,resume_from=prior,on_sim_ready=ready)
    _finish_db_run(db,rid,res.sim,'completed')
    return res
with tempfile.TemporaryDirectory() as tmp:
    db=SimDB(os.path.join(tmp,'simulation.db'))
    cfg=SimStartConfig(**{**f._SCENARIO['config'],'sim_start_time':'06:00','events':[{'at_time':'06:00','type':'system_message','message':'UNIQUE_ALERT','targets':['a']} ]})
    x=run(db,tmp,cfg,'event1',llm=f._ScriptedLLM(fail_after=0))
    y=run(db,tmp,cfg,'event2',prior='event1')
    print('EVENT_REPLAY', x.sim._elapsed_minutes, sum(e['event_type']=='scene_event' for e in x.events), sum(e['event_type']=='scene_event' for e in y.events),sum('UNIQUE_ALERT' in m['content'] for m in y.sim.agents['a'].memory))
    cfg=SimStartConfig(**{**f._SCENARIO['config'],'max_waves':1,'events':[]})
    x=run(db,tmp,cfg,'mem0')
    db.upsert_episodes('mem0','a',[{'event':'UNIQUE_OLD_MEMORY','importance':5}],0,elapsed_minutes=0)
    y=run(db,tmp,cfg,'mem1',prior='mem0')
    z=run(db,tmp,cfg,'mem2',prior='mem1')
    print('MEMORY_CHAIN',bool(y.sim.agents['a']._memory_block),bool(z.sim.agents['a']._memory_block),len(db.get_episodes('mem1','a')))
    # Same resume restoration path for infection anchors and traveling-held phone state.
    cfg=SimStartConfig(**{**f._SCENARIO['config'],'max_waves':2,'events':[]})
    def seed(s):
        s._agent_status['b']={'state':'traveling','until_elapsed':99,'arrival_location':'거실','held_incoming':[{'speaker':'a','content':'UNIQUE_PHONE','kind':'phone'}]}
        s._agent_infection['a'].update(status='I',infected_at_minutes=-10,presymptomatic_at_minutes=0,infectious_at_minutes=0,recover_at_minutes=9999)
    x=run(db,tmp,cfg,'state0',ready=seed)
    saved=db.get_agent_states('state0')
    def check(s):
        print('STATE_RESTORED', s._agent_status['b']['until_elapsed'], s._agent_status['b']['held_incoming'][0]['content'],s._agent_infection['a']['infected_at_minutes'],db.get_run('state0')['pending_wave_json'])
    y=run(db,tmp,cfg,'state1',prior='state0',ready=check)
    logs,events=_resume_history(db,'state1'); path=os.path.join(tmp,'locations.csv');open(path,'w').write(render_location_csv(logs,events));obs=rebeta.load_location_csv(path)
    print('CSV_WAVES',obs.waves,[(w,obs.start[w],obs.end[w]) for w in obs.waves])
    # Schema migration with existing row, repeated application.
    conn=sqlite3.connect(':memory:');from ABM.db.schema import SCHEMA,migrate
    conn.executescript(SCHEMA);migrate(conn);conn.execute("INSERT INTO simulation_runs(run_id,status,started_at) VALUES('legacy','completed',0)");conn.execute('ALTER TABLE simulation_runs DROP COLUMN resumed_from');migrate(conn);migrate(conn)
    print('MIGRATION',conn.execute('SELECT run_id,resumed_from FROM simulation_runs').fetchall())
from unittest.mock import patch
with tempfile.TemporaryDirectory() as tmp:
    llm=f._ScriptedLLM(fail_after=0,fail_count=1);s,ev=f._simulation(tmp,llm)
    s._agent_status['a']={'state':'sleep','until_elapsed':999,'held_incoming':[{'speaker':'b','content':'UNIQUE_HELD','kind':'phone'}]}
    s.run('a',max_waves=2,step_delay=0)
    print('HELD_RETRY',sum('UNIQUE_HELD' in m['content'] for m in s.agents['a'].memory))
with tempfile.TemporaryDirectory() as tmp:
    s,ev=f._simulation(tmp,f._ScriptedLLM(fail_after=0));s._sys_enabled=True
    with patch('ABM.system_agent.run_system_agent',return_value={'interventions':[{'targets':['a'],'message':'UNIQUE_DIRECTOR'}]}):
        s.run('a',max_waves=3,step_delay=0)
    print('DIRECTOR_RETRY',sum('UNIQUE_DIRECTOR' in m['content'] for m in s._pending_wave['a']),s._pending_wave)
with tempfile.TemporaryDirectory() as tmp:
    s,ev=f._simulation(tmp,f._ScriptedLLM(fail_after=0))
    def stop(*a,**kw):s._stop_event.set();raise RuntimeError('stop')
    s._llm=stop;s.run('a',max_waves=3,step_delay=0,resume_wave={'a':[{'speaker':'b','content':'KEEP_ON_STOP'}]})
    print('STOP_FAILED',s.completed_waves,s._elapsed_minutes,s._pending_wave)
# Actual concatenated engine -> CSV -> rebeta parity, deterministic fixture.
spec=importlib.util.spec_from_file_location('reg','tests/test_regressions.py');reg=importlib.util.module_from_spec(spec);spec.loader.exec_module(reg)
t=reg.RosterCsvParityTests()
with tempfile.TemporaryDirectory() as tmp:
    script={'a':[{'target':['b']}],'b':[{'target':['a']}]}
    x,ex=t._run(tmp,script,{'a':'거실','b':'거실'},waves=2,mode='variable',durs=[30,30])
    y,ey=t._sim(tmp,script,{'a':'거실','b':'거실'},mode='variable',durs=[30,30],elapsed_minutes_init=x._elapsed_minutes,wave_base_init=x.completed_waves)
    y.restore_agent_state(x.export_agent_state());y.run('a',max_waves=2,step_delay=0,resume_wave=x._pending_wave)
    y.shared_log=x.shared_log+y.shared_log
    obs,engine,tool,_=t._tool(tmp,y,ex+ey,mode='variable',tpw=0)
    print('REBETA_CHAIN',obs.waves,engine,tool,engine==tool)
with tempfile.TemporaryDirectory() as tmp:
    s,ev=f._simulation(tmp,f._ScriptedLLM(fail_after=0));s._time_mode='fixed';s._time_per_wave=10
    s.run('a',max_waves=20,step_delay=0)
    print('FIXED_FAILURE',s.completed_waves,s._current_elapsed_minutes(s.completed_waves))
with tempfile.TemporaryDirectory() as tmp:
    s,ev=f._simulation(tmp,f._ScriptedLLM(fail_after=0,fail_count=1))
    s.run('a',max_waves=1,step_delay=0,resume_wave={'a':[],'b':[]})
    print('PARTIAL_SUCCESS',len([1 for t,d in ev if t=='turn_complete']),s._elapsed_minutes)
```


---

## 2차 검증 결과

- 작성 에이전트: Codex
- 작성일: 2026-11-02
- 기준 커밋: `e90cd19dcce78dde6ddf05af28c73dd01fe881af` + `cli-resume` 작업 트리(`git diff HEAD` 및 미추적 `tests/test_cli_resume.py`).
- 대상: 1차 P1 2건·P2 1건·P3 1건, Claude 답변 및 관련 CLI·GUI 재개/저장 경로. `/load` 뒤 `/continue`는 요청대로 보류 사유만 검토했다.
- 최신 판정: **통과**. 1차 지적 4건은 해결됐다. 보류 항목은 아래 한계를 명시하는 조건에서 이번 범위 밖으로 두는 사유가 타당하다.

### 실행 및 결과

| 실행 ID | 명령/설정 | 결과 |
| --- | --- | --- |
| `cli-resume-review-20261102-round2-suite` (도구 세션 32139) | `PYTHONDONTWRITEBYTECODE=1 /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider`; 저장소 테스트 fixture, 별도 설정 파일 없음 | **970 passed in 14.78s**, 실패 없음. pytest-asyncio 기본 fixture loop scope 미지정 경고 |
| `cli-resume-review-20261102-round2-probe` (도구 세션 79162) | 같은 Python으로 `/private/tmp/cli_resume_review_probe_round2.py` 실행. 위 1차 부록을 그대로 추출. 로그 `/private/tmp/cli_resume_round2_probe.log` | 종료 코드 0. 아래 세 핵심 출력 및 기존 보조 검증 확인 |
| `cli-resume-review-20261102-round2-extra` | 같은 Python으로 `/private/tmp/cli_resume_round2_extra.py` 실행. 로그 `/private/tmp/cli_resume_round2_extra.log` | 종료 코드 0. 정상 wave 뒤 디렉터 개입·실패 재시도, 재개 후 실제 압축 함수 및 다시 재개 검증 |

probe/extra 설정 원본: `tests/test_cli_resume.py::_SCENARIO`, `_simulation`, `_ScriptedLLM`, `tests/test_regressions.py::RosterCsvParityTests`. 메모리의 `SimStartConfig`와 임시 디렉터리 DB만 사용했다. probe run ID는 `event1/event2`, `mem0/mem1/mem2`, `state0/state1`; extra는 별도 임시 DB의 `m0/m1/m2`다. 실제 LLM 호출은 없으며 압축도 고정 JSON 응답 callable을 사용했다.

### 1차 지적별 재판정

| 지적 | 재판정 | 확인 근거 |
| --- | --- | --- |
| P1 예정 이벤트 경계 중복 | **해결** | `export_timed_events()` → CLI `_finish_db_run`/GUI `finalize_run` → `timed_events_json` → headless/GUI resume의 `timed_events_state` 전달을 확인했다. 부록 출력은 `EVENT_REPLAY 0 1 0 1`: 정지 시각 0분, 첫 실행 발동 1건, 재개 발동 0건, 메모리 알림 1건이다. 전체 suite의 이미 발동한 경계 및 아직 발동하지 않은 경계 테스트도 모두 통과했다. 후자는 저장값을 직접 주입하는 runner 단위 테스트이며, 두 경계를 모두 GUI 세션으로 검증했다는 뜻은 아니다. |
| P1 구조화 기억 계승 | **해결** | CLI와 GUI `/resume`이 새 run에 구조화 기억 4개 테이블을 복사한다. 시점·출처 필드도 복사하며 원문과 과거 이력은 원래 run에 남긴다. 부록 `MEMORY_CHAIN True True 1`로 두 번 재개 후 캐시와 중간 run의 episode 영속화를 확인했다. 추가 검증에서는 기존 episode·fact·relationship을 심고 m0→m1 재개 후 `_compress_agent`로 새 episode를 생성하고 m1→m2 재개했다. `MEMORY_AFTER_COMPRESSION 2 1 1`이며 `_fresh_memory_block`에 기존 사건·새 사건·기존 사실·관계가 모두 남음을 단언했다. GUI의 복사 예외 시 경고 후 계속 진행하는 경로는 정상 복사를 보장하는 경로가 아니지만, 정상 DB에서의 원래 유실 지적은 해결됐다. |
| P2 디렉터 재개입 | **해결** | `retrying_failed_wave`가 실패 직후 다음 wave의 디렉터 호출을 막는다. 원본 부록은 `DIRECTOR_RETRY 0 {'a': []}`: wave 0부터 전부 실패하므로 최초 디렉터 호출도 없고 재시도 개입도 쌓이지 않는다. 따라서 추가로 wave 0 성공 → wave 1 개입·실패 → wave 2 재실패 조건에서 mock 호출 횟수와 pending의 개입 건수를 각각 단언했다. `DIRECTOR_AFTER_SUCCESS 1 1`로 최초 개입 보존 및 재호출 억제를 확인했다. 새 관련 회귀 테스트도 통과했다. |
| P3 variable 조건 누락 | **해결** | `docs/glossary.md` 실패 wave 항목과 `docs/simulation-engine.md` 18a·20이 가변 시간의 경과 0분 및 고정 시간의 wave당 시간 진행을 구별한다. `docs/decisions.md`와 일치한다. 부록도 `FIXED_FAILURE 6 60`을 유지했다. |

보조 출력도 정상이다: `HELD_RETRY 1`, `STOP_FAILED 0 0`(KEEP_ON_STOP 보존), `STATE_RESTORED 99 UNIQUE_PHONE -10 {}`, `MIGRATION [('legacy', None)]`, `REBETA_CHAIN [0, 1, 2, 3] {'b': 30} {'b': 30} True`, `PARTIAL_SUCCESS 1 17`. CSV wave는 `[0,1,2,3]`, 경과 구간은 `(0,0,16)`, `(1,16,76)`, `(2,76,99)`, `(3,99,120)`으로 연속했다. 시간 추첨에 따른 경과분은 해당 실행의 관측값이다.

### 보류 사유 판단 및 검증 한계

`/load`는 새 `Simulation`을 만들지만 저장된 `timed_events_json`을 복원하지 않고, `/continue`도 해당 인자를 전달하지 않는다. 새 객체에는 이전 `_timed_events`도 없으므로 같은 시각의 기발동 이벤트가 재발동할 한계가 남는다는 Claude 설명은 코드와 일치한다. 이 경로는 기존 동작이며 이번 요청의 CLI `--resume` 및 GUI `/resume` 복구 경로와 구별된다. 사용자가 보류 사유만 판단하도록 범위를 정했으므로 **보류는 타당**하다. 이번 통과를 `/load`→`/continue`까지 중복 방지가 보장된다는 의미로 해석하면 안 된다. 저장 이벤트 상태가 없는 구 실행 역시 계산식으로 복원하므로 새 저장값이 있는 실행과 동일한 경계 보장은 없다.

실제 GUI/LLM 세션은 실행하지 않았다. GUI 변경은 저장·복사·복원 인자 경로를 정적으로 확인했고 전체 suite로 회귀를 점검했다. 운영 코드·테스트·시나리오·기존 DB는 수정하지 않았고 `logs_graph/*.db`는 열지 않았다. 커밋하지 않았다. 검증 전후 대상 운영 코드와 테스트의 SHA-256이 같음을 확인했다. 이 절 추가 전 보고서가 원본과 바이트 단위로 같음을 확인했으며, 1차 내용과 Claude 답변은 그대로 보존했다.

**최신 판정: 통과 — 1차 지적 모두 해결, `/load` 뒤 `/continue` 보류 사유 수용.**
