"""Database-driven access control.

The Python policies in ``policy.py`` express rules that are structural -- a
resource that is read-only by nature, an ownership column that always applies.
This module covers the other kind: rules an administrator changes at runtime,
without a deployment.

A grant is one row: *this role, on this resource, may do these operations, over
these rows, with these fields hidden*. Permissions are cached because they are
consulted several times per request; the cache is invalidated whenever a grant
changes, which the resource machinery does automatically because permissions
are themselves an ordinary resource.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any, Literal

from app.core.query import Condition, Filter, ListQuery, Op, and_
from app.core.results import Ctx, Identity, Record
from app.resources.policy import DENY_ALL, Operation, Policy

if TYPE_CHECKING:
    from app.resources.resource import Resource

#: Matches every resource, for an administrator-style grant.
ANY_RESOURCE = "*"

#: How much of a resource a grant exposes.
RowScope = Literal["all", "own", "none"]

OPERATIONS: tuple[Operation, ...] = ("read", "create", "update", "delete")


@dataclass(frozen=True, slots=True)
class Grant:
    """What one role may do with one resource."""

    role: str
    resource: str = ANY_RESOURCE
    read: bool = True
    create: bool = False
    update: bool = False
    delete: bool = False
    row_scope: RowScope = "all"
    hidden_fields: frozenset[str] = frozenset()
    readonly_fields: frozenset[str] = frozenset()

    def allows(self, op: Operation) -> bool:
        return bool(getattr(self, op, False))

    @classmethod
    def from_record(cls, record: Record) -> Grant:
        return cls(
            role=str(record.get("role", "")),
            resource=str(record.get("resource", ANY_RESOURCE)),
            read=_flag(record.get("can_read")),
            create=_flag(record.get("can_create")),
            update=_flag(record.get("can_update")),
            delete=_flag(record.get("can_delete")),
            row_scope=_scope(record.get("row_scope")),
            hidden_fields=frozenset(_string_list(record.get("hidden_fields"))),
            readonly_fields=frozenset(_string_list(record.get("readonly_fields"))),
        )


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _scope(value: Any) -> RowScope:
    text = str(value or "all").strip().lower()
    return text if text in ("all", "own", "none") else "all"  # type: ignore[return-value]


def _string_list(value: Any) -> tuple[str, ...]:
    """Read a JSON array, a comma-separated string, or a real list."""
    if not value:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v) for v in value)
    text = str(value).strip()
    if text.startswith("["):
        try:
            return tuple(str(v) for v in json.loads(text))
        except (ValueError, TypeError):
            pass
    return tuple(part.strip() for part in text.split(",") if part.strip())


@dataclass
class GrantTable:
    """Every grant, indexed for lookup.

    Built once and reused until a permission changes. Holding the whole table
    is deliberate: it is small (roles × resources), and the alternative is a
    query inside every permission check.
    """

    by_role: dict[str, list[Grant]] = dc_field(default_factory=dict)
    loaded_at: float = 0.0

    @classmethod
    def from_records(cls, records: Iterable[Record]) -> GrantTable:
        table = cls(loaded_at=time.monotonic())
        for record in records:
            grant = Grant.from_record(record)
            if grant.role:
                table.by_role.setdefault(grant.role, []).append(grant)
        return table

    def for_identity(self, identity: Identity, resource_name: str) -> list[Grant]:
        """Grants that apply to this caller on this resource.

        A resource-specific grant and a wildcard grant may both apply; both are
        returned, and the caller takes the most permissive, so adding a
        wildcard role can only ever widen access, never narrow it unexpectedly.
        """
        found: list[Grant] = []
        for role in identity.roles:
            for grant in self.by_role.get(role, ()):
                if grant.resource in (resource_name, ANY_RESOURCE):
                    found.append(grant)
        return found

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_role))

    def __len__(self) -> int:
        return sum(len(g) for g in self.by_role.values())


class PermissionStore:
    """Loads and caches grants from the permissions resource.

    The cache has a short time-to-live *and* an explicit invalidation hook. The
    hook handles changes made through this application; the expiry handles
    changes made anywhere else -- a second worker, a direct SQL edit -- so a
    stale grant cannot outlive it indefinitely.

    With no shared cache configured, the consequence should be stated plainly:
    across several workers or hosts a permission change takes effect everywhere
    within ``ttl`` seconds, not immediately, because only the worker that
    handled the edit invalidates its own copy.

    Configure a shared cache and that window closes. The grant table is then
    kept under one key every worker reads, and an edit deletes that key rather
    than one worker's memory -- so the next request on any host reloads. The
    in-process copy is still consulted first, with a much shorter life, because
    a network round trip on every permission check would be its own problem.
    """

    #: How long the in-process copy is trusted when a shared cache is backing
    #: it. Short, because the shared copy is the authority and this is only
    #: here to keep the common case off the network.
    SHARED_TTL = 2.0
    #: The one key the grant table lives under.
    CACHE_KEY = "rbac:grants"

    def __init__(self, provider: Any = None, *, ttl: float = 30.0) -> None:
        self.provider = provider
        self.ttl = ttl
        self._table: GrantTable | None = None
        self._pending: set[Any] = set()
        # Without this, every request that arrives on an expired cache issues
        # its own reload. Under load that is a stampede against the one table
        # every single request consults.
        self._loading: asyncio.Lock | None = None

    def bind(self, provider: Any) -> None:
        self.provider = provider
        self.invalidate()

    def invalidate(self) -> None:
        """Drop the cached grants. Called whenever a permission row changes.

        Synchronous, because it is called from a write path that is not
        necessarily async. The shared copy is dropped as a task when there is a
        loop to schedule it on -- and if there is not, the short TTL on the
        shared entry closes the gap.
        """
        self._table = None
        self._invalidate_shared()

    def _invalidate_shared(self) -> None:
        import asyncio

        from app.cache import NullCache, cache

        if isinstance(cache, NullCache):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(cache.delete(self.CACHE_KEY))
        # Held until it finishes; an unreferenced task may be collected mid-run.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    @property
    def configured(self) -> bool:
        return self.provider is not None

    def _fresh(self) -> GrantTable | None:
        table = self._table
        if table is not None and (time.monotonic() - table.loaded_at) < self._effective_ttl():
            return table
        return None

    async def table(self, ctx: Ctx | None = None) -> GrantTable:
        fresh = self._fresh()
        if fresh is not None:
            return fresh

        # Created lazily: the lock must belong to the running loop, and this
        # store is a module-level singleton built at import time.
        if self._loading is None:
            self._loading = asyncio.Lock()

        async with self._loading:
            # Someone may have reloaded it while this request waited.
            fresh = self._fresh()
            if fresh is not None:
                return fresh
            if self.provider is None:
                self._table = GrantTable(loaded_at=time.monotonic())
                return self._table

            shared = await self._from_shared_cache()
            if shared is not None:
                self._table = shared
                return shared

            page = await self.provider.list(
                ListQuery(page_size=500, with_total=False), ctx or Ctx.system()
            )
            rows = [dict(r) for r in page.items]
            await self._to_shared_cache(rows)
            self._table = GrantTable.from_records(page.items)
            return self._table

    def _effective_ttl(self) -> float:
        from app.cache import NullCache, cache

        return self.ttl if isinstance(cache, NullCache) else min(self.ttl, self.SHARED_TTL)

    async def _from_shared_cache(self) -> GrantTable | None:
        """The grant table another worker already loaded, if one has."""
        from app.cache import cache

        try:
            rows = await cache.get(self.CACHE_KEY)
        except Exception:
            return None
        if not rows:
            return None
        return GrantTable.from_records([Record(r) for r in rows])

    async def _to_shared_cache(self, rows: list[dict[str, Any]]) -> None:
        from app.cache import cache

        try:
            await cache.set(self.CACHE_KEY, _jsonable_rows(rows), ttl=self.ttl)
        except Exception:
            # A cache that will not take the write costs a reload, nothing more.
            return

    async def grants_for(
        self, identity: Identity, resource_name: str, ctx: Ctx | None = None
    ) -> list[Grant]:
        table = await self.table(ctx)
        return table.for_identity(identity, resource_name)


#: One store per process. The registry binds it once permissions are readable.
store = PermissionStore()

#: The grants loaded for the request currently being served, keyed by resource.
#:
#: A context variable rather than an attribute on the policy: policies are
#: shared singletons held by resources, so per-request state stored on one
#: would grow without bound and could be read by a concurrent request. A
#: context variable is naturally scoped to the task handling one request.
#: The default is ``None`` rather than a dict: a mutable default on a context
#: variable is shared by every context that never sets it.
_request_grants: contextvars.ContextVar[dict[str, list[Grant] | None] | None] = (
    contextvars.ContextVar("crm_request_grants", default=None)
)


def set_request_grants(resource_name: str, grants: list[Grant] | None) -> None:
    """Record the grants that apply for the rest of this request."""
    current = dict(_request_grants.get() or {})
    current[resource_name] = grants
    _request_grants.set(current)


def get_request_grants(resource_name: str) -> list[Grant] | None:
    """Grants loaded for this request, or ``None`` if none were loaded."""
    return (_request_grants.get() or {}).get(resource_name)


def clear_request_grants() -> None:
    _request_grants.set(None)


class DbPolicy(Policy):
    """A policy backed by the permissions table.

    Falls back to ``base`` when the table says nothing about a resource, so a
    deployment that never configures permissions behaves exactly as it did
    before this module existed. That matters: an empty permissions table must
    not silently lock everyone out.

    ``base`` composes with the grants in one of two ways, chosen by
    ``narrow_only``:

    * **Default -- grants add access.** A grant may do anything ``base``
      leaves unsaid, which is the ordinary case: ``base`` is a sensible
      default (often "admin only"), and the whole point of the permissions
      table is to let an administrator widen that default without a
      deployment. This is what lets, say, ``notes`` ship hardcoded to
      administrators and still become writable by a `manager` role once
      somebody grants it.
    * **``narrow_only=True`` -- grants may only take away.** For a resource
      whose refusal is structural rather than a mere default -- a screen
      whose data is owned by a service this table knows nothing about, or the
      permissions table's own kind of resource -- ``base`` is authoritative
      for every write, superuser included, and a grant can only narrow what
      it already allows, never widen it. A grant that turns on ``can_update``
      for a resource like that must not enable it: the invariants that write
      would bypass live outside this table entirely, and no row in it should
      be able to promise a capability the structural policy never had to
      give. Reads still come from the grants alone once any are configured --
      *who may see what* is exactly the operational question this table
      answers -- ``narrow_only`` affects writes and the row/field
      restrictions attached to them.
    """

    def __init__(
        self,
        *,
        base: Policy | None = None,
        owner_field: str = "owner",
        identity_attr: str = "email",
        #: Roles that bypass the table entirely.
        superuser_roles: Sequence[str] = ("admin",),
        #: See the class docstring. False is the historical default: grants
        #: widen a sensible ``base``. Screens over another service's tables, and anything else
        #: whose refusal must hold regardless of what an administrator later
        #: grants, pass True.
        narrow_only: bool = False,
    ) -> None:
        self.base = base or Policy()
        self.owner_field = owner_field
        self.identity_attr = identity_attr
        self.superuser_roles = frozenset(superuser_roles)
        self.narrow_only = narrow_only
        #: Set by the registry when the policy is attached to a resource.
        self.resource_name = ""

    # -- request preparation ------------------------------------------------

    async def prepare(self, identity: Identity, resource_name: str, ctx: Ctx) -> list[Grant] | None:
        """Load this caller's grants and remember them for the checks below.

        Policy methods are synchronous because they are called from templates
        and tight loops; the asynchronous load therefore happens once, here,
        before any of them run.

        An empty *table* -- no permissions rows anywhere, not even for
        another role -- is recorded as ``None``, the same as "grants were
        never loaded at all". That is what makes an unconfigured install
        behave exactly as it did before this module existed: `allows()` and
        `scope()` treat ``None`` as "defer to the structural policy", and a
        deployment that has never opened the permissions screen must get that
        answer for every resource, not a hard "no" the moment one resource
        happens to be `DbPolicy`-backed. A table that has *some* rows but none
        naming this role and this resource is a different fact -- somebody
        has configured access control, deliberately, and simply did not grant
        this -- so that case stays an empty list and stays a refusal.
        """
        table = await store.table(ctx)
        grants: list[Grant] | None = table.for_identity(identity, resource_name) if len(table) else None
        set_request_grants(resource_name, grants)
        return grants

    def _cached(self, identity: Identity, resource_name: str) -> list[Grant] | None:
        return get_request_grants(resource_name)

    # -- checks -------------------------------------------------------------

    def allows(self, op: Operation, identity: Identity, record: Record | None = None) -> bool:
        if not identity.is_authenticated:
            return False

        is_super = identity.has_role(*self.superuser_roles)
        # In the default mode a superuser bypasses the table entirely, same
        # as always. In narrow-only mode the bypass is read-only: writes are
        # handled below, together with every other role, because even an
        # administrator must not be able to write through a resource whose
        # refusal is structural -- the whole point of a resource like
        # that is that the invariants live in a service this table knows
        # nothing about, and no grant, "*" included, should be able to route
        # around that by promising a capability the structural policy never
        # had to give.
        if is_super and (op == "read" or not self.narrow_only):
            return True

        if not is_super:
            grants = self._cached(identity, self.resource_name)
            if grants is None or not store.configured:
                # Nothing loaded, or no permissions table at all: defer to the
                # structural policy rather than refusing everything.
                return self.base.allows(op, identity, record)
            if not grants:
                return False

            if not any(g.allows(op) for g in grants):
                return False

            # An "own rows only" grant still has to check the row in hand,
            # since scope() narrows lists but a direct write names its own
            # record.
            if record is not None and op in ("update", "delete"):
                widest = max((g.row_scope for g in grants if g.allows(op)), key=_scope_rank)
                if widest == "own" and not self._owns(record, identity):
                    return False

        if op != "read" and self.narrow_only:
            # Grants may only narrow what the structural policy allows here,
            # never widen it. A grant is consulted on its own for reads --
            # that is the whole feature, in both modes -- but a write is let
            # through only if the structural policy would also let it
            # through, superuser or not.
            return self.base.allows(op, identity, record)

        return True

    def scope(self, identity: Identity) -> Filter | None:
        if not identity.is_authenticated:
            return DENY_ALL

        # In narrow-only mode the structural scope -- typically "rows that
        # are not soft-deleted" -- is not a restriction a grant can widen
        # away, so it is computed up front and applies to every outcome
        # below, superuser included. In the default mode it plays no part
        # once any grant is configured, same as always: a grant's row_scope
        # is the whole answer there.
        structural = self.base.scope(identity) if self.narrow_only else None
        if identity.has_role(*self.superuser_roles):
            return structural

        grants = self._cached(identity, self.resource_name)
        if grants is None or not store.configured:
            return self.base.scope(identity)
        if not grants:
            return DENY_ALL

        readable = [g for g in grants if g.read]
        if not readable:
            return DENY_ALL

        # Take the widest scope any applicable grant gives. Roles add access;
        # holding a broader role must not be undone by also holding a narrow one.
        widest = max((g.row_scope for g in readable), key=_scope_rank)
        if widest == "none":
            return DENY_ALL
        if widest == "all":
            return structural
        owned = Condition(self.owner_field, Op.EQ, getattr(identity, self.identity_attr))
        return and_(structural, owned) if self.narrow_only else owned

    def _owns(self, record: Record, identity: Identity) -> bool:
        return record.get(self.owner_field) == getattr(identity, self.identity_attr)

    # -- field visibility ---------------------------------------------------

    def readable_fields(self, identity: Identity, resource: Resource) -> tuple[str, ...]:
        visible = [f for f in resource.fields if f.visible_to(identity)]
        if self.narrow_only:
            # A field the structural policy never shows (a per-field
            # `read_roles` the base class enforces) cannot be un-hidden by a
            # grant -- same reasoning as `allows()`.
            allowed = frozenset(self.base.readable_fields(identity, resource))
            visible = [f for f in visible if f.name in allowed]
        hidden = self._hidden(identity)
        return tuple(f.name for f in visible if f.name not in hidden)

    def writable_fields(self, identity: Identity, resource: Resource) -> tuple[str, ...]:
        writable = [f for f in resource.fields if f.writable_by(identity)]
        if self.narrow_only:
            # Keeps a restricted base honest: its `writable_fields` is a fixed
            # allow-list of the columns the write actually posts to the
            # service's API, and a grant must not be able to add a column to
            # that list -- it is not this table's list to extend.
            allowed = frozenset(self.base.writable_fields(identity, resource))
            writable = [f for f in writable if f.name in allowed]
        blocked = self._hidden(identity) | self._readonly(identity)
        return tuple(f.name for f in writable if f.name not in blocked)

    def _hidden(self, identity: Identity) -> frozenset[str]:
        if identity.has_role(*self.superuser_roles):
            return frozenset()
        grants = self._cached(identity, self.resource_name) or []
        if not grants:
            return frozenset()
        # A field is hidden only if *every* applicable grant hides it, so one
        # role granting sight of a column is enough to see it.
        sets = [g.hidden_fields for g in grants]
        return frozenset.intersection(*sets) if sets else frozenset()

    def _readonly(self, identity: Identity) -> frozenset[str]:
        if identity.has_role(*self.superuser_roles):
            return frozenset()
        grants = self._cached(identity, self.resource_name) or []
        if not grants:
            return frozenset()
        sets = [g.readonly_fields for g in grants]
        return frozenset.intersection(*sets) if sets else frozenset()

    def __repr__(self) -> str:
        return f"<DbPolicy {self.resource_name!r} base={self.base!r}>"


_SCOPE_ORDER = {"none": 0, "own": 1, "all": 2}


def _jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grant rows, reduced to what JSON can carry.

    Only the columns a grant is built from. Dropping the timestamps keeps the
    shared entry small and avoids serialising a datetime for no reason.
    """
    wanted = (
        "role", "resource", "can_read", "can_create", "can_update", "can_delete",
        "row_scope", "hidden_fields", "readonly_fields",
    )
    return [{k: row.get(k) for k in wanted if k in row} for row in rows]


