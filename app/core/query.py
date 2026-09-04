"""Backend-neutral query model.

Every view speaks this language; each provider translates it into whatever its
backend understands (SQL WHERE clauses, REST query params, ...). Anything a
provider cannot translate is emulated in Python by ``providers.shim``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Literal


class Op(StrEnum):
    """Comparison operators available to filters."""

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    ICONTAINS = "icontains"
    STARTSWITH = "startswith"
    ENDSWITH = "endswith"
    IS_NULL = "is_null"
    NOT_NULL = "not_null"
    BETWEEN = "between"


#: Operators a provider must support to be considered fully filter-capable.
ALL_OPS: frozenset[Op] = frozenset(Op)

#: Operators that take no ``value``.
UNARY_OPS: frozenset[Op] = frozenset({Op.IS_NULL, Op.NOT_NULL})

#: Operators whose ``value`` is a collection rather than a scalar.
SEQUENCE_OPS: frozenset[Op] = frozenset({Op.IN, Op.NOT_IN, Op.BETWEEN})


@dataclass(frozen=True, slots=True)
class Condition:
    """A single ``field op value`` comparison."""

    field: str
    op: Op
    value: Any = None

    def __post_init__(self) -> None:
        if self.op in SEQUENCE_OPS and not isinstance(self.value, (list, tuple, set, frozenset)):
            raise ValueError(f"operator {self.op.value!r} requires a sequence value")
        if self.op is Op.BETWEEN and len(tuple(self.value)) != 2:
            raise ValueError("operator 'between' requires exactly two values")


@dataclass(frozen=True, slots=True)
class Group:
    """A boolean combination of conditions and nested groups."""

    op: Literal["and", "or"]
    children: tuple[Filter, ...]

    def __post_init__(self) -> None:
        if not self.children:
            raise ValueError("a filter group needs at least one child")


@dataclass(frozen=True, slots=True)
class Not:
    """Negation of a filter."""

    child: Filter


Filter = Condition | Group | Not
"""A filter expression tree. Build it with :func:`and_`, :func:`or_`, :func:`not_`."""


def and_(*children: Filter | None) -> Filter | None:
    """Combine filters with AND, dropping ``None`` and collapsing single children."""
    kept = tuple(c for c in children if c is not None)
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return Group("and", kept)


def or_(*children: Filter | None) -> Filter | None:
    """Combine filters with OR, dropping ``None`` and collapsing single children."""
    kept = tuple(c for c in children if c is not None)
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return Group("or", kept)


def not_(child: Filter | None) -> Filter | None:
    """Negate a filter, passing ``None`` through."""
    return None if child is None else Not(child)


def iter_conditions(f: Filter | None) -> Iterable[Condition]:
    """Yield every :class:`Condition` in a filter tree, depth first."""
    if f is None:
        return
    if isinstance(f, Condition):
        yield f
    elif isinstance(f, Not):
        yield from iter_conditions(f.child)
    else:
        for child in f.children:
            yield from iter_conditions(child)


def filter_ops(f: Filter | None) -> frozenset[Op]:
    """The set of operators used anywhere in a filter tree."""
    return frozenset(c.op for c in iter_conditions(f))


def filter_fields(f: Filter | None) -> frozenset[str]:
    """The set of field names referenced anywhere in a filter tree."""
    return frozenset(c.field for c in iter_conditions(f))


class SortDir(StrEnum):
    ASC = "asc"
    DESC = "desc"

    @property
    def opposite(self) -> SortDir:
        return SortDir.DESC if self is SortDir.ASC else SortDir.ASC


@dataclass(frozen=True, slots=True)
class Sort:
    field: str
    dir: SortDir = SortDir.ASC

    @classmethod
    def parse(cls, token: str) -> Sort:
        """Parse ``"name"`` or ``"-created_at"`` into a :class:`Sort`."""
        token = token.strip()
        if token.startswith("-"):
            return cls(token[1:], SortDir.DESC)
        return cls(token.removeprefix("+"), SortDir.ASC)

    def __str__(self) -> str:
        return f"-{self.field}" if self.dir is SortDir.DESC else self.field


DEFAULT_PAGE_SIZE = 25

#: Largest page a *user* may ask for. Enforced when parsing request parameters,
#: not by ``ListQuery`` itself -- internal machinery such as the capability shim
#: legitimately needs to fetch far larger batches to emulate a query stage.
MAX_PAGE_SIZE = 500


def clamp_page_size(value: int | None, default: int = DEFAULT_PAGE_SIZE) -> int:
    """Bound a caller-supplied page size to something safe to serve."""
    if not value or value < 1:
        return default
    return min(value, MAX_PAGE_SIZE)


@dataclass(frozen=True, slots=True)
class ListQuery:
    """Everything a list-style read needs.

    ``scope`` is kept separate from ``filter`` deliberately: it holds the
    row-level restriction derived from the caller's identity, so authorization
    can never be dropped by a view rebuilding its user filters.
    """

    filter: Filter | None = None
    scope: Filter | None = None
    search: str | None = None
    sort: tuple[Sort, ...] = ()
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    fields: tuple[str, ...] | None = None
    group_by: str | None = None
    with_total: bool = True

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("page is 1-based")
        if self.page_size < 1:
            raise ValueError("page_size must be at least 1")

    @property
    def effective_filter(self) -> Filter | None:
        """User filter AND identity scope. Providers must read this, not ``filter``."""
        return and_(self.filter, self.scope)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size

    def with_(self, **changes: Any) -> ListQuery:
        """Return a copy with the given attributes replaced."""
        return replace(self, **changes)

    def unpaged(self, limit: int) -> ListQuery:
        """A copy asking for up to ``limit`` rows from page one.

        Used by the capability shim when it must pull rows to filter or sort
        them locally. ``limit`` is the caller's memory budget, so it is required
        rather than defaulted -- an unbounded fetch should never be implicit.
        """
        return replace(self, page=1, page_size=limit)


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results plus enough context to render pagination."""

    items: Sequence[T]
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    total: int | None = None
    has_more: bool | None = None
    cursor: str | None = None

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    @property
    def total_pages(self) -> int | None:
        if self.total is None:
            return None
        return max(1, -(-self.total // self.page_size))

    @property
    def has_next(self) -> bool:
        if self.has_more is not None:
            return self.has_more
        pages = self.total_pages
        return pages is not None and self.page < pages

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def start_index(self) -> int:
        """1-based index of the first item, for "showing X-Y of Z" labels."""
        return (self.page - 1) * self.page_size + 1 if self.items else 0

    @property
    def end_index(self) -> int:
        return (self.page - 1) * self.page_size + len(self.items)

    @classmethod
    def empty(cls, q: ListQuery) -> Page[T]:
        return cls(items=[], page=q.page, page_size=q.page_size, total=0, has_more=False)


class Agg(StrEnum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class Measure:
    agg: Agg
    field: str | None = None
    alias: str = ""

    def __post_init__(self) -> None:
        if self.agg is not Agg.COUNT and not self.field:
            raise ValueError(f"aggregate {self.agg.value!r} requires a field")
        if not self.alias:
            object.__setattr__(self, "alias", f"{self.agg.value}_{self.field or 'all'}")


@dataclass(frozen=True, slots=True)
class AggSpec:
    """A group-by request, backing the chart and pivot views."""

    group_by: tuple[str, ...] = ()
    measures: tuple[Measure, ...] = (Measure(Agg.COUNT),)
    filter: Filter | None = None
    scope: Filter | None = None
    sort: tuple[Sort, ...] = ()
    limit: int | None = None

    @property
    def effective_filter(self) -> Filter | None:
        return and_(self.filter, self.scope)
