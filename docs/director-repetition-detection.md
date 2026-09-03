# 디렉터의 반복 감지 — 주제 반복 대응 (D1 + D2)

시뮬레이션의 **디렉터**(`system_agent`, 내레이터)는 서사가 정체될 때 개입한다.
정체의 대표 형태가 **반복** — 한 에이전트가 여러 wave에 걸쳐 같은 자리에서
맴도는 것이다. 이 문서는 디렉터가 반복을 감지하는 두 경로와, 그중 하나가
왜 부족했으며 어떻게 보완했는지를 정리한다.

## 배경: 두 종류의 반복

| 종류 | 예시 | 감지 |
|---|---|---|
| **축자 반복** | 에이전트가 거의 똑같은 문장을 다시 내뱉음. `content="..."`(말 안 함)를 매 턴 반복. | `_repetition_score` (어휘 유사도) |
| **주제 반복** | 표현을 조금씩 바꿔가며 같은 화제·같은 욕구를 맴돔. "10분 남았다!"→"5분 남았어!"→"종 쳤다!" | (기존엔 감지 못 함) → **D1** |

## 문제 (run `87788b02`, `_0709.md`)

Wave 46–52, 두 아이가 매 wave `"N분 남았어! 배고파! 급식 메뉴 뭐야!"`를
표현만 바꿔 5회 반복. 디렉터는 interval=2대로 **매 짝수 wave 실행됐지만**
W46·48·50·52 모두 `interventions: []`를 반환했다. 개입을 안 한 게 아니라
**반복 신호를 못 받았다.**

### 근본 원인 1 — `_repetition_score`는 어휘 지표

`_repetition_score`는 `difflib.SequenceMatcher`로 원문을 글자 단위 비교한다.
실제 로그 데이터로 재현한 유사도:

| 디렉터 wave | 동생 | 누나 | 엄마 | 임계값 |
|---|---|---|---|---|
| W46 | 0.49 | 0.37 | 0.56 | **0.65** |
| W50 | 0.57 | 0.40 | 0.33 | **0.65** |
| W52 | 0.57 | 0.40 | 0.45 | **0.65** |

아이가 감탄사·숫자·음식명을 매번 바꿔서 어휘 일치율이 0.5 언저리에 그친다.
사람이 "같은 비트 5번째"로 읽는 건 **의미적 동일성**이지 글자가 아니다.

여러 대안 지표를 실측 캘리브레이션했으나 **전부 실패**:

| 지표 | 루프 최대 | 정상 대화 최대 | 분리 |
|---|---|---|---|
| SequenceMatcher (현재) | 0.57 | 0.52 | ✗ |
| char-trigram Dice | 0.34 | 0.36 | ✗ |
| 반복 토큰 비중 | 0.68 | 0.80 | ✗ |
| min 연속쌍 유사도 | 0.44 | 0.30 | ✗ |

이 시나리오의 정상 대화(아침 등교 러시, 식사 반응, 취침 미루기)도 태생적으로
~0.4~0.5 반복적이라, 임계값을 낮추면 시뮬레이션 절반이 오탐된다.
**저비용 어휘 지표로는 주제 반복을 분리할 수 없다.**

### 근본 원인 2 — 디렉터의 장거리 신호가 요약뿐

디렉터의 cross-wave 신호는 `repetition_info`(위) + `_last_summary` +
`director_memo`(자기가 쓴 것)뿐이다. 이 시나리오는 `summary_interval: 0`이라
요약이 없고, `repetition_info`가 비면 디렉터는 **매 wave를 새로 본다** —
"이 장면이 5 wave째 점심 카운트다운에 갇혀 있다"를 알 방법이 없었다.

## 수정

### D1 — 디렉터에게 최근 활동 다이제스트 주입 (핵심)

`_recent_activity_digest(shared_log, key_to_alias, waves=6)`
([_constants.py](../ABM/simulation/_constants.py))가 마지막 6 wave의 발화를
wave별로 나열한 문자열을 만들고, 디렉터 프롬프트의 `[최근 활동]` 섹션에 넣는다.

```
[최근 활동 — 마지막 몇 wave의 발화]
— Wave 47 —
  김이경: 이제 조금만 있으면 점심이다! 배고파서 쓰러질 것 같아
  김미경: 종 치자마자 빨리 뛰어가서 줄 서자! 늦으면 다 떨어져
— Wave 48 —
  김이경: 10분 남았다! 급식 메뉴 진짜 궁금해, 빨리 종 쳐라
  ...
```

