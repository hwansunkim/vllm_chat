import ast
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
from backend.api import agents as agents_api
from backend.api import servers as servers_api
from backend.api.schemas import (
    AgentCreate, AgentUpdate, ChatMessage, ServerCreate, ServerUpdate,
)
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

    def test_jump_clamp_schema_defaults(self):
        cfg = SimStartConfig(agents=[_agent("a")], background="", start_agent="a")
        self.assertEqual(cfg.max_scene_jump_minutes, 45)
        self.assertEqual(cfg.max_daytime_jump_minutes, 180)
        cfg = SimStartConfig(agents=[_agent("a")], background="", start_agent="a",
                             max_scene_jump_minutes=0, max_daytime_jump_minutes=90)
        self.assertEqual(cfg.max_scene_jump_minutes, 0)
        self.assertEqual(cfg.max_daytime_jump_minutes, 90)


class VariableTimeJumpClampTests(unittest.TestCase):
    """가변 시간 점프의 결정론적 상한 (_RunnerMixin._clamp_time_jump).

    LLM 분류기는 '장면의 질감'만 정하고, 실제 경과 분의 상한은 엔진이 벽시계·
    동석 상황 기준으로 강제한다 — 약한 모델이 오후 한복판에서 최대 범위(예:
    480분)를 골라 학원·퇴근·저녁 식사 같은 재집결 장면을 통째로 건너뛰는 것을
    막는다. 3개 실제 run(gemma/o4-mini/solar) 중 2개에서 화요일 저녁이 8시간
    점프로 소실된 것이 계기.
    """

    def _sim(self, *, scene_cap=45, daytime_cap=180, start="14:00", elapsed=0):
        from ABM.agent import Agent
        from ABM.simulation import Simulation
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096)
                  for k in ("mom", "kid", "dad")}
        graph = [
            {"name": "livingroom", "connects_to": ["bedroom"],   "is_exterior": False, "zone": "home", "is_zone_entry": True},
            {"name": "bedroom",    "connects_to": ["livingroom"], "is_exterior": False, "zone": "home", "is_zone_entry": False},
            {"name": "office",     "connects_to": ["home"],       "is_exterior": True,  "zone": "",     "is_zone_entry": False},
            {"name": "school",     "connects_to": ["home"],       "is_exterior": True,  "zone": "",     "is_zone_entry": False},
        ]
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=lambda *a, **k: ("{}", "", {}),
            time_mode="variable", location_graph=graph,
            sim_start_time=start, elapsed_minutes_init=elapsed,
            max_scene_jump_minutes=scene_cap, max_daytime_jump_minutes=daytime_cap,
        )
        return sim

    def tearDown(self):
        if getattr(self, "_tmp", None):
            self._tmp.cleanup()

    @staticmethod
    def _spoke(*keys):
        return {k: {"success": True, "clean_content": "여보 밥 먹어요."} for k in keys}

    def test_co_located_interior_scene_is_hard_capped(self):
        sim = self._sim()
        sim._agent_location = {"mom": "livingroom", "kid": "livingroom", "dad": "office"}
        jump, reason = sim._clamp_time_jump(400, self._spoke("mom", "kid"))
        self.assertEqual(jump, 45)
        self.assertIsNotNone(reason)

    def test_lone_interior_speaker_daytime_is_capped(self):
        # 채민경 혼자 발코니, 아이들·아빠는 학교·회사 — 5.6-sol Wave 69 상황.
        sim = self._sim(start="14:46")
        sim._agent_location = {"mom": "livingroom", "kid": "school", "dad": "office"}
        jump, reason = sim._clamp_time_jump(470, self._spoke("mom"))
        self.assertEqual(jump, 180)
        self.assertIsNotNone(reason)

    def test_night_is_not_capped(self):
        sim = self._sim(start="23:10")
        sim._agent_location = {"mom": "livingroom", "kid": "bedroom", "dad": "bedroom"}
        jump, reason = sim._clamp_time_jump(400, self._spoke("mom"))
        self.assertEqual(jump, 400)
        self.assertIsNone(reason)

    def test_early_morning_is_not_capped(self):
        sim = self._sim(start="04:30")
        sim._agent_location = {"mom": "livingroom", "kid": "bedroom", "dad": "bedroom"}
        jump, reason = sim._clamp_time_jump(400, self._spoke("mom"))
        self.assertEqual(jump, 400)

    def test_fully_empty_house_daytime_is_not_capped(self):
        # 모두 외출 — 건너뛸 재집결 장면 자체가 없다.
        sim = self._sim(start="10:00")
        sim._agent_location = {"mom": "office", "kid": "school", "dad": "office"}
        jump, reason = sim._clamp_time_jump(400, self._spoke("mom", "dad"))
        self.assertEqual(jump, 400)
        self.assertIsNone(reason)

    def test_small_jump_is_never_touched(self):
        sim = self._sim()
        sim._agent_location = {"mom": "livingroom", "kid": "livingroom", "dad": "livingroom"}
        jump, reason = sim._clamp_time_jump(25, self._spoke("mom", "kid", "dad"))
        self.assertEqual(jump, 25)
        self.assertIsNone(reason)

    def test_scene_cap_zero_falls_through_to_daytime_cap(self):
        sim = self._sim(scene_cap=0, start="14:00")
        sim._agent_location = {"mom": "livingroom", "kid": "livingroom", "dad": "office"}
        jump, _ = sim._clamp_time_jump(400, self._spoke("mom", "kid"))
        self.assertEqual(jump, 180)  # 동석 캡은 꺼졌지만 주간 캡은 여전히 적용

    def test_both_caps_zero_disables_clamping(self):
        sim = self._sim(scene_cap=0, daytime_cap=0, start="14:00")
        sim._agent_location = {"mom": "livingroom", "kid": "livingroom", "dad": "office"}
        jump, reason = sim._clamp_time_jump(400, self._spoke("mom", "kid"))
        self.assertEqual(jump, 400)
        self.assertIsNone(reason)

    def test_silent_agents_do_not_count_as_a_scene(self):
        # kid가 같은 방에 있어도 이번 wave에 발화가 없으면 '진행 중 장면'이 아니다.
        sim = self._sim(start="14:00")
        sim._agent_location = {"mom": "livingroom", "kid": "livingroom", "dad": "office"}
        results = {
            "mom": {"success": True, "clean_content": "혼자 청소나 하자."},
            "kid": {"success": True, "clean_content": "   "},  # 내용 없음
        }
        jump, reason = sim._clamp_time_jump(400, results)
        self.assertEqual(jump, 180)  # 동석 아님 → 주간 캡만


class TimeJumpEndTimeStrTests(unittest.TestCase):
    """`time_jump` 이벤트의 `end_time_str` (ABM/simulation/runner.py).

    위치 이력 CSV의 `wave_end_time`은 원래 "다음 wave 턴 로그의 time_str"을
    훔쳐보는 방식이라 **마지막 wave가 늘 빈칸**이었다(다음 wave가 없으니까).
    엔진은 마지막 wave에 대해서도 경과분을 판정하므로, 그 delta를 적용한 뒤의
    절대 시각을 이벤트에 함께 실어 CSV가 다음 wave 없이도 종료 시각을 채우게 한다.

    불변식: `end_time_str == _format_time_str(_sim_start_minutes + 이번 wave까지
    누적된 _elapsed_minutes)`. runner에서 `self._elapsed_minutes += jump`는 emit
    **다음**에 실행되므로, emit 시점의 `_elapsed_minutes`에 `jump`를 더해야 한다
    — 이 순서를 뒤집으면 한 wave씩 밀린 시각이 나간다.
    """

    # 분류기가 늘 고르는 normal_scene 은 min==max 로 랜덤 요소를 없애 wave당
    # 경과분을 결정론적으로 만든다. 두 번째 카테고리는 고르지 않지만, ai 모드의
    # sanity 범위(전 카테고리의 min~max)를 넓혀 추론값이 clamp되지 않게 한다.
    CATS = [
        {"id": "normal_scene", "label": "일반 장면",
         "min_minutes": 12, "max_minutes": 12},
        {"id": "long_gap", "label": "긴 공백",
         "min_minutes": 1, "max_minutes": 480},
    ]

    # 기존 필드 — 회귀(필드 삭제/개명) 방지용.
    LEGACY_FIELDS = ("wave", "mode", "used_fallback", "category_id",
                     "category_label", "reason", "raw_minutes", "minutes",
                     "clamp_reason")

    def _llm(self, *, ai_minutes=None):
        """가짜 LLM — 에이전트 턴 / 카테고리 분류 / AI 분 추론 셋을 구분해 응답."""
        def llm(messages, max_tokens=None, **kw):
            sys_text = messages[0].get("content", "") if messages else ""
            if "시간 관찰자" in sys_text:
                if "분 단위 정수" in sys_text:      # estimate_wave_minutes (ai 모드)
                    if ai_minutes is None:
                        return "not json", "", {}   # AI 실패 → 카테고리 폴백
                    return json.dumps({"minutes": ai_minutes,
                                       "reason": "짧은 대화"}), "", {}
                return json.dumps({"category": "normal_scene",
                                   "reason": "t"}), "", {}
            return json.dumps({
                "content": "안녕하세요.", "action_note": "", "target": "all",
                "move_to": None, "update_appearance": None,
            }), "", {}
        return llm

    def _run(self, *, estimation_mode="category", ai_minutes=None,
             start="14:00", weekday="mon", elapsed_init=0, max_waves=3):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        with tempfile.TemporaryDirectory() as tmp:
            agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096)
                      for k in ("a", "b")}
            sim = Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
                llm=self._llm(ai_minutes=ai_minutes),
                time_mode="variable", time_categories=list(self.CATS),
                time_estimation_mode=estimation_mode,
                sim_start_time=start, sim_start_weekday=weekday,
                elapsed_minutes_init=elapsed_init,
            )
            emitted = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim.run("a", max_waves=max_waves, step_delay=0.0)
            jumps = [d for t, d in emitted if t == "time_jump"]
            return sim, jumps

    def _assert_invariant(self, sim, jumps, *, elapsed_init=0):
        """각 이벤트의 end_time_str이 '그 wave 적용 후' 시각과 일치하는지."""
        self.assertTrue(jumps, "time_jump 이벤트가 하나도 없음")
        elapsed = elapsed_init
        for ev in jumps:
            elapsed += ev["minutes"]
            self.assertEqual(
                ev["end_time_str"],
                sim._format_time_str(sim._sim_start_minutes + elapsed),
                f"wave {ev['wave']} 의 end_time_str 불일치",
            )
        # 마지막 이벤트는 run 종료 시점의 시계와 같아야 한다 — CSV 마지막 행이
        # 이 값을 쓰므로 여기서 어긋나면 빈칸이 '틀린 값'으로 바뀔 뿐이다.
        self.assertEqual(elapsed, sim._elapsed_minutes)
        self.assertEqual(
            jumps[-1]["end_time_str"],
            sim._format_time_str(sim._sim_start_minutes + sim._elapsed_minutes),
        )

    def test_category_mode_emits_end_time_str(self):
        sim, jumps = self._run(estimation_mode="category", max_waves=3)

        self._assert_invariant(sim, jumps)
        # 14:00 시작 + wave당 12분 → 2시 12분 / 2시 24분 / 2시 36분
        self.assertEqual([e["end_time_str"] for e in jumps],
                         ["월요일 오후 2시 12분",
                          "월요일 오후 2시 24분",
                          "월요일 오후 2시 36분"])

    def test_ai_mode_emits_end_time_str(self):
        sim, jumps = self._run(estimation_mode="ai", ai_minutes=7, max_waves=3)

        self.assertEqual([e["mode"] for e in jumps], ["ai"] * 3)
        self.assertEqual([e["minutes"] for e in jumps], [7, 7, 7])
        self._assert_invariant(sim, jumps)

    def test_ai_fallback_path_also_emits_end_time_str(self):
        # AI 추론 실패 → normal_scene 카테고리 폴백. 이 경로도 emit 지점은 같다.
        sim, jumps = self._run(estimation_mode="ai", ai_minutes=None, max_waves=2)

        self.assertTrue(all(e["used_fallback"] for e in jumps))
        self.assertTrue(all(e["category_id"] == "normal_scene" for e in jumps))
        self._assert_invariant(sim, jumps)

    def test_end_time_str_is_not_off_by_one_wave(self):
        # 회귀 방지: `_elapsed_minutes += jump` 뒤에 계산하거나(=한 wave 앞섬)
        # jump 를 빼먹으면(=한 wave 뒤처짐) 첫 이벤트가 시작 시각 그대로 나온다.
        sim, jumps = self._run(max_waves=2)
        self.assertNotEqual(jumps[0]["end_time_str"],
                            sim._format_time_str(sim._sim_start_minutes))

    def test_end_time_str_rolls_over_midnight(self):
        # 자정을 넘는 wave 도 요일까지 함께 넘어간 문자열이어야 한다.
        sim, jumps = self._run(start="23:50", weekday="mon", max_waves=2)

        self._assert_invariant(sim, jumps)
        self.assertEqual(jumps[0]["end_time_str"], "화요일 오전 0시 02분")

    def test_resumed_run_counts_from_restored_elapsed(self):
        # /continue 로 복원된 누적 경과(elapsed_minutes_init)가 기준점이어야 한다.
        sim, jumps = self._run(elapsed_init=100, max_waves=2)

        self._assert_invariant(sim, jumps, elapsed_init=100)
        self.assertEqual(jumps[0]["end_time_str"],
                         sim._format_time_str(sim._sim_start_minutes + 112))

    def test_legacy_fields_are_untouched(self):
        # end_time_str 추가로 기존 필드가 사라지거나 개명되지 않았는지.
        _, jumps = self._run(estimation_mode="category", max_waves=1)

        ev = jumps[0]
        for field in self.LEGACY_FIELDS:
            self.assertIn(field, ev)
        self.assertIn("end_time_str", ev)
        self.assertEqual(ev["mode"], "category")
        self.assertIs(ev["used_fallback"], False)
        self.assertEqual(ev["category_id"], "normal_scene")
        self.assertEqual(ev["category_label"], "일반 장면")
        self.assertEqual(ev["raw_minutes"], 12)
        self.assertEqual(ev["minutes"], 12)
        self.assertIsNone(ev["clamp_reason"])

    def test_time_jump_is_persisted_with_the_new_field(self):
        # DB 스키마 변경 없이 payload 추가만으로 영속화되는지 (data_json 그대로).
        from ABM.simulation.core import _PERSIST_EVENTS
        self.assertIn("time_jump", _PERSIST_EVENTS)


