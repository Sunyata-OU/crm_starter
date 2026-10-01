"""task source key

Tasks raised by another system carry the key that
system gave the underlying condition, so the same event delivered twice -- an
outbox retry, a replay -- finds the task it already made instead of making a
second. Unique among *open* tasks only: once one is done, the same condition
arising again is genuinely new work.

Revision ID: 7d2e91b4a6c0
Revises: c5d1a92f7b31
Created: 2026-09-30 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '7d2e91b4a6c0'
down_revision: str | None = 'c5d1a92f7b31'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_key', sa.String(length=160), nullable=True))
    op.create_index(
        'uq_tasks_open_source_key', 'tasks', ['source_key'], unique=True,
        postgresql_where=sa.text("source_key IS NOT NULL AND state IN ('open', 'doing', 'blocked')"),
        sqlite_where=sa.text("source_key IS NOT NULL AND state IN ('open', 'doing', 'blocked')"),
    )


def downgrade() -> None:
    op.drop_index('uq_tasks_open_source_key', table_name='tasks')
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('source_key')
