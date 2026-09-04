"""The provider contract.

Every provider in this project -- and every provider a user adds later -- must
pass this suite. It is the specification: if behaviour is not asserted here,
views cannot rely on it.

Each provider is tested twice over: once as itself, and once wrapped in the
capability shim while pretending to support nothing server-side. Both runs must
produce identical results, which is the central architectural claim.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import NotFound, TooManyRows
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
    not_,
    or_,
)
from app.core.results import Ctx, WriteStatus
from app.providers.base import Capabilities
from app.providers.memory import MemoryProvider
from app.providers.shim import CapabilityShim
from app.providers.sql import SQLProvider

from .conftest import CONTRACT_TABLE, SEARCH_FIELDS

CTX = Ctx.system()

#: Backends the contract is checked against. Each entry builds a provider that
#: is expected to behave identically from the outside.
CAPABILITY_PROFILES = {
    # A fully capable backend, e.g. a SQL table.
    "native": lambda rows: MemoryProvider(rows, searchable_fields=SEARCH_FIELDS),
    # A backend that can only hand over rows -- the shim supplies everything.
    "shimmed-dumb": lambda rows: CapabilityShim(
        MemoryProvider(
            rows,
            capabilities=Capabilities(read=True, write=True, delete=True),
        ),
        search_fields=SEARCH_FIELDS,
    ),
    # A backend that filters but cannot sort or paginate.
    "shimmed-partial": lambda rows: CapabilityShim(
        MemoryProvider(
            rows,
            capabilities=Capabilities(
                read=True, write=True, delete=True,
                server_filter=True, total_count=True,
            ),
        ),
        search_fields=SEARCH_FIELDS,
    ),
    # A fully capable backend that is nonetheless wrapped: wrapping must be a
    # no-op, never a behaviour change.
    "shimmed-full": lambda rows: CapabilityShim(
        MemoryProvider(rows), search_fields=SEARCH_FIELDS
    ),
}


#: Backends needing async setup are built from a fixture instead of the rows.
ASYNC_PROFILES = {
    # The real thing: a SQLite table, fully capable, shim not involved.
    "sql": lambda conn: SQLProvider(
        conn, CONTRACT_TABLE, searchable_fields=SEARCH_FIELDS
    ),
    # The same table with its capabilities denied, to prove the shim reproduces
    # SQL behaviour exactly rather than approximately.
    "sql-shimmed": lambda conn: CapabilityShim(
        _crippled(SQLProvider(conn, CONTRACT_TABLE)), search_fields=SEARCH_FIELDS
    ),
}


def _crippled(provider):
    """Strip a provider's server-side capabilities without changing its data."""
    provider.capabilities = Capabilities(read=True, write=True, delete=True)
    return provider


@pytest.fixture(params=[*sorted(CAPABILITY_PROFILES), *sorted(ASYNC_PROFILES)])
def provider(request, rows, sql_connection):
    """A provider under test, one per capability profile."""
    if request.param in CAPABILITY_PROFILES:
        return CAPABILITY_PROFILES[request.param](rows)
    return ASYNC_PROFILES[request.param](sql_connection)


def names(page) -> list[str]:
    return [r["name"] for r in page.items]


def ids(page) -> list:
    return [r.pk for r in page.items]


# -- reads -----------------------------------------------------------------


class TestList:
    async def test_returns_all_rows_by_default(self, provider):
        page = await provider.list(ListQuery(page_size=50), CTX)
        assert len(page.items) == 7
        assert page.total == 7

    async def test_records_expose_primary_key(self, provider):
        page = await provider.list(ListQuery(page_size=50), CTX)
        assert sorted(ids(page)) == [1, 2, 3, 4, 5, 6, 7]

    async def test_pagination_slices_and_reports(self, provider):
        q = ListQuery(sort=(Sort("id"),), page=2, page_size=3)
        page = await provider.list(q, CTX)
        assert ids(page) == [4, 5, 6]
        assert page.page == 2
        assert page.total == 7
        assert page.has_next is True
        assert page.has_prev is True

    async def test_last_page_reports_no_next(self, provider):
        page = await provider.list(ListQuery(sort=(Sort("id"),), page=3, page_size=3), CTX)
        assert ids(page) == [7]
        assert page.has_next is False

    async def test_page_beyond_end_is_empty_not_an_error(self, provider):
        page = await provider.list(ListQuery(page=99, page_size=10), CTX)
        assert list(page.items) == []


