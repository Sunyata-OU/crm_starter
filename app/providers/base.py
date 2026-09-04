"""The provider contract.

A provider is the only thing that knows how a resource's data is actually
stored or reached. Everything above this line -- fields, views, routes,
templates -- is written against this one interface, which is why a table in
Postgres, a read-only REST endpoint and a message queue can all back the same
screens.

Providers advertise what they can do server-side via :class:`Capabilities`.
They are never required to implement the full query language; whatever they
decline, ``providers.shim.CapabilityShim`` emulates in Python.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from app.core.errors import UnsupportedOperation
from app.core.query import ALL_OPS, AggSpec, ListQuery, Op, Page
from app.core.results import Ctx, Record, WriteResult

WriteMode = Literal["sync", "async"]

# These aliases exist because providers define a method called ``list``, which
# shadows the builtin inside the class body. Resolving the annotation here, at
# module level, keeps the natural method name without confusing type checkers.
type Rows = list[dict[str, Any]]
type Records = list[Record]


class Capabilities:
    """What a provider can do without help.

    Declaring a capability ``False`` is not a failure -- it is a request for the
    shim to handle that concern. Declaring one ``True`` is a promise the
    provider honours it exactly, because the shim will then step aside.
    """

    __slots__ = (
        "read", "write", "create", "update", "delete",
        "server_filter", "server_sort", "server_search", "server_paginate",
        "total_count", "aggregate", "write_mode", "filter_ops", "searchable_fields",
    )

    def __init__(
        self,
        *,
        read: bool = True,
        write: bool = False,
        create: bool | None = None,
        update: bool | None = None,
        delete: bool = False,
        server_filter: bool = False,
        server_sort: bool = False,
        server_search: bool = False,
        server_paginate: bool = False,
        total_count: bool = False,
        aggregate: bool = False,
        write_mode: WriteMode = "sync",
        filter_ops: frozenset[Op] | None = None,
        searchable_fields: tuple[str, ...] = (),
    ) -> None:
        self.read = read
        self.write = write
        # create/update default to whatever `write` says, but can be split so a
        # backend that only appends (an event log, an inbox) can say so.
        self.create = write if create is None else create
        self.update = write if update is None else update
        self.delete = delete
        self.server_filter = server_filter
        self.server_sort = server_sort
        self.server_search = server_search
        self.server_paginate = server_paginate
        self.total_count = total_count
        self.aggregate = aggregate
        self.write_mode = write_mode
        self.filter_ops = filter_ops if filter_ops is not None else (ALL_OPS if server_filter else frozenset())
        self.searchable_fields = searchable_fields

    @property
    def writable(self) -> bool:
        return self.create or self.update or self.delete

    @property
    def is_async_write(self) -> bool:
        """True when writes are accepted but not confirmed (queues, 202 APIs)."""
        return self.write_mode == "async"

    def supports_ops(self, ops: frozenset[Op]) -> bool:
        """True if every operator in ``ops`` can be pushed down to the backend."""
        return self.server_filter and ops.issubset(self.filter_ops)

    def replace(self, **changes: Any) -> Capabilities:
        """A copy with some capabilities overridden.

        Overriding ``write`` alone re-derives ``create`` and ``update`` from it,
        matching what the constructor does, unless they are given explicitly.
        """
        current = {name: getattr(self, name) for name in self.__slots__}
        if "write" in changes:
            current.pop("create", None)
            current.pop("update", None)
        current.update(changes)
        return Capabilities(**current)

    def __repr__(self) -> str:
        on = [n for n in self.__slots__ if n not in ("write_mode", "filter_ops", "searchable_fields") and getattr(self, n)]
        return f"Capabilities({', '.join(on)}, write_mode={self.write_mode!r})"


#: A backend that can do everything itself -- the SQL case.
FULL = Capabilities(
    read=True, write=True, delete=True,
    server_filter=True, server_sort=True, server_search=True,
    server_paginate=True, total_count=True, aggregate=True,
)

#: A backend that can only hand over rows -- a plain JSON endpoint. The shim
#: supplies filtering, sorting, searching and pagination on top.
READ_ONLY_DUMB = Capabilities(read=True)


@runtime_checkable
class Provider(Protocol):
    """The interface every data source implements.

    Only ``list`` and ``get`` are required for a read-only source; the write
    methods may be left to the defaults in :class:`BaseProvider`, which refuse
    politely.
    """

    name: str
    capabilities: Capabilities

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]: ...
    async def get(self, pk: Any, ctx: Ctx) -> Record | None: ...
    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult: ...
    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult: ...
    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult: ...
    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None: ...
    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows: ...
    async def warm(self) -> None: ...
    async def health(self) -> tuple[bool, str]: ...


class BaseProvider:
    """Convenience base: sensible refusals for everything not implemented.

    Subclassing is optional -- any object satisfying :class:`Provider` works --
    but it saves write-only and read-only providers from writing stubs.
    """

    name: str = "provider"
    capabilities: Capabilities = READ_ONLY_DUMB
    #: Column holding the primary key. Records are stamped with this.
    pk_field: str = "id"

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        raise UnsupportedOperation(f"{self.name} cannot list records")

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        raise UnsupportedOperation(f"{self.name} cannot fetch a single record")

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        raise UnsupportedOperation(f"{self.name} is not writable")

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        raise UnsupportedOperation(f"{self.name} is not writable")

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        """Update only while the stored record still matches ``expect``.

        Returns ``None`` when it does not -- somebody else changed the record
        first. That is the whole point: it turns read-then-write, which is a
        race whenever more than one process is running, into a single atomic
        step the database arbitrates.

        Two uses. A background sweep claims a row before acting on it, so two
        schedulers cannot both deliver the same reminder. And an edit form can
        pass the values it was rendered from, turning a silent overwrite of a
        colleague's changes into a message saying the record moved on.

        Providers that cannot express a conditional write refuse, rather than
        emulating it with a read and a write and calling the race fixed.
        """
        raise UnsupportedOperation(f"{self.name} cannot write conditionally")

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        raise UnsupportedOperation(f"{self.name} does not support deletion")

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        raise UnsupportedOperation(f"{self.name} cannot aggregate")

    async def warm(self) -> None:
        """Do at startup whatever the first request would otherwise pay for.

        Reflecting a table, resolving a schema, opening a channel: work that is
        the same for every request and that should not land on whoever happens
        to arrive first. Doing it here also moves the failure earlier -- a
        missing table becomes a startup error rather than a 502 an hour later.

        Optional, and allowed to fail: a provider whose backend is temporarily
        unreachable should still let the application start.
        """
        return None

    async def health(self) -> tuple[bool, str]:
        """``(healthy, detail)``. Overridden by providers with a live connection."""
        return True, "no health check implemented"

    async def close(self) -> None:
        """Release provider-owned resources. Connections are closed by the registry."""
        return None

    # -- helpers for subclasses --------------------------------------------

    def _record(self, data: dict[str, Any]) -> Record:
        return Record(data, self.pk_field)

    def _records(self, rows: Rows) -> Records:
        return [Record(row, self.pk_field) for row in rows]

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"
