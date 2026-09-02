import asyncio
import json
import os
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
from backend.api import servers as servers_api
from backend.api.schemas import ChatMessage, ServerCreate, ServerUpdate
from backend.db.database import (
    get_db, init_tables, migrate_db, seed_default_servers,
)
from backend.llm.providers.anthropic import AnthropicProvider
from backend.api.simulation import runtime as sim_runtime
from backend.api.simulation.schemas import AgentConfig, SimContinueConfig, SimStartConfig
from backend.llm import bridge
from backend.llm import client as llm_client
from backend.llm import registry as llm_registry
from backend.llm.providers.base import LLMHTTPError
from backend.llm.providers.openai import OpenAIProvider
from backend.llm.providers.vllm import VLLMProvider, _extract_reply
from backend.llm.registry import NoProviderError
from backend.llm.pipeline import build_messages
from backend.websearch import service as websearch_service
from backend.websearch.context import format_search_context
from backend.websearch.schemas import SearchResult
from backend.websearch.providers.duckduckgo import (
    DuckDuckGoProvider, _unwrap_ddg_url,
)


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
        self.seen_thinking_levels = []

    async def chat(self, messages, *, temperature=0.7, max_tokens=4096, timeout=None,
                   thinking_level=None):
        self.calls += 1
        self.seen_timeouts.append(timeout)
        self.seen_temperatures.append(temperature)
        self.seen_thinking_levels.append(thinking_level)
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


class ZoneExitTests(unittest.TestCase):
    """Zone 입구 노드 + 구역 밖으로 1홉 탈출 (SPEC PART 1).

    핵심 불변식: 그래프 파싱 직후 컴파일 단계에서 zone 엣지를 노드 엣지로 전개한다.
    전개 후 _location_graph 는 여전히 순수 노드 인접 리스트라 _find_path/_get_adjacent/
    인지 로직 전부 무변경. zone 참조가 없으면 전개는 no-op(하위 호환 100%).
    """

    # 집 = {현관(입구), 거실, 침실} 선형. 길거리는 집 밖, connects_to 에 zone명 "집".
    GRAPH = [
        {"name": "침실", "connects_to": ["거실"],        "zone": "집"},
        {"name": "거실", "connects_to": ["침실", "현관"], "zone": "집"},
        {"name": "현관", "connects_to": ["거실"], "zone": "집", "is_zone_entry": True},
        {"name": "길거리", "connects_to": ["집", "회사"]},
        {"name": "회사", "connects_to": ["길거리"]},
    ]

    def _make_sim(self, tmp, graph=None):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {"a": Agent("a", "너는 a다.", tmp, token_limit=4096)}
        return Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM({}),
            location_graph=self.GRAPH if graph is None else graph,
        )

    def test_zone_edges_expand_to_node_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp)
            g = sim._location_graph

            self.assertEqual(sim._zone_entry, {"집": "현관"})
            # 진입: 길거리 -> 현관 (입구). zone명 "집"은 사라진다.
            self.assertIn("현관", g["길거리"])
            self.assertNotIn("집", g["길거리"])
            # 탈출: 내부 모든 노드 -> 길거리
            self.assertIn("길거리", g["현관"])
            self.assertIn("길거리", g["거실"])
            self.assertIn("길거리", g["침실"])
            # X -> 비입구 내부 노드는 만들지 않는다
            self.assertNotIn("거실", g["길거리"])
            self.assertNotIn("침실", g["길거리"])

    def test_exit_is_one_hop_entry_is_multi_hop(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp)
            # 탈출: 침실에서 길거리로 1홉
            self.assertEqual(sim._find_path("침실", "길거리"), ["길거리"])
            # 진입: 길거리 -> 침실 은 현관 경유 다홉
            path = sim._find_path("길거리", "침실")
            self.assertEqual(path[0], "현관")
            self.assertIn("침실", path)
            self.assertGreater(len(path), 1)

    def test_cross_zone_commute_keeps_travel_time(self):
        # 길거리가 집·회사 두 zone 입구를 모두 참조 → 침실에서 회의실까지 순간이동이
        # 아니라 길거리를 거치는 다홉이어야 한다(통근 시간 유지).
        graph = [
            {"name": "침실", "connects_to": ["현관"], "zone": "집"},
            {"name": "현관", "connects_to": ["침실"], "zone": "집", "is_zone_entry": True},
            {"name": "길거리", "connects_to": ["집", "회사"]},
            {"name": "로비", "connects_to": ["회의실"], "zone": "회사", "is_zone_entry": True},
            {"name": "회의실", "connects_to": ["로비"], "zone": "회사"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp, graph=graph)
            self.assertEqual(sim._zone_entry, {"집": "현관", "회사": "로비"})
            # 탈출 1홉: 침실 -> 길거리
            self.assertEqual(sim._find_path("침실", "길거리"), ["길거리"])
            # 진입: 길거리 -> 로비 -> 회의실
            self.assertEqual(sim._find_path("길거리", "회의실"), ["로비", "회의실"])
            # 통근 전체는 3홉(침실->길거리->로비->회의실), 순간이동 아님
            self.assertEqual(sim._find_path("침실", "회의실"),
                             ["길거리", "로비", "회의실"])

    def test_zone_without_entry_reference_is_ignored(self):
        # zone "집"에 is_zone_entry 노드가 없다 → 길거리 -> 집 참조 무시, 크래시 없음.
        graph = [
            {"name": "침실", "connects_to": ["거실"], "zone": "집"},
            {"name": "거실", "connects_to": ["침실"], "zone": "집"},
            {"name": "길거리", "connects_to": ["집"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp, graph=graph)
            self.assertEqual(sim._zone_entry, {})
            self.assertNotIn("집", sim._location_graph["길거리"])
            # 탈출 엣지도 없어야 한다
            self.assertNotIn("길거리", sim._location_graph["침실"])

    def test_self_zone_reference_is_ignored(self):
        # 거실(zone=집)이 connects_to 에 "집"을 넣으면 자기 구역 참조 → 무시.
        graph = [
            {"name": "현관", "connects_to": ["거실"], "zone": "집", "is_zone_entry": True},
            {"name": "거실", "connects_to": ["현관", "집"], "zone": "집"},
            {"name": "길거리", "connects_to": ["집"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp, graph=graph)
            # "집"은 거실의 인접 목록에서 사라져야 하고, self-edge(거실->거실)도 없다
            self.assertNotIn("집", sim._location_graph["거실"])
            self.assertNotIn("거실", sim._location_graph["거실"])

    def test_backward_compat_no_zone_reference_is_noop(self):
        # zone 참조가 없는 기존 그래프는 전개 전후 _location_graph 가 동일해야 한다.
        graph = [
            {"name": "입구", "connects_to": ["매장"]},
            {"name": "매장", "connects_to": ["입구", "창고"], "zone": "가게"},
            {"name": "창고", "connects_to": ["매장"], "zone": "가게"},
        ]
        expected = {"입구": ["매장"], "매장": ["입구", "창고"], "창고": ["매장"]}
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp, graph=graph)
            self.assertEqual(sim._zone_entry, {})
            self.assertEqual(sim._location_graph, expected)
            # 재실행해도 변화 없음
            sim._expand_zone_edges(graph)
            self.assertEqual(sim._location_graph, expected)

    def test_duplicate_zone_entry_keeps_first(self):
        graph = [
            {"name": "현관", "connects_to": ["거실"], "zone": "집", "is_zone_entry": True},
            {"name": "뒷문", "connects_to": ["거실"], "zone": "집", "is_zone_entry": True},
            {"name": "거실", "connects_to": ["현관", "뒷문"], "zone": "집"},
            {"name": "길거리", "connects_to": ["집"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(tmp, graph=graph)
            self.assertEqual(sim._zone_entry, {"집": "현관"})
            self.assertIn("현관", sim._location_graph["길거리"])
            self.assertNotIn("뒷문", sim._location_graph["길거리"])

    def test_map_contract_marks_entry_and_adds_exit_rule(self):
        from ABM.prompt_contract import build_map_contract

        graph = {"침실": ["거실", "길거리"], "거실": ["침실", "현관", "길거리"],
                 "현관": ["거실", "길거리"], "길거리": ["현관"]}
        zones = {"침실": "집", "거실": "집", "현관": "집"}

        without = build_map_contract(location_graph=graph, location_zone=zones)
        self.assertNotIn("바깥으로 바로 나갈 수 있습니다", without)
        self.assertIn("현관 [구역: 집]", without)

        withe = build_map_contract(
            location_graph=graph, location_zone=zones, zone_entry={"집": "현관"},
        )
        self.assertIn("현관 [구역: 집, 입구]", withe)
        self.assertIn("거실 [구역: 집]", withe)  # 비입구는 그대로
        self.assertIn("바깥으로 바로 나갈 수 있습니다", withe)

    def test_world_contract_threads_zone_entry(self):
        from ABM.prompt_contract import build_world_contract

        graph = {"현관": ["거실", "길거리"], "거실": ["현관"], "길거리": ["현관"]}
        text = build_world_contract(
            location_graph=graph, location_zone={"현관": "집", "거실": "집"},
            zone_entry={"집": "현관"},
        )
        self.assertIn("현관 [구역: 집, 입구]", text)
        self.assertIn("바깥으로 바로 나갈 수 있습니다", text)


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


class InfectionTimeModelTests(unittest.TestCase):
    """감염 모델의 시간 축 (ABM/simulation/infection.py + core.py).

    핵심 불변식: **전염만 wave/접촉 기준**이고, 증상 단계 progression과 회복은
    시뮬레이션 내 경과 분(`_current_elapsed_minutes`) 기준이다. 이게 깨지면
    variable 모드에서 5분짜리 식사 wave와 7시간짜리 취침 wave가 병의 진행에
    똑같이 기여해 "밤새 앓았는데 증상은 그대로"류의 모순이 생긴다.

    LLM 계약도 함께 지킨다: 프롬프트에는 status·확률·경과분 같은 raw 값이 아니라
    시나리오가 쓴 symptom_text만 들어간다.
    """

    STAGES = [
        {"id": "incubation", "label": "잠복기", "min_minutes": 0,   "max_minutes": 119,
         "symptom_text": "아직 아무렇지도 않다."},
        {"id": "onset",      "label": "발현기", "min_minutes": 120, "max_minutes": 299,
         "symptom_text": "목이 칼칼하다."},
        {"id": "acute",      "label": "급성기", "min_minutes": 300, "max_minutes": 600,
         "symptom_text": "온몸이 불덩이다."},
    ]

    def _model(self, **over) -> dict:
        model = {
            "enabled":                  True,
            "disease_name":             "테스트열",
            "transmission_probability": 0.0,
            "symptom_stages":           [dict(s) for s in self.STAGES],
            "recovery_min_minutes":     0,
            "recovery_max_minutes":     0,   # 기본은 만성 — 회복이 단계 테스트를 방해하지 않게
            "immune_after_recovery":    True,
        }
        model.update(over)
        return model

    def _sim(self, tmp, *, infection, time_mode="fixed", time_per_wave=60,
             elapsed_init=0, keys=("a", "b"), locations=None):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in keys}
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM({k: [{"content": "...", "target": "self"}] for k in keys}),
            time_per_wave=time_per_wave, time_mode=time_mode,
            elapsed_minutes_init=elapsed_init,
            agent_locations=locations,
            infection_model=infection,
        )
        sim._emitted = []
        sim._emit = lambda t, d: sim._emitted.append((t, d))
        return sim

    @staticmethod
    def _symptom(sim, key, wave=None):
        """증상 컨텍스트에서 '[몸 상태]' 머리말을 뗀 본문. 없으면 None."""
        text = sim._build_symptom_context(key, wave)
        return None if text is None else text.split("\n", 1)[1]

    # ── 1. fixed 모드: 경과 분에 따라 단계가 바뀐다 ──────────────────────────────

    def test_fixed_mode_stage_follows_elapsed_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, infection=self._model(), time_per_wave=60)
            self.assertTrue(sim._set_infected("a", 0, "event"))

            # wave * 60분 = 경과분. 단계 경계는 분으로만 판정된다.
            self.assertEqual(self._symptom(sim, "a", 0),  "아직 아무렇지도 않다.")  #    0분
            self.assertEqual(self._symptom(sim, "a", 1),  "아직 아무렇지도 않다.")  #   60분
            self.assertEqual(self._symptom(sim, "a", 2),  "목이 칼칼하다.")        #  120분
            self.assertEqual(self._symptom(sim, "a", 5),  "온몸이 불덩이다.")      #  300분
            # 정의 범위(600분)를 넘어가면 가장 늦은 단계를 계속 유지한다.
            self.assertEqual(self._symptom(sim, "a", 40), "온몸이 불덩이다.")      # 2400분

    def test_same_wave_count_gives_different_stage_when_wave_is_longer(self):
        # 같은 "2 wave 경과"라도 wave 길이가 다르면 단계가 달라야 한다 —
        # 이 어서션이 실패하면 모델이 다시 wave 기준으로 되돌아간 것이다.
        with tempfile.TemporaryDirectory() as tmp:
            short = self._sim(tmp, infection=self._model(), time_per_wave=10)
            long  = self._sim(tmp, infection=self._model(), time_per_wave=180)
            short._set_infected("a", 0, "event")
            long._set_infected("a", 0, "event")

            self.assertEqual(self._symptom(short, "a", 2), "아직 아무렇지도 않다.")  #  20분
            self.assertEqual(self._symptom(long,  "a", 2), "온몸이 불덩이다.")       # 360분

    # ── 2. variable 모드: 누적 _elapsed_minutes로 단계 전이 ──────────────────────

    def test_variable_mode_stage_follows_accumulated_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, infection=self._model(), time_mode="variable",
                            time_per_wave=0)
            self.assertTrue(sim._set_infected("a", 0, "event"))
            self.assertEqual(sim._agent_infection["a"]["infected_at_minutes"], 0)

            # variable 모드에서는 wave 번호가 아니라 누적 경과분만이 기준이다.
            self.assertEqual(self._symptom(sim, "a", 99), "아직 아무렇지도 않다.")
            sim._elapsed_minutes = 150
            self.assertEqual(self._symptom(sim, "a", 0),  "목이 칼칼하다.")
            sim._elapsed_minutes = 400
            self.assertEqual(self._symptom(sim, "a", 0),  "온몸이 불덩이다.")

    def test_variable_mode_anchors_infection_at_current_elapsed(self):
        # 유행 도중(경과 500분)에 감염된 사람은 500분을 0으로 삼아 다시 잠복기부터.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, infection=self._model(), time_mode="variable",
                            time_per_wave=0, elapsed_init=500)
            sim._set_infected("a", 3, "transmission")

            self.assertEqual(sim._agent_infection["a"]["infected_at_minutes"], 500)
            self.assertEqual(self._symptom(sim, "a", 3), "아직 아무렇지도 않다.")
            sim._elapsed_minutes = 500 + 320
            self.assertEqual(self._symptom(sim, "a", 9), "온몸이 불덩이다.")

    # ── 3. 회복: recover_at_minutes 도달 시 회복 / max<=0이면 영구 감염 ──────────

    def test_recovers_only_after_sampled_minutes_elapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(
                tmp, time_per_wave=60,
                infection=self._model(recovery_min_minutes=180,
                                      recovery_max_minutes=180),  # 결정론적 3시간
            )
            sim._set_infected("a", 0, "event")
            self.assertEqual(sim._agent_infection["a"]["recover_at_minutes"], 180)

            sim._apply_infection_wave(1)   #  60분 — 아직
            self.assertEqual(sim._agent_infection["a"]["status"], "I")
            sim._apply_infection_wave(2)   # 120분 — 아직
            self.assertEqual(sim._agent_infection["a"]["status"], "I")
            sim._apply_infection_wave(3)   # 180분 — 도달
            self.assertEqual(sim._agent_infection["a"]["status"], "R")

    def test_recovery_time_is_sampled_within_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(20):
                sim = self._sim(tmp, infection=self._model(recovery_min_minutes=100,
                                                           recovery_max_minutes=200))
                sim._set_infected("a", 0, "event")
                self.assertTrue(100 <= sim._agent_infection["a"]["recover_at_minutes"] <= 200)

    def test_zero_recovery_max_means_never_recovers(self):
        # 구 recovery_probability=0(만성)에 대응하는 계약.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=60,
                            infection=self._model(recovery_min_minutes=0,
                                                  recovery_max_minutes=0))
            sim._set_infected("a", 0, "event")
            self.assertIsNone(sim._agent_infection["a"]["recover_at_minutes"])

            for wave in range(1, 60):
                sim._apply_infection_wave(wave)
            self.assertEqual(sim._agent_infection["a"]["status"], "I")

    # ── 4. immune_after_recovery 분기 유지 ──────────────────────────────────────

    def test_immune_after_recovery_branches(self):
        for immune, expected in ((True, "R"), (False, "S")):
            with self.subTest(immune=immune):
                with tempfile.TemporaryDirectory() as tmp:
                    sim = self._sim(
                        tmp, time_per_wave=60,
                        infection=self._model(recovery_min_minutes=60,
                                              recovery_max_minutes=60,
                                              immune_after_recovery=immune),
                    )
                    sim._set_infected("a", 0, "event")
                    sim._apply_infection_wave(1)
                    self.assertEqual(sim._agent_infection["a"]["status"], expected)
                    # 회복 안내는 상태와 무관하게 한 번 뜬다(raw 값 노출 없이).
                    self.assertIn("씻은 듯이", sim._build_symptom_context("a", 1))

    def test_sis_agent_can_be_reinfected_and_restarts_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(
                tmp, time_per_wave=60,
                infection=self._model(recovery_min_minutes=60, recovery_max_minutes=60,
                                      immune_after_recovery=False),
            )
            sim._set_infected("a", 0, "event")
            sim._apply_infection_wave(1)
            self.assertEqual(sim._agent_infection["a"]["status"], "S")

            # 재감염 — 앵커가 재감염 시점으로 옮겨져 다시 잠복기부터 시작해야 한다.
            self.assertTrue(sim._set_infected("a", 10, "transmission"))
            self.assertEqual(sim._agent_infection["a"]["infected_at_minutes"], 600)
            self.assertEqual(self._symptom(sim, "a", 10), "아직 아무렇지도 않다.")

    # ── 5. /continue·/resume 직렬화·복원: 경과 분이 이어진다 ────────────────────

    def test_continue_rebases_minute_anchor(self):
        # /continue는 같은 sim_obj를 재사용하며 completed_waves를 0으로 되돌린다.
        # rebase 없이 되돌리면 fixed 모드의 '지금'이 0분이 되어 급성기 환자가
        # 잠복기로 되감긴다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, infection=self._model(), time_per_wave=60)
            sim._set_infected("a", 0, "event")
            sim.completed_waves = 5                       # 경과 300분 = 급성기
            self.assertEqual(self._symptom(sim, "a", 5), "온몸이 불덩이다.")

            sim.rebase_infection_anchors()
            sim.completed_waves = 0
            self.assertEqual(self._symptom(sim, "a", 0), "온몸이 불덩이다.")
            self.assertEqual(sim._agent_infection["a"]["infected_at_minutes"], -300)

    def test_continue_rebase_is_noop_in_variable_mode(self):
        # variable 모드는 _elapsed_minutes가 그대로 누적된 채 이어지므로 '지금'이
        # 되감기지 않는다 — 앵커를 건드리면 오히려 경과가 두 번 빠진다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, infection=self._model(), time_mode="variable",
                            time_per_wave=0)
            sim._set_infected("a", 0, "event")
            sim._elapsed_minutes  = 400
            sim.completed_waves   = 5

            sim.rebase_infection_anchors()
            sim.completed_waves = 0
            self.assertEqual(sim._agent_infection["a"]["infected_at_minutes"], 0)
            self.assertEqual(self._symptom(sim, "a", 0), "온몸이 불덩이다.")

    def test_export_restore_preserves_elapsed_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=60,
                            infection=self._model(recovery_min_minutes=900,
                                                  recovery_max_minutes=900))
            sim._set_infected("a", 0, "event")
            sim.completed_waves = 5                        # 300분 경과

            state = sim.export_agent_state()
            saved = state["a"]["infection"]
            self.assertEqual(saved["elapsed_minutes_since_infection"], 300)
            self.assertEqual(saved["recover_at_minutes"], 900)  # 델타라 그대로 저장

            fresh = self._sim(tmp, time_per_wave=60,
                              infection=self._model(recovery_min_minutes=900,
                                                    recovery_max_minutes=900))
            fresh.restore_agent_state(state)

            self.assertEqual(fresh._agent_infection["a"]["status"], "I")
            self.assertEqual(fresh._agent_infection["a"]["infected_at_minutes"], -300)
            self.assertEqual(fresh._agent_infection["a"]["recover_at_minutes"], 900)
            self.assertEqual(self._symptom(fresh, "a", 0), "온몸이 불덩이다.")
            # 남은 600분(=10 wave)이 지나야 회복한다 — 복원으로 시계가 리셋되지 않는다.
            fresh._apply_infection_wave(9)
            self.assertEqual(fresh._agent_infection["a"]["status"], "I")
            fresh._apply_infection_wave(10)
            self.assertEqual(fresh._agent_infection["a"]["status"], "R")

    def test_restore_preserves_elapsed_minutes_in_variable_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, infection=self._model(), time_mode="variable",
                            time_per_wave=0)
            sim._set_infected("a", 0, "event")
            sim._elapsed_minutes = 350
            state = sim.export_agent_state()
            self.assertEqual(state["a"]["infection"]["elapsed_minutes_since_infection"], 350)

            # /resume은 누적 경과를 elapsed_minutes_init으로 되살린 새 Simulation을 만든다.
            fresh = self._sim(tmp, infection=self._model(), time_mode="variable",
                              time_per_wave=0, elapsed_init=350)
            fresh.restore_agent_state(state)
            self.assertEqual(fresh._agent_infection["a"]["infected_at_minutes"], 0)
            self.assertEqual(self._symptom(fresh, "a", 0), "온몸이 불덩이다.")

    # ── 6. 시간 개념 꺼짐 — 첫 단계 고정(회귀 아님) ─────────────────────────────

    def test_time_disabled_pins_first_stage_and_blocks_recovery(self):
        # fixed + time_per_wave=0 = 시간이 흐르지 않는 세계. 경과분이 항상 0이라
        # 모든 감염자가 첫 단계에 머물고 자연 회복도 없다. 이는 시간 기준 모델의
        # 정의상 결과이지 회귀가 아니다(프론트가 경고 배지로 안내한다).
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=0,
                            infection=self._model(recovery_min_minutes=1,
                                                  recovery_max_minutes=1))
            sim._set_infected("a", 0, "event")

            self.assertEqual(sim._current_elapsed_minutes(50), 0)
            self.assertEqual(self._symptom(sim, "a", 50), "아직 아무렇지도 않다.")
            for wave in range(1, 20):
                sim._apply_infection_wave(wave)
            self.assertEqual(sim._agent_infection["a"]["status"], "I")

    def test_transmission_stays_wave_based_even_without_time(self):
        # 전염은 접촉 사건이라 시간 축과 무관하게 wave 기준으로 계속 동작해야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=0,
                            infection=self._model(transmission_probability=1.0),
                            locations={"a": "매장", "b": "매장"})
            sim._set_infected("a", 0, "event")

            sim._apply_infection_wave(0)
            self.assertEqual(sim._agent_infection["b"]["status"], "I")

    # ── LLM 계약: raw 값은 절대 프롬프트에 안 들어간다 ───────────────────────────

    def test_prompt_never_leaks_raw_infection_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=60,
                            infection=self._model(recovery_min_minutes=900,
                                                  recovery_max_minutes=900))
            sim._set_infected("a", 0, "event")
            ctx = sim._assemble_agent_prompt("a", 5)
            blob = json.dumps(ctx, ensure_ascii=False, default=str)

            for leak in ("infected_at_minutes", "recover_at_minutes",
                         "transmission_probability", "recovery_min_minutes",
                         "elapsed_minutes"):
                self.assertNotIn(leak, blob, f"raw 값 누출: {leak}")
            self.assertIn("온몸이 불덩이다.", blob)

    # ── 스키마 계약 (backend/api/simulation/schemas.py) ──────────────────────────

    def test_schema_defaults_and_validation(self):
        from backend.api.simulation.schemas import InfectionModelConfig, SymptomStage

        cfg = InfectionModelConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.transmission_probability, 0.3)
        self.assertEqual(cfg.recovery_min_minutes, 7200)
        self.assertEqual(cfg.recovery_max_minutes, 14400)
        self.assertTrue(cfg.immune_after_recovery)
        self.assertEqual(cfg.symptom_stages, [])
        # 폐기된 필드는 스키마에서 사라졌다(구 설정이 와도 조용히 무시된다).
        self.assertNotIn("recovery_probability", cfg.model_dump())
        self.assertNotIn(
            "recovery_probability",
            InfectionModelConfig(recovery_probability=0.5).model_dump(),
        )

        stage = SymptomStage(id="s", label="l", min_minutes=0, max_minutes=2880,
                             symptom_text="t")
        self.assertEqual(stage.max_minutes, 2880)
        with self.assertRaises(ValidationError):
            SymptomStage(id="s", label="l", min_minutes=100, max_minutes=50,
                         symptom_text="t")
        with self.assertRaises(ValidationError):
            SymptomStage(id="s", label="l", min_minutes=-1, max_minutes=50,
                         symptom_text="t")

        with self.assertRaises(ValidationError):
            InfectionModelConfig(recovery_min_minutes=1000, recovery_max_minutes=500)
        # max=0은 "자연 회복 없음(만성)"이라는 별도 의미라서 허용된다.
        self.assertEqual(
            InfectionModelConfig(recovery_min_minutes=1000,
                                 recovery_max_minutes=0).recovery_max_minutes,
            0,
        )

    def test_engine_clamps_out_of_order_ranges(self):
        # 스키마를 거치지 않는 경로(저장된 시나리오 JSON 등)로 뒤집힌 값이 들어와도
        # 엔진이 방어적으로 min을 max로 낮춘다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, infection=self._model(
                recovery_min_minutes=500, recovery_max_minutes=200,
                symptom_stages=[{"id": "x", "label": "x", "min_minutes": 300,
                                 "max_minutes": 100, "symptom_text": "어지럽다."}],
            ))
            self.assertEqual(sim._infection_recovery_min, 200)
            self.assertEqual(sim._infection_stages[0]["min_minutes"], 100)

            sim._set_infected("a", 0, "event")
            self.assertTrue(200 >= sim._agent_infection["a"]["recover_at_minutes"] >= 200)


