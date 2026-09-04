"""Field definitions.

A field is declared once and drives everything downstream: how a value is shown
in a table cell, what input renders in a form, how submitted text is coerced
back into a Python value, what it is validated against, which filter operators
the UI offers, and whether it may be edited in place.

Keeping all of that on one object is what makes a resource declaration short.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from app.core.errors import ValidationFailed
from app.core.query import Op
from app.core.results import Ctx, Identity

#: Returned by ``to_python`` when the submitted value means "no value".
EMPTY = object()


@dataclass(frozen=True, slots=True)
class Choice:
    """One option in a select-style field."""

    value: Any
    label: str
    #: Optional colour token, used by status pills and board columns.
    color: str = ""
    help: str = ""

    @classmethod
    def coerce(cls, item: Any) -> Choice:
        """Accept a Choice, a ``(value, label)`` pair, or a bare value."""
        if isinstance(item, Choice):
            return item
        if isinstance(item, Mapping):
            return cls(**item)
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            return cls(value=item[0], label=str(item[1]), color=item[2] if len(item) > 2 else "")
        return cls(value=item, label=str(item))


#: Choices may be a fixed list or resolved per request (e.g. from another table).
ChoiceSource = Sequence[Any] | Callable[[Ctx], Sequence[Any]] | None


@dataclass(frozen=True, slots=True)
class RelationSpec:
    """How a relation field points at another resource."""

    resource: str
    #: Field on the target resource shown to humans.
    display: str = ""
    #: Fields searched by the typeahead. Defaults to ``display``.
    search: tuple[str, ...] = ()
    #: Field on the target holding the key this column stores.
    target_key: str = "id"

    def search_fields(self) -> tuple[str, ...]:
        return self.search or ((self.display,) if self.display else ())


class Field:
    """A declared column on a resource.

    Subclasses supply type behaviour; instances supply per-resource
    configuration. Most declarations only set ``name`` and a few flags.
    """

    #: Registry key, set by ``@register_field``.
    type_name: ClassVar[str] = "text"
    #: Template partials under ``fields/display`` and ``fields/input``.
    display_template: ClassVar[str] = "text.html"
    input_template: ClassVar[str] = "text.html"
    #: Operators this type offers in the filter UI.
    supported_ops: ClassVar[frozenset[Op]] = frozenset(
        {Op.EQ, Op.NE, Op.ICONTAINS, Op.IS_NULL, Op.NOT_NULL}
    )
    #: Whether the column is a sensible thing to search as free text.
    text_searchable: ClassVar[bool] = True
    #: Horizontal alignment hint for table cells.
    align: ClassVar[str] = "left"
    #: True for inputs that send nothing when "off" -- checkboxes. In a full
    #: form submission their absence is a value (False), not a non-submission.
    absent_means_value: ClassVar[bool] = False

    def __init__(
        self,
        name: str,
        label: str = "",
        *,
        help: str = "",
        placeholder: str = "",
        required: bool = False,
        default: Any = None,
        readonly: bool = False,
        # -- where the field appears --
        in_list: bool = True,
        in_form: bool = True,
        in_detail: bool = True,
        in_filter: bool = False,
        sortable: bool = True,
        searchable: bool = False,
        inline_editable: bool = False,
        # -- access --
        read_roles: frozenset[str] | Sequence[str] = (),
        write_roles: frozenset[str] | Sequence[str] = (),
        # -- presentation --
        width: str = "",
        css_class: str = "",
        widget: str = "",
        #: Called with the record to produce a value for computed columns.
        compute: Callable[[Mapping[str, Any]], Any] | None = None,
        **options: Any,
    ) -> None:
        self.name = name
        self.label = label or humanize(name)
        self.help = help
        self.placeholder = placeholder
        self.required = required
        self.default = default
        self.readonly = readonly or compute is not None
        self.in_list = in_list
        # Readonly fields still belong in forms -- they render disabled, which
        # is more useful than omitting context the user expects to see.
        self.in_form = in_form
        self.in_detail = in_detail
        self.in_filter = in_filter
        self.sortable = sortable
        self.searchable = searchable and self.text_searchable
        self.inline_editable = inline_editable and not self.readonly
        self.read_roles = frozenset(read_roles)
        self.write_roles = frozenset(write_roles)
        self.width = width
        self.css_class = css_class
        self.widget = widget
        self.compute = compute
        self.options = options

    # -- visibility ---------------------------------------------------------

    def visible_to(self, identity: Identity) -> bool:
        """Whether this field may be shown to ``identity``."""
        return not self.read_roles or identity.has_role(*self.read_roles)

    def writable_by(self, identity: Identity) -> bool:
        """Whether ``identity`` may change this field."""
        if self.readonly:
            return False
        return not self.write_roles or identity.has_role(*self.write_roles)

    # -- value pipeline -----------------------------------------------------

    def extract(self, record: Mapping[str, Any]) -> Any:
        """The stored value for this field, running ``compute`` if declared."""
        if self.compute is not None:
            return self.compute(record)
        return record.get(self.name, self.default)

    def to_python(self, raw: Any) -> Any:
        """Coerce a submitted form value into its Python type.

        Returns :data:`EMPTY` when the submission means "no value", which the
        form engine distinguishes from a valid empty string.
        """
        if raw is None:
            return EMPTY
        if isinstance(raw, str):
            raw = raw.strip()
            if raw == "":
                return EMPTY
        return raw

    def coerce(self, raw: Any, ctx: Ctx) -> Any:
        """Read a submitted value, with the request context available.

        Defaults to :meth:`to_python`. Overridden by field types whose reading
        depends on who is submitting -- a datetime, which has to be interpreted
        in the submitter's timezone.
        """
        return self.to_python(raw)

    def to_storage(self, value: Any) -> Any:
        """Convert a Python value into what the provider should store."""
        return value

    def to_display(self, value: Any) -> Any:
        """Convert a stored value into what the template renders."""
        return value

    def validate(self, value: Any, ctx: Ctx) -> None:
        """Raise :class:`ValidationFailed` if ``value`` is unacceptable.

        Called only for values that survived ``to_python``; the required check
        is handled by the form engine, which knows about partial updates.
        """
        return None

    def fail(self, message: str) -> None:
        """Reject the current value with a per-field message."""
        raise ValidationFailed({self.name: message})

    # -- filtering ----------------------------------------------------------

    def filter_ops(self) -> tuple[Op, ...]:
        """Operators offered for this field, in menu order."""
        return tuple(op for op in Op if op in self.supported_ops)

    def parse_filter_value(self, op: Op, raw: Any) -> Any:
        """Coerce a filter value from the query string into a comparable type."""
        from app.core.query import SEQUENCE_OPS, UNARY_OPS

        if op in UNARY_OPS:
            return None
        if op in SEQUENCE_OPS:
            values = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
            parsed = [self.to_python(v) for v in values]
            return [v for v in parsed if v is not EMPTY]
        value = self.to_python(raw)
        return None if value is EMPTY else value

    # -- choices ------------------------------------------------------------

    def choices(self, ctx: Ctx) -> tuple[Choice, ...]:
        """Options for select-style rendering. Empty for free-text fields."""
        return ()

    # -- misc ---------------------------------------------------------------

    def clone(self, **changes: Any) -> Field:
        """A copy with some settings changed, for extending another module's resource."""
        merged = {
            "label": self.label, "help": self.help, "placeholder": self.placeholder,
            "required": self.required, "default": self.default, "readonly": self.readonly,
            "in_list": self.in_list, "in_form": self.in_form, "in_detail": self.in_detail,
            "in_filter": self.in_filter, "sortable": self.sortable,
            "searchable": self.searchable, "inline_editable": self.inline_editable,
            "read_roles": self.read_roles, "write_roles": self.write_roles,
            "width": self.width, "css_class": self.css_class, "widget": self.widget,
            "compute": self.compute, **self.options, **changes,
        }
        return type(self)(self.name, **merged)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


def humanize(name: str) -> str:
    """``created_at`` -> ``Created at``. Used when no label is given."""
    text = name.replace("_", " ").replace(".", " ").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]