def _scope_rank(scope: str) -> int:
    return _SCOPE_ORDER.get(scope, 0)


def grant_row(role: str, resource: str, *ops: str, rows: RowScope = "all") -> dict[str, Any]:
    """One grant row with every column filled in.

    Spelled out rather than relying on column defaults, because a bulk insert
    needs the same keys on every row -- SQLAlchemy compiles one statement for
    the batch and cannot fill gaps per row.
    """
    return {
        "role": role,
        "resource": resource,
        "can_read": "read" in ops,
        "can_create": "create" in ops,
        "can_update": "update" in ops,
        "can_delete": "delete" in ops,
        "row_scope": rows,
        "hidden_fields": None,
        "readonly_fields": None,
    }


#: The grants created for a fresh installation.
DEFAULT_GRANTS: tuple[dict[str, Any], ...] = (
    grant_row("admin", ANY_RESOURCE, "read", "create", "update", "delete"),
    grant_row("manager", ANY_RESOURCE, "read", "create", "update"),
    grant_row("user", "companies", "read", "create", "update"),
    grant_row("user", "contacts", "read", "create", "update"),
    # A rep works their own pipeline and their own diary.
    grant_row("user", "deals", "read", "create", "update", rows="own"),
    grant_row("user", "activities", "read", "create", "update", "delete", rows="own"),
    grant_row("readonly", ANY_RESOURCE, "read"),
)

def role_row(name: str, label: str, description: str) -> dict[str, Any]:
    return {"name": name, "label": label, "description": description, "is_builtin": True}


DEFAULT_ROLES: tuple[dict[str, Any], ...] = (
    role_row("admin", "Administrator", "Full access, including permissions and the system page."),
    role_row("manager", "Manager", "Sees and edits everything, but cannot delete."),
    role_row("user", "User", "Works with their own records."),
    role_row("readonly", "Read only", "May look, not touch."),
)
