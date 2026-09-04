"""Accounts and machine credentials.

The one module that always loads. Everything an application needs in order to
have a *someone* -- the people who sign in, and the tokens that stand in for
them on the API -- lives here, and nothing else does. Business entities belong
to modules a developer writes or enables; see ``modules/demo_crm`` for one.

Accounts are an ordinary resource, so the same list, form, filter and
permission machinery manages them. There is no separate admin area to keep in
step with the rest of the application.
"""

from __future__ import annotations

from app.auth import current as auth
from app.auth.base import PasswordsNotManaged
from app.auth.local import generate_password
from app.core.registry import Registry
from app.core.results import Ctx
from app.fields.types import (
    BooleanField,
    DateTimeField,
    EmailField,
    JSONField,
    MultiSelectField,
    TextField,
    TimezoneField,
)
from app.resources.actions import ActionResult, action
from app.resources.policy import RolePolicy
from app.resources.rbac import DEFAULT_ROLES
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
    "name": "core_identity",
    "label": "Accounts",
    "description": "The people and machines that sign in.",
    "menu_groups": {"Administration": 90},
}

#: Role names offered by the account form.
#:
#: Taken from the shipped grants rather than repeated here, so adding a
#: built-in role is a single edit in ``app.resources.rbac``. Roles created at
#: runtime through the Roles screen are stored as free text on the user, so
#: this list constrains the picker, not the data.
ROLES = [(role["name"], role["label"]) for role in DEFAULT_ROLES]


def register(registry: Registry) -> None:
    registry.add_resource(_users())
    registry.add_resource(_api_tokens())


@action(
    "reset_password",
    "Reset password",
    icon="⚿",
    confirm="Set a temporary password for this account?",
    # Offered only where there is a password to reset. Behind SSO the button
    # is absent rather than present and broken.
    available=lambda record, identity: _can_reset(),
)
async def reset_password(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Give an account a temporary password, shown once.

    Not emailed from here: an administrator doing this is usually standing next
    to the person, or on a call with them, and a password read aloud beats one
    sitting in an inbox. The account is marked as needing a change, so the
    temporary password cannot become a permanent one.
    """
    provider = auth.password_provider()
    if provider is None:
        return ActionResult(
            message="Passwords are not managed by this application.", level="error"
        )
    if len(records) != 1:
        return ActionResult(
            message="Reset one account at a time, so each password can be read back.",
            level="warning",
        )

    record = records[0]
    temporary = generate_password()
    try:
        await provider.set_password(str(record.pk), temporary, must_change=True)
    except (PasswordsNotManaged, Exception) as exc:  # noqa: B014
        return ActionResult(message=str(exc), level="error")

    # Shown once, in the toast. It is never stored in a readable form, so
    # there is no second chance to look it up -- which is the point.
    return ActionResult(
        message=(
            f"Temporary password for {record.get('email', 'this account')}: "
            f"{temporary} — they will be asked to change it at their next sign-in."
        ),
        level="info",
    )


def _can_reset() -> bool:
    provider = auth.password_provider()
    return bool(provider and provider.capabilities.admin_reset)


def _users() -> Resource:
    return Resource(
        "users",
        provider="db.main#users",
        label="User",
        icon="◉",
        menu_group="Administration",
        menu_order=10,
        display_field="name",
        default_sort=["name"],
        policy=RolePolicy(read=["admin"], write=["admin"]),
        actions=[reset_password],
        fields=[
            TextField("id", label="ID", in_form=False, in_list=False, in_detail=False),
            TextField("name", required=True, searchable=True),
            EmailField("email", required=True, searchable=True),
            # Never rendered: it would put a password hash on screen and into
            # any CSV export.
            TextField("password_hash", in_list=False, in_form=False, in_detail=False,
                      read_roles=["nobody"]),
            MultiSelectField("roles", choices=ROLES, in_filter=True),
            BooleanField("is_active", label="Active", default=True, inline_editable=True),
            TimezoneField("timezone", in_filter=True),
            DateTimeField("last_login", label="Last sign-in", readonly=True, in_form=False),
            # Password state. Read-only everywhere: these are set by signing in
            # and by the reset action, never by editing a form.
            DateTimeField("password_changed_at", label="Password set", readonly=True,
                          in_form=False, in_list=False),
            BooleanField("must_change_password", label="Must change password",
                         readonly=True, in_form=False, in_list=False),
            DateTimeField("locked_until", label="Locked until", readonly=True,
                          in_form=False, in_list=False,
                          help="Set automatically after repeated failed sign-ins."),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
        search=SearchSpec(fields=("name", "email"), filters=("roles", "is_active")),
        views=[
            ListView(
                columns=[Column("name", link=True), "email", "roles", "timezone",
                         "is_active", "last_login"],
                default_sort=["name"],
            ),
            FormView([
                Section("Account", ["name", "email", "is_active"], columns=2),
                Section("Access", ["roles"], columns=1),
                Section("Preferences", ["timezone"], columns=1),
            ]),
            DetailView(
                sections=[
                    Section("Account", ["name", "email", "is_active"], columns=2),
                    Section("Access", ["roles"], columns=1),
                    Section("Preferences", ["timezone"], columns=1),
                    Section("History", ["last_login", "created_at"], columns=2),
                    Section("Password",
                            ["password_changed_at", "must_change_password", "locked_until"],
                            columns=3),
                ],
                title_field="name",
                subtitle_field="email",
            ),
        ],
    )


def _api_tokens() -> Resource:
    """Machine credentials for the JSON API.

    Only the hash is stored, so this resource can never show a usable token --
    which is why there is no create form here; use ``crm token`` instead.
    """
    return Resource(
        "api_tokens",
        provider="db.main#api_tokens",
        label="API token",
        icon="⚿",
        menu_group="Administration",
        menu_order=20,
        display_field="name",
        default_sort=["-created_at"],
        policy=RolePolicy(read=["admin"], write=["admin"]),
        fields=[
            TextField("id", label="ID", in_form=False, in_list=False, in_detail=False),
            TextField("name", required=True, searchable=True, label="Label"),
            TextField("token_hash", in_list=False, in_form=False, in_detail=False,
                      read_roles=["nobody"]),
            MultiSelectField("roles", choices=ROLES),
            BooleanField("is_active", label="Active", default=True, inline_editable=True),
            JSONField("meta", label="Metadata"),
            DateTimeField("last_used", label="Last used", readonly=True, in_form=False),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
    )
