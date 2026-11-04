# 인수인계: 실패 wave 시계 정지 + CLI `run --resume`

- 작성: Claude Code
- 작성일: 2026-11-02
- 브랜치: `cli-resume` (작업 트리, 미커밋 — 사용자 지시 전 커밋하지 않음). 새 파일 `tests/test_cli_resume.py`는 미추적.
- 기준 커밋: `e90cd19` → 작업 트리 (`git diff HEAD` + `tests/test_cli_resume.py`)
- 관련 결정: `docs/decisions.md` 2026-11-02 행(실패 wave·CLI 재개)
- 출발점: 작업 트리에 이미 있던 미커밋 `--resume` 초안(cli.py·headless.py·decisions.md, 2026-11-02 11:23 수정, 작성자 미상)을 리뷰해 이어받았다.

## 바꾼 것
- `ABM/simulation/runner.py`: **실패 wave** = 턴을 시도했고 성공 응답이 0. 침묵 처리보다 먼저 판정해 `silence_count`를 건드리지 않고 idle 점프를 막으며, 시간 누적을 건너뛴다(경과 0분 → 감염 t_d=0, `time_jump` 없음). `next_wave`는 이번 wave 입력(`current_wave`, 실패 턴은 incoming 롤백됨) + 이번 wave 새 주입으로 재시도. no_progress 백스톱은 그대로.
- `ABM/db/schema.py`·`ABM/db/runs.py`: `simulation_runs.resumed_from` 컬럼(추가 마이그레이션), `create_run(..., resumed_from=None)`.
- `backend/api/simulation/runtime/resume.py`: GUI `/resume`도 `resumed_from=run_id`를 남긴다(동작 변화 없음).
- `ABM/simulation/headless.py`(초안 유지): `resume_from` — 스냅샷 기억·구조화 기억 블록·런타임 상태·활성 집합·pending wave 복원, 누적 `elapsed_minutes_init`·`wave_base_init`, wave 키 이벤트 재생 안 함(at_time만), 처음부터 면역(R) 미적용, 목표 기간은 남은 분.
- `ABM/cli.py`:
  - `_load_resume_source`: 읽기 전용으로 실행 행·설정·스냅샷 완전성을 확인한다. `running` 실행, 스냅샷 없는 실행, 시나리오 입력이나 `--no-db`·`-n`·`--concurrency`와 함께 쓴 경우는 거절한다.
  - 목표 기간에 이미 도달한 실행은 실행 행을 만들기 전에 ConfigError로 거절한다(초안에서는 실행 행을 만든 뒤 런타임 예외로 처리됐다).
  - `create_run(..., resumed_from=)`.
  - `_resume_history`: `resumed_from` 사슬을 거슬러 올라가 이전 실행들의 로그·이벤트를 앞에 붙인다. `--output-dir` 묶음과 `-o`·`--format csv` 문서가 처음부터의 전체 기간을 담는다.
  - no_progress로 끝나면 `--quiet`여도 stderr에 재개 명령을 안내한다.
- 테스트:
  - 새 파일 `tests/test_cli_resume.py`(5개): 시계 정지와 마지막 정상 시각 보존, 같은 입력 재시도, 일부만 실패한 wave는 정상 wave, CLI 끝까지 재개(누적 목표·누적 wave·`resumed_from`·전체 기간 CSV), 거절 경로.
  - `tests/test_regressions.py`의 옛 동작 테스트 2개를 새 결정에 맞춰 바꿨다. `RosterCsvParityTests`는 이름을 `..._keeps_the_clock_and_contact_at_zero`로 바꿨다. `SilenceReinjectScopeTests`는 이름을 `test_failed_alert_wave_retries_the_same_agent_instead_of_silence`로 바꿨다.
- 문서: `docs/decisions.md`(초안의 꼬리 bullet을 표 행으로 바꿈, 10-27 행의 커밋 표기 `e90cd19`), `docs/simulation-engine.md` 18a·20, `docs/glossary.md`, `README.md` CLI 절.

