"""Role definitions and the checks the web layer performs.

Roles are plain strings; what they mean is up to the application. The two
conventions the framework itself relies on are ``admin`` (satisfies every role
check) and ``user`` (the default for a signed-in person).
"""

from __future__ import annotations

from app.core.errors import AuthenticationRequired, PermissionDenied
from app.core.results import Identity
from app.resources.policy import Operation
from app.resources.resource import Resource

ADMIN = "admin"
USER = "user"

#: Roles the starter ships with. Applications add their own freely.
BUILT_IN_ROLES: tuple[str, ...] = (ADMIN, "manager", USER, "readonly")


def require_authenticated(identity: Identity) -> None:
    if not identity.is_authenticated:
        raise AuthenticationRequired()


def require_roles(identity: Identity, *roles: str) -> None:
    require_authenticated(identity)
    if roles and not identity.has_role(*roles):
        raise PermissionDenied(
            f"This needs one of these roles: {', '.join(sorted(roles))}."
        )


def require_can(resource: Resource, op: Operation, identity: Identity, record=None) -> None:
    """Check a resource operation, distinguishing the two reasons it can fail.

    A caller who is not signed in gets 401 and a login redirect; a caller who
    is signed in but not permitted gets 403. Collapsing them into one status
    sends signed-in users round a login loop they cannot escape.
    """
    require_authenticated(identity)
    if resource.policy.allows(op, identity, record):
        # Policy allows it, but the backend may not support it.
        if not resource.can(op, identity, record):
            raise PermissionDenied(
                f"{resource.label_plural} cannot be {_past_tense(op)} through this data source."
            )
        return
    raise PermissionDenied(f"You cannot {op} {resource.label_plural.lower()}.")


def _past_tense(op: Operation) -> str:
    return {"create": "created", "update": "changed", "delete": "deleted"}.get(op, op)


def visible_fields(resource: Resource, identity: Identity) -> tuple[str, ...]:
    return resource.policy.readable_fields(identity, resource)


def editable_fields(resource: Resource, identity: Identity) -> tuple[str, ...]:
    """Fields this caller may change, intersected with what the backend allows."""
    if not resource.can("update", identity):
        return ()
    return resource.policy.writable_fields(identity, resource)