class FixedClockContinuityTests(unittest.TestCase):
    """fixed 시간 모드의 시계가 run 경계를 넘어 연속되는지 (m4).

    증상이었던 버그: `_current_elapsed_minutes`의 fixed 분기가 `wave*time_per_wave`
    만 계산해서, `/continue`·`/resume`이 wave를 0으로 되돌리면 에이전트가 보는
    `[현재 시각]`이 시나리오 시작 시각으로 되감겼다("1차 run 종료 시 월 14:00 →
    재개 첫 wave 월 09:00"). 감염 경과만 rebase/restore 보정으로 이어지고 시계는
    되감기는 비대칭이 남았다.

    불변식: `_elapsed_minutes`는 **두 모드 모두** '이전 run들의 누적 경과'를 담고,
    `_current_elapsed_minutes(w)`는 '누적 + 이번 run의 wave 경과'를 돌려준다.
    """

    STAGES = InfectionTimeModelTests.STAGES

    def _sim(self, tmp, *, time_mode="fixed", time_per_wave=30, elapsed_init=0,
             infection=None, start_time="09:00"):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in ("a", "b")}
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM({k: [{"content": "...", "target": "self"}] for k in ("a", "b")}),
            time_per_wave=time_per_wave, time_mode=time_mode,
            elapsed_minutes_init=elapsed_init,
            sim_start_time=start_time,
            infection_model=infection,
        )
        sim._emit = lambda t, d: None
        return sim

    @staticmethod
    def _infection_model():
        return {
            "enabled":                  True,
            "disease_name":             "테스트열",
            "transmission_probability": 0.0,
            "symptom_stages":           [dict(s) for s in InfectionTimeModelTests.STAGES],
            "recovery_min_minutes":     0,
            "recovery_max_minutes":     0,
            "immune_after_recovery":    True,
        }

    @staticmethod
    def _clock(sim, wave):
        """에이전트 프롬프트에 실제로 주입되는 '[현재 시각: ...]' 문자열."""
        return sim._format_time_str(
            sim._sim_start_minutes + sim._current_elapsed_minutes(wave)
        )

    # ── 1. /continue 후 프롬프트 시계가 연속된다 ─────────────────────────────────

    def test_fixed_continue_keeps_clock_moving_forward(self):
        from backend.api.simulation.runner import fold_elapsed_and_reset_waves

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=30)
            sim.completed_waves = 2                       # 2 wave × 30분 = 60분
            self.assertEqual(sim._current_elapsed_minutes(), 60)

            fold_elapsed_and_reset_waves(sim)             # = /continue 준비 단계

            self.assertEqual(sim.completed_waves, 0)
            # 되감기지 않는다: 재개 첫 wave가 곧 60분 지점.
            self.assertEqual(sim._current_elapsed_minutes(0), 60)
            self.assertEqual(sim._current_elapsed_minutes(1), 90)
            self.assertEqual(sim._current_elapsed_minutes(2), 120)

    def test_fixed_continue_clock_string_does_not_rewind(self):
        # 회귀 전 증상 그대로: 09:00 시작 + 10 wave × 30분 = 14:00 → 재개 첫 wave가
        # 09:00으로 돌아가면 안 된다.
        from backend.api.simulation.runner import fold_elapsed_and_reset_waves

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=30, start_time="09:00")
            sim.completed_waves = 10
            end_of_run = self._clock(sim, 10)
            self.assertIn("오후 2시", end_of_run)

            fold_elapsed_and_reset_waves(sim)

            self.assertEqual(self._clock(sim, 0), end_of_run)
            self.assertNotIn("오전 9시", self._clock(sim, 0))
            self.assertIn("오후 2시 30분", self._clock(sim, 1))

    def test_repeated_continues_keep_accumulating(self):
        # 여러 번 이어서 실행해도 누적이 계속 쌓인다(한 번만 접히는 게 아니다).
        from backend.api.simulation.runner import fold_elapsed_and_reset_waves

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=30)
            for _ in range(3):
                sim.completed_waves = 4                   # 매 run 120분
                fold_elapsed_and_reset_waves(sim)
            self.assertEqual(sim._elapsed_minutes, 360)
            self.assertEqual(sim._current_elapsed_minutes(0), 360)

    # ── 2. /resume·/load 직렬화 왕복 후에도 연속 ─────────────────────────────────

    def test_fixed_resume_roundtrip_restores_total_elapsed(self):
        # finalize_run이 DB에 넣는 값 = 총 경과. /resume·/load는 그 값을
        # elapsed_minutes_init으로 새 Simulation을 만든다.
        from backend.api.simulation.runner import _total_elapsed_minutes

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=30)
            sim.completed_waves = 7                       # 210분

            persisted = _total_elapsed_minutes(sim)
            self.assertEqual(persisted, 210)              # raw _elapsed_minutes(0)이 아니다

            fresh = self._sim(tmp, time_per_wave=30, elapsed_init=persisted)
            self.assertEqual(fresh._current_elapsed_minutes(0), 210)
            self.assertEqual(fresh._current_elapsed_minutes(3), 300)
            self.assertEqual(self._clock(fresh, 0), self._clock(sim, 7))

    def test_persisted_elapsed_survives_continue_then_resume(self):
        # /continue로 이어 돌린 뒤 저장해도 총계가 두 run을 모두 포함한다.
        from backend.api.simulation.runner import (
            _total_elapsed_minutes, fold_elapsed_and_reset_waves,
        )

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=30)
            sim.completed_waves = 4                       # 1차 run 120분
            fold_elapsed_and_reset_waves(sim)
            sim.completed_waves = 6                       # 2차 run 180분

            self.assertEqual(_total_elapsed_minutes(sim), 300)

    def test_total_elapsed_falls_back_for_legacy_sim_objects(self):
        # 헬퍼가 없는 목/구버전 객체에서도 finalize_run이 터지지 않는다.
        from backend.api.simulation.runner import _total_elapsed_minutes

        class _Legacy:
            _elapsed_minutes = 42

        self.assertEqual(_total_elapsed_minutes(_Legacy()), 42)
        self.assertIsNone(_total_elapsed_minutes(object()))

    # ── 3. 감염 경과 연속 + rebase가 no-op이 됨 ─────────────────────────────────

    def test_fixed_continue_keeps_infection_elapsed_and_rebase_is_noop(self):
        from backend.api.simulation.runner import fold_elapsed_and_reset_waves

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=60, infection=self._infection_model())
            sim._set_infected("a", 0, "event")
            sim.completed_waves = 5                       # 300분 = 급성기
            anchor_before = sim._agent_infection["a"]["infected_at_minutes"]

            fold_elapsed_and_reset_waves(sim)

            # 경과를 먼저 접었으므로 rebase는 앵커를 건드리지 않는다(no-op).
            self.assertEqual(sim._agent_infection["a"]["infected_at_minutes"],
                             anchor_before)
            # 그래도 증상 단계는 되감기지 않는다 — '지금'이 연속이기 때문.
            self.assertEqual(
                InfectionTimeModelTests._symptom(sim, "a", 0), "온몸이 불덩이다.",
            )
            self.assertEqual(sim._current_elapsed_minutes(0), 300)

    def test_rebase_defends_when_the_fold_is_skipped(self):
        # 접기를 빠뜨린 재개 경로가 생기면 rebase가 앵커를 과거로 옮겨 증상 단계
        # 되감김을 흡수한다 — no-op이 '아무 일도 안 하는 죽은 코드'가 아니라는 근거.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=60, infection=self._infection_model())
            sim._set_infected("a", 0, "event")
            sim.completed_waves = 5

            before = sim._current_elapsed_minutes(sim.completed_waves)
            sim.completed_waves = 0                       # 접기 없이 리셋(회귀 시뮬)
            sim.rebase_infection_anchors(now=before)

            self.assertEqual(sim._agent_infection["a"]["infected_at_minutes"], -300)
            self.assertEqual(
                InfectionTimeModelTests._symptom(sim, "a", 0), "온몸이 불덩이다.",
            )

    def test_fixed_resume_keeps_infection_elapsed(self):
        from backend.api.simulation.runner import _total_elapsed_minutes

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=60, infection=self._infection_model())
            sim._set_infected("a", 0, "event")
            sim.completed_waves = 5                       # 300분 = 급성기

            state     = sim.export_agent_state()
            persisted = _total_elapsed_minutes(sim)
            self.assertEqual(state["a"]["infection"]["elapsed_minutes_since_infection"], 300)

            fresh = self._sim(tmp, time_per_wave=60, infection=self._infection_model(),
                              elapsed_init=persisted)
            fresh.restore_agent_state(state)

            # 앵커가 '음수 시간'이 아니라 실제 감염 시점(0분)으로 복원된다.
            self.assertEqual(fresh._agent_infection["a"]["infected_at_minutes"], 0)
            self.assertEqual(
                InfectionTimeModelTests._symptom(fresh, "a", 0), "온몸이 불덩이다.",
            )
            # 시계와 병의 진행이 같은 원점을 쓴다.
            self.assertEqual(self._clock(fresh, 0), self._clock(sim, 5))

    # ── 4. 목표 기간: 기존 동작 유지 + 재개 시 '이번 run 이후' 기준 ────────────────

    def test_target_duration_is_per_run_in_fixed_mode(self):
        # 이전 run에서 이미 600분이 지났어도, 이번 run은 목표 60분만큼 더 돈다.
        sim, end = TargetDurationTests()._run(
            time_per_wave=30, target=60, max_waves=20, elapsed_init=600,
        )
        self.assertEqual(end["end_reason"], "target_duration")
        self.assertEqual(sim.completed_waves, 2)          # elapsed_init 없을 때와 동일
        self.assertEqual(sim._current_elapsed_minutes(), 660)

    def test_target_duration_fixed_baseline_unchanged(self):
        # elapsed_init=0인 기존 경로의 결과는 그대로.
        sim, end = TargetDurationTests()._run(time_per_wave=30, target=60, max_waves=20)
        self.assertEqual(sim.completed_waves, 2)
        self.assertEqual(end["end_reason"], "target_duration")

    # ── 5. variable / 시간 개념 꺼짐 모드는 무변경 ────────────────────────────────

    def test_variable_mode_is_unaffected_by_the_fold(self):
        from backend.api.simulation.runner import (
            _total_elapsed_minutes, fold_elapsed_and_reset_waves,
        )

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_mode="variable", time_per_wave=0)
            sim._elapsed_minutes = 400
            sim.completed_waves  = 5

            self.assertEqual(_total_elapsed_minutes(sim), 400)   # wave와 무관
            fold_elapsed_and_reset_waves(sim)
            self.assertEqual(sim._elapsed_minutes, 400)          # 접기는 no-op
            self.assertEqual(sim._current_elapsed_minutes(9), 400)

    def test_variable_mode_target_duration_unchanged(self):
        sim, end = TargetDurationTests()._run(
            time_mode="variable", target=20, max_waves=20,
            category="meal_or_brief", elapsed_init=500,
        )
        self.assertEqual(end["end_reason"], "target_duration")
        self.assertGreaterEqual(sim._elapsed_minutes - 500, 20)

    def test_time_disabled_mode_stays_frozen_at_zero(self):
        from backend.api.simulation.runner import (
            _total_elapsed_minutes, fold_elapsed_and_reset_waves,
        )

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=0)         # fixed + tpw=0 = 시간 없음
            sim.completed_waves = 50

            self.assertEqual(sim._current_elapsed_minutes(50), 0)
            self.assertEqual(_total_elapsed_minutes(sim), 0)
            fold_elapsed_and_reset_waves(sim)
            self.assertEqual(sim._elapsed_minutes, 0)
            self.assertEqual(sim._current_elapsed_minutes(50), 0)

    def test_time_disabled_mode_still_ignores_target_duration(self):
        sim, end = TargetDurationTests()._run(time_per_wave=0, target=10, max_waves=3)
        self.assertEqual(sim.completed_waves, 3)
        self.assertEqual(end["end_reason"], "max_waves")


