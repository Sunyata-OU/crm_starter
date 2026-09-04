"""Combining providers.

Two shapes come up often enough to be worth naming.

``CompositeProvider`` reads from one backend and writes to another -- a read
replica with writes going to a queue, or a cached view over an authoritative
API. The resource is declared once and neither half knows about the other.

``ReadOnly`` wraps any provider to refuse writes, for a resource that should be
visible but never edited through this application.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import UnsupportedOperation
from app.core.query import AggSpec, ListQuery, Page
from app.core.results import Ctx, Record, WriteResult
from app.providers.base import Capabilities, Provider, Rows


class CompositeProvider:
    """Reads from one provider, writes through another.

    The advertised capabilities are the read side's query support combined with
    the write side's write support, so the UI offers exactly what will work --
    including reporting queued writes when the write half is asynchronous.
    """

    def __init__(self, read: Provider, write: Provider, *, name: str = "") -> None:
        self.reader = read
        self.writer = write
        self.name = name or f"composite({getattr(read, 'name', '?')}+{getattr(write, 'name', '?')})"

        read_caps = read.capabilities
        write_caps = write.capabilities
        self.capabilities = Capabilities(
            read=read_caps.read,
            create=write_caps.create,
            update=write_caps.update,
            write=write_caps.write,
            delete=write_caps.delete,
            server_filter=read_caps.server_filter,
            server_sort=read_caps.server_sort,
            server_search=read_caps.server_search,
            server_paginate=read_caps.server_paginate,
            total_count=read_caps.total_count,
            aggregate=read_caps.aggregate,
            filter_ops=read_caps.filter_ops,
            searchable_fields=read_caps.searchable_fields,
            write_mode=write_caps.write_mode,
        )

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        return await self.reader.list(q, ctx)

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        return await self.reader.get(pk, ctx)

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        return await self.reader.aggregate(spec, ctx)

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        return await self.writer.create(data, ctx)

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        return await self.writer.update(pk, data, ctx)

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        """Delegated to the writer, and only safe when both halves agree.

        A conditional write means nothing if the expectation is evaluated
        against a different copy of the data from the one being written -- a
        read replica lagging behind the primary, say. So this asks the writer,
        which is the side that will apply it, and never the reader.
        """
        return await self.writer.update_if(pk, data, expect, ctx)

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        return await self.writer.delete(pk, ctx)

    async def warm(self) -> None:
        for half in (self.reader, self.writer):
            warm = getattr(half, "warm", None)
            if warm is not None:
                await warm()

    async def health(self) -> tuple[bool, str]:
        read_ok, read_detail = await self.reader.health()
        write_ok, write_detail = await self.writer.health()
        return read_ok and write_ok, f"read: {read_detail}; write: {write_detail}"

    async def close(self) -> None:
        for half in (self.reader, self.writer):
            closer = getattr(half, "close", None)
            if closer is not None:
                await closer()

    def __repr__(self) -> str:
        return f"<CompositeProvider read={self.reader!r} write={self.writer!r}>"


class ReadOnly:
    """Wraps a provider so every write is refused.

    Useful for a resource backed by a store this application must not change,
    even though the credentials would allow it.
    """

    def __init__(self, inner: Provider, *, reason: str = "") -> None:
        self.inner = inner
        self.name = f"readonly({getattr(inner, 'name', '?')})"
        self.reason = reason or "This data is read-only here."
        self.capabilities = inner.capabilities.replace(write=False, delete=False)

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        return await self.inner.list(q, ctx)

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        return await self.inner.get(pk, ctx)

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        return await self.inner.aggregate(spec, ctx)

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        raise UnsupportedOperation(self.reason)

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        raise UnsupportedOperation(self.reason)

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        raise UnsupportedOperation(self.reason)

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        raise UnsupportedOperation(self.reason)

    async def warm(self) -> None:
        warm = getattr(self.inner, "warm", None)
        if warm is not None:
            await warm()

    async def health(self) -> tuple[bool, str]:
        return await self.inner.health()

    async def close(self) -> None:
        closer = getattr(self.inner, "close", None)
        if closer is not None:
            await closer()

    def __repr__(self) -> str:
        return f"<ReadOnly {self.inner!r}>"
