"""Reading filters out of the query string.

Filters travel in the URL so a filtered list is a shareable, bookmarkable link
and the back button works. Two grammars are read here, but only one of them is
ever put in a link.

*Canonical* -- the readable form every link, chip and bookmark uses::

    ?f.stage=won&f.amount__gte=1000&f.closed__is_null=

*Panel* -- the indexed triples the filter builder submits, because an HTML
``<select>`` cannot rewrite the ``name`` of the input beside it::

    ?fc.0.field=amount&fc.0.op=gte&fc.0.value=1000

A request carrying panel triples is normalised into the canonical form and
redirected, so the panel is an input method and never an address: what lands in
the address bar, the history and a shared link is always the readable one.

Values may be written as aliases -- ``@me``, ``@today``, ``@month_start`` --
resolved per request against the caller and their timezone. That is what makes
a saved filter worth saving: "my deals closing this month" stays true tomorrow,
and means something different for each person who opens the link. A literal
leading at-sign is written ``@@``.

Anything referencing a field the resource does not declare, an operator that
field does not support, or a field the caller may not read is dropped rather
than passed through -- a typo in a URL should narrow nothing, never widen
access or reach the backend.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from app.core import clock
from app.core.query import SEQUENCE_OPS, UNARY_OPS, Condition, Filter, Op, Sort, and_
from app.core.results import ANONYMOUS, Identity
from app.fields.base import Field
from app.fields.types import RelationField
from app.resources.resource import Resource

PREFIX = "f."
SEPARATOR = "__"

#: Prefix of the filter builder's own triples. Never appears in a generated
#: link -- see the module docstring.
PANEL_PREFIX = "fc."

#: Marks a value that begins an alias rather than literal text.
ALIAS_SIGIL = "@"


# -- value aliases ---------------------------------------------------------


class _Unresolved:
    """An alias nothing answered to. Distinct from ``None``, which is a value."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNRESOLVED"


UNRESOLVED = _Unresolved()


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _me(identity: Identity) -> str | None:
    if not identity.is_authenticated:
        return None
    return identity.email or identity.subject or None


#: Aliases every resource understands. Each returns a *Python* value -- a date,
#: not an ISO string -- and the field it lands on does the final coercion, so
#: ``@today`` works on a date column and a datetime one alike.
#:
#: Dates are computed in the *caller's* zone, not the server's: at 23:00 in
#: London it is already tomorrow in Tokyo, and a "due today" link that used the
#: server's date would be wrong for half its readers.
BUILTIN_ALIASES: dict[str, Callable[[Identity], Any]] = {
    "me": _me,
    "now": lambda i: clock.utcnow(),
    "today": lambda i: clock.today(i.timezone),
    "yesterday": lambda i: clock.today(i.timezone) - timedelta(days=1),
    "tomorrow": lambda i: clock.today(i.timezone) + timedelta(days=1),
    "week_start": lambda i: _week_start(clock.today(i.timezone)),
    "week_end": lambda i: _week_start(clock.today(i.timezone)) + timedelta(days=6),
    "month_start": lambda i: clock.today(i.timezone).replace(day=1),
    "month_end": lambda i: _month_end(clock.today(i.timezone)),
    "year_start": lambda i: clock.today(i.timezone).replace(month=1, day=1),
}

#: Aliases offered in the filter builder's value menu, with their wording.
ALIAS_LABELS: dict[str, str] = {
    "me": "me",
    "today": "today",
    "yesterday": "yesterday",
    "tomorrow": "tomorrow",
    "week_start": "start of this week",
    "week_end": "end of this week",
    "month_start": "start of this month",
    "month_end": "end of this month",
    "year_start": "start of this year",
}


def resolve_alias(raw: Any, identity: Identity, aliases: Mapping[str, Any]) -> Any:
    """Expand an ``@alias``, or hand back a literal unchanged.

    Returns :data:`UNRESOLVED` for an alias nobody defines, so the caller can
    drop the condition instead of filtering on the string ``"@yesteday"``.
    """
    if not isinstance(raw, str) or not raw.startswith(ALIAS_SIGIL):
        return raw
    if raw.startswith(ALIAS_SIGIL * 2):
        return raw[1:]

    name = raw[1:].strip().casefold()
    # A resource's own aliases win, so a project can redefine "@me" for a
    # resource whose owner column holds something other than an email.
    if name in aliases:
        build = aliases[name]
    elif name in BUILTIN_ALIASES:
        build = BUILTIN_ALIASES[name]
    else:
        return UNRESOLVED

    value = build(identity) if callable(build) else build
    return UNRESOLVED if value is None else value


#: Column types that could plausibly hold a person, and so are worth offering
#: "@me" on. A status or an amount is not one of them.
IDENTITY_COLUMNS = frozenset({"text", "email", "relation"})

#: Aliases that only make sense against a calendar column.
TEMPORAL_ALIASES = (
    "today",
    "yesterday",
    "tomorrow",
    "week_start",
    "week_end",
    "month_start",
    "month_end",
    "year_start",
)


