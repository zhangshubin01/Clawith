"""Regression guard for the single ``_TOOL_AUTONOMY_MAP`` fact source.

History (2026-09-05): ``_TOOL_AUTONOMY_MAP`` was defined TWICE at module level
in ``agent_tools.py`` (a 9-key "intent" dict shadowed by a later 5-key "effective"
dict). Python resolves module-level re-assignment to the LAST value, so the 9-key
dict never took effect and six tools silently lost their autonomy lookup. The fix
deleted the dead 9-key dict and kept the 5-key one as the sole definition.

Because the re-assignment is resolved at import time, a unit test can never observe
the *intermediate* duplicate — but asserting the *final* content against the known
5-key set will fail loudly if anyone reintroduces a second dict that overwrites the
effective one (the final value would then deviate from the expected 5 keys).
"""

from app.models.agent import DEFAULT_AUTONOMY_POLICY
from app.services.agent_tools import _TOOL_AUTONOMY_MAP

# The single, effective map — legacy ``execute_tool`` seam's only source of
# tool_name -> autonomy action_type. File tools (write/edit/delete/move) will be
# gated by ``resolve_file_modify_permission`` (Maintainer gate) instead of this map;
# send_*/web_search/execute_code_e2b are typed or A2A-settled and never reach it.
EXPECTED_MAP = {
    "write_file": "write_workspace_files",
    "edit_file": "write_workspace_files",
    "delete_file": "delete_files",
    "execute_code": "execute_code",
    "execute_command": "execute_code",
}


def test_tool_autonomy_map_is_exactly_the_effective_five_keys() -> None:
    assert _TOOL_AUTONOMY_MAP == EXPECTED_MAP


def test_tool_autonomy_map_values_are_known_policy_keys() -> None:
    """Every action_type the map points at must be a real autonomy_policy key."""
    assert set(_TOOL_AUTONOMY_MAP.values()) <= set(DEFAULT_AUTONOMY_POLICY)