디렉터 프롬프트 규칙에도 명시:

> **[반복 중인 에이전트]는 거의 똑같은 문장만 잡아냅니다.** [최근 활동]을
> 직접 읽고, 특정 에이전트가 여러 wave에 걸쳐 같은 화제·같은 욕구·같은
> 자리에서 맴돌고 있으면(문구가 조금씩 달라도) 반복으로 간주하십시오.

어휘로 못 하는 의미 판정을, **그걸 할 수 있는 LLM이** 하게 한다.
`summary_interval: 0`이어도 작동한다 — 이 시나리오처럼 프리즈된 설정도 커버.

- 필러(`content="..."`) 턴은 `(행동 묘사)`로 폴백해 "말없이 뭘 했나"도 보인다.
- 한 줄당 70자로 자르고 6 wave로 제한 — 토큰 비용은 소폭.

### D2 — `summary_interval` 기본값 0 → 5

요약은 디렉터의 장거리 서사 신호다. 기본값을 켜서 새 시뮬레이션은 D1과
요약을 **둘 다** 받는다.

- `backend/api/simulation/schemas.py` — `SimStartConfig.summary_interval = 5`
- `frontend/js/sim/state.js`, `scenarios.js`, `settings/page.js` — 기본값·폴백 5

**저장된 시나리오에는 `summary_interval: 0`이 프리즈돼 있어 재저장해야 반영된다.
D1이 그 공백을 메운다.**

### 정리 — `_REPEAT_THRESHOLD_PCT` 하드코딩 제거

`system_agent.py`에 `_REPEAT_THRESHOLD_PCT = 65`가 `_constants._REPEAT_THRESHOLD`
(0.65)와 따로 하드코딩돼 있었다. `run_system_agent(repeat_threshold_pct=...)`
인자로 바꿔 `system.py`가 `int(_REPEAT_THRESHOLD * 100)`을 넘긴다 — 단일 출처.

## 바뀌지 않은 것

- **`_repetition_score`는 그대로.** 축자 반복(같은 대사 그대로 재출력,
  `content="..."` + 같은 행동)은 여전히 이게 잡는다. 임계값 0.65도 유지 —
  낮추면 정상 대화가 오탐된다.
- **P1 수정**([아래 관련](#관련))도 그대로 — `content="..."` 침묵 턴을
  축자 반복으로 오탐하지 않는 정규화.

## 변경 파일

| 파일 | 변경 |
|---|---|
| `ABM/simulation/_constants.py` | `_recent_activity_digest()` + `_DIRECTOR_DIGEST_WAVES`/`_DIGEST_TURN_MAXLEN` 신설 |
| `ABM/simulation/system.py` | 다이제스트 생성 후 `run_system_agent`에 전달, `repeat_threshold_pct` 전달 |
| `ABM/system_agent.py` | `recent_activity`/`repeat_threshold_pct` 인자, `[최근 활동]` 섹션 + 규칙, `_REPEAT_THRESHOLD_PCT` 하드코딩 제거 |
| `backend/api/simulation/schemas.py` | `summary_interval` 기본 5 |
| `frontend/js/sim/{state,scenarios}.js`, `settings/page.js` | `summary_interval` 기본·폴백 5 |
| `tests/test_regressions.py` | `DirectorRecentActivityTests` (+2) |

## 검증

- `pytest tests/ -q` → 292 passed (회귀 0)
- 단위: 다이제스트 wave 그룹핑·필러 폴백·window 컷오프, 디렉터 프롬프트에
  `[최근 활동]` + 임계값 % 포함, 주제 반복이 `[반복 중인 에이전트]`엔 안 잡히지만
  다이제스트엔 보임
- 실사용: 같은 시나리오(`summary_interval: 5`로 재저장 또는 CLI)를 다시 돌려
  W46–52 류 루프에서 디렉터가 개입하는지 확인 필요

## 관련

- **P1** — 디렉터가 과묵한 캐릭터(`content="..."`)를 축자 반복으로 오탐해 한
  명만 계속 붙잡던 문제. `_normalize_utterance`로 대사 없으면 행동으로 폴백.
  (커밋 `e9b8576`)
- **디렉터 wave-시작 재배치 + 시계 주입** — world_event가 "6:45인데 벽시계
  8시"를 지어내던 문제. (커밋 `8307fd7`)
- **이동 rendezvous + 프롬프트 계약 층** — 같은 커밋.
