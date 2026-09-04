"""Executing the query language in Python.

The shim and the in-memory provider both need to apply filters, search, sorting
and pagination to a plain list of rows. That logic lives here once, so a
backend's emulated behaviour is identical no matter which provider triggered it.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.query import (
    Agg,
    AggSpec,
    Condition,
    Filter,
    ListQuery,
    Not,
    Op,
    Page,
    Sort,
    SortDir,
)

MISSING = object()


def _get(row: Any, field: str) -> Any:
    """Read ``field`` from a record or mapping, returning ``MISSING`` if absent.

    Distinguishing "absent" from "None" matters: ``x is_null`` should be true for
    a stored NULL, and a filter on a field the backend never returned is a bug
    worth surfacing rather than silently matching.
    """
    try:
        return row[field]
    except (KeyError, TypeError, IndexError):
        return MISSING


def _norm(value: Any) -> Any:
    """Coerce a value into something comparable with the usual operators."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    return value


def _comparable(a: Any, b: Any) -> tuple[Any, Any]:
    """Make two values safe to compare with ``<``/``>``.

    Mixed date/datetime and numeric/string pairs are the common cases; anything
    still incomparable falls back to string form so a filter degrades to a
    predictable answer instead of raising ``TypeError`` mid-render.
    """
    if isinstance(a, datetime) and isinstance(b, date) and not isinstance(b, datetime):
        return a, datetime(b.year, b.month, b.day, tzinfo=a.tzinfo)
    if isinstance(b, datetime) and isinstance(a, date) and not isinstance(a, datetime):
        return datetime(a.year, a.month, a.day, tzinfo=b.tzinfo), b
    if isinstance(a, bool) or isinstance(b, bool):
        return a, b
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        return a, b
    if type(a) is type(b):
        return a, b
    return str(a), str(b)


def _cmp(op_fn, a: Any, b: Any) -> bool:
    try:
        left, right = _comparable(a, b)
        return bool(op_fn(left, right))
    except TypeError:
        return False


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def match_condition(row: Any, cond: Condition) -> bool:
    """Evaluate one condition against one row."""
    value = _get(row, cond.field)

    if cond.op is Op.IS_NULL:
        return value is MISSING or value is None
    if cond.op is Op.NOT_NULL:
        return value is not MISSING and value is not None

    if value is MISSING:
        return False

    target = cond.value

    match cond.op:
        case Op.EQ:
            return value == target or _cmp(operator.eq, value, target)
        case Op.NE:
            return not (value == target or _cmp(operator.eq, value, target))
        case Op.LT:
            return value is not None and _cmp(operator.lt, value, target)
        case Op.LTE:
            return value is not None and _cmp(operator.le, value, target)
        case Op.GT:
            return value is not None and _cmp(operator.gt, value, target)
        case Op.GTE:
            return value is not None and _cmp(operator.ge, value, target)
        case Op.IN:
            return any(value == t or _cmp(operator.eq, value, t) for t in target)
        case Op.NOT_IN:
            return not any(value == t or _cmp(operator.eq, value, t) for t in target)
        case Op.BETWEEN:
            low, high = tuple(target)
            return _cmp(operator.ge, value, low) and _cmp(operator.le, value, high)
        case Op.CONTAINS:
            return _as_text(target) in _as_text(value)
        case Op.ICONTAINS:
            return _as_text(target).casefold() in _as_text(value).casefold()
        case Op.STARTSWITH:
            return _as_text(value).casefold().startswith(_as_text(target).casefold())
        case Op.ENDSWITH:
            return _as_text(value).casefold().endswith(_as_text(target).casefold())
    return False


def match(row: Any, f: Filter | None) -> bool:
    """Evaluate a whole filter tree against one row."""
    if f is None:
        return True
    if isinstance(f, Condition):
        return match_condition(row, f)
    if isinstance(f, Not):
        return not match(row, f.child)
    if f.op == "and":
        return all(match(row, child) for child in f.children)
    return any(match(row, child) for child in f.children)


def apply_filter(rows: Iterable[Any], f: Filter | None) -> list[Any]:
    if f is None:
        return list(rows)
    return [row for row in rows if match(row, f)]


_WORD = re.compile(r"\S+")


def apply_search(rows: Iterable[Any], term: str | None, fields: Sequence[str]) -> list[Any]:
    """Case-insensitive AND-of-words search across ``fields``.

    Every whitespace-separated word must appear in at least one searched field,
    which is what users expect from typing two words into a search box.
    """
    rows = list(rows)
    if not term or not term.strip() or not fields:
        return rows
    words = [w.casefold() for w in _WORD.findall(term)]

    def hit(row: Any) -> bool:
        haystack = " ".join(
            _as_text(v).casefold()
            for v in (_get(row, f) for f in fields)
            if v is not MISSING and v is not None
        )
        return all(w in haystack for w in words)

    return [row for row in rows if hit(row)]


