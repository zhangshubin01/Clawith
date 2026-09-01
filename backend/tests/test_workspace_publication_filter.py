"""Layered workspace publication filter (P0) + conflict breaker (P0.5) tests.

Plan: docs/technical-plans/20260901-workspace-publication-p0-fix.md (v3).
Tests are self-contained: backend/tests is not a package, so this file keeps
its own in-memory StorageBackend instead of importing the sibling test module.
"""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.services import agent_tools
from app.services.agent_runtime.tool_repair_budget import (
    WORKSPACE_SYNC_CONFLICT_LIMIT,
    apply_workspace_sync_conflict,
)
from app.services.sandbox.workspace_policy import (
    build_workspace_policy,
    classify_publish_path,
)
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageEntry,
    StorageVersion,
    WriteCondition,
)
from app.services.storage_runtime.utils import normalize_storage_key


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


def _manifest_entry(
    agent_id: uuid.UUID,
    rel_path: str,
    token: str = "1",
    base_hash: str = "stale-hash",
):
    return agent_tools.TempWorkspaceManifestEntry(
        rel_path=rel_path,
        storage_key=normalize_storage_key(f"{agent_id}/{rel_path}"),
        base_version_token=token,
        base_hash=base_hash,
        size=0,
    )


def _temp_workspace(tmp_path: Path, agent_id: uuid.UUID, *, manifest: dict | None = None) -> agent_tools.TempWorkspace:
    """Build a TempWorkspace over tmp_path without invoking materialization."""
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return agent_tools.TempWorkspace(
        temp_dir=tmp_path,  # type: ignore[arg-type] - pytest Path; tests never call cleanup()
        root=tmp_path,
        agent_id=agent_id,
        tenant_id=None,
        materialized_paths=["workspace"],
        publish_paths=["workspace"],
        manifest=dict(manifest or {}),
    )


# ── 1. derived paths never enter either enumeration (write side) ──


