# 검증 보고서: crossed-speech

- 작성: Codex
- 작성일: 2026-11-05
- 검토 대상: `to_serve/2026-11-05_crossed-speech_handoff.md`, 브랜치 `crossed-speech`의 미커밋 작업 트리
- 기준 커밋: `ad949e68d3bfefc9385870ba0d1de99525216c51`
- 대상 파일: `ABM/simulation/{runner,status,step}.py`, `tests/test_regressions.py`의 `CrossedSpeechTests`, `docs/{decisions,simulation-engine,simulation-features}.md`
- 판정: **수정 필요** — 라우팅 표식은 검증 통과. 기억 압축에서 엔진 설명과 화자의 대사를 구분하는 계약·검증 보완이 필요(P2 1건).
- 검토 diff SHA-256: `2e37ddb9e719312c44addc22fd65c0e1983b0db701a3b9955b250578b7dcd26a` (`git diff --` 위 대상 파일). 운영 코드·원본 DB 수정, 커밋 없음.

## 테스트 결과

- 명령: `/Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider`
- 결과: **978 passed in 15.83s**. 로컬 미추적 테스트도 포함.
- 요청 2: `_RunnerMixin.run` 소스를 메모리에서 컴파일하여 `if buffer is routed and agent_key in acted_now:`만 `if False:`로 바꿔 테스트. 작업 트리 변경 없음. **2 failed, 1 passed, 907 deselected**.
  - `test_messages_from_the_same_wave_are_marked_crossed_for_both_speakers`: `crossed: True` 누락으로 실패(1336행).
  - `test_scene_messages_are_not_marked`: 대사에 표식이 없어 실패(1380행).
  - `test_reply_after_hearing_is_not_marked`: 통과. 음성 대조군으로 타당하다.
- 요청 3: 부록 스크립트로 추가 6개 시나리오 검증(수면, 유예/행동만 한 수신자, 이동 후 보강, 공간 엿듣기, 보류 경계, 문 너머/문 너머 엿듣기) 모두 통과.
- JSON 왕복한 crossed 입력을 `resume_wave`에 넣고 첫 LLM 호출을 의도적으로 실패시킨 뒤 재시도: 두 호출 모두 설명을 받고, 성공 후 기억에는 설명이 한 번만 남음. 실제 웹 재개 API 전체를 새로 구동한 검증은 아니며, `ABM/db/runs.py:95`의 전체 pending 직렬화와 `backend/api/simulation/runtime/resume.py:59`의 JSON 복원도 확인했다.
- 재현 명령(저장된 부록의 검증 스크립트 실행):

```sh
sed -n '/^# BEGIN AUDIT SCRIPT$/,/^# END AUDIT SCRIPT$/p' to_master/2026-11-05_crossed-speech_review.md | /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python
```

표식 제거 음성 대조군 재현:

```sh
/Users/wskim/anaconda/anaconda3/envs/coffee/bin/python - <<'PYCODE'
import inspect, textwrap, pytest
import ABM.simulation.runner as r
s = textwrap.dedent(inspect.getsource(r._RunnerMixin.run))
s = s.replace('if buffer is routed and agent_key in acted_now:', 'if False:')
ns = dict(vars(r))
exec(compile(s, '<crossed-disabled>', 'exec'), ns)
r._RunnerMixin.run = ns['run']
raise SystemExit(pytest.main(['tests/test_regressions.py', '-q', '-p', 'no:cacheprovider', '-k', 'CrossedSpeechTests']))
PYCODE
```

## 지적 사항

심각도: **P1** 결과·연구 해석을 바꾸는 오류, **P2** 근거·설계·검증 보완, **P3** 표현·가독성.

### [P2] 압축 입력에서 엔진의 동시성 설명이 화자의 수신 대사와 구분되지 않는다

