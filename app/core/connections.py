"""Named connections to backing systems.

A connection is a configured, reusable handle to something external -- a
database engine, an HTTP client, a message broker channel. Resources never
create these; they name one, and the registry hands over a provider bound to it.

Two properties matter. Connections open lazily, so a misconfigured analytics
database does not stop the app from serving contacts. And every type reports
health uniformly, so ``crm check-connections`` can tell you what is actually
reachable before you go looking for the bug elsewhere.
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import yaml

from app.core.errors import ConfigError

#: Builds the live handle for a connection type.
Opener = Callable[["ConnectionSpec"], Awaitable[Any]]
#: Releases it on shutdown.
Closer = Callable[[Any], Awaitable[None]]
#: Reports ``(healthy, detail)``.
Checker = Callable[[Any], Awaitable[tuple[bool, str]]]


@dataclass(slots=True)
class ConnectionType:
    name: str
    open: Opener
    close: Closer | None = None
    check: Checker | None = None


_TYPES: dict[str, ConnectionType] = {}


def register_connection(
    name: str, *, close: Closer | None = None, check: Checker | None = None
) -> Callable[[Opener], Opener]:
    """Register how to open a connection of type ``name``.

        @register_connection("sqlalchemy", close=..., check=...)
        async def open_sqlalchemy(spec): ...
    """

    def decorator(opener: Opener) -> Opener:
        _TYPES[name] = ConnectionType(name, opener, close, check)
        return opener

    return decorator


def registered_connection_types() -> tuple[str, ...]:
    return tuple(sorted(_TYPES))


@dataclass(slots=True)
class ConnectionSpec:
    """The configuration for one named connection."""

    name: str
    type: str
    options: dict[str, Any] = dc_field(default_factory=dict)
    #: Skip opening and report as disabled. Useful for optional integrations.
    enabled: bool = True

    def option(self, key: str, default: Any = None, *, required: bool = False) -> Any:
        if required and key not in self.options:
            raise ConfigError(
                f"connection {self.name!r} ({self.type}) requires the option {key!r}"
            )
        return self.options.get(key, default)

    def flag(self, key: str, default: bool = False) -> bool:
        """A boolean option, read the way a configuration file writes them.

        Necessary because environment interpolation produces strings:
        ``echo: ${CRM_SQL_ECHO:-false}`` yields the string ``"false"``, and
        ``bool("false")`` is True. Reading such an option with ``bool()`` turns
        every documented "off" default on -- silently, since the result is a
        working connection that merely does the wrong thing.
        """
        return as_bool(
            self.options.get(key, default),
            where=f"connection {self.name!r}: option {key!r}",
        )

    def number(self, key: str, default: int) -> int:
        """An integer option, tolerating the string an interpolation produces."""
        value = self.options.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(
                f"connection {self.name!r}: option {key!r} should be a whole "
                f"number, not {value!r}"
            ) from None

    def __repr__(self) -> str:
        return f"<ConnectionSpec {self.name!r} type={self.type!r}>"


TRUTHY = {"1", "true", "yes", "on"}
FALSEY = {"0", "false", "no", "off", ""}


def as_bool(value: Any, *, where: str) -> bool:
    """Interpret a configured value as a boolean.

    Anything unrecognised is an error rather than a guess: a typo like
    ``enabled: flase`` should stop the process, not quietly mean True.
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in TRUTHY:
            return True
        if text in FALSEY:
            return False
        raise ConfigError(f"{where} should be true or false, not {value!r}")
    return bool(value)


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Substitute ``${VAR}`` and ``${VAR:-default}`` throughout a config tree.

    Keeping credentials in the environment rather than the YAML file is the
    point; the file stays committable.
    """
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            resolved = os.environ.get(name)
            if resolved is None:
                if default is None:
                    raise ConfigError(
                        f"environment variable {name!r} is referenced by the "
                        f"connection config but is not set"
                    )
                return default
            return resolved

        return _ENV_PATTERN.sub(sub, value)
    if isinstance(value, Mapping):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def load_specs(config: Mapping[str, Any]) -> dict[str, ConnectionSpec]:
    """Turn the ``connections:`` block of a config file into specs."""
    raw = config.get("connections") or {}
    if not isinstance(raw, Mapping):
        raise ConfigError("'connections' must be a mapping of name to settings")

    specs: dict[str, ConnectionSpec] = {}
    for name, settings in raw.items():
        if not isinstance(settings, Mapping):
            raise ConfigError(f"connection {name!r} must be a mapping of settings")
        settings = dict(expand_env(dict(settings)))
        type_name = settings.pop("type", None)
        if not type_name:
            raise ConfigError(f"connection {name!r} does not say what 'type' it is")
        enabled = as_bool(settings.pop("enabled", True), where=f"connection {name!r}: 'enabled'")
        specs[name] = ConnectionSpec(name, str(type_name), settings, enabled)
    return specs


def load_config_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a YAML config file, returning an empty mapping if it is absent."""
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc


@dataclass(slots=True)
class Health:
    name: str
    type: str
    healthy: bool
    detail: str
    opened: bool


class ConnectionRegistry:
    """Opens connections on demand and closes them on shutdown."""

    def __init__(self, specs: Mapping[str, ConnectionSpec] | None = None) -> None:
        self._specs: dict[str, ConnectionSpec] = dict(specs or {})
        self._open: dict[str, Any] = {}

    # -- configuration ------------------------------------------------------

    def add(self, spec: ConnectionSpec) -> None:
        if spec.name in self._open:
            raise ConfigError(f"connection {spec.name!r} is already open; cannot redefine it")
        self._specs[spec.name] = spec

    def add_many(self, specs: Mapping[str, ConnectionSpec]) -> None:
        for spec in specs.values():
            self.add(spec)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> ConnectionRegistry:
        return cls(load_specs(config))

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> ConnectionRegistry:
        return cls.from_config(load_config_file(path))

    def spec(self, name: str) -> ConnectionSpec:
        try:
            return self._specs[name]
        except KeyError:
            known = ", ".join(sorted(self._specs)) or "(none configured)"
            raise ConfigError(
                f"unknown connection {name!r}; configured connections: {known}"
            ) from None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    # -- lifecycle ----------------------------------------------------------

    async def get(self, name: str) -> Any:
        """The live handle for ``name``, opening it on first use."""
        if name in self._open:
            return self._open[name]
        spec = self.spec(name)
        if not spec.enabled:
            raise ConfigError(f"connection {name!r} is disabled in configuration")
        try:
            conn_type = _TYPES[spec.type]
        except KeyError:
            known = ", ".join(registered_connection_types()) or "(none registered)"
            raise ConfigError(
                f"connection {name!r} has unknown type {spec.type!r}; "
                f"registered types: {known}"
            ) from None
        try:
            handle = await conn_type.open(spec)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(
                f"could not open connection {name!r} ({spec.type}): {exc}"
            ) from exc
        self._open[name] = handle
        return handle

    def is_open(self, name: str) -> bool:
        return name in self._open

    async def close(self, name: str) -> None:
        handle = self._open.pop(name, None)
        if handle is None:
            return
        closer = _TYPES[self.spec(name).type].close
        if closer is not None:
            await closer(handle)

    async def close_all(self) -> None:
        """Close everything, continuing past failures so one bad handle does
        not strand the rest."""
        errors: list[str] = []
        for name in list(self._open):
            try:
                await self.close(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            raise ConfigError("errors closing connections: " + "; ".join(errors))

    # -- diagnostics --------------------------------------------------------

    async def health(self, name: str) -> Health:
        """Open ``name`` if needed and report whether it works."""
        spec = self.spec(name)
        if not spec.enabled:
            return Health(name, spec.type, True, "disabled", opened=False)
        try:
            handle = await self.get(name)
        except ConfigError as exc:
            return Health(name, spec.type, False, str(exc), opened=False)
        checker = _TYPES[spec.type].check
        if checker is None:
            return Health(name, spec.type, True, "opened (no health check)", opened=True)
        try:
            healthy, detail = await checker(handle)
        except Exception as exc:
            return Health(name, spec.type, False, f"{type(exc).__name__}: {exc}", opened=True)
        return Health(name, spec.type, healthy, detail, opened=True)

    async def health_all(self) -> list[Health]:
        return [await self.health(name) for name in self.names]
