"""Shared constants for the ABM package.

Centralizes values that were previously duplicated across modules (agent.py, parser.py).
"""

# Default extra metadata fields emitted alongside each agent utterance.
# Each entry: {"name": <field>, "default": <fallback value when LLM omits it>}.
# Keep this list in sync with the output-schema contract — see
# build_output_contract / DEFAULT_OUTPUT_FORMAT_TEMPLATE in ABM/prompt_contract.py.
# NOTE: backend/api/simulation/schemas.py has an equivalent DEFAULT_EXTRA_FIELDS
# (as ExtraField models) for the API layer; keep the two in step.
DEFAULT_EXTRA_FIELDS: list[dict] = [
    {"name": "emotion",     "default": "neutral"},
    {"name": "action",      "default": "speak"},
    {"name": "action_note", "default": ""},
]
