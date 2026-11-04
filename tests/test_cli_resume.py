"""실패 wave(성공 응답 0)의 시계 정지와 CLI ``run --resume`` 재개.

v16 case1(``5370f95e``)에서 LLM 연결이 끊긴 뒤 6 wave 가 "전원 침묵"으로 처리돼 idle
점프(60·120·180분)로 08:21 → 13:30 이 흘렀다. 실패 wave 는 시간을 흘리지 않고 같은
입력으로 재시도하며, 멈춘 실행은 CLI 에서 저장 상태로 이어 갈 수 있어야 한다.
"""
from __future__ import annotations

import csv
import io
import json
import os
import signal
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch


def _reply(content, target):
    return json.dumps({"content": content, "action_note": "", "target": target}), "", {}


class _ScriptedLLM:
    """에이전트 턴은 서로를 부르는 대사, 그 밖의 호출(시간 추정 등)은 고정 응답.

    ``fail_after`` 번째 에이전트 호출부터 ``fail_count`` 번 연결 오류를 낸다(None 이면 계속).
    """

    def __init__(self, fail_after=None, fail_count=None):
        self.fail_after = fail_after
        self.fail_count = fail_count
        self.agent_calls = 0
        self.failed = 0
        self.prompts: list[tuple[str, str]] = []   # (agent, user 메시지 전체)

    def __call__(self, messages, max_tokens=None, **kw):
        system = messages[0].get("content", "") if messages else ""
        who = "a" if "너는 a다" in system else "b" if "너는 b다" in system else None
        if who is None:
            return json.dumps({"category": "normal_scene", "minutes": 5, "reason": "t"}), "", {}
        self.agent_calls += 1
        if self.fail_after is not None and self.agent_calls > self.fail_after:
            if self.fail_count is None or self.failed < self.fail_count:
                self.failed += 1
                raise RuntimeError("연결 오류 [ConnectError]: All connection attempts failed")
        users = [m.get("content", "") for m in messages if m.get("role") == "user"]
        self.prompts.append((who, "\n".join(users)))
        other = "b" if who == "a" else "a"
        return _reply(f"{who}의 말 {self.agent_calls}", other)


def _simulation(tmp, llm):
    from ABM.agent import Agent
    from ABM.simulation import Simulation
    agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=100000) for k in ("a", "b")}
    events: list[tuple[str, dict]] = []
    sim = Simulation(
        agents, [{"role": "user", "content": "[배경] t"}], tmp, llm=llm,
        time_mode="variable", time_estimation_mode="category",
    )
    sim._emit = lambda t, d: events.append((t, d))
    return sim, events


