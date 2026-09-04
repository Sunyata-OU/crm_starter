"""Turning submitted form data into a validated write.

Three concerns meet here, and separating them is what keeps the routes short:
deciding which fields a given caller may submit, coercing raw strings into
Python values, and re-rendering a rejected form with the user's own input still
in the boxes.

The last point is the one usually got wrong. On failure this returns the values
*as submitted*, not as coerced, so someone who typed "tomorrow" into a date
field sees "tomorrow" again next to the error rather than an empty box.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from app.core.errors import ValidationFailed
from app.core.results import Ctx, Identity, Record
from app.fields.base import EMPTY, Field
from app.resources.resource import Resource


@dataclass(slots=True)
class BoundField:
    """One field, ready to render: its definition, current value and error."""

    field: Field
    value: Any = None
    #: What the user actually typed, shown again when validation failed.
    raw: Any = None
    error: str = ""
    editable: bool = True

    @property
    def name(self) -> str:
        return self.field.name

    @property
    def label(self) -> str:
        return self.field.label

    @property
    def has_error(self) -> bool:
        return bool(self.error)

    @property
    def display_value(self) -> Any:
        """The value to put in the input.

        Prefers the raw submission so a rejected form does not silently discard
        what the user typed.
        """
        return self.raw if self.raw is not None else self.value


@dataclass(slots=True)
class Form:
    """A form ready to render, or the result of processing a submission."""

    resource: Resource
    fields: dict[str, BoundField] = dc_field(default_factory=dict)
    errors: dict[str, str] = dc_field(default_factory=dict)
    message: str = ""
    record: Record | None = None
    is_create: bool = True

    @property
    def valid(self) -> bool:
        return not self.errors

    def __iter__(self):
        return iter(self.fields.values())

    def __getitem__(self, name: str) -> BoundField:
        return self.fields[name]

    def __contains__(self, name: object) -> bool:
        return name in self.fields

    def get(self, name: str) -> BoundField | None:
        return self.fields.get(name)

    def values(self) -> dict[str, Any]:
        """Coerced values, ready to hand to a provider."""
        return {name: bf.value for name, bf in self.fields.items() if bf.value is not EMPTY}

    @property
    def summary(self) -> str:
        """One line describing the failure, for a toast or an aria-live region."""
        if self.message:
            return self.message
        if not self.errors:
            return ""
        count = len(self.errors)
        return f"{count} field{'s' if count > 1 else ''} need{'' if count > 1 else 's'} attention."


class FormEngine:
    """Builds and processes forms for a resource."""

    def __init__(self, resource: Resource) -> None:
        self.resource = resource

    # -- which fields -------------------------------------------------------

    def form_fields(self, identity: Identity, view_name: str = "form") -> list[Field]:
        """Fields to render, in layout order, filtered by what the caller may see.

        Visibility is the policy's answer, not the field's: a field declares
        the roles it needs, but the policy may hide it for other reasons -- a
        grant in the permissions table, for one -- and it is the authority.
        """
        view = self.resource.view(view_name)
        names = getattr(view, "field_names", ())
        if not names:
            names = tuple(f.name for f in self.resource.fields if f.in_form)

        readable = set(self.resource.policy.readable_fields(identity, self.resource))
        fields = []
        for name in names:
            field = self.resource.get_field(name)
            if field is None or name not in readable:
                continue
            fields.append(field)
        return fields

    def submittable_fields(self, identity: Identity, view_name: str = "form") -> list[Field]:
        """Fields whose submitted values will actually be accepted.

        Anything else in the payload is discarded rather than trusted, which is
        what stops a hand-crafted POST from writing a field the form never
        offered -- an owner column, a status a role may not set, a column a
        permission grant marks read-only.
        """
        writable = set(self.resource.policy.writable_fields(identity, self.resource))
        return [f for f in self.form_fields(identity, view_name) if f.name in writable]

    # -- building an empty or populated form -------------------------------

    def build(
        self,
        identity: Identity,
        *,
        record: Record | None = None,
        view_name: str = "form",
        initial: Mapping[str, Any] | None = None,
    ) -> Form:
        """A form for creating (``record=None``) or editing a record."""
        form = Form(resource=self.resource, record=record, is_create=record is None)
        initial = initial or {}
        writable = set(self.resource.policy.writable_fields(identity, self.resource))
        for field in self.form_fields(identity, view_name):
            if record is not None:
                value = field.extract(record)
            elif field.name in initial:
                value = initial[field.name]
            else:
                value = field.default
            form.fields[field.name] = BoundField(
                field=field,
                value=field.to_display(value),
                editable=field.name in writable,
            )
        return form

    def build_single(
        self, identity: Identity, field_name: str, record: Record
    ) -> Form:
        """A one-field form, for editing a cell in place."""
        field = self.resource.field(field_name)
        policy = self.resource.policy
        if field_name not in set(policy.readable_fields(identity, self.resource)):
            raise ValidationFailed({field_name: "You cannot see that field."})
        writable = field_name in set(policy.writable_fields(identity, self.resource))
        form = Form(resource=self.resource, record=record, is_create=False)
        form.fields[field_name] = BoundField(
            field=field,
            value=field.to_display(field.extract(record)),
            editable=writable and field.inline_editable,
        )
        return form

    # -- processing a submission -------------------------------------------

    def process(
        self,
        data: Mapping[str, Any],
        identity: Identity,
        ctx: Ctx,
        *,
        record: Record | None = None,
        view_name: str = "form",
        fields: Sequence[str] | None = None,
        partial: bool = False,
    ) -> Form:
        """Coerce and validate a submission.

        ``partial`` skips the required check for fields absent from the payload,
        which is what an inline single-field edit needs -- it must not reject
        the write because some other required field was not resubmitted.
        """
        form = Form(resource=self.resource, record=record, is_create=record is None)
        allowed = self.submittable_fields(identity, view_name)
        if fields is not None:
            wanted = set(fields)
            allowed = [f for f in allowed if f.name in wanted]

        for field in allowed:
            raw = _pick(data, field.name)
            # A checkbox that is off submits nothing at all. In a full form we
            # know the input was rendered, so absence means False. In a partial
            # write we cannot assume the field was on the form, so absence
            # means "not part of this submission".
            submitted = field.name in data or (not partial and field.absent_means_value)
            bound = BoundField(field=field, raw=raw, editable=True)
            form.fields[field.name] = bound

            if partial and not submitted:
                # Not part of this submission; leave the stored value alone.
                del form.fields[field.name]
                continue

            try:
                # coerce rather than to_python: a datetime must be read in the
                # submitter's timezone, and only the context knows it.
                value = field.coerce(raw, ctx)
            except ValidationFailed as exc:
                bound.error = exc.errors.get(field.name, exc.message)
                form.errors[field.name] = bound.error
                continue

            if value is EMPTY:
                if field.required and (record is None or submitted):
                    bound.error = f"{field.label} is required."
                    form.errors[field.name] = bound.error
                    continue
                # An explicitly cleared field stores NULL; one that was never
                # submitted is left alone, so a partial write does not blank
                # columns the form did not offer.
                bound.value = None if submitted else EMPTY
                continue

            try:
                field.validate(value, ctx)
            except ValidationFailed as exc:
                bound.error = exc.errors.get(field.name, exc.message)
                form.errors[field.name] = bound.error
                continue

            bound.value = value

        return form

    def to_storage(self, form: Form) -> dict[str, Any]:
        """The payload to send to the provider.

        Runs each value through its field's storage conversion and drops
        anything the provider does not persist, so a computed column or a
        backref never reaches a write.
        """
        stored_names = {f.name for f in self.resource.stored_fields}
        payload: dict[str, Any] = {}
        for name, bound in form.fields.items():
            if name not in stored_names or bound.value is EMPTY:
                continue
            payload[name] = bound.field.to_storage(bound.value)
        return payload


def _pick(data: Mapping[str, Any], name: str) -> Any:
    """Read a value from form data, preserving repeated keys as a list.

    Starlette's ``FormData`` is multi-valued; a plain ``data[name]`` silently
    returns only the first value, which would break every multi-select.
    """
    getall = getattr(data, "getlist", None)
    if getall is not None:
        values = getall(name)
        if not values:
            return None
        return values if len(values) > 1 else values[0]
    return data.get(name)
