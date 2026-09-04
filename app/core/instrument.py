"""Where the time goes.

Performance work needs a number before it needs an opinion, and the number
that matters for a data-driven application is almost always *how many queries
did that page cost*. A view that looks fine on the demo's twelve contacts and
falls over on ten thousand is nearly always issuing one query per row; the only
reliable way to catch that is to count.

So every engine the application opens is instrumented, the count and the time
are kept per request in a context variable, and a request that goes over the
configured thresholds is logged with the statement that took longest. Tests use
the same mechanism through :func:`measure`, which is how the query count of a
view can be asserted rather than hoped for.

The overhead is two event handlers and a float subtraction per statement --
small enough to leave on in production, where the slow-request log is the thing
that tells you which page regressed.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("crm.perf")

#: Statements are truncated in logs; the shape is what identifies a query, and
#: the parameters may hold personal data that has no business in a log file.
MAX_STATEMENT_CHARS = 200


@dataclass(slots=True)
class Stats:
    """What one unit of work cost."""

    queries: int = 0
    query_ms: float = 0.0
    #: The single slowest statement, as (statement, milliseconds).
    slowest: tuple[str, float] | None = None
    #: Per-statement counts, so a repeated query stands out from many
    #: different ones -- the difference between "this page is complex" and
    #: "this page has an N+1".
    by_statement: dict[str, int] = field(default_factory=dict)

    def record(self, statement: str, ms: float) -> None:
        self.queries += 1
        self.query_ms += ms
        key = " ".join(statement.split())[:MAX_STATEMENT_CHARS]
        self.by_statement[key] = self.by_statement.get(key, 0) + 1
        if self.slowest is None or ms > self.slowest[1]:
            self.slowest = (key, ms)

    @property
    def repeated(self) -> tuple[str, int] | None:
        """The most-repeated statement, when one was issued more than once.

        A repeat count that grows with the number of rows on the page is the
        signature of an N+1.
        """
        if not self.by_statement:
            return None
        statement, count = max(self.by_statement.items(), key=lambda kv: kv[1])
        return (statement, count) if count > 1 else None


_current: contextvars.ContextVar[Stats | None] = contextvars.ContextVar(
    "crm_query_stats", default=None
)


def current() -> Stats | None:
    """The statistics being collected, if anything is collecting."""
    return _current.get()


@contextmanager
def measure() -> Iterator[Stats]:
    """Collect statistics for the enclosed block.

    Nesting is deliberately not supported: an inner block would silently
    detach the outer one's counting, and a wrong number is worse than none.
    The middleware opens exactly one of these per request.
    """
    stats = Stats()
    token = _current.set(stats)
    try:
        yield stats
    finally:
        _current.reset(token)


def instrument_engine(engine: Any) -> None:
    """Count and time every statement an engine executes.

    Accepts a sync or async engine; the listeners go on the sync engine
    underneath, which is where SQLAlchemy emits these events regardless of
    which API the caller used.
    """
    from sqlalchemy import event

    target = getattr(engine, "sync_engine", engine)
    if getattr(target, "_crm_instrumented", False):
        return

    @event.listens_for(target, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        conn.info["_crm_started"] = time.perf_counter()

    @event.listens_for(target, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        stats = _current.get()
        if stats is None:
            return
        started = conn.info.pop("_crm_started", None)
        if started is None:
            return
        stats.record(statement, (time.perf_counter() - started) * 1000)

    target._crm_instrumented = True


def report(stats: Stats, *, label: str, total_ms: float, warn_queries: int, warn_ms: float) -> None:
    """Log the unit of work when it looks expensive.

    Two separate thresholds because they catch different faults: a high query
    count is a shape problem in the code, while a high elapsed time with a low
    query count is usually a missing index or a slow remote call.
    """
    if stats.queries < warn_queries and total_ms < warn_ms:
        return
    repeated = stats.repeated
    log.warning(
        "slow: %s in %.0fms (%d queries, %.0fms in the database)%s%s",
        label,
        total_ms,
        stats.queries,
        stats.query_ms,
        f"; slowest {stats.slowest[1]:.0f}ms: {stats.slowest[0]}" if stats.slowest else "",
        f"; repeated {repeated[1]}x: {repeated[0]}" if repeated else "",
    )
