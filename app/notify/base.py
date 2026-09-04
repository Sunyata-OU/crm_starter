"""Telling people about things.

A notification is a record, and delivering it is separate from creating it.
That split is the whole design: the in-app bell always works because it reads
the row, while email, chat or a webhook are channels that may fail, be slow, or
be switched off entirely without losing the notification itself.

Channels are pluggable in the same way as data providers and file stores.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.core.clock import UTC_ZONE as UTC
from app.core.clock import utcnow

log = logging.getLogger("crm.notify")


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class Kind(StrEnum):
    """What a notification is about. Drives the icon and the default channels."""

    INFO = "info"
    ASSIGNED = "assigned"
    MENTIONED = "mentioned"
    DUE = "due"
    OVERDUE = "overdue"
    CHANGED = "changed"
    APPROVAL = "approval"
    ERROR = "error"


@dataclass(slots=True)
class Notification:
    """One thing one person should know about."""

    recipient: str
    title: str
    body: str = ""
    kind: Kind | str = Kind.INFO
    priority: Priority | str = Priority.NORMAL
    #: What it concerns, so the notification can link back.
    resource: str = ""
    record_id: str = ""
    url: str = ""
    #: When it becomes relevant. A reminder is simply a future one.
    due_at: datetime | None = None
    #: Who caused it. Used to avoid telling someone about their own action.
    actor: str = ""
    channels: tuple[str, ...] = ()
    meta: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = Kind(self.kind) if not isinstance(self.kind, Kind) else self.kind
        if not isinstance(self.priority, Priority):
            self.priority = Priority(self.priority)
        # A naive due date may arrive from a caller or from a driver that
        # dropped the offset. Assume UTC, which is what is stored, rather than
        # letting a comparison fail later with "can't compare offset-naive and
        # offset-aware datetimes".
        if self.due_at is not None and self.due_at.tzinfo is None:
            self.due_at = self.due_at.replace(tzinfo=UTC)
        if not self.url and self.resource and self.record_id:
            self.url = f"/r/{self.resource}/{self.record_id}"

    @property
    def is_scheduled(self) -> bool:
        """Whether this is for later rather than now."""
        return self.due_at is not None and self.due_at > utcnow()

    def as_row(self) -> dict[str, Any]:
        """The stored form."""
        return {
            "recipient": self.recipient,
            "kind": str(self.kind),
            "priority": str(self.priority),
            "title": self.title[:200],
            "body": self.body or None,
            "resource": self.resource or None,
            "record_id": self.record_id or None,
            "url": self.url or None,
            "due_at": self.due_at,
            "actor": self.actor or None,
            "channels": ",".join(self.channels) or None,
        }


@dataclass(slots=True)
class Delivery:
    """What one channel made of one notification."""

    channel: str
    delivered: bool
    detail: str = ""
    #: Whether retrying might succeed. A timeout or a 503 is transient; a
    #: rejected address or a 404 is not, and repeating it wastes time and
    #: delays the report of a real problem.
    transient: bool = False

    @property
    def ok(self) -> bool:
        return self.delivered


@runtime_checkable
class Channel(Protocol):
    """Somewhere a notification can be sent."""

    name: str

    async def send(self, notification: Notification) -> Delivery: ...
    async def health(self) -> tuple[bool, str]: ...


#: How many times a transient delivery failure is retried, and how long the
#: first wait is. Doubling each time, so 0.5s, 1s, 2s -- over in a few seconds,
#: which is the point: a delivery is usually running in the background of a
#: user's save, and a minute of patient retrying helps nobody.
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5


async def with_retries(
    attempt: Callable[[], Awaitable[Delivery]],
    *,
    attempts: int = RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    channel: str = "",
) -> Delivery:
    """Run a delivery, retrying the failures that are worth retrying.

    The distinction matters more than the retrying. A refused address, a
    rejected payload, a 404 webhook: repeating those produces the same answer
    three times and delays the report of a real problem. A timeout, a dropped
    connection, a 503: those are a different service having a bad second, and
    the same request a moment later usually succeeds.

    So only :attr:`Delivery.transient` failures are retried, and each channel
    decides what counts as one.
    """
    import asyncio

    delay = base_delay
    result = await attempt()
    for remaining in range(attempts - 1, 0, -1):
        if result.ok or not result.transient:
            return result
        log.info(
            "%s delivery failed transiently (%s); %d attempt(s) left",
            channel or "channel", result.detail, remaining,
        )
        await asyncio.sleep(delay)
        delay *= 2
        result = await attempt()
    return result


class BaseChannel:
    """Shared behaviour for channels."""

    name = "channel"
    #: Notifications below this priority are ignored by this channel. Email is
    #: worth using for something urgent and irritating for everything else.
    min_priority: Priority = Priority.LOW

    _ORDER = {Priority.LOW: 0, Priority.NORMAL: 1, Priority.HIGH: 2, Priority.URGENT: 3}

    def wants(self, notification: Notification) -> bool:
        """Whether this channel should handle a given notification."""
        if notification.channels and self.name not in notification.channels:
            return False
        priority = notification.priority
        if not isinstance(priority, Priority):
            priority = Priority(priority)
        return self._ORDER[priority] >= self._ORDER[self.min_priority]

    async def send(self, notification: Notification) -> Delivery:
        raise NotImplementedError

    async def health(self) -> tuple[bool, str]:
        return True, "no health check implemented"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


_CHANNELS: dict[str, Callable[..., BaseChannel]] = {}


def register_channel(name: str) -> Callable[[Any], Any]:
    """Register a delivery channel under ``name``."""

    def decorator(factory: Any) -> Any:
        _CHANNELS[name] = factory
        return factory

    return decorator


def registered_channels() -> tuple[str, ...]:
    return tuple(sorted(_CHANNELS))


def build_channel(kind: str, **options: Any) -> BaseChannel:
    from app.core.errors import ConfigError

    factory = _CHANNELS.get(kind)
    if factory is None:
        known = ", ".join(registered_channels()) or "(none registered)"
        raise ConfigError(f"unknown notification channel {kind!r}; registered: {known}")
    return factory(**options)


# -- helpers for the common cases ------------------------------------------


def assigned(
    recipient: str, resource: str, record_id: Any, label: str, *, actor: str = ""
) -> Notification:
    """Someone has been given a record to deal with."""
    return Notification(
        recipient=recipient,
        kind=Kind.ASSIGNED,
        title=f"You have been assigned {label}",
        resource=resource,
        record_id=str(record_id),
        actor=actor,
        priority=Priority.NORMAL,
    )


def due(
    recipient: str, resource: str, record_id: Any, label: str, when: datetime
) -> Notification:
    """A task falls due. Scheduled rather than immediate."""
    return Notification(
        recipient=recipient,
        kind=Kind.DUE,
        title=f"Due: {label}",
        resource=resource,
        record_id=str(record_id),
        due_at=when,
        priority=Priority.NORMAL,
    )


def overdue(recipient: str, resource: str, record_id: Any, label: str) -> Notification:
    return Notification(
        recipient=recipient,
        kind=Kind.OVERDUE,
        title=f"Overdue: {label}",
        resource=resource,
        record_id=str(record_id),
        priority=Priority.HIGH,
    )


def reminder_for(when: datetime, *, before: timedelta = timedelta(days=1)) -> datetime:
    """When to warn about something happening at ``when``."""
    return when - before
