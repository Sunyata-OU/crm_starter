"""The union provider.

One property carries most of this suite: **a union of a partition answers like
the whole**. The sample rows are split across two backends, and every query is
run against the union and against a single provider holding all of them. Any
divergence is a bug in the merge, and stating it this way means the union
inherits the contract rather than re-asserting it clause by clause.

The rest covers what only a union has: routing on the source column, keys that
collide between backends, and the refusals -- because a union that guesses
which backend a write belongs in is worse than one that declines.
"""

from __future__ import annotations

import pytest

from app.core.errors import TooManyRows, UnsupportedOperation
from app.core.query import (
    Agg,
    AggSpec,
    Condition,
    ListQuery,
    Measure,
    Op,
    Sort,
    SortDir,
    and_,
    or_,
)
from app.core.results import Ctx
from app.providers.base import Capabilities
from app.providers.memory import MemoryProvider
from app.providers.shim import CapabilityShim, ensure_full
from app.providers.union import Union, UnionProvider

from .conftest import SEARCH_FIELDS

CTX = Ctx.system()

#: Where the sample rows are split. Uneven on purpose: an even split hides
#: off-by-one errors in the merge window.
SPLIT = 3


def _whole(rows):
    return MemoryProvider(rows, searchable_fields=SEARCH_FIELDS)


def _union(rows, **kw):
    """The same rows, partitioned across two backends."""
    left = ensure_full(
        MemoryProvider(rows[:SPLIT], name="left", searchable_fields=SEARCH_FIELDS),
        search_fields=SEARCH_FIELDS,
    )
    right = ensure_full(
        MemoryProvider(rows[SPLIT:], name="right", searchable_fields=SEARCH_FIELDS),
        search_fields=SEARCH_FIELDS,
    )
    kw.setdefault("searchable_fields", SEARCH_FIELDS)
    return UnionProvider([("left", left), ("right", right)], **kw)


def _dumb_union(rows):
    """A union whose halves can do nothing server-side.

    The same assertions must hold: whether the filtering happened in a database
    or in the shim below the merge is not something the union may depend on.
    """
    nothing = Capabilities(read=True)
    halves = [
        (
            label,
            CapabilityShim(
                MemoryProvider(chunk, name=label, capabilities=nothing),
                search_fields=SEARCH_FIELDS,
            ),
        )
        for label, chunk in (("left", rows[:SPLIT]), ("right", rows[SPLIT:]))
    ]
    return UnionProvider(halves, searchable_fields=SEARCH_FIELDS)


@pytest.fixture(params=["capable", "dumb"])
def pair(request, rows):
    """A union and an equivalent single provider, to compare answer for answer."""
    union = _union(rows) if request.param == "capable" else _dumb_union(rows)
    return union, _whole(rows)


# -- the central property -----------------------------------------------------

