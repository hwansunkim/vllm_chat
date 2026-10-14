"""LLM 출력 견고성 회귀 테스트 (case1 v12 오류 분석 후속, 2026-10-14).

1. extras 필드별 타입 검증 — `move_to`/`update_appearance`/`enter_state` 에 문자열이
   아닌 값이 와도 예외 없이 그 필드만 None, 대사·다른 필드는 정상.
2. 턴 처리 안전망 — `_step_agent` 의 주입 이후 예외가 turn_error·롤백·traceback 으로
   수렴하고, runner `future.result()` 도 traceback 을 남긴다.
3. 실패 원문 로깅 — kind·wave·agent·max_tokens·usage·원문 앞부분.
4. 시간 추론 출력 순서 — minutes(category) 먼저, reason 선택.
5. 시간 파서 — 펜스·첫 객체·필드 검증·실패 유형 구분.
"""
import json
import logging
import tempfile
import unittest
from unittest import mock


ENTRIES = [{"speaker": "a", "content": "안녕", "action_note": ""}]


def _fixed(reply, usage=None, reasoning=""):
    """항상 같은 응답을 내는 시간 추론용 스텁 LLM."""
    calls = []

    def llm(messages, max_tokens=None, **kw):
        calls.append(max_tokens)
        return reply, reasoning, dict(usage or {})
    llm.calls = calls
    return llm


# ── 1. extras 타입 검증 ─────────────────────────────────────────────────────

class ExtrasTypeValidationTests(unittest.TestCase):
    FIELDS = ("move_to", "update_appearance", "enter_state")
    BAD    = ({"id": "sleep"}, ["거실"], 3, 2.5, True, False)

    def test_non_string_values_drop_only_that_field(self):
        from ABM.parser import parse_json_extras_checked
        for field in self.FIELDS:
            for bad in self.BAD:
                with self.subTest(field=field, value=bad):
                    payload = {"content": "이제 잘게요.", "move_to": "안방",
                               "update_appearance": "잠옷 차림", "enter_state": "sleep",
                               "bring_along": ["신짱구"]}
                    payload[field] = bad
                    extras, errors = parse_json_extras_checked(
                        json.dumps(payload, ensure_ascii=False))
                    self.assertIsNone(extras[field])
                    self.assertEqual([e["field"] for e in errors], [field])
                    self.assertEqual(errors[0]["type"], type(bad).__name__)
                    # 다른 필드는 그대로
                    for other in self.FIELDS:
                        if other != field:
                            self.assertIsNotNone(extras[other])
                    self.assertEqual(extras["bring_along"], ["신짱구"])

    def test_no_str_coercion(self):
        # str(value) 로 바꿔 "{'id': 'sleep'}" 를 상태 ID 로 흘리면 안 된다.
        from ABM.parser import parse_json_extras
        with self.assertLogs("ABM.parser", level="WARNING") as cm:
            extras = parse_json_extras('{"content": "x", "enter_state": {"id": "sleep"}}')
        self.assertIsNone(extras["enter_state"])
        log = "\n".join(cm.output)
        self.assertIn("enter_state", log)
        self.assertIn("dict", log)
        self.assertIn('"id": "sleep"', log)

    def test_strings_and_null_keep_existing_behavior(self):
        from ABM.parser import parse_json_extras, parse_json_extras_checked
        extras, errors = parse_json_extras_checked(json.dumps({
            "content": "x", "move_to": "  거실 ", "update_appearance": "   ",
            "enter_state": None,
        }, ensure_ascii=False))
        self.assertEqual(errors, [])
        self.assertEqual(extras["move_to"], "거실")
        self.assertIsNone(extras["update_appearance"])
        self.assertIsNone(extras["enter_state"])
        # 필드 누락
        self.assertEqual(parse_json_extras('{"content": "x"}'),
                         {"move_to": None, "bring_along": None,
                          "update_appearance": None, "enter_state": None})
        # 객체 없음 → {} (기존 계약)
        self.assertEqual(parse_json_extras("그냥 텍스트"), {})


# ── 2. 턴 처리 안전망 ───────────────────────────────────────────────────────

class _AgentLLM:
    """에이전트 턴은 `reply` 를, 시간 관찰자 호출은 카테고리 JSON 을 돌려준다."""

    def __init__(self, reply: dict):
        self.reply = reply

    def __call__(self, messages, max_tokens=None, **kw):
        sys_text = messages[0].get("content", "") if messages else ""
        if "시간 관찰자" in sys_text:
            return json.dumps({"category": "normal_scene"}), "", {}
        return json.dumps(self.reply, ensure_ascii=False), "", {"completion_tokens": 12}


