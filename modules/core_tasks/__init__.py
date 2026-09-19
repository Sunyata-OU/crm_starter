"""Tasks: work a person owes, as opposed to work a worker runs.

Loads with the platform's own modules rather than as an option, for the same
reason access control does: a back office without a way to write down "somebody
has to call this company back" grows one in a spreadsheet, and the spreadsheet
is where it stays.

Assignment is deliberate. Nothing here picks an assignee, round-robins, or
claims a task on somebody's behalf -- a queue that assigns itself is a queue
nobody feels responsible for. What is automatic is being *told*: see
:mod:`app.tasks` for the sweep that announces hand-overs and due dates.

The board view is the point of the resource. A list of tasks sorted by date
answers "what is late"; a board by state answers "what is going on", which is
the question somebody standing in front of the back office actually asks.
"""

from __future__ import annotations

from app.core.clock import utcnow
from app.core.registry import Registry
from app.core.results import Ctx
from app.fields.types import (
    DateTimeField,
    StatusField,
    TextAreaField,
    TextField,
)
from app.resources.actions import ActionResult, action
from app.resources.rbac import DbPolicy
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
    "name": "core_tasks",
    "label": "Tasks",
    "description": "Work assigned to a person, with reminders.",
    "depends": ("core_identity", "core_access"),
    "menu_groups": {"Work": 1},
}

#: open -> doing -> done, with two ways out that are not "done".
STATES = [
    ("open", "Open", "blue"),
    ("doing", "In progress", "amber"),
    ("blocked", "Blocked", "red"),
    ("done", "Done", "green"),
    ("cancelled", "Cancelled", "grey"),
]

PRIORITIES = [
    ("low", "Low", "grey"),
    ("normal", "Normal", "blue"),
    ("high", "High", "amber"),
    ("urgent", "Urgent", "red"),
]


def register(registry: Registry) -> None:
    registry.add_resource(_tasks())


def _tasks() -> Resource:
    return Resource(
        "tasks",
        provider="db.main#tasks",
        label="Task",
        icon="☑",
        menu_group="Work",
        menu_order=20,
        display_field="title",
        default_sort=["due_at"],
        # Who raised it, filled in wherever the task was created from.
        stamp={"created_by": "id", "created_by_name": "label"},
        # Deliberately no RolePolicy: who may see and assign work is exactly
        # the kind of rule an administrator should be able to change from the
        # permissions screen without a deployment. `DbPolicy`'s own default
        # base (`Policy()`, anyone signed in may do anything) is the fallback
        # for a deployment that never opens that screen, so this is no
        # narrower than it always was until somebody configures it to be.
        policy=DbPolicy(),
        fields=[
            TextField("id", in_form=False, in_list=False, in_detail=False),
            TextField("title", label="Task", required=True, searchable=True),
            TextAreaField("body", label="Detail", rows=4, searchable=True, in_list=False),
            StatusField("state", label="State", choices=STATES, in_filter=True,
                        default="open", inline_editable=True),
            StatusField("priority", choices=PRIORITIES, in_filter=True, default="normal",
                        inline_editable=True),
            # Free text rather than a dropdown, deliberately. Identity here
            # comes from Keycloak and there is no local account table to offer
            # a list from; an address that reaches nobody shows up as a task
            # nobody is doing, which is visible, where an empty dropdown is
            # merely baffling.
            TextField("assignee", label="Assigned to", searchable=True, in_filter=True,
                      inline_editable=True,
                      help="The address this person signs in with. Leave empty for nobody."),
            TextField("assignee_name", label="Name", in_list=False, in_form=False),
            DateTimeField("due_at", label="Due", in_filter=True, inline_editable=True),
            # What it is about. Filled in by the "add a task" link on a record
            # page, and readable rather than editable there: retargeting a task
            # at another record is confusing enough to be worth retyping.
            TextField("resource", label="About", in_filter=True),
            TextField("record_id", label="Record"),
            TextField("created_by", label="Raised by", in_form=False, in_list=False),
            TextField("created_by_name", label="Raised by", in_form=False),
            DateTimeField("created_at", label="Raised", readonly=True, in_form=False),
            DateTimeField("done_at", label="Finished", readonly=True, in_form=False,
                          in_list=False),
            TextField("done_by", label="Finished by", in_form=False, in_list=False),
            # The sweep's bookkeeping, on the record so it can be seen when
            # somebody asks why they were or were not told.
            TextField("notified_assignee", label="Last told", in_form=False, in_list=False),
            DateTimeField("reminded_at", label="Reminded", in_form=False, in_list=False),
        ],
        search=SearchSpec(
            fields=("title", "body", "assignee"),
            filters=("state", "priority", "assignee", "resource"),
        ),
        actions=[take, finish, reopen],
        views=[
            ListView(
                columns=[
                    Column("title", link=True, width="30%"),
                    "state", "priority",
                    Column("assignee", label="Assigned to", width="18%"),
                    Column("due_at", label="Due", width="14%"),
                    Column("resource", label="About"),
                ],
                default_sort=["due_at"],
                row_actions=["take", "finish"],
                bulk_actions=["delete"],
                empty_message="Nothing outstanding.",
            ),
            BoardView(
                group_by="state",
                card=Card(title="title", subtitle="assignee", badges=["priority", "due_at"]),
                default_sort=["due_at"],
            ),
            FormView([
                Section("Task", ["title", "body"], columns=1),
                Section("Handling", ["assignee", "state", "priority", "due_at"], columns=2),
                Section("About", ["resource", "record_id"], columns=2),
            ]),
            DetailView(
                sections=[
                    Section("Task", ["title", "body"], columns=1),
                    Section("Handling", ["assignee", "state", "priority", "due_at"],
                            columns=2),
                    Section("About", ["resource", "record_id"], columns=2),
                    Section("Record", ["created_by_name", "created_at", "done_by",
                                       "done_at"], columns=2),
                ],
                title_field="title",
                subtitle_field="assignee",
            ),
        ],
    )


@action("take", "Assign to me", icon="☚", placements=("list", "detail"))
async def take(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Put my name on this.

    The one assignment that needs no form: the commonest thing anybody does
    with a queue is pick something out of it.
    """
    who = ctx.identity
    for record in records:
        await resource.provider.update(
            record.pk,
            {
                "assignee": who.email or who.subject,
                "assignee_name": who.label,
                "state": "doing" if str(record.get("state")) == "open" else record.get("state"),
            },
            ctx,
        )
    return ActionResult(message=f"{len(records)} task(s) are yours.", level="success")


@action("finish", "Mark done", icon="✓", placements=("list", "detail"))
async def finish(records, ctx: Ctx, resource: Resource) -> ActionResult:
    who = ctx.identity
    now = utcnow()
    for record in records:
        await resource.provider.update(
            record.pk,
            {"state": "done", "done_at": now, "done_by": who.email or who.subject},
            ctx,
        )
    return ActionResult(message=f"{len(records)} task(s) closed.", level="success")


@action("reopen", "Reopen", icon="↺", placements=("detail",))
async def reopen(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Put a finished task back, and let it be announced again.

    ``reminded_at`` is cleared as well as the state: a task that comes back has
    a fresh due date to warn about, and leaving the flag set would mean the
    warning was silently spent on the first time round.
    """
    for record in records:
        await resource.provider.update(
            record.pk,
            {"state": "open", "done_at": None, "done_by": None, "reminded_at": None},
            ctx,
        )
    return ActionResult(message=f"{len(records)} task(s) reopened.", level="success")
