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

from datetime import datetime, timedelta

from app.auth import current as auth
from app.auth.base import PasswordsNotManaged
from app.auth.local import generate_password
from app.core import clock
from app.core.clock import utcnow
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

    Only the hash is stored, so a token's value can never be looked up here --
    it is shown once, by the action that issues it, and after that this screen
    can say everything about the credential except what it is.

    The ordinary create form is off for the same reason: a row typed in by hand
    would have no hash behind it, which is a credential that authenticates
    nobody and looks exactly like one that works.
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
        actions=[issue_token, rotate_token],
        fields=[
            TextField("id", label="ID", in_form=False, in_list=False, in_detail=False),
            TextField("name", required=True, searchable=True, label="Label"),
            TextField("token_hash", in_list=False, in_form=False, in_detail=False,
                      read_roles=["nobody"]),
            MultiSelectField("roles", choices=ROLES),
            BooleanField("is_active", label="Active", default=True, inline_editable=True),
            DateTimeField("expires_at", label="Expires", in_filter=True,
                          help="Leave empty for a token that never expires -- "
                               "which should be a decision, not an oversight."),
            JSONField("meta", label="Metadata"),
            DateTimeField("last_used", label="Last used", readonly=True, in_form=False),
            TextField("last_used_ip", label="Last used from", readonly=True,
                      in_form=False, in_list=False),
            DateTimeField("rotated_at", label="Rotated", readonly=True, in_form=False,
                          in_list=False),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
        views=[
            ListView(
                columns=[
                    Column("name", label="Label", link=True, width="26%"),
                    "roles", "is_active",
                    Column("expires_at", label="Expires", width="14%"),
                    Column("last_used", label="Last used", width="14%"),
                ],
                default_sort=["-created_at"],
                row_actions=["rotate_token"],
                empty_message="No API tokens yet.",
            ),
            DetailView(
                sections=[
                    Section("Token", ["name", "roles", "is_active", "expires_at"], columns=2),
                    Section("Use", ["last_used", "last_used_ip", "rotated_at",
                                    "created_at"], columns=2),
                    Section("Metadata", ["meta"], columns=1),
                ],
            ),
        ],
    )


#: How long a token lasts when nobody says otherwise.
#:
#: A default rather than "forever", because the failure mode of an expiring
#: token is an integration that stops and gets fixed, while the failure mode of
#: an immortal one is a credential still working years after the laptop it was
#: pasted on was sold.
DEFAULT_TOKEN_DAYS = 365


@action(
    "issue_token",
    "Issue a token",
    icon="⚿",
    placements=("list",),
    roles=("admin",),
    prompt_fields=[
        TextField("token_name", label="Label", required=True,
                  help="What holds it -- 'tasky', 'the reconciliation script'. "
                       "This is what somebody reads when deciding to revoke it."),
        MultiSelectField("token_roles", label="Roles", choices=ROLES),
        TextField("expires_in_days", label="Expires in (days)",
                  default=str(DEFAULT_TOKEN_DAYS),
                  help="Empty for a token that never expires."),
    ],
)
async def issue_token(records, ctx: Ctx, resource: Resource, *, params) -> ActionResult:
    """Create a token and show its value, once.

    Once is the whole design: only the hash is stored, so this modal is the
    single moment the value exists anywhere outside the caller's memory. It is
    reported through the report template rather than a toast because a toast
    fades, and a credential that faded before it was copied means doing this
    again and wondering whether the first one is still live somewhere.
    """
    from app.auth.api_token import generate_token, hash_token

    label = str(params.get("token_name") or "").strip()
    if not label:
        return ActionResult(message="Give the token a label.", level="error", refresh=False)

    expires_at, problem = _expiry_from(params.get("expires_in_days"))
    if problem:
        return ActionResult(message=problem, level="error", refresh=False)

    raw = generate_token()
    result = await resource.provider.create(
        {
            "name": label,
            "token_hash": hash_token(raw),
            "roles": params.get("token_roles") or [],
            "is_active": True,
            "expires_at": expires_at,
        },
        ctx,
    )
    if result.failed:
        return ActionResult(message=result.message or "The token could not be created.",
                            level="error", refresh=False)

    return _token_report(raw, label, expires_at, verb="Issued")


@action(
    "rotate_token",
    "Rotate",
    icon="↻",
    placements=("row", "detail"),
    roles=("admin",),
    confirm="Issue a new value for this token? The current one stops working immediately.",
)
async def rotate_token(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """Replace a token's value, keeping the row.

    The row is worth keeping -- its label, its roles and when it was last used
    are the history somebody needs -- and the secret is not. Replacing the hash
    invalidates the old value at the same instant the new one starts working,
    so there is no window in which both are valid and no window in which
    neither is.
    """
    from app.auth.api_token import generate_token, hash_token

    if len(records) != 1:
        return ActionResult(
            message="Rotate one token at a time, so each new value can be read back.",
            level="warning", refresh=False,
        )

    record = records[0]
    raw = generate_token()
    result = await resource.provider.update(
        record.pk, {"token_hash": hash_token(raw), "rotated_at": utcnow()}, ctx
    )
    if result.failed:
        return ActionResult(message=result.message or "The token could not be rotated.",
                            level="error", refresh=False)

    return _token_report(
        raw, str(record.get("name") or "this token"),
        clock.parse(record.get("expires_at")), verb="Rotated",
    )


def _expiry_from(value: object) -> tuple[datetime | None, str]:
    """Read the prompt's "days" box. Returns the instant, or why it was refused."""
    text = str(value or "").strip()
    if not text:
        return None, ""
    try:
        days = int(text)
    except ValueError:
        return None, f"{text!r} is not a number of days."
    if days <= 0:
        return None, "A token has to last at least a day."
    return utcnow() + timedelta(days=days), ""


def _token_report(
    raw: str, label: str, expires_at: datetime | None, *, verb: str
) -> ActionResult:
    """The one showing of a token's value."""
    when = f"It expires on {expires_at:%d %b %Y}." if expires_at else "It never expires."
    return ActionResult(
        message=(
            f"{verb} {label}. Copy this value now -- only its hash is stored, "
            f"so it cannot be shown again. {when}"
        ),
        level="info",
        template="views/_action_report.html",
        data={
            "headings": ("Token",),
            "rows": ((raw,),),
        },
    )
