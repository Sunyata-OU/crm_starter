"""Giving a new record a name a person can say out loud.

A primary key is not that: nobody reads "ticket 1042" over the phone with
confidence they will be understood, and a support inbox needs exactly that --
a reference it can put in a subject line and trust a customer to quote back
unmangled. This is the same problem :mod:`app.providers.stamp` solves for
"who created this", solved the same way: the value depends on something only
the backend can hand back (there, the caller's identity; here, the primary
key it just assigned), so it is filled in here, below every path that can
create a record, rather than at each of them.

Necessarily two writes. The reference is a function of the primary key, and
the primary key does not exist until the insert has happened -- so the insert
runs first, bare, and the reference is patched on immediately after. A record
observed between those two writes would show no reference, which is the
correct account of its state at that instant: it is not yet finished being
created.
"""

from __future__ import annotations

from typing import Any

from app.core.query import AggSpec, ListQuery, Page
from app.core.results import Ctx, Record, WriteResult
from app.providers.base import Provider, Rows


class SequencingProvider:
    """Fills a declared column with ``f"{prefix}{pk}"`` once a record is created."""

    def __init__(self, inner: Provider, *, column: str, prefix: str) -> None:
        self.inner = inner
        self.column = column
        self.prefix = prefix
        self.name = f"sequenced({getattr(inner, 'name', '?')})"
        self.capabilities = inner.capabilities

    # -- everything but create passes straight through ----------------------

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        return await self.inner.list(q, ctx)

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        return await self.inner.get(pk, ctx)

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        return await self.inner.aggregate(spec, ctx)

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        return await self.inner.update(pk, data, ctx)

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        return await self.inner.delete(pk, ctx)

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        return await self.inner.update_if(pk, data, expect, ctx)

    async def warm(self) -> None:
        await self.inner.warm()

    async def health(self) -> tuple[bool, str]:
        return await self.inner.health()

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        # Never overrides what the caller sent -- an import carrying its own
        # reference is stating a fact this cannot improve on, the same rule
        # StampingProvider follows for who-created-this.
        if data.get(self.column):
            return await self.inner.create(data, ctx)

        result = await self.inner.create(data, ctx)
        if not result.ok or result.record is None or result.record.pk is None:
            return result

        reference = f"{self.prefix}{result.record.pk}"
        patched = await self.inner.update(result.record.pk, {self.column: reference}, ctx)
        if not patched.ok:
            # The record exists; only its reference failed to attach. Report
            # the create as it actually happened rather than failing a write
            # that, from the caller's point of view, already succeeded.
            return result
        record = patched.record if patched.record is not None else result.record.merge(
            {self.column: reference}
        )
        return WriteResult.success(record, message=result.message)
