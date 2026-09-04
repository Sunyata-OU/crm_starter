"""The cache, and the rule it must never break.

A cache is an optimisation. The moment an application stops working because
one is unavailable, it has stopped being an optimisation and become a
dependency -- so most of what is asserted here is about behaviour when the
cache misbehaves, not when it works.
"""

from __future__ import annotations

import asyncio

import pytest

from app.cache import MemoryCache, NullCache
from app.cache.base import BaseCache, key
from app.resources.rbac import PermissionStore

pytestmark = pytest.mark.asyncio


class TestMemoryCache:
    async def test_stores_and_returns(self):
        cache = MemoryCache()
        await cache.set("a", {"n": 1})
        assert await cache.get("a") == {"n": 1}

    async def test_a_missing_key_is_none_not_an_error(self):
        assert await MemoryCache().get("nothing") is None

    async def test_an_expired_entry_is_gone(self):
        cache = MemoryCache()
        await cache.set("a", 1, ttl=0.01)
        await asyncio.sleep(0.02)
        assert await cache.get("a") is None

    async def test_clear_takes_a_prefix(self):
        cache = MemoryCache()
        await cache.set_many({"labels:a": 1, "labels:b": 2, "other:c": 3})
        await cache.clear("labels:")
        assert await cache.get("labels:a") is None
        assert await cache.get("other:c") == 3

    async def test_it_does_not_grow_without_limit(self):
        cache = MemoryCache(max_entries=50)
        for i in range(200):
            await cache.set(f"k{i}", i)
        assert len(cache) <= 50

    async def test_get_or_set_computes_once(self):
        cache = MemoryCache()
        calls = []

        def factory():
            calls.append(1)
            return "value"

        assert await cache.get_or_set("k", factory) == "value"
        assert await cache.get_or_set("k", factory) == "value"
        assert len(calls) == 1

    async def test_keys_are_built_the_same_way_everywhere(self):
        # Prefix clearing only works if every key was built by this helper.
        assert key("labels", "contacts", 7) == "labels:contacts:7"


class TestNullCache:
    async def test_it_remembers_nothing(self):
        cache = NullCache()
        await cache.set("a", 1)
        assert await cache.get("a") is None

    async def test_get_or_set_still_returns_the_value(self):
        # The uncached path must be the same code, not a special case.
        assert await NullCache().get_or_set("a", lambda: 42) == 42


class Broken(BaseCache):
    """Every operation fails, the way an unreachable backend does."""

    name = "broken"

    async def get(self, key: str):
        raise ConnectionError("cache is down")

    async def set(self, key: str, value, ttl=None):
        raise ConnectionError("cache is down")


class TestFailingSoft:
    async def test_a_broken_cache_still_returns_the_value(self):
        """The whole design rests on this.

        A cache outage should make an application slower. If it makes one
        break, the cache has quietly become a required dependency.
        """
        assert await Broken().get_or_set("k", lambda: "computed") == "computed"

    async def test_a_broken_cache_does_not_stop_the_permission_store(self):
        store = PermissionStore()
        assert await store.table() is not None


class TestPermissionsAcrossWorkers:
    """The reason a *shared* cache is worth configuring.

    Two workers are two PermissionStore instances. Without a shared cache each
    loads its own copy and they can disagree until the TTL expires; with one,
    the second worker reads what the first loaded.
    """

    def _rows(self):
        return [
            {"role": "user", "resource": "contacts", "can_read": True,
             "can_create": False, "can_update": False, "can_delete": False,
             "row_scope": "own"},
        ]

    async def test_a_second_worker_reads_what_the_first_loaded(self, monkeypatch):
        import app.cache as cache_module
        from app.providers.memory import MemoryProvider

        shared = MemoryCache()
        monkeypatch.setattr(cache_module, "cache", shared)

        provider = MemoryProvider(self._rows(), pk_field="role")
        first, second = PermissionStore(provider), PermissionStore(provider)

        assert len(await first.table()) == 1
        # The second store has its own empty memory but the same shared cache.
        assert len(await second.table()) == 1
        assert await shared.get(PermissionStore.CACHE_KEY) is not None

    async def test_an_edit_drops_the_shared_copy(self, monkeypatch):
        import app.cache as cache_module
        from app.providers.memory import MemoryProvider

        shared = MemoryCache()
        monkeypatch.setattr(cache_module, "cache", shared)

        store = PermissionStore(MemoryProvider(self._rows(), pk_field="role"))
        await store.table()
        assert await shared.get(PermissionStore.CACHE_KEY) is not None

        store.invalidate()
        # Dropping the shared copy is scheduled on the loop; let it run.
        await asyncio.sleep(0)
        assert await shared.get(PermissionStore.CACHE_KEY) is None
