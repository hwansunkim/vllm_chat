import json
import time
import os
import logging
from datetime import datetime
from .config import TOKEN_LIMIT

logger = logging.getLogger(__name__)

_RESERVED_LOG_KEYS = frozenset({"timestamp", "datetime_str", "content", "reasoning", "targets"})

_DEFAULT_EXTRA_FIELDS = [
    {"name": "emotion",     "default": "neutral"},
    {"name": "action",      "default": "speak"},
    {"name": "action_note", "default": ""},
]

DEFAULT_OUTPUT_FORMAT_TEMPLATE = """

[Important Output Format]
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 출력하지 마세요.
{
    "content": "당신의 말이나 행동을 자신의 말투로",
<FIELD_LINES>
    "target": ["id1", "id2"] 또는 "all" 또는 "system"
}

- content: 말하거나 행동하는 내용
<FIELD_HINTS>
- target: 반드시 아래 시스템 ID만 사용 (표시 이름 절대 금지):
<TARGETS>  전체에게: "all" / 혼잣말·내면 행동: "system"
"""


def _build_output_format(
    available_targets: list[str],
    extra_fields: list[dict],
    key_to_alias: dict[str, str] | None = None,
    template: str | None = None,
) -> str:
    """Dynamically build the JSON output format instruction from configured fields."""
    lines = []
    for t in available_targets:
        alias = (key_to_alias or {}).get(t, "")
        lines.append(f'  - ID: "{t}"' + (f'  ({alias})' if alias else ""))
    targets_block = ("\n".join(lines) + "\n") if lines else "  (없음)\n"

    field_lines = "\n".join(
        f'    "{f["name"]}": "{f["default"]}",'
        for f in extra_fields
    )
    field_hints = "\n".join(
        f'- {f["name"]}: 적절한 값 (기본값 예시: "{f["default"]}")'
        for f in extra_fields
    )

    tmpl = template if template is not None else DEFAULT_OUTPUT_FORMAT_TEMPLATE
    return (
        tmpl
        .replace("<FIELD_LINES>", field_lines)
        .replace("<FIELD_HINTS>", field_hints)
        .replace("<TARGETS>", targets_block)
    )


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
        self._trimmed_count: int  = 0
        self._total_added:   int  = 0
        self._last_prompt_tokens: int | None = None
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

    def get_system_message(
        self,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
    ) -> dict:
        return {
            "role": "system",
            "content": self.system_prompt + _build_output_format(
                available_targets, self._extra_fields, key_to_alias,
                template=self._output_format_template,
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
    ) -> int:
        """Estimate total prompt tokens for the next LLM call."""
        msgs = self.build_messages(background_log, available_targets, key_to_alias)
        return sum(_msg_tokens(m) for m in msgs)

    def trim_to_token_limit(
        self,
        background_log: list,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
    ):
        """Remove oldest memory messages until estimated tokens fit within _token_limit."""
        while self.memory:
            if self.estimate_context_tokens(background_log, available_targets, key_to_alias) <= self._token_limit:
                break
            self.memory.pop(0)
            self._trimmed_count += 1

        # Warn when system+background alone exceeds the limit — trimming can't help further.
        if not self.memory:
            est = self.estimate_context_tokens(background_log, available_targets, key_to_alias)
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
    ) -> list:
        """[system] + background_log + agent memory"""
        return [self.get_system_message(available_targets, key_to_alias)] + background_log + self.memory
