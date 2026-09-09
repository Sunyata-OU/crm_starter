"""Grouping a list by something no column holds.

`CaseField` exists because the value a screen is organised by is often not a
column: a job's lifecycle is a status id, a cancellation date and whether any
shift is still to come. Computing that in Python would give a screen that
cannot sort, group or paginate without pulling every row; compiled into the
query it does all three.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.query import Condition, ListQuery, Op, Sort
from app.core.results import Ctx, Identity
from app.fields.types import CaseField
from app.providers.sql import SQLConnection, SQLProvider, build_derived

ADMIN = Identity(subject="t", roles=("admin",), claims={})
CTX = Ctx(identity=ADMIN, request_id="r")


class FakeResource:
    def __init__(self, fields):
        self.fields = fields
        self.pk = "id"


STAGE = CaseField(
    "stage",
    cases=[
        (Condition("cancelled", Op.EQ, True), "Cancelled"),
        (Condition("status", Op.NE, 2), "Needs publishing"),
    ],
    default="Active",
    order=["Needs publishing", "Active", "Cancelled"],
)


@pytest.fixture
async def provider(tmp_path):
    engine = sa.ext.asyncio.create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    metadata = sa.MetaData()
    sa.Table(
        "jobs", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.Integer),
        sa.Column("cancelled", sa.Boolean),
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(
            sa.text("INSERT INTO jobs (id, status, cancelled) VALUES "
                    "(1, 2, 0), (2, 1, 0), (3, 2, 1)")
        )
    connection = SQLConnection(engine)
    return SQLProvider(
        connection, "jobs", pk_field="id",
        resource=FakeResource([STAGE]),
    )


class TestTheRankIsWhatIsSelected:
    def test_the_declared_reading_order_decides_the_rank(self):
        assert [STAGE.rank(v) for v in STAGE.order] == [0, 1, 2]

    def test_a_rank_reads_back_as_its_label(self):
        assert STAGE.to_display(0) == "Needs publishing"
        assert STAGE.to_display(2) == "Cancelled"

    def test_sorting_labels_would_have_given_the_wrong_order(self):
        """Alphabetically: Active, Cancelled, Needs publishing. Nobody's idea
        of a lifecycle -- which is why the expression emits the rank."""
        assert sorted(STAGE.order) != STAGE.order

    def test_matching_order_and_reading_order_are_separate(self):
        """Cancelled has to be *tested* first, because the columns behind it
        are independent, and *read* last."""
        matched = [label for _, label in STAGE.cases]
        assert matched[0] == "Cancelled"
        assert STAGE.order[-1] == "Cancelled"

    def test_it_is_never_written(self):
        from app.core.errors import UnsupportedOperation

        with pytest.raises(UnsupportedOperation):
            STAGE.to_storage("Active")


class TestTheDatabaseDoesTheWork:
    async def test_the_expression_is_selected_alongside_the_columns(self, provider):
        page = await provider.list(ListQuery(page_size=10), CTX)
        assert all("stage" in row for row in page.items)

    async def test_each_row_lands_in_the_right_stage(self, provider):
        page = await provider.list(ListQuery(page_size=10, sort=(Sort("id"),)), CTX)
        stages = [STAGE.to_display(row["stage"]) for row in page.items]
        assert stages == ["Active", "Needs publishing", "Cancelled"]

    async def test_ordering_by_it_reads_in_the_declared_order(self, provider):
        page = await provider.list(ListQuery(page_size=10, sort=(Sort("stage"),)), CTX)
        assert [STAGE.to_display(r["stage"]) for r in page.items] == [
            "Needs publishing", "Active", "Cancelled",
        ]

    async def test_pagination_still_works_across_the_grouping(self, provider):
        """The whole reason this is a query expression: a grouped list still
        pages, because the database is doing the ordering."""
        first = await provider.list(
            ListQuery(page_size=2, sort=(Sort("stage"),)), CTX
        )
        second = await provider.list(
            ListQuery(page=2, page_size=2, sort=(Sort("stage"),)), CTX
        )
        assert [r["id"] for r in first.items] == [2, 1]
        assert [r["id"] for r in second.items] == [3]

    async def test_a_table_with_no_case_fields_is_untouched(self, tmp_path):
        engine = sa.ext.asyncio.create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/u.db")
        metadata = sa.MetaData()
        sa.Table("plain", metadata, sa.Column("id", sa.Integer, primary_key=True))
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        provider = SQLProvider(
            SQLConnection(engine), "plain", pk_field="id",
            resource=FakeResource([]),
        )
        assert await provider.derived() == {}

    async def test_a_branch_naming_a_column_that_is_not_there_says_so(self, provider):
        """Loudly, at build time, naming the columns that do exist -- the same
        error any other bad field name gives. A branch silently dropped would
        file rows under the wrong stage for ever."""
        from app.core.errors import ProviderError

        table = await provider.table()
        field = CaseField(
            "x", cases=[(Condition("nope", Op.EQ, 1), "A")], default="B",
        )
        with pytest.raises(ProviderError, match="nope"):
            build_derived(table, FakeResource([field]))
