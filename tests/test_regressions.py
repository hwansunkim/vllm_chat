import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx
from tenacity import wait_none

from backend import config, state
from backend.api import conversations
from backend.api.schemas import ChatMessage
from backend.db.database import get_db, init_tables, migrate_db
from backend.llm import bridge
from backend.llm import client as llm_client
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

    async def chat(self, messages, *, temperature=0.7, max_tokens=4096, timeout=None):
        self.calls += 1
        self.seen_timeouts.append(timeout)
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
        self.old_extract_keywords = conversations.async_extract_keywords
        conversations.get_registry = lambda: FakeRegistry()
        conversations.async_extract_keywords = self._empty_keywords
        conversations.async_stream_chat = self._failing_stream

    async def asyncTearDown(self):
        conversations.get_registry = self.old_registry
        conversations.async_stream_chat = self.old_stream_chat
        conversations.async_extract_keywords = self.old_extract_keywords
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
