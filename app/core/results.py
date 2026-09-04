"""Records, write results and caller identity.

The important type here is :class:`WriteResult`. Backends differ in whether a
write is durable by the time it returns: a SQL UPDATE is, a message published to
a queue is not. Rather than pretend otherwise, writes carry a tri-state status so
one form pipeline can serve both.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self


class Record(Mapping[str, Any]):
    """An immutable read-only mapping of one row, plus its primary key.

    Providers return these instead of raw dicts so views have a stable place to
    ask for the primary key without knowing which column holds it.
    """

    __slots__ = ("_data", "_pk_field")

    def __init__(self, data: Mapping[str, Any], pk_field: str = "id") -> None:
        self._data: dict[str, Any] = dict(data)
        self._pk_field = pk_field

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"Record({self._data!r}, pk={self.pk!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Record):
            return self._data == other._data and self._pk_field == other._pk_field
        if isinstance(other, Mapping):
            return self._data == dict(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._pk_field, tuple(sorted(self._data.items(), key=lambda kv: kv[0]))))

    @property
    def pk(self) -> Any:
        """The record's primary key, or ``None`` if the column is absent."""
        return self._data.get(self._pk_field)

    @property
    def pk_field(self) -> str:
        return self._pk_field

    def as_dict(self) -> dict[str, Any]:
        """A mutable copy of the underlying data."""
        return dict(self._data)

    def merge(self, changes: Mapping[str, Any]) -> Record:
        """A new record with ``changes`` applied on top."""
        return Record({**self._data, **changes}, self._pk_field)

    def subset(self, fields: Iterable[str]) -> Record:
        """A new record limited to ``fields``, always keeping the primary key."""
        keep = set(fields) | {self._pk_field}
        return Record({k: v for k, v in self._data.items() if k in keep}, self._pk_field)



class WriteStatus(StrEnum):
    """Outcome of a write.

    ``PENDING`` is not an error. It means the backend accepted the request but
    cannot confirm the resulting state -- a message was queued, or an upstream
    API returned 202. Views render this as "queued" rather than as a saved row.
    """

    OK = "ok"
    PENDING = "pending"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class WriteResult:
    """What a create/update/delete returns."""

    status: WriteStatus
    record: Record | None = None
    errors: dict[str, str] = field(default_factory=dict)
    message: str = ""
    correlation_id: str | None = None

    @property
    def ok(self) -> bool:
        """True when the write succeeded outright. ``PENDING`` is not ``ok``."""
        return self.status is WriteStatus.OK

    @property
    def accepted(self) -> bool:
        """True when the write was not rejected -- succeeded or queued."""
        return self.status in (WriteStatus.OK, WriteStatus.PENDING)

    @property
    def failed(self) -> bool:
        return self.status is WriteStatus.ERROR

    @classmethod
    def success(cls, record: Record | None = None, message: str = "") -> Self:
        return cls(WriteStatus.OK, record=record, message=message)

    @classmethod
    def pending(
        cls,
        record: Record | None = None,
        message: str = "Queued for processing.",
        correlation_id: str | None = None,
    ) -> Self:
        return cls(
            WriteStatus.PENDING,
            record=record,
            message=message,
            correlation_id=correlation_id or uuid.uuid4().hex,
        )

    @classmethod
    def invalid(cls, errors: Mapping[str, str], message: str = "") -> Self:
        """A rejected write with per-field validation messages."""
        return cls(
            WriteStatus.ERROR,
            errors=dict(errors),
            message=message or "Please correct the highlighted fields.",
        )

    @classmethod
    def failure(cls, message: str, errors: Mapping[str, str] | None = None) -> Self:
        return cls(WriteStatus.ERROR, errors=dict(errors or {}), message=message)


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is making the request, normalised across every auth provider."""

    subject: str
    email: str = ""
    display_name: str = ""
    roles: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = field(default_factory=dict)
    provider: str = ""
    is_authenticated: bool = True
    #: The zone this person reads times in. Presentation only: stored instants
    #: are always UTC.
    timezone: str = "UTC"
    locale: str = "en"
    #: Set when the credential in use is a temporary one that must be replaced
    #: before anything else. Carried on the identity so it survives into the
    #: session cookie: the check has to happen on every request, not only on
    #: the one that signed in.
    must_change_password: bool = False

    def replace(self, **changes: Any) -> Identity:
        from dataclasses import replace as _replace

        return _replace(self, **changes)

    @property
    def label(self) -> str:
        """Best available human-readable name."""
        return self.display_name or self.email or self.subject

    @property
    def initials(self) -> str:
        source = self.display_name or self.email or self.subject
        parts = [p for p in source.replace(".", " ").replace("_", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def has_role(self, *roles: str) -> bool:
        """True if the identity holds any of ``roles``. ``admin`` satisfies all."""
        return "admin" in self.roles or bool(self.roles.intersection(roles))

    def has_all_roles(self, *roles: str) -> bool:
        return "admin" in self.roles or set(roles).issubset(self.roles)


#: The caller when nothing authenticated them. Policies treat this as no access.
ANONYMOUS = Identity(
    subject="anonymous",
    display_name="Anonymous",
    roles=frozenset(),
    provider="",
    is_authenticated=False,
)


@dataclass(frozen=True, slots=True)
class Ctx:
    """Per-request context handed to every provider call.

    Providers receive this rather than the raw ``Request`` so they stay usable
    from the CLI, background jobs and tests.
    """

    identity: Identity = ANONYMOUS
    request_id: str = ""
    locale: str = "en"
    timezone: str = "UTC"
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def system(cls) -> Self:
        """Context for trusted internal work (seeding, migrations, jobs)."""
        return cls(
            identity=Identity(
                subject="system",
                display_name="System",
                roles=frozenset({"admin"}),
                provider="system",
            )
        )
