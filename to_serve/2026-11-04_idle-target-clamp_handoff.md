# 인수인계: idle 시간 점프의 목표 기간 마감 클램프

- 작성: Claude Code
- 작성일: 2026-11-04
- 브랜치: `cli-resume` (미커밋 작업 트리)
- 기준 커밋: `e90cd19` → 작업 트리 (`to_serve/2026-11-02_cli-resume_handoff.md` 변경 위에 추가)
- 관련 결정: 없음(버그 수정 — 목표 기간 정의는 바뀌지 않는다)
- 발견: `to_master/2026-11-04_v16-results_analysis.md` §2 "C2의 8분 초과", §8 P2

## 바꾼 것
- `ABM/simulation/runner.py` idle 점프 블록(`idle_limits`): 예정 이벤트·상태 해제 시점 후보에 **목표 기간 마감**(`_target_deadline_elapsed`)을 추가했다. 조건은 상태 해제와 같이 `0 < room < raw_jump`, 라벨은 `_clamp_time_jump`와 같은 "목표 기간 종료".
- `tests/test_regressions.py`: `_partial_lock_run`에 `target_duration_minutes` 인자 추가, `test_idle_jump_does_not_overshoot_the_target_duration` 추가.
- `docs/simulation-engine.md` 20단계 idle 클램프 설명에 목표 마감 추가.

## 의도한 동작
idle 점프(연속 전원 침묵·전원 상태 잠금)도 일반 시간 추정 경로처럼 목표 마감을 넘지 않는다. 실측: v16 case2 재개 `b7eb9b06` W1093, 10074분에서 남은 목표 6분인데 상태 해제까지 14분 점프 → 10088분 종료. 수정 후에는 6분 점프 후 `target_duration`으로 10080분에 끝나야 한다.

새 테스트: 22:30 시작, W0 일반 경로 10분 → W1 idle raw 60분, 상태 해제 39분, 목표 25분 → 15분 점프("목표 기간 종료 전까지 60→15분"), 경과 25분에서 종료.

## 검증 요청
1. 전체 테스트(`AGENTS.local.md`의 `coffee` python). Claude는 `tests/test_regressions.py`·`tests/test_cli_resume.py`만 실행했다(912 passed).
2. 새 테스트가 수정 전 코드에서 실패하는지(수정 전에는 29분 점프 기대).
3. 전원 상태 잠금 경로(`raw_jump = 해제 시점까지`)에서도 목표가 더 이르면 목표에서 멈추는지.
4. 클램프 우선순위(예정 이벤트·상태 해제·목표가 동률일 때 목록 순서)가 기존 테스트 기대와 어긋나지 않는지.

## 위험 지점
- 목표 마감에서 멈춘 뒤 같은 wave 끝 종료 검사가 `_current_elapsed_minutes(run_wave + 1)`로 도달을 판정한다. 가변 모드 기준이며 고정 모드는 idle 점프를 타지 않는다.

## 범위 밖
- 구형 원 실행(`timed_events_json` 없음) 재개 시 재개 시각 예정 알림 중복(분석 §8 P2 둘째 항목) — 문서화만 제안, 코드 미변경.
