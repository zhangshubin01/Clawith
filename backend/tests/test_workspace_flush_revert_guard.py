"""Workspace flush revert guard (方向 1) tests.

Plan: docs/technical-plans/20260905-workspace-revert-guard.md.
Self-contained: backend/tests is not a package, so this file keeps its own
in-memory StorageBackend instead of importing a sibling test module.
"""

import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.services import agent_tools
from app.services import gitlab_workspace
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageEntry,
    StorageVersion,
    WriteCondition,
    content_hash_bytes,
)


@asynccontextmanager
async def _noop_workspace_locks(*_args, **_kwargs):
    yield


@pytest.fixture(autouse=True)
def _isolate_storage_semantics_from_distributed_locking(monkeypatch):
    monkeypatch.setattr(agent_tools, "workspace_locks", _noop_workspace_locks)


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


def _temp_workspace(
    tmp_path: Path,
    agent_id: uuid.UUID,
    *,
    manifest: dict | None = None,
    git_head_hashes: dict[str, str] | None = None,
) -> agent_tools.TempWorkspace:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return agent_tools.TempWorkspace(
        temp_dir=type("TempDir", (), {"cleanup": lambda self: None})(),
        root=tmp_path,
        agent_id=agent_id,
        tenant_id=None,
        materialized_paths=["workspace"],
        publish_paths=["workspace"],
        manifest=dict(manifest or {}),
        git_head_hashes=dict(git_head_hashes or {}),
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)


def _commit_file(repo: Path, rel: str, content: bytes) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)


# ── capture_head_tree_hashes ─────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_head_tree_hashes_returns_sha256_of_tracked_files(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "a.txt", b"alpha")
    _commit_file(repo, "sub/b.txt", b"beta")
    # Untracked files must not appear in the HEAD tree.
    (repo / "untracked.txt").write_bytes(b"junk")

    hashes = await gitlab_workspace.capture_head_tree_hashes(repo, tmp_path)

    assert hashes == {
        "a.txt": content_hash_bytes(b"alpha"),
        "sub/b.txt": content_hash_bytes(b"beta"),
    }


@pytest.mark.asyncio
async def test_capture_head_tree_hashes_empty_for_non_repo(tmp_path):
    hashes = await gitlab_workspace.capture_head_tree_hashes(tmp_path / "nope", tmp_path)
    assert hashes == {}


# ── flush revert guard ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_guards_git_revert_and_preserves_storage(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    rel = "workspace/repo/file.kt"
    storage_key = f"{agent_id}/{rel}"
    head_content = b"// head version\n"
    edit_content = b"// edited version\n"
    head_hash = content_hash_bytes(head_content)
    edit_hash = content_hash_bytes(edit_content)

    storage = MemoryStorageBackend({storage_key: edit_content})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    sandbox_file = tmp_path / rel
    sandbox_file.parent.mkdir(parents=True, exist_ok=True)
    sandbox_file.write_bytes(head_content)  # git checkout/reset reverted it to HEAD

    temp_ws = _temp_workspace(
        tmp_path,
        agent_id,
        manifest={
            rel: agent_tools.TempWorkspaceManifestEntry(
                rel_path=rel,
                storage_key=storage_key,
                base_version_token="1",
                base_hash=edit_hash,
                size=len(edit_content),
            )
        },
        git_head_hashes={rel: head_hash},
    )

    result = await agent_tools.flush_temp_workspace(temp_ws)

    assert result["reverted"] == [rel]
    assert result["updated"] == []
    assert result["conflicted"] == []
    assert storage.files[storage_key] == edit_content  # the edit survives


@pytest.mark.asyncio
async def test_flush_publishes_legitimate_change_not_matching_head(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    rel = "workspace/repo/file.kt"
    storage_key = f"{agent_id}/{rel}"
    head_content = b"// head\n"
    legit_content = b"// legit new edit\n"
    head_hash = content_hash_bytes(head_content)

    storage = MemoryStorageBackend({storage_key: head_content})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    sandbox_file = tmp_path / rel
    sandbox_file.parent.mkdir(parents=True, exist_ok=True)
    sandbox_file.write_bytes(legit_content)

    temp_ws = _temp_workspace(
        tmp_path,
        agent_id,
        manifest={
            rel: agent_tools.TempWorkspaceManifestEntry(
                rel_path=rel,
                storage_key=storage_key,
                base_version_token="1",
                base_hash=head_hash,
                size=len(head_content),
            )
        },
        git_head_hashes={rel: head_hash},
    )

    result = await agent_tools.flush_temp_workspace(temp_ws)

    assert result["reverted"] == []
    assert result["updated"] == [rel]
    assert storage.files[storage_key] == legit_content


@pytest.mark.asyncio
async def test_flush_skips_when_sandbox_matches_base_hash(monkeypatch, tmp_path):
    """Storage edited without refresh + sandbox still at base → no publish, no data loss."""
    agent_id = uuid.uuid4()
    rel = "workspace/repo/file.kt"
    storage_key = f"{agent_id}/{rel}"
    head_content = b"// head\n"
    edit_content = b"// edited after materialization\n"
    head_hash = content_hash_bytes(head_content)

    # Storage was edited (edit_file) after materialization, manifest still at HEAD.
    storage = MemoryStorageBackend({storage_key: edit_content})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    sandbox_file = tmp_path / rel
    sandbox_file.parent.mkdir(parents=True, exist_ok=True)
    sandbox_file.write_bytes(head_content)  # unchanged from base snapshot

    temp_ws = _temp_workspace(
        tmp_path,
        agent_id,
        manifest={
            rel: agent_tools.TempWorkspaceManifestEntry(
                rel_path=rel,
                storage_key=storage_key,
                base_version_token="1",
                base_hash=head_hash,
                size=len(head_content),
            )
        },
        git_head_hashes={rel: head_hash},
    )

    result = await agent_tools.flush_temp_workspace(temp_ws)

    assert result["reverted"] == []
    assert result["updated"] == []
    assert rel in result["skipped"]
    assert storage.files[storage_key] == edit_content  # untouched
