"""Drop LangGraph checkpoint thread_id indexes that are fully covered by PKs.

Background:
    A database health audit (duplicate-index check) flagged three
    langgraph_checkpoint-schema indexes as covered by their tables' primary
    keys, which already start with (thread_id, ...):

    - checkpoints_thread_id_idx          covered by checkpoints_pkey (thread_id, checkpoint_ns, checkpoint_id)
    - checkpoint_blobs_thread_id_idx     covered by checkpoint_blobs_pkey (thread_id, checkpoint_ns, channel, version)
    - checkpoint_writes_thread_id_idx    covered by checkpoint_writes_pkey (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)

    The checkpoint tables are the hottest write path (checkpoint_blobs alone
    accounts for ~62% of WAL bytes), so the redundant indexes are pure write
    amplification. Unlike the f063 public-schema cleanup, these indexes are
    recreated by the LangGraph library itself: ``AsyncPostgresSaver.setup()``
    issues CREATE INDEX CONCURRENTLY IF NOT EXISTS on every bootstrap, so the
    drop is mirrored in ``app.scripts.setup_langgraph_checkpoints`` (runs right
    after setup()). Both sides are idempotent.

Scope:
    - DROP INDEX IF EXISTS only; pure DDL, no row reads or data backfill.
    - Schema-qualified names; safe on fresh environments where the LangGraph
      tables do not exist yet (IF EXISTS).

Downgrade:
    Recreates the three btree indexes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f066_checkpoint_idx_cleanup"
down_revision: Union[str, Sequence[str], None] = "f065_command_rescan_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (schema, index_name, table_name, column) — one redundant index per entry.
_REDUNDANT_THREAD_INDEXES: tuple[tuple[str, str, str, str], ...] = (
    ("langgraph_checkpoint", "checkpoints_thread_id_idx", "checkpoints", "thread_id"),
    (
        "langgraph_checkpoint",
        "checkpoint_blobs_thread_id_idx",
        "checkpoint_blobs",
        "thread_id",
    ),
    (
        "langgraph_checkpoint",
        "checkpoint_writes_thread_id_idx",
        "checkpoint_writes",
        "thread_id",
    ),
)


def upgrade() -> None:
    for schema, index_name, _table, _column in _REDUNDANT_THREAD_INDEXES:
        op.execute(f'DROP INDEX IF EXISTS "{schema}"."{index_name}"')


def downgrade() -> None:
    # The checkpoint tables only exist after the LangGraph bootstrap step
    # (setup_langgraph_checkpoints), so on fresh environments they may be
    # absent when this downgrade runs — skip the index rebuild then.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names(schema="langgraph_checkpoint"))
    for schema, index_name, table, column in _REDUNDANT_THREAD_INDEXES:
        if table not in existing_tables:
            continue
        op.execute(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{schema}"."{table}" ("{column}")'
        )
