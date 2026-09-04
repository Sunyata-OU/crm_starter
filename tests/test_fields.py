"""Field coercion, validation and rendering metadata.

These tests pin down the value pipeline: what a submitted string becomes, what
is rejected, and how a stored value is prepared for display. Getting this wrong
surfaces later as confusing form errors, so it is worth being explicit.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.core.errors import RegistryError, ValidationFailed
from app.core.query import Op
from app.core.results import Ctx, Identity
from app.fields.base import EMPTY, Choice, Field
from app.fields.registry import get_field_type, make_field, registered_types
from app.fields.types import (
    BooleanField,
    CurrencyField,
    DateField,
    DateTimeField,
    DecimalField,
    DurationField,
    EmailField,
    IntegerField,
    JSONField,
    MultiSelectField,
    PhoneField,
    RelationField,
    SelectField,
    TextField,
    URLField,
)

CTX = Ctx.system()


def coerce(field: Field, raw):
    """Run the submitted value through the field's pipeline."""
    value = field.to_python(raw)
    if value is not EMPTY:
        field.validate(value, CTX)
    return value


class TestEmptiness:
    """Distinguishing "not submitted" from "submitted as blank"."""

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_blank_text_is_empty(self, raw):
        assert TextField("x").to_python(raw) is EMPTY

    def test_zero_is_a_value_not_empty(self):
        assert IntegerField("x").to_python("0") == 0

    def test_false_is_a_value_not_empty(self):
        assert BooleanField("x").to_python("false") is False

    def test_unchecked_box_submits_nothing_and_means_false(self):
        # A cleared checkbox sends no key at all. Treating that as EMPTY would
        # make it impossible to ever turn the flag off.
        assert BooleanField("x").to_python(None) is False


class TestText:
    def test_strips_whitespace(self):
        assert TextField("x").to_python("  hi  ") == "hi"

    def test_max_length_is_enforced(self):
        with pytest.raises(ValidationFailed) as exc:
            coerce(TextField("x", max_length=3), "toolong")
        assert "x" in exc.value.errors

    def test_min_length_is_enforced(self):
        with pytest.raises(ValidationFailed):
            coerce(TextField("x", min_length=5), "abc")


