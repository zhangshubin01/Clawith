"""Tests for app.services.agent_workspace_cleanup."""

import uuid
from pathlib import Path

import pytest

from app.services.agent_workspace_cleanup import (
    agent_workspace_path,
    remove_agent_workspace,
)


@pytest.fixture()
def ws_root(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    root.mkdir()
    return root


def test_removes_existing_workspace(ws_root: Path) -> None:
    agent_id = uuid.uuid4()
    target = ws_root / str(agent_id)
    (target / "workspace" / "app").mkdir(parents=True)
    (target / "workspace" / "app" / "main.py").write_text("print('hi')")

    assert remove_agent_workspace(agent_id, root=ws_root) is True
    assert not target.exists()


def test_missing_workspace_is_noop(ws_root: Path) -> None:
    assert remove_agent_workspace(uuid.uuid4(), root=ws_root) is False


def test_refuses_symlinked_workspace(ws_root: Path) -> None:
    agent_id = uuid.uuid4()
    ws_root.mkdir(parents=True, exist_ok=True)
    outside = ws_root.parent / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("precious")
    link = ws_root / str(agent_id)
    link.symlink_to(outside)

    assert remove_agent_workspace(agent_id, root=ws_root) is False
    # The symlink and its target are both untouched.
    assert link.is_symlink()
    assert (outside / "data.txt").read_text() == "precious"


def test_target_is_strict_subdirectory_of_root() -> None:
    root = Path("/tmp/ws-root")
    target = agent_workspace_path(uuid.uuid4(), root=root)
    assert target.parent == root.resolve()
    assert target != root.resolve()


def test_non_dir_target_raises(ws_root: Path) -> None:
    agent_id = uuid.uuid4()
    (ws_root / str(agent_id)).write_text("a plain file")

    with pytest.raises(NotADirectoryError):
        remove_agent_workspace(agent_id, root=ws_root)
