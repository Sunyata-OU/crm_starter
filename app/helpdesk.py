"""Support tickets: the pure logic a mailbox needs, without a mailbox.

This branch has no IMAP client and no network code anywhere. It is the core --
the resource, the board, the reply box, the sweep -- and it is deliberately
built so that a second branch can add inbound mail without touching any of it,
by calling into the handful of functions below.

That is the whole reason this module exists as something other than a private
helper inside ``modules/core_helpdesk``: reference generation, subject-tag
parsing, deciding which ticket an inbound message threads onto, and dedupe are
all *decisions*, not I/O, and a decision is worth being able to test and reuse
without a mail server. The ingest branch is expected to call:

* :func:`match_reference` -- turn the subject of a fetched email into the
  ticket reference to look up, if the subject carries one.
* :func:`thread_ids` -- the Message-IDs (In-Reply-To, then References, oldest
  first) worth checking against ``ticket_messages.message_id`` when a subject
  tag alone did not resolve a ticket, or was stripped by the sending client.
* :func:`is_duplicate` -- whether a fetched message has already been stored,
  once the ingest branch has looked its Message-ID up.
* :func:`new_ticket_fields` and :func:`new_message_fields` -- the row to
  insert for a ticket opened by an inbound email, and for the message itself.
* :func:`next_thread_anchor` -- what ``tickets.thread_message_id`` becomes
  after a message, in either direction, is added.

Everything else here -- :func:`reference_for`, :func:`tag_subject`,
:func:`reply_headers`, :func:`new_message_id` -- is exercised by this branch's
own reply box and included because the ingest branch will want the same
functions rather than a second implementation of "how a Message-ID is shaped".
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import timedelta
from typing import Any

from app.core.clock import utcnow
from app.core.query import Condition, ListQuery, Op, Sort, SortDir, or_
from app.core.results import Ctx, Record
from app.notify.base import Kind, Notification, Priority

log = logging.getLogger("crm.helpdesk")

#: The resource tickets are stored through. Absent in a deployment with no
#: database of its own -- the case `resource_for` exists to survive.
RESOURCE = "tickets"

#: How long an open, assigned ticket may sit before the sweep decides it is
#: worth mentioning again. The CLI's default; `crm helpdesk-sweep` takes this
#: from `Settings.helpdesk_stale_hours` instead so a deployment can tune it
#: without a code change.
STALE_WINDOW = timedelta(hours=48)

#: The prefix a ticket's human reference is built from. Not a setting: it is
#: baked into every subject tag and Message-ID this application has ever sent,
#: so changing it retroactively breaks threading on every open ticket.
REFERENCE_PREFIX = "T-"

#: A reference inside square brackets, wherever it sits in a subject line --
#: a reply usually has "Re: " and a signature's worth of forwarded text ahead
#: of it by the third round trip.
_SUBJECT_TAG = re.compile(r"\[(T-\d+)\]")

#: States a ticket is still somebody's problem in. Mirrors `app.tasks`'
#: ``OPEN_STATES``: what the sweep and the board's "open" grouping both mean
#: by "still open".
OPEN_STATES = ("new", "open", "pending")

#: Directions a message on a ticket can have travelled.
IN = "in"
OUT = "out"


def reference_for(pk: Any, *, prefix: str = REFERENCE_PREFIX) -> str:
    """The human reference for a ticket whose primary key is ``pk``.

    A pure function of the key, which is what lets it be called from the
    provider that assigns it, from a template that displays it, and from the
    ingest branch's tests, and always agree.
    """
    return f"{prefix}{pk}"


def match_reference(subject: str, *, prefix: str = REFERENCE_PREFIX) -> str | None:
    """The ticket reference named in a subject line's ``[T-1042]`` tag, if any."""
    match = _SUBJECT_TAG.search(subject or "")
    if match is None:
        return None
    ref = match.group(1)
    return ref if ref.startswith(prefix) else None