class TestSort:
    async def test_ascending(self, provider):
        page = await provider.list(ListQuery(sort=(Sort("amount"),), page_size=50), CTX)
        assert [r["amount"] for r in page.items] == [1200, 3100, 5000, 6400, 7300, 8200, 9900]

    async def test_descending(self, provider):
        q = ListQuery(sort=(Sort("amount", SortDir.DESC),), page_size=50)
        page = await provider.list(q, CTX)
        assert [r["amount"] for r in page.items][0] == 9900

    async def test_multi_key_first_key_wins(self, provider):
        q = ListQuery(sort=(Sort("owner"), Sort("amount", SortDir.DESC)), page_size=50)
        page = await provider.list(q, CTX)
        owners = [r["owner"] for r in page.items]
        assert owners == sorted(owners)
        kim = [r["amount"] for r in page.items if r["owner"] == "kim"]
        assert kim == sorted(kim, reverse=True)

    async def test_nulls_sort_last(self, provider):
        q = ListQuery(sort=(Sort("closed"),), page_size=50)
        page = await provider.list(q, CTX)
        closed = [r["closed"] for r in page.items]
        assert closed[-3:] == [None, None, None]


class TestFilter:
    async def test_equality(self, provider):
        q = ListQuery(filter=Condition("stage", Op.EQ, "won"), page_size=50)
        page = await provider.list(q, CTX)
        assert len(page.items) == 3
        assert {r["stage"] for r in page.items} == {"won"}

    async def test_total_reflects_filter(self, provider):
        q = ListQuery(filter=Condition("stage", Op.EQ, "won"), page_size=2)
        page = await provider.list(q, CTX)
        assert page.total == 3
        assert len(page.items) == 2

    @pytest.mark.parametrize(
        ("op", "value", "expected"),
        [
            (Op.NE, "won", 4),
            (Op.GT, 5000, 4),
            (Op.GTE, 5000, 5),
            (Op.LT, 5000, 2),
            (Op.LTE, 5000, 3),
        ],
    )
    async def test_comparison_operators(self, provider, op, value, expected):
        field = "stage" if isinstance(value, str) else "amount"
        page = await provider.list(ListQuery(filter=Condition(field, op, value), page_size=50), CTX)
        assert len(page.items) == expected

    async def test_in_and_not_in(self, provider):
        page = await provider.list(
            ListQuery(filter=Condition("owner", Op.IN, ["kim", "lee"]), page_size=50), CTX
        )
        assert len(page.items) == 5
        page = await provider.list(
            ListQuery(filter=Condition("owner", Op.NOT_IN, ["kim", "lee"]), page_size=50), CTX
        )
        assert len(page.items) == 2

    async def test_between_is_inclusive(self, provider):
        q = ListQuery(filter=Condition("amount", Op.BETWEEN, [3100, 6400]), page_size=50)
        page = await provider.list(q, CTX)
        assert sorted(r["amount"] for r in page.items) == [3100, 5000, 6400]

    async def test_text_operators(self, provider):
        cases = [
            (Op.CONTAINS, "Lovelace", 1),
            (Op.ICONTAINS, "lovelace", 1),
            (Op.STARTSWITH, "ada", 1),
            (Op.ENDSWITH, "hopper", 1),
        ]
        for op, value, expected in cases:
            q = ListQuery(filter=Condition("name", op, value), page_size=50)
            page = await provider.list(q, CTX)
            assert len(page.items) == expected, f"{op} {value}"

    async def test_null_operators(self, provider):
        page = await provider.list(
            ListQuery(filter=Condition("closed", Op.IS_NULL), page_size=50), CTX
        )
        assert len(page.items) == 3
        page = await provider.list(
            ListQuery(filter=Condition("closed", Op.NOT_NULL), page_size=50), CTX
        )
        assert len(page.items) == 4

    async def test_and_group(self, provider):
        q = ListQuery(
            filter=and_(Condition("stage", Op.EQ, "won"), Condition("owner", Op.EQ, "kim")),
            page_size=50,
        )
        page = await provider.list(q, CTX)
        assert sorted(names(page)) == ["Ada Lovelace", "Grace Hopper"]

    async def test_or_group(self, provider):
        q = ListQuery(
            filter=or_(Condition("stage", Op.EQ, "lost"), Condition("amount", Op.GT, 9000)),
            page_size=50,
        )
        page = await provider.list(q, CTX)
        assert sorted(names(page)) == ["Barbara Liskov", "Katherine J."]

    async def test_negation(self, provider):
        q = ListQuery(filter=not_(Condition("stage", Op.EQ, "won")), page_size=50)
        page = await provider.list(q, CTX)
        assert len(page.items) == 4

    async def test_nested_groups(self, provider):
        q = ListQuery(
            filter=and_(
                or_(Condition("owner", Op.EQ, "kim"), Condition("owner", Op.EQ, "lee")),
                Condition("amount", Op.GTE, 6000),
            ),
            page_size=50,
        )
        page = await provider.list(q, CTX)
        assert sorted(names(page)) == ["Grace Hopper", "Margaret H.", "Radia Perlman"]


