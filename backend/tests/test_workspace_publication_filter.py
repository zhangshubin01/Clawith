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
    ToolRepairBudgetError,
    _content_fingerprint,
    apply_workspace_sync_conflict,
)
from app.services.sandbox.workspace_policy import (
    build_workspace_policy,
    classify_publish_path,
    redact_git_secrets,
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

    cas_files, overwrite_files, derived_paths, git_repos = agent_tools._collect_temp_workspace_files(
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
    assert git_repos == []  # no .git dir → no bundle to create

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
        (".git/HEAD", "git_metadata"),
        (".git/objects/ab/cdef1234", "git_metadata"),
        ("workspace/proj/.git/config", "git_metadata"),
        ("workspace/proj/.git.bundle", "source"),
        (".git-credentials", "derived"),
        ("workspace/pkg/.netrc", "derived"),
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
        # .git metadata never materializes per-file (git-metadata-integrity fix):
        # it is restored whole from a bundle, never as a loose .git tree.
        assert not (temp_ws.root / "workspace" / ".git" / "HEAD").exists()
        assert set(temp_ws.manifest) == {
            "workspace/src/Main.kt",
            "workspace/build/outputs/apk/x.apk",
        }
    finally:
        temp_ws.cleanup()


@pytest.mark.asyncio
async def test_materialize_drops_incomplete_git_dir(monkeypatch, tmp_path):
    """Git metadata never materializes per-file (git-metadata-integrity fix).

    Historical storage may still hold a ``.git`` tree (HEAD + objects); the
    materialize side skips every ``.git`` path outright, so the sandbox copy
    never carries a loose ``.git`` — a half-materialized ``.git`` (HEAD present,
    objects missing) is worse than no ``.git`` at all, and a bundle restore
    rebuilds it whole instead.
    """
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/mydome1/.git/HEAD": b"ref: refs/heads/main\n",
            f"{agent_id}/workspace/mydome1/.git/objects/pack/pack-huge.pack": b"x" * 64,
            f"{agent_id}/workspace/mydome1/src/Main.kt": b"fun main() {}",
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(
        agent_id,
        paths=["workspace"],
        max_file_bytes=32,
    )
    try:
        assert (temp_ws.root / "workspace" / "mydome1" / "src" / "Main.kt").is_file()
        assert not (temp_ws.root / "workspace" / "mydome1" / ".git").exists()
        assert set(temp_ws.manifest) == {"workspace/mydome1/src/Main.kt"}
    finally:
        temp_ws.cleanup()


# ── P0.5 circuit breaker (conflict streak + content fingerprints) ──


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


def _read_result() -> dict:
    return {
        "name": "read_file",
        "execution_status": "succeeded",
        "model_action": "continue",
        "side_effect_state": "confirmed",
        "content": "content",
        "tool_call_id": "call-read",
    }


def _durable_write_result(tool_name: str = "edit_file") -> dict:
    return {
        "name": tool_name,
        "execution_status": "succeeded",
        "model_action": "continue",
        "side_effect_state": "confirmed",
        "content": "updated",
        "tool_call_id": "call-write",
    }


def _default_conflict_fingerprint() -> str:
    return _content_fingerprint(
        "execute_code",
        "workspace_sync_conflict",
        "Workspace publication conflicted.",
    )


def test_workspace_sync_conflict_breaker_trips_at_limit():
    assert WORKSPACE_SYNC_CONFLICT_LIMIT == 3
    budget: object = None
    terminal = False
    for step in range(WORKSPACE_SYNC_CONFLICT_LIMIT):
        transition = apply_workspace_sync_conflict(budget, _conflict_message(), model_step=step)
        budget = transition.budget
        terminal = transition.terminal
    assert terminal is True


def test_workspace_sync_conflict_breaker_keeps_streak_across_read_results():
    budget: object = None
    for step in range(2):
        budget = apply_workspace_sync_conflict(
            budget,
            _conflict_message(),
            model_step=step,
        ).budget
    # A read-only success is part of the P0.5 remediation dance (read the
    # current file before editing it): it must NOT reset the streak, or the
    # breaker could never reach the limit.
    other = apply_workspace_sync_conflict(budget, _read_result(), model_step=3)
    assert other.terminal is False
    assert other.budget["count"] == 2  # streak kept accumulating
    resumed = apply_workspace_sync_conflict(
        other.budget,
        _conflict_message(),
        model_step=4,
    )
    assert resumed.terminal is True  # third conflict in the streak trips it


def test_conflict_streak_survives_read_interleaving():
    """Conflict → read → conflict → read → conflict trips on the 3rd (be39c1ad)."""
    budget: object = None
    transition = None
    for conflict_index in range(WORKSPACE_SYNC_CONFLICT_LIMIT):
        transition = apply_workspace_sync_conflict(
            budget,
            _conflict_message(),
            model_step=conflict_index * 2,
        )
        budget = transition.budget
        if conflict_index < WORKSPACE_SYNC_CONFLICT_LIMIT - 1:
            interleaved = apply_workspace_sync_conflict(
                budget,
                _read_result(),
                model_step=conflict_index * 2 + 1,
            )
            assert interleaved.terminal is False
            budget = interleaved.budget
    assert transition is not None
    assert transition.terminal is True


def test_workspace_sync_conflict_breaker_resets_on_durable_write_success():
    budget: object = None
    for step in range(2):
        budget = apply_workspace_sync_conflict(
            budget,
            _conflict_message(),
            model_step=step,
        ).budget
    fingerprint = _default_conflict_fingerprint()
    assert budget["count"] == 2
    assert budget["fingerprints"] == {fingerprint: 2}
    # A succeeded durable write resets the streak, but the fingerprints
    # survive: they accumulate across resets so the ping-pong hole stays shut.
    reset = apply_workspace_sync_conflict(budget, _durable_write_result(), model_step=3)
    assert reset.terminal is False
    assert reset.budget["count"] == 0
    assert reset.budget["fingerprints"] == {fingerprint: 2}
    # A conflict on different content restarts the streak at 1 and does not
    # trip the fingerprint breaker.
    resumed = apply_workspace_sync_conflict(
        reset.budget,
        {**_conflict_message(), "content": "a different conflict body"},
        model_step=4,
    )
    assert resumed.terminal is False
    assert resumed.budget["count"] == 1
    assert len(resumed.budget["fingerprints"]) == 2


def test_unrelated_failure_neither_resets_nor_counts():
    budget: object = None
    budget = apply_workspace_sync_conflict(budget, _conflict_message(), model_step=0).budget
    unchanged = apply_workspace_sync_conflict(
        budget,
        {
            "name": "execute_code",
            "execution_status": "failed",
            "error_code": "sandbox_execution_failed",
            "model_action": "continue",
            "side_effect_state": "unknown",
            "content": "exit 1",
            "tool_call_id": "call-other",
        },
        model_step=1,
    )
    assert unchanged.terminal is False
    assert unchanged.budget == budget  # no reset, no count
    next_conflict = apply_workspace_sync_conflict(
        unchanged.budget,
        _conflict_message(),
        model_step=2,
    )
    assert next_conflict.terminal is False
    assert next_conflict.budget["count"] == 2


def test_same_content_fingerprint_trips_across_write_resets():
    """conflictA → edit success → conflictA → edit success → conflictA → terminal."""
    budget: object = None
    transition = None
    for round_index in range(WORKSPACE_SYNC_CONFLICT_LIMIT):
        transition = apply_workspace_sync_conflict(
            budget,
            _conflict_message(),
            model_step=round_index * 2,
        )
        budget = transition.budget
        if round_index < WORKSPACE_SYNC_CONFLICT_LIMIT - 1:
            assert transition.terminal is False
            budget = apply_workspace_sync_conflict(
                budget,
                _durable_write_result(),
                model_step=round_index * 2 + 1,
            ).budget
            assert budget["count"] == 0  # the streak resets each time...
    assert transition is not None
    # ...but the same content fingerprint accumulated to the limit and tripped.
    assert transition.terminal is True
    assert transition.budget["count"] == 1
    assert transition.budget["fingerprints"][_default_conflict_fingerprint()] == WORKSPACE_SYNC_CONFLICT_LIMIT


def test_legacy_version_1_budget_still_parses_and_counts():
    legacy = {"version": 1, "count": 2}
    transition = apply_workspace_sync_conflict(legacy, _conflict_message(), model_step=9)
    assert transition.terminal is True  # 2 + 1 reaches the limit
    assert transition.budget["version"] == 2
    assert transition.budget["count"] == 3
    assert transition.budget["fingerprints"] == {_default_conflict_fingerprint(): 1}


def test_legacy_version_1_budget_resets_on_durable_write():
    legacy = {"version": 1, "count": 2}
    reset = apply_workspace_sync_conflict(legacy, _durable_write_result(), model_step=5)
    assert reset.terminal is False
    assert reset.budget == {"version": 2, "count": 0, "fingerprints": {}}


def test_conflict_fingerprints_are_capped_at_sixteen():
    budget: object = None
    for index in range(20):
        budget = apply_workspace_sync_conflict(
            budget,
            {**_conflict_message(), "content": f"conflict-{index}"},
            model_step=index,
        ).budget
    assert len(budget["fingerprints"]) == 16
    # Insertion order is time order: the four oldest were dropped, the newest
    # sixteen survive.
    assert _content_fingerprint("execute_code", "workspace_sync_conflict", "conflict-19") in budget["fingerprints"]
    assert _content_fingerprint("execute_code", "workspace_sync_conflict", "conflict-0") not in budget["fingerprints"]
    assert _content_fingerprint("execute_code", "workspace_sync_conflict", "conflict-3") not in budget["fingerprints"]
    assert _content_fingerprint("execute_code", "workspace_sync_conflict", "conflict-4") in budget["fingerprints"]


def test_oversized_stored_budget_is_trimmed_on_parse():
    oversized = {
        "version": 2,
        "count": 0,
        "fingerprints": {f"fp-{index}": 1 for index in range(30)},
    }
    transition = apply_workspace_sync_conflict(oversized, _read_result(), model_step=1)
    assert len(transition.budget["fingerprints"]) == 16
    assert "fp-29" in transition.budget["fingerprints"]
    assert "fp-0" not in transition.budget["fingerprints"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"version": 3, "count": 0},
        {"version": 2, "count": -1},
        {"version": 2, "count": True},
        {"version": 2, "count": 1, "fingerprints": "nope"},
        {"version": 2, "count": 1, "fingerprints": {"fp": 0}},
        {"version": 2, "count": 1, "fingerprints": {"fp": -2}},
        {"version": 2, "count": 1, "fingerprints": {"fp": True}},
        {"version": 2, "count": 1, "fingerprints": {"": 1}},
        "not-a-mapping",
    ],
)
def test_malformed_conflict_budget_raises(invalid):
    with pytest.raises(ToolRepairBudgetError):
        apply_workspace_sync_conflict(invalid, _conflict_message(), model_step=0)


