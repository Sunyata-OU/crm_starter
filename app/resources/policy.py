"""Authorization for a resource.

Two distinct questions live here, and keeping them apart matters:

* *May this person perform this operation?* -- a yes/no check.
* *Which rows may this person see at all?* -- a filter, applied to every read.

The second is the important one. Returning it as a filter, which the route folds
into ``ListQuery.scope``, means row-level restrictions are enforced by
construction rather than by each view remembering to check.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal

from app.core.query import Condition, Filter, Op
from app.core.results import Identity, Record

if TYPE_CHECKING:
    from app.resources.resource import Resource

Operation = Literal["read", "create", "update", "delete"]

ALL_OPERATIONS: tuple[Operation, ...] = ("read", "create", "update", "delete")

#: A filter that matches nothing, used to deny all rows without special-casing.
DENY_ALL: Filter = Condition("1", Op.EQ, "\x00__deny__")


class Policy:
    """Default policy: any authenticated caller may do anything.

    Subclass to tighten, or use :class:`RolePolicy` / :class:`OwnerPolicy` for
    the two common shapes.
    """

    def allows(self, op: Operation, identity: Identity, record: Record | None = None) -> bool:
        """Whether ``identity`` may perform ``op``.

        ``record`` is supplied for per-row checks on update and delete, and is
        ``None`` for create and for list-level reads.
        """
        return identity.is_authenticated

    def scope(self, identity: Identity) -> Filter | None:
        """The row restriction for this caller, or ``None`` for unrestricted.

        Applied to every list, detail read and aggregate.
        """
        return None

    def readable_fields(self, identity: Identity, resource: Resource) -> tuple[str, ...]:
        """Field names this caller may see. Defaults to every visible field."""
        return tuple(f.name for f in resource.fields if f.visible_to(identity))

    def writable_fields(self, identity: Identity, resource: Resource) -> tuple[str, ...]:
        """Field names this caller may change."""
        return tuple(f.name for f in resource.fields if f.writable_by(identity))

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


class PublicPolicy(Policy):
    """Readable without signing in; writes still require authentication."""

    def allows(self, op: Operation, identity: Identity, record: Record | None = None) -> bool:
        return True if op == "read" else identity.is_authenticated


class ReadOnlyPolicy(Policy):
    """No writes, whoever is asking."""

    def allows(self, op: Operation, identity: Identity, record: Record | None = None) -> bool:
        return op == "read" and identity.is_authenticated


class RolePolicy(Policy):
    """Each operation requires one of a set of roles."""

    def __init__(
        self,
        *,
        read: Sequence[str] = (),
        create: Sequence[str] = (),
        update: Sequence[str] = (),
        delete: Sequence[str] = (),
        write: Sequence[str] = (),
    ) -> None:
        # `write` is shorthand for the three mutating operations.
        self.requirements: dict[Operation, frozenset[str]] = {
            "read": frozenset(read),
            "create": frozenset(create or write),
            "update": frozenset(update or write),
            "delete": frozenset(delete or write),
        }

    def allows(self, op: Operation, identity: Identity, record: Record | None = None) -> bool:
        if not identity.is_authenticated:
            return False
        required = self.requirements.get(op, frozenset())
        return not required or identity.has_role(*required)


class OwnerPolicy(Policy):
    """Callers see and edit only rows they own, unless they hold a bypass role.

    The canonical row-level restriction: a sales rep sees their own accounts, a
    manager sees the whole team's.
    """

    def __init__(
        self,
        owner_field: str = "owner",
        *,
        #: Which attribute of the identity is compared to ``owner_field``.
        identity_attr: Literal["subject", "email"] = "subject",
        #: Holders of any of these roles bypass the restriction entirely.
        bypass_roles: Sequence[str] = ("admin", "manager"),
        #: Whether non-owners may read rows they do not own.
        read_all: bool = False,
    ) -> None:
        self.owner_field = owner_field
        self.identity_attr = identity_attr
        self.bypass_roles = frozenset(bypass_roles)
        self.read_all = read_all

    def _owner_value(self, identity: Identity) -> str:
        return getattr(identity, self.identity_attr)

    def _bypasses(self, identity: Identity) -> bool:
        return identity.has_role(*self.bypass_roles)

    def scope(self, identity: Identity) -> Filter | None:
        if not identity.is_authenticated:
            return DENY_ALL
        if self.read_all or self._bypasses(identity):
            return None
        return Condition(self.owner_field, Op.EQ, self._owner_value(identity))

    def allows(self, op: Operation, identity: Identity, record: Record | None = None) -> bool:
        if not identity.is_authenticated:
            return False
        if op == "create" or self._bypasses(identity):
            return True
        if record is None:
            # A list-level check; individual rows are still narrowed by scope().
            return True
        return record.get(self.owner_field) == self._owner_value(identity)


class CustomPolicy(Policy):
    """Build a policy from two callables, without declaring a class."""

    def __init__(
        self,
        *,
        allows: Callable[[Operation, Identity, Record | None], bool] | None = None,
        scope: Callable[[Identity], Filter | None] | None = None,
    ) -> None:
        self._allows = allows
        self._scope = scope

    def allows(self, op: Operation, identity: Identity, record: Record | None = None) -> bool:
        if self._allows is None:
            return identity.is_authenticated
        return self._allows(op, identity, record)

    def scope(self, identity: Identity) -> Filter | None:
        return None if self._scope is None else self._scope(identity)
