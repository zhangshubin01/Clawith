"""collapse experience scenario/problem/solution into one markdown body

`title` and `applicability` stay first-class columns — they are the only fields
`search_experience` returns as a candidate preview, so the read-or-skip gate that
keeps retrieval cheap depends on them. The three narrative parts were never used
individually by any code path (concatenated for scoring, dumped verbatim on read),
so they collapse into a single free-form markdown `body`.

Existing rows are backfilled by composing the three parts as markdown sections,
so no content is lost.

Revision ID: experience_markdown_body
Revises: experience_retired_at
Create Date: 2026-07-13
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'experience_markdown_body'
down_revision: Union[str, None] = 'experience_retired_at'
branch_labels = None
depends_on = None

_SECTIONS = (('scenario', '场景'), ('problem', '遇到的问题'), ('solution', '解决方式'))


def upgrade() -> None:
    op.add_column(
        'experience_entries',
        sa.Column('body', sa.Text(), nullable=False, server_default=''),
    )
    # Compose the three parts into markdown sections, skipping the empty ones.
    parts = " || ".join(
        f"CASE WHEN coalesce({col}, '') <> '' "
        f"THEN '## {label}' || chr(10) || {col} || chr(10) || chr(10) ELSE '' END"
        for col, label in _SECTIONS
    )
    op.execute(f"UPDATE experience_entries SET body = btrim({parts}, chr(10))")
    for col, _ in _SECTIONS:
        op.drop_column('experience_entries', col)


def downgrade() -> None:
    # Lossy by nature: the markdown body can't be split back into three columns,
    # so it all lands in `scenario` and the other two come back empty.
    for col, _ in _SECTIONS:
        op.add_column(
            'experience_entries',
            sa.Column(col, sa.Text(), nullable=False, server_default=''),
        )
    op.execute("UPDATE experience_entries SET scenario = body")
    op.drop_column('experience_entries', 'body')