class CumulativeWaveTests(unittest.TestCase):
    """`/continue`·`/resume` 후 wave 번호가 이어지도록 하는 wave 카운터 2개 분리.

    - `run()` 루프 = per-run 0-based `run_wave` → 시간/감염/목표기간 계산 전용(무변경).
    - `self._wave_base` = 이 run 이전까지 누적 wave(fresh /start는 0).
    - emit·영속화 라벨 = `disp_wave = _wave_base + run_wave`.
    - `self.completed_waves`는 per-run 유지, `cumulative_waves`가 누적 리포팅용.
    """

    def _sim(self, tmp, *, wave_base_init=0, time_per_wave=30, elapsed_init=0,
             infection=None, keys=("a", "b"), locations=None):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in keys}
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM({k: [{"content": "...", "target": "self"}] for k in keys}),
            time_per_wave=time_per_wave, time_mode="fixed",
            elapsed_minutes_init=elapsed_init,
            wave_base_init=wave_base_init,
            agent_locations=locations,
            infection_model=infection,
        )
        sim._emitted = []
        sim._emit = lambda t, d: sim._emitted.append((t, d))
        return sim

    @staticmethod
    def _infection_model(**over):
        model = {
            "enabled":                  True,
            "disease_name":             "테스트열",
            "transmission_probability": 1.0,
            "symptom_stages":           [],
            "recovery_min_minutes":     0,
            "recovery_max_minutes":     0,
            "immune_after_recovery":    True,
        }
        model.update(over)
        return model

    # ── 1. run()이 emit하는 wave가 누적값 ──────────────────────────────────────

    def test_run_emits_cumulative_wave_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, wave_base_init=10)
            sim.run("a", max_waves=3, step_delay=0.0, early_stop_enabled=False)

            wave_starts = [d["wave"] for t, d in sim._emitted if t == "wave_start"]
            self.assertEqual(wave_starts, [10, 11, 12])
            # per-run 카운터는 그대로 3.
            self.assertEqual(sim.completed_waves, 3)
            self.assertEqual(sim.cumulative_waves, 13)

    def test_fresh_start_is_unchanged_by_the_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, wave_base_init=0)
            sim.run("a", max_waves=3, step_delay=0.0, early_stop_enabled=False)

            wave_starts = [d["wave"] for t, d in sim._emitted if t == "wave_start"]
            self.assertEqual(wave_starts, [0, 1, 2])
            self.assertEqual(sim._wave_base, 0)
            self.assertEqual(sim.cumulative_waves, 3)

    def test_turn_complete_and_log_use_cumulative_wave(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, wave_base_init=7)
            sim.run("a", max_waves=2, step_delay=0.0, early_stop_enabled=False)

            tc_waves = {d["wave"] for t, d in sim._emitted if t == "turn_complete"}
            self.assertTrue(tc_waves <= {7, 8})
            self.assertIn(7, tc_waves)
            # shared_log 엔트리도 누적 wave로 저장된다(요약 구간 계산이 이 값에 의존).
            log_waves = {e["wave"] for e in sim.shared_log if isinstance(e.get("wave"), int)}
            self.assertTrue(log_waves <= {7, 8})

    # ── 2. 시각/경과 계산은 _wave_base와 무관 ─────────────────────────────────

    def test_current_elapsed_minutes_is_independent_of_wave_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            base0 = self._sim(tmp, wave_base_init=0, time_per_wave=30)
            base5 = self._sim(tmp, wave_base_init=5, time_per_wave=30)
            for w in (0, 1, 3, 10):
                self.assertEqual(
                    base0._current_elapsed_minutes(w),
                    base5._current_elapsed_minutes(w),
                    f"wave_base가 시각 계산에 샜다 (wave={w})",
                )
            base5.completed_waves = 4
            self.assertEqual(base5._current_elapsed_minutes(), 120)   # 4 * 30, base 무관

    def test_target_duration_baseline_ignores_wave_base(self):
        # 이전 run에서 누적 wave가 600이어도 이번 run은 목표 60분만큼만 더 돈다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, wave_base_init=600, time_per_wave=30)
            emitted = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim.run("a", max_waves=20, step_delay=0.0, early_stop_enabled=False,
                    target_duration_minutes=60)
            end = [d for t, d in emitted if t == "simulation_end"][-1]
            self.assertEqual(end["end_reason"], "target_duration")
            self.assertEqual(sim.completed_waves, 2)          # (1+1)*30 = 60
            wave_starts = [d["wave"] for t, d in emitted if t == "wave_start"]
            self.assertEqual(wave_starts, [600, 601])

    # ── 3. 감염 이벤트 라벨은 disp_wave, 경과 계산은 run_wave ────────────────────

    def test_infection_update_event_uses_disp_wave(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=60,
                            infection=self._infection_model(),
                            locations={"a": "매장", "b": "매장"})
            sim._set_infected("a", 0, "event")
            sim._emitted.clear()

            # run_wave=2 (경과 120분), disp_wave=42 (표시 라벨)
            sim._apply_infection_wave(2, 42)

            updates = [d for t, d in sim._emitted if t == "infection_update"]
            b_update = next(d for d in updates if d["agent"] == "b")
            self.assertEqual(b_update["wave"], 42)                    # 라벨 = disp_wave
            self.assertEqual(b_update["elapsed_minutes"], 120)       # 시간 = run_wave 기준
            self.assertEqual(sim._agent_infection["b"]["infected_at_minutes"], 120)

    def test_apply_infection_wave_disp_defaults_to_run_wave(self):
        # disp_wave 생략 시 run_wave를 라벨로 재사용(단위 테스트 하위 호환).
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, time_per_wave=60,
                            infection=self._infection_model(),
                            locations={"a": "매장", "b": "매장"})
            sim._set_infected("a", 0, "event")
            sim._emitted.clear()
            sim._apply_infection_wave(3)
            b_update = next(d for t, d in sim._emitted
                            if t == "infection_update" and d["agent"] == "b")
            self.assertEqual(b_update["wave"], 3)

    # ── 4. _last_summarized_wave 초기값 = wave_base - 1 ────────────────────────

    def test_last_summarized_wave_starts_at_base_minus_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._sim(tmp, wave_base_init=0)._last_summarized_wave, -1)
            self.assertEqual(self._sim(tmp, wave_base_init=13)._last_summarized_wave, 12)

    # ── 5. DB: start_wave 컬럼 라운드트립 ─────────────────────────────────────

    def test_create_run_persists_start_wave(self):
        from ABM.db import SimDB

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = SimDB(os.path.join(tmp, "sim.db"))
            try:
                db.create_run("r1", "scn", "시나리오", "{}", start_wave=7)
                db.create_run("r2", "scn", "시나리오", "{}")   # 기본값 0

                self.assertEqual(db.get_run("r1")["start_wave"], 7)
                self.assertEqual(db.get_run("r2")["start_wave"], 0)
                runs = {r["run_id"]: r for r in db.get_runs("scn")}
                self.assertEqual(runs["r1"]["start_wave"], 7)
            finally:
                conn = getattr(db._local, "conn", None)
                if conn is not None:
                    conn.close()


class ResumeContinueWaveBaseTests(unittest.TestCase):
    """PART 2 — 백엔드 글루: /resume·/continue 가 누적 wave base 를 엔진에 전달.

    - `/resume`: `create_run(start_wave=이전 start_wave + total_waves)` +
      `Simulation(wave_base_init=...)` + 응답에 `start_wave`.
    - `fold_elapsed_and_reset_waves`: `completed_waves` 리셋 직전 `_wave_base` 누적.
    - 3-1 버그: `/resume` 이 조기종료 설정(`early_stop_enabled`,
      `max_silence_waves`)을 `run()` 까지 전달.
    """

    def tearDown(self):
        s = sim_runtime._sim
        s["status"]      = "idle"
        s["thread"]      = None
        s["sim_obj"]     = None
        s["event_queue"] = None
        s["stop_event"]  = None

    # ── fold_elapsed_and_reset_waves ─────────────────────────────────────────

    def test_fold_accumulates_wave_base_and_resets_completed(self):
        from backend.api.simulation.runner import fold_elapsed_and_reset_waves

        class _Sim:
            def __init__(self):
                self.completed_waves  = 4
                self._wave_base       = 10
                self._elapsed_minutes = 0
                self.rebased_now      = "unset"
            def _current_elapsed_minutes(self, w=0):
                return self._elapsed_minutes
            def rebase_infection_anchors(self, now=None):
                self.rebased_now = now

        sim = _Sim()
        fold_elapsed_and_reset_waves(sim)
        self.assertEqual(sim._wave_base, 14)          # 10 + 4
        self.assertEqual(sim.completed_waves, 0)

        # 여러 번 이어서 실행해도 계속 누적된다.
        sim.completed_waves = 6
        fold_elapsed_and_reset_waves(sim)
        self.assertEqual(sim._wave_base, 20)          # 14 + 6
        self.assertEqual(sim.completed_waves, 0)

    def test_fold_defaults_wave_base_to_zero_when_absent(self):
        from backend.api.simulation.runner import fold_elapsed_and_reset_waves

        class _Sim:
            completed_waves  = 3
            _elapsed_minutes = 0
            def _current_elapsed_minutes(self, w=0):
                return 0
            def rebase_infection_anchors(self, now=None):
                pass

        sim = _Sim()
        fold_elapsed_and_reset_waves(sim)
        self.assertEqual(sim._wave_base, 3)
        self.assertEqual(sim.completed_waves, 0)

    # ── /resume 엔드포인트 (의존성 stub) ──────────────────────────────────────

    def _run_resume(self, run_row, *, snapshots=None, states=None):
        """stub 의존성으로 resume_simulation 을 돌리고 (응답, 기록된 호출)을 반환."""
        from unittest import mock
        import ABM.agent as abm_agent
        import ABM.simulation as abm_simulation
        import ABM.db as abm_db
        import ABM.memory_compressor as abm_mc
        from backend.api.simulation.runtime import resume as resume_mod

        calls = {"create_run": None, "run": None, "sim_kwargs": None}

        class FakeAgent:
            def __init__(self, *a, **k):
                self.memory = []
                self._memory_block = None

        class FakeSim:
            def __init__(self, *a, **k):
                calls["sim_kwargs"] = k
                self.agents         = {}
                self.background_log = []
                self.shared_log     = []
                self.edges          = []
                self.completed_waves = 0
                self._pending_wave  = None
                self.active_agents  = set()
            def restore_agent_state(self, s):        pass
            def export_agent_state(self):            return {}
            def _current_elapsed_minutes(self, w=0): return 0
            def run(self, *a, **k):
                calls["run"] = {"args": a, "kwargs": k}

        class FakeDB:
            def create_run(self, *a, **k):
                calls["create_run"] = {"args": a, "kwargs": k}
            def finish_run(self, *a, **k):           pass
            def save_agent_snapshots(self, *a, **k): pass
            def get_run(self, rid):                  return run_row
            def get_agent_snapshots(self, rid):      return snapshots or {}
            def get_agent_states(self, rid):         return states or {}

        fake_db = FakeDB()
        sim_runtime._sim["status"]      = "idle"
        sim_runtime._sim["event_queue"] = None

        with mock.patch.object(resume_mod, "get_sim_db", lambda: fake_db), \
             mock.patch.object(resume_mod, "_make_llm", lambda *a, **k: None), \
             mock.patch.object(resume_mod, "_make_agent_llm_map", lambda *a, **k: {}), \
             mock.patch.object(abm_agent, "Agent", FakeAgent), \
             mock.patch.object(abm_simulation, "Simulation", FakeSim), \
             mock.patch.object(abm_db, "SimDB", lambda *a, **k: fake_db), \
             mock.patch.object(abm_mc, "build_memory_block", lambda *a, **k: None):
            resp = resume_mod.resume_simulation("prev-run")
            t = sim_runtime._sim.get("thread")
            if t is not None:
                t.join(timeout=5)
                self.assertFalse(t.is_alive(), "resume thread hung")
        return resp, calls

    @staticmethod
    def _run_row(cfg: SimStartConfig, *, start_wave=0, total_waves=0):
        return {
            "config_json":        cfg.model_dump_json(),
            "start_wave":         start_wave,
            "total_waves":        total_waves,
            "scenario_id":        "scn",
            "scenario_name":      "시나리오",
            "active_agents_json": None,
            "pending_wave_json":  None,
            "elapsed_minutes":    0,
        }

    def _cfg(self, **over):
        base = dict(agents=[AgentConfig(name="a", system_prompt="너는 a다.")],
                    background="테스트", start_agent="a")
        base.update(over)
        return SimStartConfig(**base)

    def test_resume_passes_cumulative_start_wave_to_create_run(self):
        resp, calls = self._run_resume(
            self._run_row(self._cfg(), start_wave=5, total_waves=8))
        self.assertEqual(calls["create_run"]["kwargs"].get("start_wave"), 13)
        self.assertEqual(calls["sim_kwargs"].get("wave_base_init"), 13)
        self.assertEqual(resp.get("start_wave"), 13)

    def test_resume_start_wave_defaults_zero_for_legacy_rows(self):
        row = self._run_row(self._cfg(), start_wave=0, total_waves=0)
        row["start_wave"]  = None          # 구버전 row: 컬럼 없음/NULL
        row["total_waves"] = None
        resp, calls = self._run_resume(row)
        self.assertEqual(calls["create_run"]["kwargs"].get("start_wave"), 0)
        self.assertEqual(resp.get("start_wave"), 0)

    def test_resume_forwards_early_stop_settings_to_run(self):
        resp, calls = self._run_resume(
            self._run_row(self._cfg(early_stop_enabled=False, max_silence_waves=9)))
        self.assertIs(calls["run"]["kwargs"].get("early_stop_enabled"), False)
        self.assertEqual(calls["run"]["kwargs"].get("max_silence_waves"), 9)


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


    async def _run_chat_capturing_kwargs(self, msg, *, server_level=None, provider=None):
        """send_chat 을 끝까지 돌리고 async_stream_chat 이 받은 kwargs 를 돌려준다."""
        captured = {}

        async def ok_stream(messages, **kwargs):
            captured.update(kwargs)
            yield {"type": "answer", "chunk": "답"}
            yield {"type": "usage", "data": {"prompt_tokens": 3, "completion_tokens": 1,
                                             "answer": "답", "thinking": ""}}

        class LeveledProvider(FakeProvider):
            thinking_level = server_level

        if provider is not None:
            conversations.get_registry = lambda: FakeChatRegistry(provider)
        elif server_level is not None:
            conversations.get_registry = lambda: FakeChatRegistry(LeveledProvider())

        old_stream = conversations.async_stream_chat
        conversations.async_stream_chat = ok_stream
        try:
            response = await conversations.send_chat("conv-1", msg)
            chunks = [chunk async for chunk in response.body_iterator]
        finally:
            conversations.async_stream_chat = old_stream
        return captured, chunks

    async def test_chat_without_level_inherits_server_default(self):
        captured, chunks = await self._run_chat_capturing_kwargs(
            ChatMessage(content="hello"), server_level="high"
        )

        self.assertEqual(captured["thinking_level"], "high")
        self.assertTrue(any('"thinking_level": "high"' in c for c in chunks))

    async def test_chat_level_overrides_server_default(self):
        captured, _ = await self._run_chat_capturing_kwargs(
            ChatMessage(content="hello", thinking_level="low"), server_level="high"
        )

        self.assertEqual(captured["thinking_level"], "low")

    async def test_chat_can_turn_thinking_off_on_a_thinking_server(self):
        captured, chunks = await self._run_chat_capturing_kwargs(
            ChatMessage(content="hello", thinking_level="off"), server_level="medium"
        )

        self.assertEqual(captured["thinking_level"], "off")
        # off 면 max_tokens 상향(MAX_COMPLETION_TOKENS_THINKING)이 일어나지 않아야 한다.
        self.assertEqual(captured["max_tokens"], config.MAX_COMPLETION_TOKENS)
        self.assertTrue(any('"thinking_mode": false' in c for c in chunks))

    async def test_thinking_raises_max_tokens_ceiling(self):
        captured, _ = await self._run_chat_capturing_kwargs(
            ChatMessage(content="hello", thinking_level="low")
        )

        self.assertEqual(captured["max_tokens"], config.MAX_COMPLETION_TOKENS_THINKING)

    async def test_openai_reasoning_provider_raises_max_tokens_even_at_off(self):
        # QA m-5: 추론 모델은 off 여도 서버가 reasoning 을 하므로 4096 이면 빈 답이 온다.
        class ReasoningProvider(FakeProvider):
            thinking_level = "off"

            def needs_thinking_headroom(self, thinking_level=None):
                return True

        captured, _ = await self._run_chat_capturing_kwargs(
            ChatMessage(content="hello", thinking_level="off"),
            provider=ReasoningProvider(),
        )

        self.assertEqual(captured["thinking_level"], "off")
        self.assertEqual(captured["max_tokens"], config.MAX_COMPLETION_TOKENS_THINKING)

    async def test_classic_provider_at_high_level_still_uses_provider_verdict(self):
        # 일반 OpenAI 모델은 사고가 no-op 이므로 상향하지 않는다.
        class ClassicProvider(FakeProvider):
            thinking_level = "off"

            def needs_thinking_headroom(self, thinking_level=None):
                return False

        captured, _ = await self._run_chat_capturing_kwargs(
            ChatMessage(content="hello", thinking_level="high"),
            provider=ClassicProvider(),
        )

        self.assertEqual(captured["thinking_level"], "high")
        self.assertEqual(captured["max_tokens"], config.MAX_COMPLETION_TOKENS)

    async def test_provider_without_level_attribute_degrades_to_off(self):
        # 구 provider 객체/테스트 더블이 thinking_level 을 갖지 않아도 500 이 나면 안 된다.
        captured, _ = await self._run_chat_capturing_kwargs(ChatMessage(content="hello"))

        self.assertEqual(captured["thinking_level"], "off")

    async def test_web_search_emits_search_event_and_injects_context(self):
        captured = {}

        async def fake_build_query(user_msg, recent_turns):
            captured["recent_turns"] = recent_turns
            return f"q::{user_msg}"

        async def fake_web_search(query):
            captured["query"] = query
            return [
                SearchResult(title="제목1", url="https://example.com/1", snippet="스니펫1"),
                SearchResult(title="제목2", url="https://example.com/2", snippet="스니펫2"),
            ]

        async def ok_stream(messages, **kwargs):
            captured["messages"] = messages
            yield {"type": "answer", "chunk": "답"}
            yield {"type": "usage", "data": {"prompt_tokens": 3, "completion_tokens": 1,
                                             "answer": "답", "thinking": ""}}

        old_build = conversations.async_build_search_query
        old_search = conversations.web_search
        old_stream = conversations.async_stream_chat
        conversations.async_build_search_query = fake_build_query
        conversations.web_search = fake_web_search
        conversations.async_stream_chat = ok_stream
        try:
            response = await conversations.send_chat(
                "conv-1", ChatMessage(content="파이썬 뉴스", web_search=True),
            )
            chunks = [chunk async for chunk in response.body_iterator]
        finally:
            conversations.async_build_search_query = old_build
            conversations.web_search = old_search
            conversations.async_stream_chat = old_stream

        joined = "".join(chunks)
        self.assertIn("event: search", joined)
        self.assertIn("https://example.com/1", joined)
        self.assertEqual(captured["query"], "q::파이썬 뉴스")
        # 시스템 메시지에 검색 컨텍스트가 주입된다
        self.assertIn("제목1", captured["messages"][0]["content"])
        self.assertIn("스니펫2", captured["messages"][0]["content"])

        conn = sqlite3.connect(config.DB_PATH)
        row = conn.execute(
            "SELECT sources_json FROM turns WHERE conversation_id=? AND role='assistant'",
            ("conv-1",),
        ).fetchone()
        conn.close()
        self.assertIn("example.com/1", row[0])

    async def test_no_search_event_when_toggle_off(self):
        async def ok_stream(messages, **kwargs):
            yield {"type": "usage", "data": {"prompt_tokens": 1, "completion_tokens": 1,
                                             "answer": "", "thinking": ""}}

        old_stream = conversations.async_stream_chat
        conversations.async_stream_chat = ok_stream
        try:
            response = await conversations.send_chat(
                "conv-1", ChatMessage(content="hi", web_search=False),
            )
            chunks = [chunk async for chunk in response.body_iterator]
        finally:
            conversations.async_stream_chat = old_stream

        self.assertNotIn("event: search", "".join(chunks))