class FailedWaveClockTests(unittest.TestCase):

    def test_failed_waves_freeze_the_clock_and_stop_at_the_last_good_time(self):
        llm = _ScriptedLLM(fail_after=6)       # 몇 wave 정상 뒤 연결이 끊긴다
        with tempfile.TemporaryDirectory() as tmp:
            sim, events = _simulation(tmp, llm)
            sim.run("a", max_waves=100, step_delay=0.0, starvation_waves=3)
        end = next(d for t, d in events if t == "simulation_end")
        self.assertEqual(end["end_reason"], "no_progress")
        good = {d["wave"] for t, d in events if t == "turn_complete"}
        rosters = {d["wave"]: d for t, d in events if t == "wave_roster"}
        last_good = max(good)
        frozen_at = rosters[last_good]["end_elapsed_minutes"]
        failed = [w for w in rosters if w > last_good]
        self.assertGreaterEqual(len(failed), 6)
        for w in failed:
            self.assertEqual(rosters[w]["start_elapsed_minutes"], frozen_at, w)
            self.assertEqual(rosters[w]["end_elapsed_minutes"], frozen_at, w)
        jumps_after = [d for t, d in events if t == "time_jump" and d["wave"] > last_good]
        self.assertEqual(jumps_after, [])        # idle 점프도 시간 추정도 없다
        self.assertEqual(sim._elapsed_minutes, frozen_at)

    def test_failed_wave_retries_the_same_participants_with_the_same_input(self):
        # a 의 첫 말 뒤 b 의 턴 하나만 실패한다 — 그 wave 는 성공 0, 다음 wave 에 b 가 같은 말을 다시 받는다.
        llm = _ScriptedLLM(fail_after=1, fail_count=1)
        with tempfile.TemporaryDirectory() as tmp:
            sim, events = _simulation(tmp, llm)
            sim.run("a", max_waves=4, step_delay=0.0, starvation_waves=3)
        self.assertEqual(llm.failed, 1)
        b_prompts = [p for who, p in llm.prompts if who == "b"]
        self.assertTrue(b_prompts)
        self.assertIn("a의 말 1", b_prompts[0])
        # 실패 wave 는 침묵이 아니므로 시간도 흐르지 않았다(시간 추정은 정상 wave 에서만).
        rosters = [d for t, d in events if t == "wave_roster"]
        self.assertEqual(rosters[1]["start_elapsed_minutes"], rosters[1]["end_elapsed_minutes"])

    def test_wave_with_some_successes_is_not_a_failed_wave(self):
        # 둘 중 하나만 실패한 wave 는 정상 wave 다 — 시간이 흐른다.
        llm = _ScriptedLLM(fail_after=2, fail_count=1)
        with tempfile.TemporaryDirectory() as tmp:
            sim, events = _simulation(tmp, llm)
            sim.run("a", max_waves=6, step_delay=0.0, starvation_waves=3)
        rosters = [d for t, d in events if t == "wave_roster"]
        moved = [d for d in rosters if d["end_elapsed_minutes"] > d["start_elapsed_minutes"]]
        self.assertGreaterEqual(len(moved), len(rosters) - 1)


_SCENARIO = {
    "name": "재개 테스트",
    "config": {
        "agents": [
            {"name": "a", "system_prompt": "너는 a다.", "location": "거실"},
            {"name": "b", "system_prompt": "너는 b다.", "location": "거실"},
        ],
        "background": "t",
        "start_agent": "a",
        "max_waves": 200,
        "time_mode": "variable",
        "time_estimation_mode": "category",
        "target_duration_minutes": 120,
        "step_delay": 0,
        "token_limit": 100000,
        "location_graph": [{"name": "거실", "connects_to": []}],
    },
}