class TimeClassifierCurrentTimeTests(unittest.TestCase):
    """Layer 0 — 시간 분류기 프롬프트에 현재 시각 주입 (ABM/time_classifier.py)."""

    def _run(self, current_time):
        from ABM.time_classifier import classify_wave_time
        captured = {}
        def llm(messages, max_tokens=None, **kw):
            captured["user"] = messages[-1]["content"]
            return json.dumps({"category": "normal_scene", "reason": "t"}), "", {}
        classify_wave_time(
            [{"speaker": "a", "content": "안녕", "action_note": ""}],
            [{"id": "normal_scene", "label": "일반"}],
            llm, current_time=current_time,
        )
        return captured["user"]

    def test_current_time_block_is_present_when_given(self):
        user = self._run("화요일 오후 2시 46분")
        self.assertIn("[현재 시각]", user)
        self.assertIn("화요일 오후 2시 46분", user)

    def test_block_is_omitted_when_empty(self):
        self.assertNotIn("[현재 시각]", self._run(""))


class _ScriptedLLM:
    """에이전트별·호출순서별 응답을 미리 정해두는 스텁 LLM.

    script = {"a": [{"content":..., "target":..., "move_to":...,
                     "update_appearance":..., "action_note":...}, ...], ...}
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
            "action_note":       turn.get("action_note", ""),
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


class SpatialPerceptionTests(unittest.TestCase):
    """perception_mode="spatial" — 엿듣기 · 원거리 전달 · 독백 행동 관찰.

    옵트인 설정이다. 기본값 `"targeted"`는 기존 라우팅(타깃에게만 전달) 100%
    그대로이고, `"spatial"`일 때만 아래 규칙이 **추가**된다(화자의 이동 전 위치 기준):

      | 같은 방 · 직접 타깃      | 대사+행동 (기존과 동일)              |
      | 같은 방 · 제3자(엿듣기)  | 대사+행동, `[화자→대상들]` 태그       |
      | 같은 방 · 독백           | 행동만, 씬 채널                       |
      | 같은 zone 다른 방 · 타깃 | **대사만**, `[화자, 멀리서]` (행동 X) |
      | 같은 zone 다른 방 · 제3자| 없음 (zone 인지만 유지)               |
      | 다른 zone / exterior     | 없음                                  |

    가장 중요한 불변식은 첫 줄이다 — 기본 모드에서 이 로직이 **하나도** 발동하지
    않아야 한다. 대화 라우팅은 엔진의 코어 메커니즘이라 회귀 시 모든 시나리오가
    조용히 망가진다.
    """

    LOCATION_GRAPH = [
        {"name": "안방",   "connects_to": ["거실"],                 "zone": "우리집"},
        {"name": "거실",   "connects_to": ["안방", "부엌", "마당"], "zone": "우리집"},
        {"name": "부엌",   "connects_to": ["거실"],                 "zone": "우리집"},
        {"name": "마당",   "connects_to": ["거실"]},  # zone 미설정 — 벽이 그대로다
        {"name": "교실",   "connects_to": [],                       "zone": "학교"},
        {"name": "현관밖", "connects_to": ["마당"], "zone": "우리집", "is_exterior": True},
    ]

    def _make_sim(self, tmp, locations, script=None, **kw):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {
            key: Agent(key, f"너는 {key}다.", tmp, token_limit=4096)
            for key in locations
        }
        full_script = {k: [{"content": "...", "target": "self"}] for k in locations}
        full_script.update(script or {})
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM(full_script),
            agent_locations=dict(locations),
            location_graph=self.LOCATION_GRAPH,
            **kw,
        )
        sim._emit = lambda t, d: None
        return sim

    def _speak(self, sim, speaker="a"):
        """화자 한 명만 도는 wave. 다른 에이전트가 턴을 갖지 않으므로 stranger_N
        할당 순서가 결정적이다(관찰자별 태그 검증에 필요)."""
        sim.run(speaker, max_waves=1, step_delay=0.0, resume_wave={speaker: []})

    def _incoming(self, sim, agent_key):
        """_pending_wave에 쌓인 메시지를 실제 메모리 문자열로 포매팅.

        step.py `_inject_incoming`을 그대로 태워 최종 표시 형태까지 확인한다 —
        원거리 포맷이 step.py 변경 없이 성립한다는 게 설계의 핵심이라 여기서
        직접 검증한다.
        """
        msgs = sim._pending_wave.get(agent_key, [])
        return [m["content"] for m in sim._inject_incoming(sim.agents[agent_key], msgs)]

    # ── 회귀: 기본 모드(targeted) ────────────────────────────────────────────

    def test_targeted_mode_is_default_and_has_no_leakage(self):
        # 같은 방의 제3자(c)도, 같은 zone 다른 방(d)도 아무것도 받지 않는다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방", "c": "안방", "d": "거실"},
                {"a": [{"content": "밥 먹어.", "target": ["b"], "action_note": "상을 차린다"}]},
            )
            self.assertEqual(sim._perception_mode, "targeted")
            self.assertFalse(sim._is_remote_target("a", "d"))
            self.assertEqual(sim._resolve_targets(["d"], "a"), [])  # zone 완화 없음

            self._speak(sim)
            self.assertEqual(sim._pending_wave["b"], [{
                "speaker": "a", "content": "밥 먹어.", "action_note": "상을 차린다",
            }])
            self.assertIsNone(sim._pending_wave.get("c"))
            self.assertIsNone(sim._pending_wave.get("d"))

    def test_targeted_mode_monologue_action_is_not_broadcast(self):
        # 독백 행동 브로드캐스트도 기본 모드에서는 발동하지 않는다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방"},
                {"a": [{"content": "배고프네.", "target": "self", "action_note": "밥을 먹는다"}]},
            )
            self._speak(sim)
            # 아무도 받지 못해 next_wave가 비고 → 침묵 재투입(빈 리스트)만 남는다.
            self.assertEqual(sim._pending_wave.get("b", []), [])

    # ── 같은 방: 엿듣기 ──────────────────────────────────────────────────────

    def test_same_room_bystander_overhears_with_target_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방", "c": "안방"},
                {"a": [{"content": "밥 먹어.", "target": ["b"], "action_note": "상을 차린다"}]},
                perception_mode="spatial",
            )
            self._speak(sim)
            # 직접 타깃(b)은 기존과 글자 단위로 동일해야 한다.
            self.assertEqual(sim._pending_wave["b"], [{
                "speaker": "a", "content": "밥 먹어.", "action_note": "상을 차린다",
            }])
            # 제3자(c)는 누구에게 한 말인지 태그가 붙은 채로 대사+행동을 받는다.
            self.assertEqual(self._incoming(sim, "c"), ["[a→b] 밥 먹어.\n(상을 차린다)"])

    def test_eavesdrop_labels_are_per_observer_and_ignore_groups(self):
        # d는 다른 그룹이라 <TARGETS>에도 안 뜨고 "all"에도 안 잡히지만, 같은 방에
        # 물리적으로 있으므로 엿듣는다. 대신 a도 b도 모르므로 전부 stranger_N.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방", "c": "안방", "d": "안방"},
                {"a": [{"content": "밥 먹어.", "target": ["b"], "action_note": "상을 차린다"}]},
                perception_mode="spatial",
                agent_groups={"a": ["가족"], "b": ["가족"], "c": ["가족"], "d": ["이웃"]},
            )
            # 그룹 필터는 타깃 해석에만 걸린다 — d는 "all"의 후보조차 아니다.
            self.assertEqual(sorted(sim._resolve_targets(["all"], "a")), ["b", "c"])

            self._speak(sim)
            self.assertEqual(self._incoming(sim, "c"), ["[a→b] 밥 먹어.\n(상을 차린다)"])
            self.assertEqual(
                self._incoming(sim, "d"),
                ['[낯선 이(ID: "stranger_1")→낯선 이(ID: "stranger_2")] 밥 먹어.\n(상을 차린다)'],
            )

    def test_absent_named_target_is_still_overheard(self):
        # 부른 상대(b)가 이미 자리에 없어 resolved가 비어도, 원본 targets가 self가
        # 아니었으므로 "소리 내어 말한 것"이다 → 같은 방의 c는 듣는다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "마당", "c": "안방"},
                {"a": [{"content": "밥 먹어.", "target": ["b"], "action_note": "상을 차린다"}]},
                perception_mode="spatial",
            )
            self.assertEqual(sim._resolve_targets(["b"], "a"), [])  # 마당은 zone 밖
            self._speak(sim)
            self.assertIsNone(sim._pending_wave.get("b"))
            self.assertEqual(self._incoming(sim, "c"), ["[a→b] 밥 먹어.\n(상을 차린다)"])

    # ── 같은 zone, 다른 방: 원거리 ────────────────────────────────────────────

    def test_remote_direct_target_hears_speech_without_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "거실", "c": "거실", "d": "부엌"},
                {"a": [{"content": "밥 먹어.", "target": ["b"], "action_note": "상을 차린다"}]},
                perception_mode="spatial",
            )
            self.assertEqual(sim._resolve_targets(["b"], "a"), ["b"])
            self.assertTrue(sim._is_remote_target("a", "b"))

            self._speak(sim)
            self.assertEqual(sim._pending_wave["b"], [{
                "speaker": "a, 멀리서", "content": "밥 먹어.", "action_note": "",
            }])
            # step.py 변경 없이 원거리 포맷이 성립한다.
            self.assertEqual(self._incoming(sim, "b"), ["[a, 멀리서] 밥 먹어."])
            # 같은 zone 다른 방의 제3자에겐 대화 내용이 새지 않는다.
            self.assertIsNone(sim._pending_wave.get("c"))
            self.assertIsNone(sim._pending_wave.get("d"))

    def test_same_room_target_keeps_plain_format_in_spatial_mode(self):
        # 원거리 분기가 같은 방 타깃까지 오염시키지 않는지.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방"},
                {"a": [{"content": "밥 먹어.", "target": ["b"], "action_note": "상을 차린다"}]},
                perception_mode="spatial",
            )
            self.assertFalse(sim._is_remote_target("a", "b"))
            self._speak(sim)
            self.assertEqual(self._incoming(sim, "b"), ["[a] 밥 먹어.\n(상을 차린다)"])

    def test_all_and_group_targets_stay_room_local_in_spatial_mode(self):
        # zone 완화는 <key>/stranger_N 직접 타깃 전용이다. "모두"를 zone 전체로
        # 넓히면 방의 의미가 사라진다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방", "c": "거실"},
                perception_mode="spatial",
                agent_groups={"a": ["가족"], "b": ["가족"], "c": ["가족"]},
            )
            self.assertEqual(sim._resolve_targets(["all"], "a"), ["b"])
            self.assertEqual(sim._resolve_targets(["group:가족"], "a"), ["b"])
            # 직접 타깃만 원거리로 열린다.
            self.assertEqual(sim._resolve_targets(["c"], "a"), ["c"])

    # ── 격리: 다른 zone / exterior ───────────────────────────────────────────

    def test_other_zone_and_exterior_receive_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "교실", "c": "현관밖", "d": "마당"},
                {"a": [{"content": "밥 먹어.", "target": ["b", "c", "d"],
                        "action_note": "상을 차린다"}]},
                perception_mode="spatial",
            )
            self.assertEqual(sim._resolve_targets(["b", "c", "d"], "a"), [])
            self._speak(sim)
            # 아무에게도 안 갔으므로 침묵 재투입(전원 빈 리스트)만 남는다.
            self.assertTrue(all(not msgs for msgs in sim._pending_wave.values()))

    def test_exterior_speaker_delivers_nothing_even_to_same_place(self):
        # 외부 공간은 완전 격리 — 엿듣기·독백 관찰도 예외가 아니다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "현관밖", "b": "현관밖", "c": "현관밖"},
                {"a": [{"content": "밥 먹어.", "target": ["b"], "action_note": "상을 차린다"}]},
                perception_mode="spatial",
            )
            self.assertEqual(sim._resolve_targets(["b"], "a"), [])
            self._speak(sim)
            self.assertTrue(all(not msgs for msgs in sim._pending_wave.values()))

    # ── 독백: 행동만 씬으로 ──────────────────────────────────────────────────

    def test_monologue_broadcasts_action_only_as_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방", "c": "안방", "d": "거실"},
                {"a": [{"content": "배고프네.", "target": "self", "action_note": "밥을 먹는다"}]},
                perception_mode="spatial",
            )
            self._speak(sim)
            for observer in ("b", "c"):
                self.assertEqual(sim._pending_wave[observer], [{
                    "speaker": "씬", "content": "[씬] a: 밥을 먹는다", "action_note": "",
                }])
            # 혼잣말의 **대사**는 아무에게도 가지 않는다.
            self.assertNotIn("배고프네.", str(sim._pending_wave))
            # 같은 zone 다른 방에는 행동조차 보이지 않는다.
            self.assertEqual(sim._pending_wave.get("d", []), [])

    def test_monologue_action_is_anonymized_for_strangers(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방"},
                {"a": [{"content": "배고프네.", "target": "self", "action_note": "밥을 먹는다"}]},
                perception_mode="spatial",
                agent_groups={"a": ["가족"], "b": ["이웃"]},
            )
            self._speak(sim)
            self.assertEqual(
                [m["content"] for m in sim._pending_wave["b"]],
                ['[씬] 낯선 이(ID: "stranger_1"): 밥을 먹는다'],
            )

    def test_monologue_without_action_broadcasts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "안방"},
                {"a": [{"content": "배고프네.", "target": "self"}]},
                perception_mode="spatial",
            )
            self._speak(sim)
            self.assertEqual(sim._pending_wave.get("b", []), [])

    # ── 계약: 반환 타입 / 폴백 ────────────────────────────────────────────────

    def test_resolve_targets_return_type_is_unchanged_flat_list(self):
        # turn.py의 관계 그래프 edge 생성이 이 형태에 의존한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp,
                {"a": "안방", "b": "거실", "c": "안방"},
                {"a": [{"content": "밥 먹어.", "target": ["b"], "action_note": "상을 차린다"}]},
                perception_mode="spatial",
            )
            resolved = sim._resolve_targets(["b"], "a")
            self.assertIsInstance(resolved, list)
            self.assertTrue(all(isinstance(k, str) for k in resolved))

            self._speak(sim)
            # 원거리 타깃도 edge가 정상 생성된다(엿듣기는 edge를 만들지 않는다).
            self.assertEqual(
                [(e["source"], e["target"]) for e in sim.edges], [("a", "b")]
            )

    def test_invalid_perception_mode_falls_back_to_targeted(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._make_sim(
                tmp, {"a": "안방", "b": "거실"}, perception_mode="nonsense",
            )
            self.assertEqual(sim._perception_mode, "targeted")
            self.assertEqual(sim._resolve_targets(["b"], "a"), [])


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

    def test_resume_forwards_the_relationship_map(self):
        # /start 에선 관계 계약이 붙고 /resume 에선 조용히 사라지는 비일관을 막는다.
        cfg = self._cfg(agents=[
            AgentConfig(name="a", system_prompt="너는 a다.",
                        relationships={"b": "아내"}),
            AgentConfig(name="b", system_prompt="너는 b다."),
        ])
        _, calls = self._run_resume(self._run_row(cfg))
        self.assertEqual(calls["sim_kwargs"].get("agent_relationships"),
                         {"a": {"b": "아내"}, "b": {}})


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


class MeetingGoalNodeZoneTests(_MeetingSimHarness, unittest.TestCase):
    """추격 목표 노드(`_meeting_goal_node`)의 zone 이탈 판정.

    핵심 불변식: 추격자는 대상이 **자기 구역 밖으로** 나가는 길까지 따라나서면 안
    된다. zone은 인지 범위의 벽이고 추격은 인지 가능한 상대에게만 성립하므로
    (`_resolve_meet_target`), 대상이 구역을 벗어나는 순간 만남은 "가버렸다"로 끝나야
    한다. 그런데 "zone 탈출 1홉 엣지"(구역 안 어디서든 바깥 노드로 직행) 때문에 그
    이탈이 한 웨이브 만에 일어나고, 그 시점의 `_agent_location`은 아직 이동 적용
    **전**이라 `_meeting_break_reason()`의 zone 체크가 걸리지 못한다. 목표 계산
    쪽에서도 같은 기준으로 걸러야 추격자가 같은 웨이브에 함께 벽을 넘지 않는다.

    `is_exterior`(무한히 넓어 동석해도 서로 못 보는 공간)와는 완전히 별개의 조건이다.
    """

    # 안방 — 거실 — 주방(전부 zone "우리집", 입구는 거실) + zone 밖의 초등학교.
    # 초등학교는 exterior가 아니다 — 이 테스트가 겨누는 건 오직 zone 이탈이다.
    # 동네는 같은 zone 안의 exterior 노드 — 두 조건이 독립임을 확인하는 대조군.
    ZONED_GRAPH = [
        {"name": "거실",    "connects_to": ["안방", "주방"], "zone": "우리집", "is_zone_entry": True},
        {"name": "안방",    "connects_to": ["거실"],         "zone": "우리집"},
        {"name": "주방",    "connects_to": ["거실"],         "zone": "우리집"},
        {"name": "동네",    "connects_to": ["거실"],         "zone": "우리집", "is_exterior": True},
        {"name": "초등학교", "connects_to": ["우리집"]},
    ]

    # zone을 전혀 쓰지 않는 기존 시나리오(하위 호환 대조군).
    FLAT_GRAPH = [
        {"name": "X", "connects_to": ["M"]},
        {"name": "M", "connects_to": ["X", "N"]},
        {"name": "N", "connects_to": ["M"]},
    ]

    def _build(self, locations, graph):
        """실행하지 않고 상태만 세운 Simulation — 목표 노드 계산을 직접 본다."""
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        with tempfile.TemporaryDirectory() as tmp:
            agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096)
                      for k in locations}
            return Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
                llm=_ScriptedLLM({}), agent_locations=dict(locations),
                location_graph=graph,
            )

    def test_zone_escape_edge_exists(self):
        # 이 테스트들의 전제 — 구역 안 어디서든 바깥 노드로 1홉에 나갈 수 있다.
        sim = self._build({"mom": "안방"}, self.ZONED_GRAPH)
        self.assertEqual(sim._find_path("안방", "초등학교"), ["초등학교"])

    # ── (a) 같은 구역 안에서는 기존대로 최종 목적지를 쫓는다 ─────────────────────

    def test_goal_node_keeps_destination_inside_the_same_zone(self):
        sim = self._build({"son": "거실"}, self.ZONED_GRAPH)
        sim._agent_path["son"] = ["주방"]

        self.assertEqual(sim._meeting_goal_node("son"), "주방")

    def test_chase_inside_the_zone_still_targets_the_final_destination(self):
        # mom@안방이 son을 지목한 웨이브에 son이 거실→주방으로 움직인다. 같은 구역
        # 안이므로 mom은 stale 위치(거실)가 아니라 최종 목적지(주방)를 향해야 한다.
        sim, _ = self._run(
            {"mom": self._meet("son"),
             "son": [{"content": "주방 간다.", "target": "self", "move_to": "주방"}] + self.IDLE},
            {"mom": "안방", "son": "거실"}, max_waves=1, graph=self.ZONED_GRAPH,
        )

        self.assertEqual(sim._agent_location["son"], "주방")
        self.assertEqual(sim._agent_location["mom"], "거실")
        self.assertEqual(sim._agent_path["mom"], ["주방"])   # 거실이 아니라 주방까지
        self.assertEqual(sim._meeting_intent, {"mom": "son"})

    # ── (b) 구역 밖으로 나가는 목적지는 목표로 인정하지 않는다 (이번 회귀) ────────

    def test_goal_node_falls_back_when_target_leaves_its_zone(self):
        sim = self._build({"son": "거실"}, self.ZONED_GRAPH)
        sim._agent_path["son"] = ["초등학교"]

        # 초등학교는 exterior가 아니지만 zone 밖이다 → 아직 확정 목표로 보지 않는다.
        self.assertNotIn("초등학교", sim._exterior_locations)
        self.assertEqual(sim._meeting_goal_node("son"), "거실")

    def test_chaser_does_not_follow_the_target_out_of_the_zone(self):
        # 재현된 버그: mom@안방이 거실의 son을 만나러 가던 웨이브에 son이 zone 밖
        # 초등학교로 1홉 이동하자, mom도 안방→초등학교로 직행해 벽을 함께 넘었다.
        sim, _ = self._run(
            {"mom": self._meet("son"),
             "son": [{"content": "학교 간다.", "target": "self", "move_to": "초등학교"}] + self.IDLE},
            {"mom": "안방", "son": "거실"}, max_waves=1, graph=self.ZONED_GRAPH,
        )

        self.assertEqual(sim._agent_location["son"], "초등학교")
        self.assertEqual(sim._agent_location["mom"], "거실")   # son의 원래 자리까지만
        self.assertNotIn("초등학교", sim._agent_path.get("mom", []))

    def test_chase_out_of_the_zone_ends_as_gone_next_wave(self):
        # 한 웨이브 늦게, 실제로 나간 것이 확인되면 lock은 "가버렸다"로 풀린다.
        sim, emitted = self._run(
            {"mom": self._meet("son"),
             "son": [{"content": "학교 간다.", "target": "self", "move_to": "초등학교"}] + self.IDLE},
            {"mom": "안방", "son": "거실"}, max_waves=2, graph=self.ZONED_GRAPH,
        )

        self.assertNotEqual(sim._agent_location["mom"], "초등학교")
        self.assertEqual(sim._meeting_intent, {})
        self.assertEqual(self._flow(emitted, "mom"),
                         [("start", None), ("cancelled", "gone")])

    def test_exterior_check_is_independent_of_the_zone_check(self):
        # 동네는 같은 zone("우리집") 안이지만 exterior다 — zone 조건이 안 걸려도
        # 격리 공간 조건이 따로 걸러야 한다(두 조건은 별개의 이유로 존재한다).
        sim = self._build({"son": "거실"}, self.ZONED_GRAPH)
        sim._agent_path["son"] = ["동네"]

        self.assertEqual(sim._location_zone["동네"], sim._location_zone["거실"])
        self.assertEqual(sim._meeting_goal_node("son"), "거실")

    def test_target_already_outside_any_zone_keeps_its_destination(self):
        # 비교할 벽이 없는 경우(현재 위치에 zone이 없음)는 기존 동작 그대로.
        sim = self._build({"son": "초등학교"}, self.ZONED_GRAPH)
        sim._agent_path["son"] = ["거실"]

        self.assertEqual(sim._meeting_goal_node("son"), "거실")

    # ── (c) zone 미사용 시나리오는 기존 동작 100% 유지 ──────────────────────────

    def test_zoneless_scenario_is_untouched(self):
        sim = self._build({"a": "X", "b": "N"}, self.FLAT_GRAPH)
        self.assertEqual(sim._location_zone, {})   # zone 체크 자체가 무의미한 지도

        sim._agent_path["b"] = ["M"]
        self.assertEqual(sim._meeting_goal_node("b"), "M")
        sim._agent_path.pop("b")
        self.assertEqual(sim._meeting_goal_node("b"), "N")   # 이동 중이 아니면 현 위치

    def test_zoneless_chase_still_targets_the_final_destination(self):
        sim, _ = self._run(
            {"a": self._meet("b"),
             "b": [{"content": "저리 간다.", "target": "self", "move_to": "N"}] + self.IDLE},
            {"a": "X", "b": "M"}, max_waves=1, graph=self.FLAT_GRAPH,
        )

        self.assertEqual(sim._agent_location["b"], "N")
        self.assertEqual(sim._agent_location["a"], "M")
        self.assertEqual(sim._agent_path["a"], ["N"])   # M이 아니라 N까지


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


# ── 관계 지도 (AgentConfig.relationships) ──────────────────────────────────────
#
# 문제: 에이전트는 자기 정체("나는 아빠")는 알아도 **다른 에이전트가 자기에게
# 누구인지**를 모른다. 페르소나가 "딸"·"아내" 같은 역할어로만 쓰면 그 역할어가
# `target` 에 넣어야 할 어떤 ID 인지 바인딩되지 않는다. 그래서 관계를 프로즈가
# 아니라 **구조 데이터**로 받아(key = 그 사람 이름, 값 = 화자 시점의 관계어)
# 엔진이 계약 블록 · <TARGETS> 라벨 · 상황 컨텍스트 · knowledge 시드에 주입한다.
#
# 최상위 불변식: relationships 를 **쓰지 않는 시나리오는 한 글자도 달라지지
# 않는다** (`RelationshipOptOutTests`).

_REL_FAMILY = {
    "김봉남": {"채민경": "아내", "김미경": "큰딸"},
    "채민경": {"김봉남": "남편", "김미경": "큰딸"},
    "김미경": {"김봉남": "아빠", "채민경": "엄마"},
}


class RelationshipContractBuilderTests(unittest.TestCase):
    """`build_relationship_contract` 순수 함수."""

    def test_empty_map_renders_nothing(self):
        from ABM.prompt_contract import build_relationship_contract

        self.assertEqual(build_relationship_contract({}), "")
        self.assertEqual(build_relationship_contract({}, {"a": "에이"}), "")

    def test_renders_id_bound_lines_in_insertion_order(self):
        from ABM.prompt_contract import build_relationship_contract

        text = build_relationship_contract(_REL_FAMILY["김봉남"])
        self.assertIn("[아는 사람 (나와의 관계)]", text)
        self.assertIn("target 필드에 아래 ID를 씁니다", text)
        self.assertIn('  - 채민경 (ID: "채민경") — 당신의 아내', text)
        self.assertIn('  - 김미경 (ID: "김미경") — 당신의 큰딸', text)
        self.assertLess(text.index("채민경"), text.index("김미경"))

    def test_alias_supplies_the_display_name_key_stays_the_id(self):
        # key 는 언제나 시스템 ID 자리에, 표시 이름은 alias 가 있으면 그쪽을 쓴다.
        from ABM.prompt_contract import build_relationship_contract

        text = build_relationship_contract({"a": "아내"}, {"a": "채민경"})
        self.assertIn('  - 채민경 (ID: "a") — 당신의 아내', text)
        # alias 가 없으면 key 를 표시명 자리에 폴백
        self.assertIn('  - a (ID: "a") — 당신의 아내',
                      build_relationship_contract({"a": "아내"}, {}))
        self.assertIn('  - a (ID: "a") — 당신의 아내',
                      build_relationship_contract({"a": "아내"}, {"b": "비"}))

    def test_blank_relation_keeps_the_person_but_drops_the_suffix(self):
        # 관계어가 비어도 "이 사람을 안다"는 사실은 유효하다.
        from ABM.prompt_contract import build_relationship_contract

        text = build_relationship_contract({"a": "  "}, {"a": "에이"})
        self.assertIn('  - 에이 (ID: "a")', text)
        self.assertNotIn("당신의", text)

    def test_engine_contract_places_the_block_between_world_and_output(self):
        from ABM.prompt_contract import build_engine_contract

        text = build_engine_contract(
            extra_fields=_FIELDS, available_targets=["채민경"],
            location_graph=_GRAPH_ZONED, location_zone=_ZONES, time_enabled=True,
            relationships={"채민경": "아내"},
        )
        order = [
            text.index("[위치 그래프"),
            text.index("[시간 인식]"),
            text.index("[아는 사람 (나와의 관계)]"),
            text.index("[Important Output Format]"),
        ]
        self.assertEqual(order, sorted(order))
        # 같은 dict 가 <TARGETS> 라벨에도 쓰인다 — 한 번만 넘기면 두 자리에 반영.
        self.assertIn('- ID: "채민경"  (채민경 · 아내)', text)

    def test_interview_carve_out_still_drops_only_the_output_schema(self):
        from ABM.prompt_contract import build_engine_contract

        text = build_engine_contract(
            extra_fields=_FIELDS, time_enabled=True,
            relationships={"채민경": "아내"}, include_output_schema=False,
        )
        self.assertIn("[아는 사람 (나와의 관계)]", text)
        self.assertNotIn("[Important Output Format]", text)


class RelationshipTargetLabelTests(unittest.TestCase):
    """`<TARGETS>` 목록의 관계어 라벨 — `- ID: "채민경"  (채민경 · 아내)`."""

    def test_flat_targets_get_relationship_labels(self):
        from ABM.prompt_contract import build_output_contract

        text = build_output_contract(
            ["채민경", "김미경"], _FIELDS, {"채민경": "엄마"},
            speaker_relationships={"채민경": "아내", "김미경": "큰딸"},
        )
        # alias 가 있으면 표시명 자리에 alias, 없으면 key
        self.assertIn('- ID: "채민경"  (엄마 · 아내)', text)
        self.assertIn('- ID: "김미경"  (김미경 · 큰딸)', text)

    def test_sectioned_targets_get_relationship_labels(self):
        from ABM.prompt_contract import build_output_contract

        text = build_output_contract(
            [], _FIELDS, {"채민경": "엄마"},
            target_sections=[("아는 사람", ["채민경"]),
                             ("처음 보는 사람", ["stranger_1"])],
            speaker_relationships={"채민경": "아내"},
        )
        self.assertIn('- ID: "채민경"  (엄마 · 아내)', text)
        # 낯선 이 ID 는 관계 지도에 없으므로 라벨이 붙지 않는다.
        self.assertIn('- ID: "stranger_1"\n', text)

    def test_unrelated_targets_keep_the_plain_alias_label(self):
        from ABM.prompt_contract import build_output_contract

        text = build_output_contract(
            ["a", "b"], _FIELDS, {"a": "에이", "b": "비"},
            speaker_relationships={"a": "아내"},
        )
        self.assertIn('- ID: "a"  (에이 · 아내)', text)
        self.assertIn('- ID: "b"  (비)', text)      # 관계 없음 → 기존 포맷 그대로

    def test_no_relationships_is_byte_identical_to_the_old_render(self):
        from ABM.prompt_contract import build_output_contract

        base = build_output_contract(["a"], _FIELDS, {"a": "에이"})
        for empty in (None, {}):
            self.assertEqual(
                build_output_contract(["a"], _FIELDS, {"a": "에이"},
                                      speaker_relationships=empty),
                base,
            )


class RelationshipEngineWiringTests(unittest.TestCase):
    """Simulation 이 관계 지도를 소비하는 방식 (per-agent 계약 · knowledge · 상황)."""

    def _sim(self, tmp, *, keys=("김봉남", "채민경", "김미경"), rels=None, **kw):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=8192) for k in keys}
        sim = Simulation(
            agents, _BACKGROUND, tmp,
            agent_relationships=_REL_FAMILY if rels is None else rels, **kw,
        )
        return sim, agents

    def test_each_agent_gets_its_own_contract_block(self):
        # 관계는 화자 시점이라 공유 문자열 하나로는 표현할 수 없다.
        with tempfile.TemporaryDirectory() as tmp:
            _, agents = self._sim(tmp, time_per_wave=30)
            dad, mom = agents["김봉남"], agents["채민경"]
            self.assertNotEqual(dad.engine_contract, mom.engine_contract)
            self.assertIn('- 채민경 (ID: "채민경") — 당신의 아내', dad.engine_contract)
            self.assertIn('- 김봉남 (ID: "김봉남") — 당신의 남편', mom.engine_contract)
            # 공유분(세계 계약)은 그대로 양쪽에 동일하게 들어 있다.
            self.assertIn("[시간 인식]", dad.engine_contract)
            self.assertIn("[시간 인식]", mom.engine_contract)
            # 사용자 프롬프트는 여전히 오염되지 않는다.
            self.assertEqual(dad.system_prompt, "너는 김봉남다.")

    def test_display_names_come_from_name_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, agents = self._sim(
                tmp, keys=("dad", "mom"),
                rels={"dad": {"mom": "아내"}},
                name_aliases={"채민경": "mom"},   # {표시 이름: key}
            )
            self.assertIn('- 채민경 (ID: "mom") — 당신의 아내',
                          agents["dad"].engine_contract)

    def test_contract_is_replaced_not_accumulated(self):
        with tempfile.TemporaryDirectory() as tmp:
            from ABM.simulation import Simulation
            _, agents = self._sim(tmp)
            first = agents["김봉남"].engine_contract
            Simulation(agents, _BACKGROUND, tmp, agent_relationships=_REL_FAMILY)
            self.assertEqual(agents["김봉남"].engine_contract, first)
            self.assertEqual(first.count("[아는 사람 (나와의 관계)]"), 1)

    def test_targets_block_uses_the_speaker_relationships(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, agents = self._sim(tmp)
            dad = agents["김봉남"].get_system_message(["채민경"])["content"]
            mom = agents["채민경"].get_system_message(["김봉남"])["content"]
            self.assertIn('- ID: "채민경"  (채민경 · 아내)', dad)
            self.assertIn('- ID: "김봉남"  (김봉남 · 남편)', mom)

    def test_relationships_seed_mutual_knowledge_over_groups(self):
        # groups 로는 서로 모르는 사이인데 관계가 명시돼 있으면 아는 사이여야 한다.
        # (안 그러면 계약엔 "아내"라 써 놓고 같은 방에서 stranger_1 로 보인다.)
        with tempfile.TemporaryDirectory() as tmp:
            sim, _ = self._sim(
                tmp,
                agent_groups={"김봉남": ["집"], "채민경": ["직장"], "김미경": ["학교"]},
            )
            self.assertEqual(sim._agent_knowledge["김봉남"], {"채민경", "김미경"})
            self.assertEqual(sim._agent_knowledge["채민경"], {"김봉남", "김미경"})

    def test_groups_fallback_survives_when_relationships_are_absent(self):
        # relationships 없는 에이전트는 groups 규칙("groups 없으면 전원 known")대로.
        with tempfile.TemporaryDirectory() as tmp:
            sim, _ = self._sim(tmp, rels={"김봉남": {"채민경": "아내"}})
            self.assertEqual(sim._agent_knowledge["김미경"], {"김봉남", "채민경"})
            self.assertEqual(sim._agent_knowledge["김봉남"], {"채민경", "김미경"})

    def test_situation_context_labels_known_people(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim, _ = self._sim(
                tmp,
                agent_locations={"김봉남": "거실", "채민경": "거실", "김미경": "안방"},
                location_graph=[dict(n) for n in _ZONED_NODES],
            )
            known, strangers = sim._compute_wave_targets("김봉남")
            text = sim._build_situation_context("김봉남", known, strangers)
            self.assertIn('아는 사람: 채민경 (ID: "채민경", 아내)', text)

    def test_situation_context_is_unchanged_without_relationships(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim, _ = self._sim(
                tmp, rels={},
                agent_locations={"김봉남": "거실", "채민경": "거실", "김미경": "안방"},
                location_graph=[dict(n) for n in _ZONED_NODES],
            )
            known, strangers = sim._compute_wave_targets("김봉남")
            text = sim._build_situation_context("김봉남", known, strangers)
            self.assertIn('아는 사람: 채민경 (ID: "채민경")', text)

    def test_dangling_and_self_keys_are_dropped_with_a_warning(self):
        # 시나리오 편집기에서 에이전트 이름(key)을 바꾸면 남의 relationships 에
        # 옛 이름이 남는다. 존재하지 않는 ID 를 지목하라고 가르치면 안 되므로
        # 계약·knowledge 양쪽에서 빼되, raise 하지 않고 경고만 남긴다.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs("ABM.simulation.core", level="WARNING") as cm:
                sim, agents = self._sim(tmp, rels={
                    "김봉남": {"채민경": "아내", "없는사람": "유령", "김봉남": "나"},
                })
            joined = "\n".join(cm.output)
            self.assertIn("없는사람", joined)
            self.assertIn("자기 자신", joined)

            contract = agents["김봉남"].engine_contract
            self.assertIn('- 채민경 (ID: "채민경") — 당신의 아내', contract)
            self.assertNotIn("없는사람", contract)
            self.assertNotIn("유령", contract)
            self.assertEqual(sim._agent_relationships["김봉남"], {"채민경": "아내"})
            self.assertNotIn("없는사람", sim._agent_knowledge["김봉남"])
            problems = sim._verify_engine_contract()
            self.assertTrue(any("없는사람" in p for p in problems))

    def test_one_directional_relationship_warns_but_does_not_drop(self):
        # a→b 는 있는데 b→a 가 없으면 b 는 a 를 낯선 이로 본다. "각자 자기 시점"
        # 이라 오류는 아니지만 대개 config 실수라 경고만 낸다 (계약에서 빼지 않는다).
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs("ABM.simulation.core", level="WARNING") as cm:
                sim, agents = self._sim(tmp, rels={"김봉남": {"채민경": "아내"}})
            joined = "\n".join(cm.output)
            self.assertIn("김봉남→채민경", joined)
            self.assertIn("낯선 이", joined)
            # 관계는 그대로 살아 있다 — 경고일 뿐 제거 아님
            self.assertIn("당신의 아내", agents["김봉남"].engine_contract)
            self.assertEqual(sim._agent_relationships["김봉남"], {"채민경": "아내"})

    def test_symmetric_relationships_do_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim, _ = self._sim(tmp, rels={
                "김봉남": {"채민경": "아내"}, "채민경": {"김봉남": "남편"},
            })
            self.assertFalse([d for d in sim._dangling_relationships if "낯선 이" in d])

    def test_restored_knowledge_never_drops_the_relationship_seed(self):
        # /resume·/load 는 스냅샷의 knowledge 로 덮어쓴다. 저장된 run 의 시나리오에
        # 관계를 새로 추가한 뒤 재개하면, 계약엔 "아내"라고 쓰여 있는데 knowledge 엔
        # 없어 같은 방에서 stranger_N 으로 보이는 모순이 생긴다 — 관계는 config 사실
        # 이므로 복원 후에도 항상 known 이어야 한다.
        with tempfile.TemporaryDirectory() as tmp:
            sim, _ = self._sim(tmp, rels={"김봉남": {"채민경": "아내"}})
            sim.restore_agent_state({"김봉남": {"knowledge": ["김미경"]}})
            self.assertEqual(sim._agent_knowledge["김봉남"], {"김미경", "채민경"})

    def test_agent_config_schema_defaults_to_an_empty_map(self):
        from backend.api.simulation.schemas import AgentConfig

        self.assertEqual(AgentConfig(name="x", system_prompt="y").relationships, {})
        self.assertEqual(
            AgentConfig(name="x", system_prompt="y",
                        relationships={"b": "아내"}).relationships,
            {"b": "아내"},
        )


class RelationshipRestorePathTests(unittest.TestCase):
    """`/load` 도 관계 지도를 엔진에 넘긴다 (`/resume` 은 ResumeContinueWaveBaseTests).

    관계 계약은 저장되지 않고 실행 시점에 config 로부터 매번 새로 만들어진다 —
    지도/시간/감염 계약과 정확히 같은 원칙이다. 복원 경로가 이 인자를 빠뜨리면
    `/start` 로 시작한 시뮬레이션에만 관계가 붙고 `/load` 로 되살린 같은 시나리오는
    관계 없이 돌아간다(사용자는 알 방법이 없다).
    """

    def _load(self, cfg):
        from unittest import mock
        import ABM.agent as abm_agent
        import ABM.simulation as abm_simulation
        import ABM.db as abm_db
        import ABM.memory_compressor as abm_mc
        from backend.api.simulation.runtime import load as load_mod

        captured = {}

        class FakeAgent:
            def __init__(self, *a, **k):
                self.memory = []
                self._memory_block = None

        class FakeSim:
            def __init__(self, *a, **k):
                captured.update(k)
                self.agents            = {}
                self.background_log    = []
                self.shared_log        = []
                self._pending_wave     = None
                self._agent_infection  = {}
            def restore_agent_state(self, s): pass

        class FakeDB:
            def get_run(self, rid):
                return {"config_json": cfg.model_dump_json(), "start_wave": 0,
                        "total_waves": 0, "scenario_id": "scn",
                        "scenario_name": "시나리오", "active_agents_json": None,
                        "pending_wave_json": None, "elapsed_minutes": 0}
            def get_agent_snapshots(self, rid): return {}
            def get_agent_states(self, rid):    return {}
            def get_run_log(self, rid):         return []

        sim_runtime._sim["status"] = "idle"
        with mock.patch.object(load_mod, "get_sim_db", lambda: FakeDB()), \
             mock.patch.object(load_mod, "_make_llm", lambda *a, **k: None), \
             mock.patch.object(load_mod, "_make_agent_llm_map", lambda *a, **k: {}), \
             mock.patch.object(abm_agent, "Agent", FakeAgent), \
             mock.patch.object(abm_simulation, "Simulation", FakeSim), \
             mock.patch.object(abm_db, "SimDB", lambda *a, **k: None), \
             mock.patch.object(abm_mc, "build_memory_block", lambda *a, **k: None):
            resp = load_mod.load_simulation("prev-run")
        return resp, captured

    def test_load_forwards_the_relationship_map(self):
        cfg = SimStartConfig(
            agents=[AgentConfig(name="a", system_prompt="너는 a다.",
                                relationships={"b": "아내"}),
                    AgentConfig(name="b", system_prompt="너는 b다.")],
            background="테스트", start_agent="a",
        )
        resp, cap = self._load(cfg)
        self.assertEqual(resp["status"], "loaded")
        self.assertEqual(cap.get("agent_relationships"), {"a": {"b": "아내"}, "b": {}})

    def test_load_without_relationships_forwards_empty_maps(self):
        cfg = SimStartConfig(
            agents=[AgentConfig(name="a", system_prompt="너는 a다.")],
            background="테스트", start_agent="a",
        )
        _, cap = self._load(cfg)
        self.assertEqual(cap.get("agent_relationships"), {"a": {}})


class RelationshipOptOutTests(unittest.TestCase):
    """**relationships 를 쓰지 않는 시나리오는 완전히 불변**이어야 한다.

    관계 기능은 opt-in 이다. 필드를 비워 둔 시나리오에서 계약 문자열이 한 글자라도
    달라지면 프리즈 템플릿 비교·골든 파일·기존 프롬프트 튜닝이 전부 흔들린다.
    """

    def _contracts(self, tmp, **kw):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=8192,
                           extra_fields=_FIELDS) for k in ("a", "b")}
        Simulation(agents, _BACKGROUND, tmp, **kw)
        return {k: (ag.engine_contract,
                    ag.get_system_message(["b"], {"b": "비"})["content"])
                for k, ag in agents.items()}

    def test_omitted_and_empty_relationships_render_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            omitted = self._contracts(tmp, location_graph=[dict(n) for n in _ZONED_NODES],
                                      time_per_wave=30)
            empty   = self._contracts(tmp, location_graph=[dict(n) for n in _ZONED_NODES],
                                      time_per_wave=30, agent_relationships={})
            per_agent_empty = self._contracts(
                tmp, location_graph=[dict(n) for n in _ZONED_NODES],
                time_per_wave=30, agent_relationships={"a": {}, "b": {}},
            )
        self.assertEqual(omitted, empty)
        self.assertEqual(omitted, per_agent_empty)

    def test_no_relationship_block_appears_anywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._contracts(tmp, time_per_wave=30)
        for contract, assembled in out.values():
            self.assertNotIn("[아는 사람 (나와의 관계)]", contract)
            self.assertNotIn("[아는 사람 (나와의 관계)]", assembled)
            self.assertNotIn(" · ", assembled.split("[Important Output Format]")[-1])
        # 모든 에이전트가 같은 공유 계약을 받는다(= 예전 동작).
        self.assertEqual(out["a"][0], out["b"][0])

    def test_agent_relationships_defaults_to_empty(self):
        from ABM.agent import Agent

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(Agent("a", "너는 a다.", tmp).relationships, {})


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


class DirectorRecentActivityTests(unittest.TestCase):
    """디렉터가 요약 없이도 최근 활동을 직접 읽는다 (D1).

    어휘 유사도(`_repetition_score`)는 축자 반복만 잡는다. 표현을 바꿔가며 같은
    화제를 맴도는 주제 반복은 디렉터가 `[최근 활동]` 다이제스트를 보고 판단한다.
    """

    def test_digest_groups_by_wave_and_falls_back_to_action(self):
        from ABM.simulation._constants import _recent_activity_digest
        log = [
            {"wave": 1, "speaker": "a", "content": "옛날 발화", "action_note": ""},
            {"wave": 5, "speaker": "a", "content": "...", "action_note": "천장을 본다"},
            {"wave": 5, "speaker": "b", "content": "일어나", "action_note": ""},
            {"wave": 6, "speaker": "b", "content": "밥 먹어", "action_note": ""},
        ]
        out = _recent_activity_digest(log, {"a": "김", "b": "이"}, waves=2)
        self.assertIn("— Wave 5 —", out)
        self.assertIn("— Wave 6 —", out)
        self.assertNotIn("옛날 발화", out)          # window 밖
        self.assertIn("김: (천장을 본다)", out)      # 필러 → 행동 폴백
        self.assertIn("이: 일어나", out)
        self.assertEqual(_recent_activity_digest([], {}), "")

    def test_digest_is_line_capped_keeping_the_most_recent(self):
        # digest_waves 를 크게 잡아도 총 라인은 max_lines 로 캡되고 최근이 남는다.
        from ABM.simulation._constants import _recent_activity_digest
        log = [
            {"wave": w, "speaker": "a", "content": f"발화 w{w}", "action_note": ""}
            for w in range(1, 41)
        ]
        out = _recent_activity_digest(log, {}, waves=40, max_lines=10)
        lines = out.splitlines()
        self.assertLessEqual(len(lines), 11)          # +1 = "(이전 wave 생략)" 헤더
        self.assertIn("발화 w40", out)                # 최근은 남는다
        self.assertNotIn("발화 w1", out)              # 오래된 건 잘린다
        self.assertTrue(lines[0].startswith("— "))    # 첫 줄은 항상 헤더

    def test_director_prompt_includes_recent_activity_and_threshold(self):
        from ABM.agent import Agent
        from ABM.simulation import Simulation
        from ABM.simulation._constants import _REPEAT_THRESHOLD

        with tempfile.TemporaryDirectory() as tmp:
            llm    = _DirectorLLM()
            agents = {k: Agent(k, k, tmp, token_limit=8192) for k in ("a", "b")}
            sim = Simulation(
                agents, [{"role": "user", "content": "[배경] 테스트"}], tmp, llm=llm,
                system_agent={"enabled": True, "intervention_interval": 1,
                              "silence_threshold": 99, "display_name": "내레이터"},
            )
            sim._emit = lambda *a, **k: None
            sim._last_spoke_wave = {k: 9 for k in agents}
            # 주제 반복: 매 wave 표현을 바꿔 같은 욕구(배고픔·점심)를 맴돈다.
            # 어휘 유사도는 낮아 [반복 중인 에이전트]에는 안 잡힌다.
            sim.shared_log = [
                {"wave": w, "speaker": "a", "content": c, "action_note": ""}
                for w, c in [
                    (7, "언제 점심시간이야? 배고파 죽겠어, 오늘 급식 뭐지?"),
                    (8, "이제 조금만 있으면 점심이다! 배고파서 쓰러질 것 같아"),
                    (9, "10분 남았다! 급식 메뉴 진짜 궁금해, 빨리 종 쳐라"),
                ]
            ]
            sim._run_system_agent(10, {k: [] for k in agents})
            prompt = llm.director_calls[-1]

            self.assertIn("[최근 활동", prompt)
            self.assertIn("— Wave 9 —", prompt)
            self.assertIn("10분 남았다", prompt)
            # 어휘 반복은 안 잡혔다 — 디렉터는 [최근 활동]으로만 알 수 있다
            rep = prompt.split("[반복 중인 에이전트")[1].split("[최근 활동")[0]
            self.assertIn("없음", rep)
            # 반복 임계값 % 는 상수에서 온다 (하드코딩 제거)
            self.assertIn(f"{int(_REPEAT_THRESHOLD * 100)}%", prompt)


class DirectorViewAndCostTests(unittest.TestCase):
    """`system_agent.digest_waves` (디렉터 시야) + `director_call` 관측 이벤트.

    요약(summary_interval)을 제거하고, 디렉터가 되짚는 창을 직접 조절 가능하게 한
    변경. 창을 키우며 비용(prompt_tokens/elapsed_ms)을 보고 조절하라는 취지라
    director_call 이벤트가 **개입 여부와 무관하게** 매 디렉터 호출에서 나와야 한다.
    """

    def _sim(self, tmp, *, digest_waves=None, director_result=None):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        res = director_result if director_result is not None else {
            "interventions": [], "world_event": None, "director_memo": "", "reason": "x",
        }

        def llm(messages, max_tokens=None, **kw):
            user = "\n".join(m.get("content", "") for m in messages[1:])
            if "[현재 Wave:" in user:
                return json.dumps(res), "", {"prompt_tokens": 1234}
            return json.dumps({"content": "네.", "action_note": "", "target": "self",
                               "move_to": None, "update_appearance": None}), "", {}

        sa = {"enabled": True, "intervention_interval": 1, "silence_threshold": 3,
              "display_name": "내레이터"}
        if digest_waves is not None:
            sa["digest_waves"] = digest_waves
        agents = {"a": Agent("a", "너는 a다.", tmp, token_limit=8192)}
        sim = Simulation(agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
                         llm=llm, system_agent=sa)
        return sim

    def test_digest_waves_is_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._sim(tmp, digest_waves=100)._sys_digest_waves, 20)
            self.assertEqual(self._sim(tmp, digest_waves=0)._sys_digest_waves, 2)
            self.assertEqual(self._sim(tmp, digest_waves=8)._sys_digest_waves, 8)

    def test_digest_waves_defaults_to_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._sim(tmp)._sys_digest_waves, 6)

    def test_director_call_event_fires_even_without_intervention(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, digest_waves=9)          # 개입 없는 기본 result
            emitted: list[tuple[str, dict]] = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim._run_system_agent(4, {"a": []})

            calls = [d for t, d in emitted if t == "director_call"]
            self.assertEqual(len(calls), 1)
            c = calls[0]
            self.assertEqual(c["wave"], 4)
            self.assertEqual(c["digest_waves"], 9)
            self.assertEqual(c["prompt_tokens"], 1234)
            self.assertIsInstance(c["prompt_chars"], int)
            self.assertIsInstance(c["elapsed_ms"], int)
            self.assertFalse(c["intervened"])
            self.assertFalse(c["failed"])
            # 개입/세계사건 이벤트는 없어야 한다
            self.assertNotIn("system_intervention", [t for t, _ in emitted])

    def test_director_call_reports_intervention_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp, director_result={
                "interventions": [{"agent": "a", "message": "일어나"}],
                "world_event": {"content": "종이 울린다", "targets": ["all"]},
                "director_memo": "", "reason": "x",
            })
            emitted: list[tuple[str, dict]] = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim._run_system_agent(4, {"a": []})
            c = next(d for t, d in emitted if t == "director_call")
            self.assertTrue(c["intervened"])
            self.assertEqual(c["n_interventions"], 1)
            self.assertTrue(c["world_event"])

        with tempfile.TemporaryDirectory() as tmp:
            sim = self._sim(tmp)
            # 디렉터가 깨진 응답 → run_system_agent 가 None
            sim._llm = lambda *a, **k: ("not json", "", {})
            emitted = []
            sim._emit = lambda t, d: emitted.append((t, d))
            sim._run_system_agent(4, {"a": []})
            c = next(d for t, d in emitted if t == "director_call")
            self.assertTrue(c["failed"])
            self.assertFalse(c["intervened"])

    def test_summary_interval_in_old_config_is_ignored_not_rejected(self):
        # 구 시나리오 JSON 은 summary_interval 을 들고 있다 — pydantic extra=ignore.
        cfg = SimStartConfig(agents=[_agent("a")], background="", start_agent="a",
                             summary_interval=5)
        self.assertFalse(hasattr(cfg, "summary_interval"))


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

    def test_preview_renders_relationships_and_matches_the_injected_contract(self):
        # 관계 지도는 per-agent 라 프리뷰도 "지금 편집 중인 에이전트 한 명"의 시점을
        # 그린다. world_contract 조각은 그 에이전트의 engine_contract 와 같아야 한다.
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        rels = {"b": "아내"}
        res = self._preview(time_per_wave=30, relationships=rels,
                            available_targets=["b"], key_to_alias={"b": "비"})
        self.assertIn('- 비 (ID: "b") — 당신의 아내', res.world_contract)
        self.assertIn('- ID: "b"  (비 · 아내)', res.output_contract)
        self.assertEqual(res.contract, res.world_contract + res.output_contract)
        self.assertEqual(res.warnings, [])

        with tempfile.TemporaryDirectory() as tmp:
            agents = {k: Agent(k, "너는 a다.", tmp, token_limit=8192,
                               extra_fields=_FIELDS) for k in ("a", "b")}
            Simulation(agents, _BACKGROUND, tmp, time_per_wave=30,
                       name_aliases={"비": "b"},
                       agent_relationships={"a": rels})
            injected = agents["a"].get_system_message(["b"], {"b": "비"})["content"]
            self.assertEqual(agents["a"].engine_contract, res.world_contract)

        self.assertEqual(injected, "너는 a다." + res.contract)

    def test_preview_without_relationships_is_unchanged(self):
        base = self._preview(time_per_wave=30, available_targets=["b"],
                             key_to_alias={"b": "비"})
        empty = self._preview(time_per_wave=30, relationships={},
                              available_targets=["b"], key_to_alias={"b": "비"})
        self.assertEqual(base.contract, empty.contract)
        self.assertNotIn("[아는 사람 (나와의 관계)]", base.contract)

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




# ── 헤드리스 러너 + 마크다운 내보내기 (ABM/simulation/headless.py, ABM/export) ──
#
# 골든 시나리오(tests/fixtures/golden_scenario.json)는 5 wave 안에서 씬 이벤트
# 타입이 최소 1건씩 나오도록 짜여 있고, **매 wave 발화자가 정확히 1명**이라
# 스레드 완료 순서에 흔들리지 않는다(2명이 같은 wave에 발화하면 shared_log 삽입
# 순서가 경쟁 상태가 되어 골든 파일이 간헐적으로 깨진다). 시나리오를 손보려면
# 이 불변식을 먼저 확인할 것.

_FIXTURES = Path(__file__).parent / "fixtures"


class _GoldenLLM:
    """골든 시나리오용 스텁 LLM — 에이전트 턴 · 디렉터 · 시간 분류를 처리한다.

    `_ScriptedLLM`과 달리 emotion/action 까지 스크립트로 고정해 마크다운의
    meta 라인(`😊 happy · move`)과 자동 아이콘 합성까지 골든에 걸린다.
    """

    SCRIPT = {
        "a": [
            {"content": "나린아, 일어났어?", "action_note": "부엌 쪽을 바라보며",
             "target": "b", "emotion": "neutral", "action": "speak"},
            {"content": "따라가 볼까.", "action_note": "슬리퍼를 끌며",
             "target": "self", "emotion": "happy", "action": "move",
             "move_to": "b", "update_appearance": "회색 후드티 위에 남색 담요를 둘렀다"},
            {"content": "나 잠깐 나갔다 올게.", "action_note": "현관문을 열며",
             "target": "self", "emotion": "fear", "action": "leave", "move_to": "현관"},
        ],
        "b": [
            {"content": "응, 지금 일어나. 부엌에 뭐 좀 가지러 갈게.", "action_note": "이불을 개며",
             "target": "a", "emotion": "sad", "action": "move", "move_to": "부엌"},
            {"content": "왜 따라와.", "action_note": "냄비를 내려놓으며",
             "target": "a", "emotion": "angry", "action": "speak"},
        ],
    }

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.director_calls = 0

    def __call__(self, messages, max_tokens=None, **kw):
        sys_text  = messages[0].get("content", "") if messages else ""
        user_text = "\n".join(m.get("content", "") for m in messages[1:])
        if "시간 관찰자" in sys_text:                       # time_classifier
            return json.dumps({"category": "normal_scene", "reason": "t"}), "", {}
        if "[현재 Wave:" in user_text:                      # system agent (디렉터)
            self.director_calls += 1
            return json.dumps({
                "interventions": [{"agent": "b", "message": "창밖에서 자동차 경적이 길게 울린다."}],
                # targets 를 b 로 좁혀야 a 가 이 wave 에 끌려 들어오지 않는다
                # (= wave 당 발화자 1명 불변식 유지).
                "world_event":   {"content": "복도에서 이삿짐 나르는 소리가 크게 들려온다.",
                                  "targets": ["b"]},
                "director_memo": "",
                "reason":        "정적을 깨기 위해",
            }), "", {}
        key = next(k for k in self.SCRIPT if f"너는 {k}다." in sys_text)
        idx = self.calls.get(key, 0)
        self.calls[key] = idx + 1
        turns = self.SCRIPT[key]
        turn  = turns[idx] if idx < len(turns) else turns[-1]
        return json.dumps({
            "content":           turn.get("content", "..."),
            "action_note":       turn.get("action_note", ""),
            "emotion":           turn.get("emotion", "neutral"),
            "action":            turn.get("action", "speak"),
            "target":            turn.get("target", "self"),
            "move_to":           turn.get("move_to"),
            "update_appearance": turn.get("update_appearance"),
        }), "", {}


def _golden_config():
    raw = json.loads((_FIXTURES / "golden_scenario.json").read_text(encoding="utf-8"))
    return SimStartConfig(**raw["config"]), raw["name"]


def _run_golden(db=None, sim_id=None):
    from ABM.simulation.headless import run_config
    with tempfile.TemporaryDirectory() as tmp:
        cfg, name = _golden_config()
        result = run_config(cfg, llm=_GoldenLLM(), log_dir=tmp, db=db, sim_id=sim_id)
    return cfg, name, result


# 골든 마크다운의 헤더에 박히는 고정 시각(현지 시각으로 포매팅된다).
_GOLDEN_STARTED = 1_700_000_000.0
_GOLDEN_ENDED   = 1_700_003_600.0
_GOLDEN_NOW     = 1_700_007_200.0
_ALL_TOGGLES = {"time", "action", "move", "appearance", "world", "intervention",
                "infection", "meeting"}


def _render_golden(cfg, name, result, include=None):
    from ABM.export.markdown import render_markdown
    return render_markdown(
        config=cfg.model_dump(),
        shared_log=result.shared_log,
        events=result.events,
        scenario_name=name,
        started_at=_GOLDEN_STARTED,
        ended_at=_GOLDEN_ENDED,
        status="done",
        include=include,
        now=_GOLDEN_NOW,
    )


class HeadlessRunnerTests(unittest.TestCase):
    """`run_config` 가 GUI /start 와 같은 조립을 하는지 (ABM/simulation/headless.py)."""

    def test_golden_scenario_runs_to_max_waves(self):
        cfg, _, result = _run_golden()
        self.assertEqual(result.end_reason, "max_waves")
        self.assertEqual(result.completed_waves, cfg.max_waves)
        self.assertEqual(result.total_turns, 5)
        # background_log 항목 1개 + 발화 5개
        self.assertEqual(len(result.shared_log), 6)

    def test_collected_events_cover_every_markdown_type(self):
        _, _, result = _run_golden()
        kinds = {e["event_type"] for e in result.events}
        for expected in ("agent_move", "appearance_update", "system_intervention",
                         "world_event", "infection_update", "meeting_update"):
            self.assertIn(expected, kinds)

    def test_collected_events_match_what_the_db_persists(self):
        """수집 리스트는 DB에 남는 행과 같은 집합·같은 순서여야 한다.

        마크다운을 실행 직후(메모리)와 나중에(DB)에서 뽑았을 때 결과가 갈리면
        `export --run-id` 가 GUI 내보내기와 달라진다.
        """
        from ABM.db import SimDB
        with tempfile.TemporaryDirectory() as tmp:
            db = SimDB(os.path.join(tmp, "sim.db"))
            db.create_run("run-1", None, "골든", "{}")
            _, _, result = _run_golden(db=db, sim_id="run-1")
            rows = db.get_run_events("run-1")
        self.assertEqual([e["event_type"] for e in result.events],
                         [r["event_type"] for r in rows])
        self.assertEqual([e["wave"] for e in result.events],
                         [r["wave"] for r in rows])

    def test_emit_wrapper_is_removed_after_the_run(self):
        """`_emit` 래퍼가 남으면 /continue 가 끝난 run 의 리스트에 계속 append 한다."""
        _, _, result = _run_golden()
        self.assertNotIn("_emit", result.sim.__dict__)

    def test_config_reaches_the_engine_intact(self):
        """최근 추가된 인자(zone entry · 감염 모델 · 시간 카테고리)가 빠지지 않았는지."""
        _, _, result = _run_golden()
        sim = result.sim
        self.assertTrue(sim._infection_enabled)
        self.assertEqual(sim._infection_disease_name, "감기")
        self.assertEqual(sim._time_per_wave, 30)
        self.assertEqual(sim._time_mode, "fixed")
        self.assertIn("현관", sim._exterior_locations)
        self.assertEqual(sim._key_to_alias, {"a": "가온", "b": "나린"})

    def test_run_is_deterministic(self):
        a = _render_golden(*_run_golden(), include=_ALL_TOGGLES)
        b = _render_golden(*_run_golden(), include=_ALL_TOGGLES)
        self.assertEqual(a, b)


class MarkdownGoldenTests(unittest.TestCase):
    """`ABM/export/markdown.py` 출력 고정 (JS 쌍둥이와의 동등성 앵커).

    골든 파일을 갱신해야 할 때는 `frontend/js/sim/export/markdown.js` 도 같은
    변경을 받았는지 먼저 확인할 것 — 두 구현이 갈리면 브라우저 다운로드와 CLI
    출력이 달라진다.
    """

    def _assert_golden(self, filename, include):
        cfg, name, result = _run_golden()
        actual = _render_golden(cfg, name, result, include=include)
        path = _FIXTURES / filename
        if os.environ.get("UPDATE_GOLDEN"):
            path.write_text(actual, encoding="utf-8")
        expected = path.read_text(encoding="utf-8")
        self.assertEqual(actual, expected)

    def test_all_toggles_on(self):
        self._assert_golden("golden_full.md", _ALL_TOGGLES)

    def test_default_toggles(self):
        # GUI 체크박스 기본값 = 전부 켜짐
        self._assert_golden("golden_default.md", None)

    def test_toggles_actually_drop_sections(self):
        cfg, name, result = _run_golden()
        full = _render_golden(cfg, name, result, include=_ALL_TOGGLES)
        bare = _render_golden(cfg, name, result, include={"time"})
        for marker in ("[씬]", "[🌍 세계 사건]", "[🎬 내레이터]", "[🦠 감염]",
                       "[🏃 씬]"):
            self.assertIn(marker, full)
            self.assertNotIn(marker, bare)
        # action 토글이 꺼지면 action_note 줄이 사라진다
        self.assertIn("*(부엌 쪽을 바라보며)*", full)
        self.assertNotIn("*(부엌 쪽을 바라보며)*", bare)
        # time 토글은 켜져 있으므로 wave 헤딩은 시각 형식 그대로
        self.assertIn("### 🕐 월요일 오전 9시 00분  ·  Wave 0", bare)

    def test_time_toggle_off_falls_back_to_plain_wave_heading(self):
        cfg, name, result = _run_golden()
        md = _render_golden(cfg, name, result, include={"action"})
        self.assertIn("### 🌊 Wave 0", md)
        self.assertNotIn("🕐", md)

    def test_empty_log_still_renders_a_document(self):
        from ABM.export.markdown import render_markdown
        cfg, name = _golden_config()
        md = render_markdown(config=cfg.model_dump(), shared_log=[], events=[],
                             scenario_name=name, now=_GOLDEN_NOW)
        self.assertIn("*대화 기록이 없습니다.*", md)
        self.assertIn("## 등장인물", md)

    def test_background_log_entries_are_filtered_out(self):
        """`speaker` 없는 background 항목은 문서에 실리지 않는다 (/logs 와 같은 규칙)."""
        cfg, name, result = _run_golden()
        md = _render_golden(cfg, name, result, include=_ALL_TOGGLES)
        self.assertNotIn("[배경] 좁은 아파트에", md)
        self.assertEqual(md.count("**Wave** 4 · **총 턴** 5"), 1)


class MarkdownLabelPortTests(unittest.TestCase):
    """state.js 포팅분(ABM/export/labels.py)의 JS 의미 재현."""

    def test_build_infection_model_fills_defaults_for_legacy_config(self):
        from ABM.export.labels import build_infection_model
        m = build_infection_model(None)
        self.assertFalse(m["enabled"])
        self.assertEqual(len(m["symptom_stages"]), 3)   # 설정 없던 시나리오는 기본 3단계
        self.assertTrue(m["immune_after_recovery"])
        # 명시적 빈 배열은 존중한다
        self.assertEqual(build_infection_model({"symptom_stages": []})["symptom_stages"], [])
        # 명시적 false 는 살아남는다
        self.assertFalse(build_infection_model({"immune_after_recovery": False})["immune_after_recovery"])

    def test_recovery_min_is_lowered_to_max_except_for_chronic(self):
        from ABM.export.labels import build_infection_model
        m = build_infection_model({"recovery_min_minutes": 500, "recovery_max_minutes": 100})
        self.assertEqual((m["recovery_min_minutes"], m["recovery_max_minutes"]), (100, 100))
        # max == 0 은 "자연 회복 없음(만성)" 이라 min 을 건드리지 않는다
        m = build_infection_model({"recovery_min_minutes": 500, "recovery_max_minutes": 0})
        self.assertEqual((m["recovery_min_minutes"], m["recovery_max_minutes"]), (500, 0))

    def test_format_day_hour(self):
        from ABM.export.labels import format_day_hour
        self.assertEqual(format_day_hour(0), "0시간")
        self.assertEqual(format_day_hour(60), "1시간")
        self.assertEqual(format_day_hour(1440), "1일")
        self.assertEqual(format_day_hour(3600), "2일 12시간")

    def test_sim_time_label_matches_the_engine_formatter(self):
        """구버전 로그 폴백이 엔진의 `_format_time_str` 과 글자 단위로 같아야 한다."""
        from ABM.export.labels import sim_time_label
        from ABM.simulation import Simulation
        with tempfile.TemporaryDirectory() as tmp:
            sim = Simulation({}, [], tmp, sim_start_time="22:30",
                             sim_start_weekday="sat", time_per_wave=45)
            for wave in (0, 1, 5, 33, 100):
                self.assertEqual(
                    sim_time_label(wave, time_mode="fixed", time_per_wave=45,
                                   sim_start_time="22:30", sim_start_weekday="sat"),
                    sim._format_time_str(sim._sim_start_minutes + wave * 45),
                )

    def test_sim_time_label_is_none_when_time_is_off(self):
        from ABM.export.labels import sim_time_label
        self.assertIsNone(sim_time_label(3, time_mode="variable"))
        self.assertIsNone(sim_time_label(3, time_mode="fixed", time_per_wave=0))

    def test_js_string_and_truthiness_semantics(self):
        """LLM 이 emotion 에 배열을 뱉은 실제 로그가 있다 — JS 와 같이 다뤄야 한다."""
        from ABM.export.labels import js_str, js_truthy
        self.assertEqual(js_str(["a", "b"]), "a,b")     # 파이썬 str() 이면 "['a', 'b']"
        self.assertEqual(js_str(1.0), "1")
        self.assertEqual(js_str(None), "")
        self.assertTrue(js_truthy([]))                  # JS 에서 빈 배열은 참
        self.assertFalse(js_truthy(""))
        self.assertFalse(js_truthy(0))

    def test_meeting_narration_covers_every_status(self):
        from ABM.export.labels import AgentIndex, meeting_narration
        idx = AgentIndex([{"name": "a", "display_name": "가온"},
                          {"name": "b", "display_name": "나린"}])
        base = {"chaser": "a", "target": "b"}
        self.assertIn("만나러 이동 중", meeting_narration({**base, "status": "start"}, idx)["text"])
        self.assertIn("만났다", meeting_narration({**base, "status": "arrived"}, idx)["text"])
        self.assertIn("자리를 뜬 뒤였다",
                      meeting_narration({**base, "status": "cancelled", "reason": "gone"}, idx)["text"])
        self.assertIn("그만뒀다",
                      meeting_narration({**base, "status": "cancelled", "reason": "x"}, idx)["text"])
        self.assertIsNone(meeting_narration({**base, "status": "미래값"}, idx))
        self.assertIsNone(meeting_narration({}, idx))

    def test_agent_icon_auto_derivation(self):
        from ABM.export.labels import get_agent_icon
        self.assertEqual(get_agent_icon({"icon": "🐱"}, "happy"), "🐱")  # 명시 아이콘 우선
        self.assertEqual(get_agent_icon({"icon": "🤖", "system_prompt": "너는 남자다."}), "👨")
        self.assertEqual(
            get_agent_icon({"icon": "🤖", "system_prompt": "너는 여자다."}, "happy"), "👩😊")


class CliTests(unittest.TestCase):
    """`python -m ABM.cli` (ABM/cli.py) — LLM 서버가 필요 없는 경로만."""

    def _args(self, argv):
        from ABM.cli import build_parser
        return build_parser().parse_args(argv)

    def test_dry_run_prints_the_engine_contract(self):
        from ABM.cli import EXIT_OK, cmd_run
        import io
        from contextlib import redirect_stdout
        args = self._args(["run", str(_FIXTURES / "golden_scenario.json"), "--dry-run"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cmd_run(args)
        out = buf.getvalue()
        self.assertEqual(code, EXIT_OK)
        self.assertIn("골든 시나리오", out)
        self.assertIn("[위치 그래프", out)      # 위치 그래프 계약
        self.assertIn("[몸 상태", out)          # 감염 계약
        self.assertIn('"move_to"', out)         # 출력 계약
        self.assertIn("관계 지도     미사용", out)
        self.assertNotIn("[아는 사람 (나와의 관계)]", out)

    def test_dry_run_prints_the_relationship_map_per_agent(self):
        # 관계 지도만 에이전트마다 다르므로 공통 계약과 분리해서 보여준다.
        from ABM.cli import EXIT_OK, cmd_run
        import io
        from contextlib import redirect_stdout, redirect_stderr

        raw = json.loads((_FIXTURES / "golden_scenario.json").read_text(encoding="utf-8"))
        cfg = json.loads(raw["config_json"]) if "config_json" in raw else raw["config"]
        cfg["agents"][0]["relationships"] = {"b": "아내", "없는사람": "유령"}
        cfg["agents"][1]["relationships"] = {"a": "남편"}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rel.json")
            Path(path).write_text(json.dumps({"name": "관계", "config": cfg}),
                                  encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cmd_run(self._args(["run", path, "--dry-run"]))

        text = out.getvalue()
        self.assertEqual(code, EXIT_OK)
        self.assertIn("관계 지도     2명 설정", text)
        self.assertIn("## a", text)
        self.assertIn('- 나린 (ID: "b") — 당신의 아내', text)   # display_name 사용
        self.assertIn('- 가온 (ID: "a") — 당신의 남편', text)
        self.assertNotIn("유령", text)                          # dangling 은 렌더 제외
        self.assertIn("없는사람", err.getvalue())                # 대신 경고

    def test_dry_run_overrides_reach_the_config(self):
        from ABM.cli import _build_config
        args = self._args(["run", str(_FIXTURES / "golden_scenario.json"),
                           "--max-waves", "77", "--temperature", "1.5",
                           "--server-id", "srv-1", "--target-minutes", "480"])
        cfg, name = _build_config(args)
        self.assertEqual((cfg.max_waves, cfg.temperature, cfg.server_id,
                          cfg.target_duration_minutes), (77, 1.5, "srv-1", 480))
        self.assertEqual(name, "골든 시나리오")

    def test_scenario_file_shapes(self):
        from ABM.cli import ConfigError, _unwrap_scenario
        cfg = {"agents": [], "background": "", "start_agent": "a"}
        self.assertEqual(_unwrap_scenario(cfg), (cfg, ""))
        self.assertEqual(_unwrap_scenario({"name": "N", "config": cfg}), (cfg, "N"))
        self.assertEqual(_unwrap_scenario({"name": "N", "config_json": json.dumps(cfg)}),
                         (cfg, "N"))
        with self.assertRaises(ConfigError):
            _unwrap_scenario({"nope": 1})

    def test_invalid_scenario_exits_with_config_code(self):
        from ABM.cli import EXIT_CONFIG, main
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            Path(bad).write_text('{"agents": []}', encoding="utf-8")   # background/start_agent 없음
            self.assertEqual(main(["run", bad, "--dry-run"]), EXIT_CONFIG)
            missing = os.path.join(tmp, "nope.json")
            self.assertEqual(main(["run", missing, "--dry-run"]), EXIT_CONFIG)

    def test_include_exclude_parsing(self):
        from ABM.cli import ConfigError, _parse_include
        from ABM.export.markdown import DEFAULT_INCLUDE
        self.assertEqual(_parse_include(self._args(["export", "--run-id", "x"])),
                         DEFAULT_INCLUDE)
        self.assertEqual(
            _parse_include(self._args(["export", "--run-id", "x", "--include", "time,move"])),
            frozenset({"time", "move"}))
        self.assertNotIn(
            "move",
            _parse_include(self._args(["export", "--run-id", "x", "--exclude", "move"])))
        with self.assertRaises(ConfigError):
            _parse_include(self._args(["export", "--run-id", "x", "--include", "없는토글"]))

    def test_filename_helpers_match_the_browser(self):
        from ABM.cli import now_tag, safe_filename
        self.assertEqual(safe_filename('a/b:c*d?"e<f>g|h'), "a_b_c_d__e_f_g_h")
        self.assertEqual(len(safe_filename("가" * 200)), 80)
        # frontend/js/sim/utils/download.js 의 nowTag() 와 같은 모양 (UTC)
        self.assertRegex(now_tag(), r"^\d{4}-\d{2}-\d{2}_\d{4}$")

    def test_export_from_db_matches_direct_render(self):
        """`export --run-id` 가 실행 직후 렌더와 같은 문서를 만드는지."""
        from ABM.db import SimDB
        from ABM.export.markdown import render_markdown
        import ABM.cli as cli
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            db = SimDB(os.path.join(tmp, "simulation.db"))
            cfg, name = _golden_config()
            db.create_run("run-x", None, name, cfg.model_dump_json())
            _, _, result = _run_golden(db=db, sim_id="run-x")
            db.finish_run("run-x", "done", result.completed_waves, len(result.shared_log))

            direct = render_markdown(
                config=cfg.model_dump(), shared_log=result.shared_log,
                events=result.events, scenario_name=name, status="done",
                now=_GOLDEN_NOW)

            orig_log_dir = os.environ.get("ABM_LOG_DIR")
            os.environ["ABM_LOG_DIR"] = tmp
            try:
                import importlib
                import ABM.config
                importlib.reload(ABM.config)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = cli.cmd_export(self._args(["export", "--run-id", "run-x"]))
            finally:
                if orig_log_dir is None:
                    os.environ.pop("ABM_LOG_DIR", None)
                else:
                    os.environ["ABM_LOG_DIR"] = orig_log_dir
                import importlib
                import ABM.config
                importlib.reload(ABM.config)

        self.assertEqual(code, cli.EXIT_OK)
        # 추출 일시 한 줄만 다르다 (렌더 시각이 다르므로)
        strip = lambda s: [ln for ln in s.split("\n") if not ln.startswith("> **추출 일시**")]
        self.assertEqual(strip(buf.getvalue()), strip(direct))




class StartEndpointDelegationTests(unittest.TestCase):
    """`/start` 가 `run_config` 로 위임한 뒤에도 GUI 계약이 그대로인지.

    lifecycle 리팩터의 회귀 방어선이다. 조립 자체는 `run_config` 가 하지만
    **SSE 큐 · stop_event · run row · `_sim` 전역 채우기 · finalize_run** 은
    여전히 lifecycle 의 책임이고, 하나라도 빠지면 브라우저에서 실행이 조용히
    죽거나(피드 없음) 이력이 안 남는다.
    """

    def test_start_wires_queue_db_globals_and_finalize(self):
        from backend.api.simulation.runtime import lifecycle
        from backend.api.simulation.state import _sim
        import ABM.db
        import ABM.simulation.headless as headless

        cfg = _sim_cfg(max_waves=1)
        captured = {}
        created_runs = []
        finalized = []

        class _FakeDB:
            def __init__(self, path):
                created_runs.append(("db", path))

            def create_run(self, run_id, scenario_id, scenario_name, config_json,
                           start_wave=0):
                created_runs.append((run_id, scenario_id, scenario_name, config_json))

        class _FakeSim:
            agents = {"a": object()}
            background_log = [{"role": "user", "content": "[배경] 테스트 배경"}]
            shared_log = []
            edges = []

        def _fake_run_config(config, **kw):
            captured.update(kw)
            captured["cfg"] = config
            kw["on_sim_ready"](_FakeSim())
            return headless.RunResult(
                run_id="r", shared_log=[], events=[], edges=[],
                end_reason="max_waves", completed_waves=1, total_turns=0,
            )

        patches = {
            (lifecycle, "_make_llm"):           lambda *a, **k: "LLM",
            (lifecycle, "_make_agent_llm_map"): lambda c: {"a": "LLM2"},
            (lifecycle, "finalize_run"):        lambda *a, **k: finalized.append((a, k)),
            (ABM.db, "SimDB"):                  _FakeDB,
            (headless, "run_config"):           _fake_run_config,
        }
        originals = {k: getattr(k[0], k[1]) for k in patches}
        prev_status = _sim["status"]
        _sim["status"] = "idle"
        try:
            for (mod, name), value in patches.items():
                setattr(mod, name, value)
            lifecycle.start_simulation(cfg)
            _sim["thread"].join(timeout=10)
        finally:
            for (mod, name), value in originals.items():
                setattr(mod, name, value)
            _sim["status"] = prev_status

        # 1) SSE 큐와 stop_event 가 엔진까지 전달됐다 (없으면 피드가 죽는다)
        self.assertIs(captured["event_queue"], _sim["event_queue"])
        self.assertIs(captured["stop_event"], _sim["stop_event"])
        # 2) DB/run id/로그 디렉토리
        from ABM.config import LOG_DIR
        self.assertEqual(captured["log_dir"], LOG_DIR)
        self.assertIsInstance(captured["db"], _FakeDB)
        self.assertTrue(captured["sim_id"])
        # 3) run row 가 config 스냅샷과 함께 만들어졌다
        run_rows = [r for r in created_runs if r[0] != "db"]
        self.assertEqual(len(run_rows), 1)
        self.assertEqual(run_rows[0][0], captured["sim_id"])
        self.assertEqual(json.loads(run_rows[0][3])["start_agent"], "a")
        # 4) LLM 오버라이드 맵이 전달됐다
        self.assertEqual(captured["agent_llm"], {"a": "LLM2"})
        # 5) `_sim` 전역이 run() 시작 전에 채워졌다 (/status·/logs·컨텍스트 조회용)
        self.assertIsInstance(_sim["sim_obj"], _FakeSim)
        self.assertEqual(_sim["scenario_id"], cfg.scenario_id)
        self.assertEqual(json.loads(_sim["config_json"])["start_agent"], "a")
        # 6) finalize_run 이 정확히 한 번, 오류 없이 불렸다
        self.assertEqual(len(finalized), 1)
        self.assertNotIn("error", finalized[0][1])

    def test_start_reports_assembly_failure_through_finalize_run(self):
        """조립 단계에서 터져도 finalize_run(error=) 로 UI 에 전달돼야 한다."""
        from backend.api.simulation.runtime import lifecycle
        from backend.api.simulation.state import _sim
        import ABM.simulation.headless as headless

        finalized = []
        boom = RuntimeError("서버 없음")

        def _explode(config, **kw):
            raise boom

        originals = {
            "_make_llm":           lifecycle._make_llm,
            "_make_agent_llm_map": lifecycle._make_agent_llm_map,
            "finalize_run":        lifecycle.finalize_run,
        }
        import ABM.db
        orig_run_config = headless.run_config
        orig_db = ABM.db.SimDB
        prev_status = _sim["status"]
        _sim["status"] = "idle"
        try:
            lifecycle._make_llm           = lambda *a, **k: "LLM"
            lifecycle._make_agent_llm_map = lambda c: {}
            lifecycle.finalize_run        = lambda *a, **k: finalized.append((a, k))
            ABM.db.SimDB                  = lambda path: type(
                "D", (), {"create_run": lambda *a, **k: None})()
            headless.run_config           = _explode
            lifecycle.start_simulation(_sim_cfg(max_waves=1))
            _sim["thread"].join(timeout=10)
        finally:
            for name, value in originals.items():
                setattr(lifecycle, name, value)
            headless.run_config = orig_run_config
            ABM.db.SimDB = orig_db
            _sim["status"] = prev_status

        self.assertEqual(len(finalized), 1)
        self.assertIs(finalized[0][1].get("error"), boom)


class HeadlessSimulationArgumentTests(unittest.TestCase):
    """`run_config` 가 Simulation 에 넘기는 인자 목록 고정.

    lifecycle 인라인 코드에서 옮겨오며 하나라도 빠지면 GUI 가 조용히 회귀한다
    (감염 모델이 꺼지거나, zone 입구가 사라지거나, 시계가 안 흐르거나…).
    """

    def _capture(self, cfg):
        import ABM.simulation as sim_pkg
        from ABM.simulation.headless import run_config

        captured = {}

        class _Recorder:
            def __init__(self, agents, background_log, log_dir, **kw):
                captured["positional"] = (agents, background_log, log_dir)
                captured.update(kw)
                self.agents = agents
                self.background_log = background_log
                self.shared_log = []
                self.edges = []
                self.completed_waves = 0

            def _emit(self, t, d):
                pass

            def run(self, start_agent, **kw):
                captured["run_kwargs"] = kw
                captured["run_start_agent"] = start_agent

        orig = sim_pkg.Simulation
        try:
            sim_pkg.Simulation = _Recorder
            with tempfile.TemporaryDirectory() as tmp:
                run_config(cfg, llm="LLM", agent_llm={"a": "L2"}, log_dir=tmp,
                           sim_id="sid")
        finally:
            sim_pkg.Simulation = orig
        return captured

    def test_every_engine_knob_is_forwarded(self):
        cfg, _ = _golden_config()
        cap = self._capture(cfg)

        self.assertEqual(cap["sim_id"], "sid")
        self.assertEqual(cap["agent_llm"], {"a": "L2"})
        self.assertEqual(cap["name_aliases"], {"가온": "a", "나린": "b"})
        self.assertEqual(cap["agent_locations"], {"a": "거실", "b": "거실"})
        self.assertEqual(cap["agent_visuals"]["a"], "회색 후드티를 입은 사람")
        self.assertTrue(cap["system_agent"]["enabled"])
        self.assertEqual(cap["sim_start_time"], "09:00")
        self.assertEqual(cap["sim_start_weekday"], "mon")
        self.assertEqual(cap["time_per_wave"], 30)
        self.assertEqual(cap["time_mode"], "fixed")
        self.assertEqual(len(cap["time_categories"]), 4)
        self.assertEqual(cap["idle_minutes_schedule"], [60, 120, 180])
        self.assertTrue(cap["infection_model"]["enabled"])
        self.assertEqual(cap["infection_model"]["disease_name"], "감기")
        self.assertEqual(cap["llm_max_tokens"], 2048)
        self.assertTrue(cap["lang_fix_enabled"])
        self.assertEqual(cap["lang_fix_retries"], 2)
        # 위치 그래프는 dict 로 평탄화되며 zone/입구 플래그까지 실려야 한다
        node = {n["name"]: n for n in cap["location_graph"]}
        self.assertEqual(set(node["거실"]), {"name", "connects_to", "is_exterior",
                                             "zone", "is_zone_entry"})
        self.assertTrue(node["현관"]["is_exterior"])
        # sim.run 인자
        self.assertEqual(cap["run_start_agent"], "a")
        self.assertEqual(cap["run_kwargs"]["max_waves"], 5)
        self.assertEqual(cap["run_kwargs"]["max_silence_waves"], 3)
        self.assertTrue(cap["run_kwargs"]["early_stop_enabled"])
        self.assertIsNone(cap["run_kwargs"]["target_duration_minutes"])
        self.assertEqual(len(cap["run_kwargs"]["events"]), 1)
        self.assertEqual(cap["run_kwargs"]["events"][0]["type"], "infect_agent")

    def test_initial_active_and_zone_entry_survive(self):
        cfg, _ = _golden_config()
        data = cfg.model_dump()
        data["agents"][1]["initial_active"] = False
        data["location_graph"][2].update({"zone": "창고구역", "is_zone_entry": True})
        cap = self._capture(SimStartConfig(**data))
        self.assertEqual(cap["initial_agents"], ["a"])
        node = {n["name"]: n for n in cap["location_graph"]}
        self.assertEqual(node["창고"]["zone"], "창고구역")
        self.assertTrue(node["창고"]["is_zone_entry"])

    def test_all_active_agents_pass_none_so_the_engine_defaults(self):
        cfg, _ = _golden_config()
        self.assertIsNone(self._capture(cfg)["initial_agents"])

    def test_relationships_are_forwarded_per_agent(self):
        # 관계 지도는 location/groups/visuals 와 같은 방식으로 key 별로 뽑혀 전달된다.
        # 여기서 빠지면 GUI /start 만 관계 블록 없이 조용히 돌아간다(CLI 는 정상).
        cfg, _ = _golden_config()
        self.assertEqual(self._capture(cfg)["agent_relationships"], {"a": {}, "b": {}})

        data = cfg.model_dump()
        data["agents"][0]["relationships"] = {"b": "아내"}
        cap = self._capture(SimStartConfig(**data))
        self.assertEqual(cap["agent_relationships"], {"a": {"b": "아내"}, "b": {}})


class ChatAgentRelationshipsTests(unittest.TestCase):
    """관계 지도가 채팅 에이전트 테이블에서 왕복 보존되는가.

    시뮬레이션 -> 채팅 -> 시뮬레이션 왕복에서 groups 는 살아 돌아오는데
    relationships 만 경고 없이 {} 로 유실되던 회귀를 막는다.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = Path(self.tmpdir.name) / "memory.db"
        conn = get_db()
        init_tables(conn)
        migrate_db(conn)
        conn.close()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmpdir.cleanup()

    def _create(self, **kwargs):
        return agents_api.create_agent(AgentCreate(name="김봉남", **kwargs))

    def test_relationships_survive_create_and_get(self):
        created = self._create(relationships={"채민경": "아내", "김미경": "딸"})
        self.assertEqual(created["relationships"], {"채민경": "아내", "김미경": "딸"})
        # 새 커넥션으로 다시 읽어도(= DB 에 실제로 저장됐는가) 같아야 한다.
        self.assertEqual(agents_api.get_agent(created["id"])["relationships"],
                         {"채민경": "아내", "김미경": "딸"})

    def test_relationships_default_to_empty_map(self):
        created = self._create()
        self.assertEqual(created["relationships"], {})
        self.assertEqual(agents_api.list_agents()[0]["relationships"], {})

    def test_partial_update_preserves_relationships(self):
        created = self._create(relationships={"채민경": "아내"})
        # 이름만 고치는 부분 업데이트가 관계 지도를 지우면 안 된다.
        updated = agents_api.update_agent(created["id"], AgentUpdate(name="김봉남2"))
        self.assertEqual(updated["relationships"], {"채민경": "아내"})

    def test_explicit_null_does_not_reset_relationships(self):
        # groups 등 다른 보존 전용 필드와 동일하게, 명시적 null 은 "건드리지 않음"이다.
        created = self._create(relationships={"채민경": "아내"})
        updated = agents_api.update_agent(
            created["id"], AgentUpdate(**{"relationships": None, "name": "김봉남2"}))
        self.assertEqual(updated["relationships"], {"채민경": "아내"})

    def test_update_can_replace_and_clear_relationships(self):
        created = self._create(relationships={"채민경": "아내"})
        updated = agents_api.update_agent(
            created["id"], AgentUpdate(relationships={"김미경": "딸"}))
        self.assertEqual(updated["relationships"], {"김미경": "딸"})
        cleared = agents_api.update_agent(created["id"], AgentUpdate(relationships={}))
        self.assertEqual(cleared["relationships"], {})

    def test_legacy_row_without_relationships_reads_as_empty_map(self):
        # 마이그레이션으로 컬럼만 생긴 구 row 는 값이 NULL 이다 — json.loads(None) 방어.
        created = self._create(relationships={"채민경": "아내"})
        conn = get_db()
        conn.execute("UPDATE agents SET relationships=NULL WHERE id=?", (created["id"],))
        conn.commit()
        conn.close()
        self.assertEqual(agents_api.get_agent(created["id"])["relationships"], {})

    def test_corrupt_relationships_value_reads_as_empty_map(self):
        created = self._create()
        conn = get_db()
        for bad in ("not json", '["배열은 관계지도가 아니다"]'):
            conn.execute("UPDATE agents SET relationships=? WHERE id=?", (bad, created["id"]))
            conn.commit()
            self.assertEqual(agents_api.get_agent(created["id"])["relationships"], {})
        conn.close()

    def test_migration_adds_relationships_column_to_legacy_agents_table(self):
        conn = get_db()
        conn.execute("DROP TABLE agents")
        # relationships 컬럼이 없던 구 스키마 재현.
        conn.execute("""
            CREATE TABLE agents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                system_prompt TEXT NOT NULL DEFAULT '',
                icon TEXT NOT NULL DEFAULT '🤖',
                model TEXT, temperature REAL NOT NULL DEFAULT 0.7,
                max_tokens INTEGER NOT NULL DEFAULT 1024,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO agents (id, name, created_at, updated_at) VALUES (?,?,?,?)",
            ("old-1", "구버전", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()

        migrate_db(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
        conn.close()

        self.assertIn("relationships", cols)
        self.assertEqual(agents_api.get_agent("old-1")["relationships"], {})
        # 마이그레이션 후에도 새 에이전트 생성(INSERT 컬럼 목록)이 동작해야 한다.
        self.assertEqual(self._create(relationships={"a": "친구"})["relationships"],
                         {"a": "친구"})


class TurnLocationLoggingTests(unittest.TestCase):
    """턴 로그의 위치 이력 (감염병 접촉 분석용 CSV 내보내기의 데이터 원천).

    핵심 불변식: 기록되는 `location` 은 그 wave 의 이동이 적용되기 **전** 스냅샷
    이어야 한다 — 이동은 wave 안 모든 턴이 끝난 뒤 runner 가 적용하므로, 그 값이
    바로 그 에이전트가 그 wave 동안 실제로 있던(=접촉이 일어난) 장소다. 이동
    **후** 값을 남기면 '이미 떠난 장소'가 접촉 시점 위치로 둔갑한다.
    """

    GRAPH = [
        {"name": "매장", "connects_to": ["창고", "동네"]},
        {"name": "창고", "connects_to": ["매장"]},
        # 외부 공간 — 동석해도 서로 못 보는 곳. 접촉으로 오판하면 안 되므로
        # is_exterior 를 로그에 같이 남긴다.
        {"name": "동네", "connects_to": ["매장"], "is_exterior": True},
    ]

    def _run_one_wave(self, tmp, *, db=None, sim_id=None):
        from ABM.agent import Agent
        from ABM.simulation import Simulation

        agents = {k: Agent(k, f"너는 {k}다.", tmp, token_limit=4096) for k in ("a", "b")}
        sim = Simulation(
            agents, [{"role": "user", "content": "[배경] 테스트"}], tmp,
            llm=_ScriptedLLM({
                # a 는 말하면서 같은 턴에 창고로 떠난다.
                "a": [{"content": "먼저 갈게.", "target": "self", "move_to": "창고"}],
                "b": [{"content": "음.",       "target": "self"}],
            }),
            agent_locations={"a": "매장", "b": "동네"},
            location_graph=self.GRAPH,
            db=db, sim_id=sim_id,
        )
        sim._emit = lambda t, d: None
        # 둘 다 wave 0 에 투입 — 한 wave 에 두 에이전트의 턴 로그를 얻기 위함.
        sim.run("a", max_waves=1, step_delay=0.0, resume_wave={"a": [], "b": []})
        return sim

    @staticmethod
    def _entry(log, speaker):
        return next(e for e in reversed(log)
                    if e.get("speaker") == speaker)

    def test_shared_log_records_pre_move_location_and_is_exterior(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = self._run_one_wave(tmp)

            a = self._entry(sim.shared_log, "a")
            # a 는 이 wave 동안 매장에 있었고, 이동은 턴이 끝난 뒤 적용됐다.
            self.assertEqual(sim._agent_location["a"], "창고")
            self.assertEqual(a["location"], "매장")
            self.assertIs(a["is_exterior"], False)

            b = self._entry(sim.shared_log, "b")
            self.assertEqual(b["location"], "동네")
            self.assertIs(b["is_exterior"], True)

    def test_engine_persists_location_to_db_log(self):
        from ABM.db import SimDB

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = SimDB(os.path.join(tmp, "sim.db"))
            try:
                db.create_run("r1", "scn", "시나리오", "{}")
                self._run_one_wave(tmp, db=db, sim_id="r1")

                rows = {r["speaker"]: r for r in db.get_run_log("r1")}
                self.assertEqual(rows["a"]["location"], "매장")
                self.assertIs(rows["a"]["is_exterior"], False)
                self.assertEqual(rows["b"]["location"], "동네")
                self.assertIs(rows["b"]["is_exterior"], True)
            finally:
                conn = getattr(db._local, "conn", None)
                if conn is not None:
                    conn.close()

    def test_log_turn_round_trip_and_backward_compatible_default(self):
        from ABM.db import SimDB

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = SimDB(os.path.join(tmp, "sim.db"))
            try:
                db.create_run("r1", "scn", "시나리오", "{}")
                db.log_turn("r1", 0, 0, "a", "안녕", "", {"emotion": "기쁨"}, ["b"],
                            time_str="09:00", location="매장", is_exterior=False)
                db.log_turn("r1", 0, 1, "b", "그래", "", {}, [],
                            time_str="09:00", location="동네", is_exterior=True)
                # location/is_exterior 를 안 넘기는 기존 호출부(하위 호환) — 깨지지 않고
                # 그 행은 NULL 로 남는다.
                db.log_turn("r1", 1, 2, "a", "옛 로그", "", {}, [])

                rows = db.get_run_log("r1")
                self.assertEqual([r["location"] for r in rows], ["매장", "동네", None])
                self.assertEqual([r["is_exterior"] for r in rows], [False, True, None])
                # 기존 필드는 그대로 살아있어야 한다.
                self.assertEqual(rows[0]["meta"], {"emotion": "기쁨"})
                self.assertEqual(rows[0]["targets"], ["b"])
                self.assertEqual(rows[0]["time_str"], "09:00")
            finally:
                conn = getattr(db._local, "conn", None)
                if conn is not None:
                    conn.close()

    def test_legacy_db_without_location_columns_is_migrated(self):
        from ABM.db import SimDB

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "old.db")
            # 컬럼 추가 이전 스키마의 DB를 손으로 만든다.
            legacy = sqlite3.connect(path)
            legacy.execute("""
                CREATE TABLE simulation_log (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id       TEXT    NOT NULL,
                    wave         INTEGER NOT NULL DEFAULT 0,
                    turn         INTEGER NOT NULL DEFAULT 0,
                    speaker      TEXT    NOT NULL,
                    content      TEXT    NOT NULL,
                    action_note  TEXT    NOT NULL DEFAULT '',
                    meta_json    TEXT    NOT NULL DEFAULT '{}',
                    targets_json TEXT    NOT NULL DEFAULT '[]',
                    timestamp    REAL    NOT NULL
                )
            """)
            legacy.execute(
                "INSERT INTO simulation_log (run_id, speaker, content, timestamp) VALUES (?,?,?,?)",
                ("r1", "a", "옛 발화", 0.0),
            )
            legacy.commit()
            legacy.close()

            db = SimDB(path)
            try:
                cols = {r[1] for r in db._conn().execute(
                    "PRAGMA table_info(simulation_log)").fetchall()}
                self.assertIn("location", cols)
                self.assertIn("is_exterior", cols)

                # 옛 행은 백필하지 않는다 — NULL 로 조회되는 게 정상.
                old = db.get_run_log("r1")[0]
                self.assertIsNone(old["location"])
                self.assertIsNone(old["is_exterior"])

                # 마이그레이션 후에도 새 INSERT(컬럼 목록)가 동작해야 한다.
                db.log_turn("r1", 0, 0, "b", "새 발화", "", {}, [], location="창고",
                            is_exterior=False)
                self.assertEqual(db.get_run_log("r1")[1]["location"], "창고")
            finally:
                conn = getattr(db._local, "conn", None)
                if conn is not None:
                    conn.close()


class SimulationAssemblyParityTests(unittest.TestCase):
    """`Simulation(...)` 을 직접 조립하는 세 경로가 config 필드를 함께 받는지 검사.

    `/start` 는 `ABM/simulation/headless.py::run_config` 를 타지만, `/load` 와
    `/resume` 은 `backend/api/simulation/runtime/load.py` · `resume.py` 가
    **각자 따로** `Simulation(...)` 을 조립한다. 새 설정 필드를 headless 에만
    넣고 두 파일을 빠뜨리면 되살린 실행만 조용히 엔진 기본값으로 돌아가는데,
    이 버그를 실제로 세 번 반복해서 냈다(`time_estimation_mode`,
    `perception_mode` 등). 런타임 테스트로는 잘 안 잡혀서 구조로 고정한다.

    규칙: headless 의 `Simulation(...)` 키워드 중 **값 표현식이 `cfg` 를
    참조하는 것**(= 시나리오 설정에서 온 값)은 load.py 와 resume.py 의
    `Simulation(...)` 에도 반드시 키워드로 존재해야 한다.
    """

    # 정당한 예외가 생기면 여기에 필드명과 사유를 함께 남길 것.
    _EXEMPT: dict[str, str] = {}

    _HEADLESS = Path(__file__).resolve().parents[1] / "ABM" / "simulation" / "headless.py"
    _RUNTIME  = Path(__file__).resolve().parents[1] / "backend" / "api" / "simulation" / "runtime"

    @staticmethod
    def _sim_call_keywords(path: Path) -> dict[str, ast.keyword]:
        """파일 안 `Simulation(...)` 호출의 키워드 인자 맵."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: dict[str, ast.keyword] = {}
        calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Simulation":
                calls += 1
                for kw in node.keywords:
                    if kw.arg:
                        found[kw.arg] = kw
        assert calls == 1, f"{path.name}: expected exactly 1 Simulation(...) call, found {calls}"
        return found

    @classmethod
    def _cfg_derived_fields(cls) -> set[str]:
        """headless 의 Simulation 키워드 중 값이 `cfg` 에서 오는 것들."""
        out = set()
        for name, kw in cls._sim_call_keywords(cls._HEADLESS).items():
            mentions_cfg = any(
                isinstance(n, ast.Name) and n.id == "cfg" for n in ast.walk(kw.value)
            )
            if mentions_cfg and name not in cls._EXEMPT:
                out.add(name)
        return out

    def test_cfg_derived_fields_are_wired_into_load_and_resume(self):
        expected = self._cfg_derived_fields()
        # 회귀 방지 하한 — 컴프리헨션이 깨져 빈 집합이 되면 테스트가 통과해 버린다.
        self.assertGreaterEqual(len(expected), 10)
        # 이번에 추가한 필드가 실제로 감지 대상에 들어오는지 못박아 둔다.
        self.assertIn("perception_mode", expected)
        self.assertIn("time_estimation_mode", expected)

        for fname in ("load.py", "resume.py"):
            with self.subTest(file=fname):
                actual_kw = self._sim_call_keywords(self._RUNTIME / fname)
                missing = sorted(expected - set(actual_kw))
                # 키워드가 있어도 값이 cfg를 참조하지 않으면(예: 상수를 하드코딩)
                # 실제로는 배선이 안 된 것과 같다 — 그 시나리오의 설정값이 무시된다.
                constant = sorted(
                    name for name in expected & set(actual_kw)
                    if not any(
                        isinstance(n, ast.Name) and n.id == "cfg"
                        for n in ast.walk(actual_kw[name].value)
                    )
                )
                self.assertEqual(
                    (missing, constant), ([], []),
                    f"backend/api/simulation/runtime/{fname} 의 Simulation(...) 이 "
                    f"config 필드 {missing} 를 아예 안 넘기거나(missing), {constant} 를 "
                    f"cfg 참조 없는 상수로 넘긴다(constant). headless.py(/start)에만 "
                    f"제대로 넣고 여기를 빠뜨리면 /load·/resume 으로 되살린 실행만 "
                    f"조용히 엔진 기본값으로 되돌아간다.",
                )

    def test_perception_mode_survives_scenario_round_trip(self):
        """시나리오 저장(config_json) → 재파싱에서 perception_mode 가 유실되지 않는다."""
        from backend.api.simulation.scenarios import _config_json

        base = dict(
            agents=[AgentConfig(name="a", system_prompt="p")],
            background="bg",
            start_agent="a",
        )
        self.assertEqual(SimStartConfig(**base).perception_mode, "targeted")

        saved = _config_json(SimStartConfig(**base, perception_mode="spatial"))
        self.assertEqual(
            SimStartConfig(**json.loads(saved)).perception_mode, "spatial"
        )

        # 필드가 없던 구버전 config_json 은 기본값으로 안전하게 로드된다.
        legacy = json.loads(saved)
        legacy.pop("perception_mode")
        self.assertEqual(SimStartConfig(**legacy).perception_mode, "targeted")

        with self.assertRaises(ValidationError):
            SimStartConfig(**base, perception_mode="bogus")


if __name__ == "__main__":
    unittest.main()
