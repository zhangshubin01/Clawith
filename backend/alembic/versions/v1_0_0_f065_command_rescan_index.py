"""Index the product-reconciler's failed-command rescan query.

Background:
    RuntimeProductReconciler._next() polls for applied commands stuck with
    error_code='product_sync_pending', ordered by (applied_at, created_at, id).
    With no supporting index the planner full-scans agent_run_commands on every
    poll (observed ~209ms mean x 7781 calls ≈ 27min of DB time). A
    (status, error_code, applied_at, created_at, id) btree matches the WHERE
    equality pair and the complete ORDER BY.

Scope:
    - CREATE INDEX only on agent_run_commands; pure DDL, no row reads or data ops.
    - Applies to the product runtime path (reconciler poll loop); no code change.

Downgrade:
    Drops the index.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f065_command_rescan_index"
down_revision: Union[str, Sequence[str], None] = "f064_add_cache_miss_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_run_commands_status_error_applied",
        "agent_run_commands",
        ["status", "error_code", "applied_at", "created_at", "id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_run_commands_status_error_applied",
        table_name="agent_run_commands",
        if_exists=True,
    )
