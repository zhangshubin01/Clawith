"""add scope_type and scope_id to workspace tables

Revision ID: 4bb3f28af46e
Revises: allow_checkpoint_deliveries
Create Date: 2026-07-28 10:45:00

Background:
    Duplicate of work already performed by the ancestor migration
    'unify_runtime_group_schema' on the standard chain. On databases whose
    schema followed the recorded alembic history, every statement below is
    skipped by the guards and this revision is a no-op.

    The re-add branch only executes when a database schema has diverged from
    the alembic history (columns missing despite the ancestor revision being
    recorded). In that state the previous version of this migration blindly
    assumed every existing row was agent-scoped: it added the columns with
    server_default 'agent' and backfilled scope_id = agent_id, which wrote
    NULL into the NOT NULL scope_id for any agent_id-NULL (group) row and
    aborted the upgrade with a cryptic constraint violation.

Scope:
    Adds 'scope_type'/'scope_id' to 'workspace_file_revisions' and
    'workspace_edit_locks' only when they are missing, classifies
    agent-scoped rows from 'agent_id', and fails loudly with actionable
    instructions for rows that cannot be derived automatically (legacy group
    rows, agent_id IS NULL). Check constraints are (re)created when missing.

    Deviation from the DDL-only guideline: one bounded UPDATE backfill plus
    a count precondition are required here because the check constraints this
    migration creates cannot be satisfied otherwise in the diverged-schema
    branch; this mirrors the audit + single-UPDATE pattern already used by
    the ancestor 'unify_runtime_group_schema'. On the normal chain both
    statements match zero rows.

Idempotent:
    Yes — column and constraint guards make re-runs safe; the backfill only
    touches rows that are still unclassified and the precondition passes
    once every row has a valid scope.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

revision: str = '4bb3f28af46e'
down_revision: Union[str, None] = 'allow_checkpoint_deliveries'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ZERO_UUID = "'00000000-0000-0000-0000-000000000000'::uuid"
_SCOPE_TABLES = ('workspace_file_revisions', 'workspace_edit_locks')


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    cols = [c['name'] for c in inspector.get_columns(table)]
    return column in cols


def _constraint_exists(table: str, constraint_name: str) -> bool:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    constraints = [c['name'] for c in inspector.get_unique_constraints(table)]
    constraints.extend(c['name'] for c in inspector.get_check_constraints(table))
    return constraint_name in constraints


def _add_scope_columns(table: str) -> None:
    """Add scope columns as nullable; classification happens before NOT NULL."""
    if not _column_exists(table, 'scope_type'):
        op.add_column(table, sa.Column('scope_type', sa.String(20), nullable=True))
    if not _column_exists(table, 'scope_id'):
        op.add_column(table, sa.Column('scope_id', sa.UUID(), nullable=True))


def _backfill_scope(table: str) -> None:
    """Classify agent rows from agent_id; fail loudly on un-derivable rows.

    Rows with agent_id NULL are legacy group rows. They have no group_id
    column to derive scope_id from, so instead of silently mislabelling them
    this aborts with instructions (PostgreSQL rolls the migration back, and
    the guards make re-running safe once the rows are classified).
    """
    op.execute(sa.text(
        f"UPDATE {table} SET scope_type = 'agent', scope_id = agent_id "
        "WHERE agent_id IS NOT NULL AND "
        f"(scope_type IS NULL OR scope_id IS NULL OR scope_id = {_ZERO_UUID})"
    ))
    leftover = op.get_bind().execute(sa.text(
        f"SELECT count(*) FROM {table} "
        f"WHERE scope_type IS NULL OR scope_id IS NULL OR scope_id = {_ZERO_UUID}"
    )).scalar()
    if leftover:
        raise RuntimeError(
            f"{table}: {leftover} row(s) cannot be scope-backfilled automatically "
            "(agent_id IS NULL, likely legacy group rows). Classify each row "
            "manually — agent rows: scope_type='agent', scope_id=agent_id; "
            "group rows: scope_type='group', scope_id=<group uuid> — then "
            "re-run the migration."
        )


def _tighten_and_constrain(table: str) -> None:
    op.alter_column(table, 'scope_type', existing_type=sa.String(20), nullable=False)
    op.alter_column(table, 'scope_id', existing_type=sa.UUID(), nullable=False)
    if not _constraint_exists(table, f'ck_{table}_scope_type'):
        op.create_check_constraint(
            f'ck_{table}_scope_type',
            table,
            "scope_type IN ('agent', 'group')")
    if not _constraint_exists(table, f'ck_{table}_scope_identity'):
        op.create_check_constraint(
            f'ck_{table}_scope_identity',
            table,
            "(scope_type = 'agent' AND agent_id IS NOT NULL AND scope_id = agent_id) "
            "OR (scope_type = 'group' AND agent_id IS NULL)")


def upgrade() -> None:
    for table in _SCOPE_TABLES:
        if not (_column_exists(table, 'scope_type') and _column_exists(table, 'scope_id')):
            _add_scope_columns(table)
        # Always classify before constraining: a no-op UPDATE on the normal
        # chain, the repair for agent rows on diverged schemas, and the loud
        # precondition for rows that need manual classification.
        _backfill_scope(table)
        _tighten_and_constrain(table)


def downgrade() -> None:
    # Guarded mirror of the original downgrade: never fail on constraints or
    # columns that do not exist (e.g. when the ancestor migration owns them).
    for table in _SCOPE_TABLES:
        for constraint in (f'ck_{table}_scope_identity', f'ck_{table}_scope_type'):
            if _constraint_exists(table, constraint):
                op.drop_constraint(constraint, table, type_='check')
        for column in ('scope_id', 'scope_type'):
            if _column_exists(table, column):
                op.drop_column(table, column)
