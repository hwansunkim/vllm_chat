import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx
from pydantic import ValidationError
from tenacity import wait_none

from backend import config, state
from backend.api import conversations
from backend.api import _conv_helpers
from backend.api.schemas import ChatMessage
from backend.db.database import get_db, init_tables, migrate_db
from backend.api.simulation import runtime as sim_runtime
from backend.api.simulation.schemas import AgentConfig, SimContinueConfig, SimStartConfig
from backend.llm import bridge
from backend.llm import client as llm_client
from backend.llm import registry as llm_registry
from backend.llm.providers.base import LLMHTTPError
from backend.llm.providers.openai import OpenAIProvider
from backend.llm.providers.vllm import VLLMProvider, _extract_reply
from backend.llm.registry import NoProviderError


class FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeHTTPClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, *args, **kwargs):
        return FakeStreamResponse(self._lines)

    async def aclose(self):
        return None


class FakeProvider:
    id = "server-1"
    name = "test server"
    model = "test-model"
    model_len = 1000


class FakeRegistry:
    def select(self, **kwargs):
        return FakeProvider()


class FakeJSONResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakePostClient:
    """chat() 의 post 호출 인자를 캡처하는 클라이언트."""

    def __init__(self, payload):
        self._payload = payload
        self.kwargs = None

    async def post(self, url, **kwargs):
        self.kwargs = kwargs
        return FakeJSONResponse(self._payload)

    async def aclose(self):
        return None


class FakeChatProvider:
    """bridge 테스트용 provider — chat() 만 구현한다."""

    id = "server-1"
    name = "test server"
    model = "test-model"
    model_len = 1000

    def __init__(self, error=None):
        self.error = error
        self.calls = 0
        self.seen_timeouts = []
        self.seen_temperatures = []

    async def chat(self, messages, *, temperature=0.7, max_tokens=4096, timeout=None):
        self.calls += 1
        self.seen_timeouts.append(timeout)
        self.seen_temperatures.append(temperature)
        await asyncio.sleep(0.01)  # 실제 IO 처럼 루프에 양보 → 스레드 동시성 노출
        if self.error is not None:
            raise self.error
        return f"answer:{messages[-1]['content']}", {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "thinking": "hidden",
        }


class FakeChatRegistry:
    def __init__(self, provider=None, error=None):
        self.provider = provider
        self.error = error
        self.selects = 0

    def select(self, **kwargs):
        self.selects += 1
        if self.error is not None:
            raise self.error
        return self.provider


class VLLMProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_reply_accepts_whitespace_before_think_tag(self):
        answer, thinking = _extract_reply({"content": "\n<think>hidden</think>\nanswer"})

        self.assertEqual(answer, "answer")
        self.assertEqual(thinking, "hidden")

    async def test_stream_parser_accepts_whitespace_before_think_tag(self):
        first = {
            "choices": [
                {"delta": {"content": "\n<thi"}, "finish_reason": None},
            ],
        }
        second = {
            "choices": [
                {"delta": {"content": "nk>hidden</think>\nanswer"}, "finish_reason": "stop"},
            ],
        }
        usage = {"usage": {"prompt_tokens": 3, "completion_tokens": 5}}
        provider = VLLMProvider("server-1", "test", "http://vllm", "model")
        provider._client = FakeHTTPClient([
            f"data: {json.dumps(first)}",
            f"data: {json.dumps(second)}",
            f"data: {json.dumps(usage)}",
            "data: [DONE]",
        ])

        events = [
            event
            async for event in provider.stream_chat([{"role": "user", "content": "hi"}])
        ]

        self.assertIn({"type": "thinking", "chunk": "hidden"}, events)
        self.assertIn({"type": "answer", "chunk": "answer"}, events)
        self.assertEqual(events[-1]["type"], "usage")
        self.assertEqual(events[-1]["data"]["thinking"], "hidden")
        self.assertEqual(events[-1]["data"]["answer"], "answer")


