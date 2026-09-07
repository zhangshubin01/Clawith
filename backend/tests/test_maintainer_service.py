"""Unit tests for the Maintainer gate decision helper.

Covers ``MaintainerService.resolve_file_modify_permission`` — the pure decision
function behind the gate wired into ``execute_tool`` (legacy) and
``execute_pending`` (durable). The DB-backed ``is_maintainer`` table lookup is
monkeypatched so the decision order can be asserted in isolation.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.services.maintainer_service import (
    FILE_MODIFY_TOOL_NAMES,
    FileModifyDecision,
    maintainer_service,
)


def _agent(*, creator_id: uuid.UUID | None = None) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        creator_id=creator_id or uuid.uuid4(),
        name="Gate Agent",
        status="idle",
        is_expired=False,
        access_mode="company",
    )


async def _is_maintainer_yes(_db, _agent_id, _user_id) -> bool:
    return True


async def _is_maintainer_no(_db, _agent_id, _user_id) -> bool:
    return False


@pytest.mark.asyncio
async def test_non_file_tool_is_not_gated() -> None:
    agent = _agent()
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="read_file",
        arguments={"path": "workspace/notes.md"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
    )
    assert decision is FileModifyDecision.NOT_GATED


@pytest.mark.asyncio
async def test_a2a_actor_agent_id_is_not_gated() -> None:
    agent = _agent()
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
        actor_agent_id=uuid.uuid4(),
    )
    assert decision is FileModifyDecision.NOT_GATED


@pytest.mark.asyncio
async def test_group_scoped_is_not_gated() -> None:
    agent = _agent()
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="delete_file",
        arguments={"path": "workspace/notes.md"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
        is_group_scoped=True,
    )
    assert decision is FileModifyDecision.NOT_GATED


@pytest.mark.asyncio
async def test_memory_path_is_not_gated() -> None:
    agent = _agent()
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="write_file",
        arguments={"path": "memory/notes.md", "content": "x"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
    )
    assert decision is FileModifyDecision.NOT_GATED


@pytest.mark.asyncio
async def test_soul_tasks_enterprise_defer() -> None:
    agent = _agent()
    actor = uuid.uuid4()
    for tool_name, arguments in (
        ("edit_file", {"path": "soul.md", "old_string": "a", "new_string": "b"}),
        ("delete_file", {"path": "tasks.json"}),
        ("write_file", {"path": "enterprise_info/x.md", "content": "x"}),
    ):
        decision = await maintainer_service.resolve_file_modify_permission(
            None,  # type: ignore[arg-type]
            tool_name=tool_name,
            arguments=arguments,
            agent=agent,
            actor_user_id=actor,
        )
        assert decision is FileModifyDecision.DEFER, (tool_name, arguments)


@pytest.mark.asyncio
async def test_creator_is_implicitly_allowed() -> None:
    agent = _agent()
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
        agent=agent,
        actor_user_id=agent.creator_id,
    )
    assert decision is FileModifyDecision.GATED_ALLOWED


@pytest.mark.asyncio
async def test_empty_actor_falls_back_to_creator(monkeypatch) -> None:
    agent = _agent()
    # Even if the table lookup would say "no", the creator fallback short-circuits
    # before it, so the table must NOT be consulted.
    monkeypatch.setattr(
        maintainer_service,
        "is_maintainer",
        _is_maintainer_no,
    )
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="edit_file",
        arguments={"path": "skills/SKILL.md", "old_string": "a", "new_string": "b"},
        agent=agent,
        actor_user_id=None,
    )
    assert decision is FileModifyDecision.GATED_ALLOWED


@pytest.mark.asyncio
async def test_non_maintainer_is_denied(monkeypatch) -> None:
    agent = _agent()
    monkeypatch.setattr(
        maintainer_service,
        "is_maintainer",
        _is_maintainer_no,
    )
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
    )
    assert decision is FileModifyDecision.GATED_DENIED


@pytest.mark.asyncio
async def test_listed_maintainer_is_allowed(monkeypatch) -> None:
    agent = _agent()
    monkeypatch.setattr(
        maintainer_service,
        "is_maintainer",
        _is_maintainer_yes,
    )
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="delete_file",
        arguments={"path": "skills/SKILL.md"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
    )
    assert decision is FileModifyDecision.GATED_ALLOWED


@pytest.mark.asyncio
async def test_actor_user_id_string_is_coerced(monkeypatch) -> None:
    agent = _agent()
    monkeypatch.setattr(
        maintainer_service,
        "is_maintainer",
        _is_maintainer_yes,
    )
    decision = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
        agent=agent,
        actor_user_id=str(uuid.uuid4()),
    )
    assert decision is FileModifyDecision.GATED_ALLOWED


@pytest.mark.asyncio
async def test_move_file_judges_both_paths(monkeypatch) -> None:
    agent = _agent()
    monkeypatch.setattr(
        maintainer_service,
        "is_maintainer",
        _is_maintainer_no,
    )
    # Destination gated: moving from memory into workspace must be denied.
    denied = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="move_file",
        arguments={"source_path": "memory/a.md", "destination_path": "workspace/b.md"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
    )
    assert denied is FileModifyDecision.GATED_DENIED

    # Source gated: moving from workspace into memory must be denied.
    denied_src = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="move_file",
        arguments={"source_path": "workspace/a.md", "destination_path": "memory/b.md"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
    )
    assert denied_src is FileModifyDecision.GATED_DENIED

    # Both open: memory -> memory is not gated.
    open_move = await maintainer_service.resolve_file_modify_permission(
        None,  # type: ignore[arg-type]
        tool_name="move_file",
        arguments={"source_path": "memory/a.md", "destination_path": "memory/b.md"},
        agent=agent,
        actor_user_id=uuid.uuid4(),
    )
    assert open_move is FileModifyDecision.NOT_GATED


def test_file_modify_tool_names_is_exactly_four() -> None:
    assert FILE_MODIFY_TOOL_NAMES == frozenset(
        {"delete_file", "edit_file", "write_file", "move_file"}
    )