_DDG_SAMPLE_HTML = """
<html><body>
<div class="results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a"
           href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython&amp;rut=abc">
          Python (programming language)
        </a>
      </h2>
      <a class="result__snippet"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython">
        Python is a <b>high-level</b>, general-purpose programming language.
      </a>
    </div>
  </div>
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a"
           href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F&amp;rut=def">
          Welcome to Python.org
        </a>
      </h2>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F">
        The official home of the Python Programming Language.
      </a>
    </div>
  </div>
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a"
           href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython&amp;rut=ghi">
          Python (duplicate)
        </a>
      </h2>
      <a class="result__snippet">A duplicate URL that should be de-duped.</a>
    </div>
  </div>
</div>
</body></html>
"""


class _FakeDDGResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeDDGClient:
    def __init__(self, text):
        self._text = text
        self.seen = {}

    async def post(self, url, **kwargs):
        self.seen["url"] = url
        self.seen["data"] = kwargs.get("data")
        return _FakeDDGResponse(self._text)

    async def aclose(self):
        return None


class WebSearchContextTests(unittest.TestCase):
    def test_format_empty_returns_empty_string(self):
        self.assertEqual(format_search_context([]), "")

    def test_format_includes_number_title_and_url(self):
        text = format_search_context([
            SearchResult(title="티어리스트", url="https://example.com/a", snippet="요약"),
        ])
        self.assertIn("[1]", text)
        self.assertIn("티어리스트", text)
        self.assertIn("https://example.com/a", text)
        self.assertIn("요약", text)

    def test_build_messages_injects_web_context_into_system(self):
        msgs = build_messages("SYS", [], [{"role": "user", "content": "hi"}],
                              web_context="WEBCTX-MARKER")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("WEBCTX-MARKER", msgs[0]["content"])

    def test_build_messages_without_web_context_unchanged(self):
        msgs = build_messages("SYS", [], [{"role": "user", "content": "hi"}])
        self.assertEqual(msgs[0]["content"], "SYS")


class DuckDuckGoParserTests(unittest.IsolatedAsyncioTestCase):
    def test_unwrap_redirect_url(self):
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=x"
        self.assertEqual(_unwrap_ddg_url(wrapped), "https://example.com/page")

    def test_unwrap_passthrough_for_plain_url(self):
        self.assertEqual(_unwrap_ddg_url("https://plain.example/x"),
                         "https://plain.example/x")

    async def test_provider_parses_sample_html_into_results(self):
        provider = DuckDuckGoProvider()
        provider._client = _FakeDDGClient(_DDG_SAMPLE_HTML)

        results = await provider.search("python", k=5)

        self.assertEqual(len(results), 2)  # 3번째는 URL 중복으로 제거
        self.assertIsInstance(results[0], SearchResult)
        self.assertEqual(results[0].title, "Python (programming language)")
        self.assertEqual(results[0].url, "https://en.wikipedia.org/wiki/Python")
        self.assertIn("high-level", results[0].snippet)
        self.assertEqual(results[1].url, "https://www.python.org/")
        self.assertEqual(provider._client.seen["data"], {"q": "python", "kl": "wt-wt"})

    async def test_provider_respects_k_limit(self):
        provider = DuckDuckGoProvider()
        provider._client = _FakeDDGClient(_DDG_SAMPLE_HTML)

        results = await provider.search("python", k=1)

        self.assertEqual(len(results), 1)

    async def test_provider_truncates_snippet(self):
        long_snip = "x" * 5000
        html = _DDG_SAMPLE_HTML.replace(
            "Python is a <b>high-level</b>, general-purpose programming language.",
            long_snip,
        )
        provider = DuckDuckGoProvider()
        provider._client = _FakeDDGClient(html)

        results = await provider.search("python", k=5)

        self.assertLessEqual(len(results[0].snippet), config.WEB_SEARCH_MAX_SNIPPET_CHARS)


class WebSearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_search_returns_empty_list_on_provider_error(self):
        class BoomProvider:
            async def search(self, query, k):
                raise RuntimeError("429 blocked")

            async def close(self):
                pass

        old = websearch_service.get_provider
        websearch_service.get_provider = lambda: BoomProvider()
        try:
            result = await websearch_service.web_search("anything")
        finally:
            websearch_service.get_provider = old

        self.assertEqual(result, [])

    async def test_web_search_passes_through_results(self):
        class OkProvider:
            async def search(self, query, k):
                return [SearchResult(title="t", url="https://u", snippet="s")]

            async def close(self):
                pass

        old = websearch_service.get_provider
        websearch_service.get_provider = lambda: OkProvider()
        try:
            result = await websearch_service.web_search("q")
        finally:
            websearch_service.get_provider = old

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "t")


class CapturingStreamClient:
    """stream() 요청 바디를 캡처하는 클라이언트 (한 라운드만 응답)."""

    def __init__(self):
        self.json_body = None

    def stream(self, *args, **kwargs):
        self.json_body = kwargs.get("json")
        payload = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return FakeStreamResponse([f"data: {json.dumps(payload)}", "data: [DONE]"])

    async def aclose(self):
        return None


class ThinkingLevelTranslationTests(unittest.IsolatedAsyncioTestCase):
    """thinking_level(off/low/medium/high) → 프로바이더별 요청 바디 번역표."""

    async def _stream_body(self, provider, **kwargs) -> dict:
        fake = CapturingStreamClient()
        provider._client = fake
        async for _ in provider.stream_chat([{"role": "user", "content": "hi"}], **kwargs):
            pass
        return fake.json_body

    # ── vLLM ─────────────────────────────────────────────────────────
    async def test_vllm_off_omits_chat_template_kwargs(self):
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="off")
        body = await self._stream_body(p)
        self.assertNotIn("chat_template_kwargs", body)

    async def test_vllm_enables_thinking_for_every_non_off_level(self):
        for level in ("low", "medium", "high"):
            with self.subTest(level=level):
                p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level=level)
                body = await self._stream_body(p)
                self.assertEqual(
                    body["chat_template_kwargs"],
                    {"enable_thinking": True, "reasoning_effort": level},
                )

    # ── OpenAI ───────────────────────────────────────────────────────
    def test_openai_reasoning_models_send_reasoning_effort(self):
        # o1-mini / o1-preview 는 reasoning_effort 를 거부하므로 제외
        # (OpenAIReasoningEffortExclusionTests 참고).
        for model in ("gpt-5", "gpt-5.1-mini", "o1", "o3", "o3-mini", "o4-mini"):
            for level in ("low", "medium", "high"):
                with self.subTest(model=model, level=level):
                    p = OpenAIProvider("s", "t", "", model, thinking_level=level)
                    self.assertEqual(p._thinking_body(level), {"reasoning_effort": level})

    def test_openai_classic_models_never_send_reasoning_effort(self):
        # gpt-4o 등 일반 모델은 이 파라미터를 400 으로 거부한다 → level 이 조용한 no-op.
        for level in ("off", "low", "medium", "high"):
            with self.subTest(level=level):
                p = OpenAIProvider("s", "t", "", "gpt-4o", thinking_level=level)
                self.assertEqual(p._thinking_body(level), {})

    def test_openai_off_omits_reasoning_effort_even_on_reasoning_models(self):
        # OpenAI 는 reasoning 을 끄는 스위치가 없다. 생략해 서버 기본값에 맡긴다.
        p = OpenAIProvider("s", "t", "", "gpt-5", thinking_level="off")
        self.assertEqual(p._thinking_body("off"), {})

    async def test_openai_stream_body_carries_reasoning_effort(self):
        p = OpenAIProvider("s", "t", "", "gpt-5", thinking_level="high")
        body = await self._stream_body(p)
        self.assertEqual(body["reasoning_effort"], "high")
        # 추론 모델이므로 temperature 는 여전히 생략된다(기존 회귀 가드와 동일 규칙).
        self.assertNotIn("temperature", body)

    # ── Anthropic ────────────────────────────────────────────────────
    def _anthropic_body(self, level, *, max_tokens=config.MAX_COMPLETION_TOKENS_THINKING):
        p = AnthropicProvider("s", "t", "", "claude-x", thinking_level=level)
        return p._build_body(
            [{"role": "user", "content": "hi"}],
            temperature=0.7, max_tokens=max_tokens,
            thinking_level=None, stream=False,
        )

    def test_anthropic_off_has_no_thinking_block(self):
        body = self._anthropic_body("off")
        self.assertNotIn("thinking", body)
        self.assertEqual(body["temperature"], 0.7)

    def test_anthropic_budget_tokens_per_level(self):
        for level, budget in (("low", 2048), ("medium", 8192), ("high", 24576)):
            with self.subTest(level=level):
                body = self._anthropic_body(level)
                self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": budget})
                self.assertEqual(config.THINKING_BUDGET_BY_LEVEL[level], budget)

    def test_anthropic_forces_temperature_one_when_thinking(self):
        for level in ("low", "medium", "high"):
            with self.subTest(level=level):
                self.assertEqual(self._anthropic_body(level)["temperature"], 1)

    def test_anthropic_max_tokens_always_exceeds_budget(self):
        # high(24576) 는 MAX_COMPLETION_TOKENS_THINKING(16384) 보다 크므로
        # 상향이 없으면 API 가 400 을 낸다.
        for level in ("low", "medium", "high"):
            for max_tokens in (512, config.MAX_COMPLETION_TOKENS_THINKING):
                with self.subTest(level=level, max_tokens=max_tokens):
                    body = self._anthropic_body(level, max_tokens=max_tokens)
                    self.assertGreater(body["max_tokens"], body["thinking"]["budget_tokens"])

    def test_anthropic_keeps_large_max_tokens_untouched(self):
        body = self._anthropic_body("low", max_tokens=30000)
        self.assertEqual(body["max_tokens"], 30000)

    async def test_anthropic_llm_never_enables_thinking(self):
        # 메모리 키워드 추출·에이전트 라우팅 경로. 서버 기본이 high 여도 꺼져야 한다.
        p = AnthropicProvider("s", "t", "", "claude-x", thinking_level="high")
        fake = FakePostClient({
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 2},
        })
        p._client = fake

        await p.llm("hi", max_tokens=30)

        self.assertNotIn("thinking", fake.kwargs["json"])
        self.assertEqual(fake.kwargs["json"]["max_tokens"], 30)


class ThinkingLevelResolutionTests(unittest.IsolatedAsyncioTestCase):
    """서버 기본값(self.thinking_level) vs 요청별 오버라이드."""

    def test_none_falls_back_to_server_default(self):
        for cls, base in ((VLLMProvider, "http://vllm"), (AnthropicProvider, "")):
            for level in config.THINKING_LEVELS:
                with self.subTest(cls=cls.__name__, level=level):
                    p = cls("s", "t", base, "m", thinking_level=level)
                    self.assertEqual(p._effective_level(None), level)

    def test_explicit_level_overrides_server_default_in_both_directions(self):
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="off")
        self.assertEqual(p._effective_level("high"), "high")

        p2 = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="high")
        self.assertEqual(p2._effective_level("off"), "off")

    def test_unknown_level_degrades_to_off(self):
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="ultra")
        self.assertEqual(p.thinking_level, "off")
        self.assertEqual(p._effective_level("nonsense"), "off")

    def test_thinking_bool_stays_derived_from_level(self):
        for level, expected in (("off", False), ("low", True), ("medium", True), ("high", True)):
            with self.subTest(level=level):
                self.assertIs(VLLMProvider("s", "t", "http://v", "m", thinking_level=level).thinking,
                              expected)

    async def test_simulation_path_inherits_server_default(self):
        # bridge → client.async_chat → provider.chat() 은 level 을 넘기지 않는다.
        # 시뮬레이션에 별도 사고 UI 가 없어도 서버 기본값이 적용되는지 고정.
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="medium")
        fake = FakePostClient({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        })
        p._client = fake

        await p.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(
            fake.kwargs["json"]["chat_template_kwargs"],
            {"enable_thinking": True, "reasoning_effort": "medium"},
        )

    async def test_bridge_passes_thinking_level_through(self):
        provider = FakeChatProvider()
        registry = FakeChatRegistry(provider)
        old = llm_client.get_registry
        llm_client.get_registry = lambda: registry
        state.event_loop = asyncio.get_running_loop()
        try:
            chat = bridge.make_sync_chat(timeout=5)
            await asyncio.to_thread(chat, [{"role": "user", "content": "hi"}])
            # 기본은 None = 서버 기본값 상속 (프로바이더가 해석)
            self.assertEqual(provider.seen_thinking_levels, [None])

            chat_high = bridge.make_sync_chat(timeout=5, thinking_level="high")
            await asyncio.to_thread(chat_high, [{"role": "user", "content": "hi"}])
            self.assertEqual(provider.seen_thinking_levels[-1], "high")
        finally:
            llm_client.get_registry = old
            state.event_loop = None


class ThinkingHeadroomTests(unittest.IsolatedAsyncioTestCase):
    """사고가 실린 요청은 max_tokens 를 상향해야 한다 (QA M-1 / m-5)."""

    def _fake_post(self, provider):
        fake = FakePostClient({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        })
        provider._client = fake
        return fake

    async def test_short_simulation_call_gets_thinking_headroom(self):
        # ABM 의 classify_wave_time 은 max_tokens=256 으로 chat() 을 부른다.
        # 사고가 상속되면 256 이 전부 reasoning 에 소진돼 continuation 5회 빈 왕복이 된다.
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="medium")
        fake = self._fake_post(p)

        await p.chat([{"role": "user", "content": "hi"}], max_tokens=256)

        self.assertEqual(fake.kwargs["json"]["max_tokens"], config.MAX_COMPLETION_TOKENS_THINKING)

    async def test_off_leaves_short_max_tokens_untouched(self):
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="off")
        fake = self._fake_post(p)

        await p.chat([{"role": "user", "content": "hi"}], max_tokens=256)

        self.assertEqual(fake.kwargs["json"]["max_tokens"], 256)

    async def test_headroom_never_lowers_a_larger_budget(self):
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="high")
        fake = self._fake_post(p)

        await p.chat([{"role": "user", "content": "hi"}], max_tokens=32000)

        self.assertEqual(fake.kwargs["json"]["max_tokens"], 32000)

    async def test_stream_chat_also_gets_headroom(self):
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="low")
        fake = CapturingStreamClient()
        p._client = fake

        async for _ in p.stream_chat([{"role": "user", "content": "hi"}], max_tokens=256):
            pass

        self.assertEqual(fake.json_body["max_tokens"], config.MAX_COMPLETION_TOKENS_THINKING)

    def test_openai_reasoning_model_needs_headroom_even_when_off(self):
        # off 여도 OpenAI 는 reasoning 을 끌 수 없다(서버 기본값 medium).
        for model in ("gpt-5", "o1-mini", "o3", "o4-mini"):
            with self.subTest(model=model):
                p = OpenAIProvider("s", "t", "", model, thinking_level="off")
                self.assertTrue(p.needs_thinking_headroom("off"))

    def test_openai_classic_model_never_needs_headroom(self):
        # gpt-4o 는 4단계 전부 no-op 이므로 reasoning 토큰을 쓰지 않는다.
        p = OpenAIProvider("s", "t", "", "gpt-4o", thinking_level="high")
        for level in config.THINKING_LEVELS:
            with self.subTest(level=level):
                self.assertFalse(p.needs_thinking_headroom(level))

    async def test_openai_reasoning_model_off_still_raises_max_tokens(self):
        p = OpenAIProvider("s", "t", "", "gpt-5", thinking_level="off")
        fake = self._fake_post(p)

        await p.chat([{"role": "user", "content": "hi"}], max_tokens=4096)

        self.assertEqual(
            fake.kwargs["json"]["max_completion_tokens"],
            config.MAX_COMPLETION_TOKENS_THINKING,
        )
        # off 이므로 reasoning_effort 는 여전히 생략된다.
        self.assertNotIn("reasoning_effort", fake.kwargs["json"])

    def test_vllm_headroom_follows_effective_level(self):
        p = VLLMProvider("s", "t", "http://vllm", "m", thinking_level="off")
        self.assertFalse(p.needs_thinking_headroom(None))
        self.assertTrue(p.needs_thinking_headroom("low"))

    def test_anthropic_headroom_follows_effective_level(self):
        p = AnthropicProvider("s", "t", "", "claude-x", thinking_level="high")
        self.assertTrue(p.needs_thinking_headroom(None))
        self.assertFalse(p.needs_thinking_headroom("off"))


class OpenAIReasoningEffortExclusionTests(unittest.TestCase):
    """o1-mini / o1-preview 는 reasoning_effort 를 400 으로 거부한다 (QA m-2)."""

    def test_o1_mini_and_preview_omit_reasoning_effort(self):
        for model in ("o1-mini", "o1-preview", "o1-mini-2024-09-12"):
            for level in ("low", "medium", "high"):
                with self.subTest(model=model, level=level):
                    p = OpenAIProvider("s", "t", "", model, thinking_level=level)
                    self.assertEqual(p._thinking_body(level), {})

    def test_full_o1_still_sends_reasoning_effort(self):
        p = OpenAIProvider("s", "t", "", "o1", thinking_level="high")
        self.assertEqual(p._thinking_body("high"), {"reasoning_effort": "high"})

    def test_excluded_models_still_omit_temperature_and_need_headroom(self):
        # 제외는 _thinking_body 한정 — 다른 두 분기는 그대로여야 한다.
        p = OpenAIProvider("s", "t", "", "o1-mini", thinking_level="high")
        self.assertEqual(p._temperature_body(0.7), {})
        self.assertTrue(p.needs_thinking_headroom("high"))