class StepAgentSafetyNetTests(unittest.TestCase):
    INCOMING = [{"speaker": "b", "content": "잘 자", "action_note": ""}]

    def _build(self, tmp, reply):
        from ABM.agent import Agent
        from ABM.simulation import Simulation
        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in ("a", "b")}
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_AgentLLM(reply), name_aliases={"에이": "a", "비": "b"},
        )
        events = []
        sim._emit = lambda t, d: events.append((t, d))
        return sim, events

    @staticmethod
    def _mem_has_incoming(sim):
        return any("잘 자" in str(m.get("content", "")) for m in sim.agents["a"].memory)

    def test_object_extras_from_v12_turn_succeeds_with_dialogue(self):
        # 실측 재현 입력 — enter_state 에 객체. 예전엔 AttributeError 로 턴 통째 소실.
        reply = {"content": "이제 잘게요.", "target": "self",
                 "enter_state": {"id": "sleep"}, "move_to": "안방"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            sim, events = self._build(tmp, reply)
            with self.assertLogs("ABM.simulation.step", level="WARNING") as cm:
                res = sim._step_agent("a", 0, 7, 3, list(self.INCOMING))
            self.assertTrue(res["success"])
            self.assertEqual(res["clean_content"], "이제 잘게요.")
            self.assertIsNone(res["enter_state"])      # 위반 필드만 버림
            self.assertEqual(res["move_to"], "안방")    # 다른 필드는 정상
            self.assertEqual(sim.shared_log[-1]["content"], "이제 잘게요.")
            self.assertIn("turn_complete", [t for t, _ in events])
            self.assertNotIn("turn_error", [t for t, _ in events])
            log = "\n".join(cm.output)
            for token in ("kind=agent_turn", "wave=7", "agent=a",
                          "field=enter_state", "type=dict", "원문(앞 200자)="):
                self.assertIn(token, log)

    def test_unexpected_exception_before_commit_rolls_back(self):
        reply = {"content": "안녕", "target": "self"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            sim, events = self._build(tmp, reply)
            with mock.patch("ABM.simulation.step.parse_json_extras_checked",
                            side_effect=RuntimeError("boom")), \
                 self.assertLogs("ABM.simulation.step", level="ERROR") as cm:
                res = sim._step_agent("a", 0, 5, 2, list(self.INCOMING))
            self.assertEqual(res, {"success": False, "agent_key": "a"})
            errs = [d for t, d in events if t == "turn_error"]
            self.assertEqual(len(errs), 1)
            self.assertEqual(errs[0]["speaker"], "a")
            self.assertEqual(errs[0]["turn"], 2)
            self.assertIn("boom", errs[0]["error"])
            self.assertFalse(self._mem_has_incoming(sim), "incoming 이 롤백되지 않음")
            self.assertNotIn("turn_complete", [t for t, _ in events])
            self.assertFalse([e for e in sim.shared_log if e.get("speaker") == "a"])
            rec = cm.records[0]
            self.assertIsNotNone(rec.exc_info, "traceback 이 남지 않음")
            self.assertIn("wave=5", rec.getMessage())
            self.assertIn("agent=a", rec.getMessage())

    def test_exception_after_commit_does_not_roll_back(self):
        # 커밋 지점(assistant 메모리 append) 이후 예외 — 부분 반영은 되감지 않는다.
        reply = {"content": "안녕", "target": "self"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            sim, events = self._build(tmp, reply)
            sim._resolve_targets = mock.Mock(side_effect=RuntimeError("late"))
            with self.assertLogs("ABM.simulation.step", level="ERROR") as cm:
                res = sim._step_agent("a", 0, 1, 0, list(self.INCOMING))
            self.assertFalse(res["success"])
            self.assertIn("turn_error", [t for t, _ in events])
            self.assertTrue(self._mem_has_incoming(sim), "커밋 후인데 incoming 을 롤백함")
            self.assertEqual(sim.agents["a"].memory[-1]["role"], "assistant")
            self.assertIn("커밋 후", cm.records[0].getMessage())
            self.assertIsNotNone(cm.records[0].exc_info)

    def test_parse_failure_log_includes_raw_and_budget(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            sim, events = self._build(tmp, {})
            sim._llm = lambda *a, **k: ('{"content": "잘리\n는', "", {"completion_tokens": 9})
            sim._llm_for = lambda key: sim._llm
            with self.assertLogs("ABM.simulation.turn", level="WARNING") as cm:
                res = sim._step_agent("a", 0, 4, 1, list(self.INCOMING))
            self.assertFalse(res["success"])
            self.assertFalse(self._mem_has_incoming(sim))
            log = "\n".join(cm.output)
            for token in ("kind=agent_turn", "wave=4", "agent=a",
                          f"max_tokens={sim.llm_max_tokens}", "completion_tokens=9",
                          '{"content": "잘리\\n는'):
                self.assertIn(token, log)

    def test_runner_future_exception_logs_traceback(self):
        reply = {"content": "안녕", "target": "self"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            sim, _ = self._build(tmp, reply)
            sim._step_agent = mock.Mock(side_effect=ValueError("pre-inject"))
            with self.assertLogs("ABM.simulation.runner", level="ERROR") as cm:
                sim.run("a", max_waves=1, step_delay=0.0)
            recs = [r for r in cm.records if "pre-inject" in r.getMessage()]
            self.assertTrue(recs)
            self.assertIsNotNone(recs[0].exc_info)


# ── 3·4·5. 시간 추론 ────────────────────────────────────────────────────────

class TimePromptOrderTests(unittest.TestCase):
    def test_minutes_template_puts_minutes_first_and_reason_optional(self):
        from ABM.time_classifier import _MINUTES_USER_TEMPLATE as T
        self.assertLess(T.index('"minutes"'), T.index('"reason"'))
        self.assertNotIn("먼저 근거", T)
        self.assertIn('{{"minutes": 7, "reason"', T)
        self.assertIn("40자", T)

    def test_category_template_puts_category_first(self):
        from ABM.time_classifier import _USER_TEMPLATE as T
        self.assertLess(T.index('"category"'), T.index('"reason"'))
        self.assertNotIn("먼저 근거", T)


class StripFenceTests(unittest.TestCase):
    def test_fence_variants_never_raise(self):
        from ABM.time_classifier import _strip_fence
        cases = {
            "```json":                          "",
            "```":                              "",
            '```json{"minutes": 5}```':         '{"minutes": 5}',
            '```json\n{"minutes": 5}\n```':     '{"minutes": 5}',
            '```\n{"minutes": 5}':              '{"minutes": 5}',
            '{"minutes": 5}':                   '{"minutes": 5}',
            "":                                 "",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_strip_fence(raw), want)


class EstimateWaveMinutesParsingTests(unittest.TestCase):
    def _est(self, reply, **kw):
        from ABM.time_classifier import estimate_wave_minutes
        usage = kw.pop("usage", None)
        return estimate_wave_minutes(ENTRIES, _fixed(reply, usage), **kw)

    def _fails(self, reply, kind, **kw):
        with self.assertLogs("ABM.time_classifier", level="WARNING") as cm:
            self.assertIsNone(self._est(reply, **kw))
        log = "\n".join(cm.output)
        self.assertIn(f"실패유형={kind}", log)
        self.assertIn("kind=time_estimate", log)
        return log

    def test_success_minutes_first_or_last(self):
        self.assertEqual(self._est('{"minutes": 7, "reason": "짧음"}'), (7, "짧음"))
        self.assertEqual(self._est('{\n  "reason": "x",\n  "minutes": 12\n}'), (12, "x"))

    def test_reason_is_optional(self):
        self.assertEqual(self._est('{"minutes": 7}'), (7, ""))
        self.assertEqual(self._est('{"minutes": 7, "reason": 3}'), (7, ""))

    def test_integral_float_ok_fraction_rejected(self):
        self.assertEqual(self._est('{"minutes": 7.0}'), (7, ""))
        self._fails('{"minutes": 7.5}', "bad_type")

    def test_truncated_reason_after_minutes_fails_with_raw_in_log(self):
        raw = '{"minutes": 7, "reason": "장면이 길어서 한참 설명하다가 잘'
        log = self._fails(raw, "invalid_json", wave=42)
        self.assertIn("Unterminated string", log)
        self.assertIn("wave=42", log)
        self.assertIn("max_tokens=256", log)
        self.assertIn("장면이 길어서", log)

    def test_truncated_reason_first_fails(self):
        log = self._fails('{\n  "reason": "무엇을 보고 판단', "invalid_json")
        self.assertIn("line 2 column 13", log)
        self.assertIn("\\n", log)            # 개행 이스케이프

    def test_fences(self):
        self.assertEqual(self._est('```json{"minutes": 5}```'), (5, ""))
        self.assertEqual(self._est('```json\n{"minutes": 5}\n```'), (5, ""))
        self._fails("```json", "no_json")   # 예전 IndexError(list index out of range)

    def test_two_objects_take_first(self):
        self.assertEqual(self._est('{"minutes": 5}\n{"minutes": 9}'), (5, ""))
        self.assertEqual(self._est('{"minutes": 5} 설명: 짧은 대화'), (5, ""))

    def test_empty_and_prose(self):
        self._fails("", "empty")
        self._fails("   \n ", "empty")
        self._fails("약 7분 정도입니다.", "no_json")

    def test_minutes_type_and_range_errors(self):
        self._fails('{"minutes": "7"}', "bad_type")
        self._fails('{"minutes": true}', "bad_type")
        self._fails('{"minutes": null}', "missing_field")
        self._fails('{"reason": "x"}', "missing_field")
        self._fails('{"minutes": -3}', "out_of_range")
        self._fails('{"minutes": 99999}', "out_of_range")

    def test_within_absolute_range_is_clamped_not_failed(self):
        # hi 는 다음 예정 시각 캡이기도 하다 — 넘으면 실패가 아니라 clamp(기존 동작).
        self.assertEqual(self._est('{"minutes": 600}', lo=1, hi=480), (480, ""))
        self.assertEqual(self._est('{"minutes": 0}', lo=1, hi=480), (1, ""))

    def test_log_includes_usage_and_finish_reason_when_available(self):
        log = self._fails('{"minutes": ', "invalid_json", llm_max_tokens=128,
                          usage={"prompt_tokens": 900, "completion_tokens": 128})
        self.assertIn("max_tokens=128", log)
        self.assertIn("completion_tokens=128", log)
        self.assertIn("finish_reason=미제공", log)
        log = self._fails('{"minutes": ', "invalid_json",
                          usage={"completion_tokens": 256, "finish_reason": "length"})
        self.assertIn("finish_reason=length", log)
        self.assertNotIn("미제공", log)

    def test_llm_exception(self):
        from ABM.time_classifier import estimate_wave_minutes

        def boom(messages, max_tokens=None, **kw):
            raise RuntimeError("down")
        with self.assertLogs("ABM.time_classifier", level="WARNING") as cm:
            self.assertIsNone(estimate_wave_minutes(ENTRIES, boom))
        self.assertIn("실패유형=llm_exception", "\n".join(cm.output))

    def test_requested_budget_unchanged(self):
        from ABM.time_classifier import estimate_wave_minutes
        llm = _fixed('{"minutes": 3}')
        estimate_wave_minutes(ENTRIES, llm, llm_max_tokens=256)
        self.assertEqual(llm.calls, [256])


class ClassifyWaveTimeParsingTests(unittest.TestCase):
    CATS = [{"id": "normal_scene", "label": "일반"}, {"id": "long_gap", "label": "공백"}]

    def _cls(self, reply, **kw):
        from ABM.time_classifier import classify_wave_time
        return classify_wave_time(ENTRIES, self.CATS, _fixed(reply), **kw)

    def test_success_and_optional_reason(self):
        self.assertEqual(self._cls('{"category": "long_gap"}'), "long_gap")
        self.assertEqual(self._cls('{"reason": "x", "category": "normal_scene"}'), "normal_scene")
        self.assertEqual(self._cls('```json{"category": "long_gap"}```'), "long_gap")
        self.assertEqual(self._cls('{"category": "long_gap"}{"category": "x"}'), "long_gap")

    def test_failures_are_classified_and_logged(self):
        for reply, kind in (('{"category": "nope"}', "unknown_category"),
                            ('{"category": 3}', "bad_type"),
                            ('{"reason": "x"}', "missing_field"),
                            ('{"category": "long', "invalid_json"),
                            ("```json", "no_json"),
                            ("", "empty")):
            with self.subTest(reply=reply):
                with self.assertLogs("ABM.time_classifier", level="WARNING") as cm:
                    self.assertIsNone(self._cls(reply, wave=9))
                log = "\n".join(cm.output)
                self.assertIn(f"실패유형={kind}", log)
                self.assertIn("kind=time_classify", log)
                self.assertIn("wave=9", log)


class RunnerPassesWaveToTimeLogTests(unittest.TestCase):
    def test_ai_mode_failure_log_carries_wave(self):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        def llm(messages, max_tokens=None, **kw):
            sys_text = messages[0].get("content", "")
            if "시간 관찰자" in sys_text:
                return '{"minutes": 5, "reason": "잘', "", {"completion_tokens": 256}
            return json.dumps({"content": "안녕", "target": "all"}), "", {}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in ("a", "b")}
            sim = Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp, llm=llm,
                time_mode="variable", time_estimation_mode="ai",
                time_categories=[{"id": "normal_scene", "label": "일반",
                                  "min_minutes": 10, "max_minutes": 10}],
            )
            sim._emit = lambda t, d: None
            with self.assertLogs("ABM.time_classifier", level="WARNING") as cm:
                sim.run("a", max_waves=1, step_delay=0.0)
        log = "\n".join(cm.output)
        self.assertIn("kind=time_estimate", log)
        self.assertIn("wave=", log)
        self.assertNotIn("wave=-", log)
        self.assertIn("max_tokens=256", log)
        self.assertIn("completion_tokens=256", log)


class PreviewRawTests(unittest.TestCase):
    def test_escapes_and_truncates(self):
        from ABM.llm import preview_raw
        self.assertEqual(preview_raw('a\nb\tc'), "a\\nb\\tc")
        long = "가" * 250
        out = preview_raw(long)
        self.assertTrue(out.startswith("가" * 200))
        self.assertIn("(+50자)", out)
        self.assertEqual(preview_raw(None), "None")


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