- 위치: `ABM/simulation/step.py:122`, `ABM/memory_compressor.py:54`, `ABM/memory_compressor.py:360`.
- 재현: 위 부록 실행 명령. `compression formatted`와 `synthetic compression accepted` 출력 확인.
- 실제: `_inject_incoming`이 `[b] 일어나!`와 설명 줄을 한 user 메시지에 넣고, `_format_messages`는 전체를 `[수신] [b] 일어나!` 아래에 그대로 출력한다. 압축 프롬프트에는 엔진 설명이라는 출처나 화자의 발언·영구 사실로 추출하지 말라는 규칙이 없다. 부록의 스텁 압축 LLM이 `b가 동시에 나온 말이라고 설명했다` 사건, `b는 내 행동을 아직 모른다` 영구 사실을 반환하면 임시 DB에 그대로 저장된다.
- 기대: 설명이 엔진의 동시성 메타정보임을 압축기가 알고, 발언의 선후관계 해석에는 사용하되 설명 자체를 등장인물 발언이나 지속적 사실로 추출하지 않아야 한다.
- 근거의 한계: 스텁은 잘못된 추출이 저장 가능한 경로와 계약 공백을 입증한다. 실제 모델이 이 오인을 했다는 실측 결과는 아니다. 실제 모델 호출 없이 요청 4의 “오인하지 않는지”를 통과로 판정할 근거가 부족하므로 **P2 검증·계약 보완**으로 분류한다.
- 제안: 설명을 무조건 삭제하면 압축 후 인과 순서가 다시 뒤집힐 수 있다. 압축 입력에서 엔진 메타정보임을 명시하고 아래 규칙을 추가한 뒤, 같은 두 발화의 압축 프롬프트 회귀 테스트 및 연구에 쓰는 모델의 소규모 압축 샘플을 확인한다. 표식 자체를 사건·사실로 옮기지 않으면서 동시성은 보존하는지를 함께 확인해야 한다.

```diff
--- a/ABM/memory_compressor.py
+++ b/ABM/memory_compressor.py
@@
 규칙:
+- 수신 대사에 붙은 "↔ 동시에 나온 말" 괄호 줄은 엔진이 제공한 시간 관계 설명입니다.
+  등장인물이 실제로 한 말이 아니며, 설명 자체를 사건·영구 사실로 추출하지 마세요.
+  해당 대사와 직전 자기 말·행동은 동시에 나왔으므로 그 대사를 자기 말에 대한 반응으로 요약하지 마세요.
```

## 확인했지만 문제없는 것

### 요청 3: 표식 조건과 라우팅 경계

- **수면 중 직접 지목:** W0에 수신자의 턴이 없고 W1에 처음 깨워서 받으면 표식 없음. 깨운다는 사실은 W0에 수신자가 발화했다는 뜻이 아니다.
- **1-wave 유예:** 직전 wave의 위치만 유예 판정에 쓰고 전달하는 대사 자체는 이번 wave에서 생성된다. 이번 wave에 수신자도 성공하면 표식이 사실과 일치한다. 부록은 서로 다른 방, 직전 동일 위치로 재현했다.
- **이동 후 보강:** 이동한 화자가 같은 wave에 만든 발화를 도착지 수신자에게 전달한다. 수신자의 턴도 이번 wave이면 표식, 도착 씬에는 표식 없음. 물리적으로 같은 순간이라는 측정이 아니라 병렬 생성된 턴 사이의 정보 비공유라는 wave 의미론이다.
- **spatial/문 너머 엿듣기:** 화자가 수신자에게 직접 말했는지와 무관하게 해당 wave의 수신자 턴을 모르고 생성했다는 설명은 참이다. 문 너머에서는 행동 묘사가 전달되지 않는 기존 규칙도 유지된다.
- **독백·행동만 한 수신자:** `success` 기준이므로 표시됨. 문구가 “한 말·행동”이어서 행동만 있는 경우도 포괄한다. 단순 `...`도 성공 응답이면 같은 규칙을 적용한다는 결정과 일치한다.
- **held_now 경계:** 보류 판정이 표식 부여보다 앞선다. 같은 wave에서 직접 타깃 b는 표식을 받고, 새로 잠든 비직접 엿듣기 c는 원본 held 메시지를 표식 없이 받는 것을 확인했다. 기존 보류 요약에도 새 crossed 표식을 추가하지 않는다.
- `strip_held_kind`가 사본을 만들고 crossed 분기에서도 새 dict를 생성한다. 원래 routed/보류 항목 오염 없음.
- 엔진 판정·라우팅 관련 변경은 없고 incoming 문구가 늘어난다. 다만 길이가 늘어 기억 압축/trim 시점과 이후 생성 행동은 달라질 수 있다. 이는 프롬프트 계약 변경으로 결정 기록에 기재되어 있다.
- 문서의 직접/문 너머/엿듣기/보강 포함, 씬·휴대폰·보류 제외가 코드와 일치한다. Methods 원고는 변경 대상이 아니며 LaTeX 변경도 없다.