def tag_subject(subject: str, reference: str) -> str:
    """Prefix a subject with its ticket's tag, unless it already carries one.

    Idempotent on purpose: a reply to a reply still has last time's tag
    sitting in the subject line (most mail clients preserve it), and adding a
    second one would make the next reply's :func:`match_reference` ambiguous
    about which is current.
    """
    subject = (subject or "").strip()
    if match_reference(subject) == reference:
        return subject
    tag = f"[{reference}]"
    return f"{tag} {subject}" if subject else tag


def thread_ids(in_reply_to: str, references: str) -> list[str]:
    """Message-IDs worth checking against a stored message, oldest first.

    ``References`` is a whitespace-separated chain, oldest first per RFC 5322;
    ``In-Reply-To`` is usually the same value as the chain's most recent
    entry but is checked too because a client that only sets one of the two
    headers is not rare. Order does not matter to the caller -- any match
    identifies the same ticket -- but a stable, de-duplicated list makes the
    query the ingest branch runs from this predictable to test.
    """
    ids: list[str] = []
    for raw in (*(references or "").split(), in_reply_to or ""):
        candidate = raw.strip()
        if candidate and candidate not in ids:
            ids.append(candidate)
    return ids


def is_duplicate(message_id: str, known: Collection[str]) -> bool:
    """Whether a fetched message has already been stored.

    A message with no Message-ID at all is never treated as a duplicate of
    anything -- it is the ingest branch's job to decide what to do with a
    message a sender did not identify, and silently dropping it here would be
    a decision this module has no business making.
    """
    return bool(message_id) and message_id in known


def new_message_id(*, domain: str = "helpdesk.local") -> str:
    """A Message-ID for something this application sends.

    Random rather than derived from the ticket, deliberately: a Message-ID
    that repeated (two replies on the same ticket, say) would make every mail
    client that has ever seen the first one refuse to show the second as new.
    """
    return f"<{uuid.uuid4().hex}@{domain}>"


def reply_headers(
    last: Mapping[str, Any] | None, *, domain: str = "helpdesk.local"
) -> dict[str, str]:
    """Headers for an outbound reply, threaded onto the last message on the ticket.

    ``last`` is the most recent ``ticket_messages`` row -- inbound or
    outbound, whichever happened most recently -- or ``None`` for a ticket's
    first message, which threads onto nothing. In-Reply-To is that message's
    own Message-ID; References is its References with that Message-ID
    appended, so the chain grows by one link per round trip the way RFC 5322
    describes, rather than resetting to just the immediate parent.
    """
    message_id = new_message_id(domain=domain)
    if last is None:
        return {"message_id": message_id, "in_reply_to": "", "references": ""}
    prior_id = str(last.get("message_id") or "").strip()
    prior_refs = str(last.get("references") or "").strip()
    chain = f"{prior_refs} {prior_id}".strip() if prior_id else prior_refs
    return {"message_id": message_id, "in_reply_to": prior_id, "references": chain}


def next_thread_anchor(current: str | None, message_id: str | None) -> str | None:
    """What ``tickets.thread_message_id`` becomes after a message is added.

    Whichever direction added a message last owns the anchor: a reply we sent
    is exactly as good a thing to reply-thread onto as one the customer sent,
    and using "most recent" rather than "most recent inbound" means a ticket
    with several outbound messages in a row (a staff member adding context
    before the customer has answered) still threads correctly.
    """
    return message_id or current


def new_ticket_fields(
    *, subject: str, requester_name: str, requester_email: str
) -> dict[str, Any]:
    """Columns for a ticket opened by an inbound email.

    Returns a plain dict rather than creating anything -- this module has no
    provider and no database -- for the ingest branch to pass to
    ``tickets`` resource's ``provider.create``. ``source`` is hard-coded to
    ``"email"`` because that is the one fact only that branch's call site
    actually knows to be true; ``reference`` is left unset, exactly as the
    staff-raise form leaves it, because :class:`app.providers.sequence.
    SequencingProvider` fills it in from the primary key the insert assigns.
    """
    return {
        "subject": subject.strip()[:200],
        "requester_name": requester_name.strip()[:160],
        "requester_email": requester_email.strip()[:160],
        "state": "new",
        "source": "email",
    }


