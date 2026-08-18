"""Add cache-miss token accounting columns.

Background:
    DeepSeek (and other KV-cache providers) report prompt-cache misses via
    usage.prompt_cache_miss_tokens. The platform tracked cache reads and
    creations but not misses, so cache-health regressions (prefix instability,
    cache eviction) were invisible until they hit the bill. These columns
    feed the per-agent miss counters and the daily rollup.

Scope:
    - agents: + cache_miss_tokens_today/month/total (INTEGER NOT NULL DEFAULT 0)
    - daily_token_usage: + cache_miss_tokens (INTEGER NOT NULL DEFAULT 0)
    - Pure DDL; no row reads or backfill.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f064_add_cache_miss_columns"
down_revision: Union[str, Sequence[str], None] = "f063_drop_redundant_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("cache_miss_tokens_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agents",
        sa.Column("cache_miss_tokens_month", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agents",
        sa.Column("cache_miss_tokens_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "daily_token_usage",
        sa.Column("cache_miss_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("daily_token_usage", "cache_miss_tokens")
    op.drop_column("agents", "cache_miss_tokens_total")
    op.drop_column("agents", "cache_miss_tokens_month")
    op.drop_column("agents", "cache_miss_tokens_today")