### 요청 5: v16 기록의 빈도·토큰 영향 추정

원본 연결은 오직 `sqlite3.connect('file:logs_graph/simulation.db?mode=ro', uri=True)`로 열었다. 아래는 **지정된 재개 실행 자체만** 집계했다. 부모 실행은 합치지 않았다. 실행 설정의 정본은 DB `simulation_runs.config_json`이며 대응 설정 파일은 아래와 같다.

- case1: `095fe718-4c96-40fb-9bc5-d09037cbba81`, `run/case1_v16_감염병O_2026-10-30_1601.json` (누적 wave 30–1114).
- case2: `b7eb9b06-dbed-44c0-be72-05901715556f`, `run/case2_v16_감염병O_2026-10-30_1601.json` (누적 wave 6–1093).

`simulation_log`의 `(wave, speaker)`로 성공 턴 집합을 만들었다. 각 행의 `targets_json`에서 self/all/자기 자신을 제외한 고유 명시 타깃을 후보 1건으로 세고, 그 타깃이 같은 wave에 성공 기록을 가졌는지 확인했다. 상호 지목 쌍은 같은 wave 양방향이 모두 있는 무방향 쌍이다.

| 지표 | case1 | case2 |
| --- | ---: | ---: |
| 기록된 턴 / wave | 2,472 / 1,085 | 2,444 / 1,088 |
| 명시 타깃 후보 | 1,541 | 1,574 |
| 그중 같은 wave에 턴을 한 타깃 | 1,152 (74.76%) | 1,034 (65.69%) |
| 명시 타깃이 있는 wave | 885 | 928 |
| 같은 wave에 턴을 한 타깃이 있는 wave | 598 (885 중 67.57%) | 511 (928 중 55.06%) |
| 상호 지목 쌍 | 295 | 242 |
| 후보 동시 전달이 있는 wave당 평균 / 최대 | 1.93 / 6 | 2.02 / 6 |
| 실제 door_delivery 이벤트 | 937 | 892 |
| 그중 수신자가 같은 wave에 턴을 함 | 631 (67.34%) | 485 (54.37%) |
| all 타깃을 포함한 턴(위 명시 후보 집계에서 제외) | 663 | 582 |

- 이 수치는 **실제 crossed 총배달 수가 아니다**. targets는 의도이므로 거리에 막힌 말·보류될 말도 포함할 수 있고, all 확장·엿듣기·보강은 명시 후보 집계에 모두 반영되지 않는다. 따라서 전체 빈도의 하한·상한으로 주장할 수 없다. door 이벤트는 실제 채널 도달의 별도 교차 확인이며 명시 후보와 겹치므로 더하지 않는다. 중복·보류까지 재구성한 정확한 총량은 이번 추정 범위 밖이다.
- 설명은 60자, 선행 개행 포함 UTF-8 **138 bytes**. 엔진 `_estimate_tokens` 기준 **34 토큰/건**(실제 모델 토크나이저 실측 아님). 후보 수에 단순 적용하면 case1 **39,168**, case2 **35,156** 추정 토큰의 새 기억 문구다. 동시 후보가 있는 wave당 약 **65.5 / 68.8 토큰**, 최대 6건이면 **204 토큰**이다. all/엿듣기를 포함한 실제 값으로 오해하지 말아야 한다.
- 위 값은 문구를 새로 추가하는 분량이다. 기억에 남은 문구가 후속 요청에서 반복 전송되므로 **실제 누적 입력 토큰 비용과 같지 않다**. 모델별 토큰 수, memory trim/압축 시점, 생성 길이에 따라 달라진다.
- 최소한 명시 타깃 및 문 너머 표본에서 동시 턴이 흔하다. 반복 문구 부담은 드문 예외 수준이 아니다. 표식을 적용한 새 시뮬레이션의 대화 품질 개선 여부는 이번 스텁/기록 분석으로 확정하지 않았다.

