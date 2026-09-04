"""Reading filters out of the query string.

Filters travel in the URL so a filtered list is a shareable, bookmarkable link
and the back button works. The encoding is deliberately readable:

    ?f.stage=won&f.amount__gte=1000&f.closed__is_null=

Anything referencing a field the resource does not declare, or an operator that
field does not support, is dropped rather than passed through -- a typo in a URL
should narrow nothing, never widen access or reach the backend.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.query import Condition, Filter, Op, Sort, and_
from app.resources.resource import Resource

PREFIX = "f."
SEPARATOR = "__"


def parse_filters(
    params: Mapping[str, str] | Sequence[tuple[str, str]], resource: Resource
) -> Filter | None:
    """Build a filter from ``f.*`` query parameters."""
    items = params.items() if isinstance(params, Mapping) else list(params)
    conditions: list[Filter] = []

    for key, raw in items:
        if not key.startswith(PREFIX):
            continue
        spec = key[len(PREFIX) :]
        field_name, _, op_name = spec.partition(SEPARATOR)

        field = resource.get_field(field_name)
        if field is None:
            continue

        op = _parse_op(op_name, field)
        if op is None:
            continue
        if op not in field.supported_ops:
            continue

        # An empty value means "no filter" for everything except the operators
        # that legitimately take none, so a cleared filter box removes itself.
        if raw == "" and op not in (Op.IS_NULL, Op.NOT_NULL):
            continue

        try:
            value = field.parse_filter_value(op, raw)
        except Exception:
            # A value the field cannot parse narrows nothing rather than 500ing.
            continue
        if value is None and op not in (Op.IS_NULL, Op.NOT_NULL):
            continue

        conditions.append(Condition(field_name, op, value))

    return and_(*conditions)


def _parse_op(name: str, field) -> Op | None:
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
    key = f"{PREFIX}{field_name}" if op is Op.EQ else f"{PREFIX}{field_name}{SEPARATOR}{op.value}"
    return f"{key}={value}"


def active_filters(
    params: Mapping[str, str], resource: Resource
) -> list[dict[str, str]]:
    """The filters currently applied, described for rendering removable chips."""
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
            }
        )
    return chips


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
