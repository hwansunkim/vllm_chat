import json
import time
import os
import logging
from datetime import datetime
from .config import TOKEN_LIMIT
from .constants import DEFAULT_EXTRA_FIELDS as _DEFAULT_EXTRA_FIELDS

logger = logging.getLogger(__name__)

_RESERVED_LOG_KEYS = frozenset({"timestamp", "datetime_str", "content", "reasoning", "targets"})

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
<TARGETS><TARGETS_FOOTER>
"""


def _build_output_format(
    available_targets: list[str],
    extra_fields: list[dict],
    key_to_alias: dict[str, str] | None = None,
    template: str | None = None,
    target_sections: list[tuple[str, list[str]]] | None = None,
) -> str:
    """Dynamically build the JSON output format instruction from configured fields."""
    if target_sections:
        parts: list[str] = []
        for section_label, members in target_sections:
            parts.append(f"[{section_label}]")
            for t in members:
                alias = (key_to_alias or {}).get(t, "")
                parts.append(f'  - ID: "{t}"' + (f'  ({alias})' if alias else ""))
        targets_block = ("\n".join(parts) + "\n") if parts else "  (없음)\n"
    else:
        lines: list[str] = []
        for t in available_targets:
            alias = (key_to_alias or {}).get(t, "")
            lines.append(f'  - ID: "{t}"' + (f'  ({alias})' if alias else ""))
        targets_block = ("\n".join(lines) + "\n") if lines else "  (없음)\n"

    # Special-purpose descriptions for reserved fields.
    _FIELD_DESCS: dict[str, str] = {
        "action_note": (
            "말 이외의 신체 동작·표정·제스처를 간략히 서술 "
            "(예: '책상을 두드리며', '미소를 지으며'). "
            "비언어적 행동이 없으면 빈 문자열."
        ),
    }

    def _line(f: dict) -> str:
        if f["name"] == "action_note":
            return '    "action_note": "비언어적 행동 묘사 (없으면 빈 문자열)",'
        return f'    "{f["name"]}": "{f["default"]}",'

    def _hint(f: dict) -> str:
        desc = _FIELD_DESCS.get(f["name"])
        if desc:
            return f'- {f["name"]}: {desc}'
        return f'- {f["name"]}: 적절한 값 (기본값 예시: "{f["default"]}")'

    field_lines = "\n".join(_line(f) for f in extra_fields)
    field_hints = "\n".join(_hint(f) for f in extra_fields)

    # 그룹이 2개 이상일 때 그룹별 단축 표기 추가 (브릿지 에이전트용)
    named_sections = [
        label for label, _ in (target_sections or [])
        if label != "기타"
    ]
    if len(named_sections) >= 2:
        group_shortcuts = " / ".join(
            f'[{label}] 전체: "group:{label}"' for label in named_sections
        )
        targets_footer = f'  {group_shortcuts} / 모두에게: "all" / 혼잣말·내면 행동: "system"\n'
    else:
        targets_footer = '  전체에게: "all" / 혼잣말·내면 행동: "system"\n'

    tmpl = template if template is not None else DEFAULT_OUTPUT_FORMAT_TEMPLATE
    return (
        tmpl
        .replace("<FIELD_LINES>", field_lines)
        .replace("<FIELD_HINTS>", field_hints)
        .replace("<TARGETS>", targets_block)
        .replace("<TARGETS_FOOTER>", targets_footer)
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

    def get_system_message(
        self,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
        target_sections: list[tuple[str, list[str]]] | None = None,
    ) -> dict:
        return {
            "role": "system",
            "content": self.system_prompt + _build_output_format(
                available_targets, self._extra_fields, key_to_alias,
                template=self._output_format_template,
                target_sections=target_sections,
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
    ) -> int:
        """Estimate total prompt tokens for the next LLM call."""
        msgs = self.build_messages(background_log, available_targets, key_to_alias, target_sections)
        return sum(_msg_tokens(m) for m in msgs)

    def trim_to_token_limit(
        self,
        background_log: list,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
        target_sections: list[tuple[str, list[str]]] | None = None,
    ):
        """Remove oldest memory messages until estimated tokens fit within _token_limit."""
        while self.memory:
            if self.estimate_context_tokens(background_log, available_targets, key_to_alias, target_sections) <= self._token_limit:
                break
            self.memory.pop(0)
            self._trimmed_count += 1

        # Warn when system+background alone exceeds the limit — trimming can't help further.
        if not self.memory:
            est = self.estimate_context_tokens(background_log, available_targets, key_to_alias, target_sections)
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
    ) -> list:
        """[system] + background_log + [memory_block?] + agent memory"""
        msgs = [self.get_system_message(available_targets, key_to_alias, target_sections)] + background_log
        if self._memory_block:
            msgs.append({"role": "user", "content": self._memory_block})
        msgs.extend(self.memory)
        return msgs