### 검증 부록

다음 코드는 임시 디렉터리에만 실험용 에이전트 파일·DB를 만들고, 원본 DB는 읽기 전용으로 연다. 압축의 잘못된 출력은 의도적인 스텁이다.

```python
# BEGIN AUDIT SCRIPT
import sys,tempfile,json,sqlite3,collections,inspect,textwrap
sys.path.insert(0,'/Users/wskim/work/vllm');sys.path.insert(0,'/Users/wskim/work/vllm/tests')
from ABM.agent import Agent,_estimate_tokens
from ABM.simulation import Simulation
from ABM.simulation.step import CROSSED_NOTE
from test_regressions import _ScriptedLLM,NaturalStatusExpiryTests
G=[{'name':'R','connects_to':['S'],'zone':'H'},{'name':'S','connects_to':['R'],'zone':'H'}]
def run(script,locs,start=None,setup=None,waves=1,**kw):
 with tempfile.TemporaryDirectory() as tmp:
  sim=Simulation({k:Agent(k,f'너는 {k}다.',tmp,token_limit=8192) for k in script},[{'role':'user','content':'test'}],tmp,llm=_ScriptedLLM(script),agent_locations=locs,location_graph=G,time_mode='fixed',time_per_wave=10,**kw)
  if setup:setup(sim)
  turns=NaturalStatusExpiryTests._record_turns(sim)
  sim.run('a',max_waves=waves,step_delay=0,starvation_waves=100,resume_wave={k:[] for k in (start or script)})
  return sim,turns
speech={'content':'hello','target':'b'};silent={'content':'...','target':'self','action_note':'고개를 끄덕인다'}
s,t=run({'a':[speech],'b':[silent]},{'a':'R','b':'R'},start=['a'],setup=lambda s:s._agent_status.update({'b':{'state':'sleep','until_elapsed':999}}),waves=2)
m=next(inc for w,k,inc in t if w==1 and k=='b');assert not any(x.get('crossed') for x in m);print('sleeper not acted PASS')
s,t=run({'a':[speech],'b':[silent]},{'a':'R','b':'S'},setup=lambda s:setattr(s,'_prev_wave_start_location',{'a':'R','b':'R'}));assert s._pending_wave['b'][0]['crossed'];print('grace + action-only recipient PASS')
s,t=run({'a':[{**speech,'move_to':'S'}],'b':[silent]},{'a':'R','b':'S'});assert any(m.get('crossed') for m in s._pending_wave['b'] if m['speaker']=='a');assert all(not m.get('crossed') for m in s._pending_wave['b'] if m['speaker']=='씬');print('post move PASS')
s,t=run({'a':[speech],'b':[silent],'c':[silent]},{'a':'R','b':'R','c':'R'},perception_mode='spatial');assert any(m.get('crossed') for m in s._pending_wave['c'] if m['speaker']!='씬');print('spatial overhearing PASS')
s,t=run({'a':[speech],'b':[silent],'c':[{'content':'...','target':'self','enter_state':'sleep'}]},{'a':'R','b':'R','c':'R'},perception_mode='spatial');assert s._pending_wave['b'][0]['crossed'];held=s._agent_status['c']['held_incoming'];assert held and all(not m.get('crossed') for m in held);print('held vs direct PASS')
s,t=run({'a':[speech],'b':[silent],'c':[silent]},{'a':'R','b':'S','c':'S'},talk_through_doors=True,perception_mode='spatial');assert any(m.get('crossed') for m in s._pending_wave['b']);assert any(m.get('crossed') for m in s._pending_wave['c']);print('door + door overhearing PASS')
print('note chars/bytes/estimated tokens',len(CROSSED_NOTE),len(('\n'+CROSSED_NOTE).encode()),_estimate_tokens('\n'+CROSSED_NOTE))
from ABM.memory_compressor import _format_messages,_COMPRESSION_PROMPT,compress
from ABM.db import SimDB
msgs=[{'role':'assistant','content':'배고파 밥 줘','elapsed_minutes':0},{'role':'user','content':'[b] 일어나!\n'+CROSSED_NOTE,'elapsed_minutes':0}]
print('compression formatted:',_format_messages(msgs,0,0))
with tempfile.TemporaryDirectory() as tmp:
 db=SimDB(tmp+'/memory.db');captured=[]
 def llm(ms,**kw):
  captured.append(ms[-1]['content']);return json.dumps({'episodes':[{'event':'b가 동시에 나온 말이라고 설명했다','participants':['b'],'importance':3}],'facts':[{'fact':'b는 내 행동을 아직 모른다','confidence':1}],'relationships':[]}), '', {}
 compress('a','a','test',msgs,1,db,llm)
 assert CROSSED_NOTE in captured[0]
 print('synthetic compression accepted',db.get_episodes('test','a'),db.get_all_facts('test','a'))

c=sqlite3.connect('file:logs_graph/simulation.db?mode=ro',uri=True);c.row_factory=sqlite3.Row
for rid in ['095fe718-4c96-40fb-9bc5-d09037cbba81','b7eb9b06-dbed-44c0-be72-05901715556f']:
 cfg=json.loads(c.execute('select config_json from simulation_runs where run_id=?',(rid,)).fetchone()[0]);rows=[dict(r) for r in c.execute('select * from simulation_log where run_id=?',(rid,))];by=collections.defaultdict(dict)
 for r in rows:by[r['wave']][r['speaker']]=r
 pairs=set();mutual=set();direct=0;cross=0;activewaves=set();crosswaves=set();alltargets=0
 for w,rs in by.items():
  for a,r in rs.items():
   ts=json.loads(r['targets_json']);ts=[ts] if isinstance(ts,str) else ts
   targets=set(ts)-{'self','all',a};alltargets+=('all' in ts)
   for b in targets:
    direct+=1;activewaves.add(w)
    if b in rs:
     cross+=1;crosswaves.add(w);pairs.add((w,a,b))
 mutual={ (w,*sorted((a,b))) for w,a,b in pairs if (w,b,a) in pairs }
 evs=[(r['wave'],json.loads(r['data_json'])) for r in c.execute("select wave,data_json from sim_events where run_id=? and event_type='door_delivery'",(rid,))]
 doorcross=sum(d['target'] in by[w] for w,d in evs)
 perwave=collections.Counter(w for w,a,b in pairs)
 print('FREQUENCY',rid,'mode',cfg['perception_mode'],'turns',len(rows),'waves',len(by),'range',min(by),max(by),'target deliveries candidates',direct,'acted targets',cross,'ratio',round(cross/direct,4),'activewaves',len(activewaves),'crosswaves',len(crosswaves),'mutual pairs',len(mutual),'all targets',alltargets,'door',len(evs),'door acted',doorcross,'max/wave',max(perwave.values()),'mean/crosswave',cross/len(crosswaves),'estimated tokens',cross*_estimate_tokens('\n'+CROSSED_NOTE))
 print('config paths: run/ matching');print('top wave',perwave.most_common(3))
# Retry marked input after a failed wave; incoming memory must not duplicate.
with tempfile.TemporaryDirectory() as tmp:
 agents={'a':Agent('a','너는 a다.',tmp,token_limit=8192),'b':Agent('b','너는 b다.',tmp,token_limit=8192)}
 scripted=_ScriptedLLM({'a':[{'content':'ok','target':'self'}],'b':[silent]});calls=[]
 def flaky(ms,**kw):
  calls.append(ms)
  if len(calls)==1: raise RuntimeError('intentional failure')
  return scripted(ms,**kw)
 sim=Simulation(agents,[{'role':'user','content':'test'}],tmp,llm=flaky,agent_locations={'a':'R','b':'R'},location_graph=G,time_mode='variable')
 pending=json.loads(json.dumps({'a':[{'speaker':'b','content':'hello','crossed':True}]}))
 sim.run('a',max_waves=2,step_delay=0,starvation_waves=100,resume_wave=pending)
 assert len(calls)>=2 and CROSSED_NOTE in str(calls[0]) and CROSSED_NOTE in str(calls[1])
 assert sum(CROSSED_NOTE in m['content'] for m in agents['a'].memory)==1
 print('JSON resume + failed-wave retry PASS')
# END AUDIT SCRIPT
```

