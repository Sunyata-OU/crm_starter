"""The generic resource routes.

One router serves every resource. A handler here never knows which resource it
is working on beyond what the registry tells it, which is the whole point: a new
resource gets a full set of screens without a line of routing code.

Every handler answers three audiences from one body -- a full page, an HTMX
fragment, or JSON -- decided by ``View`` from the request headers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse, Response, StreamingResponse

from app.auth.permissions import require_can
from app.core import clock
from app.core.errors import NotFound, UnsupportedOperation, ValidationFailed
from app.core.query import (
    Agg,
    AggSpec,
    Condition,
    ListQuery,
    Measure,
    Op,
    Page,
    Sort,
    SortDir,
    and_,
    or_,
)
from app.core.results import Record, WriteStatus
from app.resources.form_engine import FormEngine
from app.resources.resource import Resource
from app.resources.views import CALENDAR_SCALES
from app.web.deps import View, build_view
from app.web.filters import canonical_params, has_panel_params, parse_filters, parse_sort
from app.web.uploads import discard, store_uploads

router = APIRouter(prefix="/r", tags=["resources"])

#: Relation keys looked up per query. Under SQLite's 999-parameter ceiling with
#: room for the scope filter's own parameters alongside them.
LABEL_BATCH = 500


# -- listing ---------------------------------------------------------------


@router.get("/{resource_name}")
async def list_records(resource_name: str, view: View = Depends(build_view)) -> Response:
    """The list page, or whichever alternative view was asked for."""
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)

    if (target := _canonical_url(view, resource)) is not None:
        return RedirectResponse(target, status_code=303)

    kind = view.param("view") or _default_view_kind(resource)
    spec = resource.view(kind) if resource.has_view(kind) else resource.view("list")

    match spec.kind:
        case "board":
            return await _render_board(view, resource, spec)
        case "calendar":
            return await _render_calendar(view, resource, spec)
        case "chart" | "pivot":
            return await _render_chart(view, resource, spec)
        case "tree":
            return await _render_tree(view, resource, spec)
        case "gantt":
            return await _render_gantt(view, resource, spec)
        case "map":
            return await _render_map(view, resource, spec)
        case "activity":
            return await _render_activity(view, resource, spec)
        case "dashboard":
            return await _render_dashboard(view, resource, spec)

    return await _render_list(view, resource, spec)


def _canonical_url(view: View, resource: Resource) -> str | None:
    """Where a filter-builder submission should be redirected to, if anywhere.

    The panel posts indexed triples because a ``<select>`` cannot rename the
    input beside it. Bouncing them into the readable ``f.field__op=value`` form
    keeps that form the only one that ever reaches the address bar, the back
    button or a shared link -- and it costs one redirect on Apply, not one per
    page of results.
    """
    params = view.request.query_params
    if not has_panel_params(params):
        return None
    query = urlencode(canonical_params(list(params.multi_items()), resource))
    return f"{view.request.url.path}?{query}" if query else view.request.url.path


def _default_view_kind(resource: Resource) -> str:
    views = resource.switchable_views()
    return views[0].name if views else "list"


def _quick_filter(view: View, resource: Resource):
    """The filter behind ``?quick=<name>``, if one was asked for.

    Resolved per request because a chip like "due this week" or "mine" depends
    on when it is clicked and who clicks it.
    """
    name = view.param("quick")
    if not name:
        return None
    for chip in resource.search.quick_filters:
        if chip.name == name:
            return chip.resolve(view.identity)
    return None


def _build_query(view: View, resource: Resource, spec: Any) -> ListQuery:
    """Assemble the query for a list-style view from the request."""
    page, page_size = view.page_params(getattr(spec, "page_size", None))
    sort = parse_sort(view.param("sort"), resource) or tuple(getattr(spec, "default_sort", ()))
    return resource.build_query(
        view.identity,
        filter=and_(
            parse_filters(view.request.query_params, resource, view.identity),
            _quick_filter(view, resource),
        ),
        search=view.param("q"),
        sort=sort,
        page=page,
        page_size=page_size,
    )


async def resolve_relations(
    view: View, resource: Resource, records: Sequence[Record]
) -> list[Record]:
    """Attach a human-readable label to every relation value.

    A relation stores a key; the name behind it lives on another resource,
    possibly in another backend, so it cannot come from a join. Instead the
    distinct keys are collected and fetched in **one query per relation field**
    -- not one per row, which is the obvious implementation and the wrong one.

    Labels already looked up during this request are reused. Two fields
    pointing at the same resource -- a deal's company and its contact's company
    -- would otherwise fetch overlapping sets of the same rows, and a page that
    renders a list and then a related list would fetch them again.

    The label is stamped onto each record as ``<field>_label``, which is what
    the relation display partial looks for.
    """
    rows = list(records)
    if not rows:
        return rows

    for relation in resource.relations:
        if not view.registry.has_resource(relation.relation.resource):
            continue
        target = view.registry.resource(relation.relation.resource)
        if not target.policy.allows("read", view.identity):
            continue

        keys = {
            str(value) for r in rows
            if (value := r.get(relation.name)) not in (None, "")
        }
        if not keys:
            continue

        labels = await _relation_labels(view, target, keys)
        rows = [
            r.merge({f"{relation.name}_label": labels[key]})
            if (key := str(r.get(relation.name))) in labels
            else r
            for r in rows
        ]

    return rows


async def _relation_labels(
    view: View, target: Resource, keys: set[str]
) -> dict[str, str]:
    """Look up display labels for ``keys``, fetching only the unseen ones.

    The cache is per request and keyed by resource, so it cannot leak a label
    between users -- which matters, because what a person may see is decided by
    a policy this function has already consulted, and a longer-lived cache
    would have to consult it again on every read.
    """
    cached = view.relation_labels.setdefault(target.name, {})
    missing = sorted(keys - cached.keys(), key=str)
    if not missing:
        return {k: cached[k] for k in keys if k in cached}

    # In batches, because an IN clause has a practical ceiling -- SQLite's is
    # 999 parameters by default -- and because the page size has to be at least
    # the number of keys asked for or the answer comes back short. Coming back
    # short is the failure mode worth avoiding: the rows are still rendered,
    # just with raw keys where a name belongs, which looks like missing data
    # rather than a limit being hit.
    for start in range(0, len(missing), LABEL_BATCH):
        batch = missing[start : start + LABEL_BATCH]
        query = target.build_query(
            view.identity,
            filter=Condition(target.pk, Op.IN, batch),
            page_size=len(batch),
            with_total=False,
        )
        try:
            page = await target.provider.list(query, view.ctx)
        except Exception:
            # A failing lookup must not take down the list it decorates; the key
            # is still shown and still links.
            break
        cached.update({str(r.pk): target.display_value(r) for r in page.items})

    return {k: cached[k] for k in keys if k in cached}


async def _render_list(view: View, resource: Resource, spec: Any) -> Response:
    query = _build_query(view, resource, spec)
    page = await resource.provider.list(query, view.ctx)
    page = Page(
        items=await resolve_relations(view, resource, page.items),
        page=page.page, page_size=page.page_size,
        total=page.total, has_more=page.has_more, cursor=page.cursor,
    )

    if view.wants_json:
        return view.json(
            {
                "items": [dict(r) for r in page.items],
                "page": page.page,
                "page_size": page.page_size,
                "total": page.total,
                "has_next": page.has_next,
            }
        )

    context = {
        "page": page,
        "query": query,
        "spec": spec,
        "columns": _visible_columns(resource, spec, view),
        "views": resource.switchable_views(),
        "current_view": spec.name,
        "can_create": resource.can("create", view.identity),
        "search_term": view.param("q"),
    }
    # An HTMX request re-renders only the table, so filtering and paging do not
    # rebuild the navigation or lose the user's scroll position.
    if view.is_fragment:
        return view.render("views/_list_body.html", resource=resource, **context)
    return view.render_view(resource, "list", **context)


def _visible_columns(resource: Resource, spec: Any, view: View) -> list[Any]:
    """Columns the caller may see, in the order the view declares."""
    readable = set(resource.policy.readable_fields(view.identity, resource))
    return [c for c in getattr(spec, "columns", []) if c.field in readable]


async def _render_board(view: View, resource: Resource, spec: Any) -> Response:
    """Records grouped into columns by a status-like field.

    Three things make this cheap enough to leave on a large table.

    The counts and totals come from a single grouped aggregate, not from the
    cards. That is a correctness fix as much as a performance one: a column
    holds at most ``column_limit`` cards, so summing the loaded cards reports
    the total of the first fifty deals rather than of the column.

    The cards themselves still need one query per column -- a column is its own
    sorted, limited result set, and no portable single query returns the top N
    of each group -- but those queries do not depend on each other, so they are
    issued together and the board waits once rather than five times.

    Relation labels are resolved across every column at once, so adding a
    column cannot add a lookup.
    """
    field = resource.field(spec.group_by)
    choices = field.choices(view.ctx)
    query = _build_query(view, resource, spec).with_(
        page_size=spec.column_limit,
        page=1,
        # The counts come from the aggregate below, so asking each column
        # query for a total as well would be paying twice for the same number.
        with_total=False,
    )

    measures = [Measure(Agg.COUNT)]
    if spec.sum_field:
        measures.append(Measure(Agg.SUM, spec.sum_field))
    totals = await _board_totals(view, resource, spec, query, measures)

    async def cards(choice: Any) -> Page:
        column_query = query.with_(
            filter=_and_condition(query.filter, spec.group_by, choice.value)
        )
        return await resource.provider.list(column_query, view.ctx)

    pages = await asyncio.gather(*(cards(choice) for choice in choices))

    # One batch for the whole board. resolve_relations issues a query per
    # relation field, so resolving per column would multiply that by the
    # number of columns.
    resolved = iter(
        await resolve_relations(
            view, resource, [record for page in pages for record in page.items]
        )
    )

    columns = []
    for choice, page in zip(choices, pages, strict=True):
        items = [next(resolved) for _ in page.items]
        counts = totals.get(choice.value, {})
        columns.append(
            {
                "choice": choice,
                "page": Page(
                    items=items,
                    page=page.page,
                    page_size=page.page_size,
                    total=counts.get("count"),
                    has_more=page.has_more,
                ),
                "count": counts.get("count", 0),
                "sum": counts.get("sum") if spec.sum_field else None,
            }
        )

    return view.render_view(
        resource,
        "board",
        spec=spec,
        columns=columns,
        query=query,
        views=resource.switchable_views(),
        current_view=spec.name,
        can_update=resource.can("update", view.identity),
        search_term=view.param("q"),
    )


async def _board_totals(
    view: View, resource: Resource, spec: Any, query: ListQuery, measures: list[Measure]
) -> dict[Any, dict[str, Any]]:
    """Per-column count and sum, over every matching row.

    Falls back to an empty result rather than failing the page: a provider
    without aggregate support still has a usable board, it just shows no
    counts. The shim supplies this for most providers, so the fallback is for
    the genuinely limited ones.
    """
    spec_agg = AggSpec(
        group_by=(spec.group_by,),
        measures=tuple(measures),
        filter=query.filter,
        scope=query.scope,
    )
    try:
        rows = await resource.provider.aggregate(spec_agg, view.ctx)
    except UnsupportedOperation:
        return {}

    count_alias = measures[0].alias
    sum_alias = measures[1].alias if len(measures) > 1 else None
    return {
        row.get(spec.group_by): {
            "count": row.get(count_alias) or 0,
            "sum": float(row.get(sum_alias) or 0) if sum_alias else None,
        }
        for row in rows
    }


def _and_condition(existing, field_name: str, value: Any):
    from app.core.query import and_

    return and_(existing, Condition(field_name, Op.EQ, value))


#: How wide each fixed-width calendar scale is. A month is absent because its
#: width is a property of the month, not of the scale.
CALENDAR_SPAN_DAYS = {"day": 1, "week": 7, "fortnight": 14}

#: Events drawn per cell before the rest are summarised as "+N more". A day
#: has one cell the width of the page to spend; a month has thirty-odd.
CALENDAR_CELL_EVENTS = {"day": 50, "week": 12, "fortnight": 8, "month": 4}


def _calendar_anchor(view: View) -> date:
    """The day the window is drawn around.

    ``at`` is the current spelling, shared with the gantt view. ``year`` and
    ``month`` are the spelling the month grid shipped with, still honoured so
    that a bookmarked or shared link keeps landing where it used to.
    """
    if (at := clock.parse_date(view.param("at"))) is not None:
        return at
    year, month = view.int_param("year", 0), view.int_param("month", 0)
    if year and month:
        try:
            return date(year, max(1, min(12, month)), 1)
        except ValueError:
            pass  # A year outside date's range: fall through to today.
    return clock.today(view.timezone)


def _calendar_window(anchor: date, scale: str) -> tuple[date, date]:
    """The half-open span of days a scale shows around ``anchor``."""
    if scale == "month":
        first = _snap(anchor, "month")
        return first, _advance(first, "month", 1)
    # A week and a fortnight both begin on a Monday; a day begins on itself.
    start = anchor if scale == "day" else _snap(anchor, "week")
    return start, start + timedelta(days=CALENDAR_SPAN_DAYS[scale])


def _calendar_shift(start: date, scale: str, units: int) -> date:
    """Where "previous" and "next" land, one whole window away."""
    if scale == "month":
        return _advance(start, "month", units)
    return start + timedelta(days=units * CALENDAR_SPAN_DAYS[scale])


def _calendar_rows(window_start: date, window_end: date, scale: str) -> list[list[date]]:
    """The grid, as rows of days.

    A month is padded out to whole weeks so its cells line up under Monday
    through Sunday; the narrower scales already start on the day they mean, so
    they are simply chopped into sevens -- which leaves a day as a single cell.
    """
    if scale == "month":
        from calendar import Calendar

        days = [d for week in Calendar(firstweekday=0).monthdatescalendar(
            window_start.year, window_start.month) for d in week]
    else:
        days = [window_start + timedelta(days=i) for i in range((window_end - window_start).days)]
    return [days[i:i + 7] for i in range(0, len(days), 7)]


def _calendar_label(window_start: date, window_end: date, scale: str) -> str:
    """What the window is called, above the grid."""
    last = window_end - timedelta(days=1)
    if scale == "month":
        return window_start.strftime("%B %Y")
    if scale == "day":
        return window_start.strftime("%A %-d %B %Y")
    if window_start.year != last.year:
        return f"{window_start.strftime('%-d %b %Y')} – {last.strftime('%-d %b %Y')}"
    if window_start.month != last.month:
        return f"{window_start.strftime('%-d %b')} – {last.strftime('%-d %b %Y')}"
    return f"{window_start.strftime('%-d')} – {last.strftime('%-d %b %Y')}"


async def _render_calendar(view: View, resource: Resource, spec: Any) -> Response:
    """Records placed on a day, week, fortnight or month grid by their start date."""
    scale = view.param("scale") or spec.default_scale
    if scale not in CALENDAR_SCALES:
        scale = spec.default_scale

    today = clock.today(view.timezone)
    window_start, window_end = _calendar_window(_calendar_anchor(view), scale)

    # Restrict to the visible window rather than fetching the whole table; a
    # calendar over a large resource would otherwise pull every row. The days a
    # month grid borrows from its neighbours are outside that window, and stay
    # empty: they are there to square off the grid, not to report anything.
    query = resource.build_query(
        view.identity,
        filter=and_(
            parse_filters(view.request.query_params, resource, view.identity),
            Condition(spec.start_field, Op.GTE, window_start.isoformat()),
            Condition(spec.start_field, Op.LT, window_end.isoformat()),
        ),
        search=view.param("q"),
        page=1,
        page_size=500,
    )
    page = await resource.provider.list(query, view.ctx)

    buckets: dict[str, list[Record]] = {}
    for record in page.items:
        key = str(record.get(spec.start_field) or "")[:10]
        buckets.setdefault(key, []).append(record)

    return view.render_view(
        resource,
        "calendar",
        spec=spec,
        rows=_calendar_rows(window_start, window_end, scale),
        buckets=buckets,
        scale=scale,
        scales=CALENDAR_SCALES,
        cell_events=CALENDAR_CELL_EVENTS[scale],
        window_start=window_start,
        window_end=window_end,
        period_label=_calendar_label(window_start, window_end, scale),
        prev_at=_calendar_shift(window_start, scale, -1).isoformat(),
        next_at=_calendar_shift(window_start, scale, 1).isoformat(),
        today=today,
        views=resource.switchable_views(),
        current_view=spec.name,
    )


async def _render_chart(view: View, resource: Resource, spec: Any) -> Response:
    """An aggregate drawn as a chart or laid out as a pivot table."""
    scope = resource.policy.scope(view.identity)
    user_filter = parse_filters(view.request.query_params, resource, view.identity)

    if spec.kind == "chart":
        agg = AggSpec(
            group_by=spec.group_fields,
            measures=(spec.measure,),
            filter=user_filter,
            scope=scope,
            limit=spec.limit,
        )
    else:
        agg = AggSpec(
            group_by=spec.group_fields,
            measures=spec.measures,
            filter=user_filter,
            scope=scope,
        )

    rows = await resource.provider.aggregate(agg, view.ctx)

    if spec.kind == "chart" and spec.sort_by_value:
        alias = spec.measure.alias
        rows.sort(key=lambda r: (r.get(alias) is None, r.get(alias)), reverse=True)

    if view.wants_json:
        return view.json({"rows": rows})

    return view.render_view(
        resource,
        spec.kind,
        spec=spec,
        rows=rows,
        labels=_group_labels(resource, spec, view),
        max_value=_max_value(rows, spec),
        views=resource.switchable_views(),
        current_view=spec.name,
    )


def _group_labels(resource: Resource, spec: Any, view: View) -> dict[str, dict[Any, str]]:
    """Value-to-label maps so a chart axis shows "Won", not "won"."""
    labels: dict[str, dict[Any, str]] = {}
    for name in getattr(spec, "group_fields", ()):
        field = resource.get_field(name)
        if field is None:
            continue
        choices = field.choices(view.ctx)
        if choices:
            labels[name] = {c.value: c.label for c in choices}
    return labels


def _max_value(rows: list[dict[str, Any]], spec: Any) -> float:
    if spec.kind != "chart":
        return 0.0
    alias = spec.measure.alias
    values = [float(r.get(alias) or 0) for r in rows]
    return max(values) if values else 0.0


# -- tree ------------------------------------------------------------------


def _tree_child_resource(view: View, resource: Resource, spec: Any) -> Resource:
    """The resource hung under each row -- this one, unless another is named."""
    if spec.self_referential:
        return resource
    return view.registry.resource(spec.child_resource)


def _parent_value(child: Resource, spec: Any, pk: Any) -> Any:
    """``pk`` as the parent column stores it.

    A key arrives from the URL as text, and a provider that stores it as an
    integer matches neither ``"3"`` nor rows it should. The field coerces it
    the same way it coerces a value typed into the filter panel.
    """
    field = child.get_field(spec.parent_field)
    return field.parse_filter_value(Op.EQ, pk) if field is not None else pk


async def _child_counts(
    view: View, child: Resource, spec: Any, keys: Sequence[Any]
) -> dict[Any, int] | None:
    """How many children each of ``keys`` has, in one query.

    ``None`` means the provider cannot answer, which is different from "no
    children": the caller then offers every row a toggle rather than deciding
    from a number it does not have. Guessing wrong in the other direction --
    hiding the toggle -- would make a populated branch look like a leaf.
    """
    if not keys:
        return {}
    measure = Measure(Agg.COUNT)
    agg = AggSpec(
        group_by=(spec.parent_field,),
        measures=(measure,),
        filter=Condition(spec.parent_field, Op.IN, list(keys)),
        scope=child.policy.scope(view.identity),
    )
    try:
        rows = await child.provider.aggregate(agg, view.ctx)
    except UnsupportedOperation:
        return None
    return {row.get(spec.parent_field): row.get(measure.alias) or 0 for row in rows}


async def _tree_nodes(
    view: View, child: Resource, spec: Any, records: Sequence[Record], depth: int
) -> list[dict[str, Any]]:
    """Rows plus what the expander needs to know about each one.

    A tree over two resources is one level deep by definition: contacts hang
    under companies, and nothing hangs under a contact. Only a self-join
    recurses, so rows below the first level are offered a toggle only there.
    """
    recursing = spec.self_referential or depth == 0
    if not recursing or depth >= spec.max_depth:
        return [{"record": r, "depth": depth, "count": None, "expandable": False} for r in records]

    counts = await _child_counts(
        view, child, spec, [r.pk for r in records if r.pk is not None]
    )
    return [
        {
            "record": record,
            "depth": depth,
            "count": None if counts is None else counts.get(record.pk, 0),
            # Unknown counts still get a toggle: opening an empty branch says
            # "nothing here", which is honest. A missing toggle would not be.
            "expandable": counts is None or counts.get(record.pk, 0) > 0,
        }
        for record in records
    ]


async def _render_tree(view: View, resource: Resource, spec: Any) -> Response:
    """The top of a hierarchy. Branches are fetched when they are opened.

    Searching flattens the tree. A match three levels down is not reachable by
    opening roots that do not themselves match, so a filtered tree shows every
    hit at the top level instead of hiding them behind branches the user would
    have to guess at -- the same choice a file manager makes when you search.
    """
    child = _tree_child_resource(view, resource, spec)
    require_can(child, "read", view.identity)

    user_filter = and_(
        parse_filters(view.request.query_params, resource, view.identity),
        _quick_filter(view, resource),
    )
    search = view.param("q")
    flattened = user_filter is not None or bool(search)

    # Only a self-join has roots to restrict to. When the children live on
    # another resource every row here is a root by construction.
    roots = (
        Condition(spec.parent_field, Op.IS_NULL)
        if spec.self_referential and not flattened
        else None
    )

    page_number, page_size = view.page_params(spec.page_size)
    query = resource.build_query(
        view.identity,
        filter=and_(user_filter, roots),
        search=search or None,
        sort=parse_sort(view.param("sort"), resource) or tuple(spec.default_sort),
        page=page_number,
        page_size=page_size,
    )
    page = await resource.provider.list(query, view.ctx)
    records = await resolve_relations(view, resource, page.items)

    # A flattened tree lists this resource; its rows still hang children of the
    # child resource, so the counts come from there either way.
    nodes = await _tree_nodes(view, child, spec, records, depth=0)

    return view.render_view(
        resource,
        "tree",
        spec=spec,
        nodes=nodes,
        page=Page(
            items=records, page=page.page, page_size=page.page_size,
            total=page.total, has_more=page.has_more,
        ),
        query=query,
        columns=_visible_columns(resource, spec, view),
        child_resource=child,
        flattened=flattened,
        views=resource.switchable_views(),
        current_view=spec.name,
        can_create=resource.can("create", view.identity),
        search_term=search,
    )


# -- gantt -----------------------------------------------------------------


def _snap(day: date, scale: str) -> date:
    """The start of the ``scale`` unit containing ``day``."""
    if scale == "month":
        return day.replace(day=1)
    if scale == "week":
        return day - timedelta(days=day.weekday())
    return day


def _advance(day: date, scale: str, units: int) -> date:
    if scale == "month":
        total = day.month - 1 + units
        return date(day.year + total // 12, total % 12 + 1, 1)
    return day + timedelta(days=units * (7 if scale == "week" else 1))


def _gantt_columns(start: date, scale: str, span: int) -> list[dict[str, Any]]:
    """The header cells, and where each one begins."""
    fmt = {"day": "%-d %b", "week": "%-d %b", "month": "%b %Y"}[scale]
    return [
        {"start": (at := _advance(start, scale, i)), "label": at.strftime(fmt)}
        for i in range(span)
    ]


async def _render_gantt(view: View, resource: Resource, spec: Any) -> Response:
    """Records drawn as bars across a fixed window of time.

    Only records overlapping the window are fetched -- a schedule stretching
    over years would otherwise pull every row to draw twelve weeks -- and the
    bars are positioned as percentages of the window, so nothing measures the
    page to lay itself out.
    """
    scale = view.param("scale") or spec.default_scale
    if scale not in ("day", "week", "month"):
        scale = spec.default_scale
    span = max(1, spec.span)

    anchor = clock.parse_date(view.param("at")) or clock.today(view.timezone)
    window_start = _snap(anchor, scale)
    window_end = _advance(window_start, scale, span)
    total_days = max((window_end - window_start).days, 1)

    query = resource.build_query(
        view.identity,
        filter=and_(
            parse_filters(view.request.query_params, resource, view.identity),
            _quick_filter(view, resource),
            # Overlap, not containment: a bar that starts before the window and
            # ends inside it belongs on screen, and so does its mirror image.
            Condition(spec.start_field, Op.LT, window_end.isoformat()),
            Condition(spec.end_field, Op.GTE, window_start.isoformat()),
        ),
        search=view.param("q") or None,
        sort=parse_sort(view.param("sort"), resource)
        or tuple(spec.default_sort)
        or (Sort(spec.start_field, SortDir.ASC),),
        page=1,
        page_size=spec.limit,
        with_total=False,
    )
    page = await resource.provider.list(query, view.ctx)
    records = await resolve_relations(view, resource, page.items)

    bars = []
    for record in records:
        start = clock.parse_date(record.get(spec.start_field))
        end = clock.parse_date(record.get(spec.end_field))
        if start is None or end is None:
            continue
        # Clip to the window rather than dropping: a bar running past the edge
        # should reach the edge, which is how it says "there is more here".
        visible_start = max(start, window_start)
        visible_end = min(max(end, start), window_end)
        offset = (visible_start - window_start).days / total_days * 100
        width = max((visible_end - visible_start).days / total_days * 100, 1.0)
        bars.append(
            {
                "record": record,
                "start": start,
                "end": end,
                "left": round(offset, 4),
                "width": round(min(width, 100 - offset), 4),
                "clipped_start": start < window_start,
                "clipped_end": end > window_end,
                "progress": _percent(record.get(spec.progress_field)) if spec.progress_field else None,
            }
        )

    return view.render_view(
        resource,
        "gantt",
        spec=spec,
        bars=bars,
        groups=_gantt_groups(resource, spec, bars, view),
        columns=_gantt_columns(window_start, scale, span),
        scale=scale,
        window_start=window_start,
        window_end=window_end,
        prev_from=_advance(window_start, scale, -span).isoformat(),
        next_from=_advance(window_start, scale, span).isoformat(),
        today=clock.today(view.timezone),
        today_offset=_today_offset(clock.today(view.timezone), window_start, total_days),
        views=resource.switchable_views(),
        current_view=spec.name,
        search_term=view.param("q"),
    )


def _percent(value: Any) -> float | None:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _today_offset(today: date, window_start: date, total_days: int) -> float | None:
    """Where to draw the "now" line, or ``None`` when today is off-window."""
    offset = (today - window_start).days / total_days * 100
    return round(offset, 4) if 0 <= offset <= 100 else None


def _gantt_groups(
    resource: Resource, spec: Any, bars: list[dict[str, Any]], view: View
) -> list[dict[str, Any]]:
    """Bars gathered into bands, or one unnamed band when nothing groups them."""
    if not spec.group_by:
        return [{"label": "", "value": None, "bars": bars}]
    labels = _group_labels(resource, spec, view).get(spec.group_by, {})
    bands: dict[Any, list[dict[str, Any]]] = {}
    for bar in bars:
        bands.setdefault(bar["record"].get(spec.group_by), []).append(bar)
    return [
        {"label": labels.get(value, value if value is not None else "None"),
         "value": value, "bars": rows}
        for value, rows in bands.items()
    ]


# -- map -------------------------------------------------------------------


async def _render_map(view: View, resource: Resource, spec: Any) -> Response:
    """Records as pins, placed by a coordinate pair.

    Records missing either coordinate are excluded in the query and counted
    separately, so the page can say how many it could not place. A map that
    quietly shows nine of twelve offices is worse than one that says so.
    """
    user_filter = and_(
        parse_filters(view.request.query_params, resource, view.identity),
        _quick_filter(view, resource),
    )
    search = view.param("q") or None

    located = resource.build_query(
        view.identity,
        filter=and_(
            user_filter,
            Condition(spec.lat_field, Op.NOT_NULL),
            Condition(spec.lon_field, Op.NOT_NULL),
        ),
        search=search,
        page=1,
        page_size=spec.limit,
    )
    page = await resource.provider.list(located, view.ctx)
    records = await resolve_relations(view, resource, page.items)

    colours = _group_labels(resource, spec, view) if spec.color_field else {}
    palette = _choice_colours(resource, spec.color_field, view) if spec.color_field else {}
    markers = []
    for record in records:
        point = _coordinates(record, spec)
        if point is None:
            continue
        title_field = resource.get_field(spec.title_field) if spec.title_field else None
        markers.append(
            {
                "pk": str(record.pk),
                "lat": point[0],
                "lon": point[1],
                "title": (
                    str(title_field.to_display(title_field.extract(record)))
                    if title_field is not None
                    else resource.display_value(record)
                ),
                "subtitle": str(record.get(spec.subtitle_field) or "")
                if spec.subtitle_field
                else "",
                "colour": palette.get(record.get(spec.color_field), "") if spec.color_field else "",
                "url": f"/r/{resource.name}/{record.pk}",
            }
        )

    total = page.total if page.total is not None else len(markers)
    unplaced = await _count_unplaced(view, resource, spec, user_filter, search)

    if view.wants_json:
        return view.json({"markers": markers, "unplaced": unplaced})

    return view.render_view(
        resource,
        "map",
        spec=spec,
        markers=markers,
        # Only choices that actually carry a colour: a legend whose every swatch
        # is the same default explains nothing and costs a row of the page.
        legend=[
            (colours.get(spec.color_field, {}).get(value, value), colour)
            for value, colour in palette.items()
            if colour
        ],
        placed=total,
        unplaced=unplaced,
        tile_url=view.settings.map_tile_url,
        attribution=view.settings.map_attribution,
        views=resource.switchable_views(),
        current_view=spec.name,
        search_term=view.param("q"),
    )


def _coordinates(record: Record, spec: Any) -> tuple[float, float] | None:
    """The record's point, or ``None`` if either half is unusable.

    A provider that cannot express "is not null" leaves the filtering to the
    shim, and a stored empty string passes it; this is where that is caught.
    """
    raw_lat, raw_lon = record.get(spec.lat_field), record.get(spec.lon_field)
    if raw_lat is None or raw_lon is None:
        return None
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


async def _count_unplaced(
    view: View, resource: Resource, spec: Any, user_filter: Any, search: str | None
) -> int:
    """How many matching records have no coordinates, or 0 if unknowable."""
    query = resource.build_query(
        view.identity,
        filter=and_(
            user_filter,
            or_(
                Condition(spec.lat_field, Op.IS_NULL),
                Condition(spec.lon_field, Op.IS_NULL),
            ),
        ),
        search=search,
        page=1,
        page_size=1,
    )
    try:
        return (await resource.provider.list(query, view.ctx)).total or 0
    except Exception:
        # The map itself is fine without this number; failing the page over a
        # footnote would not be.
        return 0


def _choice_colours(resource: Resource, field_name: str, view: View) -> dict[Any, str]:
    field = resource.get_field(field_name)
    if field is None:
        return {}
    return {c.value: (c.color or "") for c in field.choices(view.ctx)}


# -- activity --------------------------------------------------------------

#: How a due date reads against today, worst first. The order is the priority
#: a cell inherits from the most pressing record in it.
ACTIVITY_STATES = ("overdue", "today", "planned")


def _activity_state(due: date | None, today: date) -> str:
    if due is None:
        return "planned"
    if due < today:
        return "overdue"
    return "today" if due == today else "planned"


async def _render_activity(view: View, resource: Resource, spec: Any) -> Response:
    """Outstanding work, crossed by who owns it and what kind it is.

    Counted from the records rather than from an aggregate, because the state
    of a cell depends on comparing each due date to today -- something no
    portable GROUP BY expresses, and something that would be wrong by morning
    if it were stored.
    """
    today = clock.today(view.timezone)
    query = resource.build_query(
        view.identity,
        filter=and_(
            parse_filters(view.request.query_params, resource, view.identity),
            _quick_filter(view, resource),
            spec.done_filter,
        ),
        search=view.param("q") or None,
        sort=tuple(spec.default_sort) or (Sort(spec.due_field, SortDir.ASC),),
        page=1,
        page_size=spec.limit,
        with_total=False,
    )
    page = await resource.provider.list(query, view.ctx)
    records = await resolve_relations(view, resource, page.items)

    labels = _group_labels(resource, spec, view)
    activity_field = resource.get_field(spec.activity_field)
    activities = (
        [(c.value, c.label) for c in activity_field.choices(view.ctx)]
        if activity_field is not None and activity_field.choices(view.ctx)
        else [
            (v, str(labels.get(spec.activity_field, {}).get(v, v)))
            for v in dict.fromkeys(r.get(spec.activity_field) for r in records)
        ]
    )

    cells: dict[tuple[Any, Any], dict[str, Any]] = {}
    rows: dict[Any, str] = {}
    for record in records:
        row_value = record.get(spec.row_field)
        rows.setdefault(
            row_value,
            str(
                record.get(f"{spec.row_field}_label")
                or labels.get(spec.row_field, {}).get(row_value)
                or (row_value if row_value is not None else "Unassigned")
            ),
        )
        key = (row_value, record.get(spec.activity_field))
        due = clock.parse_date(record.get(spec.due_field))
        cell = cells.setdefault(key, {"records": [], "state": "planned", "due": None})
        cell["records"].append(record)
        state = _activity_state(due, today)
        if ACTIVITY_STATES.index(state) < ACTIVITY_STATES.index(cell["state"]):
            cell["state"] = state
        if due is not None and (cell["due"] is None or due < cell["due"]):
            cell["due"] = due

    return view.render_view(
        resource,
        "activity",
        spec=spec,
        rows=[{"value": value, "label": label} for value, label in rows.items()],
        activities=[{"value": value, "label": label} for value, label in activities],
        cells=cells,
        today=today,
        counted=len(records),
        truncated=len(records) >= spec.limit,
        views=resource.switchable_views(),
        current_view=spec.name,
        search_term=view.param("q"),
    )


# -- dashboard -------------------------------------------------------------


async def _render_dashboard(view: View, resource: Resource, spec: Any) -> Response:
    """A grid of panels, each of which is another view fetched on its own.

    The panels a caller may not read are dropped here rather than left to fail
    one by one in the browser: a dashboard of permission errors is not a
    dashboard, and the panel handler would refuse them anyway.
    """
    panels = []
    for panel in spec.panels:
        target = panel.resource or resource.name
        if not view.registry.has_resource(target):
            continue
        other = view.registry.resource(target)
        if not other.policy.allows("read", view.identity):
            continue
        panels.append(
            {
                "panel": panel,
                "resource": other,
                "title": panel.title or f"{other.label_plural} · {other.view(panel.view).label}",
                "url": panel.url(resource.name),
            }
        )

    return view.render_view(
        resource,
        "dashboard",
        spec=spec,
        panels=panels,
        views=resource.switchable_views(),
        current_view=spec.name,
    )


# -- reading one record ----------------------------------------------------


@router.get("/{resource_name}/new")
async def new_record(resource_name: str, view: View = Depends(build_view)) -> Response:
    """An empty create form."""
    resource = view.resource(resource_name)
    require_can(resource, "create", view.identity)

    engine = FormEngine(resource)
    initial = {
        k[len("with.") :]: v
        for k, v in view.request.query_params.items()
        if k.startswith("with.")
    }
    form = engine.build(view.identity, initial=initial)
    return _render_form(view, resource, form, title=f"New {resource.label.lower()}")


# -- typeahead and export --------------------------------------------------
#
# These must be declared BEFORE the /{resource_name}/{pk} route below. Routes
# match in declaration order, so with the catch-all first, a request for
# /r/contacts/options would be read as a record whose key is 'options'.

@router.get("/{resource_name}/options")
async def field_options(resource_name: str, view: View = Depends(build_view)) -> Response:
    """Suggestions for a relation field's typeahead.

    Served by the *target* resource, so a relation across two different backends
    needs no join -- each side is queried through its own provider.
    """
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)

    term = view.param("q")
    query = resource.build_query(
        view.identity, search=term or None, page=1, page_size=view.int_param("limit", 20)
    )
    page = await resource.provider.list(query, view.ctx)

    options = [
        {"value": r.pk, "label": resource.display_value(r)} for r in page.items
    ]
    if view.wants_json:
        return view.json({"options": options})
    return view.render("views/_options.html", resource=resource, options=options, term=term)


@router.get("/{resource_name}/export.csv")
async def export_csv(resource_name: str, view: View = Depends(build_view)) -> Response:
    """Stream the current filtered list as CSV.

    Streamed in pages rather than materialised: an export of a large table
    should not hold the whole result set in memory.
    """
    import csv
    import io

    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)

    spec = resource.view("list")
    columns = [c.field for c in _visible_columns(resource, spec, view)]
    fields = [resource.field(name) for name in columns]

    base = _build_query(view, resource, spec).with_(page_size=500, page=1, with_total=False)

    async def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([f.label for f in fields])
        yield _drain(buffer)

        page_number = 1
        while True:
            page = await resource.provider.list(base.with_(page=page_number), view.ctx)
            if not page.items:
                break
            for record in page.items:
                writer.writerow([_csv_value(f, record) for f in fields])
            yield _drain(buffer)
            if not page.has_next:
                break
            page_number += 1

    filename = f"{resource.name}.csv"
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -- reading one record ----------------------------------------------------


@router.get("/{resource_name}/{pk}")
async def show_record(resource_name: str, pk: str, view: View = Depends(build_view)) -> Response:
    """The detail page for one record."""
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)

    record = await _load(view, resource, pk)
    record = (await resolve_relations(view, resource, [record]))[0]
    spec = resource.view("detail")

    related = await _load_related(view, resource, record)

    if view.wants_json:
        return view.json(dict(record))

    return view.render_view(
        resource,
        "detail",
        spec=spec,
        record=record,
        related=related,
        title=resource.display_value(record),
        actions=resource.actions_for("detail", record, view.identity),
        can_update=resource.can("update", view.identity, record),
        can_delete=resource.can("delete", view.identity, record),
    )


async def _load(view: View, resource: Resource, pk: str) -> Record:
    """Fetch a record, honouring the caller's row scope.

    A record outside the caller's scope reads as absent rather than forbidden,
    which avoids confirming that a given key exists. Both the missing case and
    the out-of-scope one therefore end here the same way, which is the point.

    Where there is a scope, it goes *into* the query rather than being checked
    after the fetch. Fetching by key first assumes the key selects at most one
    row, and a scope is exactly what makes that assumption worth doubting: on a
    versioned table one key matches several rows, ``get`` returns whichever the
    backend happens to order first -- often a superseded one -- and that row
    then fails the scope test. The record listed on the previous screen 404s
    when opened. Putting the scope in the query also saves a fetch-then-discard
    round trip.

    Without a scope there is nothing to fold in, so the plain keyed read stays:
    it is what a provider can answer most cheaply, and several do so without
    building a query at all.
    """
    if resource.policy.scope(view.identity) is None:
        record = await resource.provider.get(pk, view.ctx)
    else:
        query = resource.build_query(
            view.identity, filter=resource.pk_filter(pk), page_size=1, with_total=False
        )
        page = await resource.provider.list(query, view.ctx)
        record = page.items[0] if page.items else None

    if record is None:
        raise NotFound(f"That {resource.label.lower()} does not exist.")
    return record


async def _load_related(view: View, resource: Resource, record: Record) -> dict[str, Any]:
    """Resolve the record's backref fields into embedded lists."""
    related: dict[str, Any] = {}
    for backref in resource.backrefs:
        if not view.registry.has_resource(backref.target_resource):
            continue
        target = view.registry.resource(backref.target_resource)
        if not target.policy.allows("read", view.identity):
            continue
        query = target.build_query(
            view.identity,
            filter=Condition(backref.via, Op.EQ, record.pk),
            page_size=backref.limit,
        )
        page = await target.provider.list(query, view.ctx)
        page = Page(
            items=await resolve_relations(view, target, page.items),
            page=page.page, page_size=page.page_size, total=page.total,
        )
        related[backref.name] = {
            "field": backref,
            "resource": target,
            "page": page,
            "columns": backref.columns or tuple(
                f.name for f in target.fields if f.in_list
            )[:4],
        }
    return related


