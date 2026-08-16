"""Broken-symlink resilience tests for the local storage backend.

A dangling ``app/build/intermediates -> /dev/shm/intermediates`` symlink on
the persistent workspace used to crash ``LocalStorageBackend.list_dir``
(``entry.stat()`` raised FileNotFoundError), which crashed
``_storage_walk_files`` and therefore find_files/search_files. list_dir must
skip unresolvable entries instead of failing the whole listing.
"""

from __future__ import annotations

from pathlib import Path
import uuid
from unittest.mock import patch

import pytest

from app.services.agent_tools import _storage_find_files, _storage_walk_files
from app.services.storage_runtime.local import LocalStorageBackend


@pytest.mark.asyncio
async def test_list_dir_skips_broken_symlink_and_returns_other_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "agents"
    root.mkdir()
    storage = LocalStorageBackend(str(root))

    agent_id = uuid.uuid4()
    ws = root / str(agent_id) / "workspace" / "app" / "build"
    ws.mkdir(parents=True)
    (ws / "build.gradle.kts").write_text("plugins {}")
    (ws / "outputs").mkdir()
    (ws / "intermediates").symlink_to("/nonexistent-target")

    entries = await storage.list_dir(f"{agent_id}/workspace/app/build")

    names = {entry.name: entry for entry in entries}
    assert set(names) == {"build.gradle.kts", "outputs"}
    assert names["build.gradle.kts"].is_dir is False
    assert names["outputs"].is_dir is True
    # 断链条目被跳过，而非整体失败或部分返回
    assert "intermediates" not in names


@pytest.mark.asyncio
async def test_walk_over_broken_symlink_returns_real_files(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    root.mkdir()
    storage = LocalStorageBackend(str(root))

    agent_id = uuid.uuid4()
    base = root / str(agent_id)
    build = base / "workspace" / "app" / "build"
    build.mkdir(parents=True)
    (build / "build.gradle.kts").write_text("plugins {}")
    outputs = build / "outputs"
    outputs.mkdir()
    (outputs / "app-debug.apk").write_text("artifact")
    (build / "intermediates").symlink_to("/nonexistent-target")

    entries = await _storage_walk_files(storage, str(agent_id))

    keys = [entry.key for entry in entries]
    assert f"{agent_id}/workspace/app/build/build.gradle.kts" in keys
    assert f"{agent_id}/workspace/app/build/outputs/app-debug.apk" in keys
    assert all("intermediates" not in key for key in keys)


@pytest.mark.asyncio
async def test_find_files_survives_broken_symlink_in_workspace(
    tmp_path: Path,
) -> None:
    """The incident scenario: workspace-wide find must not crash on a dangling link."""
    storage = LocalStorageBackend(str(tmp_path))
    agent_id = uuid.uuid4()
    build = tmp_path / str(agent_id) / "workspace" / "app" / "build"
    build.mkdir(parents=True)
    (build / "build.gradle.kts").write_text("plugins {}")
    (build / "intermediates").symlink_to("/nonexistent-target")

    with patch("app.services.agent_tools.get_storage_backend", return_value=storage):
        result = await _storage_find_files(
            agent_id,
            "**/build.gradle.kts",
            path="workspace",
        )

    assert "build.gradle.kts" in result
    assert "intermediates" not in result