class TestEmail:
    def test_lowercases(self):
        assert EmailField("e").to_python("Ada@Example.COM") == "ada@example.com"

    @pytest.mark.parametrize("bad", ["nope", "a@b", "@b.com", "a b@c.com"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValidationFailed):
            coerce(EmailField("e"), bad)

    def test_accepts_valid(self):
        assert coerce(EmailField("e"), "ada@analytical.io") == "ada@analytical.io"


class TestURL:
    def test_bare_domain_gets_a_scheme(self):
        assert URLField("u").to_python("example.com") == "https://example.com"

    def test_existing_scheme_is_preserved(self):
        assert URLField("u").to_python("http://example.com") == "http://example.com"

    def test_non_http_scheme_is_preserved(self):
        assert URLField("u").to_python("ftp://files.example.com") == "ftp://files.example.com"


class TestPhone:
    def test_keeps_meaningful_punctuation(self):
        assert PhoneField("p").to_python("+1 (555) 010-9999") == "+1 (555) 010-9999"

    def test_strips_letters(self):
        assert PhoneField("p").to_python("555-0100 ext") == "555-0100"


class TestNumbers:
    def test_integer_accepts_thousands_separators(self):
        assert IntegerField("n").to_python("1,200") == 1200

    def test_integer_accepts_a_trailing_decimal_zero(self):
        # Spreadsheet exports commonly render integers this way.
        assert IntegerField("n").to_python("1200.0") == 1200

    def test_integer_rejects_words(self):
        with pytest.raises(ValidationFailed):
            IntegerField("n").to_python("many")

    def test_integer_bounds(self):
        with pytest.raises(ValidationFailed):
            coerce(IntegerField("n", min=1), "0")
        with pytest.raises(ValidationFailed):
            coerce(IntegerField("n", max=10), "11")

    def test_decimal_keeps_precision(self):
        assert DecimalField("d").to_python("10.25") == Decimal("10.25")

    def test_decimal_stores_as_float(self):
        assert DecimalField("d").to_storage(Decimal("10.25")) == 10.25

    def test_currency_carries_its_unit(self):
        assert CurrencyField("amount", currency="EUR").currency == "EUR"

    def test_numeric_fields_align_right(self):
        assert IntegerField("n").align == "right"


class TestBoolean:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "y", True])
    def test_truthy(self, raw):
        assert BooleanField("b").to_python(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", False])
    def test_falsy(self, raw):
        assert BooleanField("b").to_python(raw) is False

    def test_rejects_nonsense(self):
        with pytest.raises(ValidationFailed):
            BooleanField("b").to_python("maybe")


class TestTemporal:
    def test_date_from_iso(self):
        assert DateField("d").to_python("2026-03-04") == date(2026, 3, 4)

    def test_date_truncates_a_datetime_string(self):
        assert DateField("d").to_python("2026-03-04T10:00:00") == date(2026, 3, 4)

    def test_date_rejects_other_formats(self):
        with pytest.raises(ValidationFailed):
            DateField("d").to_python("04/03/2026")

    def test_date_stores_as_a_python_object(self):
        # Not an ISO string: serialising in the field would bake in one
        # backend's wire format. A typed SQL column wants the object; a JSON
        # API wants the string. Adapting is each provider's job.
        assert DateField("d").to_storage(date(2026, 3, 4)) == date(2026, 3, 4)

    def test_datetime_accepts_a_space_separator(self):
        from datetime import UTC

        # Aware, in UTC: an instant with no zone attached is meaningless, and
        # a value arriving without one is taken to be UTC.
        assert DateTimeField("d").to_python("2026-03-04 10:30") == datetime(
            2026, 3, 4, 10, 30, tzinfo=UTC
        )

    def test_datetime_accepts_zulu_time(self):
        parsed = DateTimeField("d").to_python("2026-03-04T10:30:00Z")
        assert parsed.tzinfo is not None

    def test_duration_accepts_h_mm(self):
        assert DurationField("d").to_python("1:30") == 90

    def test_duration_accepts_plain_minutes(self):
        assert DurationField("d").to_python("45") == 45


class TestSelect:
    def test_static_choices_are_normalised(self):
        f = SelectField("stage", choices=[("won", "Won"), "open"])
        options = f.choices(CTX)
        assert options[0] == Choice("won", "Won")
        assert options[1].label == "open"

    def test_callable_choices_are_resolved_per_request(self):
        calls = []

        def source(ctx):
            calls.append(ctx)
            return ["a", "b"]

        f = SelectField("x", choices=source)
        assert len(f.choices(CTX)) == 2
        f.choices(CTX)
        assert len(calls) == 2, "choices must not be cached across requests"

    def test_value_outside_the_set_is_rejected(self):
        with pytest.raises(ValidationFailed):
            coerce(SelectField("stage", choices=["won", "open"]), "invented")

    def test_empty_choice_source_accepts_anything(self):
        # A lookup table that is legitimately empty must not reject every value.
        assert coerce(SelectField("x", choices=lambda ctx: []), "anything") == "anything"

    def test_select_defaults_to_appearing_in_filters(self):
        assert SelectField("stage", choices=["a"]).in_filter is True


class TestMultiSelect:
    def test_parses_a_comma_list(self):
        assert MultiSelectField("t").to_python("a, b ,c") == ["a", "b", "c"]

    def test_parses_json(self):
        assert MultiSelectField("t").to_python('["a","b"]') == ["a", "b"]

    def test_accepts_a_real_list_from_a_multi_valued_form_field(self):
        assert MultiSelectField("t").to_python(["a", "b"]) == ["a", "b"]

    def test_round_trips_through_storage(self):
        f = MultiSelectField("t")
        stored = f.to_storage(["a", "b"])
        assert json.loads(stored) == ["a", "b"]
        assert f.to_display(stored) == ["a", "b"]

    def test_blank_becomes_an_empty_list_not_empty(self):
        assert MultiSelectField("t").to_python("") == []


class TestJSON:
    def test_parses_an_object(self):
        assert JSONField("j").to_python('{"a": 1}') == {"a": 1}

    def test_rejects_malformed(self):
        with pytest.raises(ValidationFailed):
            JSONField("j").to_python("{not json")

    def test_display_parses_stored_text(self):
        assert JSONField("j").to_display('{"a": 1}') == {"a": 1}

    def test_display_passes_through_unparseable_text(self):
        # Better to show the raw value than to hide a data problem.
        assert JSONField("j").to_display("legacy value") == "legacy value"

    def test_stays_out_of_list_views_by_default(self):
        assert JSONField("j").in_list is False


class TestRelation:
    def test_captures_its_target(self):
        f = RelationField("company_id", resource="companies", display="name")
        assert f.relation.resource == "companies"
        assert f.relation.search_fields() == ("name",)

    def test_explicit_search_fields_win(self):
        f = RelationField("c", resource="companies", display="name", search=["name", "domain"])
        assert f.relation.search_fields() == ("name", "domain")


class TestAccessControl:
    def test_field_without_roles_is_visible_to_everyone(self):
        assert TextField("x").visible_to(Identity(subject="u"))

    def test_read_roles_are_enforced(self):
        f = TextField("salary", read_roles=["finance"])
        assert not f.visible_to(Identity(subject="u", roles=frozenset({"sales"})))
        assert f.visible_to(Identity(subject="u", roles=frozenset({"finance"})))

    def test_admin_satisfies_any_role(self):
        f = TextField("salary", read_roles=["finance"])
        assert f.visible_to(Identity(subject="u", roles=frozenset({"admin"})))

    def test_readonly_fields_are_never_writable(self):
        f = TextField("x", readonly=True)
        assert not f.writable_by(Identity(subject="u", roles=frozenset({"admin"})))

    def test_write_roles_are_enforced(self):
        f = TextField("x", write_roles=["manager"])
        assert not f.writable_by(Identity(subject="u", roles=frozenset({"sales"})))
        assert f.writable_by(Identity(subject="u", roles=frozenset({"manager"})))


class TestComputed:
    def test_compute_replaces_stored_lookup(self):
        f = Field("full", compute=lambda r: f"{r['first']} {r['last']}")
        assert f.extract({"first": "Ada", "last": "Lovelace"}) == "Ada Lovelace"

    def test_computed_fields_are_readonly(self):
        assert Field("full", compute=lambda r: "x").readonly is True

    def test_plain_field_falls_back_to_its_default(self):
        assert Field("missing", default="—").extract({}) == "—"


class TestFilterOperators:
    def test_text_offers_containment(self):
        assert Op.ICONTAINS in TextField("x").supported_ops

    def test_numbers_offer_ranges(self):
        assert Op.BETWEEN in IntegerField("n").supported_ops

    def test_booleans_do_not_offer_containment(self):
        assert Op.ICONTAINS not in BooleanField("b").supported_ops

    def test_operators_are_returned_in_menu_order(self):
        ops = TextField("x").filter_ops()
        assert list(ops) == sorted(ops, key=list(Op).index)

    def test_sequence_operator_values_are_coerced_elementwise(self):
        assert IntegerField("n").parse_filter_value(Op.IN, "1,2,3") == [1, 2, 3]

    def test_unary_operator_takes_no_value(self):
        assert TextField("x").parse_filter_value(Op.IS_NULL, "ignored") is None


class TestRegistry:
    def test_every_builtin_type_is_registered(self):
        assert len(registered_types()) >= 20

    def test_lookup_by_name(self):
        assert get_field_type("email") is EmailField

    def test_unknown_type_names_what_is_available(self):
        with pytest.raises(RegistryError, match="unknown field type"):
            get_field_type("nonexistent")

    def test_data_driven_construction(self):
        f = make_field("currency", "amount", currency="GBP", label="Deal size")
        assert isinstance(f, CurrencyField)
        assert f.label == "Deal size" and f.currency == "GBP"

    def test_each_type_declares_its_templates(self):
        for name, cls in registered_types().items():
            assert cls.display_template.endswith(".html"), name
            assert cls.input_template.endswith(".html"), name


class TestClone:
    def test_clone_overrides_settings(self):
        original = TextField("name", label="Name", required=True)
        relabelled = original.clone(label="Full name")
        assert relabelled.label == "Full name"
        assert relabelled.required is True
        assert original.label == "Name", "the original must be untouched"

    def test_clone_preserves_the_concrete_type(self):
        assert isinstance(EmailField("e").clone(label="E-mail"), EmailField)


class TestCheckboxWireFormat:
    """The rendered checkbox pairs a hidden 'false' with a 'true' value."""

    def test_checked_box_submits_both_and_the_last_wins(self):
        assert BooleanField("b").to_python(["false", "true"]) is True

    def test_unchecked_box_submits_only_the_hidden_false(self):
        assert BooleanField("b").to_python(["false"]) is False

    def test_empty_list_is_false(self):
        assert BooleanField("b").to_python([]) is False