class TimeoutAndTemperatureTests(unittest.IsolatedAsyncioTestCase):
    """per-request timeout 과 벤더별 temperature 분기."""

    def _provider_with_fake_post(self, provider):
        fake = FakePostClient({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        })
        provider._client = fake
        return fake

    async def test_chat_uses_per_request_timeout_when_given(self):
        provider = VLLMProvider("server-1", "test", "http://vllm", "model")
        fake = self._provider_with_fake_post(provider)

        await provider.chat([{"role": "user", "content": "hi"}], timeout=120)

        self.assertEqual(fake.kwargs["timeout"], httpx.Timeout(120))

    async def test_chat_keeps_client_default_timeout_when_omitted(self):
        # 회귀 가드: timeout 을 안 주면 생성자에서 정한 클라이언트 기본값을 그대로 써야 한다.
        provider = VLLMProvider("server-1", "test", "http://vllm", "model")
        fake = self._provider_with_fake_post(provider)

        await provider.chat([{"role": "user", "content": "hi"}])

        self.assertIs(fake.kwargs["timeout"], httpx.USE_CLIENT_DEFAULT)

    async def test_vllm_chat_always_sends_temperature(self):
        provider = VLLMProvider("server-1", "test", "http://vllm", "model")
        fake = self._provider_with_fake_post(provider)

        await provider.chat([{"role": "user", "content": "hi"}], temperature=0.3)

        self.assertEqual(fake.kwargs["json"]["temperature"], 0.3)

    def test_openai_reasoning_models_omit_temperature(self):
        for model in ("gpt-5", "gpt-5.1", "o1-mini", "o3", "o4-mini"):
            with self.subTest(model=model):
                p = OpenAIProvider("x", "x", "", model)
                self.assertEqual(p._temperature_body(0.7), {})

    def test_openai_classic_models_send_temperature(self):
        p = OpenAIProvider("x", "x", "", "gpt-4.1")
        self.assertEqual(p._temperature_body(0.7), {"temperature": 0.7})

    def test_vllm_temperature_body_always_present(self):
        p = VLLMProvider("x", "x", "http://vllm", "gpt-5-lookalike")
        self.assertEqual(p._temperature_body(0.7), {"temperature": 0.7})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    """워커 스레드 → 메인 이벤트 루프 브릿지 (backend/llm/bridge.py)."""

    async def asyncSetUp(self):
        self.provider = FakeChatProvider()
        self.registry = FakeChatRegistry(self.provider)
        self.old_get_registry = llm_client.get_registry
        llm_client.get_registry = lambda: self.registry
        state.event_loop = asyncio.get_running_loop()

    async def asyncTearDown(self):
        llm_client.get_registry = self.old_get_registry
        state.event_loop = None

    def _no_wait_chat(self, **kwargs):
        """재시도 대기 없이 동작하는 콜러블 (테스트 시간 단축용)."""
        old_wait = bridge.wait_exponential
        bridge.wait_exponential = lambda **_: wait_none()
        try:
            return bridge.make_sync_chat(**kwargs)
        finally:
            bridge.wait_exponential = old_wait

    async def test_sync_chat_returns_three_tuple_without_thinking_in_usage(self):
        chat = bridge.make_sync_chat(timeout=5)

        content, reasoning, usage = await asyncio.to_thread(
            chat, [{"role": "user", "content": "hi"}]
        )

        self.assertEqual(content, "answer:hi")
        self.assertEqual(reasoning, "hidden")
        self.assertNotIn("thinking", usage)
        self.assertEqual(usage["completion_tokens"], 2)
        # per-request timeout 이 provider 까지 전달되는지
        self.assertEqual(self.provider.seen_timeouts, [5])

    async def test_zero_temperature_is_not_replaced_with_default(self):
        # temperature=0.0 은 falsy 라서 `value or default` 류 패턴이면 조용히
        # 기본값(0.7)으로 되돌아간다. 0.0 이 provider 까지 그대로 전달되는지 고정.
        chat = bridge.make_sync_chat(timeout=5, temperature=0.0)

        await asyncio.to_thread(chat, [{"role": "user", "content": "hi"}])

        self.assertEqual(self.provider.seen_temperatures, [0.0])

    async def test_many_worker_threads_share_the_main_loop(self):
        # "attached to a different loop" 회귀 가드.
        chat = bridge.make_sync_chat(timeout=5)

        results = await asyncio.gather(*[
            asyncio.to_thread(chat, [{"role": "user", "content": str(i)}])
            for i in range(8)
        ])

        self.assertEqual(
            sorted(r[0] for r in results),
            sorted(f"answer:{i}" for i in range(8)),
        )
        self.assertEqual(self.provider.calls, 8)

    async def test_missing_event_loop_raises_clear_runtime_error(self):
        state.event_loop = None
        chat = bridge.make_sync_chat(timeout=5)

        with self.assertRaises(RuntimeError) as ctx:
            await asyncio.to_thread(chat, [{"role": "user", "content": "hi"}])

        self.assertIn("이벤트 루프", str(ctx.exception))
        self.assertEqual(self.provider.calls, 0)

    async def test_permanent_http_error_fails_immediately_with_original_message(self):
        self.provider.error = LLMHTTPError(400, "HTTP 400 from test server: bad param")
        chat = self._no_wait_chat(timeout=5)

        with self.assertRaises(LLMHTTPError) as ctx:
            await asyncio.to_thread(chat, [{"role": "user", "content": "hi"}])

        # RetryError 로 감싸이지 않고 원본 메시지가 그대로 보존된다
        self.assertEqual(str(ctx.exception), "HTTP 400 from test server: bad param")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.provider.calls, 1)

    async def test_transient_http_error_is_retried_three_times(self):
        self.provider.error = LLMHTTPError(429, "HTTP 429 from test server: rate limit")
        chat = self._no_wait_chat(timeout=5)

        with self.assertRaises(LLMHTTPError) as ctx:
            await asyncio.to_thread(chat, [{"role": "user", "content": "hi"}])

        self.assertEqual(str(ctx.exception), "HTTP 429 from test server: rate limit")
        self.assertEqual(self.provider.calls, 3)

    async def test_missing_server_fails_immediately(self):
        self.registry.error = NoProviderError("사용 가능한 LLM 서버가 없습니다.")
        chat = self._no_wait_chat(timeout=5)

        with self.assertRaises(RuntimeError) as ctx:
            await asyncio.to_thread(chat, [{"role": "user", "content": "hi"}])

        self.assertIn("사용 가능한 LLM 서버가 없습니다", str(ctx.exception))
        self.assertEqual(self.registry.selects, 1)


class _AllowAllRegistry:
    """_make_agent_llm_map 테스트용 — 모든 server_id를 등록된 것으로 취급."""

    def get_provider(self, server_id):
        return object()


def _agent(name, *, server_id=None, temperature=None):
    return AgentConfig(name=name, system_prompt="", server_id=server_id, temperature=temperature)


def _cfg(agents, *, server_id=None, temperature=0.7):
    return SimStartConfig(
        agents=agents, background="", start_agent=agents[0].name,
        server_id=server_id, temperature=temperature,
    )


