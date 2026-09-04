"""Reading one resource from several backends at once.

``CompositeProvider`` splits reads from writes. This splits the *rows*: a
resource whose records live in more than one place -- last year's deals in an
archive database, this year's in the live one; tickets from two regional
instances -- declared once and served as a single set of screens.

    Resource(
        "deals",
        provider=Union("db.main#deals", "db.archive#deals"),
        fields=[...],
    )

Three properties are worth stating, because a union that gets any of them
wrong is worse than no union at all.

**Pagination is exact, not approximate.** Serving page N of a merged, sorted
result needs ``offset + page_size`` rows from *each* source, merged and then
sliced. Asking each source for one page and interleaving the answers would
quietly skip rows. The cost is bounded by the page, never by the collection,
which is why this provider advertises ``server_paginate`` honestly rather than
leaving it to the shim.

**A source that fails fails the query.** A union that silently drops an
unreachable backend returns a short answer that looks like a complete one --
the same failure mode as a truncated filter, and the reason the shim raises
instead of truncating. The dashboard may degrade a card; a list view may not
lose rows without saying so.

**Keys are only unique if you make them.** Two databases will both have an
``id`` of 5. Pass ``prefixed=True`` and every key becomes ``<source>:<key>``,
so a detail route addresses one row rather than whichever source answered
first. Left off, sources are searched in declaration order and the first match
wins -- fine when the keys are UUIDs or the ranges are known not to overlap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.core.errors import TooManyRows, UnsupportedOperation
from app.core.query import (
    ALL_OPS,
    Agg,
    AggSpec,
    Condition,
    Filter,
    Group,
    ListQuery,
    Not,
    Op,
    Page,
    and_,
)
from app.core.results import Ctx, Record, WriteResult
from app.providers import local
from app.providers.base import Capabilities, Provider, Rows

#: Ceiling on rows merged in memory for one query. A union reads
#: ``offset + page_size`` rows per source, so this bounds how deep a caller may
#: page rather than how large the sources are.
DEFAULT_MAX_ROWS = 5_000

#: Operators that can decide which sources to visit rather than being pushed
#: into them. Anything else applied to the source column is refused.
SOURCE_OPS = frozenset({Op.EQ, Op.NE, Op.IN, Op.NOT_IN})


@dataclass(frozen=True, slots=True)
class UnionSource:
    """One backend contributing rows to a union.

    ``label`` is what the source column shows and what a filter on that column
    matches, so it is part of the interface: changing it changes saved filters.
    It defaults to the connection name, which is usually the right label and
    always a stable one.
    """

    ref: str
    label: str = ""

    def __post_init__(self) -> None:
        if not self.ref.strip():
            raise ValueError("a union source needs a 'connection#target' reference")
        if not self.label:
            object.__setattr__(self, "label", self.ref.partition("#")[0].strip())

    @classmethod
    def coerce(cls, item: UnionSource | str) -> UnionSource:
        return item if isinstance(item, UnionSource) else cls(item)


class Union:
    """A declaration that a resource reads from several connections.

    Held as the resource's ``provider`` until the registry binds it, at which
    point each reference is built the ordinary way and handed to
    :class:`UnionProvider`. Declaring it is therefore no different from
    declaring any other provider -- the resource does not know it is a union,
    and neither do the views.
    """

    __slots__ = ("sources", "source_field", "prefixed", "max_rows")

    def __init__(
        self,
        *sources: UnionSource | str,
        source_field: str = "source",
        prefixed: bool = False,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        if len(sources) < 2:
            raise ValueError("a union needs at least two sources")
        self.sources = tuple(UnionSource.coerce(s) for s in sources)
        labels = [s.label for s in self.sources]
        if len(set(labels)) != len(labels):
            raise ValueError(
                "union sources need distinct labels; two sources on the same "
                f"connection must be labelled explicitly (got {labels})"
            )
        #: Column stamped onto every record naming where it came from. Declare a
        #: field of this name to show, filter or group by it.
        self.source_field = source_field
        self.prefixed = prefixed
        self.max_rows = max_rows

    def __repr__(self) -> str:
        return f"Union({', '.join(s.ref for s in self.sources)})"


@dataclass(frozen=True, slots=True)
class _Bound:
    """A built provider paired with the label its rows are stamped with."""

    label: str
    provider: Provider


class UnionProvider:
    """Serves one resource from several read providers.

    Every source is expected to satisfy the full query contract -- the registry
    passes each through the shim first -- so filtering, searching and sorting
    are pushed all the way down, and only the merge happens here.
    """

    def __init__(
        self,
        sources: Sequence[_Bound] | Sequence[tuple[str, Provider]],
        *,
        name: str = "",
        pk_field: str = "id",
        source_field: str = "source",
        prefixed: bool = False,
        max_rows: int = DEFAULT_MAX_ROWS,
        searchable_fields: tuple[str, ...] = (),
    ) -> None:
        self.sources = tuple(
            s if isinstance(s, _Bound) else _Bound(s[0], s[1]) for s in sources
        )
        if len(self.sources) < 2:
            raise ValueError("a union needs at least two sources")
        self.name = name or f"union({'+'.join(s.label for s in self.sources)})"
        self.pk_field = pk_field
        self.source_field = source_field
        self.prefixed = prefixed
        self.max_rows = max_rows

        # Only operators every source can push down may be advertised: a filter
        # partially applied is a filter silently dropped on one backend and
        # honoured on another, which is the worst of both.
        ops = ALL_OPS
        for src in self.sources:
            ops = ops & src.provider.capabilities.filter_ops

        self.capabilities = Capabilities(
            read=True,
            write=False,
            delete=False,
            server_filter=bool(ops),
            server_sort=all(s.provider.capabilities.server_sort for s in self.sources),
            server_search=bool(searchable_fields)
            and all(s.provider.capabilities.server_search for s in self.sources),
            server_paginate=True,
            total_count=all(s.provider.capabilities.total_count for s in self.sources),
            aggregate=True,
            filter_ops=ops,
            searchable_fields=searchable_fields,
        )

    # -- source routing -----------------------------------------------------

    def _mentions_source(self, f: Filter | None) -> bool:
        if f is None:
            return False
        if isinstance(f, Condition):
            return f.field == self.source_field
        if isinstance(f, Not):
            return self._mentions_source(f.child)
        return any(self._mentions_source(child) for child in f.children)

    def _labels_for(self, cond: Condition) -> set[str]:
        """Which sources a condition on the source column allows."""
        if cond.op not in SOURCE_OPS:
            raise UnsupportedOperation(
                f"{self.name} can only filter {self.source_field!r} with "
                f"{', '.join(sorted(op.value for op in SOURCE_OPS))}, not {cond.op.value!r}"
            )
        every = {s.label for s in self.sources}
        wanted = (
            {str(v) for v in cond.value}
            if cond.op in (Op.IN, Op.NOT_IN)
            else {str(cond.value)}
        )
        if cond.op in (Op.NE, Op.NOT_IN):
            return every - wanted
        return every & wanted

    def _split(self, f: Filter | None) -> tuple[set[str] | None, Filter | None]:
        """Separate "which sources" from "which rows".

        A condition on the source column cannot be pushed into a backend -- the
        column does not exist there -- so it is answered by *not visiting* the
        source at all, which is both correct and cheaper than fetching rows to
        discard. Only conjunctions can be resolved this way: ``source = 'a' OR
        amount > 10`` would need rows from a source the condition excludes,
        so it is refused rather than answered approximately.
        """
        if f is None:
            return None, None
        if isinstance(f, Condition):
            if f.field != self.source_field:
                return None, f
            return self._labels_for(f), None
        if isinstance(f, Not) or (isinstance(f, Group) and f.op == "or"):
            if self._mentions_source(f):
                raise UnsupportedOperation(
                    f"{self.name} cannot filter on {self.source_field!r} inside an "
                    f"OR or NOT; put it in the top-level AND, or drop it"
                )
            return None, f

        allowed: set[str] | None = None
        kept: list[Filter] = []
        for child in f.children:
            child_allowed, child_kept = self._split(child)
            if child_allowed is not None:
                allowed = child_allowed if allowed is None else allowed & child_allowed
            if child_kept is not None:
                kept.append(child_kept)
        return allowed, and_(*kept)

    def _route(self, f: Filter | None) -> tuple[tuple[_Bound, ...], Filter | None]:
        allowed, remaining = self._split(f)
        if allowed is None:
            return self.sources, remaining
        return tuple(s for s in self.sources if s.label in allowed), remaining

    # -- record shaping -----------------------------------------------------

    def _stamp(self, record: Record, src: _Bound) -> Record:
        """Tag a row with its origin, and namespace its key when asked."""
        changes: dict[str, Any] = {self.source_field: src.label}
        if self.prefixed:
            changes[self.pk_field] = f"{src.label}:{record.pk}"
        return Record({**record.as_dict(), **changes}, self.pk_field)

    def _unprefix(self, pk: Any) -> tuple[tuple[_Bound, ...], Any]:
        """Route a prefixed key back to the one source that owns it."""
        if not self.prefixed:
            return self.sources, pk
        label, sep, rest = str(pk).partition(":")
        if not sep:
            return self.sources, pk
        owner = tuple(s for s in self.sources if s.label == label)
        return (owner or self.sources), (rest if owner else pk)

    # -- reads --------------------------------------------------------------

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        sources, remaining = self._route(q.effective_filter)
        if not sources:
            return Page.empty(q)

        # Every source must yield the rows that could appear on this page, so
        # the window is offset-inclusive. This is the whole memory cost of a
        # union, and the only thing that grows is how deep the caller pages.
        needed = q.offset + q.page_size
        if needed > self.max_rows:
            raise TooManyRows(
                f"page {q.page} of {self.name!r} would merge {needed} rows from each "
                f"of {len(sources)} sources, over the {self.max_rows} row budget; "
                f"filter further or narrow the union",
                provider=self.name,
                max_rows=self.max_rows,
            )

        # The source column is ours to sort on, not theirs: it does not exist
        # in any backend. Every other key is pushed down, which is exact --
        # within one source the source column is constant, so ordering by the
        # remaining keys is the same ordering.
        pushed_sort = tuple(s for s in q.sort if s.field != self.source_field)
        inner = q.with_(
            filter=remaining,
            scope=None,
            sort=pushed_sort,
            page=1,
            page_size=needed,
        )

        # Concurrently: these are separate systems, so the query costs one
        # round trip rather than one per source. Exceptions propagate --
        # dropping an unreachable source would silently shorten the answer.
        pages = await asyncio.gather(*(s.provider.list(inner, ctx) for s in sources))

        rows: list[Record] = []
        for src, page in zip(sources, pages, strict=True):
            rows.extend(self._stamp(r, src) for r in page.items)

        if q.sort:
            rows = local.apply_sort(rows, q.sort)

        totals = [p.total for p in pages]
        total = sum(t for t in totals if t is not None) if all(
            t is not None for t in totals
        ) else None

        window = rows[q.offset : q.offset + q.page_size]
        return Page(
            items=window,
            page=q.page,
            page_size=q.page_size,
            total=total,
            has_more=(q.offset + len(window) < total)
            if total is not None
            else len(rows) > q.offset + q.page_size,
        )

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        """The record with this key, from whichever source holds it.

        Sequential rather than concurrent: with prefixed keys exactly one
        source is asked, and without them the first match wins, so fanning out
        would spend every backend's time to answer from the first.
        """
        sources, key = self._unprefix(pk)
        for src in sources:
            record = await src.provider.get(key, ctx)
            if record is not None:
                return self._stamp(record, src)
        return None

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        """Group across every source.

        Grouped counts, sums, minima and maxima all combine from partial
        results, so each source aggregates its own rows and only the groups are
        merged here -- the work stays in the databases, where the indexes are.

        Averages do not combine: the mean of two means is not the mean, and
        weighting it needs a count of non-null values the query language cannot
        ask for. So an average falls back to merging rows, under the same row
        budget as everything else.
        """
        sources, remaining = self._route(spec.effective_filter)
        if not sources:
            return []
        if any(m.agg is Agg.AVG for m in spec.measures):
            return await self._aggregate_locally(spec, ctx)

        by_source = self.source_field in spec.group_by
        inner = replace(
            spec,
            group_by=tuple(g for g in spec.group_by if g != self.source_field),
            filter=remaining,
            scope=None,
            # Ordering and limiting a partial result would drop groups that
            # belong in the merged answer; both are applied after the merge.
            sort=(),
            limit=None,
        )

        results = await asyncio.gather(*(s.provider.aggregate(inner, ctx) for s in sources))

        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for src, rows in zip(sources, results, strict=True):
            for row in rows:
                entry = dict(row)
                if by_source:
                    entry[self.source_field] = src.label
                key = tuple(entry.get(g) for g in spec.group_by)
                if key not in merged:
                    merged[key] = entry
                    continue
                merged[key] = self._combine(merged[key], entry, spec)

        out = list(merged.values())
        if spec.sort:
            out = local.apply_sort(out, spec.sort)
        if spec.limit is not None:
            out = out[: spec.limit]
        return out

    def _combine(
        self, left: dict[str, Any], right: dict[str, Any], spec: AggSpec
    ) -> dict[str, Any]:
        """Fold one source's row for a group into the running total."""
        out = dict(left)
        for m in spec.measures:
            a, b = left.get(m.alias), right.get(m.alias)
            if a is None:
                out[m.alias] = b
            elif b is None:
                out[m.alias] = a
            elif m.agg in (Agg.COUNT, Agg.SUM):
                out[m.alias] = a + b
            elif m.agg is Agg.MIN:
                out[m.alias] = min(a, b, key=local.SortKey)
            else:
                out[m.alias] = max(a, b, key=local.SortKey)
        return out

    async def _aggregate_locally(self, spec: AggSpec, ctx: Ctx) -> Rows:
        q = ListQuery(
            filter=spec.filter,
            scope=spec.scope,
            page_size=self.max_rows,
            with_total=False,
        )
        page = await self.list(q, ctx)
        rows = list(page.items)
        if len(rows) >= self.max_rows:
            raise TooManyRows(
                f"averaging across {self.name!r} needs every matching row and there "
                f"are at least {self.max_rows}; filter further, or ask for a sum and "
                f"a count instead",
                provider=self.name,
                max_rows=self.max_rows,
            )
        return local.run_aggregate(rows, spec)

    # -- writes -------------------------------------------------------------

    def _refuse(self, verb: str) -> WriteResult:
        raise UnsupportedOperation(
            f"{self.name} is a union of {len(self.sources)} sources and cannot {verb}: "
            f"there is no answer to which one the row belongs in. Declare a resource "
            f"per source for writing, or give the resource a write provider."
        )

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        return self._refuse("create records")

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        return self._refuse("update records")

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        return self._refuse("write conditionally")

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        return self._refuse("delete records")

    # -- lifecycle ----------------------------------------------------------

    async def warm(self) -> None:
        await asyncio.gather(
            *(self._warm_one(s.provider) for s in self.sources), return_exceptions=True
        )

    @staticmethod
    async def _warm_one(provider: Provider) -> None:
        warm = getattr(provider, "warm", None)
        if warm is not None:
            await warm()

    async def health(self) -> tuple[bool, str]:
        """Healthy only when every source is.

        A union missing a source does not return fewer rows -- it fails -- so a
        half-healthy union is not a degraded service, it is a broken one.
        """
        results = await asyncio.gather(
            *(self._health_one(s) for s in self.sources), return_exceptions=True
        )
        details: list[str] = []
        healthy = True
        for src, result in zip(self.sources, results, strict=True):
            if isinstance(result, BaseException):
                healthy = False
                details.append(f"{src.label}: {type(result).__name__}: {result}")
                continue
            ok, detail = result
            healthy = healthy and ok
            details.append(f"{src.label}: {detail}")
        return healthy, "; ".join(details)

    @staticmethod
    async def _health_one(src: _Bound) -> tuple[bool, str]:
        check = getattr(src.provider, "health", None)
        if check is None:
            return True, "no health check implemented"
        return await check()

    async def close(self) -> None:
        for src in self.sources:
            closer = getattr(src.provider, "close", None)
            if closer is not None:
                await closer()

    @property
    def labels(self) -> tuple[str, ...]:
        """Source labels, in declaration order -- the choices for a source field."""
        return tuple(s.label for s in self.sources)

    def __repr__(self) -> str:
        return f"<UnionProvider {self.name!r} over {len(self.sources)} sources>"


def source_choices(union: Union) -> list[tuple[str, str]]:
    """Choices for a ``SelectField`` on the source column.

    Saves declaring the labels twice, which is the sort of duplication that
    stays right until somebody renames a source.
    """
    return [(s.label, s.label.replace("_", " ").replace(".", " ").title()) for s in union.sources]
