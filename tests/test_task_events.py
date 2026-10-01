"""Other systems raise tasks as events, and the provider tells everybody.

What these pin down: a `raise` makes one task however many times it is
delivered; an unassigned task is announced to everybody in the directory and
stays there; assigning it (by any path) withdraws that and tells only the
assignee; and `resolve`, or finishing it, withdraws it for good.
"""

from __future__ import annotations

import pytest

from app import task_events
from app import tasks as task_service
from app.core.errors import ValidationFailed
from app.core.registry import Registry, StaffDirectory
from app.core.results import Ctx, Identity
from app.fields.types import DateTimeField, TextAreaField, TextField
from app.notify import notifier
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from tests.test_tasks import KIM, SAM, rows_of, tasks_resource

AVA = "ava@example.com"


def unread(sent: MemoryProvider, who: str) -> list[dict]:
    return [r for r in rows_of(sent) if r["recipient"] == who and not r.get("read_at")]


def event(**over) -> dict:
    base = {
        "type": "raise", "key": "crm:contact:7:review",
        "title": "Seeker needs moderation: Ada", "body": "Cannot take shifts.",
        "resource": "contacts", "record_id": "7",
    }
    return {**base, **over}


@pytest.fixture
def store() -> MemoryProvider:
    return MemoryProvider([], searchable_fields=("title",))


@pytest.fixture
def sent() -> MemoryProvider:
    return MemoryProvider([], searchable_fields=("title",))


@pytest.fixture
def registry(store, sent) -> Registry:
    registry = Registry()
    tasks = tasks_resource(store)
    tasks.fields.append(TextField("source_key", in_form=False))
    tasks.provider_wrap = lambda p: task_service.AnnouncingProvider(p, registry)
    registry.add_resource(tasks)
    registry.add_resource(Resource(
        "notifications", provider=sent, audited=False, timeline=False,
        fields=[
            TextField("id", in_form=False), TextField("recipient"), TextField("title"),
            TextAreaField("body"), TextField("kind"), TextField("priority"),
            TextField("resource"), TextField("record_id"), TextField("url"),
            TextField("actor"), TextField("channels"), TextField("delivery"),
            DateTimeField("created_at"), DateTimeField("due_at"),
            DateTimeField("read_at"), DateTimeField("sent_at"),
        ],
    ))
    registry.add_resource(Resource(
        "staff",
        provider=MemoryProvider(
            [{"id": "1", "email": KIM, "username": "kim"},
             {"id": "2", "email": SAM, "username": "sam"},
             {"id": "3", "email": AVA, "username": "ava"}],
        ),
        audited=False, timeline=False,
        fields=[TextField("id", in_form=False), TextField("email"), TextField("username"),
                TextField("firstName"), TextField("lastName")],
    ))
    registry.staff_directory = StaffDirectory("staff")
    return registry


@pytest.fixture
async def bound(registry):
    from app import notes

    notes._directory_cache = None
    await registry.bind()
    notifier.bind(registry.resource("notifications").provider)
    notifier.use([])
    notifier.background = False
    yield registry
    notifier.provider = None
    notes._directory_cache = None
    await registry.close()


SERVICE = Ctx(identity=Identity(subject="service", email="service@svc", roles=frozenset({"integration"})))


class TestRaise:
    async def test_makes_one_unassigned_task(self, bound, store):
        out = await task_events.apply(bound, event(), SERVICE)
        assert out["status"] == "created"
        (task,) = rows_of(store)
        assert task["source_key"] == "crm:contact:7:review"
        assert task["record_id"] == "7" and not task.get("assignee")

    async def test_delivered_twice_is_still_one(self, bound, store):
        await task_events.apply(bound, event(), SERVICE)
        again = await task_events.apply(bound, event(), SERVICE)
        assert again["status"] == "duplicate" and len(rows_of(store)) == 1

    async def test_the_same_condition_arising_again_after_done_is_new(self, bound, store):
        await task_events.apply(bound, event(), SERVICE)
        await task_events.apply(bound, event(type="resolve"), SERVICE)
        again = await task_events.apply(bound, event(), SERVICE)
        assert again["status"] == "created" and len(rows_of(store)) == 2

    async def test_rejects_a_bad_event(self, bound):
        with pytest.raises(ValidationFailed):
            await task_events.apply(bound, {"type": "raise", "key": "", "title": ""}, SERVICE)


