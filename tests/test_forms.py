"""Form building, coercion and error round-tripping."""

from __future__ import annotations

import pytest

from app.core.results import Ctx, Identity, Record
from app.fields.types import (
    BooleanField,
    CurrencyField,
    DateField,
    EmailField,
    SelectField,
    TextField,
)
from app.resources.form_engine import FormEngine
from app.resources.resource import Resource

CTX = Ctx.system()
ADMIN = Identity(subject="a", roles=frozenset({"admin"}))
SALES = Identity(subject="kim", roles=frozenset({"sales"}))


@pytest.fixture
def resource() -> Resource:
    return Resource(
        "deals",
        provider="memory",
        fields=[
            TextField("id", in_form=False),
            TextField("name", required=True, searchable=True, inline_editable=True),
            EmailField("email"),
            CurrencyField("amount"),
            DateField("closed"),
            BooleanField("archived", default=False),
            SelectField("stage", choices=["open", "won", "lost"]),
            TextField("margin", write_roles=["finance"]),
        ],
    )


@pytest.fixture
def engine(resource) -> FormEngine:
    return FormEngine(resource)


class TestBuild:
    def test_new_form_uses_defaults(self, engine):
        form = engine.build(ADMIN)
        assert form.is_create is True
        assert form["archived"].value is False

    def test_edit_form_is_populated_from_the_record(self, engine):
        record = Record({"id": 1, "name": "Acme", "amount": 500}, "id")
        form = engine.build(ADMIN, record=record)
        assert form.is_create is False
        assert form["name"].value == "Acme"

    def test_initial_values_seed_a_new_form(self, engine):
        form = engine.build(ADMIN, initial={"stage": "won"})
        assert form["stage"].value == "won"

    def test_fields_the_caller_cannot_write_render_locked(self, engine):
        form = engine.build(SALES)
        assert form["margin"].editable is False
        assert form["name"].editable is True


class TestProcess:
    def test_valid_submission_coerces_values(self, engine):
        form = engine.process({"name": "Acme", "amount": "1,200.50"}, ADMIN, CTX)
        assert form.valid
        assert form["amount"].value == pytest.approx(1200.50)

    def test_missing_required_field_is_rejected(self, engine):
        form = engine.process({"amount": "10"}, ADMIN, CTX)
        assert not form.valid
        assert "name" in form.errors

    def test_error_message_names_the_field(self, engine):
        form = engine.process({}, ADMIN, CTX)
        assert "Name is required" in form.errors["name"]

    def test_rejected_form_keeps_what_the_user_typed(self, engine):
        # The whole point: someone who typed a bad date must see it again.
        form = engine.process({"name": "Acme", "closed": "tomorrow"}, ADMIN, CTX)
        assert not form.valid
        assert form["closed"].display_value == "tomorrow"

    def test_several_errors_are_all_reported(self, engine):
        form = engine.process({"email": "nope", "closed": "never"}, ADMIN, CTX)
        assert set(form.errors) == {"name", "email", "closed"}

    def test_summary_counts_the_errors(self, engine):
        form = engine.process({"email": "nope"}, ADMIN, CTX)
        assert "2 fields" in form.summary

    def test_unwritable_field_is_silently_discarded(self, engine):
        # A hand-crafted POST must not be able to set a field the form never
        # offered to this caller.
        form = engine.process({"name": "Acme", "margin": "99"}, SALES, CTX)
        assert form.valid
        assert "margin" not in form.values()

    def test_unknown_keys_are_ignored(self, engine):
        form = engine.process({"name": "Acme", "csrf_token": "x", "submit": "1"}, ADMIN, CTX)
        assert form.valid
        assert set(form.values()) <= set(engine.resource.field_names)


class TestPartialSubmission:
    def test_absent_fields_are_skipped(self, engine):
        record = Record({"id": 1, "name": "Acme"}, "id")
        form = engine.process({"amount": "50"}, ADMIN, CTX, record=record, partial=True)
        assert form.valid, "a partial write must not demand unrelated required fields"
        assert set(form.values()) == {"amount"}

    def test_explicitly_cleared_field_stores_null(self, engine):
        record = Record({"id": 1, "name": "Acme", "email": "a@b.com"}, "id")
        form = engine.process({"email": ""}, ADMIN, CTX, record=record, partial=True)
        assert form.valid
        assert form["email"].value is None

    def test_field_restriction_limits_what_is_processed(self, engine):
        form = engine.process(
            {"name": "Acme", "amount": "9"}, ADMIN, CTX, fields=["amount"], partial=True
        )
        assert set(form.values()) == {"amount"}


class TestStorage:
    def test_values_are_converted_for_the_provider(self, engine):
        from datetime import date

        form = engine.process({"name": "Acme", "closed": "2026-03-04"}, ADMIN, CTX)
        payload = engine.to_storage(form)
        # A canonical Python value; the provider adapts it to its own format.
        assert payload["closed"] == date(2026, 3, 4)

    def test_empty_values_are_omitted(self, engine):
        form = engine.process({"name": "Acme"}, ADMIN, CTX)
        payload = engine.to_storage(form)
        assert "amount" not in payload

    def test_computed_fields_never_reach_a_write(self, resource, engine):
        from app.fields.base import Field

        resource.add_field(Field("label", compute=lambda r: "x"))
        form = engine.process({"name": "Acme"}, ADMIN, CTX)
        assert "label" not in engine.to_storage(form)


class TestSingleFieldForm:
    def test_builds_one_field(self, engine):
        record = Record({"id": 1, "name": "Acme"}, "id")
        form = engine.build_single(ADMIN, "name", record)
        assert set(form.fields) == {"name"}
        assert form["name"].editable is True

    def test_field_not_marked_inline_editable_is_locked(self, engine):
        record = Record({"id": 1, "amount": 5}, "id")
        form = engine.build_single(ADMIN, "amount", record)
        assert form["amount"].editable is False