class TestScope:
    """Row-level restrictions must apply even when the caller sets no filter."""

    async def test_scope_applies_alone(self, provider):
        q = ListQuery(scope=Condition("owner", Op.EQ, "sam"), page_size=50)
        page = await provider.list(q, CTX)
        assert {r["owner"] for r in page.items} == {"sam"}

    async def test_scope_intersects_with_filter(self, provider):
        q = ListQuery(
            filter=Condition("stage", Op.EQ, "open"),
            scope=Condition("owner", Op.EQ, "sam"),
            page_size=50,
        )
        page = await provider.list(q, CTX)
        assert sorted(names(page)) == ["Alan Turing", "Katherine J."]

    async def test_scope_cannot_be_widened_by_an_or_filter(self, provider):
        # A user filter of OR must not escape the scope: scope is ANDed on top.
        q = ListQuery(
            filter=or_(Condition("owner", Op.EQ, "kim"), Condition("owner", Op.EQ, "lee")),
            scope=Condition("owner", Op.EQ, "sam"),
            page_size=50,
        )
        page = await provider.list(q, CTX)
        assert list(page.items) == []


class TestSearch:
    async def test_matches_across_configured_fields(self, provider):
        page = await provider.list(ListQuery(search="bletchley", page_size=50), CTX)
        assert names(page) == ["Alan Turing"]

    async def test_is_case_insensitive(self, provider):
        page = await provider.list(ListQuery(search="LOVELACE", page_size=50), CTX)
        assert names(page) == ["Ada Lovelace"]

    async def test_all_words_must_match(self, provider):
        page = await provider.list(ListQuery(search="spanning tree", page_size=50), CTX)
        assert names(page) == ["Radia Perlman"]
        page = await provider.list(ListQuery(search="spanning nonsense", page_size=50), CTX)
        assert list(page.items) == []

    async def test_combines_with_filter(self, provider):
        q = ListQuery(search="a", filter=Condition("stage", Op.EQ, "lost"), page_size=50)
        page = await provider.list(q, CTX)
        assert names(page) == ["Barbara Liskov"]

    async def test_empty_search_is_ignored(self, provider):
        page = await provider.list(ListQuery(search="   ", page_size=50), CTX)
        assert len(page.items) == 7


class TestGet:
    async def test_returns_record(self, provider):
        record = await provider.get(1, CTX)
        assert record is not None
        assert record["name"] == "Ada Lovelace"
        assert record.pk == 1

    async def test_accepts_string_key_from_a_url(self, provider):
        record = await provider.get("1", CTX)
        assert record is not None and record.pk == 1

    async def test_missing_returns_none_not_an_error(self, provider):
        assert await provider.get(999, CTX) is None


# -- writes ----------------------------------------------------------------


class TestCreate:
    async def test_returns_the_created_record(self, provider):
        result = await provider.create({"name": "Sophie Wilson", "stage": "open", "amount": 400}, CTX)
        assert result.accepted
        if result.status is WriteStatus.OK:
            assert result.record is not None
            assert result.record["name"] == "Sophie Wilson"
            assert result.record.pk is not None

    async def test_created_record_is_readable(self, provider):
        result = await provider.create({"name": "Sophie Wilson", "amount": 400}, CTX)
        if result.status is not WriteStatus.OK:
            pytest.skip("provider writes asynchronously; read-after-write not guaranteed")
        fetched = await provider.get(result.record.pk, CTX)
        assert fetched is not None and fetched["name"] == "Sophie Wilson"

    async def test_appears_in_list(self, provider):
        before = (await provider.list(ListQuery(page_size=50), CTX)).total
        result = await provider.create({"name": "Sophie Wilson"}, CTX)
        if result.status is not WriteStatus.OK:
            pytest.skip("provider writes asynchronously")
        after = (await provider.list(ListQuery(page_size=50), CTX)).total
        assert after == before + 1


