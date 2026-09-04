"""Creating and delivering notifications.

The service is a process-wide handle, like the audit sink and the permission
store, because the places that raise a notification -- an action, a provider
hook, a scheduled sweep -- have no registry to hand.

Two rules shape it. A notification is **stored first and delivered second**, so
a failing channel never loses it. And nobody is told about their own action,
because a notification you caused is noise.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.clock import utcnow
from app.core.errors import UnsupportedOperation
from app.core.query import Condition, ListQuery, Op, Sort, SortDir, and_, or_
from app.core.results import Ctx, Identity, Record
from app.notify.base import BaseChannel, Delivery, Kind, Notification, Priority
from app.notify.channels import summarise

log = logging.getLogger("crm.notify")


class Notifier:
    """Stores notifications and hands them to the channels."""

    def __init__(self) -> None:
        self.provider: Any = None
        self.channels: list[BaseChannel] = []
        #: Delivery happens in the background so a slow SMTP server cannot make
        #: a user wait for their save to finish.
        self.background = True
        self._tasks: set[asyncio.Task] = set()
        #: Set by drain(). Reported by `is_closing`, so a caller can tell
        #: whether a late delivery will actually be waited for.
        self._closing = False
        #: Logged once, not once per notification.
        self._warned_about_claiming = False

    # -- wiring -------------------------------------------------------------

    def bind(self, provider: Any) -> None:
        self.provider = provider

    def use(self, channels: Sequence[BaseChannel]) -> None:
        self.channels = list(channels)

    @property
    def configured(self) -> bool:
        return self.provider is not None

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.channels)

    # -- raising ------------------------------------------------------------

    async def send(self, notification: Notification, ctx: Ctx | None = None) -> Record | None:
        """Store a notification and deliver it.

        Returns the stored record, or ``None`` if there was nothing to do.
        """
        if not notification.recipient:
            return None
        # Telling someone about something they just did themselves is noise.
        if notification.actor and notification.actor == notification.recipient:
            return None
        if self.provider is None:
            log.info("notification (no store): %s", notification.title)
            await self._deliver(notification)
            return None

        try:
            result = await self.provider.create(
                notification.as_row(), ctx or Ctx.system()
            )
        except Exception:
            log.exception("could not store a notification: %s", notification.title)
            return None

        record = result.record

        # A future notification is a reminder; the sweep will deliver it when
        # it comes due.
        if notification.is_scheduled:
            return record

        if self.background:
            self._spawn(self._deliver_and_record(notification, record, ctx))
        else:
            await self._deliver_and_record(notification, record, ctx)
        return record

    async def send_many(
        self, notifications: Iterable[Notification], ctx: Ctx | None = None
    ) -> int:
        sent = 0
        for notification in notifications:
            if await self.send(notification, ctx) is not None:
                sent += 1
        return sent

    def _spawn(self, coro) -> None:
        """Run delivery without waiting for it.

        A reference is kept until the task finishes; without one the event loop
        may garbage-collect a running task.
        """
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            # No running loop -- a CLI command, say. Deliver inline instead.
            asyncio.run(coro)
            return
        # Tracked even while shutting down. drain() waits on this set, so a
        # delivery raised during shutdown is still awaited if drain has not
        # begun, and is cancelled with the rest if it has. What it must never
        # be is untracked, which is how a task gets collected mid-flight.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def is_closing(self) -> bool:
        """Whether shutdown has begun and deliveries may be cut short."""
        return self._closing

    async def drain(self, timeout: float = 10.0) -> int:
        """Wait for in-flight deliveries before the process goes away.

        Without this, a deploy cancels whatever was mid-delivery. The
        notification row survives -- it is written before delivery is attempted
        -- but the outbound copy is lost and the row is left claiming it was
        never sent, which is the worst of both: no email, and a record that
        invites a second attempt.

        Bounded, because a shutdown that waits indefinitely on a hung SMTP
        server is its own outage. What does not finish in time is reported.
        """
        self._closing = True
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return 0
        done, still_running = await asyncio.wait(pending, timeout=timeout)
        if still_running:
            log.warning(
                "%d notification deliveries did not finish within %.0fs; "
                "cancelling them", len(still_running), timeout,
            )
            for task in still_running:
                task.cancel()
        return len(done)

    # -- delivery -----------------------------------------------------------

    async def _deliver(self, notification: Notification) -> list[Delivery]:
        results: list[Delivery] = []
        for channel in self.channels:
            if not channel.wants(notification):
                continue
            try:
                results.append(await channel.send(notification))
            except Exception as exc:
                # One channel failing must not stop the others.
                log.warning("channel %s failed: %s", channel.name, exc)
                results.append(Delivery(channel.name, False, str(exc)))
        return results

    async def _deliver_and_record(
        self, notification: Notification, record: Record | None, ctx: Ctx | None
    ) -> None:
        deliveries = await self._deliver(notification)
        if record is None or self.provider is None:
            return
        try:
            await self.provider.update(
                record.pk,
                {"sent_at": utcnow(), "delivery": summarise(deliveries)},
                ctx or Ctx.system(),
            )
        except Exception:
            log.warning("could not record delivery for notification %s", record.pk)

    # -- reading ------------------------------------------------------------

    async def unread_for(self, identity: Identity, ctx: Ctx, limit: int = 20) -> list[Record]:
        """A person's undismissed notifications, newest first."""
        if self.provider is None:
            return []
        query = ListQuery(
            filter=and_(
                Condition("recipient", Op.EQ, identity.email or identity.subject),
                Condition("read_at", Op.IS_NULL),
                # A reminder that is not due yet has not happened.
                or_(
                    Condition("due_at", Op.IS_NULL),
                    Condition("due_at", Op.LTE, utcnow()),
                ),
            ),
            sort=(Sort("created_at", SortDir.DESC),),
            page_size=limit,
            with_total=True,
        )
        page = await self.provider.list(query, ctx)
        return list(page.items)

    async def count_unread(self, identity: Identity, ctx: Ctx) -> int:
        if self.provider is None:
            return 0
        from app.core.query import Agg, AggSpec, Measure

        spec = AggSpec(
            measures=(Measure(Agg.COUNT, alias="count"),),
            filter=and_(
                Condition("recipient", Op.EQ, identity.email or identity.subject),
                Condition("read_at", Op.IS_NULL),
                or_(
                    Condition("due_at", Op.IS_NULL),
                    Condition("due_at", Op.LTE, utcnow()),
                ),
            ),
        )
        rows = await self.provider.aggregate(spec, ctx)
        return int(rows[0].get("count", 0)) if rows else 0

    async def mark_read(self, pk: Any, identity: Identity, ctx: Ctx) -> bool:
        """Dismiss one notification, if it belongs to this caller."""
        if self.provider is None:
            return False
        record = await self.provider.get(pk, ctx)
        if record is None:
            return False
        # A notification is addressed to one person; nobody else may dismiss it.
        if record.get("recipient") != (identity.email or identity.subject):
            return False
        await self.provider.update(pk, {"read_at": utcnow()}, ctx)
        return True

    async def mark_all_read(self, identity: Identity, ctx: Ctx) -> int:
        unread = await self.unread_for(identity, ctx, limit=200)
        for record in unread:
            await self.provider.update(record.pk, {"read_at": utcnow()}, ctx)
        return len(unread)

    # -- reminders ----------------------------------------------------------

    async def deliver_due(self, ctx: Ctx | None = None, limit: int = 200) -> int:
        """Deliver scheduled notifications that have come due.

        Called by ``crm notify-due``, which a cron job or scheduler runs. A
        sweep rather than a timer, so the application owns no long-lived state.

        Safe to run from more than one place at once, which matters as soon as
        there is more than one host: each row is *claimed* with a conditional
        write before anything is sent, and a sweeper that loses the race skips
        the row. Without that, two schedulers -- or one that overruns its own
        interval -- send every reminder twice.

        Claiming before delivering makes this at-most-once: a process killed
        between the claim and the send drops that reminder. The alternative,
        sending first, duplicates reminders whenever a delivery succeeds and
        the process dies before recording it. For something that emails people,
        a rare miss beats a rare duplicate.
        """
        if self.provider is None:
            return 0
        query = ListQuery(
            filter=and_(
                Condition("sent_at", Op.IS_NULL),
                Condition("due_at", Op.NOT_NULL),
                Condition("due_at", Op.LTE, utcnow()),
            ),
            sort=(Sort("due_at"),),
            page_size=limit,
            with_total=False,
        )
        page = await self.provider.list(query, ctx or Ctx.system())

        system = ctx or Ctx.system()
        delivered = 0
        for record in page.items:
            if not await self._claim(record, system):
                continue
            deliveries = await self._deliver(_from_record(record))
            await self.provider.update(
                record.pk, {"delivery": summarise(deliveries)}, system
            )
            delivered += 1
        return delivered

    async def _claim(self, record: Record, ctx: Ctx) -> bool:
        """Take ownership of one due notification. False if someone else has it.

        Stamping ``sent_at`` is the claim: the same column the query filters on,
        so a claimed row drops out of every other sweeper's result set.
        """
        assert self.provider is not None
        try:
            result = await self.provider.update_if(
                record.pk, {"sent_at": utcnow()}, {"sent_at": None}, ctx
            )
        except UnsupportedOperation:
            # A backend that cannot write conditionally cannot be swept from
            # two places at once. Say so, once, and carry on: a single
            # scheduler is a perfectly good arrangement, and silently
            # double-sending is not.
            if not self._warned_about_claiming:
                self._warned_about_claiming = True
                log.warning(
                    "notifications provider cannot claim rows atomically; run "
                    "`crm notify-due` from exactly one scheduler, or reminders "
                    "will be delivered once per sweeper"
                )
            await self.provider.update(record.pk, {"sent_at": utcnow()}, ctx)
            return True
        return result is not None

    async def health(self) -> list[tuple[str, bool, str]]:
        results = []
        for channel in self.channels:
            try:
                healthy, detail = await channel.health()
            except Exception as exc:
                healthy, detail = False, f"{type(exc).__name__}: {exc}"
            results.append((channel.name, healthy, detail))
        return results

    def __repr__(self) -> str:
        return f"<Notifier channels={','.join(self.channel_names) or 'none'}>"


def _from_record(record: Record) -> Notification:
    """Rebuild a notification from its stored row, for delayed delivery.

    ``due_at`` is deliberately not carried over: it has already come due by the
    time this runs, and re-attaching it would make the rebuilt notification
    look scheduled again.
    """
    channels = str(record.get("channels") or "")
    return Notification(
        recipient=str(record.get("recipient", "")),
        title=str(record.get("title", "")),
        body=str(record.get("body") or ""),
        kind=str(record.get("kind") or Kind.INFO),
        priority=str(record.get("priority") or Priority.NORMAL),
        resource=str(record.get("resource") or ""),
        record_id=str(record.get("record_id") or ""),
        url=str(record.get("url") or ""),
        actor=str(record.get("actor") or ""),
        channels=tuple(c for c in channels.split(",") if c),
    )


#: The application's notifier, bound during startup.
notifier = Notifier()
