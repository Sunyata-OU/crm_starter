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
from app.core.query import Condition, ListQuery, Op, Sort, SortDir, and_, or_
from app.core.results import Ctx, Record
from app.notify.base import Kind, Notification, Priority

log = logging.getLogger("crm.tasks")

#: The resource tasks are stored through.
RESOURCE = "tasks"

#: States a task is still somebody's problem in.
OPEN_STATES = ("open", "doing", "blocked")

#: What ``notified_assignee`` holds once a task nobody owns has been announced
#: to everybody. It is not an address, so the moment a real assignee appears
#: the hand-over comparison sees a change and tells them -- and that change is
#: also what tells the sweep to withdraw the announcement from everyone else.
ANNOUNCED = "*"

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


def announcement(task: Record | dict[str, Any], watchers: Sequence[str]) -> list[Notification]:
    """"There is new work and nobody is on it", once, to everybody who could take it.

    Unlike the unassigned alert this does not wait for a due date: a task
    nobody owns is news the moment it exists. It is addressed to the task
    itself, not the record it concerns, so it can be found again to be
    withdrawn -- ``url`` still goes to the record, which is what the reader
    wants to look at.
    """
    if str(task.get("assignee") or "").strip() or str(task.get("notified_assignee") or "").strip():
        return []
    creator = str(task.get("created_by") or "")
    return [
        Notification(
            recipient=watcher,
            title=f"New: {task.get('title')}",
            body=_describe(task),
            kind=Kind.INFO,
            priority=_priority(task),
            url=link(task),
            resource=RESOURCE,
            record_id=str(task.get("id")),
            actor=creator,
        )
        for watcher in watchers
    ]


async def withdraw(task: Record | dict[str, Any], ctx: Ctx) -> int:
    """Take a task's announcement off everybody's bell.

    Marked read rather than deleted: the history of who was told what is worth
    keeping, and an unread count that drops is all anybody needs to see.
    """
    from app.notify import notifier

    if notifier.provider is None:
        return 0
    page = await notifier.provider.list(
        ListQuery(
            filter=and_(
                Condition("resource", Op.EQ, RESOURCE),
                Condition("record_id", Op.EQ, str(task.get("id"))),
                Condition("kind", Op.EQ, str(Kind.INFO)),
                Condition("read_at", Op.IS_NULL, None),
            ),
            page_size=500,
            with_total=False,
        ),
        ctx,
    )
    now = utcnow()
    for row in page.items:
        await notifier.provider.update(row.pk, {"read_at": now}, ctx)
    return len(page.items)


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
            if str(task.get("notified_assignee") or "") == ANNOUNCED:
                await withdraw(task, ctx)  # assigned behind the provider's back
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


async def everyone(registry: Any, ctx: Ctx) -> list[str]:
    """Every back-office user: the people who could take a task nobody owns."""
    return list(await _effective_watchers(registry, ctx, ()))


class AnnouncingProvider:
    """Tells people about a task on whichever path changed it.

    Wraps the tasks provider (see ``Resource.provider_wrap``), so the form, the
    "Assign to me" action, an inline edit and the events endpoint all behave
    the same, with no sweep involved. A task nobody owns is announced to
    everybody once and stays in their bell; the moment it has an assignee the
    announcement is withdrawn from everyone and only the assignee is told; a
    finished or cancelled task is withdrawn for good.

    The decision is made from the record's *state* after the write, not from a
    diff, so it is idempotent: settling a task that is already settled does
    nothing, and ``notified_assignee`` records what was said. Bookkeeping goes
    straight to the wrapped provider, so it cannot recurse.
    """

    #: Only writes that touch one of these can change what should be said.
    WATCHED = frozenset({"assignee", "state"})

    def __init__(self, inner: Any, registry: Any) -> None:
        self.inner = inner
        self.registry = registry
        self.name = f"announcing({getattr(inner, 'name', '?')})"
        self.capabilities = inner.capabilities

    def __getattr__(self, attr: str) -> Any:
        # list / get / aggregate / delete / warm / health pass straight through.
        return getattr(self.inner, attr)

    async def create(self, data: dict[str, Any], ctx: Ctx) -> Any:
        result = await self.inner.create(data, ctx)
        await self._settle(result, ctx)
        return result

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> Any:
        result = await self.inner.update(pk, data, ctx)
        if self.WATCHED & set(data):
            await self._settle(result, ctx)
        return result

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> Any:
        result = await self.inner.update_if(pk, data, expect, ctx)
        if result is not None and self.WATCHED & set(data):
            await self._settle(result, ctx)
        return result

    async def _settle(self, result: Any, ctx: Ctx) -> None:
        task = getattr(result, "record", None)
        if task is None:
            return
        try:
            await self._announce(task, ctx)
        except Exception:
            # Telling people is never allowed to fail the write it follows.
            log.exception("could not settle notifications for task %s", task.get("id"))

    async def _announce(self, task: Record, ctx: Ctx) -> None:
        from app.notify import notifier

        told = str(task.get("notified_assignee") or "").strip()
        assignee = str(task.get("assignee") or "").strip()

        if str(task.get("state")) not in OPEN_STATES:
            if told == ANNOUNCED:
                await withdraw(task, ctx)
            return

        if assignee:
            if assignee == told:
                return
            if told == ANNOUNCED:
                await withdraw(task, ctx)
            note = handover(task)
            if note is not None:
                await notifier.send(note, ctx)
            await self.inner.update(task.pk, {"notified_assignee": assignee}, ctx)
            return

        if told:
            return  # already announced, and nobody has taken it yet
        people = await everyone(self.registry, ctx)
        if not people:
            # Leave it unannounced rather than recording a telling that reached
            # nobody: the next write to this task tries again.
            log.warning("task %s is unassigned but nobody resolved to tell", task.get("id"))
            return
        for item in announcement(task, people):
            await notifier.send(item, ctx)
        await self.inner.update(task.pk, {"notified_assignee": ANNOUNCED}, ctx)