---

## Claude 답변 (Claude가 작성)

| 지적 | 처리 | 근거 |
| --- | --- | --- |
| P2 압축의 엔진 설명 출처·추출 규칙 | 수용·수정 (실모델 샘플은 보류) | `memory_compressor.py`의 `_format_messages`가 user 메시지를 `[수신] …`로 그대로 넣고 `_COMPRESSION_PROMPT` 규칙에 이 줄의 출처 설명이 없음을 직접 확인했다. 수정: 규칙 끝에 "받은 대사 아래의 '(↔ 동시에 나온 말: …)' 줄은 시스템이 붙인 시간 관계 설명 — 등장인물이 한 말이 아니므로 사건·사실로 적지 말고, 그 대사를 내 말을 듣고 한 반응으로 요약하지 말 것"을 추가했다. 동시성은 설명을 지우지 않으므로 압축 입력에 보존된다. 테스트 `test_compression_prompt_explains_the_crossed_note`: 규칙이 `CROSSED_NOTE`와 같은 표식을 가리키는지(문구가 어긋나면 실패), 포맷 입력에 표식이 남는지, 프롬프트 format 이 깨지지 않는지. 결정 기록·`simulation-features.md` §17 갱신. **보류:** 연구 모델의 실제 압축 샘플 확인은 LLM 서버 실행이 필요해 이 변경을 적용한 다음 시뮬레이션(v16 재실행)의 압축 결과에서 `동시에` 문구가 사건·사실로 남는지 확인하는 것으로 미룬다. |


