"""Pure configuration tests for LangGraph PostgreSQL checkpoint wiring."""

from unittest.mock import AsyncMock, patch
import uuid

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
import pytest
from psycopg.conninfo import conninfo_to_dict

from app.config import Settings
from app.services.agent_runtime.checkpointer import (
    CheckpointerConfigurationError,
    checkpoint_database_url,
    checkpoint_serializer,
    create_checkpointer,
    runtime_thread_config,
)
from app.services.agent_runtime.state import RunInputSnapshots, RunRegistrySnapshot


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://app:secret@db.example/clawith",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_runtime_thread_config_accepts_the_actual_thread_identity() -> None:
    run_id = uuid.uuid4()

    assert runtime_thread_config(run_id) == {"configurable": {"thread_id": str(run_id)}}
    assert runtime_thread_config("session-thread") == {
        "configurable": {"thread_id": "session-thread"}
    }


def test_dedicated_checkpoint_url_wins_and_is_normalized_for_psycopg() -> None:
    settings = _settings(
        LANGGRAPH_CHECKPOINT_DATABASE_URL=("postgresql+psycopg://checkpoint:secret@db.example/checkpoints")
    )

    assert checkpoint_database_url(settings) == (
        "postgresql://checkpoint:secret@db.example/checkpoints?options=-c%20search_path%3Dlanggraph_checkpoint%2Cpublic"
    )


def test_primary_asyncpg_url_is_the_checkpoint_fallback() -> None:
    assert checkpoint_database_url(_settings()) == (
        "postgresql://app:secret@db.example/clawith?options=-c%20search_path%3Dlanggraph_checkpoint%2Cpublic"
    )


@pytest.mark.parametrize(
    ("asyncpg_value", "psycopg_value"),
    [
        ("disable", "disable"),
        ("require", "require"),
        ("false", "disable"),
        ("true", "require"),
    ],
)
def test_primary_asyncpg_ssl_query_is_normalized_for_psycopg(
    asyncpg_value: str,
    psycopg_value: str,
) -> None:
    url = checkpoint_database_url(
        _settings(
            DATABASE_URL=(
                "postgresql+asyncpg://app:secret@db.example/clawith"
                f"?ssl={asyncpg_value}"
            )
        )
    )

    parsed = conninfo_to_dict(url)

    assert parsed["sslmode"] == psycopg_value
    assert parsed["options"] == "-c search_path=langgraph_checkpoint,public"


def test_conflicting_asyncpg_ssl_and_psycopg_sslmode_fails_closed() -> None:
    with pytest.raises(CheckpointerConfigurationError, match="conflicting ssl"):
        checkpoint_database_url(
            _settings(
                DATABASE_URL=(
                    "postgresql+asyncpg://app:secret@db.example/clawith"
                    "?ssl=disable&sslmode=require"
                )
            )
        )


def test_checkpoint_url_preserves_existing_options_and_forces_isolated_schema() -> None:
    settings = _settings(
        LANGGRAPH_CHECKPOINT_DATABASE_URL=(
            "postgresql://checkpoint:secret@db.example/checkpoints?sslmode=require&options=-cstatement_timeout%3D5000"
        )
    )

    assert checkpoint_database_url(settings) == (
        "postgresql://checkpoint:secret@db.example/checkpoints?sslmode=require&"
        "options=-cstatement_timeout%3D5000%20-c%20search_path%3Dlanggraph_checkpoint%2Cpublic"
    )


def test_psycopg_parses_search_path_as_a_separate_server_option() -> None:
    settings = _settings(
        LANGGRAPH_CHECKPOINT_DATABASE_URL=(
            "postgresql://checkpoint:secret@db.example/checkpoints?options=-cstatement_timeout%3D5000"
        )
    )

    parsed = conninfo_to_dict(checkpoint_database_url(settings))

    assert parsed["options"] == ("-cstatement_timeout=5000 -c search_path=langgraph_checkpoint,public")


def test_installed_saver_uses_unqualified_checkpoint_tables() -> None:
    migration_sql = "\n".join(AsyncPostgresSaver.MIGRATIONS)

    assert "CREATE TABLE IF NOT EXISTS checkpoint_migrations" in migration_sql
    assert "CREATE TABLE IF NOT EXISTS checkpoints" in migration_sql
    assert "CREATE TABLE IF NOT EXISTS checkpoint_blobs" in migration_sql
    assert "CREATE TABLE IF NOT EXISTS checkpoint_writes" in migration_sql
    assert "langgraph_checkpoint." not in migration_sql


