"""api token lifecycle

An API token could previously only be created and deactivated by hand, and
nothing recorded where it was being used from. Adds the three columns that make
the credential's life visible: when it stops working, where it was last used,
and whether its value has ever been replaced.

`expires_at` is nullable rather than defaulted to a date. Existing tokens keep
working -- a migration that silently expired live credentials would be an
outage delivered by deployment -- and the default only applies to tokens issued
from here on.

Revision ID: d7a4e3c81f56
Revises: b83c1d47ea02
Created: 2026-09-09 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd7a4e3c81f56'
down_revision: str | None = 'b83c1d47ea02'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('last_used_ip', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_api_tokens_expires_at'), ['expires_at'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_tokens_expires_at'))
        batch_op.drop_column('rotated_at')
        batch_op.drop_column('last_used_ip')
        batch_op.drop_column('expires_at')
