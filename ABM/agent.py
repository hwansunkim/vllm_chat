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
- move_to: 이동할 위치 이름 (이동 없으면 null)
- update_appearance: 외모 변화가 있을 때 새 외모 전체 묘사 (없으면 null)
- target: 반드시 아래 시스템 ID만 사용 (표시 이름 절대 금지):
<TARGETS><TARGETS_FOOTER>
⚠ content 필드는 반드시 한국어로만 작성하십시오. 외국어·한자 사용 금지.
"""


def _build_output_format(
    available_targets: list[str],
    extra_fields: list[dict],
    key_to_alias: dict[str, str] | None = None,
    template: str | None = None,
    target_sections: list[tuple[str, list[str]]] | None = None,
    location_name: str = "",
    situation_targets: bool = False,
) -> str:
    """Dynamically build the JSON output format instruction from configured fields."""
    if situation_targets:
        # 위치 기반 모드: 대화 상대는 상황 컨텍스트에서 ID 포함하여 제공됨
        targets_block = "  ([현재 상황] 컨텍스트에서 대화 상대와 ID를 확인하세요)\n"
    elif target_sections:
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

    _FIELD_DESCS: dict[str, str] = {
        "action_note": "행동이나 생각 묘사. 이 내용은 다른 에이전트에게 **시각적 정보**로 전달됨.",
    }

    def _line(f: dict) -> str:
        if f["name"] == "action_note":
            return '    "action_note": "행동이나 생각, 상황 묘사. 텍스트로 서술. 예: \'한숨을 쉰다\', \'눈을 흘김\'",'
        return f'    "{f["name"]}": "{f["default"]}",'

    def _hint(f: dict) -> str:
        desc = _FIELD_DESCS.get(f["name"])
        if desc:
            return f'- {f["name"]}: {desc}'
        return f'- {f["name"]}: 적절한 값 (기본값 예시: "{f["default"]}")'

    tmpl = template if template is not None else DEFAULT_OUTPUT_FORMAT_TEMPLATE
    # 템플릿에 action_note가 이미 하드코딩돼 있으면 <FIELD_LINES>/<FIELD_HINTS>에서 제외 (중복 방지)
    hardcoded = {name for name in ("action_note",) if f'"{name}"' in tmpl}
    active_fields = [f for f in extra_fields if f["name"] not in hardcoded]

    field_lines = "\n".join(_line(f) for f in active_fields)
    field_hints = "\n".join(_hint(f) for f in active_fields)

    # 그룹이 2개 이상일 때 그룹별 단축 표기 추가 (브릿지 에이전트용)
    named_sections = [
        label for label, _ in (target_sections or [])
        if label != "기타"
    ]
    if len(named_sections) >= 2:
        group_shortcuts = " / ".join(
            f'[{label}] 전체: "group:{label}"' for label in named_sections
        )
        targets_footer = f'  {group_shortcuts} / 모두에게: "all" / 혼잣말·독백·탄식 등: "self"\n'
    else:
        targets_footer = '  전체에게: "all" / 혼잣말·독백·탄식 등: "self"\n'

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
        location_name: str = "",
        situation_targets: bool = False,
    ) -> dict:
        return {
            "role": "system",
            "content": self.system_prompt + _build_output_format(
                available_targets, self._extra_fields, key_to_alias,
                template=self._output_format_template,
                target_sections=target_sections,
                location_name=location_name,
                situation_targets=situation_targets,
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
    ) -> int:
        """Estimate total prompt tokens for the next LLM call."""
        msgs = self.build_messages(background_log, available_targets, key_to_alias, target_sections, location_name, situation_targets)
        return sum(_msg_tokens(m) for m in msgs)

    def trim_to_token_limit(
        self,
        background_log: list,
        available_targets: list[str],
        key_to_alias: dict[str, str] | None = None,
        target_sections: list[tuple[str, list[str]]] | None = None,
        location_name: str = "",
        situation_targets: bool = False,
    ):
        """Remove oldest memory messages until estimated tokens fit within _token_limit."""
        while self.memory:
            if self.estimate_context_tokens(background_log, available_targets, key_to_alias, target_sections, location_name, situation_targets) <= self._token_limit:
                break
            self.memory.pop(0)
            self._trimmed_count += 1

        # Warn when system+background alone exceeds the limit — trimming can't help further.
        if not self.memory:
            est = self.estimate_context_tokens(background_log, available_targets, key_to_alias, target_sections, location_name, situation_targets)
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
    ) -> list:
        """[system] + background_log + [memory_block?] + agent memory"""
        msgs = [self.get_system_message(available_targets, key_to_alias, target_sections, location_name, situation_targets)] + background_log
        if self._memory_block:
            msgs.append({"role": "user", "content": self._memory_block})
        msgs.extend(self.memory)
        return msgs
