"""impersonation log

modules/keycloak_accounts writes one row per "Impersonate" click -- who, as whom,
when. New table, no change to anything existing.

Revision ID: 3c08abd68e98
Revises: 7d2e91b4a6c0
Created: 2026-10-01 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '3c08abd68e98'
down_revision: str | None = '7d2e91b4a6c0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'impersonation_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('impersonator_email', sa.String(length=255), nullable=False),
        sa.Column('impersonator_name', sa.String(length=255), nullable=True),
        sa.Column('target_keycloak_id', sa.String(length=64), nullable=False),
        sa.Column('target_username', sa.String(length=255), nullable=True),
        sa.Column('target_email', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index(
        op.f('ix_impersonation_log_impersonator_email'),
        'impersonation_log', ['impersonator_email'], unique=False,
    )
    op.create_index(
        op.f('ix_impersonation_log_target_keycloak_id'),
        'impersonation_log', ['target_keycloak_id'], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_impersonation_log_target_keycloak_id'), table_name='impersonation_log')
    op.drop_index(op.f('ix_impersonation_log_impersonator_email'), table_name='impersonation_log')
    op.drop_table('impersonation_log')
