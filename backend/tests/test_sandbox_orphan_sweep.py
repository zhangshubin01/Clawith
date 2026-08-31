"""Orphan sandbox sweep tests — fake docker client + injected active-run set."""

from pathlib import Path

import pytest
from docker import errors

from app.services.sandbox.local.orphan_sweep import sweep_orphan_sandboxes


class FakeOrphanContainer:
    def __init__(self, name: str, run_id: str | None):
        self.name = name
        self.labels = {"clawith.run_id": run_id} if run_id else {}
        self.removed = False

    def remove(self, force=False):
        self.removed = True


class FakeSweepClient:
    def __init__(self, containers):
        self._containers = containers
        self.list_filters = None

    @property
    def containers(self):
        return self

    def list(self, filters=None):
        self.list_filters = filters
        return self._containers


class FailingSweepClient:
    @property
    def containers(self):
        return self

    def list(self, filters=None):
        raise errors.DockerException("daemon gone")


@pytest.mark.asyncio
async def test_removes_inactive_keeps_active():
    stale = FakeOrphanContainer("clawith-exec-deadbeef-000001", "deadbeef-0000-0000-0000-000000000000")
    active = FakeOrphanContainer("clawith-exec-cafebabe-000002", "cafebabe-0000-0000-0000-000000000000")
    client = FakeSweepClient([stale, active])

    result = await sweep_orphan_sandboxes(
        client=client,
        active_run_ids={"cafebabe-0000-0000-0000-000000000000"},
        staging_parent=Path("/nonexistent-sweep-test"),
    )

    assert result["removed_containers"] == ["clawith-exec-deadbeef-000001"]
    assert result["kept_containers"] == ["clawith-exec-cafebabe-000002"]
    assert stale.removed is True
    assert active.removed is False


@pytest.mark.asyncio
async def test_unlabelled_container_is_removed():
    mystery = FakeOrphanContainer("clawith-exec-12345678-000003", None)
    client = FakeSweepClient([mystery])

    result = await sweep_orphan_sandboxes(
        client=client, active_run_ids=set(), staging_parent=Path("/nonexistent-sweep-test")
    )

    assert result["removed_containers"] == ["clawith-exec-12345678-000003"]
    assert mystery.removed is True


@pytest.mark.asyncio
async def test_local_live_session_never_removed():
    # A container whose run_id is in the in-process registry must survive even
    # if the PG active set says otherwise.
    local = FakeOrphanContainer("clawith-exec-abcdef12-000004", "abcdef12-0000-0000-0000-000000000000")
    client = FakeSweepClient([local])

    # Inject via DockerSessionBackend._run_sessions.
    from app.services.sandbox.local.docker_backend import DockerSessionBackend

    DockerSessionBackend._run_sessions["abcdef12-0000-0000-0000-000000000000"] = object()  # type: ignore[arg-type]
    try:
        result = await sweep_orphan_sandboxes(
            client=client, active_run_ids=set(), staging_parent=Path("/nonexistent-sweep-test")
        )
    finally:
        DockerSessionBackend._run_sessions.pop("abcdef12-0000-0000-0000-000000000000", None)

    assert result["removed_containers"] == []
    assert result["kept_containers"] == ["clawith-exec-abcdef12-000004"]
    assert local.removed is False


@pytest.mark.asyncio
async def test_staging_dirs_swept_by_prefix(tmp_path: Path):
    stale_dir = tmp_path / "deadbeef-aaaa1111"
    active_dir = tmp_path / "cafebabe-bbbb2222"
    weird_dir = tmp_path / "not-a-prefix"
    file_inside = tmp_path / "file.txt"
    stale_dir.mkdir()
    active_dir.mkdir()
    weird_dir.mkdir()
    file_inside.write_text("x")

    result = await sweep_orphan_sandboxes(
        client=FakeSweepClient([]),
        active_run_ids={"cafebabe-0000-0000-0000-000000000000"},
        staging_parent=tmp_path,
    )

    assert not stale_dir.exists()
    assert active_dir.exists()
    assert weird_dir.exists()
    assert file_inside.exists()
    assert str(stale_dir) in result["removed_staging"]


@pytest.mark.asyncio
async def test_docker_list_failure_returns_empty_no_raise():
    result = await sweep_orphan_sandboxes(
        client=FailingSweepClient(),
        active_run_ids=set(),
        staging_parent=Path("/nonexistent-sweep-test"),
    )
    assert result == {"removed_containers": [], "kept_containers": [], "removed_staging": []}
