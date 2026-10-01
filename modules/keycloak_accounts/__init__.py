"""Who holds the staff roles in a Keycloak realm, and signing in as them.

The people who may open a back office are often not rows in a table here: they
are accounts in an identity provider that already creates, disables and gives
them roles. Copying them into a local table makes a second answer to "who has
access", which is one answer too many. So this module declares Keycloak *as a
resource*: the Admin API is the backing store, reached through the ``rest``
provider, and the screens are the same list and detail screens as everything
else. It is optional, and off until named in ``CRM_MODULES``.

**What it adds**

* ``keycloak_users`` -- the holders of ``CRM_KEYCLOAK_ROLES``, read-only. It is
  also declared as the application's staff directory, so ``@mention`` and
  unassigned-task alerts resolve against it.
* ``impersonation_log`` and an *Impersonate* action -- sign in as an account,
  with a record of who did it and when. Other modules attach the same action to
  their own resources with :func:`impersonate_action`, supplying the one thing
  only they know: which Keycloak account a record stands for.
* *Grant / Revoke impersonation* -- hand out and take back Keycloak's
  ``impersonation`` role.

**Configuration** is ``CRM_KEYCLOAK_*`` (see ``docs/configuration.md``) plus a
REST connection named by ``CRM_KEYCLOAK_CONNECTION`` (``api.keycloak`` in the
shipped ``connections.yaml``) carrying admin credentials for the realm.

## What the Admin API cannot do

``GET /admin/realms/<realm>/users`` offers a substring ``search``,
``first``/``max`` paging, and nothing else -- no sorting, no filtering by role,
no total in the body. The provider declares exactly that, and the capability
shim supplies the rest in Python. The cost is real: the shim can only work on
rows it has, so sorting or filtering a realm of several thousand accounts pulls
up to ``CRM_MAX_LOCAL_ROWS`` of them. Searching is pushed down and stays cheap.

## Roles: realm or client

A **realm** role's members are at ``/roles/<role>/users``. A **client** role's
are nested under the client that owns it, because two clients may each define a
role called ``admin``. Set ``CRM_KEYCLOAK_CLIENT`` for client roles; the
client's internal uuid -- which is not its clientId -- is looked up once at
startup (or given as ``CRM_KEYCLOAK_CLIENT_UUID``).

## Why impersonation uses the operator's own token

Keycloak's admin console has the same button and it works because the click
happens on a page served from Keycloak's own origin. This application is a
different origin, and the impersonation endpoint's SSO cookies are scoped to
``Path=/realms/<realm>/``, so a form post never carries them. What authenticates
the endpoint is a Bearer token, so the redirect page does a ``fetch()`` with
the *operator's own* access token (``CRM_OIDC_KEEP_ACCESS_TOKEN=true``), never a
shared credential: Keycloak's admin-events log then names the person who
clicked, and Keycloak itself refuses anyone who is not really allowed to.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from app.auth.oidc import _decode_id_token
from app.core.errors import ConfigError
from app.core.registry import Registry, StaffDirectory
from app.core.results import Ctx, Record
from app.fields.base import Field
from app.fields.types import (
    BooleanField,
    ComputedField,
    DateTimeField,
    EmailField,
    JSONField,
    SelectField,
    TextField,
)
from app.providers.union import Union, UnionSource, source_choices
from app.resources.actions import Action, ActionResult
from app.resources.policy import RolePolicy
from app.resources.resource import Resource
from app.resources.views import Column, DetailView, ListView, SearchSpec, Section

from . import schema  # noqa: F401 -- registers `impersonation_log` on app.schema.metadata

MANIFEST = {
    "name": "keycloak_accounts",
    "label": "Keycloak accounts",
    "description": "Who holds the staff roles in a Keycloak realm, and impersonating them.",
    "depends": ("core_access",),
    "menu_groups": {"Access": 80},
    "optional": True,
}

log = logging.getLogger(__name__)

#: Who may sign in as someone, or change who may. Deliberately the same bar as
#: reading the account list at all; Keycloak's own authorisation on the
#: endpoint is the check that actually matters, and this one exists so the
#: button is not offered to someone who would only be refused by it.
IMPERSONATE_ROLES = ("admin",)

#: The Keycloak role this module hands out and takes back. It belongs to the
#: realm's management client, so granting it is a role *mapping* on a client.
IMPERSONATION_ROLE = "impersonation"

#: What in the caller's own token counts as "has Keycloak admin access". A
#: pre-check only, to fail with a readable sentence: Keycloak's answer to the
#: actual call is the one that decides.
_REALM_ADMIN_ROLES = frozenset({"admin"})
_MANAGEMENT_ADMIN_ROLES = frozenset({"realm-admin", "manage-users", "map-roles"})

#: Swapped for a ``MockTransport`` in tests. ``None`` means the real network.
_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True, slots=True)
class KeycloakConfig:
    url: str = ""
    realm: str = "master"
    connection: str = "api.keycloak"
    roles: tuple[str, ...] = ("admin",)
    client: str = ""
    client_uuid: str = ""
    frontend_url: str = ""

    @classmethod
    def from_settings(cls, settings: Any) -> KeycloakConfig:
        return cls(
            url=str(settings.keycloak_url).rstrip("/"),
            realm=settings.keycloak_realm,
            connection=settings.keycloak_connection,
            roles=tuple(settings.keycloak_roles),
            client=settings.keycloak_client.strip(),
            client_uuid=settings.keycloak_client_uuid.strip(),
            frontend_url=str(settings.keycloak_frontend_url).rstrip("/"),
        )

    @property
    def management_client(self) -> str:
        return "master-realm" if self.realm == "master" else "realm-management"


def register(registry: Registry) -> None:
    settings = registry.settings
    if settings is None:
        from app.settings import get_settings

        settings = get_settings()
    cfg = KeycloakConfig.from_settings(settings)
    if not cfg.roles:
        raise ConfigError("CRM_KEYCLOAK_ROLES is empty; name at least one role to list")

    registry.add_resource(_impersonation_log())
    accounts = registry.add_resource(_accounts(cfg))
    accounts.add_action(impersonate_action(_account_target, cfg))
    accounts.add_action(_grant_action(cfg))
    accounts.add_action(_revoke_action(cfg))
    registry.staff_directory = StaffDirectory("keycloak_users", roles=cfg.roles)


# -- The accounts screen -------------------------------------------------------


def _members_of(cfg: KeycloakConfig, role: str) -> str:
    """Where the members of one staff role live."""
    if cfg.client:
        return f"{cfg.connection}#/admin/realms/{cfg.realm}/clients/{{client_uuid}}/roles/{role}/users"
    return f"{cfg.connection}#/admin/realms/{cfg.realm}/roles/{role}/users"


def _client_uuid_resolver(cfg: KeycloakConfig) -> Callable[[Any, str], Awaitable[str]]:
    """Build a ``path_resolver`` that fills in ``{client_uuid}`` once.

    A closure rather than a module-level cache: ``register()`` runs once per
    application build, and a cache shared across builds would let a uuid
    resolved against one test's mock Keycloak leak into the next. Within one
    running application every role source shares it, so only the first pays.
    """
    cache: dict[str, str] = {}

    async def resolve(connection: Any, path: str) -> str:
        if "{client_uuid}" not in path:
            return path
        uuid = cfg.client_uuid or cache.get(cfg.client)
        if not uuid:
            response = await connection.client.get(
                f"/admin/realms/{cfg.realm}/clients", params={"clientId": cfg.client}
            )
            if response.status_code >= 400:
                raise ConfigError(
                    f"looking up the Keycloak client {cfg.client!r} in realm {cfg.realm!r} "
                    f"returned HTTP {response.status_code}"
                )
            rows = response.json()
            if not rows:
                raise ConfigError(
                    f"no Keycloak client named {cfg.client!r} in realm {cfg.realm!r}; check "
                    f"CRM_KEYCLOAK_CLIENT, or set CRM_KEYCLOAK_CLIENT_UUID directly"
                )
            uuid = str(rows[0]["id"])
            cache[cfg.client] = uuid
        return path.format(client_uuid=uuid)

    return resolve


def _role_backing(cfg: KeycloakConfig) -> tuple[Any, Field]:
    """The provider for the configured roles, and the field naming the role.

    Keycloak cannot answer "users holding any of these roles", but it has an
    endpoint per role, and merging several backends into one set of screens is
    what ``Union`` is for. A union needs two sources, though, and one role is a
    perfectly ordinary configuration, so a single role reads from that endpoint
    directly and the role is computed rather than stamped. Somebody holding two
    of the listed roles appears once per role: a row records one membership.
    """
    if len(cfg.roles) == 1:
        only = cfg.roles[0]
        return _members_of(cfg, only), ComputedField(
            "role", label="Role", compute=lambda record: only, in_filter=False
        )
    union = Union(
        *(UnionSource(_members_of(cfg, role), label=role) for role in cfg.roles),
        source_field="role",
    )
    return union, SelectField("role", label="Role", choices=source_choices(union), in_filter=True)


def _accounts(cfg: KeycloakConfig) -> Resource:
    """The people who hold the staff roles.

    ``createdTimestamp`` arrives as epoch milliseconds and ``attributes`` as an
    object of string lists -- both Keycloak's shapes, rendered as what they are
    rather than coerced into something that would quietly lose information.
    """
    backing, role_field = _role_backing(cfg)
    resource = Resource(
        "keycloak_users",
        provider=backing,
        label="Account",
        label_plural="Sign-in accounts",
        icon="◇",
        menu_group="Access",
        menu_order=10,
        display_field="username",
        default_sort=["username"],
        policy=RolePolicy(read=IMPERSONATE_ROLES),
        # Nothing here is written, so there is nothing to record.
        audited=False,
        fields=[
            TextField("id", label="Keycloak ID", in_list=False),
            TextField("username", searchable=True),
            role_field,
            EmailField("email", searchable=True),
            TextField("firstName", label="First name", searchable=True),
            TextField("lastName", label="Surname", searchable=True),
            BooleanField("enabled", in_filter=True),
            BooleanField("emailVerified", label="Email verified", in_filter=True),
            DateTimeField("createdTimestamp", label="Created", in_list=False),
            JSONField("attributes", label="Attributes", in_list=False,
                      help="Custom attributes. A mapper turns one of these into "
                           "a token claim, which is what a policy can act on."),
        ],
        search=SearchSpec(
            fields=("username", "email", "firstName", "lastName"),
            filters=("enabled", "emailVerified"),
            placeholder="Username, email or name",
        ),
        views=[
            ListView(
                columns=[
                    Column("username", link=True, width="24%"),
                    "role", "email", "firstName", "lastName", "enabled", "emailVerified",
                ],
                default_sort=["username"],
                inline_edit=False,
            ),
            DetailView(
                sections=[
                    Section("Account", ["username", "email", "firstName", "lastName"], columns=2),
                    Section("Access", ["role"], columns=1),
                    Section("Status", ["enabled", "emailVerified", "createdTimestamp"], columns=3),
                    Section("Attributes", ["attributes"], columns=1),
                    Section("Keycloak", ["id"], columns=1),
                ],
                title_field="username",
                subtitle_field="email",
            ),
        ],
    )
    resource.rest_mapping = {
        # No "path": each union source carries its own.
        #
        # The body is the array itself, with no envelope and no total: Keycloak
        # reports a count from a separate endpoint this provider cannot call.
        "items_key": "",
        "total_key": "",
        "pagination": "offset",
        "offset_param": "first",
        "size_param": "max",
        # One account is fetched from /users rather than from underneath a
        # role, because a member of a role is not addressable at
        # /roles/<role>/users/<id>.
        "detail_path": f"/admin/realms/{cfg.realm}/users/{{pk}}",
        "path_resolver": _client_uuid_resolver(cfg) if cfg.client else None,
    }
    return resource


# -- Impersonation ---------------------------------------------------------------

#: What a target resolver hands back: the Keycloak id, and a name and email to
#: show and log. ``None`` means this record has no Keycloak account.
Target = tuple[str, str | None, str | None]
TargetResolver = Callable[[Record, Ctx, Resource], Awaitable[Target | None]]


async def _account_target(record: Record, ctx: Ctx, resource: Resource) -> Target | None:
    """On the accounts screen the row *is* the Keycloak account."""
    keycloak_id = record.get("id")
    if not keycloak_id:
        return None
    return str(keycloak_id), record.get("username"), record.get("email")


def impersonate_action(
    resolve: TargetResolver, cfg: KeycloakConfig | None = None, *, label: str = "Impersonate"
) -> Action:
    """An *Impersonate* action for any resource.

    ``resolve(record, ctx, resource)`` answers the one question only the
    caller's resource can: which Keycloak account does this record stand for?
    Return ``(keycloak_id, username, email)``, or ``None`` if it has none.
    ``cfg`` defaults to one built from the application's settings when the
    action runs.
    """

    async def handler(records: Sequence[Record], ctx: Ctx, resource: Resource) -> ActionResult:
        config = cfg or KeycloakConfig.from_settings(_settings_of(resource))
        if not config.url:
            return _refusal("CRM_KEYCLOAK_URL is not configured; impersonation is unavailable.")
        if len(records) != 1:
            return _refusal("Impersonate one account at a time.")
        target = await resolve(records[0], ctx, resource)
        if target is None:
            return _refusal("This record has no Keycloak account to impersonate.")
        keycloak_id, username, email = target
        return await _log_and_redirect(
            config, resource, ctx, keycloak_id=keycloak_id, username=username, email=email
        )

    return Action(
        "impersonate",
        label,
        handler=handler,
        icon="◎",
        placements=("row", "detail"),
        confirm="Sign in as this account? This is logged, and everything you "
                "do afterwards is recorded as done by you on their behalf.",
        roles=IMPERSONATE_ROLES,
        style="danger",
    )


def _settings_of(resource: Resource) -> Any:
    settings = getattr(resource.registry, "settings", None)
    if settings is None:
        from app.settings import get_settings

        settings = get_settings()
    return settings


def _refusal(message: str) -> ActionResult:
    return ActionResult(message=message, level="error", refresh=False)


async def _log_and_redirect(
    cfg: KeycloakConfig, resource: Resource, ctx: Ctx, *,
    keycloak_id: str, username: str | None, email: str | None,
) -> ActionResult:
    """The steps every impersonation does once a Keycloak id is known: check
    there is a Bearer token to act with, write the audit row, then hand the
    browser a page that calls Keycloak's endpoint with it."""
    access_token = ctx.extra.get("access_token") if ctx.extra else None
    if not access_token:
        return _refusal(
            "CRM_OIDC_KEEP_ACCESS_TOKEN is not enabled; impersonation needs "
            "the operator's own Keycloak token to call the endpoint as them."
        )

    await resource.registry.resource("impersonation_log").provider.create(
        {
            "impersonator_email": ctx.identity.email or ctx.identity.subject,
            "impersonator_name": ctx.identity.display_name,
            "target_keycloak_id": keycloak_id,
            "target_username": username,
            "target_email": email,
        },
        ctx,
    )
    return ActionResult(
        message=f"Impersonating {username or email or keycloak_id}.",
        template="impersonation/redirect.html",
        data={
            "impersonate_url": f"{cfg.url}/admin/realms/{cfg.realm}/users/{keycloak_id}/impersonation",
            "frontend_url": cfg.frontend_url,
            "keycloak_account_url": f"{cfg.url}/realms/{cfg.realm}/account",
            "access_token": access_token,
        },
        refresh=False,
    )


