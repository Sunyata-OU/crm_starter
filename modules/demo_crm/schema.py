"""Tables this module needs.

Declared against the platform's :data:`~app.schema.metadata`, not a metadata of
its own. Importing the module is therefore enough to make Alembic and
``create_all`` aware of these tables, and enabling or removing the module
changes what a migration generates -- which is what makes a module a real unit
of deployment rather than a naming convention.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Table, Text

from app.schema import Instant, metadata, timestamps

companies = Table(
    "companies", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(160), nullable=False, index=True),
    Column("website", String(255)),
    Column("industry", String(60), index=True),
    Column("size", String(20)),
    Column("city", String(80)),
    Column("country", String(80)),
    Column("owner", String(60), index=True),
    Column("notes", Text),
    # Self-referential: a subsidiary points at its parent company. Nullable,
    # because most companies are nobody's subsidiary, and that is what makes a
    # row a root of the tree rather than a special case in the query.
    Column("parent_id", Integer, ForeignKey("companies.id"), index=True),
    # Where the office is. Two nullable columns rather than one point type:
    # every backend has floats, and the map view only ever reads them back.
    Column("latitude", Float),
    Column("longitude", Float),
    *timestamps(),
)

contacts = Table(
    "contacts", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(120), nullable=False, index=True),
    Column("email", String(160), nullable=False, index=True),
    Column("phone", String(40)),
    Column("title", String(120)),
    Column("company_id", Integer, ForeignKey("companies.id"), index=True),
    Column("status", String(20), default="lead", index=True),
    Column("tags", String(255)),
    Column("owner", String(60), index=True),
    Column("subscribed", Boolean, default=True),
    Column("notes", Text),
    Column("last_contacted", Instant),
    # A reference to a stored file, not the bytes: the JSON holds the storage
    # key, original name, size and type.
    Column("avatar", Text),
    Column("attachment", Text),
    *timestamps(),
)

TABLES = (companies, contacts)