class CliResumeTests(unittest.TestCase):
    """``python -m ABM.cli run --resume`` — 가짜 LLM·임시 DB 로 cmd_run 을 끝까지 태운다."""

    def _cmd(self, argv, llm, log_dir):
        import ABM.cli as cli
        import ABM.config
        from backend.api.simulation.runtime import llm_config

        class _NoRuntime:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        prev = signal.getsignal(signal.SIGINT)
        err = io.StringIO()
        try:
            with patch.object(cli, "LLMRuntime", _NoRuntime), \
                 patch.object(llm_config, "_make_llm", lambda *a, **k: llm), \
                 patch.object(llm_config, "_make_agent_llm_map", lambda c: {}), \
                 patch.object(ABM.config, "LOG_DIR", log_dir), \
                 redirect_stderr(err):
                code = cli.cmd_run(cli.build_parser().parse_args(argv))
        finally:
            signal.signal(signal.SIGINT, prev)
        return code, err.getvalue()

    def _runs(self, log_dir):
        from ABM.db import SimDB
        db = SimDB(os.path.join(log_dir, "simulation.db"))
        return db, sorted(db.get_runs(), key=lambda r: r["started_at"])

    def test_stopped_run_resumes_to_the_original_target_with_full_history(self):
        from ABM.cli import EXIT_OK
        with tempfile.TemporaryDirectory() as tmp:
            scenario = os.path.join(tmp, "s.json")
            with open(scenario, "w", encoding="utf-8") as f:
                json.dump(_SCENARIO, f, ensure_ascii=False)
            out = os.path.join(tmp, "out")

            code, err = self._cmd(["run", scenario, "--output-dir", out, "--quiet"],
                                  _ScriptedLLM(fail_after=4), tmp)
            self.assertEqual(code, EXIT_OK, err)
            db, runs = self._runs(tmp)
            first = runs[0]
            self.assertLess(first["elapsed_minutes"], 120)
            self.assertIn(f"--resume {first['run_id']}", err)

            code, err = self._cmd(["run", "--resume", first["run_id"], "--output-dir", out, "--quiet"],
                                  _ScriptedLLM(), tmp)
            self.assertEqual(code, EXIT_OK, err)
            db, runs = self._runs(tmp)
            second = runs[1]
            self.assertEqual(second["resumed_from"], first["run_id"])
            self.assertEqual(second["start_wave"], first["total_waves"])
            self.assertGreaterEqual(second["elapsed_minutes"], 120)       # 누적 목표에 도달
            self.assertLess(second["elapsed_minutes"], 120 + 60)          # 목표만큼 "더" 가지 않는다
            first_waves = {r["wave"] for r in db.get_run_log(first["run_id"])}
            second_waves = {r["wave"] for r in db.get_run_log(second["run_id"])}
            self.assertGreater(min(second_waves), max(first_waves))       # 누적 wave 번호

            # 재개 실행의 묶음 CSV 는 처음부터의 전체 기간을 담는다.
            with open(os.path.join(out, second["run_id"], "locations.csv"), newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            waves = {int(r["wave"]) for r in rows}
            self.assertIn(0, waves)
            self.assertIn(max(second_waves), waves)
            self.assertEqual(float(min(r["elapsed_minutes_start"] for r in rows if r["wave"] == "0")), 0.0)

    def test_resume_rejects_finished_targets_and_scenario_inputs(self):
        from ABM.cli import ConfigError
        with tempfile.TemporaryDirectory() as tmp:
            scenario = os.path.join(tmp, "s.json")
            with open(scenario, "w", encoding="utf-8") as f:
                json.dump(_SCENARIO, f, ensure_ascii=False)
            self._cmd(["run", scenario, "--quiet", "-o", os.path.join(tmp, "a.md")], _ScriptedLLM(), tmp)
            _, runs = self._runs(tmp)
            done = runs[0]["run_id"]
            with self.assertRaises(ConfigError) as cm:
                self._cmd(["run", "--resume", done, "--quiet"], _ScriptedLLM(), tmp)
            self.assertIn("목표 기간", str(cm.exception))
            with self.assertRaises(ConfigError):
                self._cmd(["run", scenario, "--resume", done], _ScriptedLLM(), tmp)
            with self.assertRaises(ConfigError):
                self._cmd(["run", "--resume", "없는-실행"], _ScriptedLLM(), tmp)


class ResumeStateCarryTests(unittest.TestCase):
    """재개가 이어받아야 하는 상태 — 예정 이벤트 발동 여부, 구조화 기억, 실패 재시도 입력(Codex 리뷰 2026-11-02)."""

    def _cfg(self, **over):
        from backend.api.simulation.schemas import SimStartConfig
        return SimStartConfig(**{**_SCENARIO["config"], **over})

    def _run(self, db, tmp, cfg, rid, prior=None, llm=None):
        from ABM.cli import _finish_db_run
        from ABM.simulation.headless import run_config
        row = db.get_run(prior) if prior else None
        db.create_run(rid, None, "t", cfg.model_dump_json(),
                      start_wave=(row["start_wave"] + row["total_waves"]) if row else 0,
                      resumed_from=prior)
        res = run_config(cfg, llm=llm or _ScriptedLLM(), log_dir=os.path.join(tmp, rid),
                         db=db, sim_id=rid, resume_from=prior)
        _finish_db_run(db, rid, res.sim, "done")
        return res

    def test_event_fired_at_the_frozen_time_is_not_fired_again_on_resume(self):
        from ABM.db import SimDB
        cfg = self._cfg(sim_start_time="06:00", events=[
            {"at_time": "06:00", "type": "system_message", "message": "UNIQUE_ALERT", "targets": ["a"]}])
        with tempfile.TemporaryDirectory() as tmp:
            db = SimDB(os.path.join(tmp, "simulation.db"))
            first = self._run(db, tmp, cfg, "r1", llm=_ScriptedLLM(fail_after=0))   # 06:00 발동 뒤 전부 실패
            self.assertEqual(first.sim._elapsed_minutes, 0)
            second = self._run(db, tmp, cfg, "r2", prior="r1")
        fired = lambda res: sum(e["event_type"] == "scene_event" for e in res.events)
        self.assertEqual(fired(first), 1)
        self.assertEqual(fired(second), 0)
        self.assertEqual(sum("UNIQUE_ALERT" in m["content"] for m in second.sim.agents["a"].memory), 1)

    def test_event_due_but_not_yet_fired_at_the_resume_time_still_fires(self):
        # 저장값의 다음 도래가 지금 시각이면(직전 run 이 그 시각에 막 도달해 끝났다) 재개 첫 wave 에 발동한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim, events = _simulation(tmp, _ScriptedLLM())
            sim._sim_start_minutes = 6 * 60
            ev = [{"at_time": "06:00", "type": "system_message", "message": "DUE", "targets": ["a"]}]
            sim.run("a", max_waves=1, step_delay=0.0, events=ev,
                    timed_events_state=[{"at_time": "06:00", "next_at": 0}])
            due = sum(t == "scene_event" for t, _ in events)
            sim2, events2 = _simulation(tmp, _ScriptedLLM())
            sim2._sim_start_minutes = 6 * 60
            sim2.run("a", max_waves=1, step_delay=0.0, events=ev,
                     timed_events_state=[{"at_time": "06:00", "next_at": 1440}])
            not_due = sum(t == "scene_event" for t, _ in events2)
        self.assertEqual((due, not_due), (1, 0))

    def test_structured_memory_survives_repeated_resumes(self):
        from ABM.db import SimDB
        cfg = self._cfg(max_waves=1)
        with tempfile.TemporaryDirectory() as tmp:
            db = SimDB(os.path.join(tmp, "simulation.db"))
            self._run(db, tmp, cfg, "m0")
            db.upsert_episodes("m0", "a", [{"event": "UNIQUE_OLD_MEMORY", "importance": 5}], 0, elapsed_minutes=0)
            self._run(db, tmp, cfg, "m1", prior="m0")
            last = self._run(db, tmp, cfg, "m2", prior="m1")
            for rid in ("m1", "m2"):
                self.assertEqual([e["event"] for e in db.get_episodes(rid, "a")], ["UNIQUE_OLD_MEMORY"], rid)
            self.assertIn("UNIQUE_OLD_MEMORY", last.sim._fresh_memory_block("a", 0) or "")

    def test_failed_wave_retry_does_not_call_the_director_again(self):
        # W0 정상 → W1 디렉터가 a 에게 개입, W1 전원 실패 → W2 재시도(디렉터 재호출 없음)도 실패.
        # 재시도 입력에는 W1 개입이 한 번만 있어야 한다(예전엔 재시도마다 개입이 쌓였다).
        with tempfile.TemporaryDirectory() as tmp:
            sim, _ = _simulation(tmp, _ScriptedLLM(fail_after=1))
            sim._sys_enabled = True
            sim._sys_interval = 1
            with patch("ABM.system_agent.run_system_agent",
                       return_value={"interventions": [{"targets": ["a"], "message": "UNIQUE_DIRECTOR"}]}):
                sim.run("a", max_waves=3, step_delay=0.0)
        pending = sim._pending_wave.get("a", [])
        self.assertEqual(sum("UNIQUE_DIRECTOR" in (m.get("content") or "") for m in pending), 1)


if __name__ == "__main__":
    unittest.main()