class NormalizeThinkingLevelTests(unittest.TestCase):
    """SQLite INTEGER 는 int 로 돌아온다 — bool 폴백이 실제로 동작해야 한다 (QA m-1)."""

    def test_sqlite_integers_are_coerced_like_bools(self):
        self.assertEqual(config.normalize_thinking_level(1), "medium")
        self.assertEqual(config.normalize_thinking_level(0), "off")
        self.assertEqual(config.normalize_thinking_level(True), "medium")
        self.assertEqual(config.normalize_thinking_level(False), "off")

    def test_row_fallback_works_when_level_column_is_missing_or_junk(self):
        for row in ({"thinking": 1}, {"thinking_level": None, "thinking": 1},
                    {"thinking_level": "junk", "thinking": 1}):
            with self.subTest(row=row):
                self.assertEqual(servers_api._row_thinking_level(row), "medium")

    def test_row_fallback_off_when_legacy_flag_is_zero(self):
        self.assertEqual(servers_api._row_thinking_level({"thinking": 0}), "off")

    def test_valid_level_still_wins_over_legacy_flag(self):
        self.assertEqual(
            servers_api._row_thinking_level({"thinking_level": "low", "thinking": 1}), "low")

    def test_strings_and_none_are_unchanged(self):
        self.assertEqual(config.normalize_thinking_level(None), "off")
        self.assertIsNone(config.normalize_thinking_level(None, default=None))
        self.assertEqual(config.normalize_thinking_level(" HIGH "), "high")
        self.assertEqual(config.normalize_thinking_level("ultra"), "off")


class ThinkingLevelSchemaTests(unittest.TestCase):
    """ChatMessage / ServerCreate / ServerUpdate 의 thinking_level 계약."""

    def test_chat_message_defaults_to_none_meaning_server_default(self):
        self.assertIsNone(ChatMessage(content="hi").thinking_level)

    def test_chat_message_accepts_every_level(self):
        for level in config.THINKING_LEVELS:
            with self.subTest(level=level):
                self.assertEqual(ChatMessage(content="hi", thinking_level=level).thinking_level, level)

    def test_invalid_level_is_rejected(self):
        for payload in ({"content": "hi", "thinking_level": "ultra"},
                        {"content": "hi", "thinking_level": "OFF!"}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    ChatMessage(**payload)

    def test_invalid_server_level_is_rejected(self):
        with self.assertRaises(ValidationError):
            ServerCreate(name="n", base_url="u", model="m", thinking_level="maximum")

    def test_legacy_thinking_bool_is_coerced(self):
        self.assertEqual(ChatMessage(content="hi", thinking=True).thinking_level, "medium")
        self.assertEqual(ChatMessage(content="hi", thinking=False).thinking_level, "off")
        self.assertEqual(
            ServerCreate(name="n", base_url="u", model="m", thinking=True).thinking_level, "medium")
        self.assertEqual(ServerUpdate(thinking=True).thinking_level, "medium")

    def test_explicit_level_wins_over_legacy_bool(self):
        msg = ChatMessage(content="hi", thinking=True, thinking_level="off")
        self.assertEqual(msg.thinking_level, "off")

    def test_server_create_defaults_to_off(self):
        self.assertEqual(ServerCreate(name="n", base_url="u", model="m").thinking_level, "off")

    def test_server_update_leaves_level_unset_when_absent(self):
        self.assertNotIn("thinking_level", ServerUpdate(name="x").model_dump(exclude_unset=True))


class ThinkingLevelMigrationTests(unittest.TestCase):
    """servers.thinking(bool) → servers.thinking_level(TEXT) 값 마이그레이션."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmpdir.name) / "memory.db"

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmpdir.cleanup()

    def _legacy_db(self):
        conn = get_db()
        init_tables(conn)
        # 구 스키마 재현: thinking_level 컬럼이 없는 servers 테이블
        conn.execute("DROP TABLE servers")
        conn.execute("""
            CREATE TABLE servers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                model TEXT NOT NULL, provider_type TEXT NOT NULL DEFAULT 'vllm',
                api_key TEXT NOT NULL DEFAULT '',
                weight INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1, is_default INTEGER NOT NULL DEFAULT 0,
                thinking INTEGER NOT NULL DEFAULT 0, max_model_len INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        for sid, thinking in (("s-on", 1), ("s-off", 0)):
            conn.execute(
                "INSERT INTO servers (id, name, base_url, model, thinking, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (sid, sid, "http://x", "m", thinking, "2026-01-01T00:00:00"),
            )
        conn.commit()
        return conn

    def test_thinking_true_becomes_medium_and_false_becomes_off(self):
        conn = self._legacy_db()
        migrate_db(conn)
        rows = dict(conn.execute("SELECT id, thinking_level FROM servers").fetchall())
        conn.close()

        self.assertEqual(rows["s-on"], "medium")
        self.assertEqual(rows["s-off"], "off")

    def test_migration_is_idempotent(self):
        conn = self._legacy_db()
        migrate_db(conn)
        conn.execute("UPDATE servers SET thinking_level='high' WHERE id='s-on'")
        conn.commit()
        migrate_db(conn)  # 두 번째 실행이 이미 승격된 값을 덮어쓰지 않아야 한다
        rows = dict(conn.execute("SELECT id, thinking_level FROM servers").fetchall())
        conn.close()

        self.assertEqual(rows["s-on"], "high")

    def test_invalid_stored_level_is_normalized_to_off(self):
        conn = self._legacy_db()
        migrate_db(conn)
        conn.execute("UPDATE servers SET thinking_level='ultra' WHERE id='s-on'")
        conn.commit()
        migrate_db(conn)
        rows = dict(conn.execute("SELECT id, thinking_level FROM servers").fetchall())
        conn.close()

        self.assertEqual(rows["s-on"], "off")

    def test_sanity_update_resyncs_derived_thinking_column(self):
        # API 를 거치지 않는 외부 쓰기로 두 컬럼이 드리프트해도 기동 시 맞춰져야 한다.
        conn = self._legacy_db()
        migrate_db(conn)
        conn.execute("UPDATE servers SET thinking_level='high', thinking=0 WHERE id='s-on'")
        conn.execute("UPDATE servers SET thinking_level='off', thinking=1 WHERE id='s-off'")
        conn.commit()

        migrate_db(conn)
        rows = {r[0]: (r[1], r[2]) for r in
                conn.execute("SELECT id, thinking_level, thinking FROM servers").fetchall()}
        conn.close()

        self.assertEqual(rows["s-on"], ("high", 1))
        self.assertEqual(rows["s-off"], ("off", 0))

    def test_seed_honours_provider_type_and_thinking_level(self):
        conn = get_db()
        init_tables(conn)
        migrate_db(conn)
        seed = Path(self.tmpdir.name) / "servers.json"
        seed.write_text(json.dumps([
            {"name": "anthropic-seed", "base_url": "https://api.anthropic.com",
             "model": "claude-x", "provider_type": "anthropic", "thinking_level": "high"},
            {"name": "legacy-seed", "base_url": "http://v", "model": "m", "thinking": True},
            {"name": "plain-seed", "base_url": "http://v", "model": "m"},
        ]), encoding="utf-8")

        seed_default_servers(conn, path=str(seed))
        rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM servers").fetchall()}
        conn.close()

        self.assertEqual(rows["anthropic-seed"]["provider_type"], "anthropic")
        self.assertEqual(rows["anthropic-seed"]["thinking_level"], "high")
        self.assertEqual(rows["anthropic-seed"]["thinking"], 1)
        # provider_type 미지정 seed 는 기존대로 vllm
        self.assertEqual(rows["legacy-seed"]["provider_type"], "vllm")
        self.assertEqual(rows["legacy-seed"]["thinking_level"], "medium")
        self.assertEqual(rows["plain-seed"]["thinking_level"], "off")

    def test_row_to_dict_exposes_level_and_derived_bool(self):
        conn = self._legacy_db()
        migrate_db(conn)
        row = conn.execute("SELECT * FROM servers WHERE id='s-on'").fetchone()
        conn.close()

        d = servers_api._row_to_dict(row)
        self.assertEqual(d["thinking_level"], "medium")
        self.assertIs(d["thinking"], True)


class MeetingHelperTests(unittest.TestCase):
    """랑데부 계산 순수 함수 (ABM/simulation/meeting.py).

    Simulation 상태와 무관한 계산만 담당하므로 여기서 단위로 못 박아 둔다.
    """

    # X — M — N — Z 선형 그래프 + Z에 매달린 W, 그리고 아무와도 연결 안 된 고립 노드.
    GRAPH = {
        "X": ["M"], "M": ["X", "N"], "N": ["M", "Z"], "Z": ["N", "W"], "W": ["Z"],
        "고립": [],
    }

    def _find_path(self, start, goal):
        from collections import deque
        if start == goal:
            return []
        if goal not in self.GRAPH or start not in self.GRAPH:
            return []
        visited, q = {start}, deque([(start, [])])
        while q:
            node, path = q.popleft()
            for nb in self.GRAPH.get(node, []):
                if nb == goal:
                    return path + [nb]
                if nb not in visited:
                    visited.add(nb)
                    q.append((nb, path + [nb]))
        return []

    def test_hop_count_separates_same_node_from_unreachable(self):
        from ABM.simulation.meeting import hop_count

        # _find_path는 둘 다 []로 돌려준다 — 비용 비교에서 반드시 갈라져야 한다.
        self.assertEqual(hop_count(self._find_path, "X", "X"), 0)
        self.assertEqual(hop_count(self._find_path, "X", "Z"), 3)
        self.assertIsNone(hop_count(self._find_path, "X", "고립"))

    def test_weak_components_merges_convergent_intents(self):
        from ABM.simulation.meeting import weak_components

        # a→c, b→c 는 방향이 c로만 향하지만 세 사람이 한 자리에 모여야 하므로
        # 하나의 컴포넌트다. d↔e 는 별개.
        self.assertEqual(
            weak_components({"a": "c", "b": "c", "d": "e", "e": "d"}),
            [["a", "b", "c"], ["d", "e"]],
        )

    def test_weak_components_is_order_independent(self):
        from ABM.simulation.meeting import weak_components

        chain = {"c": "b", "b": "a"}
        self.assertEqual(weak_components(chain), [["a", "b", "c"]])
        self.assertEqual(weak_components(dict(reversed(list(chain.items())))),
                         [["a", "b", "c"]])
        self.assertEqual(weak_components({}), [])

    def test_gathering_node_minimises_total_hops(self):
        from ABM.simulation.meeting import gathering_node

        # a@X, b@M, c@Z → X:0+1+3=4, M:1+0+2=3, Z:3+2+0=5 → M
        self.assertEqual(
            gathering_node({"a": "X", "b": "M", "c": "Z"}, self._find_path), "M")

    def test_gathering_node_breaks_ties_by_agent_key(self):
        from ABM.simulation.meeting import gathering_node

        # a@X, b@Z 는 어느 쪽으로 가도 총 3홉 — 사전순 앞선 a의 자리(X)로 고정.
        self.assertEqual(gathering_node({"a": "X", "b": "Z"}, self._find_path), "X")
        # key만 바꾸면 결과도 그 규칙대로 따라 움직인다(자리 이름이 아니라 key 기준).
        self.assertEqual(gathering_node({"z": "X", "b": "Z"}, self._find_path), "Z")

    def test_gathering_node_skips_unreachable_candidates_and_empty_input(self):
        from ABM.simulation.meeting import gathering_node

        # 고립 노드는 다른 참가자가 갈 수 없으므로 후보에서 빠지고, X가 남는다.
        self.assertEqual(
            gathering_node({"a": "X", "b": "고립"}, self._find_path), None)
        self.assertEqual(gathering_node({"a": "X", "b": "M"}, self._find_path), "X")
        self.assertIsNone(gathering_node({}, self._find_path))


class _MeetingSimHarness:
    """만남(추격/랑데부) 테스트용 공통 하네스. 그 자체는 TestCase가 아니다."""

    # X — M — N — Z 선형 + Z에 붙은 W, 그리고 Z 밖의 외부 공간. 전부 같은 구역.
    LOCATION_GRAPH = [
        {"name": "X", "connects_to": ["M"],      "zone": "집"},
        {"name": "M", "connects_to": ["X", "N"], "zone": "집"},
        {"name": "N", "connects_to": ["M", "Z"], "zone": "집"},
        {"name": "Z", "connects_to": ["N", "W", "밖"], "zone": "집"},
        {"name": "W", "connects_to": ["Z"],      "zone": "집"},
        {"name": "밖", "connects_to": ["Z"], "zone": "집", "is_exterior": True},
    ]

    def _run(self, script, locations, *, max_waves=1, graph=None, keys=None,
             events=None, **sim_kw):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        keys = keys or sorted(locations)
        with tempfile.TemporaryDirectory() as tmp:
            agents = {
                key: Agent(key, f"너는 {key}다.", tmp, token_limit=4096)
                for key in keys
            }
            sim = Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
                llm=_ScriptedLLM(script),
                agent_locations=dict(locations),
                location_graph=self.LOCATION_GRAPH if graph is None else graph,
                **sim_kw,
            )
            emitted = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim.run(keys[0], max_waves=max_waves, step_delay=0.0,
                    early_stop_enabled=False, events=events,
                    resume_wave={k: [] for k in keys})
            return sim, emitted

    @staticmethod
    def _meet(target):
        """상대를 지목하는 첫 턴 + 그 뒤로는 아무 것도 선언하지 않는 턴."""
        return [{"content": "만나러 간다.", "target": "self", "move_to": target},
                {"content": "...", "target": "self"}]

    IDLE = [{"content": "...", "target": "self"}]

    @staticmethod
    def _meeting_events(emitted, chaser=None):
        return [d for t, d in emitted
                if t == "meeting_update" and (chaser is None or d["chaser"] == chaser)]

    def _flow(self, emitted, chaser):
        """chaser의 meeting_update 흐름을 (status, reason) 시퀀스로."""
        return [(d["status"], d["reason"]) for d in self._meeting_events(emitted, chaser)]


