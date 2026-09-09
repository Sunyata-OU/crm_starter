"""Helpdesk: support tickets.

Loads as an opt-in module (``CRM_MODULES=core_helpdesk``), not as part of the
platform's own default set the way ``core_tasks`` does -- a deployment that
has never needed a support inbox should not gain one by upgrading. Everything
that is not screen-and-workflow lives in :mod:`app.helpdesk`: reference
generation, the sweep, subject-tag parsing. This module is the resource, the
views, and the actions that call into it.

**Messages versus notes -- the separation that matters most.** A ticket gets
the ordinary activity panel for free, being a record with a detail view: that
panel is where "waiting on billing to confirm the refund" belongs, exactly as
it would on a company or a deal. ``ticket_messages`` is a different table for
a different purpose -- the thread that *was or can be* sent to the requester.
There is deliberately no visibility flag anywhere on it, no "internal" toggle
that starts unchecked, because a flag that can be got wrong is a flag that
will eventually be got wrong, and the failure mode here is mailing a private
remark to a customer. The way this module makes that impossible is by not
representing "private" as a value at all: a row in ``ticket_messages`` has no
sense in which it could be private, so writing a private thought means using
the notes panel, and writing to the notes panel never reaches an inbox. See
``tests/test_helpdesk.py`` for the test that pins this down.

**Staff-raised tickets.** The only creation path this branch offers: a form,
with ``source`` defaulting to ``"staff"`` at the database level, filled in
without this module's code doing anything -- there is deliberately no public
web form in v1. The ingest branch adds a second creation path, writing
``source="email"`` through :func:`app.helpdesk.new_ticket_fields`.

**Outbound replies** go through the same email channel every other
notification does (see ``app.notify``), with the threading headers and the
``[T-1042]`` subject tag set by :func:`app.helpdesk.reply_headers` and
:func:`app.helpdesk.tag_subject` so a reply lands back in this ticket's thread
even in a client that quotes nothing and strips the References header, which
is the common case for a forwarded complaint rather than a direct reply.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app import helpdesk
from app.core.query import Condition, ListQuery, Op, Sort, SortDir
from app.core.registry import Registry
from app.core.results import Ctx
from app.fields.types import (
    BackrefField,
    DateTimeField,
    EmailField,
    StatusField,
    TextAreaField,
    TextField,
)
from app.resources.actions import ActionResult, action
from app.resources.resource import Resource
from app.resources.views import (
    BoardView,
    Card,
    Column,
    DetailView,
    FormView,
    ListView,
    SearchSpec,
    Section,
)

MANIFEST = {
    "name": "core_helpdesk",
    "label": "Helpdesk",
    "description": "Support tickets: a board, a thread per ticket, and a sweep.",
    "depends": ("core_identity", "core_access"),
    "menu_groups": {"Support": 5},
    # Not loaded unless a deployment asks for it -- a support inbox is not
    # part of what every back office needs the way accounts and access
    # control are, and a stateless deployment has nowhere to put it.
    "optional": True,
}

#: new -> open -> pending (waiting on the requester) -> resolved, with closed
#: as the way out that is not "we fixed it" -- a duplicate, a withdrawn
#: request, spam.
STATES = [
    ("new", "New", "blue"),
    ("open", "Open", "amber"),
    ("pending", "Waiting on requester", "grey"),
    ("resolved", "Resolved", "green"),
    ("closed", "Closed", "grey"),
]

PRIORITIES = [
    ("low", "Low", "grey"),
    ("normal", "Normal", "blue"),
    ("high", "High", "amber"),
    ("urgent", "Urgent", "red"),
]

SOURCES = [
    ("staff", "Staff", "blue"),
    ("email", "Email", "grey"),
]


def register(registry: Registry) -> None:
    registry.add_resource(_tickets())
    registry.add_resource(_ticket_messages())


def _tickets() -> Resource:
    return Resource(
        "tickets",
        provider="db.main#tickets",
        label="Ticket",
        icon="🎫",
        menu_group="Support",
        menu_order=10,
        display_field="subject",
        default_sort=["created_at"],
        # Who raised it. Filled in on every creation path -- the staff-raise
        # form is the only one this branch has -- the same way `tasks` records
        # who raised a task.
        stamp={"created_by": "id", "created_by_name": "label"},
        # The human reference, filled in immediately after the insert that
        # reveals the primary key it is built from. See
        # `app.providers.sequence.SequencingProvider`.
        sequence=("reference", helpdesk.REFERENCE_PREFIX),
        # Deliberately no RolePolicy, matching `core_tasks`: who may see and
        # work a queue of other people's problems is exactly the kind of rule
        # an administrator should set from the permissions screen.
        fields=[
            TextField("id", in_form=False, in_list=False, in_detail=False),
            TextField("reference", label="Reference", in_form=False, readonly=True,
                      searchable=True),
            TextField("subject", required=True, searchable=True),
            TextField("requester_name", label="Requester", searchable=True),
            EmailField("requester_email", label="Email", required=True, searchable=True),
            StatusField("state", choices=STATES, in_filter=True, default="new",
                        inline_editable=True),
            StatusField("priority", choices=PRIORITIES, in_filter=True, default="normal",
                        inline_editable=True),
            # Free text, not a dropdown -- identity here comes from Keycloak
            # and there is no local account table to offer a list from, the
            # same reasoning `core_tasks.assignee` documents.
            TextField("assignee", label="Assigned to", searchable=True, in_filter=True,
                      inline_editable=True,
                      help="The address this person signs in with. Leave empty for nobody."),
            TextField("assignee_name", label="Name", in_list=False, in_form=False),
            StatusField("source", choices=SOURCES, in_form=False, in_filter=True,
                        default="staff"),
            TextField("thread_message_id", in_form=False, in_list=False, in_detail=False),
            TextField("created_by", in_form=False, in_list=False, in_detail=False),
            TextField("created_by_name", label="Raised by", in_form=False),
            DateTimeField("created_at", label="Raised", readonly=True, in_form=False),
            # The sweep's bookkeeping, visible on the record for the same
            # reason `tasks.notified_assignee` is: so somebody asking "why
            # wasn't I told" can see what the sweep already did.
            TextField("notified_assignee", label="Last told", in_form=False, in_list=False),
            DateTimeField("reminded_at", label="Reminded", in_form=False, in_list=False),
            # The customer-visible thread, oldest first -- a reply reads top
            # to bottom like the conversation it is.
            BackrefField(
                "messages", resource="ticket_messages", via="ticket_id",
                columns=("direction", "author", "body", "sent_at", "received_at"),
                order=("created_at",), limit=200, label="Conversation",
            ),
        ],
        search=SearchSpec(
            fields=("subject", "requester_name", "requester_email", "reference"),
            filters=("state", "priority", "assignee", "source"),
        ),
        actions=[take, resolve, reopen, reply],
        views=[
            ListView(
                columns=[
                    Column("reference", link=True, width="10%"),
                    Column("subject", link=True, width="28%"),
                    "state", "priority",
                    Column("assignee", label="Assigned to", width="16%"),
                    Column("requester_email", label="From"),
                    Column("created_at", label="Raised"),
                ],
                default_sort=["created_at"],
                row_actions=["take", "resolve"],
                bulk_actions=["delete"],
                empty_message="No tickets.",
            ),
            BoardView(
                group_by="state",
                card=Card(title="subject", subtitle="requester_name",
                          badges=["priority", "assignee"]),
                default_sort=["created_at"],
            ),
            FormView([
                Section("Ticket", ["subject", "requester_name", "requester_email"],
                        columns=1),
                Section("Handling", ["priority", "assignee"], columns=2),
            ]),
            DetailView(
                sections=[
                    Section("Ticket", ["subject", "requester_name", "requester_email"],
                            columns=1),
                    Section("Handling", ["state", "priority", "assignee", "source"],
                            columns=2),
                    Section("Conversation", ["messages"], columns=1),
                    Section("Record", ["created_by_name", "created_at"], columns=2),
                ],
                title_field="subject",
                subtitle_field="reference",
            ),
        ],
    )


def _ticket_messages() -> Resource:
    """The thread. Not in the menu -- it is read and written through a ticket,
    never on its own -- but a real resource, so `BackrefField` can query it
    and it carries the same audit trail as anything else.
    """
    return Resource(
        "ticket_messages",
        provider="db.main#ticket_messages",
        label="Message",
        icon="✉",
        in_menu=False,
        display_field="body",
        default_sort=["created_at"],
        # A record of what was sent, not a thing to have opinions about after
        # the fact -- the timeline panel is for discussing a ticket, this
        # table already *is* part of the ticket's own record.
        timeline=False,
        fields=[
            TextField("id", in_form=False, in_list=False, in_detail=False),
            TextField("ticket_id", in_list=False),
            StatusField("direction", choices=[("in", "Received", "blue"),
                                               ("out", "Sent", "green")]),
            TextField("author", label="From"),
            # Text, never HTML. Inbound mail is, from the moment the ingest
            # branch exists, attacker-controlled content rendered inside an
            # authenticated admin session -- so this is a plain text field,
            # autoescaped by the same template machinery as every other text
            # field, and nothing here ever marks it safe for raw HTML output.
            TextAreaField("body", rows=6),
            TextField("message_id", in_list=False, in_form=False, in_detail=False),
            TextField("in_reply_to", in_list=False, in_form=False, in_detail=False),
            TextField("references", in_list=False, in_form=False, in_detail=False),
            DateTimeField("sent_at", label="Sent"),
            DateTimeField("received_at", label="Received"),
            DateTimeField("created_at", readonly=True, in_form=False, in_list=False),
        ],
    )


@action("take", "Assign to me", icon="☚", placements=("row", "detail"))
async def take(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Put my name on this, the same one gesture `core_tasks.take` offers."""
    who = ctx.identity
    for record in records:
        await resource.provider.update(
            record.pk,
            {
                "assignee": who.email or who.subject,
                "assignee_name": who.label,
                "state": "open" if str(record.get("state")) == "new" else record.get("state"),
            },
            ctx,
        )
    return ActionResult(message=f"{len(records)} ticket(s) are yours.", level="success")


