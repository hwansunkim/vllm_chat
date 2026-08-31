from __future__ import annotations

import asyncio
import logging
import sqlite3

from .. import config
from .providers.anthropic import AnthropicProvider
from .providers.base import LLMProvider
from .providers.openai import OpenAIProvider
from .providers.vllm import VLLMProvider

logger = logging.getLogger(__name__)

_PROVIDER_CLASSES: dict[str, type] = {
    "vllm":      VLLMProvider,
    "openai":    OpenAIProvider,
    "anthropic": AnthropicProvider,
}
DEFAULT_PROVIDER_TYPE = "vllm"


class NoProviderError(RuntimeError):
    """사용 가능한 서버가 하나도 없을 때. 재시도해도 소용없는 영구 실패.

    `RuntimeError` 서브클래스이므로 기존 `except RuntimeError`(conversations.py)는
    그대로 동작한다. bridge 의 재시도 predicate 가 이 예외만 즉시 포기한다.
    """


def get_provider_class(provider_type: str) -> type | None:
    """provider_type 문자열 → 프로바이더 클래스. 알 수 없으면 None.

    registry 에 등록하지 않고 임시 인스턴스를 만들려는 호출자(예: 저장 전
    모델 목록 조회)를 위한 공개 조회 함수.
    """
    return _PROVIDER_CLASSES.get((provider_type or "").strip().lower())


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

    async def _create_provider(self, row: dict) -> LLMProvider:
        ptype = (row.get("provider_type") or DEFAULT_PROVIDER_TYPE).strip().lower()
        cls = _PROVIDER_CLASSES.get(ptype)
        if cls is None:
            # 알 수 없는 타입(구버전 DB/오타)은 서버 기동을 막지 않고 vllm 으로 폴백
            logger.warning(
                "[%s] 알 수 없는 provider_type=%r → '%s' 로 폴백합니다.",
                row.get("name"), ptype, DEFAULT_PROVIDER_TYPE,
            )
            cls = _PROVIDER_CLASSES[DEFAULT_PROVIDER_TYPE]

        p = cls(
            server_id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            model=row["model"],
            api_key=row.get("api_key", ""),
            enabled=bool(row["enabled"]),
            is_default=bool(row.get("is_default", False)),
            # thinking_level 이 소스 오브 트루스. 컬럼이 없는 구 row 는 bool 로 폴백.
            thinking_level=config.normalize_thinking_level(
                row.get("thinking_level"),
                default=config.normalize_thinking_level(row.get("thinking")),
            ),
            configured_max_len=int(row.get("max_model_len", 0)),
        )
        self._providers[row["id"]] = p
        return p

    async def close_all(self) -> None:
        for p in list(self._providers.values()):
            await p.close()
        self._providers.clear()

    async def register(self, row: dict) -> LLMProvider | None:
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

    def get_provider(self, server_id: str) -> LLMProvider | None:
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

        raise NoProviderError("사용 가능한 LLM 서버가 없습니다.")

    def _round_robin(self, key: str, candidates: list[LLMProvider]) -> LLMProvider:
        idx = self._rr_counters.get(key, 0) % len(candidates)
        self._rr_counters[key] = idx + 1
        return candidates[idx]


_registry = ServerRegistry()


def get_registry() -> ServerRegistry:
    return _registry
