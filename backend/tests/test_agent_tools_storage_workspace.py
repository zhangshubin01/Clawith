import asyncio
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from app.services import agent_tools
from app.services import workspace_collaboration
from app.services.sandbox.local.run_workspace import (
    RunWorkspaceIdentity,
    close_run_workspace,
    refresh_run_workspace_path,
    use_run_workspace,
)
from app.services.sandbox.run_scope import sandbox_run_scope_id
from app.services.storage_runtime.base import StorageBackend, StorageEntry, StorageVersion, WriteCondition, ConditionalWriteResult


@asynccontextmanager
async def _noop_workspace_locks(*_args, **_kwargs):
    yield


@pytest.fixture(autouse=True)
def _isolate_storage_semantics_from_distributed_locking(monkeypatch):
    """These in-memory storage tests do not exercise the Redis lock backend."""
    monkeypatch.setattr(agent_tools, "workspace_locks", _noop_workspace_locks)
    monkeypatch.setattr(
        workspace_collaboration,
        "workspace_locks",
        _noop_workspace_locks,
    )


class MemoryStorageBackend(StorageBackend):
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.versions = {key: 1 for key in self.files}

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.files)

    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries: dict[str, StorageEntry] = {}
        for existing, data in self.files.items():
            if not existing.startswith(prefix):
                continue
            rest = existing.removeprefix(prefix)
            name, _, tail = rest.partition("/")
            entries[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
            )
        return sorted(entries.values(), key=lambda entry: (not entry.is_dir, entry.name))

    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = data
        self.versions[key] = self.versions.get(key, 0) + 1

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)
        self.versions.pop(key, None)

    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for existing in list(self.files):
            if existing.startswith(prefix):
                self.files.pop(existing)
                self.versions.pop(existing, None)

    async def stat(self, key: str) -> StorageEntry:
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.files[key]))

    async def get_version(self, key: str) -> StorageVersion:
        if key not in self.files:
            return StorageVersion(key=key, exists=False, is_dir=False)
        version = str(self.versions.get(key, 0))
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=False,
            size=len(self.files[key]),
            version_id=version,
            etag=version,
            content_hash=version,
        )

    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        current = await self.get_version(key)
        if condition:
            if condition.require_absent and current.exists:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
            if condition.version_token is not None and current.token != condition.version_token:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        await self.write_bytes(key, data, content_type=content_type)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))


@pytest.mark.asyncio
async def test_agent_file_tools_use_storage_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/notes.md": b"# Notes\nneedle\n",
        f"{agent_id}/memory/memory.md": b"# Memory\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    listing = await agent_tools._storage_list_dir(agent_id, "workspace")
    read = await agent_tools._storage_read_file(agent_id, "workspace/notes.md")
    search = await agent_tools._storage_search_files(agent_id, "needle", path="workspace", file_pattern="*.md")
    found = await agent_tools._storage_find_files(agent_id, "*.md", path="workspace")

    assert "notes.md" in listing
    assert "needle" in read
    assert "workspace/notes.md:2" in search
    assert "workspace/notes.md" in found


@pytest.mark.asyncio
async def test_read_file_outcome_rejects_binary_spreadsheet(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/inventory.xlsx": b"PK\x03\x04binary workbook",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    outcome = await agent_tools._read_file_outcome(
        agent_id,
        {"path": "workspace/inventory.xlsx"},
        tenant_id=None,
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "workspace_binary_file_unsupported"
    assert outcome.retryable is False
    assert "text files only" in (outcome.result_summary or "")


@pytest.mark.asyncio
async def test_complete_skill_read_records_package_digest(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/skills/budget/SKILL.md": b"---\nname: budget\n---\n",
        f"{agent_id}/skills/budget/scripts/auth.py": b"authenticate()\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    outcome = await agent_tools._read_file_outcome(
        agent_id,
        {"path": "skills/budget/SKILL.md"},
        tenant_id=None,
    )

    activation = outcome.metadata["skill_activation"]
    assert activation["name"] == "budget"
    assert activation["file_count"] == 2
    assert len(activation["package_digest"]) == 64


@pytest.mark.asyncio
async def test_temp_workspace_materializes_only_requested_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
        f"{agent_id}/workspace/other.md": b"# Other\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace/input.md"])
    try:
        assert (temp_ws.root / "workspace" / "input.md").read_text(encoding="utf-8") == "# Input\n"
        assert not (temp_ws.root / "workspace" / "other.md").exists()
    finally:
        temp_ws.cleanup()


