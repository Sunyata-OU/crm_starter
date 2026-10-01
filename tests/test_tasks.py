"""Tasks: work a person owes, and the sweep that tells them about it.

The design point these pin down: nothing notifies at the moment of assignment.
The sweep compares who a task is assigned to with who was last told it was
theirs, so a hand-over made from the form, an inline edit, an action or a
script is announced exactly once, by whichever sweep runs next.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from app import tasks as task_service
from app.core.clock import utcnow
from app.core.registry import Registry, StaffDirectory
from app.core.results import Ctx, Identity
from app.fields.types import DateTimeField, StatusField, TextAreaField, TextField
from app.main import create_app
from app.notify import notifier
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from app.settings import Settings
from tests.conftest import build_registry, csrf_from, sign_in

KIM = "kim@example.com"
SAM = "sam@example.com"


def tasks_resource(store: MemoryProvider) -> Resource:
    return Resource(
        "tasks",
        provider=store,
        stamp={"created_by": "id", "created_by_name": "label"},
        audited=False,
        timeline=False,
        fields=[
            TextField("id", in_form=False),
            TextField("title", required=True),
            TextAreaField("body"),
            StatusField("state", choices=[("open", "Open", "blue"), ("done", "Done", "green")],
                        default="open"),
            TextField("priority"),
            TextField("assignee"),
            TextField("assignee_name"),
            DateTimeField("due_at"),
            TextField("resource"),
            TextField("record_id"),
            TextField("created_by", in_form=False),
            TextField("created_by_name", in_form=False),
            TextField("notified_assignee", in_form=False),
            DateTimeField("reminded_at", in_form=False),
            DateTimeField("done_at", in_form=False),
            TextField("done_by", in_form=False),
        ],
    )


@pytest.fixture
def store() -> MemoryProvider:
    return MemoryProvider([], searchable_fields=("title",))


@pytest.fixture
def sent() -> MemoryProvider:
    return MemoryProvider([], searchable_fields=("title",))


@pytest.fixture
def registry(store, sent) -> Registry:
    registry = Registry()
    registry.add_resource(tasks_resource(store))
    registry.add_resource(
        Resource(
            "notifications",
            provider=sent,
            audited=False,
            timeline=False,
            fields=[
                TextField("id", in_form=False),
                TextField("recipient"), TextField("title"), TextAreaField("body"),
                TextField("kind"), TextField("priority"), TextField("resource"),
                TextField("record_id"), TextField("url"), TextField("actor"),
                TextField("channels"), TextField("delivery"),
                DateTimeField("created_at"), DateTimeField("due_at"),
                DateTimeField("read_at"), DateTimeField("sent_at"),
            ],
        )
    )
    return registry


@pytest.fixture
async def bound(registry, sent):
    """A bound registry with the notifier storing inline, as the CLI runs it."""
    await registry.bind()
    notifier.bind(registry.resource("notifications").provider)
    notifier.use([])
    notifier.background = False
    yield registry
    notifier.provider = None
    await registry.close()


@pytest.fixture
def registry_with_directory(registry) -> Registry:
    """The same registry, plus a declared staff directory to fall back to."""
    directory = MemoryProvider(
        [
            {"id": "1", "email": KIM, "username": "kim", "firstName": "Kim", "lastName": ""},
            {"id": "2", "email": SAM, "username": "sam", "firstName": "Sam", "lastName": ""},
        ],
        searchable_fields=("email",),
    )
    registry.add_resource(
        Resource(
            "keycloak_users",
            provider=directory,
            audited=False,
            timeline=False,
            fields=[
                TextField("id", in_form=False),
                TextField("email"), TextField("username"),
                TextField("firstName"), TextField("lastName"),
            ],
        )
    )
    registry.staff_directory = StaffDirectory("keycloak_users")
    return registry


@pytest.fixture
async def bound_with_directory(registry_with_directory, sent):
    await registry_with_directory.bind()
    notifier.bind(registry_with_directory.resource("notifications").provider)
    notifier.use([])
    notifier.background = False
    yield registry_with_directory
    notifier.provider = None
    await registry_with_directory.close()


def rows_of(store: MemoryProvider) -> list[dict]:
    return list(store._rows.values())


def a_task(**overrides) -> dict:
    row = {
        "id": 1, "title": "Ring the company back", "body": "", "state": "open",
        "priority": "normal", "assignee": SAM, "assignee_name": "Sam",
        "due_at": None, "resource": "companies", "record_id": "7",
        "notified_assignee": None, "reminded_at": None,
    }
    row.update(overrides)
    return row


class TestBeingTold:
    async def test_an_assignee_hears_once(self, bound, store, sent):
        store._rows[1] = a_task()
        first = await task_service.sweep(bound)
        assert first["assigned"] == 1
        assert [n["recipient"] for n in rows_of(sent)] == [SAM]

        # The second sweep has nothing to say: the task now records who was
        # told, and it has not changed hands.
        again = await task_service.sweep(bound)
        assert again["assigned"] == 0
        assert len(rows_of(sent)) == 1

    async def test_a_hand_over_is_announced_again(self, bound, store, sent):
        store._rows[1] = a_task(notified_assignee=SAM, assignee=KIM)
        await task_service.sweep(bound)
        assert [n["recipient"] for n in rows_of(sent)] == [KIM]

    async def test_a_task_belonging_to_nobody_tells_nobody(self, bound, store, sent):
        store._rows[1] = a_task(assignee=None)
        counts = await task_service.sweep(bound)
        assert counts == {"assigned": 0, "due": 0, "unassigned": 0}
        assert not rows_of(sent)

    async def test_a_finished_task_is_left_alone(self, bound, store, sent):
        store._rows[1] = a_task(state="done")
        await task_service.sweep(bound)
        assert not rows_of(sent)

    async def test_the_notification_points_at_the_record_not_the_task(self, bound, store, sent):
        store._rows[1] = a_task()
        await task_service.sweep(bound)
        assert rows_of(sent)[0]["url"] == "/r/companies/7"

    async def test_a_task_about_nothing_points_at_itself(self, bound, store, sent):
        store._rows[1] = a_task(resource=None, record_id=None)
        await task_service.sweep(bound)
        assert rows_of(sent)[0]["url"] == "/r/tasks/1"


class TestDueDates:
    async def test_an_overdue_task_says_so(self, bound, store, sent):
        store._rows[1] = a_task(
            notified_assignee=SAM, due_at=utcnow() - timedelta(hours=2)
        )
        counts = await task_service.sweep(bound)
        assert counts["due"] == 1
        assert rows_of(sent)[0]["title"].startswith("Overdue:")

    async def test_one_due_soon_warns_before_the_date(self, bound, store, sent):
        store._rows[1] = a_task(
            notified_assignee=SAM, due_at=utcnow() + timedelta(hours=3)
        )
        await task_service.sweep(bound)
        assert rows_of(sent)[0]["title"].startswith("Due soon:")

    async def test_one_due_next_week_is_not_mentioned_yet(self, bound, store, sent):
        store._rows[1] = a_task(notified_assignee=SAM, due_at=utcnow() + timedelta(days=7))
        counts = await task_service.sweep(bound)
        assert counts["due"] == 0

    async def test_the_warning_is_sent_once(self, bound, store, sent):
        store._rows[1] = a_task(notified_assignee=SAM, due_at=utcnow() - timedelta(hours=1))
        await task_service.sweep(bound)
        await task_service.sweep(bound)
        assert len(rows_of(sent)) == 1


class TestWorkNobodyIsOn:
    async def test_the_watchers_hear_about_it(self, bound, store, sent):
        store._rows[1] = a_task(assignee=None, due_at=utcnow() - timedelta(hours=1))
        counts = await task_service.sweep(bound, watchers=[KIM, SAM])
        assert counts["unassigned"] == 2
        assert {n["recipient"] for n in rows_of(sent)} == {KIM, SAM}

    async def test_and_only_once(self, bound, store, sent):
        store._rows[1] = a_task(assignee=None, due_at=utcnow() - timedelta(hours=1))
        await task_service.sweep(bound, watchers=[KIM])
        await task_service.sweep(bound, watchers=[KIM])
        assert len(rows_of(sent)) == 1

    async def test_naming_nobody_tells_nobody(self, bound, store, sent):
        store._rows[1] = a_task(assignee=None, due_at=utcnow() - timedelta(hours=1))
        counts = await task_service.sweep(bound, watchers=[])
        assert counts["unassigned"] == 0
        assert not rows_of(sent)

    async def test_naming_nobody_falls_back_to_the_staff_directory(
        self, bound_with_directory, store, sent
    ):
        store._rows[1] = a_task(assignee=None, due_at=utcnow() - timedelta(hours=1))
        counts = await task_service.sweep(bound_with_directory, watchers=[])
        assert counts["unassigned"] == 2
        assert {n["recipient"] for n in rows_of(sent)} == {KIM, SAM}

    async def test_an_explicit_list_still_overrides_the_directory(
        self, bound_with_directory, store, sent
    ):
        store._rows[1] = a_task(assignee=None, due_at=utcnow() - timedelta(hours=1))
        counts = await task_service.sweep(bound_with_directory, watchers=["only@example.com"])
        assert counts["unassigned"] == 1
        assert {n["recipient"] for n in rows_of(sent)} == {"only@example.com"}


class TestThroughTheScreens:
    @pytest.fixture
    def client(self, store):
        # The module's own declaration, pointed at memory rather than at
        # db.main: the actions and the views under test are the shipped ones.
        from modules.core_tasks import _tasks

        registry = build_registry()
        resource = _tasks()
        resource.provider_ref = store
        registry.add_resource(resource)
        app = create_app(
            settings=Settings(
                environment="test", secret_key="test-key-not-for-real-use",
                template_reload=False, auth_providers=["session", "local"],
                modules=[], notify_channels=[],
            ),
            registry=registry,
        )
        with TestClient(app, raise_server_exceptions=False) as c:
            sign_in(c, email=KIM, roles=["admin"])
            yield c

    def test_raising_one_records_who_raised_it(self, client, store):
        token = csrf_from(client, "/r/tasks/new")
        response = client.post(
            "/r/tasks",
            data={"csrf_token": token, "title": "Check the bank details",
                  "state": "open", "resource": "companies", "record_id": "7"},
            follow_redirects=False,
        )
        assert response.status_code in (200, 303)
        stored = rows_of(store)[0]
        assert stored["created_by"] == KIM
        assert stored["created_by_name"] == "Kim"

    def test_a_record_page_offers_a_task_about_itself(self, client):
        panel = client.get("/r/contacts/1/timeline").text
        assert "with.resource=contacts" in panel
        assert "with.record_id=1" in panel

    def test_assign_to_me_puts_a_name_on_it(self, client, store):
        store._rows[1] = a_task(assignee=None, assignee_name=None)
        token = csrf_from(client, "/r/tasks/1")
        client.post("/r/tasks/1/action/take", data={"csrf_token": token})
        assert store._rows[1]["assignee"] == KIM

    def test_marking_one_done_records_who_and_when(self, client, store):
        store._rows[1] = a_task()
        token = csrf_from(client, "/r/tasks/1")
        client.post("/r/tasks/1/action/finish", data={"csrf_token": token})
        assert store._rows[1]["state"] == "done"
        assert store._rows[1]["done_by"] == KIM
        assert store._rows[1]["done_at"] is not None


class TestTheStamp:
    async def test_it_never_overwrites_what_the_caller_supplied(self, registry, store):
        # An import carrying its own "raised by" is stating a fact this cannot
        # improve on.
        await registry.bind()
        try:
            resource = registry.resource("tasks")
            await resource.provider.create(
                {"title": "Imported", "created_by": "somebody@else.test"},
                Ctx(identity=Identity(subject="1", email=KIM, display_name="Kim")),
            )
        finally:
            await registry.close()
        assert rows_of(store)[0]["created_by"] == "somebody@else.test"
