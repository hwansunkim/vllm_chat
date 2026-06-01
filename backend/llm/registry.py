from __future__ import annotations

import asyncio
import sqlite3

from .providers.base import LLMProvider
from .providers.vllm import VLLMProvider


class ServerRegistry:
    """
    서버 풀을 관리하고 요청별로 최적 서버를 선택한다.

    선택 우선순위:
      1. server_id 명시 → 해당 서버 (enabled 여부 무시)
      2. model 명시    → model_match: 해당 모델을 서빙하는 enabled 서버들 중 round-robin
      3. fallback      → is_default=1 서버, 없으면 첫 번째 enabled 서버
    """

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._rr_counters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def load_from_db(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT * FROM servers WHERE enabled=1 ORDER BY is_default DESC, created_at"
        ).fetchall()
        for row in rows:
            await self._create_provider(dict(row))

    async def _create_provider(self, row: dict) -> VLLMProvider:
        p = VLLMProvider(
            server_id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            model=row["model"],
            api_key=row.get("api_key", ""),
            enabled=bool(row["enabled"]),
            is_default=bool(row.get("is_default", False)),
            thinking=bool(row.get("thinking", False)),
            configured_max_len=int(row.get("max_model_len", 0)),
        )
        self._providers[row["id"]] = p
        return p

    async def close_all(self) -> None:
        for p in list(self._providers.values()):
            await p.close()
        self._providers.clear()

    async def register(self, row: dict) -> VLLMProvider:
        async with self._lock:
            existing = self._providers.pop(row["id"], None)
            if existing:
                await existing.close()
            if not row.get("enabled", True):
                return None
            if row.get("is_default"):
                for p in self._providers.values():
                    p.is_default = False
            return await self._create_provider(row)

    async def unregister(self, server_id: str) -> None:
        async with self._lock:
            p = self._providers.pop(server_id, None)
            if p:
                await p.close()

    def get_provider(self, server_id: str) -> VLLMProvider | None:
        return self._providers.get(server_id)

    def list_providers(self) -> list[LLMProvider]:
        return list(self._providers.values())

    def get_default(self) -> LLMProvider | None:
        for p in self._providers.values():
            if p.is_default and p.enabled:
                return p
        for p in self._providers.values():
            if p.enabled:
                return p
        return None

    def select(
        self,
        *,
        model: str | None = None,
        server_id: str | None = None,
    ) -> LLMProvider:
        if server_id:
            p = self._providers.get(server_id)
            if p:
                return p

        if model:
            candidates = [p for p in self._providers.values() if p.enabled and p.model == model]
            if candidates:
                return self._round_robin(model, candidates)

        default = self.get_default()
        if default:
            return default

        raise RuntimeError("사용 가능한 LLM 서버가 없습니다.")

    def _round_robin(self, key: str, candidates: list[LLMProvider]) -> LLMProvider:
        idx = self._rr_counters.get(key, 0) % len(candidates)
        self._rr_counters[key] = idx + 1
        return candidates[idx]


_registry = ServerRegistry()


def get_registry() -> ServerRegistry:
    return _registry