@router.get("/{resource_name}/{pk}/children")
async def tree_children(
    resource_name: str, pk: str, view: View = Depends(build_view)
) -> Response:
    """One branch of a tree, fetched when its row is opened.

    The same fragment answers every level, so the depth comes in as a parameter
    and goes back out one higher on each row's own toggle. That is what makes
    the recursion work without the server ever holding the whole tree: each
    request knows only the node it was asked about.
    """
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)

    # Typed loosely, like every other handler here: a view spec is whatever
    # kind it says it is, and the guard below is what makes that safe.
    spec: Any = resource.view(view.param("view") or "tree")
    if spec.kind != "tree":
        raise NotFound(f"{resource.label} has no tree view called {view.param('view')!r}.")

    child = _tree_child_resource(view, resource, spec)
    require_can(child, "read", view.identity)

    depth = max(1, view.int_param("depth", 1))
    if depth > spec.max_depth:
        # A cycle in the data would otherwise recurse until the browser gives
        # up. The row says why it stopped rather than simply showing nothing.
        return view.render(
            "views/_tree_rows.html", resource=resource, spec=spec, nodes=[],
            columns=[], depth=depth, too_deep=True, child_resource=child,
        )

    query = child.build_query(
        view.identity,
        filter=Condition(spec.parent_field, Op.EQ, _parent_value(child, spec, pk)),
        sort=tuple(spec.default_sort),
        page=1,
        page_size=spec.child_limit,
        with_total=False,
    )
    page = await child.provider.list(query, view.ctx)
    records = await resolve_relations(view, child, page.items)

    return view.render(
        "views/_tree_rows.html",
        resource=child,
        spec=spec,
        nodes=await _tree_nodes(view, child, spec, records, depth),
        columns=_visible_columns(child, _child_columns(child, spec), view),
        depth=depth,
        too_deep=False,
        child_resource=child,
    )