def _impersonation_log() -> Resource:
    return Resource(
        "impersonation_log",
        provider="db.main#impersonation_log",
        label="Impersonation",
        label_plural="Impersonation log",
        icon="◎",
        menu_group="Access",
        menu_order=20,
        display_field="target_username",
        default_sort=["-created_at"],
        # Written only by the action above, via the provider directly. Nothing
        # here offers create/update/delete, so there is no second way for this
        # table to acquire a row.
        policy=RolePolicy(read=IMPERSONATE_ROLES),
        fields=[
            TextField("id", label="ID", in_form=False, in_list=False),
            EmailField("impersonator_email", label="Staff member", searchable=True),
            TextField("impersonator_name", label="Staff name", in_list=False),
            TextField("target_keycloak_id", label="Target Keycloak ID", in_list=False),
            TextField("target_username", label="Target", searchable=True),
            EmailField("target_email", in_list=False, searchable=True),
            DateTimeField("created_at", label="When", readonly=True, in_form=False),
        ],
        views=[
            ListView(
                columns=[
                    Column("created_at", label="When", link=True, width="18%"),
                    "impersonator_email", "target_username", "target_email",
                ],
                default_sort=["-created_at"],
                inline_edit=False,
            ),
            DetailView(
                sections=[
                    Section("Who", ["impersonator_email", "impersonator_name"], columns=2),
                    Section("As whom", ["target_username", "target_email", "target_keycloak_id"],
                            columns=1),
                    Section("When", ["created_at"], columns=1),
                ],
                title_field="target_username",
            ),
        ],
    )


