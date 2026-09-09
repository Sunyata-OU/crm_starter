"""A resource whose rows are a SELECT, and a resource that writes elsewhere.

Both exist for the same situation: the database belongs to somebody else. It
may be read and it may not be changed -- not by a migration that adds a view,
and not by an UPDATE either. So the shape a screen needs is declared here as a
derived target, and the writes go somewhere that will accept them.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.connections import ConnectionRegistry, ConnectionSpec
from app.core.errors import ProviderError, UnsupportedOperation
from app.core.query import Condition, ListQuery, Op, Sort
from app.core.registry import Registry
from app.core.results import Ctx, Identity
from app.fields.types import IntegerField, TextField
from app.providers import builtin as _builtin  # noqa: F401  (registers types)
from app.providers.memory import MemoryProvider
from app.providers.sql import SQLConnection, register_selectable, selectable
from app.resources.resource import Resource

ADMIN = Identity(subject="t", roles=("admin",), claims={})
CTX = Ctx(identity=ADMIN, request_id="r")


async def _names(provider) -> list[str]:
    page = await provider.list(ListQuery(), CTX)
    return [record["name"] for record in page.items]


async def _engine():
    """Two tables nobody here is allowed to migrate."""
    engine = sa.ext.asyncio.create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    md = sa.MetaData()
    sa.Table(
        "venues", md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(60)),
    )
    sa.Table(
        "bookings", md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("venue_id", sa.Integer),
        sa.Column("guests", sa.Integer),
    )
    async with engine.begin() as conn:
        await conn.run_sync(md.create_all)
        await conn.execute(sa.text(
            "INSERT INTO venues (id, name) VALUES (1, 'Dockside'), (2, 'Old Mill')"
        ))
        await conn.execute(sa.text(
            "INSERT INTO bookings (id, venue_id, guests) VALUES "
            "(1, 1, 40), (2, 1, 10), (3, 2, 25)"
        ))
    return engine


def _occupancy(sources):
    """Guests per venue: a join and a GROUP BY, which no table holds."""
    venues, bookings = sources["venues"], sources["bookings"]
    return (
        sa.select(
            venues.c.id.label("id"),
            venues.c.name.label("name"),
            sa.func.coalesce(sa.func.sum(bookings.c.guests), 0).label("guests"),
        )
        .select_from(venues.outerjoin(bookings, bookings.c.venue_id == venues.c.id))
        .group_by(venues.c.id, venues.c.name)
    )


@pytest.fixture
def target():
    """Registered for the test and taken away again.

    The registry of derived targets is process-wide, like the field and
    provider registries: a module declares one at import time. A test that
    left one behind would change what a later test's `venues` means.
    """
    register_selectable(
        "venue_occupancy",
        tables=("venues", "bookings"),
        build=_occupancy,
        description="Guests booked per venue.",
    )
    yield "venue_occupancy"
    from app.providers.sql import _SELECTABLES

    _SELECTABLES.pop("venue_occupancy", None)


@pytest.fixture
async def registry(target):
    connections = ConnectionRegistry()
    connections.add(ConnectionSpec("db.main", "sqlalchemy", {"url": "sqlite+aiosqlite://"}))
    connections.use("db.main", SQLConnection(await _engine()))

    registry = Registry(connections)
    registry.add_resource(
        Resource(
            "occupancy",
            provider=f"db.main#{target}",
            pk="id",
            fields=[
                IntegerField("id", in_form=False),
                TextField("name", searchable=True),
                IntegerField("guests", in_filter=True),
            ],
        )
    )
    await registry.bind()
    yield registry
    await registry.close()


class TestAScreenOverASelect:
    async def test_the_rows_are_the_select_s_rows(self, registry):
        page = await registry.resource("occupancy").provider.list(
            ListQuery(sort=(Sort("id"),)), CTX
        )
        assert [(r.pk, r["guests"]) for r in page.items] == [(1, 50), (2, 25)]

    async def test_it_filters_like_any_other_table(self, registry):
        page = await registry.resource("occupancy").provider.list(
            ListQuery(filter=Condition("guests", Op.GT, 30)), CTX
        )
        assert [r["name"] for r in page.items] == ["Dockside"]

    async def test_it_sorts_and_pages_like_any_other_table(self, registry):
        query = ListQuery(sort=(Sort.parse("-guests"),), page_size=1)
        first = await registry.resource("occupancy").provider.list(query, CTX)
        assert [r["name"] for r in first.items] == ["Dockside"]
        assert first.total == 2

    async def test_a_row_can_be_fetched_by_the_key_the_resource_named(self, registry):
        record = await registry.resource("occupancy").provider.get(2, CTX)
        assert record["name"] == "Old Mill"

    def test_the_target_is_readable_by_name(self, target):
        assert selectable(target).description == "Guests booked per venue."
        assert selectable("no_such_thing") is None


class TestNothingWritesToIt:
    """Read-only by construction, because a SELECT has no row behind it -- and
    because the database it reads is not this application's to change."""

    def test_the_capabilities_say_so(self, registry):
        assert registry.resource("occupancy").provider.capabilities.write is False

    async def test_a_write_is_refused_rather_than_attempted(self, registry):
        with pytest.raises(UnsupportedOperation):
            await registry.resource("occupancy").provider.create({"name": "x"}, CTX)

    async def test_so_is_a_delete(self, registry):
        with pytest.raises(UnsupportedOperation):
            await registry.resource("occupancy").provider.delete(1, CTX)


