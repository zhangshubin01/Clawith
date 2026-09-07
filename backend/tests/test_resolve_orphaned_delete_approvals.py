"""Tests for app/scripts/resolve_orphaned_delete_approvals.py.

Pure-function coverage for the orphan-target filter: only pending, agent-scoped
delete_files approvals are eligible; group-scoped and malformed rows are never
touched (group-scoped deletes still flow through the live L3 flow).
"""

from app.models.audit import ApprovalRequest
from app.scripts import resolve_orphaned_delete_approvals as s

import uuid


def _approval(workspace_scope) -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        action_type="delete_files",
        status="pending",
        details={
            "tool": "delete_file",
            "args": {"path": "workspace/remove-me.md"},
            "runtime_scope": {
                "tenant_id": str(uuid.uuid4()),
                "run_id": str(uuid.uuid4()),
                "workspace_scope": workspace_scope,
                "tool_call_id": "call-delete",
            },
        },
    )


def test_agent_scoped_is_target() -> None:
    assert s._is_orphan_target(_approval("agent")) is True


def test_group_scoped_is_not_target() -> None:
    assert s._is_orphan_target(_approval("group")) is False


def test_missing_runtime_scope_is_not_target() -> None:
    approval = ApprovalRequest(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        action_type="delete_files",
        status="pending",
        details={"tool": "delete_file", "args": {"path": "workspace/remove-me.md"}},
    )
    assert s._is_orphan_target(approval) is False


def test_empty_details_is_not_target() -> None:
    approval = ApprovalRequest(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        action_type="delete_files",
        status="pending",
        details={},
    )
    assert s._is_orphan_target(approval) is False