### 보류 항목 실측 확인 (Claude, 2026-11-16)

- 실행: 미커밋 작업 트리(`ad949e6` + 이 변경)로 v16 재실행. case1 `ba39a953-c658-4ebd-856c-2c0172b9469c`, case2 `8268c0fb-0e07-4ffd-acb1-a25cb4e33fb8` (설정: `run/case{1,2}_v16_감염병O_2026-10-30_1601.json`, 배치 기록 `run/v16_crossed_batch_20261113T183110.json`). 두 실행의 소스 해시가 같고 커밋 `924ebf1`과 일치한다.
- 압축 기억(episodic·semantic·relationship·self_state, case1 269행·case2 239행)에서 설명 줄이 사건·사실로 남은 사례 0건. 대사에 설명 줄 문구가 새어 나온 사례 0건.
- 혼선 감소는 실행 1회씩이라 표현 빈도로 구별되지 않았다(표본 읽기에서는 순서가 맞게 이어진 사례 확인). 사용자 결정으로 커밋.

---

## 2차 검증 결과

- 작성 에이전트: Codex
- 작성일: 2026-11-05
- 기준 커밋: `ad949e68d3bfefc9385870ba0d1de99525216c51`
- 대상: `crossed-speech` 미커밋 작업 트리, 위 Claude 답변 표, `ABM/memory_compressor.py`, `ABM/simulation/{runner,status,step}.py`, `tests/test_regressions.py`, `docs/{decisions,simulation-engine,simulation-features}.md`.
- 검토 diff SHA-256: `d4b4d83d28380e5d6eb3006a99738b8be9dbbca3b7d8aa20238275264bbdf65c` (위 대상 파일에 대한 `git diff --`). `git diff master...crossed-speech --stat`은 비어 있으며 검증 대상은 HEAD 위 작업 트리 변경이다.
- **최신 판정: 통과. 1차 P2의 압축 계약·회귀 검증 공백은 해소됐다. 실모델 샘플 보류 사유는 타당하다.**

