# ABM Engineer — Done

## Imports verified
- `from ABM.db import SimDB` -> OK
- `from ABM.simulation import Simulation` -> OK
- `from ABM.constants import DEFAULT_EXTRA_FIELDS` -> OK

## Changed files

### Modified
- `ABM/agent.py` — `_DEFAULT_EXTRA_FIELDS` definition removed; now `from .constants import DEFAULT_EXTRA_FIELDS as _DEFAULT_EXTRA_FIELDS`.
- `ABM/parser.py` — `_DEFAULT_EXTRA_FIELDS` definition removed; same import shim as agent.py.
- `ABM/simulation.py`
  - S7 fix: `_compress_agent(active_agent, agent_key, turn)` -> `_compress_agent(active_agent, agent_key, wave)`.
  - S5 fix: `agent_exit` event handler now calls `self._pending_wave.pop(agent_key, None)` after `discard`.
  - S4 fix: `_resolve_targets` and `_resolve_event_targets` use `t.strip().lower()` instead of `t.lower()`.
  - H4: `_step_agent` (143 lines) decomposed into:
    - `_inject_incoming(agent, incoming) -> list[dict]`
    - `_maybe_compress(agent, agent_key, wave, other_agents)`
    - `_call_llm_for_agent(agent, agent_key) -> (content, reasoning, usage, error)`
    - `_apply_turn_result(agent, agent_key, raw, reasoning, usage, wave, turn, est_tokens) -> dict`
    - `_rollback_incoming(agent, incoming_msgs)` — helper to undo injected messages on LLM failure.
    - `_step_agent` now a thin coordinator.

### Added
- `ABM/constants.py` — `DEFAULT_EXTRA_FIELDS` central list (single source of truth for agent + parser).
- `ABM/db/__init__.py` — re-exports `SimDB` so `from ABM.db import SimDB` keeps working.
- `ABM/db/base.py` — `SimDB` class composed from mixins; owns `__init__` (schema apply + migrate) and the cross-domain `get_full_memory`.
- `ABM/db/schema.py` — `SCHEMA` DDL string and `migrate(conn)` function.
- `ABM/db/conn.py` — `ConnMixin._conn()` thread-local sqlite3 connection helper.
- `ABM/db/messages.py` — `MessagesMixin`: `save_messages`.
- `ABM/db/episodic.py` — `EpisodicMixin`: `upsert_episodes`, `get_episodes`.
- `ABM/db/semantic.py` — `SemanticMixin`: `upsert_facts`, `get_facts` (+ `CONFIDENCE_UPDATE_THRESHOLD`).
- `ABM/db/relationship.py` — `RelationshipMixin`: `upsert_relationships`, `get_relationships` (history rows still written here).
- `ABM/db/self_state.py` — `SelfStateMixin`: `upsert_self_state`, `get_self_state`.
- `ABM/db/compression.py` — `CompressionMixin`: `log_compression`.
- `ABM/db/runs.py` — `RunsMixin`: `create_run`, `finish_run`, `get_runs`, `get_run`, `log_turn`, `get_run_log`, `save_agent_snapshots`, `get_agent_snapshots`, and the D1-fixed `delete_run` (memory tables on `sim_id`, run-scoped tables on `run_id`).

### Removed
- `ABM/db.py` — replaced by the `ABM/db/` package (same public surface via `__init__.py`).

## Notes for downstream
- Public API unchanged: `from ABM.db import SimDB` and every method previously on `SimDB` is still callable.
- `_DEFAULT_EXTRA_FIELDS` is still importable from `ABM.agent` and `ABM.parser` (now aliases pointing at the shared list in `ABM.constants`).
- `_step_agent` external contract unchanged: same args, same result dict shape; new helpers are internal.
