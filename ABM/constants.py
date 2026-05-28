"""Shared constants for the ABM package.

Centralizes values that were previously duplicated across modules (agent.py, parser.py).
"""

# Default extra metadata fields emitted alongside each agent utterance.
# Each entry: {"name": <field>, "default": <fallback value when LLM omits it>}.
# Keep this list in sync with the agent system-prompt OUTPUT FORMAT — see
# DEFAULT_OUTPUT_FORMAT_TEMPLATE in ABM/agent.py.
DEFAULT_EXTRA_FIELDS: list[dict] = [
    {"name": "emotion",     "default": "neutral"},
    {"name": "action",      "default": "speak"},
    {"name": "action_note", "default": ""},
]