@pytest.mark.asyncio
async def test_derived_paths_are_not_collected_for_publication(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    temp_ws = _temp_workspace(tmp_path, agent_id)

    (tmp_path / "workspace" / "src").mkdir(parents=True)
    (tmp_path / "workspace" / "build" / "classes").mkdir(parents=True)
    (tmp_path / "workspace" / "build" / "outputs" / "apk").mkdir(parents=True)
    (tmp_path / "workspace" / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "workspace" / "dist").mkdir(parents=True)
    (tmp_path / "workspace" / "src" / "main.kt").write_bytes(b"fun main() {}")
    (tmp_path / "workspace" / "build" / "classes" / "x.class").write_bytes(b"\xca\xfe")
    (tmp_path / "workspace" / "build" / "outputs" / "apk" / "x.apk").write_bytes(b"apk-bytes")
    (tmp_path / "workspace" / "node_modules" / "pkg" / "index.js").write_bytes(b"js")
    (tmp_path / "workspace" / "dist" / "bundle.js").write_bytes(b"bundle")
    (tmp_path / "workspace" / "build.tar.gz").write_bytes(b"tarball")

    cas_files, overwrite_files, derived_paths = agent_tools._collect_temp_workspace_files(
        temp_ws.root,
        ["workspace"],
    )

    assert cas_files == {
        "workspace/src/main.kt": tmp_path / "workspace" / "src" / "main.kt",
        "workspace/build.tar.gz": tmp_path / "workspace" / "build.tar.gz",
    }
    assert overwrite_files == {
        "workspace/build/outputs/apk/x.apk": tmp_path / "workspace" / "build" / "outputs" / "apk" / "x.apk",
    }
    assert sorted(derived_paths) == [
        "workspace/build/classes/x.class",
        "workspace/dist/bundle.js",
        "workspace/node_modules/pkg/index.js",
    ]

    result = await agent_tools.flush_temp_workspace(temp_ws)
    assert result["conflicted"] == []
    assert "workspace/src/main.kt" in result["updated"]
    assert "workspace/build.tar.gz" in result["updated"]
    assert "workspace/build/outputs/apk/x.apk" in result["updated"]
    assert result["derived_skipped_count"] == 3
    assert f"{agent_id}/workspace/src/main.kt" in storage.files
    assert f"{agent_id}/workspace/build/outputs/apk/x.apk" in storage.files
    assert f"{agent_id}/workspace/build/classes/x.class" not in storage.files
    assert f"{agent_id}/workspace/node_modules/pkg/index.js" not in storage.files
    assert f"{agent_id}/workspace/dist/bundle.js" not in storage.files


# ── 2. derived paths never enter the candidate delete loop (v1 fatal gap) ──


@pytest.mark.asyncio
async def test_derived_paths_are_skipped_in_candidate_delete_loop(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/src/removed.kt": b"old",
            f"{agent_id}/workspace/build/classes/old.class": b"\xca\xfe",
            f"{agent_id}/workspace/build/outputs/apk/old.apk": b"old-apk",
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    # History manifest (rollback mixing window): derived entries exist in the
    # manifest, and the sandbox copy no longer has the corresponding files.
    manifest = {
        "workspace/src/removed.kt": _manifest_entry(agent_id, "workspace/src/removed.kt"),
        "workspace/build/classes/old.class": _manifest_entry(agent_id, "workspace/build/classes/old.class"),
        "workspace/build/outputs/apk/old.apk": _manifest_entry(agent_id, "workspace/build/outputs/apk/old.apk"),
    }
    temp_ws = _temp_workspace(tmp_path, agent_id, manifest=manifest)
    (tmp_path / "workspace" / "src").mkdir(parents=True)

    changes = await agent_tools._workspace_candidate_changes(temp_ws)
    by_path = {change.path: change for change in changes}

    # L1 delete survives with its CAS base credentials.
    source_delete = by_path["workspace/src/removed.kt"]
    assert source_delete.operation == "delete"
    assert source_delete.base_state == "present"
    # L3 delete is unconditional (LWW symmetric semantics).
    artifact_delete = by_path["workspace/build/outputs/apk/old.apk"]
    assert artifact_delete.operation == "delete"
    assert artifact_delete.base_state == "unloaded"
    # L2 must never produce a delete candidate.
    assert "workspace/build/classes/old.class" not in by_path

    # A flush in the same state deletes the L1 source and the L3 artifact
    # (LWW symmetric), but must not touch the derived storage history.
    result = await agent_tools.flush_temp_workspace(temp_ws)
    assert result["deleted"] == [
        "workspace/src/removed.kt",
        "workspace/build/outputs/apk/old.apk",
    ]
    assert f"{agent_id}/workspace/build/classes/old.class" in storage.files
    assert f"{agent_id}/workspace/build/outputs/apk/old.apk" not in storage.files


# ── 3. L3 artifact overwrite keeps artifact_refs flowing ──


@pytest.mark.asyncio
async def test_artifact_paths_publish_with_overwrite(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    apk_rel = "workspace/build/outputs/apk/x.apk"
    storage = MemoryStorageBackend({f"{agent_id}/{apk_rel}": b"v1"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = _temp_workspace(tmp_path, agent_id, manifest={apk_rel: _manifest_entry(agent_id, apk_rel, token="1")})
    (tmp_path / "workspace" / "build" / "outputs" / "apk").mkdir(parents=True)
    (tmp_path / "workspace" / "build" / "outputs" / "apk" / "x.apk").write_bytes(b"v2-fresh")

    # Simulate a version drift before flush (the two-writer hazard): even with
    # a stale base token, the artifact path must publish instead of conflicting.
    await storage.write_bytes(f"{agent_id}/{apk_rel}", b"external-writer")
    result = await agent_tools.flush_temp_workspace(temp_ws)

    assert result["conflicted"] == []
    assert apk_rel in result["updated"]
    assert storage.files[f"{agent_id}/{apk_rel}"] == b"v2-fresh"
    assert agent_tools._workspace_artifact_ref(agent_id, apk_rel).startswith("workspace://")


# ── 4. L3 deletion is last-write-wins ──


@pytest.mark.asyncio
async def test_artifact_deletion_is_lww(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    apk_rel = "workspace/build/outputs/apk/x.apk"
    storage = MemoryStorageBackend({f"{agent_id}/{apk_rel}": b"apk"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = _temp_workspace(tmp_path, agent_id, manifest={apk_rel: _manifest_entry(agent_id, apk_rel)})
    # Sandbox deleted the artifact: no file on disk, storage must follow suit
    # even though a third version may have drifted in between.
    await storage.write_bytes(f"{agent_id}/{apk_rel}", b"drifted")
    result = await agent_tools.flush_temp_workspace(temp_ws)

    assert result["conflicted"] == []
    assert apk_rel in result["deleted"]
    assert f"{agent_id}/{apk_rel}" not in storage.files


# ── 5. L1 conflict carries the new actionable summary ──


@pytest.mark.asyncio
async def test_source_conflict_returns_actionable_error_with_paths(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({f"{agent_id}/workspace/notes.md": b"# Notes\n"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    async def runner(root: Path):
        # The tool edits the materialized copy, and a remote write drifts the
        # storage version before flush: a deterministic L1 CAS conflict.
        (root / "workspace" / "notes.md").write_bytes(b"# Changed\n")
        await storage.write_bytes(f"{agent_id}/workspace/notes.md", b"# Remote\n")
        return agent_tools._typed_success("ok")

    outcome = await agent_tools._run_with_temp_workspace_outcome(
        agent_id,
        None,
        runner,
        sync_back=True,
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "workspace_sync_conflict"
    assert "未能保存" in outcome.result_summary
    assert "read_file" in outcome.result_summary
    assert "edit_file" in outcome.result_summary
    assert "workspace/notes.md" in outcome.result_summary

    # Helper caps the path list at 5 + total.
    summary = agent_tools._workspace_conflict_summary([f"p{i}" for i in range(7)])
    assert "p0" in summary and "p4" in summary
    assert "p5" not in summary
    assert "共 7 条" in summary


# ── 6. pure derived/artifact flush succeeds with the skip count ──


@pytest.mark.asyncio
async def test_pure_derived_conflict_succeeds_with_metadata(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    temp_ws = _temp_workspace(tmp_path, agent_id)
    (tmp_path / "workspace" / "build").mkdir(parents=True)
    (tmp_path / "workspace" / "build" / "classes").mkdir(parents=True)
    (tmp_path / "workspace" / "build" / "classes" / "x.class").write_bytes(b"\xca\xfe")

    result = await agent_tools.flush_temp_workspace(temp_ws)
    assert result["conflicted"] == []
    assert result["updated"] == []
    assert result["derived_skipped_count"] == 1


# ── 7. isolated_output mode regression ──


def test_isolated_output_mode_unchanged():
    session_id = uuid.uuid4()
    policy = build_workspace_policy(
        mode="isolated_output",
        session_id=session_id,
        default_paths=["workspace"],
    )
    assert policy.publish_paths == (f"workspace/output/{session_id}",)
    assert policy.publication_conflict_mode == "overwrite"
    # The session output path never matches derived segments.
    assert classify_publish_path(f"workspace/output/{session_id}/result.json") == "source"


# ── 8. classifier boundary snapshot ──


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("build/gradle.log", "derived"),
        ("workspace/app/build/classes/x.class", "derived"),
        ("build.tar.gz", "source"),
        ("build.sh", "source"),
        ("BUILD/x", "source"),
        ("build-notes/x", "source"),
        ("node_modules/pkg/index.js", "derived"),
        ("node_modules/x.apk", "derived"),
        ("a/build/b/c.txt", "derived"),
        ("workspace/build/outputs/apk/x.apk", "artifact"),
        ("build/outputs/aab/x.aab", "artifact"),
        ("workspace/src/main.kt", "source"),
        ("_exec_tmp_run.py", "derived"),
        ("workspace/_exec_tmp_123.sh", "derived"),
        (".git/HEAD", "derived"),
        (".gradle/8.0/registry.bin", "derived"),
        ("workspace/target/x.jar", "derived"),
        ("workspace/__pycache__/mod.cpython.pyc", "derived"),
        ("workspace/dist/app.js", "derived"),
    ],
)
def test_path_classification_boundaries(rel_path, expected):
    assert classify_publish_path(rel_path) == expected


# ── 9. two-writer order: external apk write is not clobbered by an unchanged
#        sandbox copy (L3 skipped-when-identical) ──


@pytest.mark.asyncio
async def test_artifact_no_stale_overwrite_after_external_write(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    apk_rel = "workspace/build/outputs/apk/x.apk"
    storage = MemoryStorageBackend({f"{agent_id}/{apk_rel}": b"v1"})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    # The manifest base_hash reflects the bytes materialized (v1), exactly as a
    # real materialization would record them.
    temp_ws = _temp_workspace(
        tmp_path,
        agent_id,
        manifest={
            apk_rel: _manifest_entry(agent_id, apk_rel, token="1", base_hash=agent_tools.content_hash_bytes(b"v1"))
        },
    )
    (tmp_path / "workspace" / "build" / "outputs" / "apk").mkdir(parents=True)
    (tmp_path / "workspace" / "build" / "outputs" / "apk" / "x.apk").write_bytes(b"v1")

    # android_compile republishes the apk after materialization.
    await storage.write_bytes(f"{agent_id}/{apk_rel}", b"v2-external")
    result = await agent_tools.flush_temp_workspace(temp_ws)

    # The sandbox copy is byte-identical to its materialized base: skipping is
    # the correct LWW behavior, the external v2 survives.
    assert result["conflicted"] == []
    assert apk_rel not in result["updated"]
    assert storage.files[f"{agent_id}/{apk_rel}"] == b"v2-external"


# ── 10. materialization never loads derived history ──


@pytest.mark.asyncio
async def test_materialize_skips_derived(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/src/Main.kt": b"fun main() {}",
            f"{agent_id}/workspace/build/classes/x.class": b"\xca\xfe",
            f"{agent_id}/workspace/build/outputs/apk/x.apk": b"apk",
            f"{agent_id}/workspace/node_modules/m/index.js": b"js",
            f"{agent_id}/workspace/.git/HEAD": b"ref: refs/heads/main\n",
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        assert (temp_ws.root / "workspace" / "src" / "Main.kt").is_file()
        assert (temp_ws.root / "workspace" / "build" / "outputs" / "apk" / "x.apk").is_file()
        assert not (temp_ws.root / "workspace" / "build" / "classes" / "x.class").exists()
        assert not (temp_ws.root / "workspace" / "node_modules" / "m" / "index.js").exists()
        assert not (temp_ws.root / "workspace" / ".git" / "HEAD").exists()
        assert set(temp_ws.manifest) == {
            "workspace/src/Main.kt",
            "workspace/build/outputs/apk/x.apk",
        }
    finally:
        temp_ws.cleanup()


# ── P0.5 circuit breaker (same-error streak) ──


def _conflict_message() -> dict:
    return {
        "name": "execute_code",
        "execution_status": "failed",
        "error_code": "workspace_sync_conflict",
        "model_action": "continue",
        "side_effect_state": "unknown",
        "content": "Workspace publication conflicted.",
        "tool_call_id": "call-1",
    }


def test_workspace_sync_conflict_breaker_trips_at_limit():
    assert WORKSPACE_SYNC_CONFLICT_LIMIT == 3
    budget: object = None
    terminal = False
    for step in range(WORKSPACE_SYNC_CONFLICT_LIMIT):
        transition = apply_workspace_sync_conflict(budget, _conflict_message(), model_step=step)
        budget = transition.budget
        terminal = transition.terminal
    assert terminal is True


def test_workspace_sync_conflict_breaker_resets_on_other_tool_result():
    budget: object = None
    for step in range(2):
        budget = apply_workspace_sync_conflict(
            budget,
            _conflict_message(),
            model_step=step,
        ).budget
    # Any other tool result (success, another failure, another tool) breaks
    # the streak: the model changed course.
    other = apply_workspace_sync_conflict(
        budget,
        {
            "name": "read_file",
            "execution_status": "succeeded",
            "model_action": "continue",
            "side_effect_state": "confirmed",
            "content": "content",
            "tool_call_id": "call-2",
        },
        model_step=3,
    )
    assert other.terminal is False
    resumed = apply_workspace_sync_conflict(
        other.budget,
        _conflict_message(),
        model_step=4,
    )
    assert resumed.terminal is False  # streak restarted at 1