class MeetingRendezvousTests(_MeetingSimHarness, unittest.TestCase):
    """`move_to`에 사람을 지목했을 때의 추격/랑데부 (ABM/simulation/meeting.py).

    핵심 불변식: 서로를 만나려는 두 사람은 **반드시 수렴**해야 한다. 각자 상대의
    '현재 위치'를 목적지로 삼으면 두 자리를 영원히 맞바꾸며 엇갈린다(간선 스왑).
    """

    def test_mutual_intent_converges_without_swapping(self):
        # a@X와 b@Z가 서로를 지목 → 총 홉 동점(3:3)이므로 사전순 앞선 a의 자리 X로
        # 집결. b만 걸어오고 a는 한 발짝도 움직이지 않아야 한다(스왑 없음).
        sim, emitted = self._run(
            {"a": self._meet("b"), "b": self._meet("a")},
            {"a": "X", "b": "Z"}, max_waves=5,
        )

        self.assertEqual(sim._agent_location["a"], "X")
        self.assertEqual(sim._agent_location["b"], "X")
        self.assertEqual(sim._meeting_intent, {})   # 동석 성립 → lock 해제
        movers = {d["agent"] for t, d in emitted if t == "agent_move"}
        self.assertEqual(movers, {"b"})

    def test_rendezvous_node_is_not_recomputed_midway(self):
        # 랑데부는 한 번 정하면 도착까지 고정이다. 매 웨이브 다시 계산하면 b가
        # 다가오는 동안 총 홉 균형이 바뀌어 목적지가 흔들린다.
        sim, _ = self._run(
            {"a": self._meet("b"), "b": self._meet("a")},
            {"a": "X", "b": "Z"}, max_waves=2,
        )

        self.assertEqual(sim._agent_location["a"], "X")
        self.assertEqual(sim._agent_location["b"], "M")   # Z → N → M …
        self.assertEqual(sim._agent_path["b"], ["X"])     # 목적지는 계속 X

    def test_one_way_chase_targets_the_movers_final_destination(self):
        # b는 a를 만날 생각이 없고 Z→W로 이동 중. a가 b의 '현재 위치' Z를 쫓으면
        # 도착했을 때 b는 이미 없다. 최종 목적지 W로 직행해야 한다.
        sim, _ = self._run(
            {"a": self._meet("b"),
             "b": [{"content": "저리 간다.", "target": "self", "move_to": "W"}] + self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=1,
        )

        self.assertEqual(sim._agent_location["b"], "W")
        self.assertEqual(sim._agent_location["a"], "M")
        self.assertEqual(sim._agent_path["a"], ["N", "Z", "W"])  # Z가 아니라 W까지

    def test_one_way_chase_eventually_catches_up(self):
        sim, _ = self._run(
            {"a": self._meet("b"),
             "b": [{"content": "저리 간다.", "target": "self", "move_to": "W"}] + self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=6,
        )

        self.assertEqual(sim._agent_location["a"], "W")
        self.assertEqual(sim._agent_location["b"], "W")
        self.assertEqual(sim._meeting_intent, {})

    def test_new_move_to_cancels_the_lock(self):
        # 두 번째 턴에서 장소를 직접 지정하면 만남 lock은 그 발화로 취소되고,
        # 새 목적지가 기존 추격 경로를 덮어쓴다. (a는 wave 0에 X→M까지 갔다가
        # wave 1에 발길을 돌려 X로 되돌아온다.)
        sim, _ = self._run(
            {"a": [{"content": "만나러 간다.", "target": "self", "move_to": "b"},
                   {"content": "아니, 돌아간다.", "target": "self", "move_to": "X"}],
             "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=2,
        )

        self.assertEqual(sim._meeting_intent, {})
        self.assertEqual(sim._agent_location["a"], "X")
        self.assertNotIn("a", sim._agent_path)

    def test_staying_put_cancels_the_chase_path_too(self):
        # 자기가 지금 있는 곳을 move_to로 선언 = "여기 있겠다". lock만 풀고 경로를
        # 남겨두면 몸은 계속 옛 목적지로 걸어가는 유령 추격이 된다.
        sim, _ = self._run(
            {"a": [{"content": "만나러 간다.", "target": "self", "move_to": "b"},
                   {"content": "역시 여기 있겠다.", "target": "self", "move_to": "M"}],
             "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=3,
        )

        self.assertEqual(sim._meeting_intent, {})
        self.assertEqual(sim._agent_location["a"], "M")
        self.assertNotIn("a", sim._agent_path)

    def test_target_leaving_to_exterior_breaks_lock_with_scene(self):
        # b가 외부 공간으로 나가면 추격은 목적을 잃는다 — 가던 길을 멈추고
        # "어디론가 가버렸다" 씬을 받아 상황을 다시 판단할 수 있어야 한다.
        sim, _ = self._run(
            {"a": self._meet("b"),
             "b": [{"content": "...", "target": "self"},
                   {"content": "나간다.", "target": "self", "move_to": "밖"}] + self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=3,
        )

        self.assertEqual(sim._agent_location["b"], "밖")
        self.assertEqual(sim._meeting_intent, {})
        self.assertNotIn("a", sim._agent_path)          # 추격 중단
        self.assertEqual(sim._agent_location["a"], "N")  # 두 칸만 가고 멈춤
        scene = [m["content"] for m in sim._pending_wave.get("a", [])
                 if m["speaker"] == "씬"]
        self.assertIn("[씬] b이(가) 어디론가 가버렸다.", scene)

    def test_chaser_already_at_the_goal_waits_instead_of_giving_up(self):
        # a가 b를 쫓아 M까지 왔는데 b가 뒤늦게 M으로 오기 시작한 상황. a는 가던
        # 길을 멈추고 그 자리에서 기다려야 한다 — lock을 버리면 안 된다.
        sim, _ = self._run(
            {"a": self._meet("b"),
             "b": [{"content": "...", "target": "self"},
                   {"content": "그쪽으로 간다.", "target": "self", "move_to": "M"}] + self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=2,
        )

        self.assertEqual(sim._agent_location["a"], "M")
        self.assertNotIn("a", sim._agent_path)          # 제자리에서 대기
        self.assertEqual(sim._meeting_intent, {"a": "b"})  # lock 유지

    def test_chain_of_three_gathers_at_the_stationary_target(self):
        # a→c, b→c. c는 아무도 만나러 가지 않으므로 끌려다니면 안 된다.
        sim, emitted = self._run(
            {"a": self._meet("c"), "b": self._meet("c"), "c": self.IDLE},
            {"a": "X", "b": "Z", "c": "N"}, max_waves=4,
        )

        self.assertEqual(
            {k: sim._agent_location[k] for k in ("a", "b", "c")},
            {"a": "N", "b": "N", "c": "N"},
        )
        self.assertEqual(sim._meeting_intent, {})
        self.assertNotIn("c", {d["agent"] for t, d in emitted if t == "agent_move"})

    def test_intent_cycle_gathers_at_min_total_hop_node(self):
        # a→b→c→a 순환. 정지한 사람이 없으므로 총 홉 최소 노드(M)로 전원 집결.
        sim, _ = self._run(
            {"a": self._meet("b"), "b": self._meet("c"), "c": self._meet("a")},
            {"a": "X", "b": "M", "c": "Z"}, max_waves=4,
        )

        self.assertEqual(
            {k: sim._agent_location[k] for k in ("a", "b", "c")},
            {"a": "M", "b": "M", "c": "M"},
        )
        self.assertEqual(sim._meeting_intent, {})

    def test_exterior_agent_can_neither_chase_nor_be_chased(self):
        # 외부 공간은 완전 격리 — 지목 자체가 성립하지 않는다.
        sim, _ = self._run(
            {"a": self._meet("b"), "b": self._meet("a")},
            {"a": "밖", "b": "Z"}, max_waves=1,
        )

        self.assertEqual(sim._meeting_intent, {})
        self.assertEqual(sim._agent_location["a"], "밖")
        self.assertEqual(sim._agent_location["b"], "Z")

    def test_unperceivable_target_outside_zone_is_ignored(self):
        # zone이 설정된 시나리오에서 인지 범위 밖의 사람은 지목할 수 없다.
        # (그러지 않으면 zone이 세운 벽을 move_to 한 줄로 통과한다.)
        graph = self.LOCATION_GRAPH + [
            {"name": "먼곳", "connects_to": ["W"], "zone": "다른동네"},
        ]
        sim, _ = self._run(
            {"a": self._meet("b"), "b": self.IDLE},
            {"a": "X", "b": "먼곳"}, max_waves=1, graph=graph,
        )

        self.assertEqual(sim._meeting_intent, {})
        self.assertEqual(sim._agent_location["a"], "X")

    def test_situation_context_shows_who_is_being_followed(self):
        sim, emitted = self._run(
            {"a": self._meet("b"), "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=2,
        )

        texts = [d["text"] for t, d in emitted
                 if t == "turn_situation" and d["agent"] == "a"]
        self.assertNotIn("만나러 이동 중", texts[0])     # 아직 지목 전
        self.assertIn("b을(를) 만나러 이동 중 (현재 b는 Z에 있음)", texts[1])
        self.assertIn("만나러 가던 것은 취소됩니다", texts[1])

    def test_meeting_lock_survives_state_roundtrip(self):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        sim, _ = self._run(
            {"a": self._meet("b"), "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=1,
        )
        self.assertEqual(sim._meeting_intent, {"a": "b"})
        state = sim.export_agent_state()

        with tempfile.TemporaryDirectory() as tmp:
            agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096)
                      for k in ("a", "b")}
            revived = Simulation(
                agents, [], tmp, llm=_ScriptedLLM({}),
                location_graph=self.LOCATION_GRAPH,
            )
            revived.restore_agent_state(state)

            self.assertEqual(revived._meeting_intent, {"a": "b"})
            self.assertEqual(revived._agent_path["a"], sim._agent_path["a"])


class MeetingUpdateEventTests(_MeetingSimHarness, unittest.TestCase):
    """`meeting_update` SSE 이벤트 계약 (_workspace/CONTRACT_meeting_update.md).

    핵심 불변식: lock의 생성/해소가 한 wave에 한 건씩, 프론트가 추격선을 그렸다
    지울 수 있는 형태로 나가야 한다. diff 기준은 **지난 wave 종료 시점**이다 —
    _apply_move_intents가 만든 변화가 스냅샷에 먼저 녹아버리면 start 이벤트 자체가
    영영 나가지 않는다.
    """

    def test_start_event_payload(self):
        sim, emitted = self._run(
            {"a": self._meet("b"), "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=1,
            name_aliases={"민수": "a", "유나": "b"},
        )

        events = self._meeting_events(emitted)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], {
            "wave":            0,
            "chaser":          "a",
            "chaser_name":     "민수",
            "target":          "b",
            "target_name":     "유나",
            "target_location": "Z",
            "status":          "start",
            "reason":          None,
        })

    def test_arrived_event_when_chase_completes(self):
        sim, emitted = self._run(
            {"a": self._meet("b"),
             "b": [{"content": "저리 간다.", "target": "self", "move_to": "W"}] + self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=6,
        )

        self.assertEqual(self._flow(emitted, "a"),
                         [("start", None), ("arrived", "met")])
        arrived = self._meeting_events(emitted, "a")[-1]
        self.assertEqual(arrived["target_location"], "W")
        self.assertEqual(sim._agent_location["a"], "W")

    def test_mutual_rendezvous_emits_start_and_arrived_for_both(self):
        _, emitted = self._run(
            {"a": self._meet("b"), "b": self._meet("a")},
            {"a": "X", "b": "Z"}, max_waves=5,
        )

        self.assertEqual(self._flow(emitted, "a"),
                         [("start", None), ("arrived", "met")])
        self.assertEqual(self._flow(emitted, "b"),
                         [("start", None), ("arrived", "met")])

    def test_cancelled_new_move_to(self):
        _, emitted = self._run(
            {"a": [{"content": "만나러 간다.", "target": "self", "move_to": "b"},
                   {"content": "아니, 돌아간다.", "target": "self", "move_to": "X"}],
             "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=2,
        )

        self.assertEqual(self._flow(emitted, "a"),
                         [("start", None), ("cancelled", "new_move_to")])

    def test_cancelled_staying(self):
        _, emitted = self._run(
            {"a": [{"content": "만나러 간다.", "target": "self", "move_to": "b"},
                   {"content": "역시 여기 있겠다.", "target": "self", "move_to": "M"}],
             "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=3,
        )

        self.assertEqual(self._flow(emitted, "a"),
                         [("start", None), ("cancelled", "staying")])

    def test_cancelled_gone_when_target_leaves(self):
        _, emitted = self._run(
            {"a": self._meet("b"),
             "b": [{"content": "...", "target": "self"},
                   {"content": "나간다.", "target": "self", "move_to": "밖"}] + self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=3,
        )

        self.assertEqual(self._flow(emitted, "a"),
                         [("start", None), ("cancelled", "gone")])
        self.assertEqual(self._meeting_events(emitted, "a")[-1]["target_location"], "밖")

    def test_chaser_leaving_simulation_cancels_the_line(self):
        # 추격자가 퇴장하면 내부 사유는 "invalid"지만, 프론트가 추격선을 지울 수
        # 있도록 계약서 열거 안의 값(unreachable)으로 접어 내보낸다.
        _, emitted = self._run(
            {"a": self._meet("b"), "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=2,
            events=[{"wave": 1, "type": "agent_exit", "agent": "a",
                     "message": "a가 사라졌다.", "targets": ["all"]}],
        )

        self.assertEqual(self._flow(emitted, "a"),
                         [("start", None), ("cancelled", "unreachable")])

    def test_target_switch_emits_a_single_start(self):
        # 목표를 갈아타면 취소+시작 두 건이 아니라 새 목표로의 start 한 건이다
        # (프론트는 chaser당 추격선 하나만 그린다).
        _, emitted = self._run(
            {"a": [{"content": "b한테 간다.", "target": "self", "move_to": "b"},
                   {"content": "아니 c한테.", "target": "self", "move_to": "c"}],
             "b": self.IDLE, "c": self.IDLE},
            {"a": "X", "b": "Z", "c": "N"}, max_waves=2,
        )

        events = self._meeting_events(emitted, "a")
        self.assertEqual([(d["status"], d["target"]) for d in events],
                         [("start", "b"), ("start", "c")])
        self.assertEqual([d["wave"] for d in events], [0, 1])

    def test_target_name_respects_stranger_anonymity(self):
        # a와 b가 서로 모르는 사이면 이벤트에도 실명이 실리면 안 된다.
        sim, emitted = self._run(
            {"a": [{"content": "저 사람한테 간다.", "target": "self",
                    "move_to": "stranger_1"}] + self.IDLE,
             "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=1,
            agent_groups={"a": ["갑"], "b": ["을"]},
            name_aliases={"민수": "a", "유나": "b"},
        )

        # zone 인지 단계에서 stranger_1이 발급돼 있어야 지목이 성립한다.
        self.assertEqual(sim._stranger_map["a"], {"stranger_1": "b"})
        event = self._meeting_events(emitted, "a")[0]
        self.assertEqual(event["target"], "b")
        self.assertEqual(event["target_name"], '낯선 이(ID: "stranger_1")')
        self.assertEqual(event["chaser_name"], "민수")

    def test_unreachable_reason_maps_to_cancelled(self):
        # 경로가 끊겨 폐기되는 경우는 고정 지도에서 재현이 어려우므로 사유 → 페이로드
        # 변환만 직접 못 박는다.
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        with tempfile.TemporaryDirectory() as tmp:
            agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096)
                      for k in ("a", "b")}
            sim = Simulation(agents, [], tmp, llm=_ScriptedLLM({}),
                             agent_locations={"a": "X", "b": "Z"},
                             location_graph=self.LOCATION_GRAPH)
            emitted = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim._meeting_break_log["a"] = "unreachable"
            sim._emit_meeting_updates(7, {"a": "b"})

            self.assertEqual(self._flow(emitted, "a"), [("cancelled", "unreachable")])
            self.assertEqual(emitted[0][1]["wave"], 7)
            # 사유 버퍼는 wave 한정 — 소비 후 반드시 비워져야 다음 wave로 안 샌다.
            self.assertEqual(sim._meeting_break_log, {})

    def test_no_events_when_nobody_chases_anybody(self):
        # 사람 지목 move_to가 없는 기존 시나리오는 이벤트 0건.
        _, emitted = self._run(
            {"a": [{"content": "저기 간다.", "target": "self", "move_to": "M"}],
             "b": self.IDLE},
            {"a": "X", "b": "Z"}, max_waves=3,
        )

        self.assertEqual(self._meeting_events(emitted), [])

    def test_event_is_persisted(self):
        from ABM.simulation.core import _PERSIST_EVENTS

        self.assertIn("meeting_update", _PERSIST_EVENTS)


class MeetingBackCompatTests(unittest.TestCase):
    """사람 지목 move_to가 기존 시나리오의 동작을 건드리지 않는지 (하위 호환)."""

    GRAPH = [
        {"name": "입구", "connects_to": ["매장"]},
        {"name": "매장", "connects_to": ["입구", "창고"]},
        {"name": "창고", "connects_to": ["매장"]},
    ]

    def _sim(self, tmp, script, *, graph, locations=None, keys=("a", "b")):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in keys}
        return Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM(script),
            agent_locations=dict(locations) if locations else None,
            location_graph=graph,
        )

    def test_node_name_move_to_never_creates_a_meeting_lock(self):
        # zone 미설정 + 노드명 move_to = 기존 시나리오의 전형. 만남 lock은 애초에
        # 만들어지지 않아야 하고 이동 결과도 예전과 같아야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(
                tmp,
                {"a": [{"content": "창고 간다.", "target": "self", "move_to": "창고"}],
                 "b": [{"content": "음.", "target": "self"}]},
                graph=self.GRAPH, locations={"a": "매장", "b": "매장"},
            )
            sim._emit = lambda t, d: None
            sim.run("a", max_waves=1, step_delay=0.0)

            self.assertEqual(sim._meeting_intent, {})
            self.assertEqual(sim._agent_location["a"], "창고")

    def test_unknown_move_to_value_is_still_ignored(self):
        # 그래프에도 없고 사람도 아닌 값 → 예전처럼 조용히 무시(제자리).
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(
                tmp,
                {"a": [{"content": "??", "target": "self", "move_to": "달나라"}],
                 "b": [{"content": "음.", "target": "self"}]},
                graph=self.GRAPH, locations={"a": "매장", "b": "매장"},
            )
            sim._emit = lambda t, d: None
            sim.run("a", max_waves=1, step_delay=0.0)

            self.assertEqual(sim._meeting_intent, {})
            self.assertEqual(sim._agent_location["a"], "매장")

    def test_graphless_scenario_keeps_legacy_direct_move(self):
        # 위치 그래프가 없는 시나리오에서는 move_to 값이 무엇이든 예전 그대로
        # '직접 이동'으로 처리된다 — 사람 지목 해석은 지도가 있을 때만 켜진다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(
                tmp,
                {"a": [{"content": "간다.", "target": "self", "move_to": "b"}],
                 "b": [{"content": "음.", "target": "self"}]},
                graph=None,
            )
            sim._emit = lambda t, d: None
            sim.run("a", max_waves=1, step_delay=0.0)

            self.assertEqual(sim._meeting_intent, {})
            self.assertEqual(sim._agent_location["a"], "b")

    def test_person_chase_works_without_zones(self):
        # zone을 안 쓰는 지도라도 사람 지목은 동작한다(제약은 외부 공간뿐).
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(
                tmp,
                {"a": [{"content": "찾아간다.", "target": "self", "move_to": "b"}],
                 "b": [{"content": "음.", "target": "self"}]},
                graph=self.GRAPH, locations={"a": "입구", "b": "창고"},
            )
            sim._emit = lambda t, d: None
            sim.run("a", max_waves=1, step_delay=0.0)

            self.assertEqual(sim._meeting_intent, {"a": "b"})
            self.assertEqual(sim._agent_location["a"], "매장")
            self.assertEqual(sim._agent_path["a"], ["창고"])


# ── 프롬프트 계약 층 (ABM/prompt_contract.py) ─────────────────────────────────

# 이 변경 **이전에** 시나리오 저장 시점에 통째로 스냅샷돼 DB에 얼어붙은 템플릿.
# 로드된 옛 시나리오는 이걸 `output_format_template` 오버라이드로 들고 들어온다.
# `<MOVE_TO_HINT>` 자리표시자가 없다는 점이 핵심이다 — 치환이 no-op이 되고
# 하드코딩된 move_to 문구가 그대로 남아야 한다(문자열이 깨지지 않아야 한다).
_LEGACY_FROZEN_TEMPLATE = """

[Important Output Format]
당신의 응답은 반드시 다음 JSON 형식이어야 합니다. 다른 텍스트는 출력하지 마세요.
{
    "content": "당신의 말이나 행동을 자신의 말투로 (반드시 한국어로만)",
    "action_note": "행동이나 생각, 상황 묘사. 텍스트로 서술. 예: '한숨을 쉰다', '눈을 흘김'",
<FIELD_LINES>
    "target": ["id1", "id2"] 또는 "all" 또는 "self",
    "move_to": null,
    "update_appearance": null
}

- content: 당신의 말, 대사. **반드시 한국어로만 작성. 중국어 한자·영어 등 외국어 절대 금지.**
- action_note: 행동이나 생각 묘사. 이 내용은 다른 에이전트에게 **시각적 정보**로 전달됨.
<FIELD_HINTS>
- move_to: 이동할 위치 이름, 또는 **만나러 갈 사람의 ID** (그 사람이 있는 곳까지 따라갑니다). 이동 없으면 null
- update_appearance: 외모 변화가 있을 때 새 외모 전체 묘사 (없으면 null)
- target: 반드시 아래 시스템 ID만 사용 (표시 이름 절대 금지):
<TARGETS><TARGETS_FOOTER>
⚠ content 필드는 반드시 한국어로만 작성하십시오. 외국어·한자 사용 금지.
"""

_FIELDS = [
    {"name": "emotion",     "default": "neutral"},
    {"name": "action",      "default": "speak"},
    {"name": "action_note", "default": ""},
]

_GRAPH_FLAT = {"입구": ["매장"], "매장": ["입구", "창고"], "창고": ["매장"]}
_GRAPH_ZONED = {"안방": ["거실"], "거실": ["안방", "밖"], "밖": ["거실"]}
_ZONES = {"안방": "집", "거실": "집"}


class EngineContractBuilderTests(unittest.TestCase):
    """계약 빌더는 순수 함수다 — config 조합만 보고 문자열을 만든다.

    핵심 불변식: **활성화된 feature만** 지시어를 낸다. 특히 위치 그래프가 없으면
    `move_to` 사람 지목(rendezvous)을 절대 광고하지 않아야 한다 —
    `_MeetingMixin._is_location_name()`이 그래프 미설정 시 항상 True를 돌려주므로
    그 경로에서 사람 지목은 아예 해석되지 않는다(동작하지 않는 기능 광고 금지).
    """

    def test_move_to_hint_is_legacy_without_location_graph(self):
        from ABM.prompt_contract import build_move_to_hint

        hint = build_move_to_hint(has_location_graph=False, has_zone=False)
        self.assertIn("이동할 위치 이름", hint)
        self.assertNotIn("사람의 ID", hint)
        self.assertNotIn("중간", hint)  # 랑데부 안내 없음

    def test_move_to_hint_describes_rendezvous_with_graph(self):
        from ABM.prompt_contract import build_move_to_hint

        hint = build_move_to_hint(has_location_graph=True, has_zone=False)
        self.assertIn("만나러 갈 사람의 ID", hint)
        self.assertIn("중간 지점", hint)     # 랑데부
        self.assertIn("취소", hint)          # 마음 바꾸기
        # zone이 없으면 "장소명 말고 ID" 추가 안내는 붙지 않는다.
        self.assertNotIn("장소명이 아니라", hint)

    def test_move_to_hint_adds_zone_rule(self):
        from ABM.prompt_contract import build_move_to_hint

        hint = build_move_to_hint(has_location_graph=True, has_zone=True)
        self.assertIn("만나러 갈 사람의 ID", hint)
        self.assertIn("장소명이 아니라", hint)

    def test_map_contract_is_empty_without_graph(self):
        from ABM.prompt_contract import build_map_contract

        self.assertEqual(build_map_contract(location_graph=None), "")
        self.assertEqual(build_map_contract(location_graph={}), "")

    def test_map_contract_conditional_sections(self):
        from ABM.prompt_contract import build_map_contract

        plain = build_map_contract(location_graph=_GRAPH_FLAT)
        self.assertIn("[위치 그래프", plain)
        self.assertIn("매장: 입구, 창고", plain)
        self.assertNotIn("[외부 공간]", plain)
        self.assertNotIn("[구역:", plain)

        zoned = build_map_contract(
            location_graph=_GRAPH_ZONED,
            exterior_locations={"밖"},
            location_zone=_ZONES,
        )
        self.assertIn("안방 [구역: 집]", zoned)
        self.assertIn("밖 [외부 공간]", zoned)
        self.assertIn("시뮬레이션 경계 밖", zoned)
        self.assertIn("같은 생활권", zoned)

    def test_time_contract_toggles(self):
        from ABM.prompt_contract import build_time_contract

        self.assertEqual(build_time_contract(time_enabled=False), "")
        self.assertIn("[시간 인식]", build_time_contract(time_enabled=True))

    def test_infection_contract_toggles_and_names_disease(self):
        from ABM.prompt_contract import build_infection_contract

        self.assertEqual(build_infection_contract(infection_enabled=False), "")
        named = build_infection_contract(infection_enabled=True, disease_name="독감")
        self.assertIn("[몸 상태 인식]", named)
        self.assertIn("독감", named)
        # raw 상태값(S/I/R)·확률은 절대 새면 안 된다.
        self.assertNotIn("감염 확률", named)
        self.assertIn("지어내지 마세요", named)

        unnamed = build_infection_contract(infection_enabled=True, disease_name="")
        self.assertIn("전염병", unnamed)

    def test_world_contract_combines_only_active_features(self):
        from ABM.prompt_contract import build_world_contract

        self.assertEqual(build_world_contract(), "")

        full = build_world_contract(
            location_graph=_GRAPH_ZONED, exterior_locations={"밖"},
            location_zone=_ZONES, time_enabled=True,
            infection_enabled=True, disease_name="독감",
        )
        # 조립 순서: 지도 → 시간 → 감염
        self.assertLess(full.index("[위치 그래프"), full.index("[시간 인식]"))
        self.assertLess(full.index("[시간 인식]"), full.index("[몸 상태 인식]"))

    def test_output_contract_target_shapes(self):
        from ABM.prompt_contract import build_output_contract

        flat = build_output_contract(["a", "b"], _FIELDS, {"a": "에이", "b": "비"})
        self.assertIn('- ID: "a"  (에이)', flat)
        self.assertIn('전체에게: "all"', flat)

        sectioned = build_output_contract(
            [], _FIELDS, {"a": "에이"},
            target_sections=[("아는 사람", ["a"]), ("처음 보는 사람", ["stranger_1"])],
        )
        self.assertIn("[아는 사람]", sectioned)
        self.assertIn('group:아는 사람', sectioned)  # 그룹 2개 이상 → 단축 표기

        situational = build_output_contract([], _FIELDS, situation_targets=True)
        self.assertIn("[현재 상황] 컨텍스트에서", situational)

        empty = build_output_contract([], _FIELDS)
        self.assertIn("(없음)", empty)

    def test_engine_contract_full_assembly_order(self):
        from ABM.prompt_contract import build_engine_contract

        text = build_engine_contract(
            extra_fields=_FIELDS, available_targets=["a"],
            location_graph=_GRAPH_ZONED, exterior_locations={"밖"},
            location_zone=_ZONES, time_enabled=True,
            infection_enabled=True, disease_name="독감",
        )
        order = [
            text.index("[위치 그래프"),
            text.index("[시간 인식]"),
            text.index("[몸 상태 인식]"),
            text.index("[Important Output Format]"),
        ]
        self.assertEqual(order, sorted(order))

    def test_engine_contract_interview_carve_out(self):
        # 인터뷰 모드는 자연어 산문 답변을 받아야 하므로 출력 스키마를 뺀다.
        from ABM.prompt_contract import build_engine_contract

        text = build_engine_contract(
            extra_fields=_FIELDS, location_graph=_GRAPH_FLAT, time_enabled=True,
            include_output_schema=False,
        )
        self.assertIn("[위치 그래프", text)
        self.assertNotIn("[Important Output Format]", text)
        self.assertNotIn('"update_appearance"', text)

    def test_legacy_frozen_template_still_renders_equivalently(self):
        # 옛 프리즈 템플릿이 오버라이드로 들어와도 스키마/target/update_appearance
        # 문구는 엔진 생성본과 동등해야 한다(회귀 없음).
        from ABM.prompt_contract import build_output_contract

        kwargs = dict(key_to_alias={"a": "에이"}, has_location_graph=True)
        frozen = build_output_contract(["a"], _FIELDS, template=_LEGACY_FROZEN_TEMPLATE, **kwargs)
        engine = build_output_contract(["a"], _FIELDS, **kwargs)

        for token in ('"target": ["id1", "id2"]', '"move_to": null',
                      '"update_appearance": null', '- ID: "a"  (에이)',
                      '전체에게: "all"', '"emotion": "neutral"'):
            self.assertIn(token, frozen, token)
            self.assertIn(token, engine, token)
        # 자리표시자가 남지 않아야 한다.
        self.assertNotIn("<", frozen.replace("**", ""))

    def test_agent_module_reexports_stay_importable(self):
        # backend/api/simulation/scenarios.py 가 아직 이 경로로 import 한다.
        from ABM.agent import DEFAULT_OUTPUT_FORMAT_TEMPLATE, _build_output_format
        from ABM.prompt_contract import (
            DEFAULT_OUTPUT_FORMAT_TEMPLATE as CANON, build_output_contract,
        )

        self.assertIs(DEFAULT_OUTPUT_FORMAT_TEMPLATE, CANON)
        self.assertIs(_build_output_format, build_output_contract)


class EngineContractAssemblyTests(unittest.TestCase):
    """Simulation이 계약을 소유·주입하는 방식 (ABM/simulation/core.py).

    핵심 불변식: `agent.system_prompt`(사용자 소유)는 절대 오염되지 않는다.
    예전엔 core.py가 `agent.system_prompt += map_section` 으로 직접 이어붙여
    사용자 프롬프트와 엔진 지시어가 한 문자열로 뭉개졌고, 같은 Agent로
    Simulation을 두 번 만들면 계약이 중복 누적됐다.
    """

    def _sim(self, tmp, agents, **kw):
        from ABM.simulation import Simulation
        return Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp, **kw,
        )

    def _agents(self, tmp):
        from ABM.agent import Agent
        return {k: Agent(k, f"너는 {k}다.", tmp, token_limit=8192) for k in ("a", "b")}

    def test_user_system_prompt_stays_pure(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = self._agents(tmp)
            self._sim(
                tmp, agents,
                location_graph=[{"name": "안방", "connects_to": ["거실"], "zone": "집"},
                                {"name": "거실", "connects_to": ["안방"], "zone": "집"}],
                time_per_wave=30,
                infection_model={"enabled": True, "disease_name": "독감"},
            )
            self.assertEqual(agents["a"].system_prompt, "너는 a다.")
            self.assertIn("[위치 그래프", agents["a"].engine_contract)
            self.assertIn("[시간 인식]", agents["a"].engine_contract)
            self.assertIn("[몸 상태 인식]", agents["a"].engine_contract)

    def test_contract_is_replaced_not_accumulated(self):
        # 같은 Agent 객체로 Simulation을 두 번 만들어도 계약은 한 벌만 남아야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            agents = self._agents(tmp)
            graph = [{"name": "안방", "connects_to": []}]
            self._sim(tmp, agents, location_graph=graph, time_per_wave=30)
            first = agents["a"].engine_contract
            self._sim(tmp, agents, location_graph=graph, time_per_wave=30)
            self.assertEqual(agents["a"].engine_contract, first)
            self.assertEqual(first.count("[시간 인식]"), 1)

    def test_assembled_system_message_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = self._agents(tmp)
            self._sim(
                tmp, agents,
                location_graph=[{"name": "안방", "connects_to": ["거실"], "zone": "집"},
                                {"name": "거실", "connects_to": ["안방"], "zone": "집"}],
                time_per_wave=30,
                infection_model={"enabled": True, "disease_name": "독감"},
            )
            text = agents["a"].get_system_message(["b"], {"b": "비"})["content"]
            order = [
                text.index("너는 a다."),
                text.index("[위치 그래프"),
                text.index("[시간 인식]"),
                text.index("[몸 상태 인식]"),
                text.index("[Important Output Format]"),
            ]
            self.assertEqual(order, sorted(order))

    def test_legacy_scenario_gets_no_world_contract_and_legacy_move_to(self):
        # 위치 그래프·시간·감염이 전부 꺼진 옛 시나리오: 계약은 출력 스키마뿐이고
        # move_to 문구는 레거시(장소 이름)여야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            agents = self._agents(tmp)
            self._sim(tmp, agents, time_per_wave=0)
            self.assertEqual(agents["a"].engine_contract, "")
            text = agents["a"].get_system_message(["b"])["content"]
            self.assertNotIn("[위치 그래프", text)
            self.assertNotIn("[시간 인식]", text)
            self.assertNotIn("[몸 상태", text)
            self.assertIn("- move_to: 이동할 위치 이름", text)
            self.assertNotIn("만나러 갈 사람의 ID", text)

    def test_interview_path_never_inherits_the_contract(self):
        # 인터뷰는 cfg.system_prompt로 Agent를 새로 만들고 system 메시지를 통째로
        # 교체한다 — 출력 스키마도, 세계 계약도 붙지 않아야 한다.
        from ABM.agent import Agent
        from backend.api.simulation.interview import build_interview_system_prompt

        with tempfile.TemporaryDirectory() as tmp:
            fresh = Agent("a", "너는 a다.", tmp, token_limit=4096)
            self.assertEqual(fresh.engine_contract, "")

        text = build_interview_system_prompt("너는 a다.", "에이", "memory_only")
        for token in ("[Important Output Format]", '"update_appearance"',
                      "[위치 그래프", "[시간 인식]"):
            self.assertNotIn(token, text)


class EngineContractVerificationTests(unittest.TestCase):
    """시작 시 검증 어서션 — 활성 feature ⇒ 지시어 존재 (경고만, raise 금지)."""

    def _sim(self, tmp, agents, **kw):
        from ABM.simulation import Simulation
        return Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp, **kw,
        )

    GRAPH = [{"name": "안방", "connects_to": ["거실"], "zone": "집"},
             {"name": "거실", "connects_to": ["안방"], "zone": "집"}]

    def test_healthy_config_produces_no_warnings(self):
        from ABM.agent import Agent

        with tempfile.TemporaryDirectory() as tmp:
            agents = {"a": Agent("a", "너는 a다.", tmp, token_limit=8192)}
            sim = self._sim(
                tmp, agents, location_graph=self.GRAPH, time_per_wave=30,
                infection_model={"enabled": True, "disease_name": "독감"},
            )
            self.assertEqual(sim._verify_engine_contract(), [])

    def test_broken_override_is_warned_not_raised(self):
        # 사용자가 출력 스키마를 통째로 지운 오버라이드를 넣은 경우.
        from ABM.agent import Agent

        with tempfile.TemporaryDirectory() as tmp:
            agents = {"a": Agent(
                "a", "너는 a다.", tmp, token_limit=8192,
                output_format_template="\n\n[출력] 그냥 아무 말이나 하세요.\n",
            )}
            with self.assertLogs("ABM.simulation.core", level="WARNING") as cm:
                sim = self._sim(tmp, agents, location_graph=self.GRAPH)
            joined = "\n".join(cm.output)
            self.assertIn("계약 검증", joined)
            self.assertIn("move_to", joined)
            # raise 하지 않는다 — 시뮬레이션 객체는 정상적으로 만들어진다.
            self.assertIsNotNone(sim)
            problems = sim._verify_engine_contract()
            self.assertTrue(any("target" in p for p in problems))

    def test_legacy_frozen_override_passes_verification(self):
        # 옛 프리즈 템플릿을 들고 있는 시나리오도 경고 없이 통과해야 한다 —
        # 세계 계약(지도/시간/감염)은 이제 오버라이드와 무관하게 따로 주입된다.
        from ABM.agent import Agent

        with tempfile.TemporaryDirectory() as tmp:
            agents = {"a": Agent(
                "a", "너는 a다.", tmp, token_limit=8192,
                output_format_template=_LEGACY_FROZEN_TEMPLATE,
            )}
            sim = self._sim(
                tmp, agents, location_graph=self.GRAPH, time_per_wave=30,
                infection_model={"enabled": True, "disease_name": "독감"},
            )
            self.assertEqual(sim._verify_engine_contract(), [])

    def test_verify_contract_is_a_pure_function(self):
        from ABM.prompt_contract import verify_contract

        self.assertEqual(verify_contract("아무 것도 없음"), [
            p for p in verify_contract("아무 것도 없음")
        ])
        # 아무 feature도 켜지 않고 스키마도 요구하지 않으면 문제 없음.
        self.assertEqual(verify_contract("", include_output_schema=False), [])
        # 시간만 켜면 시간 지시어 하나만 문제로 잡힌다.
        problems = verify_contract("", time_enabled=True, include_output_schema=False)
        self.assertEqual(len(problems), 1)
        self.assertIn("[시간 인식]", problems[0])


class ZoneMeetHintTests(_MeetingSimHarness, unittest.TestCase):
    """[같은 구역의 다른 곳] 각 줄의 인라인 `→ 만나려면 move_to: "<ID>"` 힌트.

    정적 안내("move_to로 이동하세요")만으로는 모델이 장소명과 사람 ID 사이에서
    갈렸다. 지목해야 할 **정확한 입력값**을 그 사람 줄에 붙여준다.
    """

    def _situation(self, sim, key):
        known, strangers = sim._compute_wave_targets(key)
        return sim._build_situation_context(
            key, known, strangers, sim._compute_zone_awareness(key),
        )

    def _sim(self, tmp, locations, **kw):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in locations}
        return Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            agent_locations=dict(locations),
            location_graph=self.LOCATION_GRAPH, **kw,
        )

    def test_known_person_elsewhere_gets_inline_move_to_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, {"a": "X", "b": "Z"})
            text = self._situation(sim, "a")
            self.assertIn('- b (ID: "b") — Z  → 만나려면 move_to: "b"', text)

    def test_stranger_elsewhere_is_hinted_by_stranger_id_not_real_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, {"a": "X", "b": "Z"},
                            agent_groups={"a": ["가족"], "b": ["이웃"]})
            text = self._situation(sim, "a")
            self.assertIn('→ 만나려면 move_to: "stranger_1"', text)
            # 실명은 지목 수단으로 노출되지 않는다(_resolve_meet_target의 인지 규칙).
            self.assertNotIn('move_to: "b"', text)

    def test_existing_meeting_lock_suppresses_the_hint(self):
        # 이미 그 사람을 만나러 가는 중이면 "만나러 이동 중" 줄이 상태를 보여주므로
        # 같은 사람에 대한 힌트는 중복이라 붙이지 않는다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, {"a": "X", "b": "Z", "c": "W"})
            sim._meeting_intent["a"] = "b"
            text = self._situation(sim, "a")
            self.assertIn("을(를) 만나러 이동 중", text)
            self.assertNotIn('만나려면 move_to: "b"', text)
            # 다른 사람(c)에 대한 힌트는 그대로 남는다.
            self.assertIn('만나려면 move_to: "c"', text)