class TestAnnouncing:
    async def test_everybody_is_told_once(self, bound, sent):
        await task_events.apply(bound, event(), SERVICE)
        await task_events.apply(bound, event(), SERVICE)
        for who in (KIM, SAM, AVA):
            assert len(unread(sent, who)) == 1

    async def test_it_stays_until_somebody_takes_it(self, bound, sent, registry):
        await task_events.apply(bound, event(), SERVICE)
        await task_service.sweep(bound)  # unrelated sweeps must not disturb it
        assert len(unread(sent, KIM)) == 1

    async def test_assigning_leaves_only_the_assignee_told(self, bound, sent, registry):
        await task_events.apply(bound, event(), SERVICE)
        tasks = registry.resource("tasks")
        await tasks.provider.update(1, {"assignee": SAM}, Ctx.system())
        assert unread(sent, KIM) == [] and unread(sent, AVA) == []
        assert [r["kind"] for r in unread(sent, SAM)] == ["assigned"]

    async def test_resolving_withdraws_it_from_everyone(self, bound, sent):
        await task_events.apply(bound, event(), SERVICE)
        out = await task_events.apply(bound, event(type="resolve"), SERVICE)
        assert out["status"] == "resolved"
        assert all(unread(sent, who) == [] for who in (KIM, SAM, AVA))

    async def test_finishing_it_by_hand_withdraws_it_too(self, bound, sent, registry):
        await task_events.apply(bound, event(), SERVICE)
        await registry.resource("tasks").provider.update(1, {"state": "done"}, Ctx.system())
        assert all(unread(sent, who) == [] for who in (KIM, SAM, AVA))

    async def test_a_task_made_by_hand_is_announced_but_not_to_its_maker(self, bound, sent, registry):
        ctx = Ctx(identity=Identity(subject="k", email=KIM))
        await registry.resource("tasks").provider.create({"title": "Ring them", "state": "open"}, ctx)
        assert unread(sent, KIM) == [] and len(unread(sent, SAM)) == 1

    async def test_resolving_what_does_not_exist_is_a_no_op(self, bound):
        out = await task_events.apply(bound, event(type="resolve", key="nope"), SERVICE)
        assert out["status"] == "noop"


#: Real callers authenticate with a bearer token, which is what exempts them
#: from CSRF; the header is what matters here, the session cookie stands in for
#: the token provider.
BEARER = {"Authorization": "Bearer test"}


class TestRoute:
    @pytest.fixture
    def client(self, store):
        from starlette.testclient import TestClient

        from app.main import create_app
        from app.settings import Settings
        from modules.core_tasks import _tasks
        from tests.conftest import build_registry

        registry = build_registry()
        resource = _tasks(registry)
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
            yield c

    def test_needs_the_integration_role(self, client):
        from tests.conftest import sign_in

        sign_in(client, email="someone@example.com", roles=["user"])
        assert client.post("/events/tasks", json=event(), headers=BEARER).status_code == 403

    def test_raises_and_is_idempotent(self, client, store):
        from tests.conftest import sign_in

        sign_in(client, email="service@svc", roles=["integration"])
        first = client.post("/events/tasks", json=event(), headers=BEARER)
        again = client.post("/events/tasks", json=event(), headers=BEARER)
        assert first.status_code == 201 and first.json()["status"] == "created"
        assert again.status_code == 200 and again.json()["status"] == "duplicate"
        assert len(rows_of(store)) == 1

    def test_a_bad_payload_is_a_422(self, client):
        from tests.conftest import sign_in

        sign_in(client, email="service@svc", roles=["integration"])
        assert client.post("/events/tasks", json={"type": "raise"}, headers=BEARER).status_code == 422


class TestDirectoryAndNobodyToTell:
    async def test_the_directory_is_read_sorted(self, registry):
        """A union refuses an unsorted page (TooManyRows); the read must sort."""
        from app import notes
        from app.core.query import ListQuery

        seen: list[ListQuery] = []
        await registry.bind()
        provider = registry.resource("staff").provider
        original = provider.list

        async def spy(q, ctx):
            seen.append(q)
            return await original(q, ctx)

        provider.list = spy
        notes._directory_cache = None
        try:
            await notes.staff_directory(registry, Ctx.system())
            assert seen and seen[0].sort, "the directory read must carry a sort"
        finally:
            notes._directory_cache = None
            await registry.close()

    async def test_an_empty_directory_does_not_mark_the_task_announced(self, store, sent):
        registry = Registry()
        tasks = tasks_resource(store)
        tasks.fields.append(TextField("source_key", in_form=False))
        tasks.provider_wrap = lambda p: task_service.AnnouncingProvider(p, registry)
        registry.add_resource(tasks)
        registry.add_resource(Resource(
            "notifications", provider=sent, audited=False, timeline=False,
            fields=[TextField("id", in_form=False), TextField("recipient"), TextField("title")],
        ))
        await registry.bind()
        notifier.bind(registry.resource("notifications").provider)
        notifier.use([])
        notifier.background = False
        try:
            await task_events.apply(registry, event(), SERVICE)
            (task,) = rows_of(store)
            assert not task.get("notified_assignee")
            assert rows_of(sent) == []
        finally:
            notifier.provider = None
            await registry.close()