def alias_choices(resource: Resource, field: Field) -> list[dict[str, str]]:
    """Aliases worth offering on ``field``, as ``{value, label}`` pairs.

    Offering "@today" on a name column, or "@me" on a date, would be noise in
    the menu -- so the suggestions follow the column's type. A resource's own
    aliases are always offered: their author knows where they apply.
    """
    if field.type_name in ("date", "datetime"):
        names = list(TEMPORAL_ALIASES)
    elif field.type_name in IDENTITY_COLUMNS:
        names = ["me"]
    else:
        # A status, an amount, a checkbox: no built-in alias means anything here.
        names = []
    names += [n for n in resource.search.value_aliases if n not in names]
    return [
        {"value": f"{ALIAS_SIGIL}{name}", "label": ALIAS_LABELS.get(name, name.replace("_", " "))}
        for name in names
    ]


# -- canonical parsing -----------------------------------------------------


def parse_filters(
    params: Mapping[str, str] | Sequence[tuple[str, str]],
    resource: Resource,
    identity: Identity = ANONYMOUS,
) -> Filter | None:
    """Build a filter from ``f.*`` query parameters."""
    items = params.items() if isinstance(params, Mapping) else list(params)
    aliases = resource.search.value_aliases
    conditions: list[Filter] = []

    for key, raw in items:
        if not key.startswith(PREFIX):
            continue
        spec = key[len(PREFIX) :]
        field_name, _, op_name = spec.partition(SEPARATOR)

        field = resource.get_field(field_name)
        if field is None or not field.visible_to(identity):
            # Filtering on a column the caller cannot read is a yes/no oracle
            # over its contents, so an unreadable field is not filterable.
            continue

        op = _parse_op(op_name, field)
        if op is None or op not in field.supported_ops:
            continue

        # An empty value means "no filter" for everything except the operators
        # that legitimately take none, so a cleared filter box removes itself.
        if raw == "" and op not in UNARY_OPS:
            continue

        try:
            value = _parse_value(field, op, raw, identity, aliases)
            if value is None and op not in UNARY_OPS:
                continue
            if op in SEQUENCE_OPS and not value:
                continue
            conditions.append(Condition(field_name, op, value))
        except Exception:
            # A value the field cannot parse -- or a "between" left with one
            # end -- narrows nothing rather than 500ing.
            continue

    return and_(*conditions)


def _parse_value(
    field: Field, op: Op, raw: Any, identity: Identity, aliases: Mapping[str, Any]
) -> Any:
    """Expand aliases, then let the field coerce the result."""
    if op in UNARY_OPS:
        return None

    if op in SEQUENCE_OPS:
        tokens = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        expanded = [resolve_alias(str(t).strip(), identity, aliases) for t in tokens]
        if any(v is UNRESOLVED for v in expanded):
            return None
        return field.parse_filter_value(op, expanded)

    value = resolve_alias(raw, identity, aliases)
    if value is UNRESOLVED:
        return None
    return field.parse_filter_value(op, value)


def _parse_op(name: str, field: Field) -> Op | None:
    if not name:
        # Bare `f.stage=won` means equality, the common case.
        return Op.EQ
    try:
        return Op(name)
    except ValueError:
        return None


def parse_sort(raw: str, resource: Resource) -> tuple[Sort, ...]:
    """Read ``?sort=-amount,name`` into sort keys, ignoring unknown columns."""
    if not raw:
        return ()
    sorts = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        sort = Sort.parse(token)
        field = resource.get_field(sort.field)
        if field is None or not field.sortable:
            continue
        sorts.append(sort)
    return tuple(sorts)


def encode_filter(field_name: str, op: Op, value: str = "") -> str:
    """Build the query-string key/value for one condition, for links."""
    return f"{filter_key(field_name, op)}={value}"


def filter_key(field_name: str, op: Op) -> str:
    """The canonical query-string key for one condition."""
    if op is Op.EQ:
        return f"{PREFIX}{field_name}"
    return f"{PREFIX}{field_name}{SEPARATOR}{op.value}"


# -- the panel's own grammar -----------------------------------------------


def has_panel_params(params: Mapping[str, str] | Sequence[tuple[str, str]]) -> bool:
    """Whether a request came from the filter builder rather than a link."""
    keys = params.keys() if isinstance(params, Mapping) else (k for k, _ in params)
    return any(k.startswith(PANEL_PREFIX) for k in keys)


def canonical_params(items: Sequence[tuple[str, str]], resource: Resource) -> list[tuple[str, str]]:
    """Rewrite panel triples into canonical ``f.*`` pairs.

    The panel always submits the *complete* set of conditions -- it is
    pre-loaded with the ones already applied -- so any ``f.*`` arriving
    alongside it is the stale copy in the form's action URL and is dropped.
    Blank rows are dropped too, which is how a condition is removed.
    """
    rows: dict[str, dict[str, str]] = {}
    kept: list[tuple[str, str]] = []

    for key, value in items:
        if key.startswith(PANEL_PREFIX):
            index, _, part = key[len(PANEL_PREFIX) :].partition(".")
            if part in ("field", "op", "value"):
                rows.setdefault(index, {})[part] = value
            continue
        if key.startswith(PREFIX):
            continue
        if key == "page":
            # A changed filter set makes the old page number meaningless, and
            # landing on page 7 of a 2-page result reads as "no records".
            continue
        kept.append((key, value))

    for index in sorted(rows, key=_row_order):
        pair = _canonical_pair(rows[index], resource)
        if pair is not None:
            kept.append(pair)
    return kept


