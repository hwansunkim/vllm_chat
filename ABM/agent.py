import json
import time
import os
import logging
from datetime import datetime
from .config import MEMORY_LIMIT

logger = logging.getLogger(__name__)

_OUTPUT_FORMAT_TMPL = """

[Important Output Format]
당신의 응답은 반드시 다음 JSON 형식이어야 합니다. 다른 텍스트는 출력하지 마세요.
{{
    "content": "당신의 말투로 한 말",
    "emotion": "angry, happy, neutral, sad, etc.",
    "action": "yell, ask, ignore, etc.",
    "target": ["id1", "id2"] 또는 "all" 또는 "system"
}}

- emotion: 감정을 나타내는 영어 단어 (소문자)
- action: 행동을 나타내는 영어 단어 (소문자)
- target: 반드시 아래 시스템 ID만 사용 (한국어 이름 절대 금지):
{targets}
  전체에게: "all" / 혼잣말·행동묘사: "system"
"""


def _build_output_format(available_targets: list[str], key_to_alias: dict[str, str] | None = None) -> str:
    lines = []
    for t in available_targets:
        alias = (key_to_alias or {}).get(t, "")
        lines.append(f'  - ID: "{t}"' + (f'  ({alias})' if alias else ""))
    targets_block = "\n".join(lines) if lines else "  (없음)"
    return _OUTPUT_FORMAT_TMPL.format(targets=targets_block)


class Agent:
    def __init__(self, name: str, system_prompt: str, log_dir: str, memory_limit: int = MEMORY_LIMIT):
        self.name = name
        self.system_prompt = system_prompt
        self.log_file = os.path.join(log_dir, f"{name}.json")
        self.memory: list[dict] = []
        self._log_buffer: list[dict] = []
        self._memory_limit = memory_limit
        self._total_added: int = 0  # 한도 초과로 잘린 것 포함 총 추가 횟수
        self._init_log_file()

    def _init_log_file(self):
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        with open(self.log_file, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)

    def add_to_log(self, content="", reasoning="", emotion="", action="", targets=None):
        if targets is None:
            targets = []
        self._log_buffer.append({
            "timestamp":    time.time(),
            "datetime_str": datetime.now().isoformat(),
            "content":      content,
            "reasoning":    reasoning,
            "emotion":      emotion,
            "action":       action,
            "targets":      targets,
        })
        with open(self.log_file, 'w', encoding='utf-8') as f:
            json.dump(self._log_buffer, f, ensure_ascii=False, indent=2)

    def get_system_message(self, available_targets: list[str], key_to_alias: dict[str, str] | None = None) -> dict:
        return {"role": "system", "content": self.system_prompt + _build_output_format(available_targets, key_to_alias)}

    def add_to_memory(self, message: dict):
        self._total_added += 1
        self.memory.append(message)
        if len(self.memory) > self._memory_limit:
            self.memory.pop(0)

    def build_messages(self, background_log: list, available_targets: list[str], key_to_alias: dict[str, str] | None = None) -> list:
        """[system] + background_log(공통 장면 묘사) + 에이전트 개별 memory"""
        return [self.get_system_message(available_targets, key_to_alias)] + background_log + self.memory