def _child_columns(child: Resource, spec: Any) -> Any:
    """What to show for a child row.

    A self-join reuses the tree's own columns -- the rows are the same kind of
    thing. A child on another resource cannot: its fields are different ones,
    so it falls back to that resource's list view.
    """
    return spec if spec.self_referential else child.view("list")


@router.get("/{resource_name}/{pk}/related/{field_name}")
async def related_fragment(
    resource_name: str, pk: str, field_name: str, view: View = Depends(build_view)
) -> Response:
    """One related list, refreshed on its own."""
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)
    record = await _load(view, resource, pk)
    related = await _load_related(view, resource, record)
    entry = related.get(field_name)
    if entry is None:
        raise NotFound(f"No related list called {field_name!r}.")
    return view.render("views/_related.html", resource=resource, record=record, entry=entry)


# -- history ---------------------------------------------------------------


@router.get("/{resource_name}/{pk}/timeline")
async def record_timeline(
    resource_name: str, pk: str, view: View = Depends(build_view)
) -> Response:
    """What has happened to this record.

    Reads the audit log rather than a separate activity table, so the history
    is a by-product of the writes themselves and cannot drift from them.
    """
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)
    # Load the record first: someone who cannot see it must not be able to read
    # its history either.
    record = await _load(view, resource, pk)

    entries: list[Record] = []
    if view.registry.has_resource("audit_log"):
        log_resource = view.registry.resource("audit_log")
        query = ListQuery(
            filter=and_(
                Condition("resource", Op.EQ, resource.name),
                Condition("record_id", Op.EQ, str(pk)),
            ),
            sort=(Sort("at", SortDir.DESC),),
            page_size=view.int_param("limit", 25),
            with_total=False,
        )
        page = await log_resource.provider.list(query, view.ctx)
        entries = list(page.items)

    return view.render(
        "views/_timeline.html",
        resource=resource,
        record=record,
        entries=[_readable_entry(e, view) for e in entries],
    )


