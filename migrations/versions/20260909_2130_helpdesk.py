"""helpdesk

Support tickets: a core helpdesk. `tickets`
tracks a request from receipt to close, with the sweep bookkeeping columns
that mirror `tasks` (`notified_assignee`, `reminded_at`) and a
`thread_message_id` anchor for outbound mail. `ticket_messages` is the
customer-visible thread only -- internal remarks stay in `timeline_entries`,
the panel every other record already has -- with a unique constraint on
`message_id` now, ahead of the ingest branch that dedupes on it.

Revision ID: e1b7c0d43a92
Revises: c5d1a92f7b31
Created: 2026-09-09 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e1b7c0d43a92'
down_revision: str | None = 'c5d1a92f7b31'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'tickets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('reference', sa.String(length=20), nullable=True),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('requester_name', sa.String(length=160), nullable=True),
        sa.Column('requester_email', sa.String(length=160), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False, server_default='new'),
        sa.Column('priority', sa.String(length=20), nullable=True),
        sa.Column('assignee', sa.String(length=160), nullable=True),
        sa.Column('assignee_name', sa.String(length=160), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='staff'),
        sa.Column('thread_message_id', sa.String(length=255), nullable=True),
        sa.Column('created_by', sa.String(length=160), nullable=True),
        sa.Column('created_by_name', sa.String(length=160), nullable=True),
        sa.Column('notified_assignee', sa.String(length=160), nullable=True),
        sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tickets_reference'), ['reference'],
                              unique=False)
        batch_op.create_index(batch_op.f('ix_tickets_requester_email'),
                              ['requester_email'], unique=False)
        batch_op.create_index(batch_op.f('ix_tickets_state'), ['state'], unique=False)
        batch_op.create_index(batch_op.f('ix_tickets_priority'), ['priority'], unique=False)
        batch_op.create_index(batch_op.f('ix_tickets_assignee'), ['assignee'], unique=False)
        batch_op.create_index(batch_op.f('ix_tickets_source'), ['source'], unique=False)
        batch_op.create_index('ix_tickets_open', ['state', 'created_at'], unique=False)

    op.create_table(
        'ticket_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('author', sa.String(length=160), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('message_id', sa.String(length=255), nullable=True),
        sa.Column('in_reply_to', sa.String(length=255), nullable=True),
        sa.Column('references', sa.Text(), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id'),
    )
    with op.batch_alter_table('ticket_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ticket_messages_ticket_id'), ['ticket_id'],
                              unique=False)
        batch_op.create_index(batch_op.f('ix_ticket_messages_direction'), ['direction'],
                              unique=False)
        batch_op.create_index('ix_ticket_messages_thread', ['ticket_id', 'created_at'],
                              unique=False)


def downgrade() -> None:
    with op.batch_alter_table('ticket_messages', schema=None) as batch_op:
        batch_op.drop_index('ix_ticket_messages_thread')
        batch_op.drop_index(batch_op.f('ix_ticket_messages_direction'))
        batch_op.drop_index(batch_op.f('ix_ticket_messages_ticket_id'))
    op.drop_table('ticket_messages')

    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.drop_index('ix_tickets_open')
        batch_op.drop_index(batch_op.f('ix_tickets_source'))
        batch_op.drop_index(batch_op.f('ix_tickets_assignee'))
        batch_op.drop_index(batch_op.f('ix_tickets_priority'))
        batch_op.drop_index(batch_op.f('ix_tickets_state'))
        batch_op.drop_index(batch_op.f('ix_tickets_requester_email'))
        batch_op.drop_index(batch_op.f('ix_tickets_reference'))
    op.drop_table('tickets')
