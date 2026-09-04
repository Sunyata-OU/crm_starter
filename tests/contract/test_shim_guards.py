"""The shim's refusal behaviour.

Emulating a query stage means holding rows in memory. When that would exceed
the budget the shim must fail loudly: a truncated result set looks like a valid
answer, which is worse than an error.
"""

from __future__ import annotations

import pytest

from app.core.errors import TooManyRows, UnsupportedOperation
from app.core.query import Condition, ListQuery, Op, Sort
from app.core.results import Ctx
from app.providers.base import Capabilities
from app.providers.memory import MemoryProvider
from app.providers.shim import CapabilityShim

CTX = Ctx.system()

DUMB = Capabilities(read=True)


def big_provider(n: int, **kw) -> MemoryProvider:
    rows = [{"id": i, "name": f"row-{i:05d}", "bucket": i % 7} for i in range(1, n + 1)]
    return MemoryProvider(rows, capabilities=Capabilities(read=True, **kw))


class TestRowBudget:
    async def test_within_budget_is_served(self):
        shim = CapabilityShim(big_provider(50), max_rows=100)
        page = await shim.list(ListQuery(filter=Condition("bucket", Op.EQ, 0), page_size=10), CTX)
        assert page.total == 7

    async def test_exceeding_budget_raises_rather_than_truncating(self):
        shim = CapabilityShim(big_provider(500), max_rows=100)
        with pytest.raises(TooManyRows):
            await shim.list(ListQuery(filter=Condition("bucket", Op.EQ, 0), page_size=10), CTX)

    async def test_error_names_the_provider(self):
        shim = CapabilityShim(big_provider(500), max_rows=100)
        with pytest.raises(TooManyRows) as exc:
            await shim.list(ListQuery(page_size=10), CTX)
        assert exc.value.context.get("provider") == "memory"

    async def test_capable_backend_is_not_subject_to_the_budget(self):
        # The backend does the work, so nothing is held in memory and a large
        # table pages normally.
        inner = big_provider(500, server_filter=True, server_sort=True,
                             server_paginate=True, total_count=True)
        shim = CapabilityShim(inner, max_rows=100)
        page = await shim.list(ListQuery(sort=(Sort("id"),), page_size=10), CTX)
        assert len(page.items) == 10
        assert page.total == 500

    async def test_aggregate_over_budget_raises(self):
        from app.core.query import Agg, AggSpec, Measure

        shim = CapabilityShim(big_provider(500), max_rows=100)
        with pytest.raises(TooManyRows):
            await shim.aggregate(AggSpec(group_by=("bucket",), measures=(Measure(Agg.COUNT),)), CTX)


class HardCappedProvider(MemoryProvider):
    """A backend that enforces its own page limit no matter what is asked.

    Realistic: most public APIs cap a page at 100 rows regardless of the
    requested size. The shim cannot filter locally over such a response,
    because the rows it holds are one page of a larger set.
    """

    hard_cap = 100

    async def list(self, q, ctx):
        return await super().list(q.with_(page_size=min(q.page_size, self.hard_cap)), ctx)


class TestPaginatedButIncapableBackend:
    """A backend that pages but cannot filter would give an incomplete answer."""

    async def test_refuses_when_it_would_filter_one_page_of_many(self):
        rows = [{"id": i, "bucket": i % 7} for i in range(1, 501)]
        inner = HardCappedProvider(
            rows,
            capabilities=Capabilities(read=True, server_paginate=True, total_count=True),
        )
        shim = CapabilityShim(inner, max_rows=10_000)
        with pytest.raises(TooManyRows, match="incomplete"):
            await shim.list(ListQuery(filter=Condition("bucket", Op.EQ, 0), page_size=10), CTX)

    async def test_serves_normally_when_the_whole_set_fits_in_one_page(self):
        rows = [{"id": i, "bucket": i % 7} for i in range(1, 51)]
        inner = HardCappedProvider(
            rows,
            capabilities=Capabilities(read=True, server_paginate=True, total_count=True),
        )
        shim = CapabilityShim(inner, max_rows=10_000)
        page = await shim.list(ListQuery(filter=Condition("bucket", Op.EQ, 0), page_size=10), CTX)
        assert page.total == 7


class TestWritesAreNeverInvented:
    async def test_create_on_read_only_backend_is_refused(self):
        shim = CapabilityShim(MemoryProvider([], capabilities=DUMB))
        with pytest.raises(UnsupportedOperation):
            await shim.create({"name": "x"}, CTX)

    async def test_update_on_read_only_backend_is_refused(self):
        shim = CapabilityShim(MemoryProvider([{"id": 1}], capabilities=DUMB))
        with pytest.raises(UnsupportedOperation):
            await shim.update(1, {"name": "x"}, CTX)

    async def test_delete_on_non_deletable_backend_is_refused(self):
        shim = CapabilityShim(
            MemoryProvider([{"id": 1}], capabilities=Capabilities(read=True, write=True))
        )
        with pytest.raises(UnsupportedOperation):
            await shim.delete(1, CTX)


class TestPushdownDecisions:
    """Pagination may only be pushed down when the row set is already final."""

    async def test_pagination_not_pushed_when_filtering_locally(self):
        inner = big_provider(50, server_paginate=True, total_count=True)
        shim = CapabilityShim(inner, max_rows=1000)
        seen: list[ListQuery] = []
        original = inner.list

        async def spy(q, ctx):
            seen.append(q)
            return await original(q, ctx)

        inner.list = spy  # type: ignore[method-assign]
        await shim.list(ListQuery(filter=Condition("bucket", Op.EQ, 0), page_size=5), CTX)
        assert seen[0].page_size > 5, "backend must not slice before we filter"
        assert seen[0].filter is None, "unpushed stages must be stripped from the inner query"

    async def test_unsupported_operator_forces_local_filtering(self):
        # The backend handles equality only; a text filter must come back to us.
        inner = big_provider(50, server_filter=True, filter_ops=frozenset({Op.EQ}))
        shim = CapabilityShim(inner, max_rows=1000)
        page = await shim.list(
            ListQuery(filter=Condition("name", Op.ICONTAINS, "ROW-00003"), page_size=10), CTX
        )
        assert [r["name"] for r in page.items] == ["row-00003"]
