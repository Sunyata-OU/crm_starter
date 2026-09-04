"""Recording who changed what.

Implemented as a provider wrapper rather than as calls in the route handlers.
There are half a dozen places a write can originate -- a form, an inline cell,
a bulk action, a custom action, the API, a script -- and a rule that has to be
remembered at each of them is a rule that will eventually be forgotten. Every
write already passes through the provider, so wrapping it makes the audit trail
structural.

What is recorded is the *diff*, not the submitted payload: a form resubmits
every field, and a log saying "changed 14 fields" when one actually changed is
a log nobody reads.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from app.core.query import AggSpec, ListQuery, Page
from app.core.results import Ctx, Record, WriteResult, WriteStatus
from app.providers.base import Provider, Rows

# ``list`` is shadowed by the method of that name inside the class body.
type Change = list[Any]

log = logging.getLogger("crm.audit")

#: Never written to the log, whatever a resource happens to call them.
SENSITIVE = frozenset({
    "password", "password_hash", "token", "token_hash", "secret",
    "api_key", "private_key", "access_token", "refresh_token",
})

REDACTED = "***"


def _plain(value: Any) -> Any:
    """Reduce a value to something JSON can hold."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def diff(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Change]:
    """The fields that actually changed, as ``{field: [before, after]}``.

    Comparison is on the plain form of each value, so a ``date`` read back from
    a driver as a string does not register as a change when nothing changed.
    """
    changes: dict[str, Change] = {}
    for key, new_value in after.items():
        if key in SENSITIVE:
            changes[key] = [REDACTED, REDACTED]
            continue
        old_value = (before or {}).get(key)
        old_plain, new_plain = _plain(old_value), _plain(new_value)
        if old_plain != new_plain:
            changes[key] = [old_plain, new_plain]
    return changes


class AuditSink:
    """Where audit entries go.

    A separate object so the destination is configurable: the shipped one
    writes through an ordinary provider, but a deployment could send entries to
    a log aggregator instead without touching the wrapper.
    """

    def __init__(self, provider: Provider | None = None) -> None:
        self.provider = provider

    @property
    def configured(self) -> bool:
        return self.provider is not None

    def bind(self, provider: Provider) -> None:
        self.provider = provider

    async def write(self, entry: dict[str, Any]) -> None:
        """Store one entry.

        May raise; the caller is responsible for containing the failure, so a
        custom sink does not have to remember to swallow its own errors.
        """
        if self.provider is None:
            log.info("audit (no sink): %s", entry)
            return
        await self.provider.create(entry, Ctx.system())


#: One sink per process, bound by the registry once the log is writable.
sink = AuditSink()


class AuditingProvider:
    """Wraps a provider so every write is recorded.

    Reads pass straight through untouched -- auditing them would be a different
    feature with a very different cost.
    """

    def __init__(
        self,
        inner: Provider,
        resource_name: str,
        *,
        label_field: str = "",
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.inner = inner
        self.resource_name = resource_name
        self.label_field = label_field
        self.sink = audit_sink or sink
        self.name = f"audited({getattr(inner, 'name', '?')})"
        self.capabilities = inner.capabilities

    # -- reads pass through -------------------------------------------------

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        return await self.inner.list(q, ctx)

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        return await self.inner.get(pk, ctx)

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        return await self.inner.aggregate(spec, ctx)

    # -- writes are recorded ------------------------------------------------

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        result = await self.inner.create(data, ctx)
        stored = dict(result.record) if result.record is not None else dict(data)
        await self._record(
            "create", ctx, result,
            pk=result.record.pk if result.record is not None else None,
            changes=diff(None, stored),
            label=self._label(stored),
        )
        return result

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        """Conditional writes are recorded like any other update, when they win.

        A losing claim changed nothing, so there is nothing to record; an audit
        entry for it would say a record was updated when it was not.
        """
        before = await self._safe_get(pk, ctx)
        result = await self.inner.update_if(pk, data, expect, ctx)
        if result is None:
            return None
        after = dict(result.record) if result.record is not None else {**(before or {}), **data}
        changed = diff(before, {k: after.get(k) for k in data} if before else after)
        await self._record(
            "update", ctx, result, pk=pk, changes=changed,
            label=self._label(after or before or {}),
        )
        return result

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        # Read first, so the entry can say what the value *was*. One extra read
        # per write, which is the price of a log that is worth keeping.
        before = await self._safe_get(pk, ctx)
        result = await self.inner.update(pk, data, ctx)
        after = dict(result.record) if result.record is not None else {**(before or {}), **data}
        changed = diff(before, {k: after.get(k) for k in data} if before else after)
        await self._record(
            "update", ctx, result, pk=pk, changes=changed,
            label=self._label(after or before or {}),
        )
        return result

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        before = await self._safe_get(pk, ctx)
        result = await self.inner.delete(pk, ctx)
        await self._record(
            "delete", ctx, result, pk=pk,
            # The whole record, since it is about to stop existing.
            changes={k: [_plain(v), None] for k, v in (before or {}).items()
                     if k not in SENSITIVE},
            label=self._label(before or {}),
        )
        return result

    async def _safe_get(self, pk: Any, ctx: Ctx) -> dict[str, Any] | None:
        """The record before a change, or ``None`` if it cannot be read.

        A write-only backend has nothing to read; that is not an error, it just
        means the entry records the request rather than the difference.
        """
        try:
            record = await self.inner.get(pk, ctx)
        except Exception:
            return None
        return dict(record) if record is not None else None

    def _label(self, data: Mapping[str, Any]) -> str:
        if self.label_field and data.get(self.label_field) not in (None, ""):
            return str(data[self.label_field])[:200]
        return ""

    async def _record(
        self,
        action: str,
        ctx: Ctx,
        result: WriteResult,
        *,
        pk: Any,
        changes: dict[str, Change],
        label: str,
    ) -> None:
        identity = ctx.identity
        entry = {
            "actor": identity.email or identity.subject,
            "actor_name": identity.display_name or identity.label,
            "actor_provider": identity.provider,
            "action": action,
            "resource": self.resource_name,
            "record_id": None if pk is None else str(pk),
            "record_label": label,
            "changes": json.dumps(changes) if changes else None,
            # A queued write is recorded as queued, not as done: the log must
            # not claim an outcome the backend never confirmed.
            "status": result.status.value if isinstance(result.status, WriteStatus)
            else str(result.status),
            "detail": result.message or None,
            "request_id": ctx.request_id or None,
            "ip": str(ctx.extra.get("ip", "")) or None,
        }
        try:
            await self.sink.write(entry)
        except Exception:
            # Auditing must never fail the operation being audited: a broken
            # log would otherwise take the whole application down with it. The
            # failure is logged loudly instead, so the gap is visible.
            log.exception("could not write an audit entry: %s", entry)

    # -- passthrough --------------------------------------------------------

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
        return f"<AuditingProvider {self.resource_name!r} over {self.inner!r}>"