@pytest.mark.asyncio
async def test_default_materialization_reserves_capacity_for_complete_skills(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/history.bin": b"w" * 8,
        f"{agent_id}/skills/budget/SKILL.md": b"skill",
        f"{agent_id}/skills/budget/scripts/auth.py": b"auth",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_tools, "TOOL_MATERIALIZE_MAX_TOTAL_BYTES", 10)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id)
    try:
        assert (temp_ws.root / "skills/budget/SKILL.md").read_bytes() == b"skill"
        assert (temp_ws.root / "skills/budget/scripts/auth.py").read_bytes() == b"auth"
    finally:
        temp_ws.cleanup()


@pytest.mark.asyncio
async def test_temp_workspace_rejects_partial_skill_snapshot(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/skills/budget/SKILL.md": b"instructions",
        f"{agent_id}/skills/budget/scripts/auth.py": b"auth",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    with pytest.raises(agent_tools.SkillSnapshotIncompleteError):
        await agent_tools._prepare_temp_workspace(
            agent_id,
            max_file_bytes=5,
        )


def test_temp_workspace_materialization_limits_are_50_and_500_mib():
    assert agent_tools.TOOL_MATERIALIZE_MAX_FILE_BYTES == 50 * 1024 * 1024
    assert agent_tools.TOOL_MATERIALIZE_MAX_TOTAL_BYTES == 500 * 1024 * 1024


@pytest.mark.asyncio
async def test_temp_workspace_materializes_file_above_previous_10_mib_limit(
    monkeypatch,
):
    agent_id = uuid.uuid4()
    content = b"x" * (11 * 1024 * 1024)
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/presentation.pptx": content,
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(
        agent_id,
        paths=["workspace/presentation.pptx"],
    )
    try:
        assert (temp_ws.root / "workspace" / "presentation.pptx").read_bytes() == content
    finally:
        temp_ws.cleanup()


@pytest.mark.asyncio
async def test_temp_workspace_logs_file_skipped_by_per_file_limit(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/oversized.pptx"
    storage = MemoryStorageBackend({storage_key: b"too large"})
    storage.get_version = AsyncMock(  # type: ignore[method-assign]
        return_value=StorageVersion(
            key=storage_key,
            exists=True,
            is_dir=False,
            size=51 * 1024 * 1024,
        )
    )
    warning = Mock()
    monkeypatch.setattr(agent_tools.logger, "warning", warning)

    await agent_tools._materialize_storage_path_with_budget(
        storage,
        storage_key,
        "workspace/oversized.pptx",
        tmp_path,
        {"total": 0},
        {},
    )

    assert not (tmp_path / "workspace" / "oversized.pptx").exists()
    warning.assert_called_once_with(
        "Tool workspace materialization skipped file: path={} size_bytes={} limit_bytes={} reason={}",
        "workspace/oversized.pptx",
        51 * 1024 * 1024,
        50 * 1024 * 1024,
        "per_file_limit",
    )


@pytest.mark.asyncio
async def test_temp_workspace_logs_file_skipped_by_total_limit(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/second.pptx"
    storage = MemoryStorageBackend({storage_key: b"second"})
    warning = Mock()
    monkeypatch.setattr(agent_tools.logger, "warning", warning)

    await agent_tools._materialize_storage_path_with_budget(
        storage,
        storage_key,
        "workspace/second.pptx",
        tmp_path,
        {"total": agent_tools.TOOL_MATERIALIZE_MAX_TOTAL_BYTES},
        {},
    )

    assert not (tmp_path / "workspace" / "second.pptx").exists()
    warning.assert_called_once_with(
        "Tool workspace materialization skipped file: path={} size_bytes={} limit_bytes={} reason={}",
        "workspace/second.pptx",
        len(b"second"),
        500 * 1024 * 1024,
        "total_limit",
    )


@pytest.mark.asyncio
async def test_execute_tool_list_files_does_not_create_persistent_workspace(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    async def _tenant(_agent_id):
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", _tenant)

    result = await agent_tools.execute_tool("list_files", {"path": "workspace"}, agent_id, agent_id)

    assert "input.md" in result
    assert not (tmp_path / str(agent_id)).exists()


@pytest.mark.asyncio
async def test_write_workspace_file_does_not_mirror_to_local_for_non_local_storage(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/test.md",
        content="hello",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"hello"
    assert not (tmp_path / str(agent_id) / "workspace" / "test.md").exists()


@pytest.mark.asyncio
async def test_write_workspace_file_appends_with_version_guard(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/page.html": b"<main>",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)
    revisions = []

    async def _record_revision(*args, **kwargs):
        revisions.append(kwargs)
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _record_revision)

    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/page.html",
        content="content</main>",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        append=True,
    )

    assert result.ok is True
    assert result.message == "Appended to workspace/page.html (14 chars; 20 total)"
    assert storage.files[f"{agent_id}/workspace/page.html"] == b"<main>content</main>"
    assert revisions[0]["before_content"] == "<main>"
    assert revisions[0]["after_content"] == "<main>content</main>"


@pytest.mark.asyncio
async def test_write_workspace_file_rejects_append_to_missing_file(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/page.html",
        content="content",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        append=True,
    )

    assert result.ok is False
    assert result.message == "Cannot append to missing file: workspace/page.html"
    assert storage.files == {}


@pytest.mark.asyncio
async def test_write_workspace_file_append_does_not_overwrite_a_concurrent_change(
    monkeypatch,
    tmp_path,
):
    agent_id = uuid.uuid4()
    key = f"{agent_id}/workspace/page.html"

    class RacingStorageBackend(MemoryStorageBackend):
        async def write_bytes_if_match(self, storage_key, data, **kwargs):
            await self.write_bytes(storage_key, b"concurrent")
            return await super().write_bytes_if_match(storage_key, data, **kwargs)

    storage = RacingStorageBackend({key: b"first"})
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/page.html",
        content=" second",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        append=True,
    )

    assert result.ok is False
    assert result.message == "Conflict detected while writing workspace/page.html"
    assert storage.files[key] == b"concurrent"


@pytest.mark.asyncio
async def test_flush_temp_workspace_only_writes_changed_files(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
        f"{agent_id}/workspace/other.md": b"# Other\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        (temp_ws.root / "workspace" / "input.md").write_text("# Updated\n", encoding="utf-8")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["updated"] == ["workspace/input.md"]
    assert "workspace/other.md" in result["skipped"]
    assert storage.files[f"{agent_id}/workspace/input.md"] == b"# Updated\n"
    assert storage.files[f"{agent_id}/workspace/other.md"] == b"# Other\n"


@pytest.mark.asyncio
async def test_flush_temp_workspace_refreshes_manifest_for_reused_workspace(monkeypatch):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/input.md"
    storage = MemoryStorageBackend({storage_key: b"first"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        local_file = temp_ws.root / "workspace" / "input.md"
        local_file.write_bytes(b"second")
        first = await agent_tools.flush_temp_workspace(temp_ws)
        first_token = temp_ws.manifest["workspace/input.md"].base_version_token

        local_file.write_bytes(b"first")
        second = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert first["updated"] == ["workspace/input.md"]
    assert second["updated"] == ["workspace/input.md"]
    assert storage.files[storage_key] == b"first"
    assert temp_ws.manifest["workspace/input.md"].base_hash == agent_tools.content_hash_bytes(b"first")
    assert temp_ws.manifest["workspace/input.md"].base_version_token != first_token


@pytest.mark.asyncio
async def test_flush_temp_workspace_fails_on_conflict(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace/input.md"])
    try:
        (temp_ws.root / "workspace" / "input.md").write_text("# Local change\n", encoding="utf-8")
        await storage.write_bytes(f"{agent_id}/workspace/input.md", b"# Remote change\n")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["conflicted"] == ["workspace/input.md"]
    assert storage.files[f"{agent_id}/workspace/input.md"] == b"# Remote change\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_before_materialize", [False, True])
async def test_flush_temp_workspace_accepts_stable_identical_concurrent_write(
    monkeypatch,
    existing_before_materialize,
):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/output/session-id/result.md"
    initial_files = {storage_key: b"# Initial\n"} if existing_before_materialize else None
    storage = MemoryStorageBackend(initial_files)
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(
        agent_id,
        paths=["workspace/output/session-id"],
        publish_paths=["workspace/output/session-id"],
    )
    try:
        output_path = temp_ws.root / "workspace/output/session-id/result.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"# Identical result\n")

        # Another publisher wins the CAS with this execution's exact bytes.
        await storage.write_bytes(storage_key, b"# Identical result\n")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result == {
        "updated": [],
        "deleted": [],
        "conflicted": [],
        "skipped": ["workspace/output/session-id/result.md"],
    }
    assert storage.files[storage_key] == b"# Identical result\n"
    manifest = temp_ws.manifest["workspace/output/session-id/result.md"]
    assert manifest.base_version_token == (await storage.get_version(storage_key)).token
    assert manifest.base_hash == agent_tools.content_hash_bytes(b"# Identical result\n")


@pytest.mark.asyncio
async def test_flush_temp_workspace_rejects_identical_bytes_when_version_changes_during_check(
    monkeypatch,
):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/output/session-id/result.md"

    class RacingReadStorageBackend(MemoryStorageBackend):
        mutate_after_read = False

        async def read_bytes(self, key: str) -> bytes:
            data = await super().read_bytes(key)
            if self.mutate_after_read:
                self.mutate_after_read = False
                await self.write_bytes(key, b"# Changed again\n")
            return data

    storage = RacingReadStorageBackend({storage_key: b"# Initial\n"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(
        agent_id,
        paths=["workspace/output/session-id"],
        publish_paths=["workspace/output/session-id"],
    )
    try:
        output_path = temp_ws.root / "workspace/output/session-id/result.md"
        output_path.write_bytes(b"# Identical result\n")
        await storage.write_bytes(storage_key, b"# Identical result\n")
        storage.mutate_after_read = True

        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["conflicted"] == ["workspace/output/session-id/result.md"]
    assert result["skipped"] == []
    assert storage.files[storage_key] == b"# Changed again\n"


@pytest.mark.asyncio
async def test_flush_isolated_output_overwrites_unmanifested_existing_file(monkeypatch):
    agent_id = uuid.uuid4()
    session_path = f"workspace/output/{uuid.uuid4()}"
    storage_key = f"{agent_id}/{session_path}/result.json"
    storage = MemoryStorageBackend()
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(
        agent_id,
        paths=[],
        publish_paths=[session_path],
    )
    try:
        output_file = temp_ws.root / session_path / "result.json"
        output_file.parent.mkdir(parents=True)
        output_file.write_bytes(b"session-result")
        await storage.write_bytes(storage_key, b"previous-result")
        result = await agent_tools.flush_temp_workspace(
            temp_ws,
            conflict_mode="overwrite",
        )
    finally:
        temp_ws.cleanup()

    assert result["updated"] == [f"{session_path}/result.json"]
    assert result["conflicted"] == []
    assert storage.files[storage_key] == b"session-result"
    assert f"{session_path}/result.json" in temp_ws.manifest


@pytest.mark.asyncio
async def test_flush_isolated_output_deletes_newer_existing_file(monkeypatch):
    agent_id = uuid.uuid4()
    session_path = f"workspace/output/{uuid.uuid4()}"
    storage_key = f"{agent_id}/{session_path}/result.json"
    storage = MemoryStorageBackend({storage_key: b"materialized-result"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(
        agent_id,
        paths=[session_path],
        publish_paths=[session_path],
    )
    try:
        (temp_ws.root / session_path / "result.json").unlink()
        await storage.write_bytes(storage_key, b"newer-result")
        result = await agent_tools.flush_temp_workspace(
            temp_ws,
            conflict_mode="overwrite",
        )
    finally:
        temp_ws.cleanup()

    assert result["deleted"] == [f"{session_path}/result.json"]
    assert result["conflicted"] == []
    assert storage_key not in storage.files
    assert f"{session_path}/result.json" not in temp_ws.manifest


@pytest.mark.asyncio
async def test_flush_temp_workspace_filters_manifest_deletions_to_publish_paths(monkeypatch):
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    session_path = f"workspace/output/{session_id}"
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/read-only.md": b"keep",
        f"{agent_id}/{session_path}/result.txt": b"delete-me",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(
        agent_id,
        tenant_id=str(uuid.uuid4()),
        paths=["workspace"],
        publish_paths=[session_path],
    )
    try:
        (temp_ws.root / session_path / "result.txt").unlink()
        (temp_ws.root / "workspace" / "read-only.md").write_text("changed", encoding="utf-8")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["deleted"] == [f"{session_path}/result.txt"]
    assert storage.files[f"{agent_id}/workspace/read-only.md"] == b"keep"


@pytest.mark.asyncio
async def test_write_workspace_file_fails_on_expected_version_conflict(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/test.md": b"old",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    version = await storage.get_version(f"{agent_id}/workspace/test.md")
    await storage.write_bytes(f"{agent_id}/workspace/test.md", b"remote-new")
    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/test.md",
        content="local-new",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        expected_version_token=version.token,
    )

    assert result.ok is False
    assert "Conflict detected" in result.message
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"remote-new"


@pytest.mark.asyncio
async def test_move_workspace_path_fails_when_source_changes(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/source.md": b"old",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    version = await storage.get_version(f"{agent_id}/workspace/source.md")
    await storage.write_bytes(f"{agent_id}/workspace/source.md", b"remote-new")
    result = await workspace_collaboration.move_workspace_path(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        source_path="workspace/source.md",
        destination_path="workspace/dest.md",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        expected_source_version_token=version.token,
    )

    assert result.ok is False
    assert "Conflict detected" in result.message
    assert f"{agent_id}/workspace/dest.md" not in storage.files


@pytest.mark.asyncio
async def test_move_overwrite_keeps_existing_target_when_candidate_write_conflicts(
    monkeypatch,
    tmp_path,
):
    agent_id = uuid.uuid4()

    class ConflictingTargetStorage(MemoryStorageBackend):
        async def write_bytes_if_match(self, key, data, **kwargs):
            if key.endswith("workspace/dest.md"):
                current = await self.get_version(key)
                return ConditionalWriteResult(
                    ok=False,
                    conflict=True,
                    current_version=current,
                )
            return await super().write_bytes_if_match(key, data, **kwargs)

    storage = ConflictingTargetStorage(
        {
            f"{agent_id}/workspace/source.md": b"candidate",
            f"{agent_id}/workspace/dest.md": b"keep-current",
        }
    )
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)
    result = await workspace_collaboration.move_workspace_path(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        source_path="workspace/source.md",
        destination_path="workspace/dest.md",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        overwrite=True,
    )

    assert result.ok is False
    assert storage.files[f"{agent_id}/workspace/dest.md"] == b"keep-current"
    assert storage.files[f"{agent_id}/workspace/source.md"] == b"candidate"


@pytest.mark.asyncio
async def test_delete_workspace_directory_uses_prefix_existence(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/dir/a.txt": b"a",
        f"{agent_id}/workspace/dir/nested/b.txt": b"b",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.delete_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/dir",
        actor_type="user",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert f"{agent_id}/workspace/dir/a.txt" not in storage.files
    assert f"{agent_id}/workspace/dir/nested/b.txt" not in storage.files


@pytest.mark.asyncio
async def test_flush_temp_workspace_creates_new_file_when_absent(monkeypatch, tmp_path):
    """Tool-produced file lands when the target does not exist yet."""
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "build.tar.gz").write_bytes(b"new-artifact")
    temp_ws = agent_tools.TempWorkspace(
        temp_dir=tmp_path,
        root=tmp_path,
        agent_id=agent_id,
        tenant_id=None,
        materialized_paths=["workspace"],
        publish_paths=["workspace"],
        manifest={},
    )
    result = await agent_tools.flush_temp_workspace(temp_ws)

    assert result["conflicted"] == []
    assert "workspace/build.tar.gz" in result["updated"]
    assert storage.files[f"{agent_id}/workspace/build.tar.gz"] == b"new-artifact"


@pytest.mark.asyncio
async def test_flush_temp_workspace_replaces_stale_artifact(monkeypatch, tmp_path):
    """A stale artifact at the same path is replaced via explicit overwrite mode."""
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/build.tar.gz": b"stale-artifact",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "build.tar.gz").write_bytes(b"fresh-artifact")
    temp_ws = agent_tools.TempWorkspace(
        temp_dir=tmp_path,
        root=tmp_path,
        agent_id=agent_id,
        tenant_id=None,
        materialized_paths=["workspace"],
        publish_paths=["workspace"],
        manifest={},
    )
    result = await agent_tools.flush_temp_workspace(
        temp_ws,
        conflict_mode="overwrite",
    )

    assert result["conflicted"] == []
    assert "workspace/build.tar.gz" in result["updated"]
    assert storage.files[f"{agent_id}/workspace/build.tar.gz"] == b"fresh-artifact"


@pytest.mark.asyncio
async def test_flush_temp_workspace_conflicts_on_concurrent_new_file(monkeypatch, tmp_path):
    """A genuinely concurrent edit at the same path still surfaces as a conflict."""
    agent_id = uuid.uuid4()

    class RacingStorageBackend(MemoryStorageBackend):
        async def write_bytes_if_match(self, storage_key, data, **kwargs):
            self.versions[storage_key] = self.versions.get(storage_key, 0) + 100
            return await super().write_bytes_if_match(storage_key, data, **kwargs)

    storage = RacingStorageBackend({
        f"{agent_id}/workspace/build.tar.gz": b"concurrent-edit",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "build.tar.gz").write_bytes(b"tool-output")
    temp_ws = agent_tools.TempWorkspace(
        temp_dir=tmp_path,
        root=tmp_path,
        agent_id=agent_id,
        tenant_id=None,
        materialized_paths=["workspace"],
        publish_paths=["workspace"],
        manifest={},
    )
    result = await agent_tools.flush_temp_workspace(temp_ws)

    assert result["conflicted"] == ["workspace/build.tar.gz"]
    assert storage.files[f"{agent_id}/workspace/build.tar.gz"] == b"concurrent-edit"


# --- ADR 0011: direct storage writes refresh the run-scoped workspace ---------


@asynccontextmanager
async def _fake_async_session():
    db = Mock()
    db.commit = AsyncMock()
    yield db


async def _noop_active_lock(*_args, **_kwargs):
    return None


async def _noop_revision(*_args, **_kwargs):
    return None


def _patch_workspace_db(monkeypatch):
    """Stub the DB touchpoints inside write/delete/move workspace helpers."""
    monkeypatch.setattr(agent_tools, "async_session", _fake_async_session)
    monkeypatch.setattr(workspace_collaboration, "get_active_lock", _noop_active_lock)
    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)


def _patch_storage(monkeypatch, storage):
    """Point every module-level get_storage_backend at the in-memory backend.

    _execute_workspace_mutation mixes agent_tools' storage lookups (edit_file
    reads) with workspace_collaboration's (write_workspace_file etc.), so both
    must resolve to the same instance.
    """
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)


async def _materialize_run_workspace(
    agent_id: uuid.UUID,
    paths: list[str],
) -> tuple[str, "agent_tools.TempWorkspace"]:
    """Materialize a run-scoped workspace exactly as production does.

    The state stays registered in the run-workspace registry after the
    context exits (same as one settled Agent loop between executions); the
    caller must `close_run_workspace(run_id)` to clean up.
    """
    run_id = str(uuid.uuid4())
    identity = RunWorkspaceIdentity(
        agent_id=str(agent_id),
        tenant_id=None,
        session_id=None,
        workspace_mode="direct",
        materialized_paths=tuple(paths),
        publish_paths=tuple(paths),
    )

    async def factory():
        return await agent_tools._prepare_temp_workspace(agent_id, paths=paths)

    async with use_run_workspace(run_id=run_id, identity=identity, factory=factory) as run_ws:
        workspace = run_ws
    return run_id, cast(agent_tools.TempWorkspace, workspace)


@pytest.mark.asyncio
async def test_edit_file_direct_write_refreshes_run_workspace_so_flush_does_not_conflict(
    monkeypatch,
    tmp_path,
):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/notes.txt"
    storage = MemoryStorageBackend({storage_key: b"# Notes\nline one\n"})
    _patch_storage(monkeypatch, storage)
    _patch_workspace_db(monkeypatch)

    run_id, run_ws = await _materialize_run_workspace(agent_id, ["workspace"])
    token = sandbox_run_scope_id.set(run_id)
    try:
        result = await agent_tools._execute_workspace_mutation(
            "edit_file",
            {"path": "workspace/notes.txt", "old_string": "line one", "new_string": "line ONE"},
            agent_id=agent_id,
            base_dir=tmp_path / str(agent_id),
            session_id=None,
        )
        assert "Replaced 1 occurrence(s)" in result

        # Simulate the run's execute_code changing the same path differently.
        (run_ws.root / "workspace" / "notes.txt").write_text(
            "# Notes\nline ONE\nextra\n", encoding="utf-8"
        )
        flush = await agent_tools.flush_temp_workspace(run_ws)

        assert flush["conflicted"] == []
        assert "workspace/notes.txt" in flush["updated"]
        assert storage.files[storage_key] == b"# Notes\nline ONE\nextra\n"
    finally:
        sandbox_run_scope_id.reset(token)
        await close_run_workspace(run_id)


@pytest.mark.asyncio
async def test_edit_file_direct_write_updates_run_workspace_view(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/notes.txt"
    storage = MemoryStorageBackend({storage_key: b"# Notes\nline one\n"})
    _patch_storage(monkeypatch, storage)
    _patch_workspace_db(monkeypatch)

    run_id, run_ws = await _materialize_run_workspace(agent_id, ["workspace"])
    token = sandbox_run_scope_id.set(run_id)
    try:
        await agent_tools._execute_workspace_mutation(
            "edit_file",
            {"path": "workspace/notes.txt", "old_string": "line one", "new_string": "line ONE"},
            agent_id=agent_id,
            base_dir=tmp_path / str(agent_id),
            session_id=None,
        )

        # The run workspace temp view is immediately consistent with storage.
        assert (run_ws.root / "workspace" / "notes.txt").read_bytes() == b"# Notes\nline ONE\n"
        entry = run_ws.manifest["workspace/notes.txt"]
        assert entry.base_version_token == (await storage.get_version(storage_key)).token
        assert entry.base_hash == agent_tools.content_hash_bytes(b"# Notes\nline ONE\n")
    finally:
        sandbox_run_scope_id.reset(token)
        await close_run_workspace(run_id)


@pytest.mark.asyncio
async def test_delete_file_direct_delete_removes_from_run_workspace(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/notes.txt"
    storage = MemoryStorageBackend({storage_key: b"# Notes\n"})
    _patch_storage(monkeypatch, storage)
    _patch_workspace_db(monkeypatch)

    run_id, run_ws = await _materialize_run_workspace(agent_id, ["workspace"])
    token = sandbox_run_scope_id.set(run_id)
    try:
        result = await agent_tools._execute_workspace_mutation(
            "delete_file",
            {"path": "workspace/notes.txt"},
            agent_id=agent_id,
            base_dir=tmp_path / str(agent_id),
            session_id=None,
        )
        assert "✅ Deleted" in result

        # Temp file and manifest entry are gone immediately.
        assert not (run_ws.root / "workspace" / "notes.txt").exists()
        assert "workspace/notes.txt" not in run_ws.manifest

        # A later flush (e.g. execute_code also removed the file) produces no
        # delete candidate conflict.
        flush = await agent_tools.flush_temp_workspace(run_ws)
        assert flush["conflicted"] == []
        assert flush["deleted"] == []
    finally:
        sandbox_run_scope_id.reset(token)
        await close_run_workspace(run_id)


@pytest.mark.asyncio
async def test_per_call_flush_refreshes_run_workspace(monkeypatch):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/notes.txt"
    storage = MemoryStorageBackend({storage_key: b"first"})
    _patch_storage(monkeypatch, storage)

    run_id, run_ws = await _materialize_run_workspace(agent_id, ["workspace"])
    token = sandbox_run_scope_id.set(run_id)
    try:
        # A Family-2 per-call temp workspace publishes to the same path.
        other_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"])
        try:
            (other_ws.root / "workspace" / "notes.txt").write_bytes(b"second")
            flush = await agent_tools.flush_temp_workspace(other_ws)
        finally:
            other_ws.cleanup()

        assert flush["updated"] == ["workspace/notes.txt"]
        assert storage.files[storage_key] == b"second"

        # The run workspace temp view and manifest track the publication.
        assert (run_ws.root / "workspace" / "notes.txt").read_bytes() == b"second"
        entry = run_ws.manifest["workspace/notes.txt"]
        assert entry.base_version_token == (await storage.get_version(storage_key)).token
        assert entry.base_hash == agent_tools.content_hash_bytes(b"second")
    finally:
        sandbox_run_scope_id.reset(token)
        await close_run_workspace(run_id)


@pytest.mark.asyncio
async def test_write_file_direct_write_materializes_new_path_into_run_workspace(
    monkeypatch,
    tmp_path,
):
    agent_id = uuid.uuid4()
    storage_key = f"{agent_id}/workspace/new.md"
    storage = MemoryStorageBackend()
    _patch_storage(monkeypatch, storage)
    _patch_workspace_db(monkeypatch)

    run_id, run_ws = await _materialize_run_workspace(agent_id, ["workspace"])
    token = sandbox_run_scope_id.set(run_id)
    try:
        result = await agent_tools._execute_workspace_mutation(
            "write_file",
            {"path": "workspace/new.md", "content": "# New\n"},
            agent_id=agent_id,
            base_dir=tmp_path / str(agent_id),
            session_id=None,
        )
        assert "✅ Written to workspace/new.md" in result

        # The brand-new path was materialized into the run workspace.
        assert (run_ws.root / "workspace" / "new.md").read_bytes() == b"# New\n"
        entry = run_ws.manifest["workspace/new.md"]
        assert entry.base_version_token == (await storage.get_version(storage_key)).token

        # execute_code then edits the new path; the flush CAS matches.
        (run_ws.root / "workspace" / "new.md").write_text("# New\nedited\n", encoding="utf-8")
        flush = await agent_tools.flush_temp_workspace(run_ws)
        assert flush["conflicted"] == []
        assert storage.files[storage_key] == b"# New\nedited\n"
    finally:
        sandbox_run_scope_id.reset(token)
        await close_run_workspace(run_id)


@pytest.mark.asyncio
async def test_move_file_direct_move_refreshes_run_workspace(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    source_key = f"{agent_id}/workspace/a.txt"
    storage = MemoryStorageBackend({source_key: b"content"})
    _patch_storage(monkeypatch, storage)
    _patch_workspace_db(monkeypatch)

    run_id, run_ws = await _materialize_run_workspace(agent_id, ["workspace"])
    token = sandbox_run_scope_id.set(run_id)
    try:
        result = await agent_tools._execute_workspace_mutation(
            "move_file",
            {"source_path": "workspace/a.txt", "destination_path": "workspace/b.txt"},
            agent_id=agent_id,
            base_dir=tmp_path / str(agent_id),
            session_id=None,
        )
        assert "✅" in result

        # Destination materialized, source removed from temp and manifest.
        assert (run_ws.root / "workspace" / "b.txt").read_bytes() == b"content"
        assert "workspace/b.txt" in run_ws.manifest
        assert not (run_ws.root / "workspace" / "a.txt").exists()
        assert "workspace/a.txt" not in run_ws.manifest
    finally:
        sandbox_run_scope_id.reset(token)
        await close_run_workspace(run_id)


@pytest.mark.asyncio
async def test_refresh_run_workspace_path_noop_without_run_workspace():
    # No registered run workspace: a no-op, not an error.
    result = await refresh_run_workspace_path(
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        "workspace/notes.txt",
        data=b"new",
        version_token="9",
        size=3,
    )
    assert result is None


@pytest.mark.asyncio
async def test_refresh_run_workspace_path_skips_own_workspace(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({f"{agent_id}/workspace/notes.txt": b"first"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    run_id, run_ws = await _materialize_run_workspace(agent_id, ["workspace"])
    try:
        # The run workspace flushing itself must not re-enter its own lock.
        await refresh_run_workspace_path(
            run_id,
            str(agent_id),
            "workspace/notes.txt",
            data=b"same",
            version_token="9",
            size=4,
            skip_workspace=run_ws,
        )
        assert (run_ws.root / "workspace" / "notes.txt").read_bytes() == b"first"
        assert run_ws.manifest["workspace/notes.txt"].base_version_token == "1"
    finally:
        await close_run_workspace(run_id)


@pytest.mark.asyncio
async def test_refresh_run_workspace_path_delete_removes_temp_and_manifest(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/notes.txt": b"first",
        f"{agent_id}/workspace/sub/child.txt": b"child",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    run_id, run_ws = await _materialize_run_workspace(agent_id, ["workspace"])
    try:
        await refresh_run_workspace_path(
            run_id,
            str(agent_id),
            "workspace/sub",
            data=None,
        )
        assert not (run_ws.root / "workspace" / "sub").exists()
        assert "workspace/sub/child.txt" not in run_ws.manifest
        # Sibling paths untouched.
        assert (run_ws.root / "workspace" / "notes.txt").exists()
        assert "workspace/notes.txt" in run_ws.manifest
    finally:
        await close_run_workspace(run_id)


@pytest.mark.asyncio
async def test_flush_of_run_workspace_itself_does_not_deadlock(monkeypatch):
    """execute_code flushes while the run workspace lock is held; the refresh
    hook must recognize its own workspace and not re-enter that lock."""
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({f"{agent_id}/workspace/notes.txt": b"first"})
    _patch_storage(monkeypatch, storage)

    run_id = str(uuid.uuid4())
    token = sandbox_run_scope_id.set(run_id)
    identity = RunWorkspaceIdentity(
        agent_id=str(agent_id),
        tenant_id=None,
        session_id=None,
        workspace_mode="direct",
        materialized_paths=("workspace",),
        publish_paths=("workspace",),
    )
    try:
        async with use_run_workspace(
            run_id=run_id,
            identity=identity,
            factory=lambda: agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"]),
        ) as run_ws:
            run_ws = cast(agent_tools.TempWorkspace, run_ws)
            (run_ws.root / "workspace" / "notes.txt").write_bytes(b"second")
            flush = await asyncio.wait_for(
                agent_tools.flush_temp_workspace(run_ws),
                timeout=5,
            )
        assert flush["updated"] == ["workspace/notes.txt"]
        assert storage.files[f"{agent_id}/workspace/notes.txt"] == b"second"
    finally:
        sandbox_run_scope_id.reset(token)
        await close_run_workspace(run_id)
