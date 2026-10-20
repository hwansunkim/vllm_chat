# Handoff: separate recorded vs attempted turns in the live end-of-run display

- Author: Claude Code
- Date: 2026-10-21
- Branch: `fix/attempted-turns-label`
- Base → head: `2e1ca60` (master) → `ad1975e`. Review range: `git diff master...fix/attempted-turns-label` (2 commits)
- Related decision: `docs/decisions.md`, 2026-10-20 "turn count" entry (updated in this change)
- Origin: P2 deferred item in `to_master/2026-10-20_turn-count-mismatch_analysis.md`

## Changes
- `ef9bd47` `.gitignore`: `run/`, `out.md`, `config.json` anchored to the repo root (`/run/` …). The unanchored `run/` (added in `381560b`) also ignored `frontend/js/sim/run/`, which blocked `git add` there.
- `ad1975e`:
  - `ABM/simulation/runner.py`: `simulation_end.total_turns` = agent records appended to `shared_log` during this `run()` (`len(shared_log) - log_start`); new field `attempted_turns` = sum of wave assignments (old meaning of `total_turns`). `turn.py:90` is the only `shared_log.append`, so this matches DB `simulation_log` rows.
  - `ABM/simulation/headless.py`: `RunResult.attempted_turns` (default 0).
  - `ABM/cli.py`: progress line shows recorded turns, plus `(시도 M)` only when they differ; JSON summary gains `attempted_turns`.
  - `frontend/js/sim/run/sse.js`: end message `완료 | 총 N턴 (시도 M) | reason`, the `(시도 M)` part only when they differ.
  - Docs: `api-reference.md`, `glossary.md`, `simulation-engine.md`, `decisions.md`.
  - Test `RunTurnCountTests.test_simulation_end_reports_recorded_and_attempted_turns_separately` (ok run 1/1, parse failure 0/1, continued run 1/1).

## Intended behaviour
- Before: `simulation_end.total_turns` counted attempts, so one parse failure showed "총 1턴" while the run history (DB) showed 0.
- After: the live display, `RunResult.total_turns` and the stored `total_turns` (since `dcbbf15`) use the same definition: recorded turns of that run. Attempts are a separate, explicitly named number.

## Verification requested
1. Full suite: `<vllm python> -m pytest tests/ -q -p no:cacheprovider` (paths in `AGENTS.local.md`).
2. Claude ran only: related `-k "RunTurnCount or HeadlessRunner or Cli or ..."` → 33 passed; the new test fails without the `runner.py` change (2 failed / 1 passed); `node --check frontend/js/sim/run/sse.js` OK.
3. Check every consumer of `simulation_end` / `RunResult.total_turns` (frontend, backend lifecycle, CLI, exports) still works and none relied on the old "attempted" meaning.
4. `.gitignore`: confirm root-only matching and that no tracked file is ignored (`git ls-files -ci --exclude-standard` empty).

## Risk points
- Any code path that appends to `shared_log` other than `turn.py:90` would make `total_turns` overcount — please re-check (`grep` found none).
- Stop/error paths: `simulation_end` is emitted only on the normal exit path; GUI error handling (`finalize_run`) does not use it.
- JS strict inequality: `d.attempted_turns !== d.total_turns` with numbers from JSON.

## Out of scope
- Historical rows in `simulation_runs` (still +1 before 2026-10-20); history/replay screens already read the DB value.