class _DirectorLLM:
    """system 에이전트(디렉터)와 일반 에이전트 응답을 함께 처리하는 스텁 LLM."""

    def __init__(self):
        self.agent_calls: list[tuple[str, str]] = []    # (system_text, user_text)
        self.director_calls: list[str] = []             # user_text

    def __call__(self, messages, max_tokens=None, **kw):
        sys_text  = messages[0].get("content", "") if messages else ""
        user_text = "\n".join(m.get("content", "") for m in messages[1:])
        if "[현재 Wave:" in user_text:
            self.director_calls.append(user_text)
            n = len(self.director_calls)
            return json.dumps({
                "interventions": [{"agent": "a", "message": f"디렉터 자극 {n}"}],
                "world_event":   None,
                "director_memo": "",
                "reason":        "테스트",
            }), "", {}
        self.agent_calls.append((sys_text, user_text))
        return json.dumps({
            "content": "...", "action_note": "", "target": "self",
            "move_to": None, "update_appearance": None,
        }), "", {}


class SystemAgentTimeAndOrderTests(unittest.TestCase):
    """디렉터(system 에이전트)의 실행 시점과 시각 주입.

    핵심 불변식 2가지:
      1. 디렉터는 wave **시작**에 돈다 — 개입이 그 wave에서 소비되므로 emit의
         `wave`, 반응 wave, 표시 시각이 셋 다 일치한다. 예전엔 루프 끝에서 돌아
         한 wave 어긋났다.
      2. 디렉터가 보는 시각은 에이전트가 보는 시각과 **글자 단위로 같다** —
         갈라지면 world_event가 "벽시계 8시"를 치는데 에이전트는 6:45를 본다.
    """

    def _run(self, tmp, *, waves=3, interval=1, threshold=3, **sim_kw):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        llm    = _DirectorLLM()
        agents = {"a": Agent("a", "너는 a다.", tmp, token_limit=8192)}
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=llm,
            system_agent={"enabled": True, "intervention_interval": interval,
                          "silence_threshold": threshold, "display_name": "내레이터"},
            **sim_kw,
        )
        emitted: list[tuple[str, dict]] = []
        sim._emit = lambda t, d: emitted.append((t, d))
        sim.run("a", max_waves=waves, step_delay=0.0, early_stop_enabled=False)
        return sim, llm, emitted

    def test_director_runs_before_wave_start_and_skips_wave_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, emitted = self._run(tmp, waves=3)

            seq = [(t, d.get("wave")) for t, d in emitted
                   if t in ("wave_start", "system_intervention")]
            self.assertEqual(
                seq,
                [("wave_start", 0),
                 ("system_intervention", 1), ("wave_start", 1),
                 ("system_intervention", 2), ("wave_start", 2)],
            )

    def test_intervention_is_consumed_in_the_same_wave(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, llm, _ = self._run(tmp, waves=3)

            # 에이전트는 wave마다 정확히 한 번 호출된다(a 혼자, 전원 재투입).
            self.assertEqual(len(llm.agent_calls), 3)
            self.assertNotIn("디렉터 자극", llm.agent_calls[0][1])   # wave 0
            self.assertIn("디렉터 자극 1", llm.agent_calls[1][1])    # wave 1
            self.assertIn("디렉터 자극 2", llm.agent_calls[2][1])    # wave 2

    def test_interval_selects_the_same_waves_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, emitted = self._run(tmp, waves=7, interval=3)
            waves = [d["wave"] for t, d in emitted if t == "system_intervention"]
            self.assertEqual(waves, [3, 6])

    def test_silence_threshold_keeps_its_meaning_after_the_reorder(self):
        """디렉터가 wave **시작**에 돌면 `_last_spoke_wave`는 wave_num-1까지만
        반영돼 있다(turn.py가 발화 후 갱신). 그래서 침묵 판정도 (wave_num-1)
        기준이어야 디렉터가 wave 끝에서 돌던 이전 배치와 임계값 의미가 같다.
        보정이 없으면 실효 임계값이 1 줄어, threshold=1에서 **직전 wave에 말한**
        에이전트도 침묵으로 잡힌다(M-1 회귀)."""
        with tempfile.TemporaryDirectory() as tmp:
            _, llm, _ = self._run(tmp, waves=4, interval=1, threshold=1)
            # a는 _DirectorLLM 상 매 wave 발화한다. threshold=1("직전 wave 미발화 =
            # 침묵")인데 a는 직전 wave에 늘 말했으므로 어떤 디렉터 호출에서도
            # 침묵 목록에 없어야 한다.
            self.assertTrue(llm.director_calls)
            for call in llm.director_calls:
                silent_block = call.split("[침묵 중인 에이전트")[1].split("[반복")[0]
                self.assertIn("없음", silent_block)
                self.assertNotIn('"a"', silent_block)

    def test_director_sees_the_same_clock_as_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, llm, _ = self._run(
                tmp, waves=3, sim_start_time="06:45", sim_start_weekday="wed",
                time_per_wave=30,
            )
            # wave 1 → 06:45 + 30분
            self.assertIn("[현재 시각]\n수요일 오전 7시 15분", llm.director_calls[0])
            self.assertIn("[현재 시각: 수요일 오전 7시 15분]", llm.agent_calls[1][1])
            # wave 2 → 06:45 + 60분
            self.assertIn("[현재 시각]\n수요일 오전 7시 45분", llm.director_calls[1])
            self.assertIn("[현재 시각: 수요일 오전 7시 45분]", llm.agent_calls[2][1])

    def test_time_section_is_omitted_when_time_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, llm, _ = self._run(tmp, waves=2, time_per_wave=0)
            self.assertTrue(llm.director_calls)
            for user_msg in llm.director_calls:
                # 섹션 헤더는 자기 줄을 차지한다. 규칙 문구 안의 "[현재 시각]"
                # 언급("위 [현재 시각]만을 참조")과 구분하려고 개행까지 본다.
                self.assertNotIn("[현재 시각]\n", user_msg)
                self.assertNotIn("오전", user_msg)

    def test_director_rules_forbid_inventing_clocks_and_physical_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, llm, _ = self._run(tmp, waves=2, time_per_wave=30)
            rules = llm.director_calls[0]
            self.assertIn("시각·시계·시간을 임의로 지어내지 말 것", rules)
            self.assertIn("world_event는 물리적 사실을 새로 만들지 않습니다", rules)
            self.assertIn("느껴진다", rules)

    def test_director_off_is_unaffected(self):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        with tempfile.TemporaryDirectory() as tmp:
            llm    = _DirectorLLM()
            agents = {"a": Agent("a", "너는 a다.", tmp, token_limit=8192)}
            sim = Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp, llm=llm,
            )
            emitted: list[tuple[str, dict]] = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim.run("a", max_waves=3, step_delay=0.0, early_stop_enabled=False)

            self.assertEqual(llm.director_calls, [])
            self.assertFalse([t for t, _ in emitted if t == "system_intervention"])


