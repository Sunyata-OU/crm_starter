"""Roles, permissions, the audit log and notifications, as ordinary resources.

Loads by default, with :mod:`modules.core_identity`: an application without a
way to say who may do what is not something a developer should have to build
first.

The point of the module is that access control needs no bespoke admin area.
Roles and grants are records; they get list views, forms, filters and search
from the same machinery any other resource does, and the audit log is a
read-only resource like any other.
"""

from __future__ import annotations

from app.core.placement import DEFAULT_CONNECTION
from app.core.registry import Registry
from app.core.results import Ctx
from app.fields.types import (
    BooleanField,
    DateTimeField,
    JSONField,
    MultiSelectField,
    SelectField,
    StatusField,
    TextAreaField,
    TextField,
)
from app.resources.actions import ActionResult, action
from app.resources.policy import OwnerPolicy, RolePolicy
from app.resources.rbac import ANY_RESOURCE, store
from app.resources.resource import Resource
from app.resources.views import (
    Column,
    DetailView,
    FormView,
    ListView,
    SearchSpec,
    Section,
)

MANIFEST = {
    "name": "core_access",
    "label": "Access control",
    "description": "Roles, permissions, the audit trail and notifications.",
    "depends": ("core_identity",),
    "menu_groups": {"Administration": 90},
}

ROW_SCOPES = [
    ("all", "Every record", "green"),
    ("own", "Only their own", "amber"),
    ("none", "None", "red"),
]

ACTIONS = [
    ("create", "Created", "green"),
    ("update", "Updated", "blue"),
    ("delete", "Deleted", "red"),
]

JOB_STATES = [
    ("queued", "Queued", "amber"),
    ("running", "Running", "blue"),
    ("done", "Done", "green"),
    ("failed", "Failed", "red"),
]

STATUSES = [
    ("ok", "Applied", "green"),
    ("pending", "Queued", "amber"),
    ("error", "Failed", "red"),
]

NOTIFICATION_KINDS = [
    ("info", "Information", "default"),
    ("assigned", "Assigned", "blue"),
    ("mentioned", "Mentioned", "blue"),
    ("due", "Due", "amber"),
    ("overdue", "Overdue", "red"),
    ("changed", "Changed", "default"),
    ("approval", "Needs approval", "amber"),
    ("error", "Problem", "red"),
]

PRIORITIES = [
    ("low", "Low", "default"),
    ("normal", "Normal", "blue"),
    ("high", "High", "amber"),
    ("urgent", "Urgent", "red"),
]


def register(registry: Registry) -> None:
    registry.add_resource(_roles())
    registry.add_resource(_permissions(registry))
    registry.add_resource(_audit_log())
    registry.add_resource(_notifications())
    registry.add_resource(_jobs(registry))


def _known_resources(registry: Registry):
    """Resource names a grant may refer to, resolved when the form renders.

    A callable rather than a fixed list, because modules loading after this one
    add resources this dropdown should offer.
    """

    def choices(ctx: Ctx):
        options = [(ANY_RESOURCE, "Every resource")]
        options += [(r.name, r.label_plural) for r in registry.resources]
        return options

    return choices