def _readable_entry(entry: Record, view: View) -> dict[str, Any]:
    """Turn a stored audit row into something a template can render."""
    import json

    raw = entry.get("changes")
    changes: list[dict[str, Any]] = []
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            parsed = {}
        target = view.registry.resource(str(entry.get("resource", "")))\
            if view.registry.has_resource(str(entry.get("resource", ""))) else None
        for name, pair in (parsed or {}).items():
            field = target.get_field(name) if target else None
            before, after = (pair + [None, None])[:2] if isinstance(pair, list) else (None, pair)
            changes.append({
                "field": name,
                "label": field.label if field else name,
                "before": before,
                "after": after,
            })
    return {
        "at": entry.get("at"),
        "actor": entry.get("actor_name") or entry.get("actor") or "Unknown",
        "action": entry.get("action", ""),
        "status": entry.get("status", "ok"),
        "detail": entry.get("detail"),
        "changes": changes,
    }


# -- writing ---------------------------------------------------------------


def _render_form(
    view: View,
    resource: Resource,
    form: Any,
    *,
    title: str,
    status_code: int = 200,
    record: Record | None = None,
) -> Response:
    """Render a create or edit form, as a page or a modal fragment."""
    spec = resource.view("form")
    context = {
        "spec": spec,
        "form": form,
        "record": record,
        "title": title,
        "action": _form_action(resource, record),
    }
    if view.is_fragment:
        # A modal request gets the dialog wrapper; an ordinary fragment -- a
        # rejected submission swapping itself -- gets just the form, or it
        # would nest a second dialog inside the first.
        template = (
            "views/_form_modal.html"
            # getattr, because a project may substitute its own view class
            # for the form, and it need not have declared this setting.
            if view.param("modal") and getattr(spec, "modal", False)
            else "views/_form_body.html"
        )
        return view.render(template, resource=resource, status_code=status_code, **context)
    return view.render_view(resource, "form", status_code=status_code, **context)


