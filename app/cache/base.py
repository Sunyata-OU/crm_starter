"""A small shared cache.

Deliberately small. The tempting thing to build is a generic query cache that
memoises every provider read; the reason not to is invalidation. A framework
cannot know when someone else's database changed, and with row-level scoping a
wrongly shared entry is not a stale number on a screen -- it is one person
seeing another's records. So nothing here caches records.

What it does cache is the handful of things whose key is obvious and whose
invalidation point is a write this application performs:

* relation labels -- the id-to-name lookups every list and board repeats;
* permission grants, where a *shared* cache also fixes something a per-worker
  one cannot: a permission change reaching every worker at once instead of
  within a TTL;
* aggregate counts, on a short TTL, where being a few seconds stale is
  obviously acceptable and the alternative is a table scan per dashboard.

Two backends ship. The in-memory one needs nothing and is per-worker, which is
correct for a single process and a lie in a cluster. A shared one -- Redis or
anything speaking its protocol -- is what makes the cache mean the same thing
on every host. Both satisfy the same protocol, so code written against it does
not know or care which is configured.

**A cache must never be load-bearing.** Every method here fails soft: a backend
that is down produces a miss, not an exception. A cache outage should make an
application slow, never broken.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Hashable, Iterable
from typing import Any, Protocol

log = logging.getLogger("crm.cache")

#: What a cache entry may hold. Not records: see the module docstring.
Value = Any


class Cache(Protocol):
    """The interface a cache backend implements."""

    name: str

    async def get(self, key: str) -> Value | None: ...
    async def set(self, key: str, value: Value, ttl: float | None = None) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def get_many(self, keys: Iterable[str]) -> dict[str, Value]: ...
    async def set_many(self, values: dict[str, Value], ttl: float | None = None) -> None: ...
    #: Drop everything under a prefix. The invalidation hook a write uses.
    async def clear(self, prefix: str = "") -> None: ...
    async def health(self) -> tuple[bool, str]: ...


class BaseCache:
    """Defaults, including the fail-soft behaviour every backend must have."""

    name = "cache"

    async def get(self, key: str) -> Value | None:
        return None

    async def set(self, key: str, value: Value, ttl: float | None = None) -> None:
        return None

    async def delete(self, *keys: str) -> None:
        return None

    async def get_many(self, keys: Iterable[str]) -> dict[str, Value]:
        found = {}
        for key in keys:
            value = await self.get(key)
            if value is not None:
                found[key] = value
        return found

    async def set_many(self, values: dict[str, Value], ttl: float | None = None) -> None:
        for key, value in values.items():
            await self.set(key, value, ttl)

    async def clear(self, prefix: str = "") -> None:
        return None

    async def health(self) -> tuple[bool, str]:
        return True, "no health check implemented"

    async def close(self) -> None:
        return None

    async def get_or_set(
        self, key: str, factory: Callable[[], Any], ttl: float | None = None
    ) -> Value:
        """Read through: return the cached value, or compute and store it.

        The obvious helper, and the one place a caller can forget the fail-soft
        rule, so it is written once here. If the cache misbehaves, the factory
        still runs and the caller still gets an answer.
        """
        try:
            hit = await self.get(key)
        except Exception:
            log.warning("cache read failed for %r; continuing without it", key, exc_info=True)
            hit = None
        if hit is not None:
            return hit

        value = factory()
        if hasattr(value, "__await__"):
            value = await value
        if value is not None:
            try:
                await self.set(key, value, ttl)
            except Exception:
                log.warning("cache write failed for %r", key, exc_info=True)
        return value


class NullCache(BaseCache):
    """Caches nothing. The default, so caching is something a deployment opts into.

    Having a real object rather than ``None`` means no caller needs an
    ``if cache is not None`` branch, and the uncached path is exercised by the
    same code as the cached one.
    """

    name = "null"

    async def health(self) -> tuple[bool, str]:
        return True, "caching is off"


class MemoryCache(BaseCache):
    """A dict with expiry times.

    Per-process, so in a cluster each worker keeps its own copy and they can
    disagree for up to the TTL. That is fine for immutable-ish things like
    relation labels and wrong for anything that must be consistent -- which is
    why permission grants get a shared cache when one is configured.
    """

    name = "memory"

    def __init__(self, *, max_entries: int = 10_000) -> None:
        self._entries: dict[str, tuple[float | None, Value]] = {}
        self.max_entries = max_entries

    async def get(self, key: str) -> Value | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires is not None and expires < time.monotonic():
            self._entries.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Value, ttl: float | None = None) -> None:
        if len(self._entries) >= self.max_entries:
            self._evict()
        expires = time.monotonic() + ttl if ttl else None
        self._entries[key] = (expires, value)

    def _evict(self) -> None:
        """Make room: drop what has expired, and failing that the oldest tenth.

        Not an LRU. Tracking access order costs more than it saves at this size,
        and the entries here are cheap to recompute -- an unlucky eviction is
        one extra query, not a correctness problem.
        """
        now = time.monotonic()
        expired = [k for k, (e, _) in self._entries.items() if e is not None and e < now]
        for key in expired:
            self._entries.pop(key, None)
        if len(self._entries) < self.max_entries:
            return
        for key in list(self._entries)[: max(1, self.max_entries // 10)]:
            self._entries.pop(key, None)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._entries.pop(key, None)

    async def clear(self, prefix: str = "") -> None:
        if not prefix:
            self._entries.clear()
            return
        for key in [k for k in self._entries if k.startswith(prefix)]:
            self._entries.pop(key, None)

    async def health(self) -> tuple[bool, str]:
        return True, f"{len(self._entries)} entries in memory"

    def __len__(self) -> int:
        return len(self._entries)


def key(*parts: Hashable) -> str:
    """Build a cache key from its parts.

    A single helper so keys are shaped the same everywhere and a prefix can be
    cleared reliably -- ``clear("labels:contacts:")`` only works if every key
    under it was built the same way.
    """
    return ":".join("" if p is None else str(p) for p in parts)
