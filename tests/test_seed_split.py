"""Seeding when the schema is spread over several databases.

The failure this prevents is quiet and destructive: ``crm seed`` drops and
recreates the tables it knows about, and the metadata it knows about is the
whole application's. Run against a secondary database without narrowing, it
would create empty copies of the platform's tables there -- and, with
``--reset``, drop them from the metadata it was handed. So the narrowing is
asserted directly rather than left to the CLI.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.modules import select as select_modules
from app.schema import metadata, users
from app.seed import seed


async def _tables(engine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


@pytest.fixture
def modules():
    """The demo modules, loaded so their tables are on the metadata."""
    return select_modules(enabled=["demo_crm", "demo_sales"])


@pytest.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield engine
    await engine.dispose()


class TestSeedingOneDatabaseOfSeveral:
    @pytest.mark.asyncio
    async def test_only_the_named_tables_are_created(self, engine, modules):
        await seed(engine, modules=modules, tables={"companies", "contacts"})
        assert await _tables(engine) == {"companies", "contacts"}

    @pytest.mark.asyncio
    async def test_the_platform_tables_are_left_alone(self, engine, modules):
        """No accounts in the analytics database, and no empty copies either."""
        await seed(engine, modules=modules, tables={"companies", "contacts"})
        assert users.name not in await _tables(engine)

    @pytest.mark.asyncio
    async def test_accounts_are_only_seeded_where_they_live(self, engine, modules):
        """There is one set of accounts, not one per database."""
        summary = await seed(engine, modules=modules, tables={"companies", "contacts"})
        assert "users" not in summary
        assert "roles" not in summary

    @pytest.mark.asyncio
    async def test_a_module_whose_tables_are_elsewhere_does_not_seed(self, engine, modules):
        """Its seeder would insert into tables this database does not have."""
        summary = await seed(engine, modules=modules, tables={"companies", "contacts"})
        assert "companies" in summary
        assert "deals" not in summary

    @pytest.mark.asyncio
    async def test_seeding_the_platform_database_still_does_everything(self, engine, modules):
        """The narrowing must not change the single-database path."""
        every = set(metadata.tables)
        summary = await seed(engine, modules=modules, tables=every)
        assert {"users", "roles", "grants", "companies", "deals"} <= set(summary)

    @pytest.mark.asyncio
    async def test_no_narrowing_means_the_whole_schema(self, engine, modules):
        summary = await seed(engine, modules=modules)
        assert "users" in summary and "deals" in summary
        assert users.name in await _tables(engine)

    @pytest.mark.asyncio
    async def test_reset_only_drops_this_databases_tables(self, engine, modules):
        """The dangerous one: a reset must not reach into the other database.

        Seeding the whole schema and then re-seeding a narrowed subset with
        ``reset=True`` must leave the tables outside the subset standing.
        """
        await seed(engine, modules=modules)
        before = await _tables(engine)
        assert users.name in before

        await seed(engine, modules=modules, reset=True, tables={"companies"})
        after = await _tables(engine)
        assert users.name in after, "a narrowed reset dropped another database's table"
        assert "companies" in after
