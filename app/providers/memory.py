"""An in-memory provider.

Useful in three ways: as the fixture the contract suite runs first, as the
backend for demos and prototypes before a real store exists, and as the
reference showing the smallest complete implementation of the interface.

Its capabilities are configurable, which is how the test suite proves the shim
covers every combination of what a backend can and cannot do.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterable, Mapping
from typing import Any

from app.core.errors import ConflictError, NotFound
from app.core.query import AggSpec, ListQuery, Page
from app.core.results import Ctx, Record, WriteResult
from app.providers import local
from app.providers.base import FULL, BaseProvider, Capabilities, Rows


class MemoryProvider(BaseProvider):
    """Rows held in a dict, guarded by a lock."""

    def __init__(
        self,
        rows: Iterable[Mapping[str, Any]] = (),
        *,
        name: str = "memory",
        pk_field: str = "id",
        capabilities: Capabilities | None = None,
        searchable_fields: tuple[str, ...] = (),
        autoincrement: bool = True,
    ) -> None:
        self.name = name
        self.pk_field = pk_field
        self.autoincrement = autoincrement
        base = capabilities or FULL
        self.capabilities = base.replace(searchable_fields=searchable_fields or base.searchable_fields)
        self._rows: dict[Any, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._counter = itertools.count(1)
        for row in rows:
            data = dict(row)
            pk = data.get(pk_field)
            if pk is None:
                pk = self._next_pk()
                data[pk_field] = pk
            self._rows[pk] = data
            self._bump_counter(pk)

    # -- internals ----------------------------------------------------------

    def _next_pk(self) -> Any:
        return next(self._counter)

    def _bump_counter(self, pk: Any) -> None:
        """Keep generated keys ahead of any integer key supplied by the caller."""
        if isinstance(pk, int) and not isinstance(pk, bool):
            self._counter = itertools.count(pk + 1)

    def _coerce_pk(self, pk: Any) -> Any:
        """Accept a primary key from a URL path (always a string) as-is or as int.

        Route parameters arrive as text; stored keys are often integers. Rather
        than force every caller to know which, try the natural conversion.
        """
        if pk in self._rows:
            return pk
        if isinstance(pk, str):
            try:
                as_int = int(pk)
            except ValueError:
                return pk
            if as_int in self._rows:
                return as_int
        return pk

    @property
    def _search_fields(self) -> tuple[str, ...]:
        return self.capabilities.searchable_fields

    # -- reads --------------------------------------------------------------

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        caps = self.capabilities
        rows = [self._record(dict(r)) for r in self._rows.values()]
        page = local.run_query(
            rows,
            q,
            search_fields=self._search_fields,
            # Honour exactly what this instance claims to support, so a
            # deliberately-crippled instance really does behave like one.
            do_filter=caps.server_filter,
            do_search=caps.server_search,
            do_sort=caps.server_sort,
            do_paginate=caps.server_paginate,
        )
        if not caps.total_count:
            page = Page(
                items=page.items, page=page.page, page_size=page.page_size,
                total=None, has_more=page.has_more,
            )
        return page

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        row = self._rows.get(self._coerce_pk(pk))
        return self._record(dict(row)) if row is not None else None

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        rows = [self._record(dict(r)) for r in self._rows.values()]
        return local.run_aggregate(rows, spec)

    # -- writes -------------------------------------------------------------

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        async with self._lock:
            row = dict(data)
            pk = row.get(self.pk_field)
            if pk is None:
                if not self.autoincrement:
                    return WriteResult.failure(f"{self.pk_field} is required")
                pk = self._next_pk()
                row[self.pk_field] = pk
            elif pk in self._rows:
                raise ConflictError(f"a record with {self.pk_field}={pk!r} already exists")
            self._rows[pk] = row
            self._bump_counter(pk)
            return WriteResult.success(self._record(dict(row)))

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        async with self._lock:
            key = self._coerce_pk(pk)
            existing = self._rows.get(key)
            if existing is None:
                raise NotFound(f"no record with {self.pk_field}={pk!r}")
            # Partial update: only the submitted keys change, and the primary
            # key is never rewritten from the payload.
            merged = {**existing, **data, self.pk_field: key}
            self._rows[key] = merged
            return WriteResult.success(self._record(dict(merged)))

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        """Compare and set, under the same lock every other write takes.

        Genuinely atomic here rather than approximately so: the comparison and
        the write happen without an await between them, so no other coroutine
        can interleave.
        """
        async with self._lock:
            key = self._coerce_pk(pk)
            existing = self._rows.get(key)
            if existing is None:
                raise NotFound(f"no record with {self.pk_field}={pk!r}")
            if any(existing.get(name) != value for name, value in expect.items()):
                return None
            merged = {**existing, **data, self.pk_field: key}
            self._rows[key] = merged
            return WriteResult.success(self._record(dict(merged)))

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        async with self._lock:
            key = self._coerce_pk(pk)
            if key not in self._rows:
                raise NotFound(f"no record with {self.pk_field}={pk!r}")
            removed = self._rows.pop(key)
            return WriteResult.success(self._record(removed))

    async def health(self) -> tuple[bool, str]:
        return True, f"{len(self._rows)} rows in memory"

    # -- test/demo helpers --------------------------------------------------

    def load(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Replace all rows. Synchronous, for fixtures and seeding."""
        self._rows.clear()
        self._counter = itertools.count(1)
        for row in rows:
            data = dict(row)
            pk = data.get(self.pk_field) or self._next_pk()
            data[self.pk_field] = pk
            self._rows[pk] = data
            self._bump_counter(pk)

    def snapshot(self) -> Rows:
        return [dict(r) for r in self._rows.values()]

    def __len__(self) -> int:
        return len(self._rows)