class DirectorRepetitionDetectionTests(unittest.TestCase):
    """디렉터의 반복 에이전트 판정 — content="..."(말 안 함)를 반복으로 오탐하지 않는다.

    과묵한 캐릭터는 매 턴 content="..."에 행동 묘사만 담는다. 예전엔 content만
    비교해 유사도 100%로 매 interval 반복 플래그가 떠, 디렉터가 그 한 명만
    "반복되는 생각에서 벗어나라"고 계속 붙잡았다. 이제 대사가 없으면 action_note로
    폴백해 "표현된 행동"이 실제로 도는지를 본다.
    """

    def _repetition_block(self, tmp, shared_log):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        llm    = _DirectorLLM()
        agents = {k: Agent(k, k, tmp, token_limit=8192) for k in ("a", "b")}
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp, llm=llm,
            system_agent={"enabled": True, "intervention_interval": 1,
                          "silence_threshold": 99, "display_name": "내레이터"},
        )
        sim._emit = lambda *a, **k: None
        # _last_spoke_wave 를 최근으로 맞춰 침묵 목록이 비게 한다 (반복만 보려고).
        sim._last_spoke_wave = {k: 9 for k in agents}
        sim.shared_log = shared_log
        sim._run_system_agent(10, {k: [] for k in agents})
        prompt = llm.director_calls[-1]
        return prompt.split("[반복 중인 에이전트")[1].split("[")[0]

    def _log(self, speaker, content, action_note, n=4):
        return [{"speaker": speaker, "content": content, "action_note": action_note}
                for _ in range(n)]

    def test_normalize_utterance_drops_pure_filler(self):
        from ABM.simulation._constants import _normalize_utterance
        for filler in ("...", "…", "", "   ", ". . .", "—", "ㆍㆍ"):
            self.assertIsNone(_normalize_utterance(filler, ""))
        # 필러 content + 실제 행동 → 행동으로 폴백
        self.assertEqual(_normalize_utterance("...", "천장을 본다"), "천장을 본다")
        # 최소한의 발성도 발화로 친다(필러 아님)
        self.assertEqual(_normalize_utterance("으으...", ""), "으으...")
        # 둘 다 비면 None
        self.assertIsNone(_normalize_utterance("", "   "))

    def test_silent_character_with_varied_actions_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = (
                [{"speaker": "a", "content": "...", "action_note": act}
                 for act in ("천장을 본다", "세수를 한다", "드레스룸으로 간다", "휴대폰을 본다")]
                + self._log("b", "빨리 준비해", "재촉한다")   # b 는 같은 대사 반복
            )
            block = self._repetition_block(tmp, log)
            self.assertNotIn('"a"', block)   # 회귀: 과묵 캐릭터는 안 잡힌다
            self.assertIn('"b"', block)      # 실제 대사 반복은 여전히 잡힌다

    def test_silent_character_repeating_one_action_is_still_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log("a", "...", "소파에 앉아 휴대폰을 본다")
            block = self._repetition_block(tmp, log)
            self.assertIn('"a"', block)   # 행동까지 똑같이 반복하면 진짜 정체 — 잡아야 한다

    def test_all_filler_history_scores_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log("a", "...", "")   # 대사도 행동도 없음
            block = self._repetition_block(tmp, log)
            self.assertNotIn('"a"', block)


# ── 계약 프리즈 중단 / 프리뷰 (backend/api/simulation) ─────────────────────────

_BACKGROUND = [{"role": "user", "content": "[배경] 테스트"}]

_ZONED_NODES = [
    {"name": "안방", "connects_to": ["거실"], "zone": "집"},
    {"name": "거실", "connects_to": ["안방"], "zone": "집"},
]


def _sim_cfg(**over) -> SimStartConfig:
    base = dict(
        agents=[AgentConfig(name="a", system_prompt="너는 a다."),
                AgentConfig(name="b", system_prompt="너는 b다.")],
        background="테스트 배경",
        start_agent="a",
    )
    base.update(over)
    return SimStartConfig(**base)


def _agents_like_lifecycle(cfg: SimStartConfig, tmp: str) -> dict:
    """lifecycle.py / load.py / resume.py 가 Agent 를 만드는 방식 그대로."""
    from ABM.agent import Agent
    return {
        a.name: Agent(
            a.name, a.system_prompt, tmp,
            token_limit=cfg.token_limit,
            extra_fields=[f.model_dump() for f in cfg.extra_fields],
            output_format_template=cfg.effective_output_format_override(),
        )
        for a in cfg.agents
    }


class ContractFreezeTests(unittest.TestCase):
    """C1 — 출력 계약은 더 이상 시나리오에 얼려 저장되지 않는다.

    프리즈의 실제 피해: 엔진에 기능을 추가해도(예: move_to 사람 지목 → 랑데부)
    기존 시나리오는 옛 지시어에 묶여 그 기능이 **조용히** 죽는다. 게다가 현재
    템플릿에는 `<MOVE_TO_HINT>` 자리표시자가 있어 그대로 프리즈하면 미치환
    문자열이 DB 에 들어간다.
    """

    def test_default_output_format_endpoint_returns_empty(self):
        from backend.api.simulation.scenarios import get_default_output_format

        body = get_default_output_format()
        self.assertEqual(body, {"template": ""})
        # 자리표시자·스키마가 스냅샷으로 새어 나가지 않는지
        self.assertNotIn("<MOVE_TO_HINT>", body["template"])
        self.assertNotIn("[Important Output Format]", body["template"])

    def test_override_is_forwarded_only_when_actually_set(self):
        self.assertIsNone(_sim_cfg().effective_output_format_override())
        self.assertIsNone(_sim_cfg(output_format_override="").effective_output_format_override())
        cfg = _sim_cfg(output_format_override="\n\n[출력] 커스텀\n")
        self.assertEqual(cfg.effective_output_format_override(), "\n\n[출력] 커스텀\n")

    def test_legacy_frozen_field_is_ignored_even_when_present(self):
        cfg = _sim_cfg(output_format_template=_LEGACY_FROZEN_TEMPLATE)
        self.assertIsNone(cfg.effective_output_format_override())
        # 새 오버라이드가 함께 있으면 그쪽이 유일한 진실
        both = _sim_cfg(output_format_template=_LEGACY_FROZEN_TEMPLATE,
                        output_format_override="\n\n[출력] 커스텀\n")
        self.assertEqual(both.effective_output_format_override(), "\n\n[출력] 커스텀\n")

    def test_config_without_the_new_field_still_parses(self):
        # DB 에 남아 있는 옛 config_json 은 output_format_override 키가 아예 없다.
        raw = json.loads(_sim_cfg(output_format_template=_LEGACY_FROZEN_TEMPLATE)
                         .model_dump_json())
        raw.pop("output_format_override")
        cfg = SimStartConfig(**raw)
        self.assertEqual(cfg.output_format_override, "")
        self.assertIsNone(cfg.effective_output_format_override())

    def test_loaded_legacy_scenario_gets_the_current_engine_contract(self):
        # 핵심 회귀: 옛 프리즈 문자열을 들고 있는 시나리오를 로드해도 그 값은
        # 무시되고, 엔진이 **현재 설정**으로 계약을 다시 만든다.
        from ABM.simulation import Simulation

        cfg = _sim_cfg(output_format_template=_LEGACY_FROZEN_TEMPLATE,
                       location_graph=_ZONED_NODES, time_per_wave=30)
        with tempfile.TemporaryDirectory() as tmp:
            agents = _agents_like_lifecycle(cfg, tmp)
            Simulation(agents, _BACKGROUND, tmp,
                       location_graph=[dict(n) for n in _ZONED_NODES],
                       time_per_wave=30)
            text = agents["a"].get_system_message(["b"], {"b": "비"})["content"]

        self.assertIn("만나러 갈 사람의 ID", text)
        self.assertIn("다음 발화에서 다른 장소나 다른 사람을 넣으면", text)  # 신규 문구
        self.assertNotIn("이동할 위치 이름, 또는", text)                    # 옛 프리즈 문구
        self.assertIn("[위치 그래프", text)
        self.assertIn("[시간 인식]", text)
        self.assertEqual(agents["a"].system_prompt, "너는 a다.")

    def test_explicit_override_still_wins_at_load_time(self):
        from ABM.simulation import Simulation

        cfg = _sim_cfg(output_format_override="\n\n[출력] 그냥 아무 말이나 하세요.\n",
                       location_graph=_ZONED_NODES, time_per_wave=30)
        with tempfile.TemporaryDirectory() as tmp:
            agents = _agents_like_lifecycle(cfg, tmp)
            Simulation(agents, _BACKGROUND, tmp,
                       location_graph=[dict(n) for n in _ZONED_NODES],
                       time_per_wave=30)
            text = agents["a"].get_system_message(["b"], {"b": "비"})["content"]

        self.assertIn("[출력] 그냥 아무 말이나 하세요.", text)
        self.assertNotIn("[Important Output Format]", text)
        # 오버라이드는 출력 계약만 대체한다 — 세계 계약은 계속 엔진 소유
        self.assertIn("[위치 그래프", text)
        self.assertIn("[시간 인식]", text)


class ScenarioContractPersistenceTests(unittest.TestCase):
    """C2 — 시나리오 CRUD/복제에서 계약 필드 왕복 정합성."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmpdir.name) / "memory.db"
        conn = get_db()
        init_tables(conn)
        migrate_db(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmpdir.cleanup()

    def _save(self, cfg, name="시나리오"):
        from backend.api.simulation.scenarios import create_scenario
        from backend.api.simulation.schemas import ScenarioSave
        return create_scenario(ScenarioSave(name=name, config=cfg))["id"]

    def _loaded(self, sid) -> SimStartConfig:
        from backend.api.simulation.scenarios import list_scenarios
        row = next(r for r in list_scenarios() if r["id"] == sid)
        return SimStartConfig(**row["config"])

    def test_new_scenario_saves_an_empty_output_format(self):
        # 프론트가 아직 옛 필드를 보내더라도 죽은 지시어를 저장하지 않는다.
        sid = self._save(_sim_cfg(output_format_template=_LEGACY_FROZEN_TEMPLATE))
        cfg = self._loaded(sid)
        self.assertEqual(cfg.output_format_template, "")
        self.assertEqual(cfg.output_format_override, "")
        self.assertIsNone(cfg.effective_output_format_override())

    def test_override_round_trips_through_save_load_clone_and_update(self):
        from backend.api.simulation.scenarios import update_scenario
        from backend.api.simulation.schemas import ScenarioSave

        override = "\n\n[출력] 커스텀 계약\n"
        sid = self._save(_sim_cfg(output_format_override=override,
                                  location_graph=_ZONED_NODES))
        loaded = self._loaded(sid)
        self.assertEqual(loaded.output_format_override, override)
        self.assertEqual([n.name for n in loaded.location_graph], ["안방", "거실"])

        # 복제 = 로드한 config 를 그대로 다시 저장
        clone_id = self._save(loaded, name="시나리오 (복사본)")
        self.assertNotEqual(clone_id, sid)
        self.assertEqual(self._loaded(clone_id).output_format_override, override)

        # 수정 — 오버라이드 해제하면 다시 엔진 생성 경로로 돌아온다
        update_scenario(sid, ScenarioSave(name="시나리오", config=_sim_cfg()))
        self.assertIsNone(self._loaded(sid).effective_output_format_override())


class ContractPreviewEndpointTests(unittest.TestCase):
    """C4 — POST /api/simulation/contract-preview (읽기 전용)."""

    def _preview(self, **kw):
        from backend.api.simulation.contract import preview_engine_contract
        from backend.api.simulation.schemas import ContractPreviewRequest
        return preview_engine_contract(ContractPreviewRequest(**kw))

    def test_preview_matches_what_the_engine_actually_injects(self):
        # 미리보기가 의미를 가지려면 실제 주입본과 **글자 단위로** 같아야 한다.
        # flat 타깃 경로와, 위치 그래프가 있을 때의 situation_targets 경로 둘 다.
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        infection = {"enabled": True, "disease_name": "독감"}
        res = self._preview(location_graph=_ZONED_NODES, time_per_wave=30,
                            infection_model=infection,
                            available_targets=["b"], key_to_alias={"b": "비"})
        # 위치 그래프가 있는 시나리오의 실제 <TARGETS>는 flat 목록이 아니라
        # "[현재 상황]에서 확인" 안내다 — 프론트가 그때 situation_targets=True를 보낸다.
        res_sit = self._preview(location_graph=_ZONED_NODES, time_per_wave=30,
                                infection_model=infection, situation_targets=True,
                                available_targets=["b"], key_to_alias={"b": "비"})

        with tempfile.TemporaryDirectory() as tmp:
            agents = {k: Agent(k, "너는 a다.", tmp, token_limit=8192,
                               extra_fields=_FIELDS) for k in ("a", "b")}
            Simulation(agents, _BACKGROUND, tmp,
                       location_graph=[dict(n) for n in _ZONED_NODES],
                       time_per_wave=30, infection_model=infection)
            injected = agents["a"].get_system_message(["b"], {"b": "비"})["content"]
            injected_sit = agents["a"].get_system_message(
                ["b"], {"b": "비"}, situation_targets=True)["content"]
            world = agents["a"].engine_contract

        self.assertEqual(injected, "너는 a다." + res.contract)
        self.assertEqual(injected_sit, "너는 a다." + res_sit.contract)
        self.assertNotEqual(res.contract, res_sit.contract)   # target 블록이 실제로 다르다
        self.assertEqual(res.world_contract, world)
        self.assertEqual(res.contract, res.world_contract + res.output_contract)
        self.assertEqual(res.warnings, [])

    def test_preview_reflects_feature_toggles(self):
        # time_per_wave 기본값은 SimStartConfig 와 같은 30 이므로, "아무 feature
        # 없음" 을 보려면 명시적으로 꺼야 한다 (프론트는 항상 폼 값을 보낸다).
        bare = self._preview(time_per_wave=0)
        self.assertEqual(bare.world_contract, "")
        self.assertIn("- move_to: 이동할 위치 이름", bare.contract)
        self.assertNotIn("만나러 갈 사람의 ID", bare.contract)
        self.assertEqual(
            bare.flags.model_dump(),
            {"has_location_graph": False, "has_zone": False, "time_enabled": False,
             "infection_enabled": False, "include_output_schema": True},
        )

        # 기본값(time_per_wave 생략)은 SimStartConfig 와 동일하게 시간 활성이다
        self.assertTrue(self._preview().flags.time_enabled)

        # variable 모드는 time_per_wave 가 0 이어도 시간 활성
        timed = self._preview(time_mode="variable")
        self.assertTrue(timed.flags.time_enabled)
        self.assertIn("[시간 인식]", timed.world_contract)

        flat = self._preview(location_graph=[{"name": "매장", "connects_to": []}])
        self.assertTrue(flat.flags.has_location_graph)
        self.assertFalse(flat.flags.has_zone)
        self.assertIn("만나러 갈 사람의 ID", flat.contract)

        zoned = self._preview(location_graph=_ZONED_NODES)
        self.assertTrue(zoned.flags.has_zone)
        self.assertIn("[구역: 집]", zoned.world_contract)

        sick = self._preview(infection_model={"enabled": True, "disease_name": "독감"})
        self.assertTrue(sick.flags.infection_enabled)
        self.assertIn("독감", sick.world_contract)

    def test_preview_renders_extra_fields_in_the_output_schema(self):
        res = self._preview(extra_fields=[{"name": "stress", "default": "0"}])
        self.assertIn('"stress"', res.output_contract)
        self.assertNotIn('"emotion"', res.output_contract)

    def test_preview_uses_a_placeholder_target_when_none_given(self):
        res = self._preview()
        self.assertIn('- ID: "agent_id"', res.output_contract)
        self.assertIn("표시 이름", res.output_contract)

    def test_preview_override_replaces_only_the_output_contract(self):
        override = "\n\n[출력] 그냥 아무 말이나 하세요.\n"
        res = self._preview(location_graph=_ZONED_NODES, time_per_wave=30,
                            output_format_override=override)
        self.assertEqual(res.output_contract, override)
        self.assertIn("[위치 그래프", res.world_contract)
        self.assertIn("[시간 인식]", res.world_contract)
        # 필수 지시어가 빠진 오버라이드는 경고로 잡아 준다(차단은 하지 않는다)
        self.assertTrue(res.warnings)
        self.assertTrue(any("target" in w for w in res.warnings))

    def test_preview_interview_carve_out_drops_the_output_schema(self):
        res = self._preview(location_graph=_ZONED_NODES, include_output_schema=False)
        self.assertEqual(res.output_contract, "")
        self.assertEqual(res.contract, res.world_contract)
        self.assertNotIn("[Important Output Format]", res.contract)
        self.assertEqual(res.warnings, [])

    def test_preview_has_no_side_effects_on_disk(self):
        # Simulation/Agent 를 만들지 않으므로 로그 파일도 DB 도 건드리지 않는다.
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self._preview(location_graph=_ZONED_NODES, time_per_wave=30)
            finally:
                os.chdir(old_cwd)
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
