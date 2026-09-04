"""Module discovery and loading.

A module is a package that adds resources, fields, actions or menu entries to
the registry. Splitting an application this way means a deployment can enable
only the parts it needs, and -- more usefully -- a later module can adjust what
an earlier one declared without editing its source.

Load order follows declared dependencies, so by the time a module runs, every
module it names has already registered.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Protocol

from app.core.errors import ConfigError, RegistryError
from app.core.registry import Registry


@dataclass(slots=True)
class Manifest:
    """What a module declares about itself."""

    name: str
    label: str = ""
    description: str = ""
    version: str = "0.1.0"
    #: Modules that must load first.
    depends: tuple[str, ...] = ()
    #: Menu groups this module introduces, with their ordering.
    menu_groups: dict[str, int] = dc_field(default_factory=dict)
    #: Loaded only when explicitly enabled in settings.
    optional: bool = False

    @classmethod
    def coerce(cls, raw: Any, fallback_name: str) -> Manifest:
        if isinstance(raw, Manifest):
            return raw
        if isinstance(raw, dict):
            data = dict(raw)
            data.setdefault("name", fallback_name)
            data["depends"] = tuple(data.get("depends", ()))
            known = set(cls.__dataclass_fields__)
            unknown = set(data) - known
            if unknown:
                raise ConfigError(
                    f"module {fallback_name!r} manifest has unknown keys: "
                    f"{', '.join(sorted(unknown))}"
                )
            return cls(**data)
        raise ConfigError(f"module {fallback_name!r} has an unreadable MANIFEST")


class ModuleProtocol(Protocol):
    MANIFEST: Any

    def register(self, registry: Registry) -> None: ...


@dataclass(slots=True)
class LoadedModule:
    manifest: Manifest
    module: Any
    source: str

    @property
    def name(self) -> str:
        return self.manifest.name


def discover(package: str = "modules", search_path: Path | None = None) -> list[str]:
    """Find candidate module packages under ``package``.

    Directory scanning is the discovery mechanism because it makes adding a
    module a matter of creating a folder -- no registration file to edit.
    """
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        return []
    paths = [str(search_path)] if search_path else list(getattr(pkg, "__path__", []))
    return [
        f"{package}.{info.name}"
        for info in pkgutil.iter_modules(paths)
        if info.ispkg and not info.name.startswith("_")
    ]


def discover_entry_points(group: str = "crm.modules") -> list[str]:
    """Find modules installed by other distributions.

    This is how a module shipped as its own package plugs in without living
    inside this repository.
    """
    from importlib.metadata import entry_points

    return [ep.value or ep.name for ep in entry_points(group=group)]


def _read_module(dotted: str) -> LoadedModule:
    try:
        mod = importlib.import_module(dotted)
    except Exception as exc:
        raise ConfigError(f"could not import module {dotted!r}: {exc}") from exc
    if not hasattr(mod, "register"):
        raise ConfigError(
            f"module {dotted!r} defines no register(registry) function, so it "
            f"cannot contribute anything"
        )
    fallback = dotted.rsplit(".", 1)[-1]
    manifest = Manifest.coerce(getattr(mod, "MANIFEST", {}), fallback)
    return LoadedModule(manifest, mod, dotted)


def resolve_order(modules: Sequence[LoadedModule]) -> list[LoadedModule]:
    """Order modules so dependencies load first.

    Raises on a cycle or a missing dependency rather than loading in an
    arbitrary order, because a module running before its dependency fails in
    confusing, far-away ways.
    """
    by_name = {m.name: m for m in modules}
    ordered: list[LoadedModule] = []
    state: dict[str, str] = {}

    def visit(name: str, trail: tuple[str, ...]) -> None:
        mark = state.get(name)
        if mark == "done":
            return
        if mark == "visiting":
            cycle = " -> ".join([*trail, name])
            raise ConfigError(f"circular dependency between modules: {cycle}")
        module = by_name.get(name)
        if module is None:
            raise ConfigError(
                f"module {trail[-1]!r} depends on {name!r}, which was not found. "
                f"Available: {', '.join(sorted(by_name)) or '(none)'}"
            )
        state[name] = "visiting"
        for dependency in module.manifest.depends:
            visit(dependency, (*trail, name))
        state[name] = "done"
        ordered.append(module)

    # Deterministic starting order so an unordered set of independent modules
    # still loads the same way every time.
    for module in sorted(modules, key=lambda m: m.name):
        visit(module.name, (module.name,))
    return ordered


#: Submodules a module package may provide, imported when the module is
#: selected. Keeping them out of ``__init__`` matters: every discovered
#: package is imported to read its manifest, including ones that will not be
#: enabled, and an optional module must not put its tables on the shared
#: metadata merely by existing on disk.
SUBMODULES = ("schema",)


def submodule(module: LoadedModule, name: str) -> Any | None:
    """Import ``<package>.<name>``, or None if the module has no such file."""
    dotted = f"{module.source}.{name}"
    if importlib.util.find_spec(dotted) is None:
        return None
    try:
        return importlib.import_module(dotted)
    except Exception as exc:
        raise ConfigError(f"could not import {dotted!r}: {exc}") from exc


def _import_submodules(modules: Sequence[LoadedModule]) -> None:
    for module in modules:
        for name in SUBMODULES:
            submodule(module, name)


def select(
    *,
    package: str = "modules",
    enabled: Iterable[str] | None = None,
    include_entry_points: bool = True,
) -> list[LoadedModule]:
    """Import the modules that would load, without registering anything.

    Importing is a side effect worth having on its own: a module's table
    declarations attach to the shared metadata as it is imported, so this is
    how Alembic learns about the tables belonging to whichever modules are
    enabled -- without needing a registry or any provider connections.
    """
    dotted = discover(package)
    if include_entry_points:
        dotted.extend(discover_entry_points())
    found = [_read_module(name) for name in dict.fromkeys(dotted)]

    wanted = set(enabled or ())
    unknown = wanted - {m.name for m in found}
    if unknown:
        raise ConfigError(
            f"these modules are enabled in settings but were not found: "
            f"{', '.join(sorted(unknown))}"
        )

    # A non-optional module always loads. ``enabled`` adds to that set rather
    # than replacing it, so naming a module in the settings cannot silently
    # switch off the platform's own accounts and access control -- a
    # configuration that produces an application nobody can administer.
    selected = [m for m in found if not m.manifest.optional or m.name in wanted]
    # A dependency of an enabled module is itself required, even if the
    # settings did not list it.
    ordered = resolve_order(_pull_in_dependencies(selected, found))
    _import_submodules(ordered)
    return ordered


def load_modules(
    registry: Registry,
    *,
    package: str = "modules",
    enabled: Iterable[str] | None = None,
    include_entry_points: bool = True,
    on_loaded: Callable[[LoadedModule], None] | None = None,
) -> list[LoadedModule]:
    """Discover, order and register every module.

    ``enabled`` names the optional modules to switch on. Everything not marked
    optional loads regardless.
    """
    loaded: list[LoadedModule] = []
    for module in select(
        package=package, enabled=enabled, include_entry_points=include_entry_points
    ):
        registry.set_group_order(**module.manifest.menu_groups)
        try:
            module.module.register(registry)
        except (RegistryError, ConfigError):
            raise
        except Exception as exc:
            raise ConfigError(
                f"module {module.name!r} failed while registering: {exc}"
            ) from exc
        loaded.append(module)
        if on_loaded is not None:
            on_loaded(module)
    return loaded


def _pull_in_dependencies(
    selected: list[LoadedModule], found: list[LoadedModule]
) -> list[LoadedModule]:
    by_name = {m.name: m for m in found}
    result = {m.name: m for m in selected}
    queue = list(selected)
    while queue:
        module = queue.pop()
        for dependency in module.manifest.depends:
            if dependency in result:
                continue
            required = by_name.get(dependency)
            if required is None:
                raise ConfigError(
                    f"module {module.name!r} depends on {dependency!r}, which was not found"
                )
            result[dependency] = required
            queue.append(required)
    return list(result.values())
