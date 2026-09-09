"""notes and tasks

Two things a back office needs the moment it has a database of its own.

`timeline_entries` goes from a declared-but-unused table to the one the
activity panel writes to: who wrote a note as an identifier rather than only as
a display name, whom it named, what it carries, whether it has been edited,
whether it is pinned, and the composite index the panel's only query wants.

`tasks` is new: work a person owes, assigned by hand, with the two columns the
sweep uses to avoid saying the same thing twice.

Revision ID: c5d1a92f7b31
Revises: d7a4e3c81f56
Created: 2026-09-09 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c5d1a92f7b31'
down_revision: str | None = 'd7a4e3c81f56'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('timeline_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('author_id', sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column('mentions', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('attachments', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True))
        # server_default as well as default: the column is NOT NULL and the
        # table may already have rows, which need a value the database itself
        # can supply.
        batch_op.add_column(sa.Column(
            'pinned', sa.Boolean(), nullable=False, server_default=sa.false()
        ))
        batch_op.create_index(
            batch_op.f('ix_timeline_entries_author_id'), ['author_id'], unique=False
        )
        batch_op.create_index(
            'ix_timeline_record', ['resource', 'record_id', 'created_at'], unique=False
        )

    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('resource', sa.String(length=60), nullable=True),
        sa.Column('record_id', sa.String(length=60), nullable=True),
        sa.Column('assignee', sa.String(length=160), nullable=True),
        sa.Column('assignee_name', sa.String(length=160), nullable=True),
        sa.Column('state', sa.String(length=20), nullable=False, server_default='open'),
        sa.Column('priority', sa.String(length=20), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(length=160), nullable=True),
        sa.Column('created_by_name', sa.String(length=160), nullable=True),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('done_by', sa.String(length=160), nullable=True),
        sa.Column('notified_assignee', sa.String(length=160), nullable=True),
        sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tasks_resource'), ['resource'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_assignee'), ['assignee'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_state'), ['state'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_priority'), ['priority'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_due_at'), ['due_at'], unique=False)
        batch_op.create_index('ix_tasks_open', ['state', 'due_at'], unique=False)


def downgrade() -> None:
    op.drop_table('tasks')

    with op.batch_alter_table('timeline_entries', schema=None) as batch_op:
        batch_op.drop_index('ix_timeline_record')
        batch_op.drop_index(batch_op.f('ix_timeline_entries_author_id'))
        batch_op.drop_column('pinned')
        batch_op.drop_column('edited_at')
        batch_op.drop_column('attachments')
        batch_op.drop_column('mentions')
        batch_op.drop_column('author_id')
