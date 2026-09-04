"""The archive's own table, in the archive's own database.

Identical in shape to ``deals`` and deliberately named differently: a union
matches records by their *columns*, not by the table they came from, so the two
halves may be called whatever each system already calls them.

Where this table lives is not stated here. It is decided by the resource that
names it -- ``db.archive#archived_deals`` -- and read back out of that
declaration by ``app.core.placement``, so the schema and the placement cannot
drift apart.
"""

from __future__ import annotations

from sqlalchemy import Column, Date, Float, Integer, String, Table, Text

from app.schema import metadata, timestamps

archived_deals = Table(
    "archived_deals", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(160), nullable=False, index=True),
    # No foreign keys to companies or contacts: those tables are in the other
    # database, and a constraint across two databases is not a constraint. The
    # archive keeps the company's name instead, which is what an archive is for.
    Column("company_name", String(160)),
    Column("amount", Float, default=0),
    Column("stage", String(20), index=True),
    Column("expected_close", Date),
    Column("closed_on", Date, index=True),
    Column("owner", String(60), index=True),
    Column("origin", String(40)),
    Column("notes", Text),
    *timestamps(),
)

TABLES = (archived_deals,)
