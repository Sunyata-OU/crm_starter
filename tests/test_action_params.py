"""Actions that ask for something before they run.

Two features meet here, and they exist for the same reason: an action is
usually a call to something outside this application, and the interesting part
is what you tell it and what it says back.

`prompt_fields` collects values the record does not carry -- a reason, a note,
a date -- and hands them to the handler. `RowOutcome` lets a handler that ran
over forty records say what became of each, rather than the framework choosing
between "Done." and nothing.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from app.core.errors import ConfigError
from app.core.registry import Registry
from app.core.results import Ctx, Identity
from app.fields.types import SelectField, TextAreaField, TextField
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.actions import Action, ActionResult, RowOutcome, action
from app.resources.resource import Resource
from app.resources.views import Column, DetailView, ListView, Section
from app.settings import Settings
from tests.conftest import sign_in

REASONS = [("spam", "Spam"), ("dupe", "Duplicate")]

#: What each call to the parameterised action was given, for the assertions.
CALLS: list[dict] = []


@action(
    "close",
    "Close",
    style="danger",
    placements=("row", "detail", "bulk"),
    prompt_fields=[
        SelectField("reason", label="Reason", choices=REASONS, required=True),
        TextAreaField("note", label="Note", rows=2),
    ],
)
async def close_tickets(records, ctx: Ctx, resource: Resource, *, params) -> ActionResult:
    CALLS.append({"pks": [r.pk for r in records], "params": dict(params)})
    outcomes = [
        # The second ticket refuses, so the partial-failure path is exercised
        # by something other than a mock.
        RowOutcome(r.pk, r.pk != "2", "" if r.pk != "2" else "already closed")
        for r in records
    ]
    return ActionResult.from_outcomes(outcomes, done="Closed")


@action("touch", "Touch", placements=("row", "detail", "bulk"))
async def touch(records, ctx: Ctx, resource: Resource) -> ActionResult:
    """The old three-argument shape, which must keep working untouched."""
    CALLS.append({"pks": [r.pk for r in records], "params": None})
    return ActionResult(message="Touched.")


def build_registry() -> Registry:
    registry = Registry()
    registry.add_resource(
        Resource(
            "tickets",
            provider=MemoryProvider(
                [
                    {"id": "1", "subject": "Printer on fire", "note": ""},
                    {"id": "2", "subject": "Cannot log in", "note": ""},
                    {"id": "3", "subject": "Password reset", "note": ""},
                ],
                searchable_fields=("subject",),
            ),
            fields=[
                TextField("id", in_form=False),
                TextField("subject", required=True, searchable=True),
                TextAreaField("note"),
            ],
            actions=[close_tickets, touch],
            views=[
                ListView(
                    columns=[Column("subject", link=True)],
                    row_actions=["close", "touch"],
                    bulk_actions=["close", "touch"],
                ),
                DetailView(sections=[Section("Ticket", ["subject", "note"])]),
            ],
        )
    )
    return registry


@pytest.fixture
def admin():
    settings = Settings(
        environment="test",
        secret_key="test-key-not-for-real-use",
        debug=False,
        template_reload=False,
        csrf_enabled=True,
        auth_providers=["session", "local"],
        modules=[],
    )
    app = create_app(settings=settings, registry=build_registry())
    CALLS.clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        sign_in(client, email="admin@example.com", roles=["admin"])
        yield client


def token_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the dialog"
    return match.group(1)


class TestDeclaringPrompts:
    def test_a_field_instance_is_used_as_declared(self):
        resource = build_registry().resource("tickets")
        names = [f.name for f in resource.action("close").prompts(resource)]
        assert names == ["reason", "note"]

    def test_a_name_resolves_against_the_resource(self):
        resource = build_registry().resource("tickets")
        act = Action("x", handler=touch.handler, prompt_fields=["subject"])
        assert [f.name for f in act.prompts(resource)] == ["subject"]

    def test_an_unknown_name_says_so_with_both_names(self):
        resource = build_registry().resource("tickets")
        act = Action("x", handler=touch.handler, prompt_fields=["nonsense"])
        with pytest.raises(ConfigError, match="nonsense"):
            act.prompts(resource)

    def test_an_action_without_prompts_needs_no_dialog(self):
        resource = build_registry().resource("tickets")
        assert resource.action("close").prompts_for_input
        assert not resource.action("touch").prompts_for_input


class TestCallingTheHandler:
    async def test_params_reach_a_handler_that_asks_for_them(self):
        resource = build_registry().resource("tickets")
        ctx = Ctx(identity=Identity(subject="t"), request_id="t")
        await resource.action("close").run([], ctx, resource, {"reason": "spam"})
        assert CALLS[-1]["params"] == {"reason": "spam"}

    async def test_a_three_argument_handler_is_called_unchanged(self):
        """The compatibility promise: existing actions keep working."""
        resource = build_registry().resource("tickets")
        ctx = Ctx(identity=Identity(subject="t"), request_id="t")
        result = await resource.action("touch").run([], ctx, resource, {"ignored": 1})
        assert result.message == "Touched."
        assert CALLS[-1]["params"] is None


class TestReportingOutcomes:
    def test_all_succeeding_reads_as_success(self):
        result = ActionResult.from_outcomes(
            [RowOutcome(1, True), RowOutcome(2, True)], done="Closed"
        )
        assert result.level == "success" and "2" in result.message

    def test_a_mixed_batch_is_a_warning_naming_the_failures(self):
        result = ActionResult.from_outcomes(
            [RowOutcome(1, True), RowOutcome(2, False, "already closed")], done="Closed"
        )
        assert result.level == "warning"
        assert "1 failed" in result.message and "already closed" in result.message

    def test_a_batch_that_all_failed_is_an_error_and_does_not_refresh(self):
        result = ActionResult.from_outcomes([RowOutcome(2, False, "nope")])
        assert result.level == "error" and result.refresh is False

    def test_long_failure_lists_are_summarised_rather_than_dumped(self):
        outcomes = [RowOutcome(i, False, "nope") for i in range(10)]
        assert "and 7 more" in ActionResult.from_outcomes(outcomes).message


class TestTheDialog:
    def test_the_row_button_opens_the_dialog_rather_than_running(self, admin):
        html = admin.get("/r/tickets").text
        assert "/r/tickets/1/action/close/prompt" in html
        # The unparameterised one still fires straight from the row.
        assert 'hx-post="/r/tickets/1/action/touch"' in html

    def test_the_dialog_renders_the_fields(self, admin):
        html = admin.get("/r/tickets/1/action/close/prompt").text
        assert 'name="reason"' in html and 'name="note"' in html
        assert "Printer on fire" in html  # says what is being acted on

    def test_running_with_values_passes_them_to_the_handler(self, admin):
        prompt = admin.get("/r/tickets/1/action/close/prompt").text
        response = admin.post(
            "/r/tickets/1/action/close",
            data={"csrf_token": token_from(prompt), "reason": "spam", "note": "obvious"},
        )
        assert response.status_code < 400
        assert CALLS[-1]["params"] == {"reason": "spam", "note": "obvious"}

    def test_a_missing_required_value_returns_the_dialog_and_runs_nothing(self, admin):
        prompt = admin.get("/r/tickets/1/action/close/prompt").text
        response = admin.post(
            "/r/tickets/1/action/close",
            data={"csrf_token": token_from(prompt), "reason": "", "note": "x"},
        )
        assert response.status_code == 422
        assert "Reason is required" in response.text
        assert not CALLS

    def test_the_bulk_dialog_carries_the_selection(self, admin):
        html = admin.get("/r/tickets/bulk/close/prompt?selected=1&selected=3").text
        assert html.count('name="selected"') == 2
        assert "2 selected" in html

    def test_a_bulk_run_reports_each_row(self, admin):
        prompt = admin.get("/r/tickets/bulk/close/prompt?selected=1&selected=2").text
        response = admin.post(
            "/r/tickets/bulk/close",
            data={
                "csrf_token": token_from(prompt),
                "selected": ["1", "2"],
                "reason": "dupe",
            },
        )
        assert response.status_code < 400
        assert CALLS[-1]["pks"] == ["1", "2"]
        # Ticket 2 refuses, and the toast says so rather than claiming success.
        toast = response.headers.get("hx-trigger", "") + response.text
        assert "already closed" in toast or "failed" in toast