@pytest.mark.parametrize("database_url", ["sqlite:///tmp.db", "", "not-a-url"])
def test_non_postgres_or_invalid_checkpoint_url_fails_closed(
    database_url: str,
) -> None:
    with pytest.raises(CheckpointerConfigurationError):
        checkpoint_database_url(_settings(DATABASE_URL=database_url))


def test_aes_serializer_round_trips_checkpoint_values() -> None:
    serializer = checkpoint_serializer(_settings(LANGGRAPH_AES_KEY="k" * 32))

    assert serializer is not None
    encoded = serializer.dumps_typed({"secret": "checkpoint-value"})

    assert b"checkpoint-value" not in encoded[1]
    assert serializer.loads_typed(encoded) == {"secret": "checkpoint-value"}


def test_runtime_dataclasses_are_explicitly_allowlisted_and_restore_tuples() -> None:
    serializer = checkpoint_serializer(_settings())
    registry = RunRegistrySnapshot(
        tenant_id="tenant-1",
        run_id="run-1",
        goal="finish",
        run_kind="foreground",
        source_type="chat",
        model_id="model-1",
        graph_name="runtime",
        graph_version="v1",
    )
    snapshots = RunInputSnapshots(
        session_context={"version": 1},
        session_context_version=1,
        recent_session_messages=({"role": "user", "content": "go"},),
        related_run_summaries=({"run_id": "parent-1"},),
        initial_input={"message_id": "message-1"},
    )

    restored_registry = serializer.loads_typed(serializer.dumps_typed(registry))
    restored_snapshots = serializer.loads_typed(serializer.dumps_typed(snapshots))

    assert restored_registry == registry
    assert restored_snapshots == snapshots
    assert isinstance(restored_snapshots.recent_session_messages, tuple)
    assert isinstance(restored_snapshots.pending_session_messages, tuple)
    assert isinstance(restored_snapshots.related_run_summaries, tuple)


def test_aes_key_length_is_validated_as_encoded_bytes() -> None:
    with pytest.raises(CheckpointerConfigurationError, match="16, 24, or 32 bytes"):
        checkpoint_serializer(_settings(LANGGRAPH_AES_KEY="too-short"))


@pytest.mark.asyncio
async def test_factory_binds_shared_pool_and_never_runs_checkpointer_setup() -> None:
    saver = AsyncMock()
    pool = AsyncMock()
    test_settings = _settings()

    with (
        patch(
            "app.services.agent_runtime.checkpointer.AsyncPostgresSaver",
            return_value=saver,
        ) as saver_cls,
        patch(
            "app.services.agent_runtime.checkpointer.get_shared_checkpoint_pool",
            new=AsyncMock(return_value=pool),
        ) as pool_factory,
    ):
        created = create_checkpointer(test_settings)
        # Lazy: the shared pool is only requested once the context is entered.
        pool_factory.assert_not_awaited()
        async with created as yielded:
            assert yielded is saver

    pool_factory.assert_awaited_once_with(test_settings)
    saver_cls.assert_called_once()
    call = saver_cls.call_args
    assert call.kwargs["conn"] is pool
    assert isinstance(call.kwargs["serde"], JsonPlusSerializer)
    saver.setup.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_pool_opens_once_and_is_reused() -> None:
    import app.services.agent_runtime.checkpointer as ckpt

    pool = AsyncMock()
    test_settings = _settings()
    try:
        with patch(
            "app.services.agent_runtime.checkpointer.AsyncConnectionPool",
            return_value=pool,
        ) as pool_cls:
            first = await ckpt.get_shared_checkpoint_pool(test_settings)
            second = await ckpt.get_shared_checkpoint_pool(test_settings)
            assert first is pool
            assert second is pool

        pool_cls.assert_called_once()
        call = pool_cls.call_args
        assert call.kwargs["min_size"] == 1
        assert call.kwargs["max_size"] == 4
        assert call.kwargs["timeout"] == 10
        assert call.kwargs["open"] is False
        pool.open.assert_awaited_once()
    finally:
        await ckpt.close_checkpointer_pool()


@pytest.mark.asyncio
async def test_shared_pool_rejects_a_different_database_url_after_open() -> None:
    import app.services.agent_runtime.checkpointer as ckpt

    stale_pool = AsyncMock()
    ckpt._checkpoint_pool = stale_pool
    ckpt._checkpoint_pool_dsn = "postgresql://app:secret@db.example/old"
    try:
        with pytest.raises(CheckpointerConfigurationError):
            await ckpt.get_shared_checkpoint_pool(_settings())
    finally:
        ckpt._checkpoint_pool = None
        ckpt._checkpoint_pool_dsn = None
