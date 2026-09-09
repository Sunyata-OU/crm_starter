"""Recording who created a record, on whichever path created it.

The same problem the audit wrapper solves, and solved the same way. A record
can be created from a form, an inline action, a bulk import, the API or a
script, and a "set created_by here" remembered at each of those places is one
that will eventually be forgotten at one of them. Every write already passes
through the provider, so the stamp goes there.

Only ``create``. An update deliberately leaves the columns alone: they say who
raised this, not who touched it last -- that question is the audit log's.
"""

from __future__ import annotations

from typing import Any

from app.core.query import AggSpec, ListQuery, Page
from app.core.results import Ctx, Identity, Record, WriteResult
from app.providers.base import Provider, Rows

#: What a stamp may record about the person, by name.
SOURCES = ("id", "label", "email", "subject")


def value_of(identity: Identity, source: str) -> str:
    """One attribute of an identity, as a stamp records it."""
    match source:
        case "id":
            # What notifications are addressed to, and what a row is matched
            # against later. Email where there is one, because a subject from
            # an SSO provider is a UUID nobody recognises.
            return identity.email or identity.subject
        case "label":
            return identity.label
        case "email":
            return identity.email
        case "subject":
            return identity.subject
    raise ValueError(f"unknown stamp source {source!r}; expected one of {SOURCES}")


class StampingProvider:
    """Fills declared columns from the caller's identity as a record is created."""

    def __init__(self, inner: Provider, stamp: dict[str, str]) -> None:
        self.inner = inner
        self.stamp = dict(stamp)
        self.name = f"stamped({getattr(inner, 'name', '?')})"
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
        identity = ctx.identity
        values = dict(data)
        for column, source in self.stamp.items():
            # Never overrides what the caller sent. An import that carries its
            # own "raised by" column is stating a fact this cannot improve on.
            if values.get(column):
                continue
            stamped = value_of(identity, source)
            if stamped:
                values[column] = stamped
        return await self.inner.create(values, ctx)
