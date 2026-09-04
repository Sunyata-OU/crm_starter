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
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response, StreamingResponse

from app.auth.permissions import require_can
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
)
from app.core.results import Record, WriteStatus
from app.resources.form_engine import FormEngine
from app.resources.resource import Resource
from app.web.deps import View, build_view
from app.web.filters import active_filters, parse_filters, parse_sort
from app.web.uploads import discard, store_uploads

router = APIRouter(prefix="/r", tags=["resources"])


# -- listing ---------------------------------------------------------------


@router.get("/{resource_name}")
async def list_records(resource_name: str, view: View = Depends(build_view)) -> Response:
    """The list page, or whichever alternative view was asked for."""
    resource = view.resource(resource_name)
    require_can(resource, "read", view.identity)

    kind = view.param("view") or _default_view_kind(resource)
    spec = resource.view(kind) if resource.has_view(kind) else resource.view("list")

    match spec.kind:
        case "board":
            return await _render_board(view, resource, spec)
        case "calendar":
            return await _render_calendar(view, resource, spec)
        case "chart" | "pivot":
            return await _render_chart(view, resource, spec)

    return await _render_list(view, resource, spec)


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
            parse_filters(view.request.query_params, resource),
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
    missing = keys - cached.keys()
    if not missing:
        return {k: cached[k] for k in keys if k in cached}

    query = target.build_query(
        view.identity,
        filter=Condition(target.pk, Op.IN, sorted(missing, key=str)),
        page_size=min(len(missing), 500),
        with_total=False,
    )
    try:
        page = await target.provider.list(query, view.ctx)
    except Exception:
        # A failing lookup must not take down the list it decorates; the key is
        # still shown and still links.
        return {k: cached[k] for k in keys if k in cached}

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
        "chips": active_filters(dict(view.request.query_params), resource),
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
        chips=active_filters(dict(view.request.query_params), resource),
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


async def _render_calendar(view: View, resource: Resource, spec: Any) -> Response:
    """Records placed on a month grid by their start date."""
    from calendar import Calendar
    from datetime import date

    today = date.today()
    year = view.int_param("year", today.year)
    month = max(1, min(12, view.int_param("month", today.month)))

    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1)

    from app.core.query import and_

    # Restrict to the visible month rather than fetching the whole table; a
    # calendar over a large resource would otherwise pull every row.
    query = resource.build_query(
        view.identity,
        filter=and_(
            parse_filters(view.request.query_params, resource),
            Condition(spec.start_field, Op.GTE, first.isoformat()),
            Condition(spec.start_field, Op.LT, last.isoformat()),
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

    weeks = Calendar(firstweekday=0).monthdatescalendar(year, month)

    return view.render_view(
        resource,
        "calendar",
        spec=spec,
        weeks=weeks,
        buckets=buckets,
        month=month,
        year=year,
        today=today,
        views=resource.switchable_views(),
        current_view=spec.name,
    )


async def _render_chart(view: View, resource: Resource, spec: Any) -> Response:
    """An aggregate drawn as a chart or laid out as a pivot table."""
    scope = resource.policy.scope(view.identity)
    user_filter = parse_filters(view.request.query_params, resource)

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
    which avoids confirming that a given key exists.
    """
    record = await resource.provider.get(pk, view.ctx)
    if record is None:
        raise NotFound(f"That {resource.label.lower()} does not exist.")
    scope = resource.policy.scope(view.identity)
    if scope is not None:
        from app.providers.local import match

        if not match(record, scope):
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
