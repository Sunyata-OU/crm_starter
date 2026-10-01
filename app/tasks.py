"""The back office's work queue, and the sweep that watches it.

Not to be confused with :mod:`app.jobs`, which is the *machine's* queue. A job
is claimed, retried and abandoned on a timeout; none of those verbs mean
anything for something a colleague has promised to do. A task is assigned by a
person, to a person, and the only automation around it is being told about it.

The telling is a sweep rather than a hook on the write, and that is the design
decision worth defending. A task can be assigned from the form, from an inline
edit, from a bulk action or from a script, and a notification raised at each of
those places is a notification that will eventually be forgotten at one of
them. Instead the sweep compares two columns -- who the task is assigned to,
and who was last told it was theirs -- and announces the difference. Whatever
made the change, and whether or not the process that made it survived, the
person finds out.

The same shape covers due dates: ``reminded_at`` records that a warning has
gone out, so a sweep running every minute does not send sixty of them an hour.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from app.core.clock import utcnow
from app.core.query import Condition, ListQuery, Op, Sort, SortDir, or_
from app.core.results import Ctx, Record
from app.notify.base import Kind, Notification, Priority

log = logging.getLogger("crm.tasks")

#: The resource tasks are stored through.
RESOURCE = "tasks"

#: States a task is still somebody's problem in.
OPEN_STATES = ("open", "doing", "blocked")

#: How far ahead a due date is worth a warning. A day: long enough to do
#: something about it, short enough that the warning is still about today.
DUE_WINDOW = timedelta(hours=24)


def resource_for(registry: Any) -> Any | None:
    """The tasks resource, or ``None`` where this deployment has no database."""
    if registry is not None and registry.has_resource(RESOURCE):
        return registry.resource(RESOURCE)
    return None


def link(task: Record | dict[str, Any]) -> str:
    """Where a notification about this task should send somebody.

    The record it concerns when it names one -- which is what the reader
    actually wants to look at -- and the task itself otherwise.
    """
    resource = str(task.get("resource") or "")
    record_id = str(task.get("record_id") or "")
    if resource and record_id:
        return f"/r/{resource}/{record_id}"
    return f"/r/{RESOURCE}/{task.get('id')}"


def _describe(task: Record | dict[str, Any]) -> str:
    """A one-line body for a notification about this task."""
    parts = [str(task.get("body") or "").strip()]
    due = task.get("due_at")
    if due:
        parts.append(f"Due {due}.")
    return " ".join(p for p in parts if p)[:400]


def _priority(task: Record | dict[str, Any]) -> Priority:
    return Priority.HIGH if str(task.get("priority")) in ("high", "urgent") else Priority.NORMAL


async def open_tasks(tasks: Any, ctx: Ctx, *, limit: int = 500) -> list[Record]:
    """Every task still owed to somebody, soonest due first."""
    query = ListQuery(
        filter=or_(*[Condition("state", Op.EQ, state) for state in OPEN_STATES]),
        sort=(Sort("due_at", SortDir.ASC),),
        page_size=limit,
        with_total=False,
    )
    page = await tasks.provider.list(query, ctx)
    return list(page.items)


def handover(task: Record | dict[str, Any]) -> Notification | None:
    """"This is yours now", when the assignee is not who was last told."""
    assignee = str(task.get("assignee") or "").strip()
    told = str(task.get("notified_assignee") or "").strip()
    if not assignee or assignee == told:
        return None
    return Notification(
        recipient=assignee,
        title=f"Assigned to you: {task.get('title')}",
        body=_describe(task),
        kind=Kind.ASSIGNED,
        priority=_priority(task),
        url=link(task),
        resource=str(task.get("resource") or ""),
        record_id=str(task.get("record_id") or ""),
        actor=str(task.get("created_by") or ""),
    )


def reminder(task: Record | dict[str, Any], *, now=None, window: timedelta = DUE_WINDOW):
    """"This is due", once, for a task whose date has nearly or already passed."""
    due = task.get("due_at")
    if due is None or task.get("reminded_at"):
        return None
    now = now or utcnow()
    if hasattr(due, "tzinfo") and due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)
    try:
        soon = due <= now + window
    except TypeError:  # a driver handed back something that is not an instant
        log.warning("task %s has an unusable due date %r", task.get("id"), due)
        return None
    if not soon:
        return None

    overdue = due <= now
    recipient = str(task.get("assignee") or "").strip()
    if not recipient:
        return None
    return Notification(
        recipient=recipient,
        title=("Overdue: " if overdue else "Due soon: ") + str(task.get("title")),
        body=_describe(task),
        kind=Kind.OVERDUE if overdue else Kind.DUE,
        priority=Priority.HIGH if overdue else _priority(task),
        url=link(task),
        resource=str(task.get("resource") or ""),
        record_id=str(task.get("record_id") or ""),
    )


def unassigned_alerts(
    tasks: Sequence[Record], watchers: Sequence[str], *, now=None, window: timedelta = DUE_WINDOW
) -> list[Notification]:
    """Tell the watchers about work that is due and belongs to nobody.

    The one case the sweep cannot address to the person responsible, because
    there is not one. Whoever is named in ``CRM_TASK_WATCHERS`` gets it -- and
    where nobody is named, the task simply waits, which is the honest outcome
    for a deployment that has not said who minds the queue.
    """
    now = now or utcnow()
    out: list[Notification] = []
    for task in tasks:
        out.extend(unassigned_alert(task, watchers, now=now, window=window))
    return out


def unassigned_alert(
    task: Record | dict[str, Any],
    watchers: Sequence[str],
    *,
    now=None,
    window: timedelta = DUE_WINDOW,
) -> list[Notification]:
    """The same, for one task: one notification per watcher, or none."""
    if not watchers:
        return []
    if str(task.get("assignee") or "").strip() or task.get("reminded_at"):
        return []
    due = task.get("due_at")
    if due is None:
        return []
    now = now or utcnow()
    if hasattr(due, "tzinfo") and due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)
    try:
        if due > now + window:
            return []
    except TypeError:
        log.warning("task %s has an unusable due date %r", task.get("id"), due)
        return []
    return [
        Notification(
            recipient=watcher,
            title=f"Nobody is on: {task.get('title')}",
            body=_describe(task),
            kind=Kind.OVERDUE if due <= now else Kind.DUE,
            priority=Priority.HIGH,
            url=link(task),
            resource=str(task.get("resource") or ""),
            record_id=str(task.get("record_id") or ""),
        )
        for watcher in watchers
    ]


async def _effective_watchers(registry: Any, ctx: Ctx, watchers: Sequence[str]) -> Sequence[str]:
    """``CRM_TASK_WATCHERS`` where an operator named one, otherwise everybody.

    A task nobody is on should reach whoever *could* pick it up, not sit
    waiting for an address somebody remembered to configure. Where a staff
    directory is declared (``Registry.staff_directory``) it is the default; an explicit
    ``CRM_TASK_WATCHERS`` still overrides it for a deployment that wants a
    narrower list. Only a deployment with neither is left with nobody told.
    """
    if watchers:
        return watchers
    from app.notes import staff_directory

    people = await staff_directory(registry, ctx)
    if not people:
        return watchers
    return [p["email"] for p in people if p.get("email")]


async def sweep(
    registry: Any,
    ctx: Ctx | None = None,
    *,
    watchers: Sequence[str] = (),
    window: timedelta = DUE_WINDOW,
) -> dict[str, int]:
    """Announce hand-overs and due dates. Returns what it did.

    Bookkeeping is written *after* the notification is stored, so a sweep
    interrupted half way repeats itself rather than swallowing the one
    notification nobody received.
    """
    from app.notify import notifier

    tasks = resource_for(registry)
    if tasks is None:
        return {"assigned": 0, "due": 0, "unassigned": 0}
    ctx = ctx or Ctx.system()
    watchers = await _effective_watchers(registry, ctx, watchers)
    now = utcnow()
    counts = {"assigned": 0, "due": 0, "unassigned": 0}

    rows = await open_tasks(tasks, ctx)
    for task in rows:
        note = handover(task)
        if note is not None:
            if await notifier.send(note, ctx) is not None:
                counts["assigned"] += 1
            await tasks.provider.update(
                task.pk, {"notified_assignee": note.recipient}, ctx
            )

        due = reminder(task, now=now, window=window)
        if due is not None:
            if await notifier.send(due, ctx) is not None:
                counts["due"] += 1
            await tasks.provider.update(task.pk, {"reminded_at": now}, ctx)
            continue

        # Same bookkeeping column, because it answers the same question: has
        # anybody been warned that this one is nearly late? Only reached when
        # there was no assignee to warn.
        alerts = unassigned_alert(task, watchers, now=now, window=window)
        if alerts:
            for alert in alerts:
                if await notifier.send(alert, ctx) is not None:
                    counts["unassigned"] += 1
            await tasks.provider.update(task.pk, {"reminded_at": now}, ctx)

    if any(counts.values()):
        log.info(
            "task sweep: %d assigned, %d due, %d unassigned",
            counts["assigned"], counts["due"], counts["unassigned"],
        )
    return counts
