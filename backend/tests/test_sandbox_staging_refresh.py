"""Direct-write → sandbox staging refresh (direction 3a) unit tests.

Plan: docs/technical-plans/20260905-workspace-single-authority-root-fix.md.
These tests exercise the run-scoped staging-sync seam (Seam A) in isolation:
the pure registry-driven ``refresh_sandbox_staging_path`` primitive, without
touching either concrete backend.
"""

import asyncio
from pathlib import Path

from app.services.sandbox.local.shared import (
    refresh_sandbox_staging_path,
    register_sandbox_staging,
    unregister_sandbox_staging,
)


async def test_refresh_writes_file_into_registered_staging(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    lock = asyncio.Lock()
    register_sandbox_staging("run-1", staging, lock)
    try:
        await refresh_sandbox_staging_path("run-1", "workspace/app/a.kt", b"hello")
    finally:
        unregister_sandbox_staging("run-1")
    assert (staging / "workspace" / "app" / "a.kt").read_bytes() == b"hello"


async def test_refresh_is_noop_when_run_not_registered(tmp_path: Path) -> None:
    # No registration → must not raise and must not touch the filesystem.
    await refresh_sandbox_staging_path("run-missing", "workspace/a.kt", b"x")


async def test_refresh_deletes_file_when_data_is_none(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    target = staging / "workspace" / "app" / "a.kt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    lock = asyncio.Lock()
    register_sandbox_staging("run-1", staging, lock)
    try:
        await refresh_sandbox_staging_path("run-1", "workspace/app/a.kt", None)
    finally:
        unregister_sandbox_staging("run-1")
    assert not target.exists()


async def test_refresh_rejects_path_escaping_staging_root(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    lock = asyncio.Lock()
    register_sandbox_staging("run-1", staging, lock)
    try:
        await refresh_sandbox_staging_path("run-1", "../outside.txt", b"x")
    finally:
        unregister_sandbox_staging("run-1")
    assert not (tmp_path / "outside.txt").exists()


async def test_refresh_rejects_symlink_component_in_staging(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    (staging / "workspace").mkdir(parents=True)
    # A sandbox-planted symlink pointing outside the staging root.
    (staging / "workspace" / "evil").symlink_to(tmp_path)
    lock = asyncio.Lock()
    register_sandbox_staging("run-1", staging, lock)
    try:
        await refresh_sandbox_staging_path("run-1", "workspace/evil/out.txt", b"x")
    finally:
        unregister_sandbox_staging("run-1")
    assert not (tmp_path / "out.txt").exists()


async def test_refresh_skips_non_publish_path_in_isolated_output(tmp_path: Path) -> None:
    """isolated_output runs write only their single publish path into B."""
    staging = tmp_path / "staging"
    staging.mkdir()
    lock = asyncio.Lock()
    register_sandbox_staging(
        "run-1",
        staging,
        lock,
        workspace_mode="isolated_output",
        publish_paths=("workspace/output/sess",),
    )
    try:
        # Inside the publish path: reflected.
        await refresh_sandbox_staging_path("run-1", "workspace/output/sess/result.txt", b"ok")
        assert (staging / "workspace" / "output" / "sess" / "result.txt").read_bytes() == b"ok"
        # Outside the publish path: the read-only remainder stays untouched.
        await refresh_sandbox_staging_path("run-1", "workspace/app.kt", b"nope")
        assert not (staging / "workspace" / "app.kt").exists()
    finally:
        unregister_sandbox_staging("run-1")