class AgentLlmMapTests(unittest.TestCase):
    """_make_agent_llm_map 의 server_id/temperature 두 축 판정 (backend/api/simulation/runtime.py)."""

    def setUp(self):
        self.old_get_registry = llm_registry.get_registry
        llm_registry.get_registry = lambda: _AllowAllRegistry()

    def tearDown(self):
        llm_registry.get_registry = self.old_get_registry

    def test_zero_override_differs_from_nonzero_default(self):
        # temperature=0.0 은 falsy 라서 `agent.temperature or cfg.temperature` 류
        # 판정이면 "값 없음"과 구별이 안 돼 조용히 매핑에서 빠진다. `is None` 비교로
        # 0.0 오버라이드가 실제로 매핑에 포함되는지 고정.
        cfg = _cfg([_agent("alice", temperature=0.0), _agent("bob")], temperature=0.7)

        result = sim_runtime._make_agent_llm_map(cfg)

        self.assertIn("alice", result)
        self.assertNotIn("bob", result)

    def test_zero_default_and_zero_override_match_and_are_excluded(self):
        cfg = _cfg([_agent("alice", temperature=0.0)], temperature=0.0)

        result = sim_runtime._make_agent_llm_map(cfg)

        self.assertNotIn("alice", result)

    def test_server_only_and_temperature_only_overrides_both_map(self):
        cfg = _cfg(
            [
                _agent("server_only", server_id="srv-2"),
                _agent("temp_only", temperature=1.5),
                _agent("neither"),
            ],
            server_id="srv-1", temperature=0.7,
        )

        result = sim_runtime._make_agent_llm_map(cfg)

        self.assertIn("server_only", result)
        self.assertIn("temp_only", result)
        self.assertNotIn("neither", result)


class TargetDurationTests(unittest.TestCase):
    """목표 기간(target_duration_minutes) 종료 조건 (ABM/simulation/runner.py)."""

    def _llm(self, category="normal_scene"):
        def llm(messages, max_tokens=None, **kw):
            sys_text = messages[0].get("content", "") if messages else ""
            if "시간 관찰자" in sys_text:  # time_classifier 호출
                return json.dumps({"category": category, "reason": "t"}), "", {}
            return json.dumps({
                "content": "안녕하세요.", "action_note": "", "target": "all",
                "move_to": None, "update_appearance": None,
            }), "", {}
        return llm

    def _run(self, *, time_mode="fixed", time_per_wave=30, target=None,
             max_waves=20, elapsed_init=0, category="normal_scene"):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        with tempfile.TemporaryDirectory() as tmp:
            agents = {
                key: Agent(key, f"너는 {key}다.", tmp, token_limit=4096)
                for key in ("a", "b")
            }
            sim = Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
                llm=self._llm(category),
                time_per_wave=time_per_wave, time_mode=time_mode,
                elapsed_minutes_init=elapsed_init,
            )
            emitted = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim.run("a", max_waves=max_waves, step_delay=0.0,
                    target_duration_minutes=target)
            end = [d for t, d in emitted if t == "simulation_end"][-1]
            return sim, end

    def test_fixed_mode_stops_when_target_reached(self):
        # tpw=30, target=60 → wave 0,1 실행 후 (1+1)*30 = 60 도달
        sim, end = self._run(time_per_wave=30, target=60, max_waves=20)

        self.assertEqual(sim.completed_waves, 2)
        self.assertEqual(end["end_reason"], "target_duration")

    def test_max_waves_still_caps_when_target_is_far(self):
        sim, end = self._run(time_per_wave=30, target=600, max_waves=3)

        self.assertEqual(sim.completed_waves, 3)
        self.assertEqual(end["end_reason"], "max_waves")

    def test_variable_mode_stops_on_accumulated_minutes(self):
        sim, end = self._run(time_mode="variable", target=20, max_waves=20,
                             category="meal_or_brief")  # wave당 5~10분

        self.assertEqual(end["end_reason"], "target_duration")
        self.assertGreaterEqual(sim._elapsed_minutes, 20)
        self.assertLessEqual(sim.completed_waves, 4)

    def test_resume_gets_a_fresh_duration_budget(self):
        # 복원된 누적 경과(elapsed_minutes_init)가 이미 목표를 넘었어도 즉시 멈추지
        # 않고, 이번 실행에서 목표 기간만큼 더 진행한다 (max_waves와 같은 성격).
        sim, end = self._run(time_mode="variable", target=20, max_waves=20,
                             category="meal_or_brief", elapsed_init=500)

        self.assertEqual(end["end_reason"], "target_duration")
        self.assertGreaterEqual(sim._elapsed_minutes - 500, 20)
        self.assertGreaterEqual(sim.completed_waves, 2)

    def test_target_ignored_when_time_concept_disabled(self):
        # fixed + time_per_wave=0 = 시간 개념 비활성 → 목표 기간은 무시(에러 아님)
        sim, end = self._run(time_per_wave=0, target=10, max_waves=3)

        self.assertEqual(sim.completed_waves, 3)
        self.assertEqual(end["end_reason"], "max_waves")

    def test_none_target_keeps_legacy_behavior(self):
        sim, end = self._run(target=None, max_waves=4)

        self.assertEqual(sim.completed_waves, 4)
        self.assertEqual(end["end_reason"], "max_waves")

    def test_schema_defaults_and_validation(self):
        cfg = SimStartConfig(agents=[_agent("a")], background="", start_agent="a")
        self.assertIsNone(cfg.target_duration_minutes)  # 구버전 설정 = 미사용

        cfg = SimStartConfig(agents=[_agent("a")], background="", start_agent="a",
                             target_duration_minutes=480)
        self.assertEqual(cfg.target_duration_minutes, 480)

        for bad in (0, -30):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    SimStartConfig(agents=[_agent("a")], background="",
                                   start_agent="a", target_duration_minutes=bad)

        self.assertIsNone(SimContinueConfig(start_agent="a").target_duration_minutes)
        self.assertEqual(
            SimContinueConfig(start_agent="a", target_duration_minutes=60)
            .target_duration_minutes,
            60,
        )