def _roles() -> Resource:
    return Resource(
        "roles",
        provider="db.main#roles",
        label="Role",
        icon="◎",
        menu_group="Administration",
        menu_order=30,
        display_field="label",
        default_sort=["name"],
        policy=RolePolicy(read=["admin"], write=["admin"]),
        fields=[
            TextField("id", in_form=False, in_list=False, in_detail=False),
            TextField("name", required=True, searchable=True,
                      help="The value stored on a user. Lowercase, no spaces."),
            TextField("label", label="Display name", searchable=True, inline_editable=True),
            TextAreaField("description", rows=2),
            BooleanField("is_builtin", label="Built in", readonly=True, in_form=False,
                         help="Built-in roles cannot be deleted."),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
        search=SearchSpec(fields=("name", "label", "description")),
        views=[
            ListView(
                columns=[Column("label", link=True), "name", "description", "is_builtin"],
                default_sort=["name"],
            ),
            FormView([Section("Role", ["name", "label", "description"], columns=1)]),
        ],
    )


def _permissions(registry: Registry) -> Resource:
    return Resource(
        "permissions",
        provider="db.main#permissions",
        label="Permission",
        icon="⚿",
        menu_group="Administration",
        menu_order=40,
        display_field="role",
        default_sort=["role", "resource"],
        policy=RolePolicy(read=["admin"], write=["admin"]),
        description="What each role may do. Changes take effect immediately.",
        actions=[reload_permissions],
        fields=[
            TextField("id", in_form=False, in_list=False, in_detail=False),
            TextField("role", required=True, searchable=True, in_filter=True,
                      help="Matches the name of a role."),
            SelectField("resource", required=True, in_filter=True,
                        choices=_known_resources(registry), default=ANY_RESOURCE),
            BooleanField("can_read", label="Read", default=True, inline_editable=True),
            BooleanField("can_create", label="Create", default=False, inline_editable=True),
            BooleanField("can_update", label="Update", default=False, inline_editable=True),
            BooleanField("can_delete", label="Delete", default=False, inline_editable=True),
            StatusField("row_scope", label="Rows visible", choices=ROW_SCOPES, default="all",
                        in_filter=True, inline_editable=True,
                        help="'Only their own' compares the record's owner to the signed-in user."),
            MultiSelectField("hidden_fields", label="Hidden fields",
                             help="Field names this role never sees."),
            MultiSelectField("readonly_fields", label="Read-only fields",
                             help="Field names this role sees but cannot change."),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
        search=SearchSpec(fields=("role", "resource"), filters=("role", "resource", "row_scope")),
        views=[
            ListView(
                columns=[
                    Column("role", link=True, width="14%"),
                    Column("resource", width="18%"),
                    Column("can_read", label="Read", align="center"),
                    Column("can_create", label="Create", align="center"),
                    Column("can_update", label="Update", align="center"),
                    Column("can_delete", label="Delete", align="center"),
                    "row_scope",
                ],
                default_sort=["role", "resource"],
                bulk_actions=["delete"],
                empty_message="No grants yet. Without any, the built-in policies apply.",
            ),
            FormView([
                Section("Applies to", ["role", "resource"], columns=2),
                Section("Operations", ["can_read", "can_create", "can_update", "can_delete"],
                        columns=4),
                Section("Rows", ["row_scope"], columns=1),
                Section("Fields", ["hidden_fields", "readonly_fields"], columns=2,
                        description="Leave empty to allow every field."),
            ]),
            DetailView(
                sections=[
                    Section("Applies to", ["role", "resource"], columns=2),
                    Section("Operations", ["can_read", "can_create", "can_update", "can_delete"],
                            columns=4),
                    Section("Restrictions", ["row_scope", "hidden_fields", "readonly_fields"],
                            columns=3),
                ],
                timeline=True,
            ),
        ],
    )


def _audit_log() -> Resource:
    """Every change, by whom, with the before and after.

    Read-only through the UI *and* excluded from auditing itself: recording
    writes to the audit log would fill it with entries about itself.
    """
    return Resource(
        "audit_log",
        provider="db.main#audit_log",
        label="Audit entry",
        label_plural="Audit log",
        icon="◵",
        menu_group="Administration",
        menu_order=50,
        display_field="record_label",
        default_sort=["-at"],
        policy=RolePolicy(read=["admin"]),
        audited=False,
        fields=[
            TextField("id", in_form=False, in_list=False, in_detail=False),
            DateTimeField("at", label="When", readonly=True),
            TextField("actor_name", label="Who", searchable=True, in_filter=True),
            TextField("actor", label="Account", searchable=True),
            StatusField("action", label="Action", choices=ACTIONS, in_filter=True),
            TextField("resource", label="Resource", in_filter=True),
            TextField("record_id", label="Record"),
            TextField("record_label", label="Name", searchable=True),
            JSONField("changes", label="Changes"),
            StatusField("status", choices=STATUSES, in_filter=True),
            TextField("detail", label="Detail", in_list=False),
            TextField("request_id", label="Request", in_list=False),
            TextField("ip", label="IP address", in_list=False),
            TextField("actor_provider", label="Signed in via", in_list=False),
        ],
        search=SearchSpec(
            fields=("actor_name", "actor", "record_label"),
            filters=("action", "resource", "actor_name", "status"),
        ),
        views=[
            ListView(
                columns=[
                    Column("at", label="When", width="14%"),
                    Column("actor_name", label="Who", width="14%"),
                    "action", "resource",
                    Column("record_label", label="Record"),
                    "status",
                ],
                default_sort=["-at"],
                inline_edit=False,
                empty_message="Nothing has been changed yet.",
            ),
            DetailView(
                sections=[
                    Section("What", ["at", "action", "resource", "record_label"], columns=2),
                    Section("Who", ["actor_name", "actor", "actor_provider", "ip"], columns=2),
                    Section("Changes", ["changes"], columns=1),
                    Section("Context", ["status", "detail", "request_id"], columns=3),
                ],
                timeline=False,
            ),
        ],
    )


def _notifications() -> Resource:
    """Everything anyone has been told.

    Scoped to the recipient, so a person sees their own -- the same
    ``OwnerPolicy`` used for deals, pointed at a different column. Not audited:
    a notification is already a record of something that happened.
    """
    return Resource(
        "notifications",
        provider="db.main#notifications",
        label="Notification",
        icon="◈",
        menu_group="Administration",
        menu_order=60,
        display_field="title",
        default_sort=["-created_at"],
        policy=OwnerPolicy("recipient", identity_attr="email", bypass_roles=("admin",)),
        audited=False,
        fields=[
            TextField("id", in_form=False, in_list=False, in_detail=False),
            DateTimeField("created_at", label="When", readonly=True, in_form=False),
            TextField("recipient", label="For", in_filter=True),
            StatusField("kind", label="Type", choices=NOTIFICATION_KINDS, in_filter=True),
            StatusField("priority", choices=PRIORITIES, in_filter=True),
            TextField("title", required=True, searchable=True),
            TextAreaField("body", rows=3),
            TextField("resource", label="About", in_filter=True),
            TextField("record_id", label="Record", in_list=False),
            TextField("url", label="Link", in_list=False),
            DateTimeField("due_at", label="Due", in_filter=True,
                          help="Leave empty to deliver immediately."),
            DateTimeField("read_at", label="Read", readonly=True, in_form=False),
            DateTimeField("sent_at", label="Delivered", readonly=True, in_form=False),
            TextField("channels", label="Channels", in_list=False),
            JSONField("delivery", label="Delivery result", in_list=False),
            TextField("actor", label="Raised by", in_list=False),
        ],
        search=SearchSpec(
            fields=("title", "body", "recipient"),
            filters=("kind", "priority", "resource"),
        ),
        views=[
            ListView(
                columns=[
                    Column("created_at", label="When", width="14%"),
                    Column("title", link=True, width="30%"),
                    "kind", "priority", "recipient", "read_at",
                ],
                default_sort=["-created_at"],
                inline_edit=False,
                bulk_actions=["delete"],
                empty_message="Nothing to report.",
            ),
            DetailView(
                sections=[
                    Section("Notification", ["title", "body", "kind", "priority"], columns=2),
                    Section("About", ["resource", "record_id", "url"], columns=3),
                    Section("Delivery", ["recipient", "channels", "due_at", "sent_at",
                                         "read_at", "delivery"], columns=3),
                ],
                timeline=False,
            ),
        ],
    )


@action(
    "reload_permissions",
    "Apply changes now",
    icon="↻",
    placements=("list",),
    roles=("admin",),
)
async def reload_permissions(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Drop the cached grant table.

    Grants are cached for a few seconds because they are read several times per
    request; this forces the reload rather than waiting for the cache to expire.
    """
    store.invalidate()
    table = await store.table(ctx)
    return ActionResult(
        message=f"Reloaded {len(table)} grant(s) across {len(table.roles)} role(s).",
        level="success",
    )


def _jobs(registry: Registry) -> Resource:
    """The durable work queue, as a screen.

    Worth having for the same reason the audit log is: when a background job
    has not happened, the first question is whether it was ever accepted, and
    the answer is a row. Administrators only -- a payload can carry anything --
    and read-only apart from one action, because editing a job's state by hand
    while a worker holds it is a race with no upside.
    """
    # Named rather than hardcoded, so a deployment can put the queue on its own
    # database. A worker polls once a second per process; on a busy queue that
    # is a steady write load with no reason to share a connection pool with the
    # requests people are waiting on.
    #
    # Read from the registry rather than from `get_settings()`, so a caller that
    # built this registry with particular settings -- a test, an embedding
    # application -- gets the settings it passed rather than the environment's.
    connection = getattr(registry.settings, "jobs_connection", None) or DEFAULT_CONNECTION
    return Resource(
        "jobs",
        provider=f"{connection}#jobs",
        label="Job",
        label_plural="Background jobs",
        icon="◷",
        menu_group="Administration",
        menu_order=60,
        display_field="kind",
        default_sort=["-created_at"],
        policy=RolePolicy(read=["admin"]),
        # Not audited: a job row is already a record of something happening,
        # and a worker updates it several times per run.
        audited=False,
        fields=[
            TextField("id", in_form=False),
            TextField("kind", label="Kind", searchable=True, in_filter=True),
            StatusField("status", choices=JOB_STATES, in_filter=True),
            DateTimeField("created_at", label="Enqueued", readonly=True),
            DateTimeField("run_at", label="Runs at", readonly=True),
            DateTimeField("finished_at", label="Finished", readonly=True),
            TextField("attempts", label="Attempts", readonly=True),
            TextField("max_attempts", label="Limit", readonly=True, in_list=False),
            TextField("claimed_by", label="Worker", readonly=True, in_filter=True),
            DateTimeField("claimed_at", label="Claimed", readonly=True, in_list=False),
            TextField("key", label="Key", searchable=True, in_list=False),
            TextField("priority", label="Priority", in_list=False),
            JSONField("payload", label="Payload", in_list=False),
            TextAreaField("last_error", label="Last error", in_list=False),
        ],
        search=SearchSpec(
            fields=("kind", "key", "last_error"),
            filters=("status", "kind", "claimed_by"),
        ),
        actions=[_retry_job],
        views=[
            ListView(
                columns=[
                    Column("kind", link=True, width="22%"),
                    "status",
                    Column("attempts", label="Tries", width="8%"),
                    Column("run_at", label="Runs at"),
                    Column("claimed_by", label="Worker"),
                    Column("finished_at", label="Finished"),
                ],
                default_sort=["-created_at"],
                inline_edit=False,
                empty_message="Nothing has been queued.",
            ),
            DetailView(
                sections=[
                    Section("Job", ["kind", "status", "key", "priority"], columns=2),
                    Section("Timing", ["created_at", "run_at", "claimed_at", "finished_at"], columns=2),
                    Section("Attempts", ["attempts", "max_attempts", "claimed_by"], columns=3),
                    Section("Payload", ["payload"], columns=1),
                    Section("Last error", ["last_error"], columns=1),
                ],
                timeline=False,
            ),
        ],
    )


@action(
    "retry",
    "Queue again",
    icon="↻",
    confirm="Put this job back on the queue?",
    roles=["admin"],
    available=lambda record, identity: str(record.get("status")) in ("failed", "done"),
)
async def _retry_job(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Queue a finished job again, with its attempt count reset.

    Resetting is the point. A job that failed five times is unretryable exactly
    at the moment somebody has fixed the reason it failed, which is the only
    moment anyone wants to retry it.

    Only finished jobs: requeueing one a worker currently holds would have two
    workers running it, which is the one thing the claim exists to prevent.
    """
    from app.jobs import queue

    queued = 0
    for record in records:
        if str(record.get("status")) not in ("failed", "done"):
            continue
        await queue.retry(record.pk, ctx)
        queued += 1
    if not queued:
        return ActionResult(
            message="Only a finished job can be queued again.", level="warning", refresh=False
        )
    return ActionResult(message=f"Queued {queued} job(s) again.")
