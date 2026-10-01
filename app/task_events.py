"""Tasks raised by other systems, as events.

Other systems know the moment a company needs moderating or a job
is waiting for its prepayment. They say so here -- from a transactional outbox
on their side, so the message survives a restart and is retried -- and this
makes the task. There is no polling: nothing in this application goes looking
for work.

Two events, both idempotent on ``key``, because an outbox delivers at least
once and a retry after a lost response must not raise the work twice:

``raise``    a task for this key unless one is already open;
``resolve``  close the open task for this key, if any.

Who is told, and when, is not decided here. Creating an unassigned task and
closing one both go through the tasks provider, whose
:class:`app.tasks.AnnouncingProvider` does the announcing and the withdrawing.
"""

from __future__ import annotations

from typing import Any

from app import tasks as task_service
from app.core.clock import utcnow
from app.core.errors import ValidationFailed
from app.core.query import Condition, ListQuery, Op, and_, or_
from app.core.results import Ctx, Record

EVENT_TYPES = ("raise", "resolve")
PRIORITIES = ("low", "normal", "high", "urgent")


def clean(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate an event, returning only what is used."""
    errors: dict[str, str] = {}
    kind = str(payload.get("type") or "")
    if kind not in EVENT_TYPES:
        errors["type"] = f"must be one of {', '.join(EVENT_TYPES)}"
    key = str(payload.get("key") or "").strip()
    if not key or len(key) > 160:
        errors["key"] = "is required, at most 160 characters"
    title = str(payload.get("title") or "").strip()
    if kind == "raise" and (not title or len(title) > 200):
        errors["title"] = "is required, at most 200 characters"
    priority = str(payload.get("priority") or "normal")
    if priority not in PRIORITIES:
        errors["priority"] = f"must be one of {', '.join(PRIORITIES)}"
    if errors:
        raise ValidationFailed(errors)
    return {
        "type": kind,
        "key": key,
        "title": title,
        "body": str(payload.get("body") or "").strip() or None,
        "resource": str(payload.get("resource") or "").strip()[:60] or None,
        "record_id": str(payload.get("record_id") or "").strip()[:60] or None,
        "priority": priority,
    }


async def _open_for(tasks: Any, key: str, ctx: Ctx) -> list[Record]:
    page = await tasks.provider.list(
        ListQuery(
            filter=and_(
                Condition("source_key", Op.EQ, key),
                or_(*[Condition("state", Op.EQ, s) for s in task_service.OPEN_STATES]),
            ),
            page_size=10,
            with_total=False,
        ),
        ctx,
    )
    return list(page.items)


async def apply(registry: Any, payload: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """Handle one event. Returns what happened, for the caller's logs."""
    tasks = task_service.resource_for(registry)
    if tasks is None:
        raise ValidationFailed({"tasks": "this deployment has no tasks resource"})
    event = clean(payload)
    existing = await _open_for(tasks, event["key"], ctx)

    if event["type"] == "resolve":
        for task in existing:
            await tasks.provider.update(
                task.pk,
                {"state": "done", "done_at": utcnow(), "done_by": ctx.identity.email or ctx.identity.subject},
                ctx,
            )
        return {"status": "resolved" if existing else "noop", "count": len(existing)}

    if existing:
        return {"status": "duplicate", "id": existing[0].pk}
    result = await tasks.provider.create(
        {
            "title": event["title"],
            "body": event["body"],
            "resource": event["resource"],
            "record_id": event["record_id"],
            "state": "open",
            "priority": event["priority"],
            "source_key": event["key"],
        },
        ctx,
    )
    if not result.ok:
        # Most likely the unique index: a concurrent delivery of this same key
        # got there first. Which is exactly "duplicate".
        again = await _open_for(tasks, event["key"], ctx)
        if again:
            return {"status": "duplicate", "id": again[0].pk}
        raise ValidationFailed(result.errors or {"task": result.message or "could not be created"})
    return {"status": "created", "id": result.record.pk if result.record else None}