class TestUpdate:
    async def test_changes_only_submitted_fields(self, provider):
        result = await provider.update(3, {"stage": "won"}, CTX)
        assert result.accepted
        if result.status is not WriteStatus.OK:
            pytest.skip("provider writes asynchronously")
        record = await provider.get(3, CTX)
        assert record["stage"] == "won"
        assert record["name"] == "Alan Turing", "unsubmitted fields must be preserved"

    async def test_missing_record_raises_not_found(self, provider):
        with pytest.raises(NotFound):
            await provider.update(999, {"stage": "won"}, CTX)


class TestDelete:
    async def test_removes_the_record(self, provider):
        result = await provider.delete(5, CTX)
        assert result.accepted
        if result.status is not WriteStatus.OK:
            pytest.skip("provider writes asynchronously")
        assert await provider.get(5, CTX) is None

    async def test_missing_record_raises_not_found(self, provider):
        with pytest.raises(NotFound):
            await provider.delete(999, CTX)


# -- aggregation -----------------------------------------------------------


class TestAggregate:
    async def test_group_and_count(self, provider):
        spec = AggSpec(group_by=("stage",), measures=(Measure(Agg.COUNT, alias="n"),))
        result = await provider.aggregate(spec, CTX)
        by_stage = {r["stage"]: r["n"] for r in result}
        assert by_stage == {"won": 3, "open": 3, "lost": 1}

    async def test_sum_and_avg(self, provider):
        spec = AggSpec(
            group_by=("owner",),
            measures=(Measure(Agg.SUM, "amount"), Measure(Agg.AVG, "amount")),
        )
        result = await provider.aggregate(spec, CTX)
        kim = next(r for r in result if r["owner"] == "kim")
        assert kim["sum_amount"] == 5000 + 8200 + 1200
        assert kim["avg_amount"] == pytest.approx((5000 + 8200 + 1200) / 3)

    async def test_respects_filter_and_scope(self, provider):
        spec = AggSpec(
            group_by=("stage",),
            measures=(Measure(Agg.COUNT, alias="n"),),
            scope=Condition("owner", Op.EQ, "kim"),
        )
        result = await provider.aggregate(spec, CTX)
        assert {r["stage"]: r["n"] for r in result} == {"won": 2, "lost": 1}

    async def test_an_ungrouped_count_counts_every_row(self, provider):
        """A grand total, with no group-by at all.

        This is the dashboard's query. It is worth stating separately because
        the SQL for it references no column of the table, so a provider can
        emit a statement with no FROM clause and get 1 back -- a plausible
        number, and the wrong one.
        """
        rows = await provider.aggregate(
            AggSpec(measures=(Measure(Agg.COUNT, alias="count"),)), CTX
        )
        assert len(rows) == 1
        assert rows[0]["count"] == 7

    async def test_an_ungrouped_count_respects_a_filter(self, provider):
        rows = await provider.aggregate(
            AggSpec(
                measures=(Measure(Agg.COUNT, alias="count"),),
                filter=Condition("stage", Op.EQ, "won"),
            ),
            CTX,
        )
        assert rows[0]["count"] == 3

    async def test_an_ungrouped_count_respects_the_scope(self, provider):
        rows = await provider.aggregate(
            AggSpec(
                measures=(Measure(Agg.COUNT, alias="count"),),
                scope=Condition("owner", Op.EQ, "kim"),
            ),
            CTX,
        )
        assert rows[0]["count"] == 3

    async def test_min_max_ignore_nulls(self, provider):
        spec = AggSpec(measures=(Measure(Agg.MIN, "amount"), Measure(Agg.MAX, "amount")))
        result = await provider.aggregate(spec, CTX)
        assert result[0]["min_amount"] == 1200
        assert result[0]["max_amount"] == 9900


# -- conditional writes -----------------------------------------------------


class TestConditionalWrite:
    """``update_if`` is the primitive that makes a write safe under concurrency.

    Every provider that can write must either implement it atomically or refuse
    it outright. What none of them may do is emulate it with a read and a
    write, which would look correct in these tests and still lose rows in
    production.
    """

    async def test_applies_when_the_record_still_matches(self, provider):
        result = await provider.update_if(3, {"stage": "won"}, {"stage": "open"}, CTX)
        assert result is not None
        assert (await provider.get(3, CTX))["stage"] == "won"

    async def test_declines_when_the_record_has_moved_on(self, provider):
        result = await provider.update_if(3, {"stage": "won"}, {"stage": "lost"}, CTX)
        assert result is None
        # And left the record exactly as it was.
        assert (await provider.get(3, CTX))["stage"] == "open"

    async def test_a_null_expectation_matches_a_null_column(self, provider):
        # Row 3 has no closing date. "IS NULL", not "= NULL", which in SQL
        # matches nothing at all.
        result = await provider.update_if(3, {"stage": "won"}, {"closed": None}, CTX)
        assert result is not None

    async def test_only_one_of_two_claims_wins(self, provider):
        # The reason this exists: two schedulers sweeping the same rows. The
        # claim pattern is to write the very column the expectation reads, so
        # the winner's write removes the row from the loser's condition.
        claims = await asyncio.gather(
            provider.update_if(4, {"stage": "won"}, {"stage": "open"}, CTX),
            provider.update_if(4, {"stage": "lost"}, {"stage": "open"}, CTX),
        )
        assert [c is not None for c in claims].count(True) == 1