## 의도한 동작
실측: v16 case1 `5370f95e-229e-4438-8d72-6ebe860a6ba7`(설정 `run/case1_v16_감염병O_2026-10-30_1601.json`).
- W23 이후 LLM 연결이 끊겼다(ConnectError).
- W24~29가 전원 침묵으로 처리돼 idle 60·120·180분 점프가 걸렸다. 마지막 정상 wave 종료 시각은 151분(08:21)이었는데 460분(13:30)에서 no_progress로 멈췄다.
- 아무도 행동하지 않은 5시간이 흘렀고, 그동안의 동석 시간은 감염 판정에 들어간다.

수정 뒤에는 다음과 같아야 한다.
- 실패 wave에서는 시계가 서 있다.
- no_progress로 멈추면 마지막 정상 wave 시각의 상태와 재시도 입력(pending)이 저장된다.
- `python -m ABM.cli run --resume <id> --output-dir <폴더>`로 그 시각부터 원래 목표 기간까지 이어 가고, 결과 묶음은 전체 기간을 담는다.
- 성공 응답이 하나라도 있는 wave와 실패가 없는 실행은 동작이 예전과 같아야 한다.

사용자 결정: 엔진 수정, 멈춘 v16 두 실행은 재개하지 않고 처음부터 재실행.

## 검증 요청
1. 전체 테스트: `<vllm python> -m pytest tests/ -q -p no:cacheprovider`. Claude 측은 관련 부분집합만 돌렸다(129 passed, `-k "resume or no_progress or Cli or silence or idle or starv or dead or create_run or migrat or Roster or alert"` + 새 파일 5개).
2. 실패 wave 판정이 다른 경로와 겹치지 않는지 확인:
   - 중지(stop) 중의 실패 결과
   - 이동 중 보류(traveling hold)
   - 자기 선언 상태 보류분(`_pop_held_for_turn`)의 재전달이 이중으로 되거나 유실되지 않는지
   - 예정 이벤트 알림이 재시도 때 중복 주입되는지(`_execute_event`가 memory에 직접 쓰는 부분과 롤백의 관계)
   - 디렉터 개입
   - 빈 wave 안전장치
   - 전원 상태 잠금
3. 재개 정합성:
   - 재개 run의 첫 wave가 `pending_wave_json` 입력을 받는지
   - 시각·감염 진행이 끊김 없이 이어지는지(`infected_at_minutes` 기준)
   - 보관 중이던 휴대폰 메시지와 이동 중 상태가 유지되는지
   - 이어 붙인 CSV에 wave 번호 중복이나 구멍이 없는지(`ABM/tools/rebeta.py`로 이어 붙인 CSV를 재계산하면 엔진과 같은지)
4. GUI `/resume` 경로가 `resumed_from` 추가 외에는 바뀌지 않았는지, 구 DB 마이그레이션이 동작하는지.
5. 고정 시간 모드는 시계가 wave 번호로 정해져 실패 wave에도 시간이 흐른다(결정 기록에 명시했다). 이 한계를 이번에 두는 것이 타당한지 판단해 달라.

## 위험 지점
- 실패 wave 재시도는 같은 입력을 무한히 다시 보내지 않는다. 진행 불가 한도(`max(6, starvation_waves×2)`)에서 멈춘다. 다만 재시도 사이에 대기가 없어 짧은 네트워크 끊김에도 6번을 빨리 소진할 수 있다. 대기·백오프는 넣지 않았다.
- 파싱 실패만 있는 wave(LLM은 응답했지만 형식 오류)도 실패 wave로 분류된다. 예전에는 침묵 1회째로 처리돼 시간 추정이 걸렸다.
- `_resume_history`는 `resumed_from`이 없는 이음매(`/continue`, 이번 변경 전의 GUI 재개)에서 멈춘다.

## 범위 밖
- 실제 LLM 실행, v16 재실행.
- 실행 중 강제 종료(kill)된 실행의 재개: 스냅샷이 없어 거절한다.
- 재시도 대기·백오프.
