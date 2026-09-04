"""Rate limiting for the endpoints worth guessing at.

Account lockout and rate limiting solve different halves of the same problem,
which is why both are here. Lockout protects *one account* from many guesses:
after eight wrong passwords, that account stops accepting them. It does nothing
about the opposite shape of attack -- one guess each against ten thousand
accounts, which is what credential stuffing is -- because no single account ever
reaches its limit. Rate limiting by source address is what bounds that.

The algorithm is a token bucket, chosen over a fixed window because a fixed
window lets an attacker send a full window's worth at 59.9 seconds and another
at 60.1. The bucket refills continuously, so the long-run rate is what the
configuration says.

Two honest limitations, stated rather than hidden:

* The buckets are in memory, so they are per worker. Four workers means roughly
  four times the configured rate. Sizing the limit with that in mind is simpler
  than requiring Redis to start the application, and a shared cache backend
  raises the ceiling for a deployment that wants a hard number.
* An address is a poor identifier. A corporate NAT is one address for a
  thousand people, and an attacker with a botnet has thousands of addresses for
  one person. It raises the cost of the easy attack; it does not stop a
  determined one. That is why lockout exists as well.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("crm.ratelimit")


@dataclass(slots=True)
class Bucket:
    tokens: float
    updated: float


@dataclass(slots=True)
class RateLimiter:
    """A token bucket per key.

    ``rate`` is refills per second and ``burst`` is the bucket's capacity, so a
    caller may arrive in a clump of ``burst`` and then sustain ``rate``. For a
    login form the useful shape is a small burst -- people do mistype -- and a
    slow refill.
    """

    rate: float
    burst: int
    #: Keys idle longer than this are forgotten, so the map does not grow with
    #: every address that has ever visited.
    idle_seconds: float = 3600.0
    max_keys: int = 50_000
    _buckets: dict[str, Bucket] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.rate > 0 and self.burst > 0

    def check(self, key: str) -> bool:
        """Take one token. False means the caller is over their limit."""
        if not self.enabled:
            return True
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_keys:
                self._evict(now)
            self._buckets[key] = Bucket(tokens=self.burst - 1, updated=now)
            return True

        # Refill for the time that has passed, never above capacity.
        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    def retry_after(self, key: str) -> int:
        """Seconds until one more attempt would be allowed."""
        bucket = self._buckets.get(key)
        if bucket is None or not self.enabled:
            return 0
        return max(1, int((1 - bucket.tokens) / self.rate))

    def reset(self, key: str) -> None:
        """Forget a key. Called after a *successful* sign-in.

        Someone who has just proved who they are should not spend the rest of
        the hour rationed because they mistyped twice first.
        """
        self._buckets.pop(key, None)

    def _evict(self, now: float) -> None:
        stale = [k for k, b in self._buckets.items() if now - b.updated > self.idle_seconds]
        for key in stale:
            self._buckets.pop(key, None)
        if len(self._buckets) >= self.max_keys:
            # Still full: drop the oldest tenth rather than refuse to serve.
            oldest = sorted(self._buckets, key=lambda k: self._buckets[k].updated)
            for key in oldest[: max(1, self.max_keys // 10)]:
                self._buckets.pop(key, None)
            log.warning("rate limiter at capacity; dropped the oldest entries")
