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

    def choices(self, ctx: Ctx) -> tuple[Choice, ...]:
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

    def choices(self, ctx: Ctx) -> tuple[Choice, ...]:
        source = self._choices
        if source is None:
            return ()
        if callable(source):
            source = source(ctx)
        return tuple(Choice.coerce(item) for item in source)

    def choice_map(self, ctx: Ctx) -> dict[Any, Choice]:
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

    def choices(self, ctx: Ctx) -> tuple[Choice, ...]:
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
        **kw: Any,
    ):
        kw.setdefault("in_list", False)
        kw.setdefault("in_form", False)
        super().__init__(name, resource=resource, via=via, columns=tuple(columns),
                         limit=limit, readonly=True, **kw)
        self.target_resource = resource
        #: Field on the target resource holding this record's key.
        self.via = via
        self.columns = tuple(columns)
        self.limit = limit

    def extract(self, record: Mapping[str, Any]) -> Any:
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