def _form_action(resource: Resource, record: Record | None) -> str:
    if record is None:
        return f"/r/{resource.name}"
    return f"/r/{resource.name}/{record.pk}"


@router.post("/{resource_name}")
async def create_record(
    resource_name: str, request: Request, view: View = Depends(build_view)
) -> Response:
    """Create from a submitted form."""
    resource = view.resource(resource_name)
    require_can(resource, "create", view.identity)

    engine = FormEngine(resource)
    data = await request.form()
    form = engine.process(data, view.identity, view.ctx)

    if not form.valid:
        # 422 so the browser and any API client both see a rejection, while
        # HTMX still swaps the re-rendered form with its errors in place.
        return _render_form(
            view, resource, form, title=f"New {resource.label.lower()}", status_code=422
        )

    payload = engine.to_storage(form)
    # Files are stored before the record, since the column holds a reference to
    # them. A validation failure above therefore costs no storage at all.
    uploaded, replaced = await store_uploads(data, resource, view.identity)
    payload.update(uploaded)

    result = await resource.provider.create(payload, view.ctx)
    if result.accepted:
        await discard(replaced)
    return _after_write(view, resource, result, form, created=True)


# Declared before the per-record write routes below: /r/x/bulk/delete would
# otherwise match /{resource_name}/{pk}/delete with pk='bulk'.
@router.post("/{resource_name}/bulk/{action_name}")
async def run_bulk_action(
    resource_name: str, action_name: str, request: Request, view: View = Depends(build_view)
) -> Response:
    """Run an action over the rows the user selected."""
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)

    form = await request.form()
    keys = [k for k in form.getlist("selected") if k]
    if not keys:
        empty: Response = view.render("views/_noop.html")
        return view.toast(empty, "Nothing was selected.", "warning")

    records = []
    for key in keys:
        try:
            records.append(await _load(view, resource, str(key)))
        except NotFound:
            # A row deleted by someone else between selecting and submitting is
            # skipped rather than failing the whole batch.
            continue

    if action_name == "delete":
        return await _bulk_delete(view, resource, records)

    action = resource.action(action_name)
    if action.handler is None:
        raise ValidationFailed({action_name: "That action cannot be run over a selection."})
    allowed = [r for r in records if action.visible_for(r, view.identity)]
    result = await action.handler(allowed, view.ctx, resource)
    return _after_action(
        view, resource, action, result, back=f"/r/{resource.name}", count=len(allowed)
    )


