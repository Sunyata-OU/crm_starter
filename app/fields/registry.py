"""The field type registry.

Field types are looked up by name so a resource can be declared from data --
YAML, a database table, a generator -- not only from Python imports. Adding a
type is a decorator away, which is the intended extension point for anyone
building on this starter.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from app.core.errors import RegistryError
from app.fields.base import Field

F = TypeVar("F", bound=type[Field])

_TYPES: dict[str, type[Field]] = {}


def register_field(name: str) -> Callable[[F], F]:
    """Register a field class under ``name``.

    Re-registering a name replaces the previous class, which lets a project
    override a built-in type (a stricter email field, say) without forking.
    """

    def decorator(cls: F) -> F:
        cls.type_name = name
        _TYPES[name] = cls
        return cls

    return decorator


def get_field_type(name: str) -> type[Field]:
    """The field class registered under ``name``."""
    try:
        return _TYPES[name]
    except KeyError:
        known = ", ".join(sorted(_TYPES)) or "(none registered)"
        raise RegistryError(f"unknown field type {name!r}; known types: {known}") from None


def make_field(type_name: str, name: str, **options: Any) -> Field:
    """Build a field instance from a type name -- the data-driven entry point."""
    return get_field_type(type_name)(name, **options)


def registered_types() -> dict[str, type[Field]]:
    """A copy of the registry, for introspection and the CLI."""
    return dict(_TYPES)
