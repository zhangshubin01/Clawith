import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.scripts import setup_langgraph_checkpoints


class _RecordingCursor:
    """Fake psycopg cursor recording executed statements in order."""

    def __init__(self, executed: list) -> None:
        self.executed = executed

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, statement, parameters=None):
        self.executed.append(statement)


class _RecordingConnection:
    """Fake psycopg connection whose cursor records into ``executed``."""

    def __init__(self, executed: list) -> None:
        self.executed = executed

    def cursor(self):
        return _RecordingCursor(self.executed)

    async def close(self):
        self.executed.append("closed")


def _fake_connect(executed: list):
    async def fake_connect(*args, **kwargs):
        return _RecordingConnection(executed)

    return fake_connect


@pytest.mark.asyncio
async def test_setup_uses_the_pinned_saver_migration_ledger(monkeypatch) -> None:
    saver = type("Saver", (), {"setup": AsyncMock()})()
    settings = Settings(DATABASE_URL="postgresql+asyncpg://app:secret@db/clawith")
    received = []

    @asynccontextmanager
    async def fake_checkpointer(actual_settings):
        received.append(actual_settings)
        yield saver

    @asynccontextmanager
    async def fake_lock(actual_settings):
        received.append(("lock", actual_settings))
        yield

    drop_indexes = AsyncMock()
    ensure_metadata_index = AsyncMock()

    monkeypatch.setattr(
        setup_langgraph_checkpoints,
        "create_checkpointer",
        fake_checkpointer,
    )
    monkeypatch.setattr(
        setup_langgraph_checkpoints,
        "checkpoint_setup_lock",
        fake_lock,
    )
    monkeypatch.setattr(
        setup_langgraph_checkpoints,
        "drop_redundant_thread_indexes",
        drop_indexes,
    )
    monkeypatch.setattr(
        setup_langgraph_checkpoints,
        "ensure_checkpoint_metadata_index",
        ensure_metadata_index,
    )

    await setup_langgraph_checkpoints.setup_checkpoint_tables(settings)

    assert received == [("lock", settings), settings]
    saver.setup.assert_awaited_once_with()
    drop_indexes.assert_awaited_once_with(settings)
    ensure_metadata_index.assert_awaited_once_with(settings)


@pytest.mark.asyncio
async def test_drop_redundant_thread_indexes_emits_three_idempotent_drops(
    monkeypatch,
) -> None:
    executed = []

    monkeypatch.setattr(
        setup_langgraph_checkpoints.AsyncConnection,
        "connect",
        _fake_connect(executed),
    )

    await setup_langgraph_checkpoints.drop_redundant_thread_indexes(
        Settings(DATABASE_URL="postgresql+asyncpg://app:secret@db/clawith")
    )

    assert executed == [
        "DROP INDEX IF EXISTS langgraph_checkpoint.checkpoints_thread_id_idx",
        "DROP INDEX IF EXISTS langgraph_checkpoint.checkpoint_blobs_thread_id_idx",
        "DROP INDEX IF EXISTS langgraph_checkpoint.checkpoint_writes_thread_id_idx",
        "closed",
    ]


@pytest.mark.asyncio
async def test_ensure_checkpoint_metadata_index_emits_idempotent_gin_create(
    monkeypatch,
) -> None:
    executed = []

    monkeypatch.setattr(
        setup_langgraph_checkpoints.AsyncConnection,
        "connect",
        _fake_connect(executed),
    )

    await setup_langgraph_checkpoints.ensure_checkpoint_metadata_index(
        Settings(DATABASE_URL="postgresql+asyncpg://app:secret@db/clawith")
    )

    assert executed == [
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "checkpoints_metadata_gin_idx "
        "ON langgraph_checkpoint.checkpoints USING gin (metadata jsonb_path_ops)",
        "closed",
    ]