async def _bulk_delete(view: View, resource: Resource, records: list[Record]) -> Response:
    """Delete several records, reporting partial success honestly."""
    require_can(resource, "delete", view.identity)
    deleted, failed = 0, 0
    for record in records:
        if not resource.can("delete", view.identity, record):
            failed += 1
            continue
        try:
            result = await resource.provider.delete(record.pk, view.ctx)
            deleted += 1 if result.accepted else 0
            failed += 0 if result.accepted else 1
        except Exception:
            failed += 1

    response: Response = view.redirect(f"/r/{resource.name}")
    if failed and deleted:
        return view.toast(response, f"Deleted {deleted}; {failed} could not be deleted.", "warning")
    if failed:
        return view.toast(response, f"Could not delete {failed} record(s).", "error")
    return view.toast(response, f"Deleted {deleted} record(s).", "success")


@router.get("/{resource_name}/{pk}/edit")
async def edit_record(resource_name: str, pk: str, view: View = Depends(build_view)) -> Response:
    """The edit form for one record."""
    resource = view.resource(resource_name)
    record = await _load(view, resource, pk)
    require_can(resource, "update", view.identity, record)

    form = FormEngine(resource).build(view.identity, record=record)
    return _render_form(
        view, resource, form, title=f"Edit {resource.display_value(record)}", record=record
    )