#: Every query shape the views actually issue.
QUERIES = [
    ListQuery(sort=(Sort("id"),)),
    ListQuery(sort=(Sort("amount", SortDir.DESC),)),
    ListQuery(sort=(Sort("stage"), Sort("amount", SortDir.DESC))),
    ListQuery(filter=Condition("stage", Op.EQ, "won"), sort=(Sort("id"),)),
    ListQuery(filter=Condition("amount", Op.GT, 5000), sort=(Sort("id"),)),
    ListQuery(filter=Condition("closed", Op.IS_NULL), sort=(Sort("id"),)),
    ListQuery(
        filter=and_(
            Condition("stage", Op.NE, "lost"),
            Condition("amount", Op.GTE, 3000),
        ),
        sort=(Sort("name"),),
    ),
    ListQuery(filter=or_(
        Condition("owner", Op.EQ, "kim"), Condition("owner", Op.EQ, "lee")
    ), sort=(Sort("id"),)),
    ListQuery(search="ada", sort=(Sort("id"),)),
    ListQuery(search="referral", sort=(Sort("id"),)),
    ListQuery(scope=Condition("owner", Op.EQ, "kim"), sort=(Sort("id"),)),
    ListQuery(sort=(Sort("id"),), page=2, page_size=3),
    ListQuery(sort=(Sort("id"),), page=3, page_size=2),
    ListQuery(sort=(Sort("amount"),), page=2, page_size=2),
    ListQuery(sort=(Sort("id"),), page_size=100),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", QUERIES, ids=range(len(QUERIES)))
async def test_union_of_a_partition_answers_like_the_whole(pair, query):
    union, whole = pair
    merged = await union.list(query, CTX)
    single = await whole.list(query, CTX)

    assert [r.pk for r in merged.items] == [r.pk for r in single.items]
    assert merged.total == single.total
    assert merged.has_next == single.has_next


@pytest.mark.asyncio
async def test_every_record_is_stamped_with_its_source(pair):
    union, _ = pair
    page = await union.list(ListQuery(sort=(Sort("id"),)), CTX)
    stamps = {r.pk: r["source"] for r in page.items}
    assert stamps[1] == "left"
    assert stamps[7] == "right"


@pytest.mark.asyncio
async def test_get_finds_a_record_in_either_source(pair):
    union, _ = pair
    left = await union.get(1, CTX)
    right = await union.get(7, CTX)
    assert left is not None and left["source"] == "left"
    assert right is not None and right["source"] == "right"
    assert await union.get(999, CTX) is None


# -- aggregation --------------------------------------------------------------

AGG_SPECS = [
    AggSpec(group_by=("stage",), sort=(Sort("stage"),)),
    AggSpec(
        group_by=("stage",),
        measures=(Measure(Agg.COUNT), Measure(Agg.SUM, "amount")),
        sort=(Sort("stage"),),
    ),
    AggSpec(
        group_by=("owner",),
        measures=(Measure(Agg.MIN, "amount"), Measure(Agg.MAX, "amount")),
        sort=(Sort("owner"),),
    ),
    AggSpec(measures=(Measure(Agg.COUNT), Measure(Agg.SUM, "amount"))),
    AggSpec(
        group_by=("stage",),
        filter=Condition("amount", Op.GT, 3000),
        sort=(Sort("stage"),),
    ),
    # Averages cannot be combined from partial results, so this exercises the
    # fallback path rather than the merge.
    AggSpec(
        group_by=("stage",),
        measures=(Measure(Agg.AVG, "amount"),),
        sort=(Sort("stage"),),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", AGG_SPECS, ids=range(len(AGG_SPECS)))
async def test_aggregates_match_the_whole(pair, spec):
    union, whole = pair
    assert await union.aggregate(spec, CTX) == await whole.aggregate(spec, CTX)


@pytest.mark.asyncio
async def test_can_group_by_the_source_itself(pair):
    """The source column is a real grouping key, not just a display stamp."""
    union, _ = pair
    result = await union.aggregate(
        AggSpec(group_by=("source",), sort=(Sort("source"),)), CTX
    )
    assert result == [
        {"source": "left", "count_all": SPLIT},
        {"source": "right", "count_all": 7 - SPLIT},
    ]


# -- routing on the source column ---------------------------------------------


@pytest.mark.asyncio
async def test_filtering_by_source_visits_only_that_source(rows):
    union = _union(rows)
    page = await union.list(
        ListQuery(filter=Condition("source", Op.EQ, "left"), sort=(Sort("id"),)), CTX
    )
    assert [r.pk for r in page.items] == [1, 2, 3]
    assert page.total == 3


@pytest.mark.asyncio
async def test_source_filter_combines_with_an_ordinary_one(rows):
    union = _union(rows)
    page = await union.list(
        ListQuery(
            filter=and_(
                Condition("source", Op.EQ, "right"),
                Condition("stage", Op.EQ, "open"),
            ),
            sort=(Sort("id"),),
        ),
        CTX,
    )
    assert [r.pk for r in page.items] == [4, 6]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        (Op.NE, "left", [4, 5, 6, 7]),
        (Op.IN, ["left"], [1, 2, 3]),
        (Op.NOT_IN, ["right"], [1, 2, 3]),
    ],
)
async def test_source_routing_operators(rows, op, value, expected):
    union = _union(rows)
    page = await union.list(
        ListQuery(filter=Condition("source", op, value), sort=(Sort("id"),)), CTX
    )
    assert [r.pk for r in page.items] == expected


@pytest.mark.asyncio
async def test_a_source_filter_matching_nothing_is_an_empty_page(rows):
    union = _union(rows)
    page = await union.list(
        ListQuery(filter=Condition("source", Op.EQ, "nowhere")), CTX
    )
    assert page.items == [] and page.total == 0


@pytest.mark.asyncio
async def test_sorting_by_source_is_applied_after_the_merge(rows):
    union = _union(rows)
    page = await union.list(
        ListQuery(sort=(Sort("source", SortDir.DESC), Sort("id"))), CTX
    )
    assert [r.pk for r in page.items] == [4, 5, 6, 7, 1, 2, 3]


@pytest.mark.asyncio
async def test_source_condition_inside_an_or_is_refused(rows):
    """Refused rather than answered approximately.

    ``source = 'left' OR amount > 5000`` needs rows from a source the condition
    excludes, so routing cannot express it and pretending otherwise would drop
    rows silently.
    """
    union = _union(rows)
    query = ListQuery(
        filter=or_(
            Condition("source", Op.EQ, "left"), Condition("amount", Op.GT, 5000)
        )
    )
    with pytest.raises(UnsupportedOperation, match="OR or NOT"):
        await union.list(query, CTX)


@pytest.mark.asyncio
async def test_unsupported_operator_on_the_source_column_is_refused(rows):
    union = _union(rows)
    with pytest.raises(UnsupportedOperation, match="can only filter"):
        await union.list(
            ListQuery(filter=Condition("source", Op.ICONTAINS, "lef")), CTX
        )


# -- colliding keys -----------------------------------------------------------


def _colliding():
    """Two backends that both number their rows from 1."""
    left = MemoryProvider([{"id": 1, "name": "left one"}], name="left")
    right = MemoryProvider([{"id": 1, "name": "right one"}], name="right")
    return left, right


@pytest.mark.asyncio
async def test_without_prefixes_the_first_source_wins():
    left, right = _colliding()
    union = UnionProvider([("left", left), ("right", right)])
    record = await union.get(1, CTX)
    assert record is not None and record["name"] == "left one"


@pytest.mark.asyncio
async def test_prefixed_keys_address_one_row_each():
    left, right = _colliding()
    union = UnionProvider([("left", left), ("right", right)], prefixed=True)

    page = await union.list(ListQuery(sort=(Sort("id"),)), CTX)
    assert sorted(str(r.pk) for r in page.items) == ["left:1", "right:1"]

    record = await union.get("right:1", CTX)
    assert record is not None and record["name"] == "right one"
    assert record.pk == "right:1"


# -- refusals and bounds ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["create", "update", "delete"])
async def test_writes_are_refused(rows, verb):
    union = _union(rows)
    args = {"create": ({},), "update": (1, {}), "delete": (1,)}[verb]
    with pytest.raises(UnsupportedOperation, match="cannot"):
        await getattr(union, verb)(*args, CTX)


@pytest.mark.asyncio
async def test_paging_past_the_row_budget_is_refused_not_truncated(rows):
    """The same bargain the shim makes: a bounded answer or an honest failure."""
    union = _union(rows, max_rows=10)
    with pytest.raises(TooManyRows, match="row budget"):
        await union.list(ListQuery(page=3, page_size=5, sort=(Sort("id"),)), CTX)


@pytest.mark.asyncio
async def test_an_unreachable_source_fails_the_query(rows):
    """A dropped source would return a short answer that looks complete."""

    class Broken(MemoryProvider):
        async def list(self, q, ctx):
            raise ConnectionError("archive is down")

    union = UnionProvider(
        [("live", _whole(rows)), ("archive", Broken([], name="archive"))]
    )
    with pytest.raises(ConnectionError):
        await union.list(ListQuery(sort=(Sort("id"),)), CTX)


@pytest.mark.asyncio
async def test_health_reports_every_source(rows):
    class Sick(MemoryProvider):
        async def health(self):
            return False, "cannot reach the archive"

    union = UnionProvider([("live", _whole(rows)), ("archive", Sick([], name="a"))])
    healthy, detail = await union.health()
    assert healthy is False
    assert "archive: cannot reach the archive" in detail


# -- the declaration ----------------------------------------------------------


def test_union_needs_at_least_two_sources():
    with pytest.raises(ValueError, match="at least two"):
        Union("db.main#deals")


def test_union_source_labels_default_to_the_connection():
    union = Union("db.main#deals", "db.archive#deals")
    assert [s.label for s in union.sources] == ["db.main", "db.archive"]


def test_two_sources_on_one_connection_must_be_labelled():
    with pytest.raises(ValueError, match="distinct labels"):
        Union("db.main#deals_2025", "db.main#deals_2026")


# -- declaring one, end to end ------------------------------------------------


async def _sqlite(rows_):
    """A throwaway SQLite database holding ``rows_``."""
    from sqlalchemy import Column, Integer, MetaData, String, Table
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    md = MetaData()
    table = Table(
        "deals", md,
        Column("id", Integer, primary_key=True),
        Column("name", String(120)),
        Column("amount", Integer),
    )
    async with engine.begin() as conn:
        await conn.run_sync(md.create_all)
        await conn.execute(table.insert(), rows_)
    return engine


@pytest.mark.asyncio
async def test_a_declared_union_binds_and_serves():
    """The declaration path: two real databases, one resource, one screen.

    This is the claim the provider exists to support, so it is asserted against
    actual engines rather than fixtures -- a union that works over
    ``MemoryProvider`` and not over two SQLite files would prove nothing.
    """
    from app.core.connections import ConnectionRegistry, ConnectionSpec
    from app.core.registry import Registry
    from app.fields.types import IntegerField, SelectField, TextField
    from app.providers import builtin as _builtin  # noqa: F401  (registers types)
    from app.resources.resource import Resource

    live = await _sqlite([{"id": 1, "name": "Renewal", "amount": 500}])
    archive = await _sqlite([{"id": 2, "name": "Old deal", "amount": 900}])

    connections = ConnectionRegistry()
    for name, engine in (("db.live", live), ("db.archive", archive)):
        connections.add(ConnectionSpec(name, "sqlalchemy", {"url": "sqlite+aiosqlite://"}))
        # Adopt the already-seeded engine rather than opening a second
        # in-memory database, which would be a different, empty one.
        connections.use(name, _sql_connection(engine))

    registry = Registry(connections)
    registry.add_resource(
        Resource(
            "deals",
            provider=Union("db.live#deals", "db.archive#deals"),
            fields=[
                TextField("id", in_form=False),
                TextField("name", searchable=True),
                IntegerField("amount", in_filter=True),
                SelectField("source", choices=["db.live", "db.archive"], in_filter=True),
            ],
        )
    )
    await registry.bind()

    provider = registry.resource("deals").provider
    page = await provider.list(ListQuery(sort=(Sort("id"),)), CTX)
    assert [(r.pk, r["source"]) for r in page.items] == [
        (1, "db.live"),
        (2, "db.archive"),
    ]
    assert page.total == 2

    # And the shim above it did not have to emulate anything: the union
    # advertises the full contract, so both filters went to their databases.
    filtered = await provider.list(
        ListQuery(filter=Condition("amount", Op.GT, 600), sort=(Sort("id"),)), CTX
    )
    assert [r.pk for r in filtered.items] == [2]

    await registry.close()


def _sql_connection(engine):
    from app.providers.sql import SQLConnection

    return SQLConnection(engine)