class _ScriptedLLM:
    """에이전트별·호출순서별 응답을 미리 정해두는 스텁 LLM.

    script = {"a": [{"content":..., "target":..., "move_to":...,
                     "update_appearance":...}, ...], ...}
    시스템 프롬프트의 "너는 {key}다." 로 화자를 식별한다. 스크립트가 소진되면
    마지막 항목을 반복한다. 생략된 필드는 None(=미사용).
    """

    def __init__(self, script: dict):
        self.script = script
        self.calls: dict[str, int] = {}

    def __call__(self, messages, max_tokens=None, **kw):
        sys_text = messages[0].get("content", "") if messages else ""
        if "시간 관찰자" in sys_text:  # time_classifier 호출
            return json.dumps({"category": "normal_scene", "reason": "t"}), "", {}
        key = next(k for k in self.script if f"너는 {k}다." in sys_text)
        idx = self.calls.get(key, 0)
        self.calls[key] = idx + 1
        turns = self.script[key]
        turn  = turns[idx] if idx < len(turns) else turns[-1]
        return json.dumps({
            "content":           turn.get("content", "..."),
            "action_note":       "",
            "target":            turn.get("target", "self"),
            "move_to":           turn.get("move_to"),
            "update_appearance": turn.get("update_appearance"),
        }), "", {}


class MoveRoutingTests(unittest.TestCase):
    """move_to(이동)와 발화 라우팅/씬 통보의 순서 회귀 (ABM/simulation/runner.py).

    핵심 불변식: 발화 라우팅은 turn.py의 엣지 기록과 **같은 이동 전 스냅샷**으로
    판정돼야 한다. 이동 후 스냅샷으로 재해석하면 같은 턴에 발화하며 떠난
    에이전트의 말이 그래프엔 남고 수신자에겐 안 가는 "유령 발화"가 된다.
    """

    LOCATION_GRAPH = [
        {"name": "입구", "connects_to": ["매장"]},
        {"name": "매장", "connects_to": ["입구", "창고"]},
        {"name": "창고", "connects_to": ["매장"]},
    ]

    def _run(self, script, *, max_waves=1, early_stop_enabled=True, locations=None,
             resume_wave=None):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        with tempfile.TemporaryDirectory() as tmp:
            agents = {
                key: Agent(key, f"너는 {key}다.", tmp, token_limit=4096)
                for key in ("a", "b")
            }
            sim = Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
                llm=_ScriptedLLM(script),
                agent_locations=locations or {"a": "매장", "b": "매장"},
                location_graph=self.LOCATION_GRAPH,
            )
            emitted = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim.run("a", max_waves=max_waves, step_delay=0.0,
                    early_stop_enabled=early_stop_enabled, resume_wave=resume_wave)
            return sim, emitted

    @staticmethod
    def _spoken(msgs):
        """씬 메시지를 제외한 실제 발화만."""
        return [m for m in msgs if m["speaker"] != "씬"]

    def test_farewell_reaches_origin_room(self):
        # a가 같은 턴에 b에게 말하면서 창고로 떠난다. 이동 후 위치로 라우팅하면
        # (a@창고 vs b@매장) 이 작별 인사가 통째로 폐기된다.
        sim, _ = self._run({
            "a": [{"content": "먼저 갈게.", "target": ["b"], "move_to": "창고"}],
            "b": [{"content": "응.",       "target": "self"}],
        })

        self.assertIn("b", sim._pending_wave)
        self.assertEqual(
            [m["content"] for m in self._spoken(sim._pending_wave["b"])],
            ["먼저 갈게."],
        )
        self.assertEqual(sim._agent_location["a"], "창고")

    def test_edge_and_routing_agree(self):
        # turn_complete.new_edges(그래프·피드·DB)와 실제 next_wave 수신자가
        # 같은 진실 원천을 봐야 한다. 서로를 타겟하도록 wave 0에 둘 다 투입.
        sim, emitted = self._run(
            {
                "a": [{"content": "먼저 갈게.",  "target": ["b"], "move_to": "창고"}],
                "b": [{"content": "어, 조심해.", "target": ["a"]}],
            },
            resume_wave={"a": [], "b": []},
        )

        edge_targets = {
            e["target"]
            for t, d in emitted if t == "turn_complete"
            for e in d["new_edges"]
        }
        routed_recipients = {
            key for key, msgs in sim._pending_wave.items()
            if self._spoken(msgs)
        }

        self.assertEqual(edge_targets, {"a", "b"})
        self.assertEqual(edge_targets, routed_recipients)

    def test_departure_scene_injected_at_origin(self):
        # 내부 → 내부 이동. 도착지 알림만 있고 출발지 알림이 없으면 남은 쪽은
        # 상대가 떠난 걸 모른 채 계속 말을 건다.
        sim, _ = self._run({
            "a": [{"content": "창고 좀 볼게.", "target": "self", "move_to": "창고"}],
            "b": [{"content": "음.",          "target": "self"}],
        })

        scene = [m["content"] for m in sim._pending_wave.get("b", [])
                 if m["speaker"] == "씬"]
        self.assertEqual(scene, ["[씬] a이(가) 자리를 떠났다."])

    def test_situation_context_reflects_new_location(self):
        # 이미 정상 동작하는 경로의 회귀 가드 — 라우팅 순서를 바꿔도 다음 wave의
        # [현재 상황] 블록은 이동 후 위치를 반영해야 한다.
        # b를 입구에 두어 wave 0의 next_wave가 비게 하고(씬 주입 대상 없음),
        # early_stop_enabled=False로 전원 재투입시켜 a가 wave 1에도 돌게 한다.
        sim, emitted = self._run(
            {
                "a": [{"content": "창고 좀 볼게.", "target": "self", "move_to": "창고"},
                      {"content": "여긴 창고군.", "target": "self"}],
                "b": [{"content": "음.", "target": "self"}],
            },
            max_waves=2, early_stop_enabled=False,
            locations={"a": "매장", "b": "입구"},
        )

        situations = {
            (d["agent"], d["wave"]): d["text"]
            for t, d in emitted if t == "turn_situation"
        }
        self.assertIn("현재 위치: 매장", situations[("a", 0)])
        self.assertIn("현재 위치: 창고", situations[("a", 1)])
        self.assertIn("현재 위치: 입구", situations[("b", 1)])