# -- Granting and revoking the role itself ---------------------------------------
#
# The impersonate action *uses* impersonation; these decide who may. Every call
# is made with the operator's own access token, never the shared admin
# credential the accounts list is read with: Keycloak's admin-events log then
# names the person who clicked, and Keycloak itself refuses the change unless
# that person really holds admin rights. The role check on the button and
# `_has_keycloak_admin` are courtesy; a 403 from Keycloak is the authority.


def _has_keycloak_admin(cfg: KeycloakConfig, access_token: str) -> bool:
    """Whether the caller's own token carries Keycloak admin rights.

    Read from the access token, not from ``ctx.identity.claims``: which claims
    the identity keeps depends on ``CRM_OIDC_ROLES_CLAIM``, and this needs both
    the realm roles and the management client's.
    """
    claims = _decode_id_token(access_token)
    if set((claims.get("realm_access") or {}).get("roles") or ()) & _REALM_ADMIN_ROLES:
        return True
    client_roles = set(
        ((claims.get("resource_access") or {}).get(cfg.management_client) or {}).get("roles") or ()
    )
    return bool(client_roles & _MANAGEMENT_ADMIN_ROLES)


def _grant_action(cfg: KeycloakConfig) -> Action:
    async def handler(records: Sequence[Record], ctx: Ctx, resource: Resource) -> ActionResult:
        return await _change_impersonation_role(cfg, records, ctx, grant=True)

    return Action(
        "grant_impersonation",
        "Grant impersonation",
        handler=handler,
        icon="◎",
        placements=("row", "detail"),
        confirm="Let this person impersonate any user in Keycloak? They will be able "
                "to sign in as other accounts, and can do so until you revoke it.",
        roles=IMPERSONATE_ROLES,
        style="danger",
    )


