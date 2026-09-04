"""Durable background work.

The application already has two ways to do something later, and both are right
for what they do. A delivery spawned on the event loop is immediate and costs
nothing, and is lost if the worker stops. ``crm notify-due`` is a sweep run by
cron, which survives anything but only runs on a schedule.

This is the third: work that is **accepted now and must happen even if this
process does not survive the minute**. A row is written before the caller is
told the work was accepted, and a worker -- this one, another host's, or one
started tomorrow -- picks it up.

Three decisions shape everything below.

**At-least-once, not at-most-once.** A job that fails transiently is retried,
which means a job that succeeded and died before recording it runs twice. That
is the opposite bargain from ``deliver_due``, which claims before sending so a
reminder is never duplicated. The difference is deliberate: a reminder is an
email, where a rare duplicate is worse than a rare miss, whereas a job is code
you wrote, which can be made idempotent. Handlers must be.

**Claimed, not locked.** A worker claims a row with a conditional write and
stamps its name on it. No advisory locks, no ``SELECT ... FOR UPDATE``, nothing
that needs a particular database -- the same ``update_if`` every provider
either supports or honestly refuses.

**Retry only what retrying can fix.** A timeout deserves another attempt; a
``KeyError`` in a handler will produce the same ``KeyError`` five times and
delay the report of a real bug. Handlers raise :class:`Retry` to ask for
another go and anything else to stop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.core.clock import UTC_ZONE as UTC
from app.core.clock import utcnow

log = logging.getLogger("crm.jobs")


class JobStatus(StrEnum):
    """Where a job is.

    Only four, and the absence of a fifth is the point: there is no "retrying"
    state. A job waiting to be retried is ``QUEUED`` with a later ``run_at``,
    so one query finds everything eligible and a retry needs no timer.
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Retry(Exception):
    """Raised by a handler that wants another attempt.

    ``delay`` overrides the backoff when the failure knows better than the
    default -- an HTTP 429 carrying ``Retry-After``, say.
    """

    def __init__(self, reason: str = "", *, delay: float | None = None) -> None:
        super().__init__(reason or "retry requested")
        self.reason = reason
        self.delay = delay


class Cancelled(Exception):
    """Raised by a handler that has decided the job should not run.

    Distinct from a failure: the record is closed as done with the reason
    noted, rather than retried or reported as broken. A job for a record
    somebody deleted in the meantime is the usual case.
    """


#: Attempts before a job is parked as failed, and the first backoff in seconds.
#: Doubling: 30s, 1m, 2m, 4m. Slower than a notification's in-request retry
#: because nobody is waiting -- the cost of patience here is a delay, not a
#: user watching a spinner.
DEFAULT_MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = 30.0
RETRY_MAX_DELAY = 3600.0


def backoff(attempts: int, *, base: float = RETRY_BASE_DELAY) -> float:
    """How long to wait before attempt ``attempts + 1``."""
    return min(base * (2 ** max(0, attempts - 1)), RETRY_MAX_DELAY)


@dataclass(slots=True)
class Job:
    """One unit of work, as it exists in the table."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: Any = None
    status: JobStatus | str = JobStatus.QUEUED
    run_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    claimed_by: str = ""
    last_error: str = ""
    #: Optional idempotency handle. Two jobs with the same key are the same job.
    key: str = ""
    #: Higher runs first among jobs that are due.
    priority: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, JobStatus):
            self.status = JobStatus(self.status)
        if self.run_at is None:
            self.run_at = utcnow()
        elif self.run_at.tzinfo is None:
            # A naive value from a driver that dropped the offset. Assume UTC,
            # which is what is stored, rather than failing a comparison later.
            self.run_at = self.run_at.replace(tzinfo=UTC)

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    def as_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": json.dumps(self.payload, default=str),
            "status": str(self.status),
            "run_at": self.run_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "claimed_by": self.claimed_by or None,
            "key": self.key or None,
            "priority": self.priority,
        }

    @classmethod
    def from_row(cls, row: Any) -> Job:
        """Rebuild a job from a stored record.

        Tolerant of a malformed payload: a job whose JSON cannot be read is
        still a job, and running its handler with an empty payload produces a
        real error message rather than one from inside this function.
        """
        raw = row.get("payload") or "{}"
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            log.warning("job %s has an unreadable payload; treating it as empty", row.get("id"))
            payload = {}
        return cls(
            kind=str(row.get("kind") or ""),
            payload=payload,
            id=row.get("id"),
            status=str(row.get("status") or JobStatus.QUEUED),
            run_at=row.get("run_at"),
            attempts=int(row.get("attempts") or 0),
            max_attempts=int(row.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
            claimed_by=str(row.get("claimed_by") or ""),
            last_error=str(row.get("last_error") or ""),
            key=str(row.get("key") or ""),
            priority=int(row.get("priority") or 0),
        )

    def next_run(self, *, delay: float | None = None) -> datetime:
        return utcnow() + timedelta(seconds=delay if delay is not None else backoff(self.attempts))


@runtime_checkable
class Handler(Protocol):
    """What runs a job. Registered by kind."""

    async def __call__(self, job: Job) -> None: ...


_HANDLERS: dict[str, Handler] = {}


def register(kind: str) -> Callable[[Handler], Handler]:
    """Say how to run jobs of one kind.

        @register("report.export")
        async def export(job): ...
    """

    def decorator(fn: Handler) -> Handler:
        if kind in _HANDLERS:
            raise ValueError(f"a handler for job kind {kind!r} is already registered")
        _HANDLERS[kind] = fn
        return fn

    return decorator


def handler_for(kind: str) -> Handler | None:
    return _HANDLERS.get(kind)


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


def clear_handlers() -> None:
    """For tests. Never call this from application code."""
    _HANDLERS.clear()


#: Signature the worker uses to run one job, so tests can substitute it.
Runner = Callable[[Job], Awaitable[None]]
