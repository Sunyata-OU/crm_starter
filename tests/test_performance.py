"""Query counts, pinned.

An N+1 is invisible in every other kind of test: the page renders, the data is
right, and the only symptom is that the cost grows with the number of rows.
The way to catch it is to count the queries and assert the count does not move
when the data does.

So these tests run the real routes against a real SQLite database at two sizes
and compare. A number changing here is not automatically a failure -- adding a
feature can legitimately cost a query -- but it should never change with the
row count, and a change should be a decision rather than a surprise.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert
from starlette.testclient import TestClient

from app.core.instrument import measure
from app.core.registry import Registry
from app.fields.types import (
    RelationField,
    StatusField,
    TextField,
)
from app.main import create_app
from app.providers.sql import SQLConnection, SQLProvider
from app.resources.resource import Resource
from app.resources.views import BoardView, Card, ListView
from app.resources.views import Column as ViewColumn
from app.settings import Settings

STAGES = [("open", "Open", "blue"), ("won", "Won", "green"), ("lost", "Lost", "red")]

metadata = MetaData()

owners = Table(
    "perf_owners", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(60)),
)

items = Table(
    "perf_items", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(60)),
    Column("stage", String(20)),
    Column("owner_id", Integer),
    # A second relation to the *same* resource, which is what catches a
    # per-field lookup that ignores what another field already fetched.
    Column("reviewer_id", Integer),
)


def _fill(url: str, count: int) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        metadata.create_all(conn)
        conn.execute(insert(owners), [{"id": i, "name": f"Owner {i}"} for i in range(1, 6)])
        conn.execute(
            insert(items),
            [
                {
                    "id": i,
                    "name": f"Item {i}",
                    "stage": STAGES[i % len(STAGES)][0],
                    "owner_id": (i % 5) + 1,
                    "reviewer_id": ((i + 1) % 5) + 1,
                }
                for i in range(1, count + 1)
            ],
        )
    engine.dispose()


def _resources(connection: SQLConnection) -> Registry:
    registry = Registry()
    registry.add_resource(
        Resource(
            "perf_owners",
            provider="",
            label="Owner",
            fields=[TextField("id"), TextField("name")],
        )
    )
    registry.add_resource(
        Resource(
            "perf_items",
            provider="",
            label="Item",
            fields=[
                TextField("id"),
                TextField("name"),
                StatusField("stage", choices=STAGES),
                # The field that tempts an implementation into one query per
                # row: every card needs the owner's name, which lives in
                # another table.
                RelationField("owner_id", resource="perf_owners", display="name"),
                RelationField("reviewer_id", resource="perf_owners", display="name"),
            ],
            views=[
                ListView(
                    columns=[ViewColumn("name", link=True), "stage", "owner_id", "reviewer_id"]
                ),
                BoardView(group_by="stage", card=Card(title="name", subtitle="owner_id")),
            ],
        )
    )
    for name in registry.resource_names:
        registry.resource(name).provider = SQLProvider(connection, name)
    registry._bound = True
    return registry


@pytest_asyncio.fixture
async def client_for(tmp_path):
    """Builds a client over a database of a requested size."""
    clients = []

    async def build(rows: int) -> TestClient:
        path = tmp_path / f"perf{rows}.db"
        _fill(f"sqlite:///{path}", rows)
        from sqlalchemy.ext.asyncio import create_async_engine

        connection = SQLConnection(create_async_engine(f"sqlite+aiosqlite:///{path}"))
        settings = Settings(
            secret_key="k" * 32,
            # debug is what makes the middleware report X-Query-Count, which is
            # how a test reads the cost of a request from the outside.
            debug=True,
            auth_providers=[],
            dev_auth=True,
            csrf_enabled=False,
        )
        app = create_app(settings, _resources(connection))
        client = TestClient(app)
        client.__enter__()
        clients.append((client, connection))
        return client

    yield build

    for client, connection in clients:
        client.__exit__(None, None, None)
        await connection.engine.dispose()


def cost(client: TestClient, url: str) -> int:
    """Queries issued by one request, with caches already warm."""
    client.get(url)
    response = client.get(url)
    assert response.status_code == 200, response.text
    return int(response.headers["X-Query-Count"])


class TestQueryCountsDoNotGrowWithRows:
    async def test_the_list_view_is_flat(self, client_for):
        small, large = await client_for(10), await client_for(200)
        assert cost(small, "/r/perf_items") == cost(large, "/r/perf_items")

    async def test_the_list_view_is_flat_across_page_sizes(self, client_for):
        client = await client_for(200)
        counts = {cost(client, f"/r/perf_items?page_size={n}") for n in (5, 25, 100)}
        # One number for every page size: relation labels are batched, so
        # showing twenty times as many rows costs the same number of queries.
        assert len(counts) == 1

    async def test_the_board_is_flat(self, client_for):
        small, large = await client_for(10), await client_for(200)
        assert cost(small, "/r/perf_items?view=board") == cost(
            large, "/r/perf_items?view=board"
        )

    async def test_the_board_costs_a_query_per_column_plus_a_fixed_overhead(
        self, client_for
    ):
        client = await client_for(60)
        # Three stages: one aggregate for the counts, one query per column for
        # the cards, and one batch for the relation labels. The point of the
        # assertion is the shape -- a board that resolved relations per column
        # would cost three more, and one that counted per column three more
        # again.
        assert cost(client, "/r/perf_items?view=board") == 1 + len(STAGES) + 1


class TestRelationLookupsAreShared:
    async def test_two_fields_on_one_resource_cost_one_lookup(self, client_for):
        """Both relation fields point at perf_owners.

        Resolving them independently means two queries against the same small
        table, fetching overlapping rows. A request-scoped cache makes the
        second one free -- and the count below is what keeps it that way.
        """
        client = await client_for(60)
        # rows + count + one owners lookup shared by both relation fields.
        assert cost(client, "/r/perf_items") == 3


class TestTheInstrumentIsHonest:
    async def test_it_counts_something(self, client_for):
        client = await client_for(10)
        assert cost(client, "/r/perf_items") > 0

    async def test_it_reports_a_repeat(self):
        # Guards the detector itself: `repeated` is what turns a query count
        # into a diagnosis, so it must actually notice a repeated statement.
        with measure() as stats:
            stats.record("SELECT 1", 1.0)
            stats.record("SELECT 1", 1.0)
            stats.record("SELECT 2", 1.0)
        assert stats.repeated == ("SELECT 1", 2)
        assert stats.queries == 3