def _row_order(index: str) -> tuple[int, str]:
    """Panel rows are numbered, but sort defensively -- the numbers are input."""
    return (int(index), "") if index.isdigit() else (10**6, index)


def _canonical_pair(row: Mapping[str, str], resource: Resource) -> tuple[str, str] | None:
    field = resource.get_field(row.get("field", "").strip())
    if field is None:
        return None
    op = _parse_op(row.get("op", "").strip(), field)
    if op is None or op not in field.supported_ops:
        return None
    value = row.get("value", "").strip()
    if op in UNARY_OPS:
        return filter_key(field.name, op), ""
    if not value:
        return None
    return filter_key(field.name, op), value


# -- describing what is applied --------------------------------------------


def active_filters(params: Mapping[str, str], resource: Resource) -> list[dict[str, str]]:
    """The filters currently applied, described for chips and for the panel."""
    chips = []
    for key, raw in params.items():
        if not key.startswith(PREFIX):
            continue
        field_name, _, op_name = key[len(PREFIX) :].partition(SEPARATOR)
        field = resource.get_field(field_name)
        if field is None:
            continue
        op = _parse_op(op_name, field)
        if op is None:
            continue
        chips.append(
            {
                "key": key,
                "field": field_name,
                "label": field.label,
                "op": op.value,
                "op_label": OP_LABELS.get(op, op.value),
                "value": raw,
                "value_label": _value_label(field, op, raw),
            }
        )
    return chips


def _value_label(field: Field, op: Op, raw: str) -> str:
    """How a chip reads. Choice values show their label, aliases their wording."""
    if op in UNARY_OPS:
        return ""
    parts = []
    for token in raw.split(",") if op in SEQUENCE_OPS else [raw]:
        token = token.strip()
        if token.startswith(ALIAS_SIGIL) and not token.startswith(ALIAS_SIGIL * 2):
            name = token[1:].casefold()
            parts.append(ALIAS_LABELS.get(name, name.replace("_", " ")))
            continue
        parts.append(_choice_label(field, token))
    return ", ".join(p for p in parts if p)


def _choice_label(field: Field, token: str) -> str:
    for choice in field.choices(None):
        if str(choice.value) == token:
            return choice.label
    return token


def filter_schema(resource: Resource, identity: Identity = ANONYMOUS) -> list[dict[str, Any]]:
    """The filter builder's menus, as plain data for the browser.

    Which operators a row may offer depends on the field chosen in that row --
    "is before" belongs to a date and not to a checkbox -- so the panel needs
    the whole map up front rather than a round trip on every selection.
    """
    schema = []
    for field in resource.filterable_fields():
        if not field.supported_ops or not field.visible_to(identity):
            continue
        schema.append(
            {
                "name": field.name,
                "label": field.label,
                "input": INPUT_TYPES.get(field.type_name, "text"),
                # A relation's value is a key, so the row borrows the same
                # typeahead the form uses rather than asking for an id.
                "relation": field.relation.resource if isinstance(field, RelationField) else "",
                "aliases": alias_choices(resource, field),
                "choices": [{"value": str(c.value), "label": c.label} for c in field.choices(None)],
                "ops": [
                    {
                        "value": op.value,
                        "label": OP_LABELS.get(op, op.value),
                        "arity": _arity(op),
                    }
                    for op in field.filter_ops()
                ],
            }
        )
    return schema


def _arity(op: Op) -> str:
    if op in UNARY_OPS:
        return "none"
    if op is Op.BETWEEN:
        return "range"
    if op in SEQUENCE_OPS:
        return "many"
    return "one"


#: Human wording for each operator, used in the filter builder and chips.
OP_LABELS: dict[Op, str] = {
    Op.EQ: "is",
    Op.NE: "is not",
    Op.LT: "is before",
    Op.LTE: "is at most",
    Op.GT: "is after",
    Op.GTE: "is at least",
    Op.IN: "is any of",
    Op.NOT_IN: "is none of",
    Op.CONTAINS: "contains",
    Op.ICONTAINS: "contains",
    Op.STARTSWITH: "starts with",
    Op.ENDSWITH: "ends with",
    Op.IS_NULL: "is empty",
    Op.NOT_NULL: "is not empty",
    Op.BETWEEN: "is between",
}

#: The ``type`` a plain value box gets, so a date column offers a date picker
#: and a number column a numeric keypad. Only types that differ are listed.
INPUT_TYPES: dict[str, str] = {
    "integer": "number",
    "decimal": "number",
    "currency": "number",
    "percent": "number",
    "date": "date",
    "datetime": "datetime-local",
    "time": "time",
    "email": "email",
    "url": "url",
}