# ── 8. git credential redaction (publish side, sandbox-git fix plan §3.2) ──


def test_redact_git_secrets_strips_userinfo_from_git_urls():
    data = (
        b"[remote \"origin\"]\n"
        b"\turl = https://user:glpat-token@git.example.com/a/b.git\n"
        b"[core]\n\trepositoryformatversion = 0\n"
    )
    assert redact_git_secrets(".git/config", data) == (
        b"[remote \"origin\"]\n"
        b"\turl = https://git.example.com/a/b.git\n"
        b"[core]\n\trepositoryformatversion = 0\n"
    )


def test_redact_git_secrets_rewrites_extraheader_values():
    data = b"[http \"https://git.example.com/\"]\n\textraheader = PRIVATE-TOKEN: glpat-secret\n"
    out = redact_git_secrets(".git/config", data)
    assert b"glpat-secret" not in out
    assert b"extraheader = <redacted>\n" in out


def test_redact_git_secrets_strips_userinfo_from_fetch_head():
    data = b"abc123\t\tbranch 'main' of https://user:tok@git.example.com/a/b.git\n"
    assert redact_git_secrets(".git/FETCH_HEAD", data) == (
        b"abc123\t\tbranch 'main' of https://git.example.com/a/b.git\n"
    )


def test_redact_git_secrets_extraheader_only_inside_config():
    data = b"extraheader = Authorization: Basic abc\n"
    assert redact_git_secrets(".git/FETCH_HEAD", data) == data


