# 검증 보고서: idle 시간 점프의 목표 기간 마감 클램프

- 작성: Codex
- 작성일: 2026-11-04
- 검토 대상: `to_serve/2026-11-04_idle-target-clamp_handoff.md`, 브랜치 `cli-resume`의 미커밋 작업 트리(`git diff HEAD`)
- 대상 파일: `ABM/simulation/runner.py`, `tests/test_regressions.py`, `docs/simulation-engine.md`; 선행 CLI 재개 변경과의 연결도 확인
- 기준 커밋: `e90cd19dcce78dde6ddf05af28c73dd01fe881af`
- 판정: **통과**
- 실행 ID: `idle-clamp-full`, `idle-clamp-probes`, `idle-clamp-positive` (본 검증의 로컬 테스트 식별자; 실제 시뮬레이션 run ID 없음)
- 설정: `AGENTS.local.md`의 coffee 인터프리터, 저장소 테스트 fixture 및 `/private/tmp/idle-clamp-review/test_idle_clamp_review.py`의 결정론적 설정. 외부 시나리오 설정 파일·실제 DB·LLM 서버는 사용하지 않음.

## 테스트 결과

- 명령: `/Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest tests/ -q -p no:cacheprovider`
- 결과: **971 passed in 14.91s**, 실패 없음. 미추적 `tests/test_cli_resume.py`, `tests/test_household_location_plot.py`도 포함.
- 전체 로그: `/private/tmp/idle-clamp-full-tests.log`.
- 추가 검증: **29 passed, 1 deselected in 0.91s**. 아래 수정 전 실패 테스트만 제외하고 실행한 결과이며, 가져온 `NaturalStatusExpiryTests`의 기존 16개와 직접 작성한 13개 검증을 포함한다. 정확한 시간은 `/private/tmp/idle-clamp-positive.log` 참조.
- 수정 전 재현을 함께 실행한 결과: **1 failed, 29 passed in 1.41s**. 유일한 실패는 의도한 음성 대조 `test_new_regression_before_fix`: `tests/test_regressions.py:9587`, `AssertionError: 29 != 15`.
- 임시 검증 파일 최초 실행은 `/private/tmp` 상위 디렉터리 수집 중 sandbox 권한 오류로 수집되지 않았다. 전용 하위 디렉터리와 명시적 `--rootdir`·`--confcutdir`로 해결한 뒤 위 결과를 얻었다.

추가 검증 재현 명령(임시 파일이 남아 있는 현재 환경):

```sh
PYTHONPATH=/Users/wskim/work/vllm /Users/wskim/anaconda/anaconda3/envs/coffee/bin/python -m pytest /private/tmp/idle-clamp-review/test_idle_clamp_review.py --rootdir=/private/tmp/idle-clamp-review --confcutdir=/private/tmp/idle-clamp-review -q -p no:cacheprovider
```

수정 후 양성 검증만 실행하려면 위 명령에 `-k 'not test_new_regression_before_fix'`를 붙인다. 전체 재현 로그는 `/private/tmp/idle-clamp-probes.log`.

### 요청 1 — 전체 테스트

위 971개 모두 통과. 목표 기간의 일반 시간 경로, fixed 모드, 상태 자연 만료, 기존 idle 이벤트·상태 클램프, CLI 재개 테스트가 함께 통과했다.

### 요청 2 — 새 테스트의 수정 전 실패

작업 트리는 되돌리지 않았다. `inspect.getsource(_RunnerMixin.run)`에서 새로 추가된 목표 후보의 실행문 4줄만 제거하고 `compile`·`exec`로 함수를 메모리에 만든 뒤 `patch.object(_RunnerMixin, 'run', BEFORE)` 범위에서 **새 테스트 자체**를 호출했다. 선행 CLI 재개·실패 wave 수정은 그대로 유지했다.

- 수정 전: 첫 idle 점프 **29분**, 최종 경과 **39분**. 새 테스트의 `15분` 단언에서 실패.
- 수정 후: 첫 idle 점프 **15분**, clamp_reason `목표 기간 종료 전까지 60→15분`, 최종 경과 **25분**.
- 별도 관찰 검증에서 종료 이벤트가 `target_duration`이고 roster wave가 `[0, 1]`인지 확인했다. 목표 도달 뒤 W2를 추가 실행하지 않는다.

### 요청 3 — 전원 상태 잠금 경로

기존 `_sim` fixture로 a는 sleep(300분), b는 busy(20분)에 동시에 진입하게 했다. idle 스케줄은 `[9999]`, `max_waves=1`로 두어 상태 대기 경로를 명확하게 구별했다.

| 목표 분 | idle raw 분 | 실제 점프 | 사유 / 종료 |
| --- | --- | --- | --- |
| 15 | 20 | 15 | `목표 기간 종료 전까지 20→15분`, `target_duration` |
| 20 | 20 | 20 | clamp_reason 없음, `target_duration` |
| 25 | 20 | 20 | clamp_reason 없음, `max_waves` |

