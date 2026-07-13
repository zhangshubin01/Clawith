"""Static contract tests for the channel delivery outbox migration."""

from importlib import util
from pathlib import Path

from app.models.channel_delivery import ChannelDelivery


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202607141500_create_channel_delivery_outbox.py"
)


def _load_migration():
    spec = util.spec_from_file_location("create_channel_delivery_outbox", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_channel_delivery_migration_follows_the_runtime_schema_head() -> None:
    migration = _load_migration()

    assert migration.revision == "create_channel_delivery_outbox"
    assert migration.down_revision == "add_group_workspace_scope"


def test_channel_delivery_model_is_an_outbox_not_runtime_state() -> None:
    columns = set(ChannelDelivery.__table__.columns.keys())

    assert {
        "run_id",
        "message_id",
        "channel",
        "target",
        "status",
        "attempt_count",
        "next_attempt_at",
        "claim_expires_at",
    } <= columns
    assert not {
        "runtime_thread_id",
        "checkpoint_id",
        "graph_name",
        "graph_state",
        "next_node",
    } & columns


def test_channel_delivery_model_has_retry_and_idempotency_constraints() -> None:
    table = ChannelDelivery.__table__
    names = {constraint.name for constraint in table.constraints}
    indexes = {index.name for index in table.indexes}

    assert "uq_channel_deliveries_run_idempotency" in names
    assert "uq_channel_deliveries_message_id" in names
    assert "ck_channel_deliveries_attempt_count" in names
    assert "ix_channel_deliveries_pending_due" in indexes

