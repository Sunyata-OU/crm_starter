"""company parent and coordinates

Adds the two things a tree view and a map view need from a demo resource: a
self-referential parent, and a point. Both nullable -- a company with no parent
is a root, and one with no coordinates is simply not on the map.

Revision ID: b83c1d47ea02
Revises: a4f99851b539
Created: 2026-09-06 10:50:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.schema import metadata

revision: str = 'b83c1d47ea02'
down_revision: str | None = 'a4f99851b539'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enabled() -> bool:
    """Only when the module that declares the table is enabled."""
    return "companies" in metadata.tables


def upgrade() -> None:
    if not _enabled():
        return
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('latitude', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('longitude', sa.Float(), nullable=True))
        batch_op.create_index(batch_op.f('ix_companies_parent_id'), ['parent_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_companies_parent_id_companies', 'companies', ['parent_id'], ['id']
        )


def downgrade() -> None:
    if not _enabled():
        return
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.drop_constraint('fk_companies_parent_id_companies', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_companies_parent_id'))
        batch_op.drop_column('longitude')
        batch_op.drop_column('latitude')
        batch_op.drop_column('parent_id')