세 경우 모두 원래 reason이 `전원 상태(수면·이동 등) 해제 대기`였다. 스케줄 9999분 대신 해제까지 20분이 raw로 선택되고, 더 이른 목표만 이를 줄인다. 목표와 raw가 같으면 추가 클램프 라벨 없이 정확히 도달한다.

### 요청 4 — 클램프 우선순위와 경계

부분 잠금 fixture는 W0 10분, W1 raw 60분, 상태 해제 누적 39분이다. 다음 표의 jump는 W1 경과분이다.

| 목표 누적 분 | 예정 이벤트 | W1 jump | 선택 라벨 |
| --- | --- | --- | --- |
| 25 | 없음 | 15 | 목표 기간 종료 |
| 39 | 없음 | 29 | 상태 해제 시점 |
| 39 | 23:09(누적 39분) | 29 | 예정 이벤트(23:09) |
| 25 | 22:55(누적 25분) | 15 | 예정 이벤트(22:55) |
| 25 | 22:50(누적 20분) | 10 | 예정 이벤트(22:50) |
| 20 | 22:55(누적 25분) | 10 | 목표 기간 종료 |
| 70 | 없음 | 29 | 상태 해제 시점 |
| 11 | 없음 | 1 | 목표 기간 종료 |

모두 통과. `min(..., key=lambda t: t[0])`는 첫 최솟값을 유지하므로 동률 우선순위는 **예정 이벤트 → 상태 해제 → 목표**다. 후보의 최소 분이 먼저이고 목록 순서는 동률 라벨에만 영향을 준다. 새 후보는 뒤에 추가되므로 기존 이벤트·상태 우선순위를 바꾸지 않는다. 목표와 이벤트/해제가 동률이면 같은 wave 끝 목표 검사에서 종료하므로 해당 시각의 다음 wave 이벤트·자연 해제는 이번 run에서 처리하지 않는다. 기존 일반 경로의 목표 종료 구조와 같다.

## 지적 사항

P1·P2 지적 없음. 이번 목표 클램프 변경에서 수정이 필요한 반례를 찾지 못했다.

새 저장소 테스트는 종료 이벤트를 직접 단언하지 않지만, 추가 검증에서 `target_duration` 및 추가 wave 부재를 확인했다. 추후 이 단언과 전원 잠금·동률 사례를 영구 회귀 테스트로 편입하면 검증 범위를 유지하기 좋다(이번 통과를 막는 사항 아님).

## 확인했지만 문제없는 것

- `runner.py:1244–1247`의 목표 마감은 run 시작 누적 경과에 상대 목표를 더한 절대 마감이다. 선행 headless CLI 재개가 전달하는 남은 기간과 연결되며, GUI의 호출 이후 추가 기간 정의도 유지한다.
- `0 < room < raw_jump`는 목표가 더 이른 경우만 제한한다. 정상 제어 흐름에서는 목표 도달 직후 같은 wave 끝에서 break하므로 다음 idle wave에 목표 잔여 0분이 들어가는 경로가 없다. 남은 1분도 넘지 않았다.
- 전원 잠금 raw가 해제 시각에서 이미 정해져도 목표 후보가 별도로 더 짧게 제한한다.
- 목표 미지정 시 후보를 추가하지 않는다. 목표가 상태 해제보다 늦은 경우 기존 상태 클램프가 유지됐다.
- fixed 모드는 이번 idle 누적 분기를 실행하지 않는다. 관련 기존 테스트도 전체 스위트에서 통과했다.
- 문서의 세 상한 설명과 실행 코드가 일치한다. 주변 코드 주석의 '두 시각/두 후보' 표현은 새 목표 후보를 반영하지 않은 사소한 잔재이나 실행에는 영향이 없다.
- 운영 코드·테스트·결정 기록·실제 DB를 수정하지 않았고 git 커밋도 하지 않았다. 종료 전 `git status --short`에서 기존 변경 파일 목록과 같은 상태임을 확인했다. 생성 산출물은 이 보고서와 저장소 밖 임시 검증 파일·로그다.
- 판정 범위는 이번 인수인계의 목표 클램프 및 요청 1–4다. 선행 CLI 재개 변경 전체에 대한 별도의 재심 판정이나 실제 v16 실행 재현을 의미하지 않는다.

---

## Claude 답변 (Claude가 작성)

| 지적 | 처리 | 근거 |
| --- | --- | --- |
| P1·P2 없음 | 종료 | 판정 통과. |
| (참고) 주변 주석 "두 시각/두 후보" 잔재 | 수용 | `runner.py` idle 클램프 주석을 목표 후보·동률 순서(이벤트 → 상태 해제 → 목표)에 맞게 고쳤다. 주석만 변경, 관련 테스트 912 passed. |
| (참고) 종료 이벤트·전원 잠금·동률 사례 영구 회귀 편입 | 보류 | 통과를 막는 사항이 아니고 이번 범위의 핵심(목표 미초과)은 새 테스트가 덮는다. 다음 엔진 시간 작업 때 편입한다. |
