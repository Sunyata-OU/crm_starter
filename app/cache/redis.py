"""A cache shared by every worker and every host.

The reason to reach for this rather than :class:`~app.cache.base.MemoryCache`
is not speed -- an in-process dict is faster than any network -- it is
agreement. A per-worker cache means four workers can hold four different
answers, which is tolerable for a relation label and not for a permission
grant. One shared cache makes an invalidation mean the same thing everywhere,
and is what lets a permission change take effect immediately across a cluster
instead of within a TTL.

The client is imported lazily, so a deployment that does not configure a cache
never needs the dependency installed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from app.cache.base import BaseCache, Value

log = logging.getLogger("crm.cache")


class RedisCache(BaseCache):
    """A cache over Redis, or anything that speaks its protocol.

    Values are stored as JSON rather than pickled: a cache is shared state, and
    unpickling shared state is a way to execute whatever is in it. JSON also
    means a value written by one version of the application can be read by the
    next, which matters during a rolling deploy.
    """

    name = "redis"

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "crm:",
        default_ttl: float | None = 300.0,
        socket_timeout: float = 1.0,
    ) -> None:
        self.url = url
        self.prefix = prefix
        self.default_ttl = default_ttl
        # Short by design. A cache is not allowed to be the slow part of a
        # request; if it cannot answer quickly, doing the work is faster than
        # waiting for it.
        self.socket_timeout = socket_timeout
        self._client: Any = None

    async def client(self) -> Any:
        if self._client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as exc:  # pragma: no cover - depends on install
                raise RuntimeError(
                    "the redis cache backend needs the 'redis' package: "
                    "uv add redis"
                ) from exc
            self._client = Redis.from_url(
                self.url,
                socket_timeout=self.socket_timeout,
                socket_connect_timeout=self.socket_timeout,
                decode_responses=True,
            )
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    async def get(self, key: str) -> Value | None:
        try:
            raw = await (await self.client()).get(self._key(key))
        except Exception:
            # Fail soft: a cache outage makes the application slower, never
            # broken. This is the rule the whole design depends on.
            log.warning("cache unavailable on read", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def set(self, key: str, value: Value, ttl: float | None = None) -> None:
        ttl = self.default_ttl if ttl is None else ttl
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError):
            log.warning("value for %r is not JSON-serialisable; not cached", key)
            return
        try:
            client = await self.client()
            if ttl:
                await client.set(self._key(key), payload, ex=int(ttl))
            else:
                await client.set(self._key(key), payload)
        except Exception:
            log.warning("cache unavailable on write", exc_info=True)

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await (await self.client()).delete(*(self._key(k) for k in keys))
        except Exception:
            log.warning("cache unavailable on delete", exc_info=True)

    async def get_many(self, keys: Iterable[str]) -> dict[str, Value]:
        wanted = list(keys)
        if not wanted:
            return {}
        try:
            raw = await (await self.client()).mget([self._key(k) for k in wanted])
        except Exception:
            log.warning("cache unavailable on read", exc_info=True)
            return {}
        found = {}
        for key, value in zip(wanted, raw, strict=True):
            if value is None:
                continue
            try:
                found[key] = json.loads(value)
            except ValueError:
                continue
        return found

    async def clear(self, prefix: str = "") -> None:
        """Delete everything under a prefix.

        Uses SCAN rather than KEYS: KEYS blocks the server for the length of
        the keyspace, which on a shared Redis is somebody else's outage.
        """
        pattern = f"{self.prefix}{prefix}*"
        try:
            client = await self.client()
            batch: list[str] = []
            async for key in client.scan_iter(match=pattern, count=500):
                batch.append(key)
                if len(batch) >= 500:
                    await client.delete(*batch)
                    batch = []
            if batch:
                await client.delete(*batch)
        except Exception:
            log.warning("cache unavailable on clear", exc_info=True)

    async def health(self) -> tuple[bool, str]:
        try:
            await (await self.client()).ping()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"{self.url.split('@')[-1]} reachable"

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
