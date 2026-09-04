"""Caching.

One interface, several backends, chosen in ``connections.yaml`` -- the same
arrangement as file storage and data providers. The default is to cache
nothing, so a deployment opts in rather than discovering stale data it did not
ask for.

Importing this package registers the built-in backends.
"""

from __future__ import annotations

from app.cache.base import (  # noqa: F401
    BaseCache,
    Cache,
    MemoryCache,
    NullCache,
    Value,
    key,
)
from app.cache.redis import RedisCache  # noqa: F401
from app.core.connections import ConnectionSpec, register_connection
from app.core.errors import ConfigError

__all__ = [
    "BaseCache",
    "Cache",
    "MemoryCache",
    "NullCache",
    "RedisCache",
    "Value",
    "cache",
    "key",
]


#: The process-wide cache, replaced at startup when one is configured.
#:
#: A real object rather than ``None`` so no caller needs a null check, and the
#: uncached path is the same code as the cached one.
cache: BaseCache = NullCache()


def use(backend: BaseCache) -> None:
    """Install the cache the application will use."""
    global cache
    cache = backend


@register_connection(
    "cache",
    close=lambda handle: handle.close(),
    check=lambda handle: handle.health(),
)
async def open_cache(spec: ConnectionSpec) -> BaseCache:
    """Open a cache from a ``type: cache`` connection.

        cache.main:
          type: cache
          backend: memory        # or: redis
          url: ${CRM_REDIS_URL}
    """
    backend = str(spec.option("backend", "memory")).lower()
    if backend in ("memory", "local"):
        return MemoryCache(max_entries=spec.number("max_entries", 10_000))
    if backend in ("redis", "valkey"):
        return RedisCache(
            str(spec.option("url", required=True)),
            prefix=str(spec.option("prefix", "crm:")),
            default_ttl=float(spec.option("default_ttl", 300)),
            socket_timeout=float(spec.option("timeout", 1.0)),
        )
    if backend in ("none", "null", "off"):
        return NullCache()
    raise ConfigError(
        f"unknown cache backend {backend!r}; expected memory, redis or none"
    )
