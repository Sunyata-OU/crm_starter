"""Filter values are coerced to the type their column compares against.

These assert on the **bound parameters** of the compiled statement rather than
on query results, and deliberately so: the suite runs on SQLite, which compares
``'335'`` to an integer column happily and returns the row. A results-based test
would pass here and the application would still return nothing on PostgreSQL,
which is where this bug lived -- silently, because the relation-label lookup
swallows its exceptions so a failed lookup cannot break the list it decorates.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Boolean, Column, Date, Integer, MetaData, Numeric, String, Table

from app.core.query import Condition, Op
from app.providers.sql import _coerce_value, build_condition, build_filter


@pytest.fixture
def table() -> Table:
    md = MetaData()
    return Table(
        "records",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(120)),
        Column("amount", Numeric(10, 2)),
        Column("active", Boolean),
        Column("closed", Date),
    )


def params(expression) -> list:
    """The values SQLAlchemy would bind, flattened.

    An ``IN`` binds its whole list to one expanding parameter, so the values
    are flattened to keep these assertions about types rather than about how
    SQLAlchemy happens to group them. Booleans are absent here by design --
    they compile to a SQL literal and bind nothing, which is why the boolean
    cases below test the coercion directly instead.
    """
    compiled = expression.compile()
    out: list = []
    for key in compiled.positiontup or compiled.params:
        value = compiled.params[key]
        out.extend(value) if isinstance(value, list) else out.append(value)
    return out


class TestIntegerColumns:
    def test_a_text_value_is_bound_as_an_integer(self, table):
        # The bug: a relation key collected with str(value) and compared to an
        # integer column matches nothing on PostgreSQL.
        assert params(build_condition(table, Condition("id", Op.EQ, "335"))) == [335]

    def test_every_element_of_an_in_clause_is_converted(self, table):
        # This is the relation-label lookup's shape: pk IN (...) built from keys
        # the caller stringified.
        bound = params(build_condition(table, Condition("id", Op.IN, ["1", "2", "3"])))
        assert bound == [1, 2, 3]

    def test_a_mixed_in_clause_converts_only_what_needs_it(self, table):
        assert params(build_condition(table, Condition("id", Op.IN, ["1", 2]))) == [1, 2]

    def test_not_in_is_converted_too(self, table):
        assert params(build_condition(table, Condition("id", Op.NOT_IN, ["7"]))) == [7]

    def test_both_ends_of_a_between_are_converted(self, table):
        bound = params(build_condition(table, Condition("id", Op.BETWEEN, ["10", "20"])))
        assert bound == [10, 20]

    def test_comparisons_are_converted(self, table):
        for op in (Op.LT, Op.LTE, Op.GT, Op.GTE, Op.NE):
            assert params(build_condition(table, Condition("id", op, "42"))) == [42]

    def test_an_integer_is_left_alone(self, table):
        assert params(build_condition(table, Condition("id", Op.EQ, 335))) == [335]


class TestOtherTypedColumns:
    def test_a_decimal_column_gets_a_decimal(self, table):
        [bound] = params(build_condition(table, Condition("amount", Op.GTE, "1000.50")))
        assert bound == Decimal("1000.50") and isinstance(bound, Decimal)

    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("1", True), ("yes", True), ("on", True), ("T", True),
        ("false", False), ("0", False), ("no", False), ("off", False), ("F", False),
    ])
    def test_a_boolean_column_reads_the_usual_spellings(self, table, raw, expected):
        # Asserted on the coerced value: a boolean compiles to a SQL literal
        # and binds no parameter, so there is nothing for `params` to see.
        assert _coerce_value(table.c.active, Op.EQ, raw) is expected

    def test_a_text_column_is_untouched(self, table):
        assert params(build_condition(table, Condition("name", Op.EQ, "335"))) == ["335"]

    def test_a_date_value_passes_through(self, table):
        # Dates are already typed by the field layer before they get here.
        when = date(2026, 6, 15)
        assert params(build_condition(table, Condition("closed", Op.GTE, when))) == [when]


class TestPatternOperatorsAreExcluded:
    """Coercing "12" for a LIKE would turn a substring search into equality."""

    @pytest.mark.parametrize("op", [Op.CONTAINS, Op.ICONTAINS, Op.STARTSWITH, Op.ENDSWITH])
    def test_a_pattern_value_stays_a_string(self, table, op):
        [bound] = params(build_condition(table, Condition("id", op, "12")))
        assert isinstance(bound, str) and "12" in bound


class TestValuesThatWillNotConvert:
    def test_an_unconvertible_value_reaches_the_backend_as_itself(self, table):
        # Rewriting it into something that parses would silently answer a
        # different question than the one asked.
        assert params(build_condition(table, Condition("id", Op.EQ, "abc"))) == ["abc"]

    def test_an_unrecognised_boolean_spelling_is_left_alone(self, table):
        assert _coerce_value(table.c.active, Op.EQ, "maybe") == "maybe"

    def test_a_null_check_needs_no_value(self, table):
        assert params(build_condition(table, Condition("id", Op.IS_NULL))) == []


class TestThroughAFilterTree:
    def test_conditions_nested_in_groups_are_coerced(self, table):
        from app.core.query import and_, or_

        tree = and_(
            Condition("id", Op.IN, ["1", "2"]),
            or_(Condition("amount", Op.GT, "5"), Condition("active", Op.EQ, "true")),
        )
        # The boolean binds nothing, so it does not appear among the values.
        assert params(build_filter(table, tree)) == [1, 2, Decimal("5")]
