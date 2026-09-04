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