@action("resolve", "Resolve", icon="✓", placements=("row", "detail"))
async def resolve(records, ctx: Ctx, resource: Resource) -> ActionResult:
    for record in records:
        await resource.provider.update(record.pk, {"state": "resolved"}, ctx)
    return ActionResult(message=f"{len(records)} ticket(s) resolved.", level="success")


@action("reopen", "Reopen", icon="↺", placements=("detail",))
async def reopen(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Put a closed ticket back, and let the sweep mention it again.

    ``reminded_at`` is cleared along with the state, matching
    `core_tasks.reopen`: a ticket that comes back is news again, and leaving
    the flag set would spend that on the sweep pass that already ran.
    """
    for record in records:
        await resource.provider.update(
            record.pk, {"state": "open", "reminded_at": None}, ctx,
        )
    return ActionResult(message=f"{len(records)} ticket(s) reopened.", level="success")


@action(  # type: ignore[arg-type]  # a `params`-taking handler; see ParameterisedHandler
    "reply", "Reply", icon="↩", placements=("detail",), style="primary",
    prompt_fields=[TextAreaField("body", label="Reply", required=True, rows=6)],
)
async def reply(records, ctx: Ctx, resource: Resource, *, params: dict[str, Any]) -> ActionResult:
    """Send a reply, and record it as the newest message on the thread.

    Threads onto the last message on the ticket -- inbound or outbound,
    whichever happened most recently -- so a customer's mail client shows this
    in the same conversation regardless of which of us spoke last. The
    outgoing row is written *before* the send is attempted: a reply the
    customer never received but this application believes it sent is a worse
    outcome than a duplicate somebody can see and re-send, and writing first
    means the thread here always matches what this application tried to do.
    """
    from app.notify import Notification, Priority, notifier
    from app.settings import get_settings

    registry = resource.registry
    if registry is None or not registry.has_resource("ticket_messages"):
        return ActionResult(message="No message thread is available.", level="warning")
    messages = registry.resource("ticket_messages")
    settings = get_settings()
    who = ctx.identity
    body = str(params.get("body") or "").strip()
    if not body:
        return ActionResult(message="Write something first.", level="warning")

    sent = 0
    for ticket in records:
        last = await _last_message(messages, ticket.pk, ctx)
        headers = helpdesk.reply_headers(last, domain=settings.helpdesk_mail_domain)
        now = datetime.now(UTC)
        await messages.provider.create(
            helpdesk.new_message_fields(
                ticket_id=ticket.pk, direction=helpdesk.OUT, author=who.label,
                body=body, message_id=headers["message_id"],
                in_reply_to=headers["in_reply_to"], references=headers["references"],
                sent_at=now,
            ),
            ctx,
        )
        await resource.provider.update(
            ticket.pk, {"thread_message_id": headers["message_id"]}, ctx,
        )
        reference = str(ticket.get("reference") or "")
        subject = helpdesk.tag_subject(str(ticket.get("subject") or ""), reference)
        recipient = str(ticket.get("requester_email") or "").strip()
        if recipient:
            await notifier.send(
                Notification(
                    recipient=recipient,
                    title=subject,
                    body=body,
                    # Urgent, so this is never dropped by
                    # `CRM_EMAIL_MIN_PRIORITY`: a reply is transactional, not
                    # a notification somebody can reasonably choose to
                    # filter out.
                    priority=Priority.URGENT,
                    channels=("email",),
                    resource="tickets",
                    record_id=str(ticket.pk),
                    actor=who.email or who.subject,
                    meta={
                        "message_id": headers["message_id"],
                        "in_reply_to": headers["in_reply_to"],
                        "references": headers["references"],
                    },
                ),
                ctx,
            )
            sent += 1
    return ActionResult(message=f"Sent {sent} repl(y/ies).", level="success")


async def _last_message(messages: Resource, ticket_id: Any, ctx: Ctx):
    """The most recently added message on a ticket, in either direction."""
    query = ListQuery(
        filter=Condition("ticket_id", Op.EQ, str(ticket_id)),
        sort=(Sort("created_at", SortDir.DESC),),
        page_size=1,
        with_total=False,
    )
    page = await messages.provider.list(query, ctx)
    items = list(page.items)
    return items[0] if items else None
