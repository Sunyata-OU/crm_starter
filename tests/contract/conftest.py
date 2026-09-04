"""Fixtures for the provider contract suite.

The sample rows and the ``rows`` fixture live in the top-level conftest, since
the audit and RBAC suites use them too.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import Column, Date, Integer, MetaData, String, Table
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.providers.sql import SQLConnection

# Re-exported so this package's tests can import them from here, which is
# where they used to live.
from ..conftest import SAMPLE_ROWS, SEARCH_FIELDS  # noqa: F401

# -- a real SQLite database, so the contract is checked against actual SQL ----



CONTRACT_TABLE = "records"


def _metadata() -> MetaData:
    md = MetaData()
    Table(
        CONTRACT_TABLE,
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("name", String(120)),
        Column("email", String(120)),
        Column("stage", String(20)),
        Column("amount", Integer),
        Column("owner", String(20)),
        Column("closed", Date),
        Column("note", String(200)),
    )
    return md


@pytest_asyncio.fixture
async def sql_connection(rows):
    """An in-memory SQLite database seeded with the sample rows.

    StaticPool keeps every checkout on the same connection; without it each
    checkout would get its own empty ``:memory:`` database.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    md = _metadata()
    async with engine.begin() as conn:
        await conn.run_sync(md.create_all)
        await conn.execute(md.tables[CONTRACT_TABLE].insert(), rows)
    connection = SQLConnection(engine)
    try:
        yield connection
    finally:
        await connection.close()
