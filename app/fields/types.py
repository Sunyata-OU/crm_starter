"""The built-in field types.

Each type answers the same four questions: how does a submitted string become a
Python value, what is that value validated against, how is it rendered, and
which filter operators make sense for it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.errors import UnsupportedOperation, ValidationFailed
from app.core.query import Op
from app.core.results import Ctx
from app.fields.base import EMPTY, Choice, ChoiceSource, Field, RelationSpec
from app.fields.registry import register_field

TEXT_OPS = frozenset({
    Op.EQ, Op.NE, Op.ICONTAINS, Op.STARTSWITH, Op.ENDSWITH, Op.IN, Op.IS_NULL, Op.NOT_NULL,
})
ORDERED_OPS = frozenset({
    Op.EQ, Op.NE, Op.LT, Op.LTE, Op.GT, Op.GTE, Op.BETWEEN, Op.IS_NULL, Op.NOT_NULL,
})
CHOICE_OPS = frozenset({Op.EQ, Op.NE, Op.IN, Op.NOT_IN, Op.IS_NULL, Op.NOT_NULL})


# -- text ------------------------------------------------------------------


@register_field("text")
class TextField(Field):
    """A single line of text."""

    display_template = "text.html"
    input_template = "text.html"
    supported_ops = TEXT_OPS

    def __init__(self, name: str, *, max_length: int | None = None, min_length: int = 0, **kw: Any):
        super().__init__(name, max_length=max_length, min_length=min_length, **kw)
        self.max_length = max_length
        self.min_length = min_length

    def to_python(self, raw: Any) -> Any:
        value = super().to_python(raw)
        return value if value is EMPTY else str(value)

    def validate(self, value: Any, ctx: Ctx) -> None:
        if self.max_length and len(value) > self.max_length:
            self.fail(f"Must be {self.max_length} characters or fewer.")
        if self.min_length and len(value) < self.min_length:
            self.fail(f"Must be at least {self.min_length} characters.")


@register_field("textarea")
class TextAreaField(TextField):
    """Multi-line text."""

    display_template = "textarea.html"
    input_template = "textarea.html"

    def __init__(self, name: str, *, rows: int = 4, **kw: Any):
        super().__init__(name, rows=rows, **kw)
        self.rows = rows
        # Long text rarely belongs in a table column.
        self.in_list = kw.get("in_list", False)


@register_field("richtext")
class RichTextField(TextAreaField):
    """Text stored as a limited subset of HTML."""

    display_template = "richtext.html"
    input_template = "richtext.html"
    sortable = False


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@register_field("email")
class EmailField(TextField):
    display_template = "email.html"
    input_template = "email.html"

    def to_python(self, raw: Any) -> Any:
        value = super().to_python(raw)
        return value if value is EMPTY else value.lower()

    def validate(self, value: Any, ctx: Ctx) -> None:
        super().validate(value, ctx)
        if not EMAIL_RE.match(value):
            self.fail("Enter a valid email address.")


@register_field("url")
class URLField(TextField):
    display_template = "url.html"
    input_template = "url.html"

    def to_python(self, raw: Any) -> Any:
        value = super().to_python(raw)
        if value is EMPTY:
            return value
        # A bare domain is what people type; make it a usable link.
        if value and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
            value = f"https://{value}"
        return value


@register_field("phone")
class PhoneField(TextField):
    display_template = "phone.html"
    input_template = "phone.html"

    def to_python(self, raw: Any) -> Any:
        value = super().to_python(raw)
        if value is EMPTY:
            return value
        # Keep punctuation people rely on; strip only decorative whitespace.
        return re.sub(r"[^\d+()\-. ]", "", value).strip()


# -- numbers ---------------------------------------------------------------


@register_field("integer")
class IntegerField(Field):
    display_template = "number.html"
    input_template = "number.html"
    supported_ops = ORDERED_OPS
    text_searchable = False
    align = "right"

    def __init__(self, name: str, *, min: int | None = None, max: int | None = None, step: int = 1, **kw: Any):
        super().__init__(name, min=min, max=max, step=step, **kw)
        self.min = min
        self.max = max
        self.step = step

    def to_python(self, raw: Any) -> Any:
        value = super().to_python(raw)
        if value is EMPTY:
            return value
        try:
            # Accept "1,200" and "1200.0" from a pasted spreadsheet cell.
            return int(Decimal(str(value).replace(",", "")))
        except (ValueError, TypeError, InvalidOperation):
            self.fail("Enter a whole number.")

    def validate(self, value: Any, ctx: Ctx) -> None:
        if self.min is not None and value < self.min:
            self.fail(f"Must be at least {self.min}.")
        if self.max is not None and value > self.max:
            self.fail(f"Must be at most {self.max}.")


@register_field("decimal")
class DecimalField(IntegerField):
    display_template = "decimal.html"
    input_template = "number.html"

    def __init__(self, name: str, *, places: int = 2, **kw: Any):
        kw.setdefault("step", 10 ** -places)
        super().__init__(name, places=places, **kw)
        self.places = places

    def to_python(self, raw: Any) -> Any:
        value = Field.to_python(self, raw)
        if value is EMPTY:
            return value
        try:
            return Decimal(str(value).replace(",", ""))
        except (ValueError, TypeError, InvalidOperation):
            self.fail("Enter a number.")

    def to_storage(self, value: Any) -> Any:
        # Most drivers round-trip float more predictably than Decimal.
        return None if value is None else float(value)


@register_field("currency")
class CurrencyField(DecimalField):
    display_template = "currency.html"

    def __init__(self, name: str, *, currency: str = "USD", **kw: Any):
        super().__init__(name, currency=currency, **kw)
        self.currency = currency


@register_field("percent")
class PercentField(DecimalField):
    display_template = "percent.html"

    def __init__(self, name: str, **kw: Any):
        kw.setdefault("min", 0)
        kw.setdefault("max", 100)
        super().__init__(name, **kw)


# -- boolean ---------------------------------------------------------------


TRUTHY = {"1", "true", "yes", "on", "y", "t"}
FALSY = {"0", "false", "no", "off", "n", "f", ""}


@register_field("boolean")
class BooleanField(Field):
    display_template = "boolean.html"
    input_template = "boolean.html"
    supported_ops = frozenset({Op.EQ, Op.NE})
    text_searchable = False
    align = "center"
    absent_means_value = True

    def to_python(self, raw: Any) -> Any:
        # A cleared checkbox submits nothing at all, which means False here --
        # never EMPTY, or the value could never be turned off.
        if raw is None:
            return False
        if isinstance(raw, bool):
            return raw
        # The rendered input pairs a hidden "false" with the checkbox's "true",
        # so a checked box submits both. Last value wins, as browsers intend.
        if isinstance(raw, (list, tuple)):
            if not raw:
                return False
            raw = raw[-1]
        text = str(raw).strip().casefold()
        if text in TRUTHY:
            return True
        if text in FALSY:
            return False
        self.fail("Enter yes or no.")

    def choices(self, ctx: Ctx | None = None) -> tuple[Choice, ...]:
        return (Choice(True, "Yes"), Choice(False, "No"))


# -- temporal --------------------------------------------------------------


@register_field("date")
class DateField(Field):
    display_template = "date.html"
    input_template = "date.html"
    supported_ops = ORDERED_OPS
    text_searchable = False

    def to_python(self, raw: Any) -> Any:
        value = super().to_python(raw)
        if value is EMPTY:
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            self.fail("Enter a date as YYYY-MM-DD.")

    def to_storage(self, value: Any) -> Any:
        # Returned as a Python date, not an ISO string. Serialising here would
        # bake in one backend's wire format: a typed SQL column wants the
        # object, a JSON API wants the string. Adapting is the provider's job.
        return value


@register_field("datetime")
class DateTimeField(DateField):
    """An instant.

    Unlike :class:`DateField`, this is a point on the world's timeline, so it
    is stored in UTC and rendered in the reader's zone. A value typed into a
    form has no offset -- a person entering "14:00" means two o'clock where
    they are -- so it is interpreted in the submitter's zone before storage.

    Shown as a distance from now ("3 hours ago") by default, which is what a
    created-at or a last-changed is read for. Pass ``absolute=True`` for the
    other kind -- an appointment rather than a timestamp -- and the date and
    clock time become the label instead, with the distance kept as the
    tooltip. A shift starting "in 2 months" is the case that made this
    necessary: it is the one column nobody can plan from.
    """

    display_template = "datetime.html"
    input_template = "datetime.html"

    def to_python(self, raw: Any) -> Any:
        value = Field.to_python(self, raw)
        if value is EMPTY:
            return value
        from app.core.clock import parse as parse_instant

        parsed = parse_instant(value)
        if parsed is None:
            self.fail("Enter a date and time.")
        return parsed

    def coerce(self, raw: Any, ctx: Ctx) -> Any:
        """Read a submitted value, assuming the submitter's zone.

        Separate from ``to_python`` because only this needs the request
        context, and every other field type is happily context-free.
        """
        value = Field.to_python(self, raw)
        if value is EMPTY:
            return value
        from app.core.clock import parse as parse_instant

        parsed = parse_instant(value, assume=ctx.timezone)
        if parsed is None:
            self.fail("Enter a date and time.")
        return parsed

    def to_storage(self, value: Any) -> Any:
        """Always UTC, whatever zone it arrived in."""
        if isinstance(value, datetime):
            from app.core.clock import to_utc

            return to_utc(value)
        return value


@register_field("time")
class TimeField(Field):
    display_template = "time.html"
    input_template = "time.html"
    supported_ops = ORDERED_OPS
    text_searchable = False

    def to_python(self, raw: Any) -> Any:
        value = super().to_python(raw)
        if value is EMPTY:
            return value
        if isinstance(value, time):
            return value
        try:
            return time.fromisoformat(str(value))
        except ValueError:
            self.fail("Enter a time as HH:MM.")

    def to_storage(self, value: Any) -> Any:
        return value


@register_field("duration")
class DurationField(IntegerField):
    """A span of time, stored as whole minutes."""

    display_template = "duration.html"

    def to_python(self, raw: Any) -> Any:
        value = Field.to_python(self, raw)
        if value is EMPTY:
            return value
        text = str(value).strip()
        # Accept "1:30" alongside a plain minute count.
        if ":" in text:
            try:
                hours, minutes = text.split(":", 1)
                return int(hours) * 60 + int(minutes)
            except ValueError:
                self.fail("Enter a duration as minutes or H:MM.")
        return super().to_python(text)


# -- choices ---------------------------------------------------------------


class ChoiceFieldMixin:
    """Shared choice resolution for select-style fields."""

    _choices: ChoiceSource

    def choices(self, ctx: Ctx | None = None) -> tuple[Choice, ...]:
        source = self._choices
        if source is None:
            return ()
        if callable(source):
            source = source(ctx)
        return tuple(Choice.coerce(item) for item in source)

    def choice_map(self, ctx: Ctx | None = None) -> dict[Any, Choice]:
        """Value-to-choice lookup, for rendering a stored value as its label."""
        return {c.value: c for c in self.choices(ctx)}


@register_field("select")
class SelectField(ChoiceFieldMixin, Field):
    """One value from a fixed or dynamically resolved set."""

    display_template = "select.html"
    input_template = "select.html"
    supported_ops = CHOICE_OPS

    def __init__(self, name: str, *, choices: ChoiceSource = None, **kw: Any):
        super().__init__(name, choices=choices, **kw)
        self._choices = choices
        self.in_filter = kw.get("in_filter", True)

    def validate(self, value: Any, ctx: Ctx) -> None:
        allowed = self.choices(ctx)
        # A callable source may legitimately return nothing (an empty lookup
        # table); refusing every value in that case would be unhelpful.
        if allowed and value not in {c.value for c in allowed}:
            self.fail("Choose one of the available options.")


@register_field("status")
class StatusField(SelectField):
    """A select rendered as a coloured pill, and usable as a board column."""

    display_template = "status.html"


@register_field("multiselect")
class MultiSelectField(ChoiceFieldMixin, Field):
    """Several values from a set, stored as a JSON list."""

    display_template = "multiselect.html"
    input_template = "multiselect.html"
    supported_ops = frozenset({Op.CONTAINS, Op.IS_NULL, Op.NOT_NULL})
    sortable = False

    def __init__(self, name: str, *, choices: ChoiceSource = None, **kw: Any):
        super().__init__(name, choices=choices, **kw)
        self._choices = choices

    def to_python(self, raw: Any) -> Any:
        if raw is None:
            return []
        if isinstance(raw, (list, tuple, set)):
            return [v for v in raw if v not in (None, "")]
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                return list(json.loads(text))
            except json.JSONDecodeError:
                pass
        return [part.strip() for part in text.split(",") if part.strip()]

    def to_storage(self, value: Any) -> Any:
        return json.dumps(list(value or []))

    def to_display(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return [v.strip() for v in value.split(",") if v.strip()]
        return list(value or [])


@register_field("tags")
class TagsField(MultiSelectField):
    """Free-form labels, entered as typed text rather than chosen from a list."""

    display_template = "tags.html"
    input_template = "tags.html"


# -- structured ------------------------------------------------------------


@register_field("json")
class JSONField(Field):
    display_template = "json.html"
    input_template = "json.html"
    supported_ops = frozenset({Op.IS_NULL, Op.NOT_NULL})
    sortable = False
    text_searchable = False

    def __init__(self, name: str, **kw: Any):
        kw.setdefault("in_list", False)
        super().__init__(name, **kw)

    def to_python(self, raw: Any) -> Any:
        value = super().to_python(raw)
        if value is EMPTY or isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            self.fail("Enter valid JSON.")

    def to_storage(self, value: Any) -> Any:
        return json.dumps(value) if isinstance(value, (dict, list)) else value

    def to_display(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value


@register_field("timezone")
class TimezoneField(ChoiceFieldMixin, Field):
    """A zone name, offered as a list with current offsets shown.

    Seeing "Europe/London (UTC+01:00)" is how a person confirms they picked the
    right one. Offsets are computed per request, not stored, because an offset
    is not a property of a zone -- London is +00:00 in January and +01:00 in
    July.
    """

    display_template = "text.html"
    input_template = "select.html"
    supported_ops = CHOICE_OPS
    text_searchable = False

    def __init__(self, name: str = "timezone", **kw: Any):
        kw.setdefault("label", "Timezone")
        kw.setdefault("default", "UTC")
        kw.setdefault("help", "Times are shown in this zone. Stored values are always UTC.")
        super().__init__(name, **kw)
        self._choices = None

    def choices(self, ctx: Ctx | None = None) -> tuple[Choice, ...]:
        from app.core.clock import timezone_choices

        return tuple(Choice(value, label) for value, label in timezone_choices())

    def validate(self, value: Any, ctx: Ctx) -> None:
        from app.core.clock import is_valid_timezone

        # Checked against the system's zone database rather than the dropdown,
        # so a zone typed in by hand -- or one this list omits -- is accepted.
        if not is_valid_timezone(str(value)):
            self.fail("That is not a timezone this system knows.")


@register_field("color")
class ColorField(TextField):
    display_template = "color.html"
    input_template = "color.html"
    sortable = False


@register_field("file")
class FileField(Field):
    """A reference to a stored file. The bytes live wherever the app configures."""

    display_template = "file.html"
    input_template = "file.html"
    supported_ops = frozenset({Op.IS_NULL, Op.NOT_NULL})
    sortable = False
    text_searchable = False


@register_field("case")
class CaseField(Field):
    """A column that does not exist, expressed in terms of ones that do.

    For the value a screen is really organised by when no column holds it. A
    job's lifecycle is the example: "needs publishing", "active", "over",
    "cancelled" is how anybody thinks about the list, and the table stores a
    status id and a cancellation date that between them imply it.

    Declared as ordered branches -- the first whose condition matches wins, as
    in SQL -- so the declaration is also the sort order. That is the point:
    computing this in Python would give a screen that cannot sort, cannot
    group and cannot paginate without pulling every row. Compiled into the
    query, it does all three, and costs nothing beyond the CASE.

    A branch is a `Filter`, built from the same `Condition` and `and_`/`or_`
    vocabulary as everything else. It may also be a plain string, which is
    inserted as SQL: that exists for the conditions the filter vocabulary
    cannot reach -- a correlated subquery over another table -- and it is the
    one place in this application where SQL is written by hand. It is static
    text authored beside the resource, never built from a request, and it ties
    the field to whichever database dialect it was written for.
    """

    display_template = "text.html"
    supported_ops = CHOICE_OPS
    #: Written nowhere: it is an expression, not a column.
    readonly = True

    def __init__(
        self,
        name: str,
        *,
        cases: Sequence[tuple[Any, Any]],
        default: Any = None,
        order: Sequence[Any] = (),
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.cases = list(cases)
        self.default = default
        #: The order the values sort and group in. Separate from the order the
        #: branches are *matched* in, because the two are different questions
        #: and conflating them silently gets one of them wrong: a cancelled job
        #: has to be tested before a draft one (the columns are independent),
        #: and read after it (nobody works cancelled jobs first).
        self.order = list(order) or [value for _, value in cases] + (
            [default] if default is not None else []
        )
        self.in_filter = kw.get("in_filter", True)

    def rank(self, value: Any) -> int:
        """Where a value sorts. What the database actually selects.

        The expression emits this rather than the label, so ORDER BY is the
        declared order rather than the alphabet -- "Active, Cancelled, Needs
        publishing, Over" is what sorting the labels would give, and it is
        nobody's idea of a lifecycle.
        """
        return self.order.index(value) if value in self.order else len(self.order)

    def label_for(self, rank: Any) -> Any:
        try:
            return self.order[int(rank)]
        except (TypeError, ValueError, IndexError):
            return rank

    def to_display(self, value: Any) -> Any:
        return self.label_for(value)

    def choices(self, ctx: Ctx | None = None) -> tuple[Choice, ...]:
        return tuple(
            Choice(value=index, label=str(value)) for index, value in enumerate(self.order)
        )

    def to_storage(self, value: Any) -> Any:
        raise UnsupportedOperation(
            f"{self.name} is derived from other columns; there is nothing to write."
        )


@register_field("rows")
class RowsField(Field):
    """Several records entered at once, as a table of real inputs.

    For the case where the subject of the action is a *set* of new things --
    a dozen contacts, a season of price rules -- and the alternative is asking
    somebody to hand-write a CSV and find out what was wrong with it
    afterwards. Here the form is what is submitted: every cell is the same
    widget the single-record form would use, with the same validation, so a
    bad value is a red cell next to the row it belongs to rather than a line
    number in a report.

    A file can still be the *starting point* -- the input template offers one
    and fills the rows from it in the browser -- but the file is never
    uploaded and never parsed here. What arrives is the grid.

    Submitted as parallel arrays, one repeated key per column
    (``rows.email``, ``rows.name``), which is what a table of inputs produces
    naturally and what keeps a deleted row from shifting the ones below it:
    the browser drops the whole row's inputs at once, so every column stays
    the same length.
    """

    display_template = "text.html"
    input_template = "rows.html"
    supported_ops = frozenset()
    sortable = False
    text_searchable = False

    def __init__(self, name: str, *, columns: Sequence[Field], min_rows: int = 3, **kw: Any):
        super().__init__(name, **kw)
        self.columns = list(columns)
        #: Blank rows drawn when the dialog opens. Not a minimum: a row left
        #: entirely empty is dropped rather than refused, because the usual
        #: way to enter two records is to open a grid of three.
        self.min_rows = min_rows
        self.in_list = False
        self.in_filter = False

    def key(self, column: Field) -> str:
        """The form key one column's inputs share."""
        return f"{self.name}.{column.name}"

    def was_submitted(self, data: Mapping[str, Any]) -> bool:
        return any(self.key(column) in data for column in self.columns)

    def submitted_rows(self, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The cells as typed, uncoerced, for redrawing a rejected grid.

        Twelve rows entered and one of them wrong must come back as twelve
        rows with the wrong one marked, not as an empty grid: retyping the
        eleven good ones is the work this field exists to save. Uncoerced
        because the bad cell has no coerced form -- being uncoercible is what
        is wrong with it -- and the reader has to see what they typed to see
        why.
        """
        columns = {column.name: _pick_all(data, self.key(column)) for column in self.columns}
        height = max((len(values) for values in columns.values()), default=0)
        rows = []
        for index in range(height):
            cells = {
                name: values[index] if index < len(values) else None
                for name, values in columns.items()
            }
            if all(_blank(value) for value in cells.values()):
                continue
            rows.append(cells)
        return rows

    def collect(self, data: Mapping[str, Any], ctx: Ctx) -> Any:
        """Read the parallel column arrays back into a list of rows.

        Errors name the row and the column -- "Row 3, ID code: ..." -- because
        a grid of forty cells has forty places to be wrong and the message is
        the only thing pointing at one of them.
        """
        columns: dict[str, list[Any]] = {}
        for column in self.columns:
            raw = _pick_all(data, self.key(column))
            columns[column.name] = raw
        height = max((len(v) for v in columns.values()), default=0)
        if height == 0:
            return EMPTY

        rows: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for index in range(height):
            cells = {
                column.name: columns[column.name][index]
                if index < len(columns[column.name]) else None
                for column in self.columns
            }
            # A row nobody filled in is not an error; it is the empty part of
            # the grid the dialog opened with.
            if all(_blank(value) for value in cells.values()):
                continue
            row: dict[str, Any] = {}
            for column in self.columns:
                try:
                    value = column.coerce(cells[column.name], ctx)
                    if value is EMPTY:
                        if column.required:
                            raise ValidationFailed(
                                {column.name: f"{column.label} is required."}
                            )
                        value = None
                    else:
                        column.validate(value, ctx)
                except ValidationFailed as exc:
                    errors[f"{self.name}.{index}.{column.name}"] = (
                        f"Row {len(rows) + 1}, {column.label}: "
                        f"{exc.errors.get(column.name, exc.message)}"
                    )
                    continue
                row[column.name] = value
            rows.append(row)

        if errors:
            raise ValidationFailed(
                {self.name: " ".join(errors.values())}
            )
        if not rows:
            return EMPTY
        return rows

    def to_storage(self, value: Any) -> Any:
        raise UnsupportedOperation(
            f"{self.name} is a set of rows to act on, not a column to store."
        )


def _pick_all(data: Mapping[str, Any], key: str) -> list[Any]:
    getall = getattr(data, "getlist", None)
    if getall is not None:
        return list(getall(key))
    value = data.get(key)
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


@register_field("image")
class ImageField(FileField):
    display_template = "image.html"
    input_template = "image.html"


# -- relations -------------------------------------------------------------


@register_field("relation")
class RelationField(Field):
    """A pointer to a record on another resource, rendered as a link.

    The form input is a typeahead served by the target resource, so no join is
    required -- which matters when the two resources live in different backends.
    """

    display_template = "relation.html"
    input_template = "relation.html"
    supported_ops = CHOICE_OPS
    text_searchable = False

    def __init__(
        self,
        name: str,
        *,
        resource: str,
        display: str = "name",
        search: Sequence[str] = (),
        target_key: str = "id",
        **kw: Any,
    ):
        super().__init__(name, resource=resource, display=display, search=tuple(search),
                         target_key=target_key, **kw)
        self.relation = RelationSpec(
            resource=resource, display=display, search=tuple(search), target_key=target_key
        )
        self.in_filter = kw.get("in_filter", True)


@register_field("backref")
class BackrefField(Field):
    """The many side of a relation: rows elsewhere pointing back at this record.

    Not stored on the record. It renders as an embedded list on the detail
    view and is resolved by querying the other resource.
    """

    display_template = "backref.html"
    input_template = "backref.html"
    supported_ops = frozenset()
    sortable = False
    text_searchable = False

    def __init__(
        self,
        name: str,
        *,
        resource: str,
        via: str,
        columns: Sequence[str] = (),
        limit: int = 10,
        actions: bool = False,
        tree: str = "",
        order: Sequence[str] = (),
        **kw: Any,
    ):
        kw.setdefault("in_list", False)
        kw.setdefault("in_form", False)
        super().__init__(name, resource=resource, via=via, columns=tuple(columns),
                         limit=limit, readonly=True, order=tuple(order), **kw)
        self.target_resource = resource
        #: Field on the target resource holding this record's key.
        self.via = via
        self.columns = tuple(columns)
        self.limit = limit
        #: Offer the target's row actions on each embedded row. Off by default:
        #: an embedded list is usually context, and a screen that grows buttons
        #: because another resource gained an action is a screen nobody
        #: designed. Turned on where the embedded row is the thing being
        #: worked on -- a shift's assignments, which is where somebody is
        #: dismissed from.
        self.actions = actions
        #: Name of a tree view on the target resource. Set, the embedded list
        #: is drawn with that view's expanders instead of as a flat table, and
        #: each row opens through the target's own `/children` fragment -- so
        #: a person's jobs can carry their shifts underneath without this
        #: screen knowing anything about shifts.
        self.tree = tree
        #: How the embedded rows are ordered, as ``["-start_time"]``. Empty
        #: takes the target's own default, which is right when the embedded
        #: list is the target's list in miniature and wrong when the reading
        #: order depends on which record you arrived from -- a person's shifts
        #: read by when they are worked, not by when they were assigned.
        self.order = tuple(order)

    def extract(self, record: Mapping[str, Any], ctx: Ctx | None = None) -> Any:
        # Resolved by the view layer, which has provider access.
        return None


@register_field("computed")
class ComputedField(Field):
    """A value derived from the record rather than stored."""

    supported_ops = frozenset()
    sortable = False

    def __init__(self, name: str, *, compute, **kw: Any):
        kw.setdefault("in_form", False)
        super().__init__(name, compute=compute, readonly=True, **kw)
