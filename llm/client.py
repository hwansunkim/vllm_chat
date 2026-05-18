from __future__ import annotations

import json
import re

import httpx

import config

# ── HTTP 클라이언트 생명주기 ──────────────────────────────────────────────────
# app.py lifespan에서 setup_client() / teardown_client()를 호출해 관리한다.

_http_client: httpx.AsyncClient | None = None


def setup_client() -> None:
    global _http_client
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))


async def teardown_client() -> None:
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


def _client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized")
    return _http_client


# ── JSON 파싱 유틸 ────────────────────────────────────────────────────────────

def _parse_json(text: str, array: bool = False) -> list | dict | None:
    open_ch, close_ch = ('[', ']') if array else ('{', '}')
    fallback: list | None = [] if array else None

    fence = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass

    start = text.find(open_ch)
    if start == -1:
        return fallback
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return fallback
    return fallback


# ── LLM 호출 ─────────────────────────────────────────────────────────────────

async def async_llm(prompt: str, max_tokens: int = 512, temperature: float = 0.1) -> str:
    response = await _client().post(
        f"{config.BASE_URL}/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        json={
            "model": config.MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


async def async_chat(
    messages: list,
    *,
    temperature: float = 0.7,
    max_tokens: int = config.MAX_COMPLETION_TOKENS,
    model: str | None = None,
) -> tuple[str, dict]:
    full_reply = ""
    total_completion_tokens = 0
    final_usage: dict = {}
    current_messages = list(messages)
    _model = model or config.MODEL

    for _ in range(config.MAX_CONTINUATION_ROUNDS):
        response = await _client().post(
            f"{config.BASE_URL}/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json={
                "model": _model,
                "messages": current_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        response.raise_for_status()
        data = response.json()

        choice = data["choices"][0]
        partial: str = choice["message"]["content"]
        finish_reason: str = choice.get("finish_reason", "stop")
        usage: dict = data.get("usage", {})

        full_reply += partial
        total_completion_tokens += usage.get("completion_tokens", 0)
        final_usage = usage

        if finish_reason != "length":
            break

        current_messages = current_messages + [
            {"role": "assistant", "content": partial},
            {"role": "user", "content": config.CONTINUE_PROMPT},
        ]

    merged_usage = dict(final_usage)
    merged_usage["completion_tokens"] = total_completion_tokens
    return full_reply, merged_usage


async def async_get_model_context_limit() -> int:
    try:
        response = await _client().get(
            f"{config.BASE_URL}/v1/models",
            timeout=5,
        )
        for m in response.json().get("data", []):
            if m["id"] == config.MODEL:
                return m.get("max_model_len", 0)
    except Exception:
        pass
    return 0