@router.post("/{resource_name}/{pk}")
async def update_record(
    resource_name: str, pk: str, request: Request, view: View = Depends(build_view)
) -> Response:
    """Apply an edit."""
    resource = view.resource(resource_name)
    record = await _load(view, resource, pk)
    require_can(resource, "update", view.identity, record)

    engine = FormEngine(resource)
    data = await request.form()
    form = engine.process(data, view.identity, view.ctx, record=record)

    if not form.valid:
        return _render_form(
            view,
            resource,
            form,
            title=f"Edit {resource.display_value(record)}",
            record=record,
            status_code=422,
        )

    payload = engine.to_storage(form)
    uploaded, replaced = await store_uploads(
        data, resource, view.identity, previous=record
    )
    payload.update(uploaded)

    result = await resource.provider.update(pk, payload, view.ctx)
    # Only once the record is safely written: deleting first would lose the old
    # file if the write then failed.
    if result.accepted:
        await discard(replaced)
    return _after_write(view, resource, result, form, created=False, pk=pk)


@router.post("/{resource_name}/{pk}/delete")
async def delete_record(resource_name: str, pk: str, view: View = Depends(build_view)) -> Response:
    """Delete one record."""
    resource = view.resource(resource_name)
    record = await _load(view, resource, pk)
    require_can(resource, "delete", view.identity, record)

    result = await resource.provider.delete(pk, view.ctx)

    if view.wants_json:
        return view.json({"status": result.status.value, "message": result.message})

    response = view.redirect(f"/r/{resource.name}")
    message = result.message or f"{resource.label} deleted."
    return view.toast(response, message, "success" if result.ok else "info")


