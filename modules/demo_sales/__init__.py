"""Demo: a sales pipeline of deals and activities.

The second half of the worked example, and demo only. Where ``demo_crm`` shows
the everyday list/form/detail path, this one exercises the parts of the
framework that go beyond it: a board view, a calendar, charts, a pivot, custom
actions, and row-level scoping so a rep sees only their own deals.

It also extends a resource it did not declare -- see :func:`_extend_records` --
which is how a module adds to another module's screens without editing its
source.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.core.clock import utcnow
from app.core.query import Agg, Condition, Measure, Op, and_
from app.core.registry import Registry
from app.core.results import Ctx
from app.fields.types import (
    BooleanField,
    CurrencyField,
    DateField,
    DateTimeField,
    DurationField,
    PercentField,
    RelationField,
    SelectField,
    StatusField,
    TextAreaField,
    TextField,
)
from app.resources.actions import ActionResult, RowOutcome, action
from app.resources.policy import OwnerPolicy
from app.resources.rbac import DbPolicy
from app.resources.resource import Resource
from app.resources.views import (
    ActivityView,
    BoardView,
    CalendarView,
    Card,
    ChartView,
    Column,
    DashboardView,
    DetailView,
    FormView,
    GanttView,
    ListView,
    Panel,
    PivotView,
    QuickFilter,
    SearchSpec,
    Section,
)

MANIFEST = {
    "name": "demo_sales",
    "label": "Demo: sales pipeline",
    "description": "Deals, pipeline and scheduled activities. Not loaded unless enabled.",
    "depends": ("demo_crm",),
    "menu_groups": {"Sales": 20},
    "optional": True,
}

STAGES = [
    ("qualifying", "Qualifying", "default"),
    ("proposal", "Proposal", "blue"),
    ("negotiation", "Negotiation", "amber"),
    ("won", "Won", "green"),
    ("lost", "Lost", "red"),
]

#: Stages a deal can no longer move out of.
CLOSED_STAGES = {"won", "lost"}

ACTIVITY_KINDS = [
    ("call", "Call"),
    ("meeting", "Meeting"),
    ("email", "Email"),
    ("task", "Task"),
]


def register(registry: Registry) -> None:
    registry.add_resource(_deals())
    registry.add_resource(_activities())
    _extend_records(registry)


# -- actions ---------------------------------------------------------------


@action(
    "mark_won",
    "Mark as won",
    style="primary",
    icon="✓",
    confirm="Mark this deal as won?",
    available=lambda record, identity: record.get("stage") not in CLOSED_STAGES,
)
async def mark_won(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Close a deal, stamping the date the same way for every record."""
    today = date.today().isoformat()
    results = [
        await resource.provider.update(
            r.pk, {"stage": "won", "probability": 100, "closed_on": today}, ctx
        )
        for r in records
    ]
    failed = [r for r in results if r.failed]
    if failed:
        return ActionResult(
            message=f"{len(failed)} of {len(results)} could not be updated.",
            level="warning",
        )
    if any(r.status.value == "pending" for r in results):
        return ActionResult(message="Queued.", level="info")
    return ActionResult(message=f"Marked {len(results)} deal(s) as won.", level="success")


#: Why a deal was lost. Not a column on the deal -- it is something the person
#: closing it knows and the record does not, which is exactly what an action's
#: `prompt_fields` are for.
LOST_REASONS = [
    ("price", "Price"),
    ("competitor", "Went to a competitor"),
    ("timing", "Bad timing"),
    ("no_budget", "No budget"),
    ("no_reply", "Went quiet"),
]


@action(
    "mark_lost",
    "Mark as lost",
    style="danger",
    available=lambda record, identity: record.get("stage") not in CLOSED_STAGES,
    # Asked before the action runs, and handed to the handler as `params`.
    # No `confirm` alongside it: the dialog is already a deliberate step, and
    # two of them in a row is a habit people learn to click through.
    prompt_fields=[
        SelectField("lost_reason", label="Reason", choices=LOST_REASONS, required=True),
        TextAreaField("lost_note", label="Note", rows=3,
                      help="Anything the next person picking this account up "
                           "should know."),
    ],
)
async def mark_lost(records, ctx: Ctx, resource: Resource, *, params) -> ActionResult:
    """Close deals as lost, recording why -- one outcome per record.

    Reported per record rather than as one number: a bulk close over forty
    deals is forty separate writes, and "Done." would hide the three that were
    not.
    """
    today = date.today().isoformat()
    reason = params.get("lost_reason", "")
    note = (params.get("lost_note") or "").strip()
    outcomes = []
    for record in records:
        result = await resource.provider.update(
            record.pk,
            {
                "stage": "lost",
                "probability": 0,
                "closed_on": today,
                "notes": _with_reason(record.get("notes"), reason, note),
            },
            ctx,
        )
        outcomes.append(
            RowOutcome(record.pk, not result.failed, result.message or "")
        )
    return ActionResult.from_outcomes(outcomes, done="Marked as lost")