def test_redact_git_secrets_leaves_non_git_and_binary_untouched():
    binary = b"\xff\xfe\x00\x01https://user:t@host/x"
    assert redact_git_secrets("workspace/src/main.kt", b"https://user:t@host/x") == b"https://user:t@host/x"
    assert redact_git_secrets(".git/index", binary) == binary


@pytest.mark.asyncio
async def test_flush_publishes_git_bundle_not_git_metadata(monkeypatch, tmp_path):
    """`.git/**` never materializes nor publishes; only a single bundle blob does.

    Replaces the old ``.git/config`` materialize-and-redact semantics: git
    metadata is packed into one bundle (credential-free by construction) and
    published atomically instead of per-file.
    """
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/mydome1/.git/config": (
                b"[remote \"origin\"]\n\turl = https://user:glpat-tok@git.example.com/a/b.git\n"
            ),
            f"{agent_id}/workspace/mydome1/src/Main.kt": b"fun main() {}",
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    async def _fake_bundle(repo_dir):
        return b"fake-bundle-bytes"

    monkeypatch.setattr(agent_tools.gitlab_workspace, "create_git_bundle", _fake_bundle)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        # .git/config was skipped on materialize (git metadata never loads).
        assert not (temp_ws.root / "workspace" / "mydome1" / ".git" / "config").exists()
        # A repo appears in the sandbox: flush packs it into a bundle and must
        # never publish any .git/** file.
        (temp_ws.root / "workspace" / "mydome1" / ".git").mkdir(parents=True)
        (temp_ws.root / "workspace" / "mydome1" / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        result = await agent_tools.flush_temp_workspace(temp_ws)
        assert result["conflicted"] == []
        # Source file unchanged → skipped; only the bundle is published.
        assert result["updated"] == ["workspace/mydome1/.git.bundle"]
        assert storage.files[f"{agent_id}/workspace/mydome1/.git.bundle"] == b"fake-bundle-bytes"
        # The sandbox's loose .git/HEAD was pruned, never published per-file.
        assert f"{agent_id}/workspace/mydome1/.git/HEAD" not in storage.files
        # Historical .git/config in storage is left untouched (migration concern).
        assert b"glpat-tok" in storage.files[f"{agent_id}/workspace/mydome1/.git/config"]
    finally:
        temp_ws.cleanup()


@pytest.mark.asyncio
async def test_candidate_changes_publish_bundle_not_git_metadata(monkeypatch, tmp_path):
    """The reconciliation path emits a bundle create, never a .git/** candidate.

    This is the exact path where the historical PermissionError surfaced
    (recover_publication → apply_candidate); the old redact-then-CAS semantics
    are gone.
    """
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend(
        {
            f"{agent_id}/workspace/mydome1/.git/config": (
                b"[remote \"origin\"]\n\turl = https://git.example.com/a/b.git\n"
            ),
        }
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    async def _fake_bundle(repo_dir):
        return b"candidate-bundle-bytes"

    monkeypatch.setattr(agent_tools.gitlab_workspace, "create_git_bundle", _fake_bundle)

    # Historical manifest still carries a .git entry (pre-fix residue); the
    # sandbox has a live repo whose .git must be packed, not CAS'd per-file.
    manifest = {
        "workspace/mydome1/.git/config": _manifest_entry(agent_id, "workspace/mydome1/.git/config"),
    }
    temp_ws = _temp_workspace(tmp_path, agent_id, manifest=manifest)
    (tmp_path / "workspace" / "mydome1" / ".git").mkdir(parents=True)
    (tmp_path / "workspace" / "mydome1" / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")

    changes = await agent_tools._workspace_candidate_changes(temp_ws)
    assert [c.path for c in changes] == ["workspace/mydome1/.git.bundle"]
    assert changes[0].operation == "create"
    assert changes[0].data == b"candidate-bundle-bytes"