### 1차 P2 재확인

1. `ABM/memory_compressor.py:95`의 추가 규칙은 표식 줄이 시스템의 시간 관계 설명임을 명시하고, 등장인물의 발언이나 사건·사실로 추출하지 말도록 지시한다. 해당 대사가 직전 자기 말·행동과 동시에 생성됐으며 자기 말을 듣고 한 반응으로 요약하면 안 된다는 지시도 있다. 설명을 지우지 않아 동시성 해석에 필요한 정보가 압축 입력에 남는다. `compress`는 해당 `_COMPRESSION_PROMPT.format(...)`을 실제 호출 프롬프트로 사용하는 경로다.
2. `test_compression_prompt_explains_the_crossed_note`는 실제 `CROSSED_NOTE`에서 얻은 표식과 프롬프트의 표식을 대조하고, 발언 오인·반응 오인 방지 문구, 두 발화 포맷에서의 표식 보존 및 format 성공을 확인한다. 이는 프롬프트 계약의 회귀 테스트이며 모델 출력 품질을 검증하는 테스트는 아니다.
3. 음성 대조: 별도 Python 프로세스의 메모리에서만 추가 규칙을 제거하고 같은 테스트를 실행하면 표식 누락 assertion에서 실패한다. 새 테스트가 수정 전 계약 공백을 잡는 것을 확인했다. 파일 변경 없음.
4. 결정 기록과 `simulation-features.md` §17의 압축 설명은 실제 규칙과 일치한다. `simulation-engine.md`의 라우팅 경계도 작업 트리 구현과 일치한다. 재검토에서 새 P1/P2는 발견하지 않았다.

### 실행 결과

- 전체 테스트 실행 ID: `crossed-speech-r2-full-20261105` (도구 세션 `18061`). 설정 파일: 없음(pytest CLI 기본 수집, 시뮬레이션 실행 아님).
- 명령: `/Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider`
- 결과: **979 passed in 15.57s**, 종료 코드 0. 1차 대비 새 압축 회귀 테스트 1개 증가. 로컬 미추적 테스트 포함.
- 음성 대조 실행 ID: `crossed-speech-r2-no-rule-20261105` (도구 세션 `48806`). 설정 파일: 없음. 결과: **1 failed, 910 deselected in 1.29s** (의도한 실패).

```sh
/Users/wskim/anaconda/anaconda3/envs/coffee/bin/python - <<'PY'
import pytest
import ABM.memory_compressor as mc
mc._COMPRESSION_PROMPT = mc._COMPRESSION_PROMPT.split('- 받은 대사 아래의')[0]
raise SystemExit(pytest.main(['tests/test_regressions.py', '-q', '-p', 'no:cacheprovider', '-k', 'test_compression_prompt_explains_the_crossed_note']))
PY
```

- `git diff --check`: `docs/simulation-features.md:1488`의 EOF 빈 줄 1건만 검출. **P3** 서식 사항으로 재검증 반복이나 통과 차단 사유가 아니다.
- pytest 시작 시 `asyncio_default_fixture_loop_scope` 미설정 deprecation 경고가 출력됐으나 테스트 실패는 없다.

### 보류 판단과 검증 한계

연구 모델의 실제 압축 샘플은 서버 실행과 해당 프롬프트로 생성된 입력·출력이 있어야 확인할 수 있다. 다음 시뮬레이션(v16 재실행)의 압축 결과로 확인하겠다는 보류는 타당하다. 이번에는 사용자 지시대로 사유만 판단했으며 실모델 호출·시뮬레이션·원본 DB 접근은 하지 않았다. 다음 확인에서는 설명 자체가 등장인물 발언·지속 사실로 남는지뿐 아니라, 원래 동시 발화를 자기 말에 대한 반응으로 잘못 요약하는지도 함께 봐야 한다. 이번 통과는 계약 보완과 회귀 검증에 대한 판정이며 실제 모델이 항상 규칙을 준수한다는 실측 결론은 아니다.

운영 코드·DB 수정과 git 커밋 없이 이 보고서에만 2차 결과를 덧붙였다.