class _SortKey:
    """Wrapper making heterogeneous values sortable without raising.

    Ordering across types is arbitrary but stable: ``None`` sorts last, then
    values group by type name. This keeps a column with mixed or missing data
    from crashing a list view.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def _rank(self) -> tuple[int, str]:
        v = self.value
        if v is MISSING or v is None:
            return (2, "")
        if isinstance(v, bool):
            return (0, "bool")
        if isinstance(v, (int, float, Decimal)):
            return (0, "num")
        if isinstance(v, datetime):
            return (0, "dt")
        if isinstance(v, date):
            return (0, "dt")
        if isinstance(v, str):
            return (1, "str")
        return (1, type(v).__name__)

    def __lt__(self, other: _SortKey) -> bool:
        ra, rb = self._rank(), other._rank()
        if ra != rb:
            return ra < rb
        if ra[0] == 2:
            return False
        a, b = self.value, other.value
        if isinstance(a, str) and isinstance(b, str):
            return a.casefold() < b.casefold()
        try:
            left, right = _comparable(a, b)
            return bool(left < right)
        except TypeError:
            return str(a) < str(b)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _SortKey) and self.value == other.value


#: Public name for the ordering wrapper, so other modules can order values the
#: same way a sort does without reaching for a private class.
SortKey = _SortKey


def apply_sort(rows: Iterable[Any], sorts: Sequence[Sort]) -> list[Any]:
    """Stable multi-key sort. Applied right-to-left so the first key wins."""
    result = list(rows)
    for spec in reversed(sorts):
        field_name = spec.field

        def key(row: Any, _f: str = field_name) -> _SortKey:
            return _SortKey(_get(row, _f))

        result.sort(key=key, reverse=spec.dir is SortDir.DESC)
    return result


def paginate(rows: Sequence[Any], q: ListQuery, total: int | None = None) -> Page[Any]:
    """Slice ``rows`` for the requested page and build a :class:`Page`."""
    if total is None:
        total = len(rows)
    window = rows[q.offset : q.offset + q.page_size]
    return Page(
        items=list(window),
        page=q.page,
        page_size=q.page_size,
        total=total,
        has_more=q.offset + len(window) < total,
    )


def run_query(
    rows: Iterable[Any],
    q: ListQuery,
    *,
    search_fields: Sequence[str] = (),
    do_filter: bool = True,
    do_search: bool = True,
    do_sort: bool = True,
    do_paginate: bool = True,
    total: int | None = None,
) -> Page[Any]:
    """Apply the requested stages of the query pipeline to ``rows``.

    Each stage can be switched off so the shim only emulates what its wrapped
    provider did not already handle.
    """
    result = list(rows)
    if do_filter:
        result = apply_filter(result, q.effective_filter)
    if do_search:
        result = apply_search(result, q.search, search_fields)
    if do_sort and q.sort:
        result = apply_sort(result, q.sort)
    if not do_paginate:
        return Page(items=result, page=q.page, page_size=q.page_size, total=total if total is not None else len(result), has_more=False)
    return paginate(result, q, total=total)


def run_aggregate(rows: Iterable[Any], spec: AggSpec) -> list[dict[str, Any]]:
    """Group ``rows`` and compute measures, in Python."""
    kept = apply_filter(rows, spec.effective_filter)

    buckets: dict[tuple[Any, ...], list[Any]] = {}
    for row in kept:
        key = tuple(
            (None if (v := _get(row, f)) is MISSING else v) for f in spec.group_by
        )
        buckets.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for key, group in buckets.items():
        entry: dict[str, Any] = dict(zip(spec.group_by, key, strict=True))
        for m in spec.measures:
            if m.agg is Agg.COUNT:
                entry[m.alias] = len(group)
                continue
            # Non-COUNT measures always carry a field; Measure enforces it.
            source = m.field or ""
            values = [
                v for r in group
                if (v := _get(r, source)) is not MISSING and v is not None
            ]
            if not values:
                entry[m.alias] = None
            elif m.agg is Agg.AVG:
                entry[m.alias] = sum(values) / len(values)
            elif m.agg is Agg.SUM:
                entry[m.alias] = sum(values)
            elif m.agg is Agg.MIN:
                entry[m.alias] = min(values, key=_SortKey)
            else:
                entry[m.alias] = max(values, key=_SortKey)
        out.append(entry)

    if spec.sort:
        out = apply_sort(out, spec.sort)
    if spec.limit is not None:
        out = out[: spec.limit]
    return out
