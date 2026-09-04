"""Tables this module needs.

Attached to the platform metadata the same way ``demo_crm`` does it; see the
note there. The foreign keys point at tables ``demo_crm`` declares, which is
why this module depends on it.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, Date, Float, ForeignKey, Integer, String, Table, Text

from app.schema import metadata, timestamps

deals = Table(
    "deals", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(160), nullable=False, index=True),
    Column("company_id", Integer, ForeignKey("companies.id"), index=True),
    Column("contact_id", Integer, ForeignKey("contacts.id"), index=True),
    Column("amount", Float, default=0),
    Column("stage", String(20), default="qualifying", index=True),
    Column("probability", Float, default=10),
    Column("expected_close", Date),
    Column("closed_on", Date),
    Column("owner", String(60), index=True),
    Column("source", String(40)),
    Column("notes", Text),
    *timestamps(),
)

activities = Table(
    "activities", metadata,
    Column("id", Integer, primary_key=True),
    Column("subject", String(200), nullable=False),
    Column("kind", String(20), default="task", index=True),
    Column("deal_id", Integer, ForeignKey("deals.id"), index=True),
    Column("contact_id", Integer, ForeignKey("contacts.id"), index=True),
    Column("due_on", Date, index=True),
    Column("minutes", Integer),
    Column("done", Boolean, default=False, index=True),
    Column("owner", String(60), index=True),
    Column("notes", Text),
    *timestamps(),
)

TABLES = (deals, activities)
