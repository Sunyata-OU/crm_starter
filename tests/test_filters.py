"""Filtering: the query-string grammars, value aliases, and the builder's menus.

Two grammars reach these routes -- the readable ``f.field__op=value`` that every
link uses, and the indexed ``fc.N.*`` triples the filter panel submits -- and the
second is only ever an input method: it is rewritten into the first and
redirected. Most of what follows is about that boundary, and about the aliases
that let one saved link mean something different to each person who opens it.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.core import clock
from app.core.query import Condition, Op, iter_conditions
from app.core.results import ANONYMOUS, Identity
from app.fields.types import DateField, SelectField, TextField
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from app.resources.views import SearchSpec
from app.web.filters import (
    UNRESOLVED,
    active_filters,
    canonical_params,
    filter_schema,
    has_panel_params,
    parse_filters,
    resolve_alias,
)

from .conftest import sign_in


def rows_in(html: str) -> int:
    return len(re.findall(r'data-pk="[^"]+"', html))


def conditions(f) -> list[Condition]:
    return list(iter_conditions(f))


STAGES = [("open", "Open"), ("won", "Won"), ("lost", "Lost")]


@pytest.fixture
def tasks() -> Resource:
    """A resource with a date column, an owner and a choice list to filter on."""
    return Resource(
        "tasks",
        provider=MemoryProvider([]),
        fields=[
            TextField("id", in_form=False),
            TextField("title", searchable=True),
            TextField("owner", in_filter=True),
            SelectField("stage", choices=STAGES, in_filter=True),
            DateField("due", in_filter=True),
            TextField("secret", read_roles=["admin"], in_filter=True),
        ],
        search=SearchSpec(
            value_aliases={
                "my_region": lambda identity: "emea",
                # A resource may shadow a built-in: this owner column holds a
                # login name, not the email "@me" would otherwise resolve to.
                "me": lambda identity: identity.subject,
            }
        ),
    )


KIM = Identity(subject="kim", email="kim@example.com", timezone="Europe/London")


class TestAliasResolution:
    def test_a_plain_value_passes_through(self):
        assert resolve_alias("won", KIM, {}) == "won"

    def test_me_becomes_the_callers_email(self):
        assert resolve_alias("@me", KIM, {}) == "kim@example.com"

    def test_me_is_unresolved_for_an_anonymous_caller(self):
        assert resolve_alias("@me", ANONYMOUS, {}) is UNRESOLVED

    def test_today_is_a_date_not_a_string(self):
        # The field does the final coercion, so the alias layer stays ignorant
        # of whether it landed on a date column or a datetime one.
        assert isinstance(resolve_alias("@today", KIM, {}), date)

    def test_month_start_is_the_first(self):
        assert resolve_alias("@month_start", KIM, {}).day == 1

    def test_week_end_is_six_days_after_week_start(self):
        start = resolve_alias("@week_start", KIM, {})
        assert resolve_alias("@week_end", KIM, {}) - start == timedelta(days=6)
        assert start.weekday() == 0

    def test_an_unknown_alias_is_unresolved_rather_than_literal(self):
        # Filtering on the string "@yesteday" would quietly return nothing and
        # look like a working filter over an empty result.
        assert resolve_alias("@yesteday", KIM, {}) is UNRESOLVED

    def test_a_doubled_sigil_is_a_literal_at_sign(self):
        assert resolve_alias("@@me", KIM, {}) == "@me"

    def test_a_resource_alias_shadows_a_builtin(self, tasks):
        assert resolve_alias("@me", KIM, tasks.search.value_aliases) == "kim"

    def test_a_resource_alias_of_its_own(self, tasks):
        assert resolve_alias("@my_region", KIM, tasks.search.value_aliases) == "emea"


class TestParsingAliasedFilters:
    def test_an_alias_reaches_the_condition(self, tasks):
        [condition] = conditions(parse_filters({"f.owner": "@me"}, tasks, KIM))
        assert condition == Condition("owner", Op.EQ, "kim")

    def test_a_temporal_alias_is_coerced_by_the_field(self, tasks):
        [condition] = conditions(parse_filters({"f.due__gte": "@today"}, tasks, KIM))
        assert condition.value == clock.today("Europe/London")

    def test_the_date_is_the_callers_day_not_the_servers(self, tasks):
        # At 23:00 in London it is already tomorrow in Auckland, so "@today"
        # is resolved in the reader's zone or it is wrong for half of them.
        auckland = KIM.replace(timezone="Pacific/Auckland")
        [here] = conditions(parse_filters({"f.due": "@today"}, tasks, KIM))
        [there] = conditions(parse_filters({"f.due": "@today"}, tasks, auckland))
        assert there.value - here.value in (timedelta(0), timedelta(days=1))

    def test_an_unknown_alias_drops_the_condition(self, tasks):
        assert parse_filters({"f.owner": "@nobody"}, tasks, KIM) is None

    def test_an_alias_inside_a_list_is_expanded(self, tasks):
        [condition] = conditions(parse_filters({"f.owner__in": "@me,sam"}, tasks, KIM))
        assert condition.value == ["kim", "sam"]

    def test_an_unknown_alias_in_a_list_drops_the_whole_condition(self, tasks):
        # Silently dropping just the bad element would widen the match.
        assert parse_filters({"f.owner__in": "@nope,sam"}, tasks, KIM) is None

    def test_a_literal_at_sign_survives(self, tasks):
        [condition] = conditions(parse_filters({"f.owner": "@@handle"}, tasks, KIM))
        assert condition.value == "@handle"


class TestFieldLevelAccess:
    def test_a_field_the_caller_cannot_read_is_not_filterable(self, tasks):
        # An equality filter on a hidden column is a yes/no oracle over it.
        assert parse_filters({"f.secret": "x"}, tasks, KIM) is None

    def test_the_same_filter_works_for_someone_who_may_read_it(self, tasks):
        admin = KIM.replace(roles=frozenset({"admin"}))
        assert parse_filters({"f.secret": "x"}, tasks, admin) is not None

    def test_a_hidden_field_is_absent_from_the_builder(self, tasks):
        names = [f["name"] for f in filter_schema(tasks, KIM)]
        assert "secret" not in names and "owner" in names


class TestUnchangedCanonicalParsing:
    def test_a_bare_key_means_equality(self, tasks):
        [condition] = conditions(parse_filters({"f.stage": "won"}, tasks, KIM))
        assert condition.op is Op.EQ

    def test_an_unknown_field_is_ignored(self, tasks):
        assert parse_filters({"f.nope": "x"}, tasks, KIM) is None

    def test_an_operator_the_field_rejects_is_ignored(self, tasks):
        # A date has no "starts with".
        assert parse_filters({"f.due__startswith": "2026"}, tasks, KIM) is None

    def test_a_blank_value_removes_itself(self, tasks):
        assert parse_filters({"f.stage": ""}, tasks, KIM) is None

    def test_a_unary_operator_keeps_its_blank_value(self, tasks):
        [condition] = conditions(parse_filters({"f.due__is_null": ""}, tasks, KIM))
        assert condition.op is Op.IS_NULL

    def test_a_half_written_between_is_dropped_rather_than_raising(self, tasks):
        assert parse_filters({"f.due__between": "2026-01-01"}, tasks, KIM) is None

    def test_an_empty_list_is_dropped(self, tasks):
        assert parse_filters({"f.stage__in": ","}, tasks, KIM) is None


class TestPanelGrammar:
    def test_panel_params_are_recognised(self):
        assert has_panel_params({"fc.0.field": "stage"})
        assert not has_panel_params({"f.stage": "won"})

    def canonical(self, items, tasks):
        return dict(canonical_params(items, tasks))

    def test_a_triple_becomes_a_readable_pair(self, tasks):
        items = [("fc.0.field", "due"), ("fc.0.op", "gte"), ("fc.0.value", "2026-01-01")]
        assert self.canonical(items, tasks) == {"f.due__gte": "2026-01-01"}

    def test_equality_drops_the_operator_suffix(self, tasks):
        items = [("fc.0.field", "stage"), ("fc.0.op", "eq"), ("fc.0.value", "won")]
        assert self.canonical(items, tasks) == {"f.stage": "won"}

    def test_a_unary_operator_needs_no_value(self, tasks):
        items = [("fc.0.field", "due"), ("fc.0.op", "is_null"), ("fc.0.value", "")]
        assert self.canonical(items, tasks) == {"f.due__is_null": ""}

    def test_a_blank_row_is_how_a_condition_is_removed(self, tasks):
        items = [("fc.0.field", "stage"), ("fc.0.op", "eq"), ("fc.0.value", "  ")]
        assert self.canonical(items, tasks) == {}

    def test_an_unsupported_operator_is_dropped(self, tasks):
        items = [("fc.0.field", "due"), ("fc.0.op", "startswith"), ("fc.0.value", "2026")]
        assert self.canonical(items, tasks) == {}

    def test_an_unknown_field_is_dropped(self, tasks):
        items = [("fc.0.field", "nope"), ("fc.0.op", "eq"), ("fc.0.value", "x")]
        assert self.canonical(items, tasks) == {}

    def test_stale_canonical_filters_are_replaced_not_merged(self, tasks):
        # The panel submits the complete set; the f.* alongside it is the copy
        # baked into the form's action URL, and keeping it would make a removed
        # condition impossible to remove.
        items = [
            ("f.stage", "lost"),
            ("fc.0.field", "stage"),
            ("fc.0.op", "eq"),
            ("fc.0.value", "won"),
        ]
        assert self.canonical(items, tasks) == {"f.stage": "won"}

    def test_the_page_number_is_dropped(self, tasks):
        items = [("page", "7"), ("fc.0.field", "stage"), ("fc.0.op", "eq"), ("fc.0.value", "won")]
        assert "page" not in self.canonical(items, tasks)

    def test_everything_else_is_carried_through(self, tasks):
        items = [("q", "ada"), ("view", "board"), ("sort", "-due")]
        assert self.canonical(items, tasks) == {"q": "ada", "view": "board", "sort": "-due"}

    def test_rows_are_ordered_numerically_not_lexically(self, tasks):
        items = []
        for index, stage in ((0, "won"), (10, "lost"), (2, "open")):
            items += [
                (f"fc.{index}.field", "stage"),
                (f"fc.{index}.op", "in"),
                (f"fc.{index}.value", stage),
            ]
        assert [v for _, v in canonical_params(items, tasks)] == ["won", "open", "lost"]


class TestBuilderSchema:
    def test_operators_follow_the_column(self, tasks):
        schema = {f["name"]: f for f in filter_schema(tasks, KIM)}
        date_ops = [o["value"] for o in schema["due"]["ops"]]
        text_ops = [o["value"] for o in schema["owner"]["ops"]]
        assert "between" in date_ops and "between" not in text_ops
        assert "startswith" in text_ops and "startswith" not in date_ops

    def test_operator_arity_is_declared(self, tasks):
        schema = {f["name"]: f for f in filter_schema(tasks, KIM)}
        arity = {o["value"]: o["arity"] for o in schema["due"]["ops"]}
        assert arity["is_null"] == "none"
        assert arity["between"] == "range"
        assert arity["eq"] == "one"

    def test_a_choice_column_carries_its_options(self, tasks):
        schema = {f["name"]: f for f in filter_schema(tasks, KIM)}
        assert [c["value"] for c in schema["stage"]["choices"]] == ["open", "won", "lost"]
        assert schema["owner"]["choices"] == []

    def test_temporal_aliases_are_offered_only_on_dates(self, tasks):
        # "@today" on a name column, or "@me" on a date, would be menu noise.
        schema = {f["name"]: f for f in filter_schema(tasks, KIM)}
        due = [a["value"] for a in schema["due"]["aliases"]]
        owner = [a["value"] for a in schema["owner"]["aliases"]]
        assert "@today" in due and "@today" not in owner
        assert "@me" in owner

    def test_resource_aliases_are_offered_everywhere(self, tasks):
        schema = {f["name"]: f for f in filter_schema(tasks, KIM)}
        assert "@my_region" in [a["value"] for a in schema["owner"]["aliases"]]

    def test_a_date_column_asks_for_a_date_input(self, tasks):
        schema = {f["name"]: f for f in filter_schema(tasks, KIM)}
        assert schema["due"]["input"] == "date"
        assert schema["owner"]["input"] == "text"


class TestChips:
    def test_a_choice_value_reads_as_its_label(self, tasks):
        [chip] = active_filters({"f.stage": "won"}, tasks)
        assert chip["value_label"] == "Won"

    def test_an_alias_reads_as_its_wording(self, tasks):
        [chip] = active_filters({"f.due__gte": "@month_start"}, tasks)
        assert chip["value_label"] == "start of this month"

    def test_an_empty_operator_has_no_value_to_show(self, tasks):
        [chip] = active_filters({"f.due__is_null": ""}, tasks)
        assert chip["op_label"] == "is empty" and chip["value_label"] == ""


class TestThroughTheRoutes:
    def test_me_narrows_to_the_callers_own_records(self, client):
        sign_in(client, email="kim@example.com", roles=["admin"])
        assert rows_in(client.get("/r/contacts?f.owner=@me").text) == 2

    def test_the_same_link_means_something_else_for_someone_else(self, admin):
        assert rows_in(admin.get("/r/contacts?f.owner=@me").text) == 0

    def test_the_panel_redirects_to_a_readable_url(self, admin):
        response = admin.get(
            "/r/contacts?fc.0.field=status&fc.0.op=eq&fc.0.value=active",
            follow_redirects=False,
        )
        assert response.status_code == 303
        query = dict(parse_qsl(urlsplit(response.headers["location"]).query))
        assert query == {"f.status": "active"}

    def test_following_the_redirect_filters(self, admin):
        response = admin.get("/r/contacts?fc.0.field=status&fc.0.op=eq&fc.0.value=active")
        assert rows_in(response.text) == 2

    def test_the_redirect_keeps_the_search_term(self, admin):
        response = admin.get(
            "/r/contacts?q=ada&fc.0.field=status&fc.0.op=eq&fc.0.value=active",
            follow_redirects=False,
        )
        assert ("q", "ada") in parse_qsl(urlsplit(response.headers["location"]).query)

    def test_an_empty_panel_submission_clears_every_filter(self, admin):
        response = admin.get(
            "/r/contacts?f.status=active&fc.0.field=status&fc.0.op=eq&fc.0.value=",
            follow_redirects=False,
        )
        assert urlsplit(response.headers["location"]).query == ""

    def test_a_link_without_panel_params_is_not_redirected(self, admin):
        response = admin.get("/r/contacts?f.status=active", follow_redirects=False)
        assert response.status_code == 200

    def test_the_builder_ships_its_menus_to_the_page(self, admin):
        html = admin.get("/r/contacts").text
        assert "crm.filterPanel(" in html
        assert "is any of" in html


class TestTheToolbarOnEveryView:
    """The filter panel is the same panel on a list, a board, a chart and a
    calendar, so it is supplied centrally -- these guard against one view
    quietly losing it."""

    @pytest.mark.parametrize("url", [
        "/r/deals",
        "/r/deals?view=board",
        "/r/deals?view=chart",
    ])
    def test_the_builder_is_present(self, admin, url):
        assert "crm.filterPanel(" in admin.get(url).text

    def test_a_filter_applied_on_a_board_still_shows_its_chip(self, admin):
        assert "<strong>Won</strong>" in admin.get("/r/deals?view=board&f.stage=won").text

    def test_the_panel_carries_the_view_through_its_submission(self, admin):
        html = admin.get("/r/deals?view=board").text
        assert '<input type="hidden" name="view" value="board">' in html

    def test_the_panel_carries_an_unrelated_parameter_through(self, admin):
        # Listed by exclusion, so a parameter a project adds later survives
        # Apply without anyone remembering to add it here.
        html = admin.get("/r/deals?view=calendar&month=3&year=2026").text
        assert '<input type="hidden" name="month" value="3">' in html

    def test_the_panel_does_not_carry_the_stale_filters(self, admin):
        html = admin.get("/r/deals?f.stage=won").text
        assert 'name="f.stage"' not in html.split('class="chips"')[0].split("<noscript>")[0]
