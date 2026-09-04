"""Emulating query features a backend does not have.

This is what lets one set of views work against every backend. A provider says
what it can push down; the shim does the rest in Python and, crucially, reports
the union of both as its own capabilities. Callers therefore never branch on
which backend they happen to be talking to.

The cost is honest: emulating a filter means fetching rows that will be thrown
away. ``max_rows`` bounds that, and exceeding it raises
:class:`~app.core.errors.TooManyRows` rather than returning a truncated -- and
therefore silently wrong -- answer.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import TooManyRows, UnsupportedOperation
from app.core.query import AggSpec, ListQuery, Page, filter_ops
from app.core.results import Ctx, Record, WriteResult
from app.providers import local
from app.providers.base import Capabilities, Provider, Records, Rows

#: Ceiling on rows pulled into memory to emulate one query stage.
DEFAULT_MAX_ROWS = 5_000

#: Rows the streaming path may *look at* before giving up.
#:
#: Higher than the in-memory ceiling because streaming holds only one chunk and
#: the matches found so far, so scanning further costs time rather than memory.
#: Still bounded: a filter that matches nothing in a million-row source should
#: report that it cannot answer, not scan for a minute and then say so.
DEFAULT_MAX_SCAN_ROWS = 50_000

#: How many rows to ask for at a time when streaming.
SCAN_CHUNK = 500


class CapabilityShim:
    """Wraps a provider so it satisfies the full query contract.

    Delegates whatever the inner provider advertises, emulates the remainder.
    A fully-capable provider is passed through untouched apart from one extra
    attribute lookup per call.
    """

    def __init__(
        self,
        inner: Provider,
        *,
        search_fields: tuple[str, ...] = (),
        max_rows: int = DEFAULT_MAX_ROWS,
        max_scan_rows: int = DEFAULT_MAX_SCAN_ROWS,
    ) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", type(inner).__name__)
        self.max_rows = max_rows
        self.max_scan_rows = max_scan_rows
        # Search needs to know which columns to look in. The provider may
        # declare them; a resource can override with its own searchable fields.
        self.search_fields = search_fields or inner.capabilities.searchable_fields
        self.capabilities = self._advertised(inner.capabilities)

    def _advertised(self, inner: Capabilities) -> Capabilities:
        """Capabilities as seen from outside: everything the shim can cover.

        Writes are never emulated -- a shim cannot invent a way to persist data
        -- so those pass through unchanged.
        """
        return inner.replace(
            server_filter=True,
            server_sort=True,
            server_search=bool(self.search_fields) or inner.server_search,
            server_paginate=True,
            total_count=True,
            aggregate=True,
            filter_ops=local_supported_ops(),
        )

    @property
    def inner_capabilities(self) -> Capabilities:
        """What the wrapped provider actually does natively."""
        return self.inner.capabilities

    # -- reads --------------------------------------------------------------

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        caps = self.inner.capabilities

        # Decide, per stage, whether the backend handles it or we do. Filtering
        # is pushed down only if the backend supports every operator used --
        # partial pushdown would silently drop conditions.
        push_filter = caps.supports_ops(filter_ops(q.effective_filter))
        # Search is only pushed down when the backend searches exactly the
        # fields we would have searched. A backend that claims server-side
        # search but looks in different columns would answer a different
        # question, so we take the work back rather than accept the mismatch.
        push_search = (
            bool(q.search)
            and caps.server_search
            and tuple(caps.searchable_fields) == tuple(self.search_fields)
            and bool(self.search_fields)
        )
        push_sort = caps.server_sort and bool(q.sort)
        # Pagination can only be pushed down when nothing after it changes the
        # row set. Slicing before filtering would page over the wrong rows.
        push_page = caps.server_paginate and push_filter and push_sort and (
            push_search or not q.search
        )

        if self._can_stream(q, caps, push_filter, push_search):
            return await self._stream(q, ctx, push_filter, push_search)

        inner_q = self._inner_query(q, push_filter, push_search, push_sort, push_page)
        page = await self.inner.list(inner_q, ctx)

        if push_page:
            return self._finish_pushed_page(page, q, caps, ctx)

        rows = list(page.items)
        self._guard(rows, page)
        return local.run_query(
            rows,
            q,
            search_fields=self.search_fields,
            do_filter=not push_filter,
            do_search=not push_search,
            do_sort=not push_sort,
            do_paginate=True,
        )

    def _can_stream(
        self, q: ListQuery, caps: Capabilities, push_filter: bool, push_search: bool
    ) -> bool:
        """Whether this query can be answered by scanning rather than loading.

        Only in a narrow case, and the narrowness is the honest part. Local
        *sorting* needs every matching row before it can order them, and a
        local *total* needs every matching row before it can count them; no
        amount of streaming avoids that. What streaming does avoid is holding a
        whole collection in order to hand back the first page of a filtered
        subset -- which is the common shape for a read-only API that paginates
        but cannot filter.

        So: local filtering or searching to do, nothing to sort, no total
        wanted, and a backend that can paginate for us.
        """
        return (
            caps.server_paginate
            and (not push_filter or not push_search)
            and not q.sort
            and not q.with_total
            and bool(q.effective_filter or q.search)
        )

    async def _stream(
        self, q: ListQuery, ctx: Ctx, push_filter: bool, push_search: bool
    ) -> Page[Record]:
        """Scan the backend a chunk at a time, stopping once the page is full.

        Memory is bounded by the chunk plus the rows destined for this page,
        not by the size of the collection. Time is bounded by
        ``max_scan_rows``: reaching it without filling the page means the
        filter is too sparse to answer this way, and that is reported rather
        than answered with a page that is quietly short.
        """
        needed = q.offset + q.page_size + 1  # one extra answers "is there more?"
        matched: list[Record] = []
        scanned = 0
        page_number = 1

        while len(matched) < needed and scanned < self.max_scan_rows:
            chunk_q = self._inner_query(q, push_filter, push_search, False, False).with_(
                page=page_number, page_size=SCAN_CHUNK, with_total=False
            )
            chunk = await self.inner.list(chunk_q, ctx)
            rows = list(chunk.items)
            if not rows:
                break
            scanned += len(rows)
            page_number += 1

            kept = local.run_query(
                rows,
                q.with_(page=1, page_size=len(rows), with_total=False),
                search_fields=self.search_fields,
                do_filter=not push_filter,
                do_search=not push_search,
                do_sort=False,
                do_paginate=False,
            )
            matched.extend(kept.items)

            if len(rows) < SCAN_CHUNK:
                break  # the source is exhausted

        if len(matched) < needed and scanned >= self.max_scan_rows:
            raise TooManyRows(
                f"{self.name!r} was scanned for {scanned} rows without filling the "
                f"requested page; the filter matches too little of the collection "
                f"to answer without server-side support",
                provider=self.name,
                max_rows=self.max_scan_rows,
            )

        window = matched[q.offset : q.offset + q.page_size]
        return Page(
            items=window,
            page=q.page,
            page_size=q.page_size,
            total=None,
            has_more=len(matched) > q.offset + q.page_size,
        )

    def _inner_query(
        self, q: ListQuery, push_filter: bool, push_search: bool,
        push_sort: bool, push_page: bool,
    ) -> ListQuery:
        """The query to hand the backend, stripped of what we will do ourselves.

        Anything not pushed down is removed so a provider that quietly ignores
        an unsupported argument cannot double-apply it.
        """
        inner = q
        if not push_filter:
            inner = inner.with_(filter=None, scope=None)
        if not push_search:
            inner = inner.with_(search=None)
        if not push_sort:
            inner = inner.with_(sort=())
        if not push_page:
            # Ask for one row beyond the budget: if the backend returns it, we
            # know the set was truncated and can refuse rather than mislead.
            inner = inner.unpaged(self.max_rows + 1)
        return inner

    def _finish_pushed_page(
        self, page: Page[Record], q: ListQuery, caps: Capabilities, ctx: Ctx
    ) -> Page[Record]:
        """The backend did everything; only the total may still be missing."""
        if page.total is not None or not q.with_total:
            return page
        # Without a count we can still answer "is there a next page?" honestly,
        # which is all the pagination control strictly needs.
        return Page(
            items=page.items,
            page=q.page,
            page_size=q.page_size,
            total=None,
            has_more=len(page.items) >= q.page_size,
            cursor=page.cursor,
        )

    def _guard(self, rows: Records, page: Page[Record]) -> None:
        """Refuse to answer from a row set we know to be incomplete."""
        if len(rows) > self.max_rows:
            raise TooManyRows(
                f"{self.name!r} returned more than {self.max_rows} rows for local "
                f"processing; narrow the filter or give the provider server-side support",
                provider=self.name,
                max_rows=self.max_rows,
            )
        # The backend paginated but we still have work to do: the rows we hold
        # are one page of a larger set, so any local filter or sort would be
        # computed over the wrong population.
        if page.has_more and page.total is not None and page.total > len(rows):
            raise TooManyRows(
                f"{self.name!r} paginated its response ({len(rows)} of {page.total} rows) "
                f"but cannot filter or sort server-side, so the result would be incomplete",
                provider=self.name,
            )

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        return await self.inner.get(pk, ctx)

    async def warm(self) -> None:
        warm = getattr(self.inner, "warm", None)
        if warm is not None:
            await warm()

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        if self.inner.capabilities.aggregate:
            return await self.inner.aggregate(spec, ctx)
        # Emulate by pulling the filtered rows and grouping locally.
        q = ListQuery(
            filter=spec.filter,
            scope=spec.scope,
            page_size=self.max_rows + 1,
            with_total=False,
        )
        page = await self.list(q, ctx)
        rows = list(page.items)
        if len(rows) > self.max_rows:
            raise TooManyRows(
                f"aggregating {self.name!r} locally would need more than {self.max_rows} rows",
                provider=self.name,
            )
        return local.run_aggregate(rows, spec)

    # -- writes (never emulated) -------------------------------------------

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        if not self.inner.capabilities.create:
            raise UnsupportedOperation(f"{self.name} cannot create records")
        return await self.inner.create(data, ctx)

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        if not self.inner.capabilities.update:
            raise UnsupportedOperation(f"{self.name} cannot update records")
        return await self.inner.update(pk, data, ctx)

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        # Not emulated. Read-then-write in Python would look like it worked
        # and still lose the race the caller asked to be protected from.
        return await self.inner.update_if(pk, data, expect, ctx)

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        if not self.inner.capabilities.delete:
            raise UnsupportedOperation(f"{self.name} cannot delete records")
        return await self.inner.delete(pk, ctx)

    async def health(self) -> tuple[bool, str]:
        return await self.inner.health()

    async def close(self) -> None:
        closer = getattr(self.inner, "close", None)
        if closer is not None:
            await closer()

    def __repr__(self) -> str:
        return f"<CapabilityShim over {self.inner!r}>"


def local_supported_ops():
    """Operators the Python engine implements -- i.e. all of them."""
    from app.core.query import ALL_OPS

    return ALL_OPS


def ensure_full(provider: Provider, **kwargs: Any) -> Provider:
    """Wrap ``provider`` unless it already satisfies the whole contract.

    Idempotent, so it is safe to call wherever a provider enters the system.
    """
    if isinstance(provider, CapabilityShim):
        return provider
    caps = provider.capabilities
    already_full = (
        caps.server_filter
        and caps.server_sort
        and caps.server_paginate
        and caps.total_count
        and caps.aggregate
        and caps.filter_ops == local_supported_ops()
    )
    if already_full and not kwargs.get("search_fields"):
        return provider
    return CapabilityShim(provider, **kwargs)
