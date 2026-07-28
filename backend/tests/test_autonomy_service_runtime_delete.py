"""Runtime-specific approval execution tests."""

from contextlib import asynccontextmanager
import uuid

import pytest

from app.services import autonomy_service as autonomy_module
from app.services import group_file_service


@pytest.mark.asyncio
async def test_approved_group_workspace_delete_keeps_original_scope(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    group_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    delete_calls: list[dict] = []

    class _DB:
        def __init__(self) -> None:
            self.committed = False

        async def commit(self) -> None:
            self.committed = True

    db = _DB()

    @asynccontextmanager
    async def session_factory():
        yield db

    async def delete_workspace_file(db_arg, **kwargs):
        assert db_arg is db
        delete_calls.append(kwargs)

    async def forbidden_direct_executor(*args, **kwargs):
        raise AssertionError(
            f"Group approval used Agent Workspace executor: {args}, {kwargs}"
        )

    monkeypatch.setattr(
        autonomy_module,
        "async_session",
        session_factory,
        raising=False,
    )
    monkeypatch.setattr(
        group_file_service,
        "delete_workspace_file",
        delete_workspace_file,
    )
    monkeypatch.setattr(
        "app.services.agent_tools._execute_tool_direct",
        forbidden_direct_executor,
    )

    result = await autonomy_module.AutonomyService()._execute_approved_action(
        agent_id,
        "delete_files",
        {
            "tool": "delete_file",
            "args": {
                "path": "workspace/remove-me.md",
                "workspace_scope": "group",
            },
            "runtime_scope": {
                "tenant_id": str(tenant_id),
                "run_id": str(uuid.uuid4()),
                "session_id": str(session_id),
                "workspace_scope": "group",
                "group_id": str(group_id),
                "actor_participant_id": str(participant_id),
                "workspace_path": "remove-me.md",
            },
        },
    )

    assert result == "✅ Deleted remove-me.md from Group Workspace"
    assert delete_calls == [
        {
            "tenant_id": tenant_id,
            "group_id": group_id,
            "actor_participant_id": participant_id,
            "path": "remove-me.md",
            "expected_version_token": None,
            "session_id": session_id,
        }
    ]
    assert db.committed is True