def new_message_fields(
    *,
    ticket_id: Any,
    direction: str,
    author: str,
    body: str,
    message_id: str = "",
    in_reply_to: str = "",
    references: str = "",
    sent_at: Any = None,
    received_at: Any = None,
) -> dict[str, Any]:
    """Columns for one row of a ticket's customer-visible thread.

    Shared by this branch's reply action and the ingest branch's inbound
    handler, so both write exactly the same shape and the thread never has to
    guess which columns a given direction fills in.
    """
    return {
        "ticket_id": ticket_id,
        "direction": direction,
        "author": author[:160] if author else None,
        "body": body,
        "message_id": message_id or None,
        "in_reply_to": in_reply_to or None,
        "references": references or None,
        "sent_at": sent_at,
        "received_at": received_at,
    }


def unassigned(tickets: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Open tickets nobody is on."""
    return [t for t in tickets if str(t.get("state")) in OPEN_STATES
            and not str(t.get("assignee") or "").strip()]


# -- the sweep ---------------------------------------------------------------
#
# Same shape as `app.tasks`, and the same reason: a ticket can be assigned
# from the form, an inline edit, a bulk action or (eventually) the ingest
# branch auto-opening one, and a notification raised at each of those places
# is one that will eventually be forgotten at one of them. The sweep compares
# columns instead and announces the difference, so whatever changed the
# ticket -- and whether or not the process that changed it survived -- the
# right person finds out, exactly once.


def resource_for(registry: Any) -> Any | None:
    """The tickets resource, or ``None`` where this deployment has no database."""
    if registry is not None and registry.has_resource(RESOURCE):
        return registry.resource(RESOURCE)
    return None


def link(ticket: Record | Mapping[str, Any]) -> str:
    return f"/r/{RESOURCE}/{ticket.get('id')}"


def _priority(ticket: Record | Mapping[str, Any]) -> Priority:
    return Priority.HIGH if str(ticket.get("priority")) in ("high", "urgent") else Priority.NORMAL


def _describe(ticket: Record | Mapping[str, Any]) -> str:
    return f"{ticket.get('requester_name') or ticket.get('requester_email') or 'Someone'} — " \
           f"{ticket.get('subject')}"


async def open_tickets(tickets: Any, ctx: Ctx, *, limit: int = 500) -> list[Record]:
    """Every ticket still open, oldest first -- the board's own ordering."""
    query = ListQuery(
        filter=or_(*[Condition("state", Op.EQ, state) for state in OPEN_STATES]),
        sort=(Sort("created_at", SortDir.ASC),),
        page_size=limit,
        with_total=False,
    )
    page = await tickets.provider.list(query, ctx)
    return list(page.items)


def handover(ticket: Record | Mapping[str, Any]) -> Notification | None:
    """"This is yours now", when the assignee is not who was last told."""
    assignee = str(ticket.get("assignee") or "").strip()
    told = str(ticket.get("notified_assignee") or "").strip()
    if not assignee or assignee == told:
        return None
    return Notification(
        recipient=assignee,
        title=f"Assigned to you: {ticket.get('reference') or ticket.get('subject')}",
        body=_describe(ticket),
        kind=Kind.ASSIGNED,
        priority=_priority(ticket),
        url=link(ticket),
        resource=RESOURCE,
        record_id=str(ticket.get("id")),
        actor=str(ticket.get("created_by") or ""),
    )


def unassigned_alert(
    ticket: Record | Mapping[str, Any], watchers: Sequence[str]
) -> list[Notification]:
    """"Nobody is on this", the moment a ticket is seen with no assignee.

    Unlike a task's unassigned alert, this is not gated on a due date -- a
    ticket has none -- so it fires the first time the sweep sees the ticket
    unassigned, and `reminded_at` stops it firing on every later pass.
    """
    if not watchers or str(ticket.get("assignee") or "").strip() or ticket.get("reminded_at"):
        return []
    return [
        Notification(
            recipient=watcher,
            title=f"Nobody is on: {ticket.get('reference') or ticket.get('subject')}",
            body=_describe(ticket),
            kind=Kind.ASSIGNED,
            priority=Priority.HIGH,
            url=link(ticket),
            resource=RESOURCE,
            record_id=str(ticket.get("id")),
        )
        for watcher in watchers
    ]


def stale_alert(
    ticket: Record | Mapping[str, Any],
    watchers: Sequence[str],
    *,
    now=None,
    window: timedelta = STALE_WINDOW,
) -> list[Notification]:
    """"This has been open a while", once, for an assigned ticket ageing past ``window``.

    Reaches the assignee as well as the watchers -- unlike the unassigned
    alert, there is somebody responsible here, and they are the one who most
    needs telling their ticket has gone quiet.
    """
    if ticket.get("reminded_at"):
        return []
    assignee = str(ticket.get("assignee") or "").strip()
    if not assignee:
        return []
    created = ticket.get("created_at")
    if created is None:
        return []
    now = now or utcnow()
    if hasattr(created, "tzinfo") and created.tzinfo is None:
        created = created.replace(tzinfo=now.tzinfo)
    try:
        aged = now - created >= window
    except TypeError:
        log.warning("ticket %s has an unusable created_at %r", ticket.get("id"), created)
        return []
    if not aged:
        return []
    recipients = [assignee, *[w for w in watchers if w != assignee]]
    return [
        Notification(
            recipient=recipient,
            title=f"Ageing: {ticket.get('reference') or ticket.get('subject')}",
            body=_describe(ticket),
            kind=Kind.OVERDUE,
            priority=Priority.HIGH,
            url=link(ticket),
            resource=RESOURCE,
            record_id=str(ticket.get("id")),
        )
        for recipient in recipients
    ]


async def sweep(
    registry: Any,
    ctx: Ctx | None = None,
    *,
    watchers: Sequence[str] = (),
    window: timedelta = STALE_WINDOW,
) -> dict[str, int]:
    """Announce hand-overs, new unassigned tickets, and tickets ageing quietly.

    Bookkeeping is written *after* the notification is stored, so a sweep
    interrupted half way repeats itself rather than swallowing the one
    notification nobody received -- the same ordering `app.tasks.sweep` uses,
    for the same reason.
    """
    from app.notify import notifier

    tickets = resource_for(registry)
    if tickets is None:
        return {"assigned": 0, "unassigned": 0, "stale": 0}
    ctx = ctx or Ctx.system()
    now = utcnow()
    counts = {"assigned": 0, "unassigned": 0, "stale": 0}

    rows = await open_tickets(tickets, ctx)
    for ticket in rows:
        note = handover(ticket)
        if note is not None:
            if await notifier.send(note, ctx) is not None:
                counts["assigned"] += 1
            await tickets.provider.update(
                ticket.pk, {"notified_assignee": note.recipient}, ctx
            )

        alerts = unassigned_alert(ticket, watchers)
        if alerts:
            for alert in alerts:
                if await notifier.send(alert, ctx) is not None:
                    counts["unassigned"] += 1
            await tickets.provider.update(ticket.pk, {"reminded_at": now}, ctx)
            continue

        # Only reached once a ticket has an assignee (the branch above would
        # otherwise have taken it and `continue`d), so this is purely the
        # ageing check.
        stales = stale_alert(ticket, watchers, now=now, window=window)
        if stales:
            for alert in stales:
                if await notifier.send(alert, ctx) is not None:
                    counts["stale"] += 1
            await tickets.provider.update(ticket.pk, {"reminded_at": now}, ctx)

    if any(counts.values()):
        log.info(
            "helpdesk sweep: %d assigned, %d unassigned, %d ageing",
            counts["assigned"], counts["unassigned"], counts["stale"],
        )
    return counts