def test_bootstrap_index_cleanup_matches_f066_migration() -> None:
    """The startup mirror must drop exactly what the f066 migration drops.

    Guards the dual maintenance point: the bootstrap runs after every
    ``AsyncPostgresSaver.setup()`` (which recreates the indexes) while the
    migration covers environments that skip the bootstrap — the two lists
    must never drift apart.
    """
    import importlib.util

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "v1_0_0_f066_checkpoint_idx_cleanup.py"
    )
    spec = importlib.util.spec_from_file_location(
        "f066_checkpoint_idx_cleanup",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    migration_targets = {
        f"{schema}.{index_name}"
        for schema, index_name, _table, _column in module._REDUNDANT_THREAD_INDEXES
    }
    bootstrap_targets = {
        statement.split()[4]
        for statement in setup_langgraph_checkpoints._DROP_REDUNDANT_THREAD_INDEXES
    }

    assert bootstrap_targets == migration_targets


@pytest.mark.asyncio
async def test_setup_lock_is_released_after_saver_failure(monkeypatch) -> None:
    events = []

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement, parameters):
            events.append((statement, parameters))

    class Connection:
        def cursor(self):
            return Cursor()

        async def close(self):
            events.append(("closed", None))

    async def fake_connect(*args, **kwargs):
        events.append(("connected", kwargs))
        return Connection()

    monkeypatch.setattr(
        setup_langgraph_checkpoints.AsyncConnection,
        "connect",
        fake_connect,
    )

    with pytest.raises(RuntimeError, match="setup failed"):
        async with setup_langgraph_checkpoints.checkpoint_setup_lock(
            Settings(DATABASE_URL="postgresql+asyncpg://app:secret@db/clawith")
        ):
            raise RuntimeError("setup failed")

    assert "pg_advisory_lock" in events[1][0]
    assert "pg_advisory_unlock" in events[2][0]
    assert events[-1] == ("closed", None)


def test_main_runs_the_explicit_async_setup(monkeypatch) -> None:
    calls = []

    async def fake_setup() -> None:
        calls.append("setup")

    monkeypatch.setattr(
        setup_langgraph_checkpoints,
        "setup_checkpoint_tables",
        fake_setup,
    )

    setup_langgraph_checkpoints.main()

    assert calls == ["setup"]


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\nset -eu\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _run_backend_entrypoint(
    tmp_path: Path,
    *,
    process_role: str = "all",
    python_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    call_log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "id", "printf '1000\\n'\n")
    _write_executable(
        bin_dir / "alembic",
        'printf \'alembic %s\\n\' "$*" >> "$CALL_LOG"\n',
    )
    _write_executable(
        bin_dir / "python",
        'printf \'python %s\\n\' "$*" >> "$CALL_LOG"\nexit "${PYTHON_EXIT:-0}"\n',
    )
    start_command = bin_dir / "start-app"
    _write_executable(start_command, "printf 'start\\n' >> \"$CALL_LOG\"\n")

    backend_dir = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALL_LOG": str(call_log),
        "ALLOW_MIGRATION_FAILURE": "false",
        "PROCESS_ROLE": process_role,
        "PYTHON_EXIT": str(python_exit),
        "START_COMMAND": str(start_command),
        # 418f6c16 fail-fast guard: entrypoint refuses to boot without real
        # secrets; the tests exercise bootstrap ordering, not the guard.
        "SECRET_KEY": "test-secret-key-not-a-placeholder",
        "JWT_SECRET_KEY": "test-jwt-secret-key-not-a-placeholder",
    }
    result = subprocess.run(
        ["bash", str(backend_dir / "entrypoint.sh")],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = call_log.read_text(encoding="utf-8").splitlines()
    return result, calls


def test_backend_entrypoint_bootstraps_checkpoint_before_app_start(tmp_path: Path) -> None:
    result, calls = _run_backend_entrypoint(tmp_path)

    assert result.returncode == 0, result.stderr
    assert calls == [
        "alembic upgrade head",
        "python -X faulthandler -m app.scripts.setup_langgraph_checkpoints",
        "start",
    ]


def test_backend_entrypoint_stops_when_checkpoint_setup_fails(tmp_path: Path) -> None:
    result, calls = _run_backend_entrypoint(tmp_path, python_exit=23)

    assert result.returncode == 23
    assert calls == [
        "alembic upgrade head",
        "python -X faulthandler -m app.scripts.setup_langgraph_checkpoints",
    ]
    assert "LangGraph checkpoint setup FAILED" in result.stdout


def test_backend_entrypoint_keeps_checkpoint_ddl_out_of_runtime_only_roles(
    tmp_path: Path,
) -> None:
    result, calls = _run_backend_entrypoint(tmp_path, process_role="api,worker")

    assert result.returncode == 0, result.stderr
    assert calls == ["start"]
    assert "Skipping LangGraph checkpoint setup" in result.stdout