def _with_reason(existing: Any, reason: str, note: str) -> str:
    """Append the reason to the deal's notes rather than replacing them."""
    line = f"Lost ({reason})" + (f": {note}" if note else "")
    return f"{existing}\n{line}".strip() if existing else line


@action(
    "assign",
    "Assign to me",
    icon="◉",
    available=lambda record, identity: record.get("owner") != identity.email,
)
async def assign_to_me(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Take ownership, and tell the previous owner.

    The notification is the point of this action existing as an example: work
    changing hands is exactly the sort of thing a person needs to be told
    about, and the notifier will not tell them about their own action.
    """
    from app.notify import Kind, Notification, notifier

    for record in records:
        previous = record.get("owner")
        await resource.provider.update(record.pk, {"owner": ctx.identity.email}, ctx)
        if previous:
            await notifier.send(
                Notification(
                    recipient=str(previous),
                    kind=Kind.CHANGED,
                    title=f"{ctx.identity.label} took over {record.get('name')}",
                    body="It is no longer on your pipeline.",
                    resource=resource.name,
                    record_id=str(record.pk),
                    actor=ctx.identity.email,
                ),
                ctx,
            )
    return ActionResult(message=f"Assigned {len(records)} deal(s) to you.")


@action("remind", "Remind me", icon="◔", placements=("row", "detail"))
async def remind_me(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Schedule a nudge for tomorrow.

    A reminder is a notification with a future ``due_at``; the sweep run by
    ``crm notify-due`` delivers it when the time comes. There is no timer and
    no long-lived state, so several workers can run the sweep safely.
    """
    from datetime import timedelta

    from app.notify import Kind, Notification, notifier

    when = utcnow() + timedelta(days=1)
    for record in records:
        await notifier.send(
            Notification(
                recipient=ctx.identity.email or ctx.identity.subject,
                kind=Kind.DUE,
                title=f"Follow up: {record.get('name')}",
                resource=resource.name,
                record_id=str(record.pk),
                due_at=when,
            ),
            ctx,
        )
    return ActionResult(message=f"You will be reminded {when:%d %b at %H:%M}.")


@action("log_call", "Log a call", icon="☎", placements=("detail",))
async def log_call(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Create a follow-up activity against the deal.

    Writes through a *different* resource's provider, which is the point: the
    two need not share a backend.
    """
    registry = resource.registry
    if registry is None or not registry.has_resource("activities"):
        return ActionResult(message="No activities resource is available.", level="warning")

    activities = registry.resource("activities")
    for record in records:
        await activities.provider.create(
            {
                "subject": f"Call about {record.get('name')}",
                "kind": "call",
                "deal_id": record.pk,
                "due_on": date.today().isoformat(),
                "owner": ctx.identity.subject,
                "done": False,
            },
            ctx,
        )
    return ActionResult(message="Call logged.", redirect="")


# -- resources -------------------------------------------------------------


def _deals() -> Resource:
    return Resource(
        "deals",
        provider="db.main#deals",
        label="Deal",
        icon="◈",
        menu_group="Sales",
        menu_order=10,
        display_field="name",
        default_sort=["-amount"],
        timeline=True,
        # Access comes from the permissions table, falling back to ownership
        # when the table says nothing -- so a deployment that never configures
        # permissions still behaves sensibly, and one that does can change the
        # rules without a release.
        #
        # Matched on email: it is the identifier every auth provider supplies,
        # so the same rule works under passwords, SSO or a gateway header.
        policy=DbPolicy(
            base=OwnerPolicy("owner", identity_attr="email",
                             bypass_roles=("admin", "manager")),
            owner_field="owner",
            identity_attr="email",
        ),
        actions=[mark_won, mark_lost, log_call, assign_to_me, remind_me],
        fields=[
            TextField("id", label="ID", in_form=False, in_list=False, in_detail=False),
            TextField("name", label="Deal", required=True, searchable=True,
                      inline_editable=True, max_length=160),
            RelationField("company_id", label="Company", resource="companies",
                          display="name", in_filter=True),
            RelationField("contact_id", label="Primary contact", resource="contacts",
                          display="name", in_filter=True),
            CurrencyField("amount", label="Value", inline_editable=True),
            StatusField("stage", choices=STAGES, default="qualifying",
                        in_filter=True, inline_editable=True),
            PercentField("probability", label="Probability", default=10, inline_editable=True),
            DateField("expected_close", label="Expected close", inline_editable=True),
            DateField("closed_on", label="Closed", readonly=True, in_form=False),
            TextField("owner", label="Owner", in_filter=True, inline_editable=True),
            SelectField("source", choices=["inbound", "outbound", "referral", "partner"],
                        in_filter=True),
            TextAreaField("notes", rows=4),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
        search=SearchSpec(
            fields=("name", "notes"),
            filters=("stage", "owner", "source", "company_id"),
            group_by=("stage", "owner", "source"),
            quick_filters=[
                QuickFilter(
                    "mine", "Mine", icon="◉",
                    description="Deals you own.",
                    build=lambda identity: Condition("owner", Op.EQ, identity.email),
                ),
                QuickFilter(
                    "open", "In play", icon="◈",
                    description="Not yet won or lost.",
                    build=lambda identity: Condition("stage", Op.NOT_IN, list(CLOSED_STAGES)),
                ),
                QuickFilter(
                    "closing", "Closing soon", icon="◔",
                    description="Expected to close within a fortnight.",
                    build=lambda identity: and_(
                        Condition("stage", Op.NOT_IN, list(CLOSED_STAGES)),
                        Condition("expected_close", Op.LTE,
                                  (date.today() + timedelta(days=14)).isoformat()),
                    ),
                ),
                QuickFilter(
                    "big", "Over 50k", icon="▲",
                    build=lambda identity: Condition("amount", Op.GTE, 50000),
                ),
            ],
        ),
        views=[
            ListView(
                columns=[
                    Column("name", link=True, width="24%"),
                    "company_id",
                    Column("amount", align="right"),
                    "stage", "probability", "expected_close", "owner",
                ],
                default_sort=["-amount"],
                row_actions=["mark_won"],
                bulk_actions=["mark_won", "mark_lost", "assign", "delete"],
            ),
            BoardView(
                group_by="stage",
                card=Card(
                    title="name",
                    subtitle="company_id",
                    badges=["amount", "probability"],
                ),
                sum_field="amount",
                default_sort=["-amount"],
                label="Pipeline",
            ),
            ChartView(
                group_by="stage",
                measure=Measure(Agg.SUM, "amount", alias="value"),
                label="Value by stage",
                chart="column",
            ),
            PivotView(
                rows=("owner",),
                columns=("stage",),
                measures=(Measure(Agg.SUM, "amount", alias="value"),),
                label="Owner × stage",
            ),
            # From the day a deal was created to the day it is expected to
            # close, banded by stage. Both dates are already on the record, so
            # the schedule is a way of reading the pipeline, not a second
            # place to maintain it.
            GanttView(
                start_field="created_at",
                end_field="expected_close",
                title_field="name",
                group_by="stage",
                progress_field="probability",
                label="Schedule",
                default_scale="month",
            ),
            DashboardView(
                panels=[
                    Panel(view="chart", title="Value by stage", span=1),
                    Panel(view="pivot", title="Owner × stage", span=1),
                    Panel(view="board", title="Pipeline", span=2, height="26rem"),
                    Panel(view="activity", resource="activities",
                          title="Outstanding work", span=2),
                ],
                label="Overview",
                columns=2,
            ),
            FormView([
                Section("Deal", ["name", "company_id", "contact_id"], columns=2),
                Section("Commercials", ["amount", "stage", "probability", "expected_close"], columns=2),
                Section("Attribution", ["owner", "source", "notes"], columns=1),
            ]),
            DetailView(
                sections=[
                    Section("Deal", ["name", "company_id", "contact_id"], columns=2),
                    Section("Commercials", ["amount", "stage", "probability",
                                            "expected_close", "closed_on"], columns=3),
                    Section("Attribution", ["owner", "source", "notes"], columns=1),
                ],
                title_field="name",
                timeline=True,
            ),
        ],
    )


def _activities() -> Resource:
    return Resource(
        "activities",
        provider="db.main#activities",
        label="Activity",
        label_plural="Activities",
        icon="◔",
        menu_group="Sales",
        menu_order=20,
        display_field="subject",
        default_sort=["due_on"],
        policy=DbPolicy(
            base=OwnerPolicy("owner", identity_attr="email",
                             bypass_roles=("admin", "manager"), read_all=True),
            owner_field="owner",
            identity_attr="email",
        ),
        fields=[
            TextField("id", label="ID", in_form=False, in_list=False, in_detail=False),
            TextField("subject", required=True, searchable=True, inline_editable=True),
            SelectField("kind", label="Type", choices=ACTIVITY_KINDS, default="task",
                        in_filter=True),
            RelationField("deal_id", label="Deal", resource="deals", display="name",
                          in_filter=True),
            RelationField("contact_id", label="Contact", resource="contacts", display="name"),
            DateField("due_on", label="Due", required=True, inline_editable=True),
            DurationField("minutes", label="Duration"),
            BooleanField("done", label="Completed", default=False, inline_editable=True,
                         in_filter=True),
            TextField("owner", label="Owner", in_filter=True),
            TextAreaField("notes", rows=3),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
        search=SearchSpec(
            fields=("subject", "notes"),
            filters=("kind", "done", "owner", "deal_id"),
            quick_filters=[
                QuickFilter(
                    "todo", "To do", icon="◔",
                    description="Not yet completed.",
                    build=lambda identity: Condition("done", Op.EQ, False),
                ),
                QuickFilter(
                    "overdue", "Overdue", icon="▲",
                    description="Past their due date and still open.",
                    build=lambda identity: and_(
                        Condition("done", Op.EQ, False),
                        Condition("due_on", Op.LT, date.today().isoformat()),
                    ),
                ),
                QuickFilter(
                    "week", "This week", icon="▤",
                    build=lambda identity: Condition(
                        "due_on", Op.BETWEEN,
                        [date.today().isoformat(),
                         (date.today() + timedelta(days=7)).isoformat()],
                    ),
                ),
                QuickFilter(
                    "mine", "Mine", icon="◉",
                    build=lambda identity: Condition("owner", Op.EQ, identity.email),
                ),
            ],
        ),
        views=[
            ListView(
                columns=[
                    Column("subject", link=True, width="30%"),
                    "kind", "due_on", "deal_id", "owner", "done",
                ],
                default_sort=["due_on"],
                bulk_actions=["delete"],
            ),
            CalendarView(
                start_field="due_on",
                title_field="subject",
                color_field="kind",
                label="Schedule",
            ),
            ChartView(
                group_by="kind",
                measure=Measure(Agg.COUNT, alias="value"),
                label="By type",
                chart="bar",
            ),
            # Who owes what, by when. Completed work is excluded by the spec
            # rather than by a filter the user has to remember to apply: an
            # activity that is done is not outstanding, on anybody's grid.
            ActivityView(
                row_field="owner",
                activity_field="kind",
                due_field="due_on",
                title_field="subject",
                done_filter=Condition("done", Op.EQ, False),
                label="Workload",
            ),
            FormView([
                Section("Activity", ["subject", "kind", "due_on", "minutes"], columns=2),
                Section("Links", ["deal_id", "contact_id", "owner"], columns=3),
                Section("Detail", ["done", "notes"], columns=1),
            ]),
        ],
    )


def _extend_records(registry: Registry) -> None:
    """Add this module's concerns to resources ``demo_crm`` declared.

    Nothing in ``demo_crm`` knows deals exist. Extending its resources here
    keeps the dependency pointing one way, and means removing this module
    cleanly removes its additions.
    """
    if registry.has_resource("companies"):
        companies = registry.resource("companies")
        companies.add_field(
            _deal_backref("companies"),
        )
        companies.view("list").add_columns(Column("industry"), after="name")

    if registry.has_resource("contacts"):
        contacts = registry.resource("contacts")
        contacts.add_field(
            _deal_backref("contacts", via="contact_id"),
        )


def _deal_backref(owner: str, via: str = "company_id"):
    from app.fields.types import BackrefField

    return BackrefField(
        "deals",
        resource="deals",
        via=via,
        label="Deals",
        columns=("name", "amount", "stage", "expected_close"),
        limit=10,
    )