def _revoke_action(cfg: KeycloakConfig) -> Action:
    async def handler(records: Sequence[Record], ctx: Ctx, resource: Resource) -> ActionResult:
        return await _change_impersonation_role(cfg, records, ctx, grant=False)

    return Action(
        "revoke_impersonation",
        "Revoke impersonation",
        handler=handler,
        icon="◎",
        placements=("row", "detail"),
        confirm="Remove this person's ability to impersonate users? Sessions they "
                "already opened stay valid until they expire.",
        roles=IMPERSONATE_ROLES,
    )


async def _change_impersonation_role(
    cfg: KeycloakConfig, records: Sequence[Record], ctx: Ctx, *, grant: bool
) -> ActionResult:
    if not cfg.url:
        return _refusal("CRM_KEYCLOAK_URL is not configured; this is unavailable.")
    if len(records) != 1:
        return _refusal("Change one account at a time.")
    target = records[0]
    target_id = target.get("id")
    if not target_id:
        return _refusal("This row has no Keycloak ID.")
    access_token = ctx.extra.get("access_token") if ctx.extra else None
    if not access_token:
        return _refusal(
            "CRM_OIDC_KEEP_ACCESS_TOKEN is not enabled; this needs your own Keycloak "
            "token so Keycloak can check you are an administrator."
        )
    if not _has_keycloak_admin(cfg, access_token):
        return _refusal(
            "Your Keycloak account does not have admin access, so you cannot change "
            "who may impersonate. Sign out and in again if it was granted recently."
        )
    if str(target_id) == str(ctx.identity.subject) and not grant:
        return _refusal("You cannot revoke your own impersonation role.")

    who = target.get("username") or target.get("email") or str(target_id)
    admin = f"/admin/realms/{cfg.realm}"
    try:
        async with httpx.AsyncClient(
            base_url=cfg.url, timeout=20, transport=_transport,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as client:
            clients = await client.get(
                f"{admin}/clients", params={"clientId": cfg.management_client}
            )
            if clients.status_code >= 400 or not clients.json():
                return _keycloak_refusal(clients, f"find the {cfg.management_client} client")
            client_uuid = clients.json()[0]["id"]
            role = await client.get(f"{admin}/clients/{client_uuid}/roles/{IMPERSONATION_ROLE}")
            if role.status_code >= 400:
                return _keycloak_refusal(role, f"read the {IMPERSONATION_ROLE} role")
            mapping = f"{admin}/users/{target_id}/role-mappings/clients/{client_uuid}"
            response = await client.request(
                "POST" if grant else "DELETE", mapping, json=[role.json()]
            )
    except httpx.HTTPError as exc:
        log.warning("impersonation role change failed: %s", exc)
        return _refusal(f"Keycloak could not be reached: {exc}")

    verb = "grant" if grant else "revoke"
    # Who changed whose access is the record of this feature; Keycloak's admin
    # events say the same, but only if that logging is switched on.
    log.info(
        "impersonation role %s: target %s (%s) by %s (request %s) -> %s",
        verb, target_id, who, ctx.identity.email or ctx.identity.subject,
        ctx.request_id, response.status_code,
    )
    if response.status_code >= 400:
        return _keycloak_refusal(response, f"{verb} the {IMPERSONATION_ROLE} role")
    return ActionResult(
        message=f"{who} can now impersonate users." if grant
        else f"{who} can no longer impersonate users.",
        level="success",
    )


def _keycloak_refusal(response: httpx.Response, doing: str) -> ActionResult:
    if response.status_code in (401, 403):
        return _refusal(
            f"Keycloak refused to {doing}: your account lacks the rights for it. "
            "Sign out and in again if they were granted recently."
        )
    return _refusal(f"Keycloak could not {doing} (HTTP {response.status_code}).")
