from __future__ import annotations

import asyncio
import sqlite3

from llm.providers.base import LLMProvider
from llm.providers.vllm import VLLMProvider


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
        self._rr_counters: dict[str, int] = {}  # model → round-robin 카운터
        self._lock = asyncio.Lock()

    # ── 초기화 / 생명주기 ─────────────────────────────────────────────────────

    async def load_from_db(self, conn: sqlite3.Connection) -> None:
        """앱 시작 시 DB의 enabled 서버를 모두 로드한다."""
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

    # ── 런타임 CRUD (API 핸들러에서 호출) ────────────────────────────────────

    async def register(self, row: dict) -> VLLMProvider:
        """서버 추가 또는 변경 시 호출. 기존 클라이언트는 종료 후 재생성."""
        async with self._lock:
            existing = self._providers.pop(row["id"], None)
            if existing:
                await existing.close()
            if not row.get("enabled", True):
                return None  # disabled 상태로 저장만, 메모리엔 미등록
            # is_default 변경 시 다른 서버의 is_default 플래그 해제
            if row.get("is_default"):
                for p in self._providers.values():
                    p.is_default = False
            return await self._create_provider(row)

    async def unregister(self, server_id: str) -> None:
        """서버 삭제 시 호출."""
        async with self._lock:
            p = self._providers.pop(server_id, None)
            if p:
                await p.close()

    def get_provider(self, server_id: str) -> VLLMProvider | None:
        return self._providers.get(server_id)

    def list_providers(self) -> list[LLMProvider]:
        return list(self._providers.values())

    # ── 라우팅 ───────────────────────────────────────────────────────────────

    def get_default(self) -> LLMProvider | None:
        """is_default=True 서버 우선, 없으면 첫 번째 enabled 서버."""
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
        """
        라우팅 우선순위에 따라 서버를 선택한다.
        enabled 서버가 없으면 RuntimeError.
        """
        # 1. 명시적 server_id
        if server_id:
            p = self._providers.get(server_id)
            if p:
                return p

        # 2. model_match + round-robin
        if model:
            candidates = [
                p for p in self._providers.values()
                if p.enabled and p.model == model
            ]
            if candidates:
                return self._round_robin(model, candidates)

        # 3. failover: default → 첫 번째 enabled
        default = self.get_default()
        if default:
            return default

        raise RuntimeError("사용 가능한 LLM 서버가 없습니다.")

    def _round_robin(self, key: str, candidates: list[LLMProvider]) -> LLMProvider:
        idx = self._rr_counters.get(key, 0) % len(candidates)
        self._rr_counters[key] = idx + 1
        return candidates[idx]


# ── 모듈 싱글톤 ───────────────────────────────────────────────────────────────

_registry = ServerRegistry()


def get_registry() -> ServerRegistry:
    return _registry
