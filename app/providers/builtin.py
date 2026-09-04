"""Importing the built-in providers so they register themselves.

Connection types and provider factories register on import, via decorators.
Anything that needs to resolve a ``connections.yaml`` entry must therefore have
imported the relevant module first -- otherwise the type appears unknown, which
is a confusing way to learn that an import is missing.

This module exists so that requirement is one call rather than a list every
entry point has to keep in step. It is imported for its side effects.
"""

from __future__ import annotations

# Caches and file stores register their own connection types the same way.
from app import cache as _cache  # noqa: F401
from app import storage as _storage  # noqa: F401

# Ordering does not matter; each registers under its own type name.
from app.providers import amqp as _amqp  # noqa: F401
from app.providers import rest as _rest  # noqa: F401
from app.providers import sql as _sql  # noqa: F401

#: Connection types this module makes available.
BUILTIN_CONNECTION_TYPES = ("sqlalchemy", "rest", "amqp", "files", "cache")


def ensure_registered() -> tuple[str, ...]:
    """Confirm the built-ins are present, and report what is registered.

    Callable rather than relying on the import alone, so a caller can be
    explicit about why it imported this module.
    """
    from app.core.connections import registered_connection_types

    return registered_connection_types()