class ZoneAwarenessTests(unittest.TestCase):
    """LocationNode.zone — 인지 범위(같은 zone)와 대화 범위(같은 노드)의 분리.

    핵심 불변식: zone은 "저기 누가 있다"는 인지 정보만 넓힌다. 같은 zone이어도
    노드가 다르면 _resolve_targets()는 여전히 타깃을 폐기해야 한다. 이게 깨지면
    zone이 사실상 방 벽을 없애버려 위치 시스템 자체가 무의미해진다.

    주의: 여기서의 zone은 위치 개념이며, agent_groups(캐릭터 관계 그룹)와는 별개다.
    """

    LOCATION_GRAPH = [
        {"name": "안방",   "connects_to": ["거실"],                 "zone": "우리집"},
        {"name": "거실",   "connects_to": ["안방", "부엌", "마당"], "zone": "우리집"},
        {"name": "부엌",   "connects_to": ["거실"],                 "zone": "우리집"},
        {"name": "마당",   "connects_to": ["거실"]},  # zone 미설정
        {"name": "교실",   "connects_to": ["운동장"],               "zone": "학교"},
        {"name": "운동장", "connects_to": ["교실"],                 "zone": "학교"},
        {"name": "현관밖", "connects_to": ["마당"], "zone": "우리집", "is_exterior": True},
    ]

    # a=안방, b=거실(같은 zone/다른 노드), c=교실(다른 zone), d=마당(zone 없음),
    # e=현관밖(zone은 우리집이지만 외부 공간), f=안방(a와 같은 노드)
    LOCATIONS = {"a": "안방", "b": "거실", "c": "교실", "d": "마당", "e": "현관밖", "f": "안방"}

    def _make_sim(self, tmp, script=None, **kw):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {
            key: Agent(key, f"너는 {key}다.", tmp, token_limit=4096)
            for key in self.LOCATIONS
        }
        return Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM(script or {}),
            agent_locations=dict(self.LOCATIONS),
            location_graph=self.LOCATION_GRAPH,
            **kw,
        )

    def _situation(self, sim, agent_key):
        known, strangers = sim._compute_wave_targets(agent_key)
        zone_awareness   = sim._compute_zone_awareness(agent_key)
        return sim._build_situation_context(agent_key, known, strangers, zone_awareness)

    def test_same_zone_other_room_is_perceived_but_not_addressable(self):
        # a(안방)는 같은 "우리집" zone의 거실에 있는 b를 인지해야 한다 — 그래야
        # move_to로 찾아갈 동기가 생긴다. 하지만 말을 걸 수는 없어야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp)
            text = self._situation(sim, "a")

            self.assertIn("[같은 구역(우리집)의 다른 곳]", text)
            self.assertIn("거실", text.split("[같은 구역(우리집)의 다른 곳]")[1])
            self.assertIn('(ID: "b")', text)

            # 인지는 되지만 대화 스코프는 여전히 같은 노드 기준.
            known, strangers = sim._compute_wave_targets("a")
            self.assertEqual(known, ["f"])          # 같은 안방의 f만 대화 가능
            self.assertEqual(strangers, [])
            self.assertEqual(sim._resolve_targets(["b"], "a"), [])
            self.assertEqual(sim._resolve_targets(["all"], "a"), ["f"])

            # 같은 노드(f)는 [이 자리의 사람들]에만, zone 섹션엔 중복 노출 금지.
            zone_section = text.split("[같은 구역(우리집)의 다른 곳]")[1]
            self.assertNotIn('"f"', zone_section)

    def test_other_zone_and_zoneless_and_exterior_are_not_perceived(self):
        # c=학교 zone, d=zone 미설정, e=외부 공간(zone이 우리집이어도 격리).
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp)
            zone_section = self._situation(sim, "a").split("[같은 구역(우리집)의 다른 곳]")[1]

            for key, loc in (("c", "교실"), ("d", "마당"), ("e", "현관밖")):
                self.assertNotIn(f'"{key}"', zone_section)
                self.assertNotIn(loc, zone_section)

            known_elsewhere, strangers_elsewhere = sim._compute_zone_awareness("a")
            self.assertEqual(known_elsewhere, [("b", "거실")])
            self.assertEqual(strangers_elsewhere, [])

            # zone 미설정 노드(마당)에 있는 d에겐 zone 섹션 자체가 없어야 한다.
            self.assertEqual(sim._compute_zone_awareness("d"), ([], []))
            self.assertNotIn("[같은 구역", self._situation(sim, "d"))
            # 외부 공간(e)은 기존대로 완전 격리.
            self.assertEqual(sim._compute_zone_awareness("e"), ([], []))
            self.assertNotIn("[같은 구역", self._situation(sim, "e"))

    def test_zone_stranger_gets_stable_id_and_stays_unaddressable(self):
        # 낯선 사람을 zone 너머로 인지할 때도 stranger_N 체계를 공유해야, 나중에
        # 실제로 만났을 때 ID가 흔들리지 않는다. 그 ID로 말을 걸 순 없어야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp, agent_groups={"a": ["가족"], "f": ["가족"]})
            _, strangers_elsewhere = sim._compute_zone_awareness("a")
            self.assertEqual([(sid, key) for sid, key, _, _ in strangers_elsewhere],
                             [("stranger_1", "b")])

            self.assertEqual(sim._resolve_targets(["stranger_1"], "a"), [])

            # 같은 방으로 옮겨오면 같은 ID가 유지되고 그제서야 대화 가능.
            sim._agent_location["b"] = "안방"
            self.assertEqual(sim._compute_zone_awareness("a"), ([], []))
            _, strangers = sim._compute_wave_targets("a")
            self.assertEqual([(sid, key) for sid, key, _ in strangers], [("stranger_1", "b")])
            self.assertEqual(sim._resolve_targets(["stranger_1"], "a"), ["b"])

    def test_zone_section_appears_in_running_simulation_prompt(self):
        # _assemble_agent_prompt 경유 end-to-end: turn_situation에 zone 섹션이 실리고,
        # zone 인지 대상이 <TARGETS>(visible_agents)로는 새지 않아야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp, script={
                k: [{"content": "...", "target": "self"}] for k in self.LOCATIONS
            })
            emitted = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim.run("a", max_waves=1, step_delay=0.0)

            texts = [d["text"] for t, d in emitted
                     if t == "turn_situation" and d["agent"] == "a"]
            self.assertTrue(texts)
            self.assertIn("[같은 구역(우리집)의 다른 곳]", texts[0])

            ctx = sim._assemble_agent_prompt("a")
            self.assertNotIn("b", ctx["visible_agents"])