def _after_write(
    view: View,
    resource: Resource,
    result: Any,
    form: Any,
    *,
    created: bool,
    pk: Any = None,
) -> Response:
    """Turn a provider write result into the right response.

    The three statuses need genuinely different handling. A rejection re-renders
    the form with per-field errors. A queued write cannot navigate to the record
    -- it may not exist yet -- so it reports back and stays put. Only a
    confirmed write navigates.
    """
    if result.failed:
        for field_name, message in result.errors.items():
            if field_name in form:
                form[field_name].error = message
        form.errors.update(result.errors)
        form.message = result.message
        title = f"New {resource.label.lower()}" if created else f"Edit {resource.label.lower()}"
        return _render_form(view, resource, form, title=title, status_code=422)

    if view.wants_json:
        payload = dict(result.record) if result.record else {}
        return view.json(
            {"status": result.status.value, "record": payload, "message": result.message},
            status_code=201 if created and result.ok else 200,
        )

    if result.status is WriteStatus.PENDING:
        pending: Response = view.render(
            "views/_pending.html",
            resource=resource,
            result=result,
            message=result.message,
        )
        return view.toast(pending, result.message or "Queued.", "info")

    target = result.record.pk if result.record is not None else pk
    response = view.redirect(f"/r/{resource.name}/{target}")
    return view.toast(
        response,
        f"{resource.label} {'created' if created else 'saved'}.",
        "success",
    )


# -- editing one cell in place ---------------------------------------------


@router.get("/{resource_name}/{pk}/field/{field_name}")
async def field_editor(
    resource_name: str, pk: str, field_name: str, view: View = Depends(build_view)
) -> Response:
    """Swap a table cell for an input."""
    resource = view.resource(resource_name)
    record = await _load(view, resource, pk)
    require_can(resource, "update", view.identity, record)

    field = resource.field(field_name)
    # The editor's cancel button asks for the cell back. Without this it would
    # be handed another editor, which looks like the button doing nothing.
    if view.param("cancel"):
        return _cell(view, resource, record, field)

    form = FormEngine(resource).build_single(view.identity, field_name, record)
    bound = form[field_name]
    if not bound.editable:
        # Not editable: hand back the display cell so the swap is a no-op the
        # user can see, rather than an empty gap.
        return _cell(view, resource, record, bound.field)
    return view.render(
        "views/_cell_edit.html", resource=resource, record=record, bound=bound, form=form
    )


@router.post("/{resource_name}/{pk}/field/{field_name}")
async def save_field(
    resource_name: str,
    pk: str,
    field_name: str,
    request: Request,
    view: View = Depends(build_view),
) -> Response:
    """Save one field and hand back the rendered cell."""
    resource = view.resource(resource_name)
    record = await _load(view, resource, pk)
    require_can(resource, "update", view.identity, record)

    field = resource.field(field_name)
    writable = set(resource.policy.writable_fields(view.identity, resource))
    if field_name not in writable or not field.inline_editable:
        raise ValidationFailed({field_name: "That field cannot be edited here."})

    engine = FormEngine(resource)
    data = await request.form()
    form = engine.process(
        data, view.identity, view.ctx, record=record, fields=[field_name], partial=True
    )

    if not form.valid:
        bound = form[field_name]
        editor: Response = view.render(
            "views/_cell_edit.html",
            resource=resource,
            record=record,
            bound=bound,
            form=form,
            status_code=422,
        )
        return view.toast(editor, bound.error, "error")

    result = await resource.provider.update(pk, engine.to_storage(form), view.ctx)

    if result.failed:
        response = _cell(view, resource, record, field)
        return view.toast(response, result.message, "error")

    updated = result.record if result.record is not None else record.merge(form.values())
    response = _cell(view, resource, updated, field, pending=result.status is WriteStatus.PENDING)
    if result.status is WriteStatus.PENDING:
        return view.toast(response, result.message or "Queued.", "info")
    return view.toast(response, "Saved.", "success")


def _cell(
    view: View, resource: Resource, record: Record, field: Any, *, pending: bool = False
) -> Response:
    return view.render(
        "views/_cell.html", resource=resource, record=record, field=field, pending=pending
    )


# -- actions ---------------------------------------------------------------


@router.post("/{resource_name}/{pk}/action/{action_name}")
async def run_action(
    resource_name: str,
    pk: str,
    action_name: str,
    request: Request,
    view: View = Depends(build_view),
) -> Response:
    """Run a declared action against one record."""
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)

    action = resource.action(action_name)
    record = await _load(view, resource, pk)
    if not action.visible_for(record, view.identity):
        from app.core.errors import PermissionDenied

        raise PermissionDenied(f"You cannot run {action.label!r} on this record.")

    if action.url or action.handler is None:
        return view.redirect(action.resolve_url(record))

    result = await action.handler([record], view.ctx, resource)
    return _after_action(view, resource, action, result, back=f"/r/{resource.name}/{pk}")


def _after_action(
    view: View, resource: Resource, action: Any, result: Any, *, back: str, count: int = 1
) -> Response:
    from app.resources.actions import ActionResult

    outcome = result or ActionResult(message=f"{action.label} done.")
    if view.wants_json:
        return view.json({"message": outcome.message, "level": outcome.level})

    target = outcome.redirect or (back if outcome.refresh else "")
    response: Response = view.redirect(target) if target else view.render("views/_noop.html")
    message = outcome.message or f"{action.label} applied to {count} record(s)."
    return view.toast(response, message, outcome.level)


def _drain(buffer) -> str:
    value = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return value


def _csv_value(field: Any, record: Record) -> str:
    """Render a value for a spreadsheet: plain, unformatted, no markup."""
    value = field.to_display(field.extract(record))
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)