class TestTheKeyHasToBeNamed:
    """A subquery declares no primary key, so the resource's `pk` is the only
    thing that can address its rows. Saying that is more use than the
    AttributeError SQLAlchemy would raise three frames down."""

    async def test_a_target_without_the_named_key_says_which_one_is_missing(self):
        from app.providers.sql import SQLProvider

        register_selectable(
            "nameless", tables=("venues",),
            build=lambda s: sa.select(s["venues"].c.name.label("name")),
        )
        try:
            provider = SQLProvider(SQLConnection(await _engine()), "nameless", pk_field="id")
            with pytest.raises(ProviderError, match="no primary key"):
                await provider.get(1, CTX)
        finally:
            from app.providers.sql import _SELECTABLES

            _SELECTABLES.pop("nameless", None)


class TestReadHereWriteThere:
    """`write_provider` is the other half of the same problem: the rows are
    read from a database this application does not own, and the change goes to
    whatever does own it -- an API, a queue, another connection."""

    @staticmethod
    def _resource(write_provider):
        return Resource(
            "venues",
            provider="db.main#venues",
            write_provider=write_provider,
            fields=[IntegerField("id", in_form=False), TextField("name")],
        )

    async def _bound(self, write_provider):
        connections = ConnectionRegistry()
        connections.add(
            ConnectionSpec("db.main", "sqlalchemy", {"url": "sqlite+aiosqlite://"})
        )
        connections.use("db.main", SQLConnection(await _engine()))
        registry = Registry(connections)
        registry.add_resource(self._resource(write_provider))
        await registry.bind()
        return registry

    async def test_a_ready_made_provider_takes_the_writes(self):
        sink = MemoryProvider([])
        registry = await self._bound(sink)
        result = await registry.resource("venues").provider.create({"name": "Boathouse"}, CTX)
        assert result.ok
        assert await _names(sink) == ["Boathouse"]
        # And the read half is untouched: the database still holds two venues.
        page = await registry.resource("venues").provider.list(ListQuery(), CTX)
        assert page.total == 2
        await registry.close()

    async def test_a_factory_is_given_the_registry_and_the_resource(self):
        """Which is the point of allowing one: a write half frequently has to
        look something up in another resource before it knows where to send
        the write, and a factory keeps that dependency in the open."""
        seen = {}
        sink = MemoryProvider([])

        def build(registry, resource):
            seen["resource"] = resource.name
            seen["registry"] = registry
            return sink

        registry = await self._bound(build)
        assert seen == {"resource": "venues", "registry": registry}
        await registry.resource("venues").provider.create({"name": "Lighthouse"}, CTX)
        assert await _names(sink) == ["Lighthouse"]
        await registry.close()

    async def test_an_async_factory_is_awaited(self):
        sink = MemoryProvider([])

        async def build(registry, resource):
            return sink

        registry = await self._bound(build)
        await registry.resource("venues").provider.create({"name": "Quayside"}, CTX)
        assert await _names(sink) == ["Quayside"]
        await registry.close()