class AppearanceUpdateTests(unittest.TestCase):
    """update_appearance(외모 변경) 처리의 순서·익명화·격리 회귀.

    핵심 불변식 3가지:
      1. 순서 — 외모 변경은 이동 **적용 전** 위치 스냅샷으로 판정된다. MoveRoutingTests가
         발화 라우팅에 대해 지키는 것과 같은 불변식이다. 깨지면 실제 목격자(출발지
         동석자)가 알림을 놓치고, 도착 알림이 갱신 전 옛 외모를 실어 뒤따르는 외모
         알림과 모순되는 두 줄이 도착지에 꽂힌다.
      2. 익명화 — 모르는 사이에게는 실명 대신 stranger_N ID로 나간다. 도착/이탈 씬
         메시지와 같은 knowledge 분기를 타야 한다. 새어나간 실명은 단순 노출로 끝나지
         않고 stranger_N 핸드셰이크를 우회하는 유효 타깃이 된다.
      3. 격리 — 외부 공간(is_exterior)에는 씬 메시지가 오가지 않는다.
    """

    LOCATION_GRAPH = [
        {"name": "입구", "connects_to": ["매장"]},
        {"name": "매장", "connects_to": ["입구", "창고"]},
        {"name": "창고", "connects_to": ["매장", "옥상"]},
        {"name": "옥상", "connects_to": ["창고"], "is_exterior": True},
    ]

    def _build(self, tmp, keys, script, locations, visuals, groups, **kw):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in keys}
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM(script),
            agent_locations=locations,
            agent_visuals=visuals,
            agent_groups=groups,
            location_graph=self.LOCATION_GRAPH,
            **kw,
        )
        sim._emit = lambda t, d: None
        return sim

    @staticmethod
    def _scenes(msgs):
        return [m["content"] for m in msgs if m["speaker"] == "씬"]

    # ── A-1 / A-2: 이동 순서 ─────────────────────────────────────────────────

    def test_appearance_change_reaches_origin_room_when_speaker_also_moves(self):
        # a가 같은 턴에 옷을 갈아입고 매장 → 창고로 떠난다. 눈앞에서 그걸 본 건
        # 출발지(매장)의 b다. 외모 처리가 이동 뒤에 있으면 my_loc이 창고가 되어
        # b는 알림을 못 받고, 그 자리에 없던 c가 대신 받는다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b", "c"),
                script={
                    "a": [{"content": "간다.", "target": "self", "move_to": "창고",
                           "update_appearance": "빨간 코트를 걸친 사람"}],
                    "b": [{"content": "음.", "target": "self"}],
                    "c": [{"content": "흠.", "target": "self"}],
                },
                locations={"a": "매장", "b": "매장", "c": "창고"},
                visuals={"a": "검은 코트를 걸친 사람", "b": "", "c": ""},
                groups={"a": ["g1"], "b": ["g2"], "c": ["g3"]},
            )
            sim.run("a", max_waves=1, step_delay=0.0,
                    resume_wave={"a": [], "b": [], "c": []})

            self.assertEqual(sim._agent_location["a"], "창고")
            self.assertEqual(sim._agent_visual["a"], "빨간 코트를 걸친 사람")

            b_scenes = self._scenes(sim._pending_wave.get("b", []))
            # 목격자 b는 외모 변화 + 이탈을 둘 다, 그 순서대로 받는다.
            self.assertEqual(len(b_scenes), 2)
            self.assertIn("외모가 변했다", b_scenes[0])
            self.assertIn("빨간 코트를 걸친 사람", b_scenes[0])
            self.assertIn("자리를 떠났다", b_scenes[1])

    def test_arrival_notice_carries_new_appearance_without_duplicate(self):
        # 도착지 c는 갱신된 새 외모가 실린 도착 알림 **한 줄만** 받아야 한다.
        # 예전엔 옛 외모("검은 코트")로 도착 알림이 나간 뒤 새 외모 알림이 또 와서,
        # c에게는 두 사람이 들어온 것처럼 읽히는 모순된 두 줄이 됐다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b", "c"),
                script={
                    "a": [{"content": "간다.", "target": "self", "move_to": "창고",
                           "update_appearance": "빨간 코트를 걸친 사람"}],
                    "b": [{"content": "음.", "target": "self"}],
                    "c": [{"content": "흠.", "target": "self"}],
                },
                locations={"a": "매장", "b": "매장", "c": "창고"},
                visuals={"a": "검은 코트를 걸친 사람", "b": "", "c": ""},
                groups={"a": ["g1"], "b": ["g2"], "c": ["g3"]},
            )
            sim.run("a", max_waves=1, step_delay=0.0,
                    resume_wave={"a": [], "b": [], "c": []})

            c_scenes = self._scenes(sim._pending_wave.get("c", []))
            self.assertEqual(c_scenes, ["[씬] 낯선 이가 나타났다: 빨간 코트를 걸친 사람"])
            # 무효한 옛 외모가 남아있으면 안 된다.
            self.assertNotIn("검은 코트", " ".join(c_scenes))

    # ── B-1 / B-2: 실명 노출 ─────────────────────────────────────────────────

    def test_runtime_appearance_change_is_anonymized_for_strangers(self):
        # b에게 a는 확실히 '낯선 이'다(그룹 분리). 실명 'a'가 새면 안 되고,
        # [현재 상황] 블록과 같은 stranger_N ID로 특정 가능해야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b"),
                script={
                    "a": [{"content": "옷을 갈아입는다.", "target": "self",
                           "update_appearance": "빨간 코트를 걸친 사람"}],
                    "b": [{"content": "음.", "target": "self"}],
                },
                locations={"a": "매장", "b": "매장"},
                visuals={"a": "검은 코트를 걸친 사람", "b": ""},
                groups={"a": ["g1"], "b": ["g2"]},
                name_aliases={"민준": "a", "서연": "b"},
            )
            known, strangers = sim._compute_wave_targets("b")
            self.assertEqual(known, [])
            self.assertEqual([(s[0], s[1]) for s in strangers], [("stranger_1", "a")])

            sim.run("a", max_waves=1, step_delay=0.0, resume_wave={"a": [], "b": []})

            b_scenes = self._scenes(sim._pending_wave.get("b", []))
            self.assertEqual(
                b_scenes,
                ['[씬] 낯선 이(ID: "stranger_1")의 외모가 변했다: 빨간 코트를 걸친 사람'],
            )
            self.assertNotIn("민준", " ".join(b_scenes))  # 별칭도 새면 안 됨

    def test_known_agents_still_see_real_name(self):
        # 익명화가 과하게 걸려 아는 사이끼리도 실명을 잃으면 안 된다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b"),
                script={
                    "a": [{"content": "옷을 갈아입는다.", "target": "self",
                           "update_appearance": "빨간 코트"}],
                    "b": [{"content": "음.", "target": "self"}],
                },
                locations={"a": "매장", "b": "매장"},
                visuals={"a": "검은 코트", "b": ""},
                groups={"a": ["가족"], "b": ["가족"]},   # 서로 아는 사이
                name_aliases={"민준": "a"},
            )
            sim.run("a", max_waves=1, step_delay=0.0, resume_wave={"a": [], "b": []})

            self.assertEqual(
                self._scenes(sim._pending_wave.get("b", [])),
                ["[씬] 민준의 외모가 변했다: 빨간 코트"],
            )

    def test_scripted_event_appearance_change_is_anonymized(self):
        # events.py 경로(시나리오 스크립트 이벤트)도 런타임과 같은 규칙을 써야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b"), script={},
                locations={"a": "매장", "b": "매장"},
                visuals={"a": "검은 코트를 걸친 사람", "b": ""},
                groups={"a": ["g1"], "b": ["g2"]},
            )
            sim._execute_event({
                "type": "update_appearance", "agent": "a",
                "message": "빨간 코트를 걸친 사람", "targets": ["all"], "wave": 0,
            })

            injected = [m["content"] for m in sim.agents["b"].memory
                        if "[씬]" in str(m.get("content", ""))]
            self.assertEqual(
                injected,
                ['[씬] 낯선 이(ID: "stranger_1")의 외모가 변했다: 빨간 코트를 걸친 사람'],
            )
            self.assertEqual(sim._agent_visual["a"], "빨간 코트를 걸친 사람")

    def test_leaked_name_cannot_bypass_stranger_handshake(self):
        # C-2: 어떤 경로로든 이름을 알게 돼도, 낯선 이 상태에서는 실명/key로 말을
        # 걸 수 없어야 한다. 정상 경로(stranger_N)는 그대로 동작해야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b"),
                script={
                    "a": [{"content": "옷 갈아입음", "target": "self",
                           "update_appearance": "빨간 코트"}],
                    "b": [{"content": "...", "target": "self"}],
                },
                locations={"a": "매장", "b": "매장"},
                visuals={"a": "검은 코트", "b": ""},
                groups={"a": ["g1"], "b": ["g2"]},
                name_aliases={"민준": "a", "서연": "b"},
            )
            sim.run("a", max_waves=1, step_delay=0.0, resume_wave={"a": [], "b": []})

            self.assertEqual(sim._resolve_targets(["민준"], "b"), [])  # 별칭
            self.assertEqual(sim._resolve_targets(["a"], "b"), [])     # 원본 key
            # 정상 핸드셰이크는 통하고, 그 후에는 실명 타깃도 열린다.
            self.assertEqual(sim._resolve_targets(["stranger_1"], "b"), ["a"])
            self.assertEqual(sim._resolve_targets(["민준"], "b"), ["a"])

    def test_legacy_scenario_without_groups_or_locations_still_routes(self):
        # C-2 회귀 방지(가장 중요) — 위치·그룹을 아예 쓰지 않는 레거시 시나리오는
        # knowledge 검사 때문에 대화가 막히면 안 된다. core.py가 그룹 미설정
        # 에이전트의 knowledge에 전원을 넣으므로 낯선 이 자체가 존재하지 않는다.
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        with tempfile.TemporaryDirectory() as tmp:
            agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096)
                      for k in ("a", "b")}
            sim = Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
                llm=_ScriptedLLM({
                    "a": [{"content": "안녕.", "target": ["b"]}],
                    "b": [{"content": "응.",   "target": ["a"]}],
                }),
                name_aliases={"민준": "a", "서연": "b"},
            )   # agent_locations / agent_groups / location_graph 전부 미설정
            sim._emit = lambda t, d: None

            self.assertEqual(sim._resolve_targets(["b"], "a"), ["b"])
            self.assertEqual(sim._resolve_targets(["서연"], "a"), ["b"])
            self.assertEqual(sim._resolve_targets(["all"], "a"), ["b"])

            sim.run("a", max_waves=1, step_delay=0.0, resume_wave={"a": [], "b": []})
            self.assertEqual(
                [m["content"] for m in sim._pending_wave.get("b", [])], ["안녕."]
            )

    def test_location_only_scenario_without_groups_still_routes(self):
        # 같은 회귀 방지 — 위치는 쓰되 그룹은 안 쓰는 시나리오(전원 아는 사이).
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b"),
                script={"a": [{"content": "안녕.", "target": ["b"]}],
                        "b": [{"content": "응.",   "target": ["a"]}]},
                locations={"a": "매장", "b": "매장"},
                visuals={"a": "", "b": ""},
                groups=None,
            )
            self.assertEqual(sim._resolve_targets(["b"], "a"), ["b"])
            sim.run("a", max_waves=1, step_delay=0.0, resume_wave={"a": [], "b": []})
            self.assertEqual(
                [m["content"] for m in sim._pending_wave.get("b", [])], ["안녕."]
            )

    # ── C-1: 외부 공간 격리 ──────────────────────────────────────────────────

    def test_exterior_space_isolation_is_preserved(self):
        # 같은 옥상(is_exterior)에 있어도 서로 볼 수 없다 — _compute_wave_targets가
        # 빈 스코프를 주는 것과 대칭으로, 외모 알림도 오가면 안 된다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b"),
                script={
                    "a": [{"content": "...", "target": "self",
                           "update_appearance": "빨간 코트"}],
                    "b": [{"content": "...", "target": "self"}],
                },
                locations={"a": "옥상", "b": "옥상"},
                visuals={"a": "검은 코트", "b": ""},
                groups={"a": ["g1"], "b": ["g2"]},
            )
            self.assertEqual(sim._compute_wave_targets("b"), ([], []))
            sim.run("a", max_waves=1, step_delay=0.0, resume_wave={"a": [], "b": []})

            self.assertEqual(self._scenes(sim._pending_wave.get("b", [])), [])
            # 격리돼도 외모 자체는 갱신된다 — 내부로 돌아오면 새 외모가 보여야 한다.
            self.assertEqual(sim._agent_visual["a"], "빨간 코트")

    def test_agent_in_exterior_does_not_receive_appearance_notice(self):
        # 내부에 있는 a의 외모 변화가 외부 공간(옥상)의 b에게 새면 안 된다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b"),
                script={
                    "a": [{"content": "...", "target": "self",
                           "update_appearance": "빨간 코트"}],
                    "b": [{"content": "...", "target": "self"}],
                },
                locations={"a": "창고", "b": "옥상"},
                visuals={"a": "검은 코트", "b": ""},
                groups={"a": ["가족"], "b": ["가족"]},   # 아는 사이여도 격리 우선
            )
            sim.run("a", max_waves=1, step_delay=0.0, resume_wave={"a": [], "b": []})
            self.assertEqual(self._scenes(sim._pending_wave.get("b", [])), [])

    def test_scripted_event_respects_exterior_isolation(self):
        # events.py 경로도 같은 격리 가드를 가져야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._build(
                tmp, ("a", "b", "c"), script={},
                locations={"a": "옥상", "b": "옥상", "c": "창고"},
                visuals={"a": "검은 코트", "b": "", "c": ""},
                groups={"a": ["가족"], "b": ["가족"], "c": ["가족"]},
            )
            sim._execute_event({
                "type": "update_appearance", "agent": "a",
                "message": "빨간 코트", "targets": ["all"], "wave": 0,
            })
            for key in ("b", "c"):
                injected = [m["content"] for m in sim.agents[key].memory
                            if "[씬]" in str(m.get("content", ""))]
                self.assertEqual(injected, [], f"{key}에게 누출됨")
            self.assertEqual(sim._agent_visual["a"], "빨간 코트")


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmpdir.name) / "memory.db"
        conn = get_db()
        init_tables(conn)
        migrate_db(conn)
        now = "2026-05-22T00:00:00"
        conn.execute(
            """INSERT INTO conversations
               (id, title, system_prompt, agent_id, router_mode, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            ("conv-1", "새 대화", "", None, 0, now, now),
        )
        conn.commit()
        conn.close()

        self.old_registry = conversations.get_registry
        self.old_stream_chat = conversations.async_stream_chat
        self.old_extract_keywords = _conv_helpers.async_extract_keywords
        conversations.get_registry = lambda: FakeRegistry()
        _conv_helpers.async_extract_keywords = self._empty_keywords
        conversations.async_stream_chat = self._failing_stream

    async def asyncTearDown(self):
        conversations.get_registry = self.old_registry
        conversations.async_stream_chat = self.old_stream_chat
        _conv_helpers.async_extract_keywords = self.old_extract_keywords
        config.DB_PATH = self.old_db_path
        self.tmpdir.cleanup()

    async def _empty_keywords(self, text):
        return []

    async def _failing_stream(self, *args, **kwargs):
        raise RuntimeError("model down")
        yield

    async def test_user_turn_is_saved_when_llm_stream_fails(self):
        response = await conversations.send_chat(
            "conv-1",
            ChatMessage(content="hello", thinking=False),
        )

        chunks = [chunk async for chunk in response.body_iterator]

        conn = sqlite3.connect(config.DB_PATH)
        rows = conn.execute(
            "SELECT role, content FROM turns WHERE conversation_id=? ORDER BY created_at",
            ("conv-1",),
        ).fetchall()
        conn.close()

        self.assertEqual(rows, [("user", "hello")])
        self.assertTrue(any("event: error" in chunk for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
