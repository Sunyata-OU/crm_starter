"""The application registry.

Holds every resource, the menu built from them, and the wiring that turns a
resource's ``provider`` reference into a live, fully-capable provider.

Binding happens once at startup, after modules have registered but before the
first request. Doing it in one place is what lets a resource declare
``provider="db.main#contacts"`` as a string and still end up with a shimmed,
connection-backed object at runtime.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from app.core.connections import ConnectionRegistry
from app.core.errors import ConfigError, RegistryError
from app.core.results import Identity
from app.providers.base import Provider
from app.providers.shim import ensure_full
from app.providers.union import Union, UnionProvider
from app.resources.resource import Resource

#: Builds a provider from a connection handle and the options after the '#'.
ProviderFactory = Callable[[Any, str, Resource], Provider]

_FACTORIES: dict[str, ProviderFactory] = {}

log = logging.getLogger("crm.registry")


def register_provider_factory(connection_type: str) -> Callable[[ProviderFactory], ProviderFactory]:
    """Say how to build a provider for a given connection type.

        @register_provider_factory("sqlalchemy")
        def build(handle, target, resource): ...

    ``target`` is whatever followed the ``#`` in the resource's provider
    reference -- a table name, an endpoint path, a routing key.
    """

    def decorator(fn: ProviderFactory) -> ProviderFactory:
        _FACTORIES[connection_type] = fn
        return fn

    return decorator


@dataclass(slots=True)
class MenuItem:
    label: str
    url: str
    icon: str = ""
    order: int = 100
    group: str = ""
    resource: str = ""


@dataclass(slots=True)
class MenuGroup:
    label: str
    items: list[MenuItem] = dc_field(default_factory=list)
    order: int = 100

    @property
    def sorted_items(self) -> list[MenuItem]:
        return sorted(self.items, key=lambda i: (i.order, i.label))


class Registry:
    """Everything the application knows about, in one object."""

    def __init__(self, connections: ConnectionRegistry | None = None) -> None:
        self.connections = connections or ConnectionRegistry()
        self._resources: dict[str, Resource] = {}
        self._group_order: dict[str, int] = {}
        self._bound = False
        #: The modules that filled this registry, in the order they loaded.
        #: Kept so anything downstream -- seeding, diagnostics, the system
        #: page -- can ask what the running application is actually made of.
        self.loaded_modules: list[Any] = []

    # -- resources ----------------------------------------------------------

    def add_resource(self, resource: Resource) -> Resource:
        """Register a resource. Returns it, so declarations can be one expression."""
        if resource.name in self._resources:
            raise RegistryError(
                f"resource {resource.name!r} is already registered; use "
                f"registry.resource({resource.name!r}) to extend it instead"
            )
        if self._bound:
            raise RegistryError(
                f"cannot add resource {resource.name!r} after providers are bound"
            )
        self._resources[resource.name] = resource
        resource.registry = self
        return resource

    def resource(self, name: str) -> Resource:
        try:
            return self._resources[name]
        except KeyError:
            known = ", ".join(sorted(self._resources)) or "(none registered)"
            raise RegistryError(
                f"unknown resource {name!r}; registered resources: {known}"
            ) from None

    def has_resource(self, name: str) -> bool:
        return name in self._resources

    @property
    def resources(self) -> tuple[Resource, ...]:
        return tuple(self._resources.values())

    @property
    def resource_names(self) -> tuple[str, ...]:
        return tuple(self._resources)

    def __iter__(self) -> Iterator[Resource]:
        return iter(self._resources.values())

    def __contains__(self, name: object) -> bool:
        return name in self._resources

    def __len__(self) -> int:
        return len(self._resources)

    # -- provider binding ---------------------------------------------------

    async def bind(self) -> None:
        """Give every resource a live provider, and open the file stores.

        Called once during startup. Idempotent, so a test that builds the app
        twice does not double-open connections.
        """
        if self._bound:
            return
        await self._open_cache()
        await self._open_file_stores()
        for resource in self._resources.values():
            resource.provider = await self._build_provider(resource)
        self._bound = True
        self._wire_access_control()
        await self._warm_providers()

    async def _warm_providers(self) -> None:
        """Let each provider do its one-off setup before any request arrives.

        Concurrently, because these are independent round trips to different
        systems, and tolerantly: a backend that is down should delay its own
        resource, not stop the application from serving the others. What it
        must not do is stay silent -- the warning here is usually the first
        sign of a misspelled table name.
        """
        resources = list(self._resources.values())
        results = await asyncio.gather(
            *(r.provider.warm() for r in resources), return_exceptions=True
        )
        for resource, result in zip(resources, results, strict=True):
            if isinstance(result, BaseException):
                log.warning(
                    "resource %r could not prepare its provider: %s", resource.name, result
                )

    async def _open_cache(self) -> None:
        """Install the configured cache, if there is one.

        Before providers bind, because the permission store consults it while
        answering the very first request. A cache that will not open is a
        warning and nothing more: the application is required to work without
        one, and starting without a cache is better than not starting.
        """
        from app import cache as cache_module

        for name in self.connections.names:
            spec = self.connections.spec(name)
            if spec.type != "cache" or not spec.enabled:
                continue
            try:
                handle = await self.connections.get(name)
            except ConfigError as exc:
                log.warning("cache %r could not be opened: %s", name, exc)
                continue
            cache_module.use(handle)
            log.info("cache %r is a %s cache", name, handle.name)
            return

    async def _open_file_stores(self) -> None:
        """Open every ``type: files`` connection and register it by name.

        Done before providers bind, because a resource may reference a store
        while rendering its very first page.
        """
        from app.storage import store as file_stores

        for name in self.connections.names:
            spec = self.connections.spec(name)
            if spec.type != "files" or not spec.enabled:
                continue
            try:
                handle = await self.connections.get(name)
            except ConfigError:
                # A misconfigured file store must not stop the application from
                # serving records; uploads will report the problem instead.
                continue
            file_stores.bind(name, handle)
            # The first store configured is the one a field gets by default.
            if len(file_stores.names) == 1:
                file_stores.default_name = name

    def _wire_access_control(self) -> None:
        """Point the permission store and audit sink at their resources.

        Both are process-wide singletons consulted from places that have no
        registry to hand -- a policy method called from a template, a provider
        wrapper deep in a write -- so they are bound once, here.
        """
        from app.providers.audit import sink
        from app.resources.rbac import DbPolicy, store

        if self.has_resource("permissions"):
            store.bind(self._resources["permissions"].provider)
        if self.has_resource("audit_log"):
            sink.bind(self._resources["audit_log"].provider)

        # Tell each database-backed policy which resource it guards; it cannot
        # know that from its own constructor.
        for name, resource in self._resources.items():
            if isinstance(resource.policy, DbPolicy):
                resource.policy.resource_name = name

    @property
    def bound(self) -> bool:
        return self._bound

    async def _build_provider(self, resource: Resource) -> Provider:
        """Resolve one resource's provider reference into a live provider."""
        ref = resource.provider_ref

        # A resource whose rows live in several backends. Resolved before the
        # string case because a union is a declaration, not a live provider.
        if isinstance(ref, Union):
            return ensure_full(
                await self._build_union(ref, resource),
                search_fields=resource.searchable_fields(),
            )

        # An already-constructed provider, for tests and in-memory resources.
        if not isinstance(ref, str):
            return ensure_full(ref, search_fields=resource.searchable_fields())

        connection_name, _, target = ref.partition("#")
        connection_name = connection_name.strip()
        target = target.strip() or resource.name

        spec = self.connections.spec(connection_name)
        factory = _FACTORIES.get(spec.type)
        if factory is None:
            known = ", ".join(sorted(_FACTORIES)) or "(none registered)"
            raise ConfigError(
                f"resource {resource.name!r} uses connection {connection_name!r} of type "
                f"{spec.type!r}, but no provider factory is registered for it; "
                f"available: {known}"
            )
        handle = await self.connections.get(connection_name)
        provider = factory(handle, target, resource)

        # A resource may read from one backend and write to another -- a read
        # replica with writes going to a queue, say. Declaring the write half
        # separately keeps that a one-line change rather than a custom provider.
        write_ref = getattr(resource, "write_provider", None)
        if write_ref:
            from app.providers.composite import CompositeProvider

            provider = CompositeProvider(
                provider, await self._build_named(write_ref, resource)
            )

        # Auditing wraps the backend but sits inside the shim: it must see the
        # real write, and it must not see reads the shim performs to emulate a
        # query stage.
        if resource.audited:
            from app.providers.audit import AuditingProvider

            provider = AuditingProvider(
                provider, resource.name, label_field=resource.display_field
            )

        # Every provider enters the system through the shim, so views can rely
        # on the full query contract regardless of the backend.
        return ensure_full(provider, search_fields=resource.searchable_fields())

    async def _build_union(self, spec: Union, resource: Resource) -> Provider:
        """Build one provider per source and merge them.

        Each source goes through the shim on its own, so a union of a SQL table
        and an HTTP endpoint has both halves answering the full query contract
        before the merge sees them -- which is what lets the union push a
        filter down to each rather than fetching everything and filtering here.
        """
        searchable = resource.searchable_fields()
        bound = [
            (
                source.label,
                ensure_full(
                    await self._build_named(source.ref, resource),
                    search_fields=searchable,
                ),
            )
            for source in spec.sources
        ]
        return UnionProvider(
            bound,
            name=f"union:{resource.name}",
            pk_field=resource.pk,
            source_field=spec.source_field,
            prefixed=spec.prefixed,
            max_rows=spec.max_rows,
            searchable_fields=searchable,
        )

    async def _build_named(self, ref: str, resource: Resource) -> Provider:
        """Build one provider from a ``connection#target`` reference."""
        connection_name, _, target = ref.partition("#")
        spec = self.connections.spec(connection_name.strip())
        factory = _FACTORIES.get(spec.type)
        if factory is None:
            raise ConfigError(
                f"resource {resource.name!r} names a provider of type {spec.type!r}, "
                f"for which no factory is registered"
            )
        handle = await self.connections.get(connection_name.strip())
        return factory(handle, target.strip() or resource.name, resource)

    async def close(self) -> None:
        for resource in self._resources.values():
            closer = getattr(resource.provider, "close", None)
            if closer is not None:
                await closer()
        await self.connections.close_all()
        self._bound = False

    # -- menu ---------------------------------------------------------------

    def set_group_order(self, **orders: int) -> None:
        """Control the order of menu groups: ``set_group_order(Sales=10)``."""
        self._group_order.update(orders)

    def menu(self, identity: Identity) -> list[MenuGroup]:
        """The navigation this caller should see.

        Resources the caller cannot read are omitted rather than shown and
        refused, which is both kinder and less informative to a probe.
        """
        groups: dict[str, MenuGroup] = {}
        for resource in self._resources.values():
            if not resource.in_menu:
                continue
            if not resource.policy.allows("read", identity):
                continue
            group_name = resource.menu_group
            group = groups.get(group_name)
            if group is None:
                group = MenuGroup(
                    label=group_name,
                    order=self._group_order.get(group_name, 100),
                )
                groups[group_name] = group
            group.items.append(
                MenuItem(
                    label=resource.label_plural,
                    url=f"/r/{resource.name}",
                    icon=resource.icon,
                    order=resource.menu_order,
                    group=group_name,
                    resource=resource.name,
                )
            )
        # Ungrouped items first, then groups in configured order.
        return sorted(groups.values(), key=lambda g: (g.order, g.label))

    def __repr__(self) -> str:
        state = "bound" if self._bound else "unbound"
        return f"<Registry {len(self._resources)} resources, {state}>"