# -- streaming --------------------------------------------------------------


class TestStreamingLargeCollections:
    """Answering a filtered page without loading the collection.

    The shim's default is to pull rows into memory and refuse past a ceiling,
    which is right when it has to sort or count: both need every matching row.
    When it only has to *filter*, and the caller wants one page and no total, it
    can scan instead -- which is the difference between serving a page from a
    50,000-row API and telling the user it cannot.
    """

    def _big_provider(self, rows: int = 12_000):
        # Paginates, cannot filter: the shape of a plain REST list endpoint.
        data = [
            {"id": i, "name": f"Row {i}", "stage": "won" if i < 40 else "open",
             "owner": "kim", "amount": i, "email": f"r{i}@x.io", "note": None,
             "closed": None}
            for i in range(1, rows + 1)
        ]
        inner = MemoryProvider(
            data,
            capabilities=Capabilities(read=True, server_paginate=True, total_count=True),
            searchable_fields=SEARCH_FIELDS,
        )
        return CapabilityShim(inner, search_fields=SEARCH_FIELDS, max_rows=5_000)

    async def test_a_filtered_page_is_served_from_a_collection_too_big_to_load(self):
        shim = self._big_provider()
        page = await shim.list(
            ListQuery(
                filter=Condition("stage", Op.EQ, "won"),
                page_size=10,
                with_total=False,
            ),
            CTX,
        )
        assert len(page.items) == 10
        assert all(r["stage"] == "won" for r in page.items)
        assert page.has_more is True

    async def test_the_same_query_wanting_a_total_still_refuses(self):
        # Not a regression: counting needs every matching row, so there is
        # nothing to stream. Refusing beats returning a number that is wrong.
        shim = self._big_provider()
        with pytest.raises(TooManyRows):
            await shim.list(
                ListQuery(filter=Condition("stage", Op.EQ, "won"), page_size=10),
                CTX,
            )

    async def test_a_filter_matching_almost_nothing_reports_rather_than_grinds(self):
        shim = self._big_provider()
        shim.max_scan_rows = 2_000
        with pytest.raises(TooManyRows, match="too little"):
            await shim.list(
                ListQuery(
                    filter=Condition("name", Op.EQ, "no such row"),
                    page_size=10,
                    with_total=False,
                ),
                CTX,
            )

    async def test_a_later_page_is_still_correct(self):
        shim = self._big_provider()
        first = await shim.list(
            ListQuery(filter=Condition("stage", Op.EQ, "won"), page_size=10,
                      with_total=False),
            CTX,
        )
        second = await shim.list(
            ListQuery(filter=Condition("stage", Op.EQ, "won"), page=2, page_size=10,
                      with_total=False),
            CTX,
        )
        assert [r.pk for r in first.items] != [r.pk for r in second.items]
        assert len(second.items) == 10


# -- capability reporting --------------------------------------------------


class TestCapabilityReporting:
    async def test_shimmed_provider_advertises_full_query_support(self, rows):
        shim = CapabilityShim(
            MemoryProvider(rows, capabilities=Capabilities(read=True)),
            search_fields=SEARCH_FIELDS,
        )
        caps = shim.capabilities
        assert caps.server_filter and caps.server_sort and caps.server_paginate
        assert caps.total_count and caps.aggregate

    async def test_shim_does_not_invent_write_support(self, rows):
        shim = CapabilityShim(MemoryProvider(rows, capabilities=Capabilities(read=True)))
        assert shim.capabilities.writable is False

    async def test_shim_reports_what_the_backend_really_does(self, rows):
        inner = MemoryProvider(rows, capabilities=Capabilities(read=True))
        shim = CapabilityShim(inner, search_fields=SEARCH_FIELDS)
        assert shim.inner_capabilities.server_filter is False
        assert shim.capabilities.server_filter is True

    async def test_health_is_reported(self, provider):
        healthy, detail = await provider.health()
        assert healthy is True
        assert isinstance(detail, str)
