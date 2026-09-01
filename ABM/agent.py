import json
import time
import os
import logging
from datetime import datetime
from .config import TOKEN_LIMIT
from .constants import DEFAULT_EXTRA_FIELDS as _DEFAULT_EXTRA_FIELDS
from .prompt_contract import (
    DEFAULT_OUTPUT_FORMAT_TEMPLATE,
    build_output_contract,
)

logger = logging.getLogger(__name__)

_RESERVED_LOG_KEYS = frozenset({"timestamp", "datetime_str", "content", "reasoning", "targets"})

# 출력 계약(JSON 스키마 / move_to 의미 / target ID 규칙)의 정본은 이제
# `ABM/prompt_contract.py`다 — 엔진이 소유하고 실행 시 생성하는 "계약 층".
# 아래 두 이름은 기존 import 경로(`from ABM.agent import ...`)를 위한 재노출이다.
__all__ = ["Agent", "DEFAULT_OUTPUT_FORMAT_TEMPLATE", "_build_output_format"]

# 구 이름 alias. 새 코드는 `prompt_contract.build_output_contract`를 직접 쓸 것.
_build_output_format = build_output_contract


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: UTF-8 bytes / 4 (conservative for Korean/English mixed text)."""
    return max(1, len(text.encode("utf-8")) // 4)


def _msg_tokens(msg: dict) -> int:
    return _estimate_tokens(msg.get("content", "")) + 4  # +4 for role/formatting overhead


class Agent:
    def __init__(
        self,
        name: str,
        system_prompt: str,
        log_dir: str,
        token_limit: int = TOKEN_LIMIT,
        extra_fields: list[dict] | None = None,
        output_format_template: str | None = None,
    ):
        self.name          = name
        self.system_prompt = system_prompt
        self.log_file      = os.path.join(log_dir, f"{name}.json")
        self.memory: list[dict] = []
        self._log_buffer: list[dict] = []
        self._token_limit              = token_limit
        self._extra_fields             = extra_fields if extra_fields is not None else list(_DEFAULT_EXTRA_FIELDS)
        self._output_format_template   = output_format_template
        # ── 엔진 계약 층 ──────────────────────────────────────────────────────
        # `system_prompt`는 **사용자 소유**(페르소나 + 배경)로 순수하게 남긴다.
        # 지도/시간/감염 같은 정적 엔진 규칙은 여기에 따로 담기고, 시뮬레이션이
        # `set_engine_contract()`로 채워 넣는다. 이렇게 분리해야 (1) 인터뷰처럼
        # 페르소나만 필요한 경로가 계약을 상속하지 않고, (2) 같은 Agent 객체를
        # 다시 초기화해도 계약이 중복 누적되지 않는다(예전 `+=` 주입의 버그).
        self.engine_contract: str = ""
        self._has_location_graph: bool = False
        self._has_zone: bool = False
        self._trimmed_count: int  = 0
        self._total_added:   int  = 0
        self._last_prompt_tokens: int | None = None
        self._memory_block: str | None = None   # populated after compression
        self._init_log_file()

    def _init_log_file(self):
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        with open(self.log_file, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)

    def add_to_log(self, content="", reasoning="", extra: dict | None = None, targets=None):
        if targets is None:
            targets = []
        safe_extra = {k: v for k, v in (extra or {}).items() if k not in _RESERVED_LOG_KEYS}
        if extra and len(safe_extra) < len(extra):
            dropped = set(extra) - set(safe_extra)
            logger.warning(f"[{self.name}] extra fields {dropped} conflict with reserved log keys, ignored")
        self._log_buffer.append({
            "timestamp":    time.time(),
            "datetime_str": datetime.now().isoformat(),
            "content":      content,
            "reasoning":    reasoning,
            "targets":      targets,
            **safe_extra,
        })
        with open(self.log_file, 'w', encoding='utf-8') as f:
            json.dump(self._log_buffer, f, ensure_ascii=False, indent=2)

    def set_engine_contract(
        self,
        world_contract: str,
        *,
        has_location_graph: bool = False,
        has_zone: bool = False,
    ) -> None:
        """시뮬레이션이 소유한 정적 계약 블록(지도/시간/감염)을 이 에이전트에 건다.

        `+=`가 아니라 **대입**이다 — 같은 Agent를 두 번 초기화해도 계약이 두 번
        붙지 않는다. `has_*` 플래그는 출력 계약의 `move_to` 문구를 조건부로
        만드는 데 쓰인다(그래프 없는 시나리오에 rendezvous 안내가 새지 않도록).
        """
        self.engine_contract = world_contract or ""
        self._has_location_graph = bool(has_location_graph)
        self._has_zone = bool(has_zone)

    def get_system_message(
        self,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
        target_sections: list[tuple[str, list[str]]] | None = None,
        location_name: str = "",
        situation_targets: bool = False,
    ) -> dict:
        """[사용자 페르소나] + [엔진 정적 계약] + [엔진 출력 계약] (계약이 맨 뒤).

        출력 계약은 매 턴 타깃이 달라지므로 여기서 새로 만든다.
        `_output_format_template`(사용자 오버라이드)이 명시적으로 주어졌을 때만
        그걸 쓰고, 아니면 언제나 엔진이 현재 설정으로 생성한다.
        """
        return {
            "role": "system",
            "content": self.system_prompt + self.engine_contract + build_output_contract(
                available_targets, self._extra_fields, key_to_alias,
                template=self._output_format_template,
                target_sections=target_sections,
                location_name=location_name,
                situation_targets=situation_targets,
                has_location_graph=self._has_location_graph,
                has_zone=self._has_zone,
            ),
        }

    def add_to_memory(self, message: dict):
        self._total_added += 1
        self.memory.append(message)

    def estimate_context_tokens(
        self,
        background_log: list,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
        target_sections: list[tuple[str, list[str]]] | None = None,
        location_name: str = "",
        situation_targets: bool = False,
        ephemeral_msgs: list[dict] | None = None,
    ) -> int:
        """Estimate total prompt tokens for the next LLM call."""
        msgs = self.build_messages(
            background_log, available_targets, key_to_alias, target_sections,
            location_name, situation_targets, ephemeral_msgs,
        )
        return sum(_msg_tokens(m) for m in msgs)

    def trim_to_token_limit(
        self,
        background_log: list,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
        target_sections: list[tuple[str, list[str]]] | None = None,
        location_name: str = "",
        situation_targets: bool = False,
        ephemeral_msgs: list[dict] | None = None,
    ):
        """Remove oldest memory messages until estimated tokens fit within _token_limit."""
        while self.memory:
            if self.estimate_context_tokens(
                background_log, available_targets, key_to_alias, target_sections,
                location_name, situation_targets, ephemeral_msgs,
            ) <= self._token_limit:
                break
            self.memory.pop(0)
            self._trimmed_count += 1

        # Warn when system+background alone exceeds the limit — trimming can't help further.
        if not self.memory:
            est = self.estimate_context_tokens(
                background_log, available_targets, key_to_alias, target_sections,
                location_name, situation_targets, ephemeral_msgs,
            )
            if est > self._token_limit:
                logger.warning(
                    f"[{self.name}] Context exceeds token_limit even with empty memory "
                    f"({est} est. tokens > {self._token_limit}). "
                    "Shorten system_prompt or background_log."
                )

    def build_messages(
        self,
        background_log: list,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
        target_sections: list[tuple[str, list[str]]] | None = None,
        location_name: str = "",
        situation_targets: bool = False,
        ephemeral_msgs: list[dict] | None = None,
    ) -> list:
        """[system] + background_log + [memory_block?] + agent memory + [ephemeral_msgs]"""
        msgs = [self.get_system_message(available_targets, key_to_alias, target_sections, location_name, situation_targets)] + background_log
        if self._memory_block:
            msgs.append({"role": "user", "content": self._memory_block})
        msgs.extend(self.memory)
        if ephemeral_msgs:
            msgs.extend(ephemeral_msgs)
        return msgs
