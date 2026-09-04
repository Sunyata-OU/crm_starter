"""The jobs that ship.

One, and it is the one the scaling notes named as the seam: notification
delivery. Everything upstream of it is unchanged -- the notification is still
stored first, the channels are still the channels -- but where a delivery used
to be a task on this worker's event loop, it can now be a row that survives the
worker.

A module adds its own the same way:

    from app.jobs import Retry, register

    @register("report.export")
    async def export(job):
        ...
"""

from __future__ import annotations

import logging

from app.core.results import Ctx
from app.jobs.base import Cancelled, Job, Retry, register

log = logging.getLogger("crm.jobs")

#: The kind under which a queued notification delivery is enqueued.
NOTIFY_DELIVER = "notify.deliver"


@register(NOTIFY_DELIVER)
async def deliver_notification(job: Job) -> None:
    """Send one stored notification through the channels.

    The payload carries the notification's row id rather than its contents, on
    purpose. A job is a pointer to work, not a copy of it: re-reading the row
    means a notification dismissed or amended between enqueueing and delivery
    is handled as it now is, and it keeps the queue from becoming a second
    place the same data lives.

    Retries are asked for only where retrying can help. A channel reports
    whether its failure was transient -- a timeout or a 503, as against a
    rejected address -- and only the transient ones come back here as
    :class:`Retry`.
    """
    from app.notify import notifier
    from app.notify.channels import summarise
    from app.notify.service import _from_record

    if notifier.provider is None:
        raise Retry("the notification store is not bound yet")

    pk = job.payload.get("id")
    if pk is None:
        raise ValueError("a notify.deliver job needs the notification's id")

    ctx = Ctx.system()
    record = await notifier.provider.get(pk, ctx)
    if record is None:
        # Deleted between enqueueing and delivery. Not a failure worth
        # reporting: there is simply nothing to send any more.
        raise Cancelled(f"notification {pk} no longer exists")
    if record.get("sent_at"):
        raise Cancelled(f"notification {pk} has already been delivered")

    deliveries = await notifier.deliver_now(_from_record(record))
    await notifier.record_delivery(record, deliveries, ctx)

    transient = [d for d in deliveries if not d.ok and d.transient]
    if transient and len(transient) == len([d for d in deliveries if not d.ok]):
        # Every failure was transient and none succeeded outright, so the whole
        # delivery is worth another attempt. A partial success is not retried:
        # doing so would send the channels that worked a second copy.
        succeeded = [d for d in deliveries if d.ok]
        if not succeeded:
            raise Retry(summarise(transient))
