"""Helpdesk: pure ticket logic, the sweep, and the messages/notes separation.

Mirrors `tests/test_tasks.py` for the sweep half -- same design point, same
reason: nothing notifies at the moment a ticket changes hands or opens
unassigned. The sweep compares columns, so whatever changed the ticket, and
whether the process that changed it survived, the sweep says it exactly once.

The pure-logic half exists because `app.helpdesk` is the seam the ingest
branch plugs into: these tests are the contract that branch can rely on
without a mail server.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import helpdesk
from app.core.clock import utcnow
from app.core.registry import Registry
from app.core.results import Ctx, Identity
from app.fields.types import DateTimeField, StatusField, TextAreaField, TextField
from app.notify import notifier
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource

KIM = "kim@example.com"
SAM = "sam@example.com"
REQUESTER = "customer@example.com"


# -- pure logic ---------------------------------------------------------


class TestReference:
    def test_a_reference_is_built_from_the_primary_key(self):
        assert helpdesk.reference_for(1042) == "T-1042"

    def test_a_custom_prefix_is_honoured(self):
        assert helpdesk.reference_for(7, prefix="CASE-") == "CASE-7"


class TestSubjectTag:
    def test_a_tag_is_found_anywhere_in_the_subject(self):
        assert helpdesk.match_reference("Re: [T-1042] Refund for order 88") == "T-1042"

    def test_a_subject_with_no_tag_matches_nothing(self):
        assert helpdesk.match_reference("Refund for order 88") is None

    def test_tagging_an_untagged_subject_prepends_it(self):
        assert helpdesk.tag_subject("Refund for order 88", "T-1042") == \
            "[T-1042] Refund for order 88"

    def test_tagging_is_idempotent(self):
        # A reply to a reply already carries last time's tag; a second one
        # would make the next reply's match_reference ambiguous.
        once = helpdesk.tag_subject("Refund for order 88", "T-1042")
        twice = helpdesk.tag_subject(once, "T-1042")
        assert once == twice

    def test_an_empty_subject_becomes_just_the_tag(self):
        assert helpdesk.tag_subject("", "T-1042") == "[T-1042]"


class TestThreadIds:
    def test_references_are_read_oldest_first(self):
        ids = helpdesk.thread_ids("", "<a@x> <b@x> <c@x>")
        assert ids == ["<a@x>", "<b@x>", "<c@x>"]

    def test_in_reply_to_is_appended_and_deduplicated(self):
        ids = helpdesk.thread_ids("<c@x>", "<a@x> <c@x>")
        assert ids == ["<a@x>", "<c@x>"]

    def test_nothing_present_yields_nothing_to_check(self):
        assert helpdesk.thread_ids("", "") == []


class TestDuplicate:
    def test_a_known_message_id_is_a_duplicate(self):
        assert helpdesk.is_duplicate("<a@x>", {"<a@x>", "<b@x>"})

    def test_an_unknown_one_is_not(self):
        assert not helpdesk.is_duplicate("<z@x>", {"<a@x>"})

    def test_an_unidentified_message_is_never_called_a_duplicate(self):
        # It is the ingest branch's job to decide what an unidentified
        # message is; silently dropping it here would not be this module's
        # decision to make.
        assert not helpdesk.is_duplicate("", {"<a@x>"})


class TestReplyHeaders:
    def test_the_first_message_threads_onto_nothing(self):
        headers = helpdesk.reply_headers(None)
        assert headers["in_reply_to"] == ""
        assert headers["references"] == ""
        assert headers["message_id"]

    def test_a_reply_threads_onto_the_last_message(self):
        last = {"message_id": "<in1@customer>", "references": ""}
        headers = helpdesk.reply_headers(last)
        assert headers["in_reply_to"] == "<in1@customer>"
        assert headers["references"] == "<in1@customer>"

    def test_the_chain_grows_rather_than_resets(self):
        last = {"message_id": "<out1@helpdesk.local>", "references": "<in1@customer>"}
        headers = helpdesk.reply_headers(last)
        assert headers["references"] == "<in1@customer> <out1@helpdesk.local>"

    def test_message_ids_are_never_repeated(self):
        first = helpdesk.reply_headers(None)
        second = helpdesk.reply_headers(None)
        assert first["message_id"] != second["message_id"]

    def test_the_domain_is_used_in_the_generated_id(self):
        headers = helpdesk.reply_headers(None, domain="support.example.com")
        assert headers["message_id"].endswith("@support.example.com>")


class TestThreadAnchor:
    def test_a_new_message_becomes_the_anchor(self):
        assert helpdesk.next_thread_anchor("<old@x>", "<new@x>") == "<new@x>"

    def test_no_new_message_leaves_the_anchor_alone(self):
        assert helpdesk.next_thread_anchor("<old@x>", None) == "<old@x>"


class TestNewTicketFields:
    def test_an_inbound_email_opens_a_ticket_with_source_email(self):
        fields = helpdesk.new_ticket_fields(
            subject="Help", requester_name="Robin", requester_email="robin@example.com",
        )
        assert fields["source"] == "email"
        assert fields["state"] == "new"
        assert "reference" not in fields  # left for SequencingProvider to fill in

    def test_new_message_fields_carry_the_direction(self):
        fields = helpdesk.new_message_fields(
            ticket_id=1, direction=helpdesk.IN, author="robin@example.com", body="Help please",
        )
        assert fields["direction"] == "in"
        assert fields["ticket_id"] == 1


class TestUnassigned:
    def test_only_open_tickets_with_no_assignee_are_counted(self):
        tickets = [
            {"state": "new", "assignee": None},
            {"state": "open", "assignee": "kim@example.com"},
            {"state": "closed", "assignee": None},
        ]
        assert helpdesk.unassigned(tickets) == [{"state": "new", "assignee": None}]


# -- the messages/notes separation ---------------------------------------


class TestMessagesCannotBePrivate:
    """A row in `ticket_messages` is, by construction, customer-visible.

    Pins down the design decision the module docstring makes: there is no
    field on this resource that could be mistaken for an "internal" flag, so
    there is nothing for a form or a future contributor to get wrong in the
    direction of mailing a private remark to a customer. A private remark has
    exactly one place to go: the notes panel, which this table has nothing to
    do with.
    """

    def test_the_resource_declares_no_visibility_flag(self):
        from modules.core_helpdesk import _ticket_messages

        names = {f.name for f in _ticket_messages().fields}
        # Every column earns its place by being part of what was sent or
        # received; none of them could be repurposed as "internal".
        suspicious = {n for n in names if "internal" in n or "private" in n
                      or "visib" in n or "public" in n}
        assert not suspicious

    def test_the_resource_carries_its_own_activity_panel_off(self):
        # A message row is a record of what was sent, not a thing to
        # separately discuss -- the ticket it belongs to already has that.
        from modules.core_helpdesk import _ticket_messages

        assert _ticket_messages().timeline is False


# -- the sweep ------------------------------------------------------------


def tickets_resource(store: MemoryProvider) -> Resource:
    return Resource(
        "tickets",
        provider=store,
        stamp={"created_by": "id", "created_by_name": "label"},
        audited=False,
        timeline=False,
        fields=[
            TextField("id", in_form=False),
            TextField("reference", in_form=False),
            TextField("subject", required=True),
            TextField("requester_name"),
            TextField("requester_email"),
            StatusField("state", choices=[("new", "New", "blue"), ("open", "Open", "amber"),
                                          ("closed", "Closed", "grey")], default="new"),
            TextField("priority"),
            TextField("assignee"),
            TextField("assignee_name"),
            TextField("source", in_form=False),
            TextField("thread_message_id", in_form=False),
            TextField("created_by", in_form=False),
            TextField("created_by_name", in_form=False),
            TextField("notified_assignee", in_form=False),
            DateTimeField("reminded_at", in_form=False),
            DateTimeField("created_at", in_form=False),
        ],
    )


def notifications_resource(store: MemoryProvider) -> Resource:
    return Resource(
        "notifications",
        provider=store,
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


@pytest.fixture
def store() -> MemoryProvider:
    return MemoryProvider([], searchable_fields=("subject",))


@pytest.fixture
def sent() -> MemoryProvider:
    return MemoryProvider([], searchable_fields=("title",))


@pytest.fixture
def registry(store, sent) -> Registry:
    registry = Registry()
    registry.add_resource(tickets_resource(store))
    registry.add_resource(notifications_resource(sent))
    return registry


@pytest.fixture
async def bound(registry, sent):
    await registry.bind()
    notifier.bind(registry.resource("notifications").provider)
    notifier.use([])
    notifier.background = False
    yield registry
    notifier.provider = None
    await registry.close()


def rows_of(store: MemoryProvider) -> list[dict]:
    return list(store._rows.values())


def a_ticket(**overrides) -> dict:
    row = {
        "id": 1, "reference": "T-1", "subject": "Refund", "requester_name": "Robin",
        "requester_email": REQUESTER, "state": "open", "priority": "normal",
        "assignee": SAM, "assignee_name": "Sam", "source": "email",
        "thread_message_id": None, "notified_assignee": None, "reminded_at": None,
        "created_at": utcnow(),
    }
    row.update(overrides)
    return row


class TestHandover:
    async def test_an_assignee_hears_once(self, bound, store, sent):
        store._rows[1] = a_ticket()
        first = await helpdesk.sweep(bound)
        assert first["assigned"] == 1
        assert [n["recipient"] for n in rows_of(sent)] == [SAM]

        again = await helpdesk.sweep(bound)
        assert again["assigned"] == 0
        assert len(rows_of(sent)) == 1

    async def test_a_hand_over_is_announced_again(self, bound, store, sent):
        store._rows[1] = a_ticket(notified_assignee=SAM, assignee=KIM)
        await helpdesk.sweep(bound)
        assert [n["recipient"] for n in rows_of(sent)] == [KIM]

    async def test_a_closed_ticket_is_left_alone(self, bound, store, sent):
        store._rows[1] = a_ticket(state="closed")
        await helpdesk.sweep(bound)
        assert not rows_of(sent)


class TestNewUnassigned:
    async def test_the_watchers_hear_about_it_immediately(self, bound, store, sent):
        store._rows[1] = a_ticket(assignee=None, assignee_name=None)
        counts = await helpdesk.sweep(bound, watchers=[KIM, SAM])
        assert counts["unassigned"] == 2
        assert {n["recipient"] for n in rows_of(sent)} == {KIM, SAM}

    async def test_and_only_once(self, bound, store, sent):
        store._rows[1] = a_ticket(assignee=None, assignee_name=None)
        await helpdesk.sweep(bound, watchers=[KIM])
        await helpdesk.sweep(bound, watchers=[KIM])
        assert len(rows_of(sent)) == 1

    async def test_naming_nobody_tells_nobody(self, bound, store, sent):
        store._rows[1] = a_ticket(assignee=None, assignee_name=None)
        counts = await helpdesk.sweep(bound, watchers=[])
        assert counts["unassigned"] == 0
        assert not rows_of(sent)


class TestAgeing:
    async def test_an_assigned_ticket_past_the_window_is_mentioned(self, bound, store, sent):
        store._rows[1] = a_ticket(
            notified_assignee=SAM, created_at=utcnow() - timedelta(hours=72),
        )
        counts = await helpdesk.sweep(bound, watchers=[KIM], window=timedelta(hours=48))
        # One ticket, but two people hear about it -- the assignee and the
        # watcher -- so the count (of notifications sent, like `unassigned`
        # and `assigned`) is 2.
        assert counts["stale"] == 2
        recipients = {n["recipient"] for n in rows_of(sent)}
        assert recipients == {SAM, KIM}

    async def test_a_fresh_ticket_is_not_mentioned_yet(self, bound, store, sent):
        store._rows[1] = a_ticket(
            notified_assignee=SAM, created_at=utcnow() - timedelta(hours=1),
        )
        counts = await helpdesk.sweep(bound, watchers=[KIM], window=timedelta(hours=48))
        assert counts["stale"] == 0

    async def test_the_warning_is_sent_once(self, bound, store, sent):
        store._rows[1] = a_ticket(
            notified_assignee=SAM, created_at=utcnow() - timedelta(hours=72),
        )
        await helpdesk.sweep(bound, watchers=[], window=timedelta(hours=48))
        await helpdesk.sweep(bound, watchers=[], window=timedelta(hours=48))
        assert len(rows_of(sent)) == 1


class TestNoDatabase:
    async def test_a_deployment_with_no_tickets_resource_sweeps_quietly(self):
        registry = Registry()
        await registry.bind()
        try:
            counts = await helpdesk.sweep(registry)
        finally:
            await registry.close()
        assert counts == {"assigned": 0, "unassigned": 0, "stale": 0}


class TestTheStamp:
    async def test_it_never_overwrites_what_the_caller_supplied(self, registry, store):
        await registry.bind()
        try:
            resource = registry.resource("tickets")
            await resource.provider.create(
                {"subject": "Imported", "requester_email": REQUESTER,
                 "created_by": "somebody@else.test"},
                Ctx(identity=Identity(subject="1", email=KIM, display_name="Kim")),
            )
        finally:
            await registry.close()
        assert rows_of(store)[0]["created_by"] == "somebody@else.test"


class TestTheSequence:
    """`Resource(sequence=...)`, exercised through the real `tickets` resource.

    A generic capability (`app.providers.sequence.SequencingProvider`), but
    the risk it exists to cover is specific to this module: a ticket with no
    reference cannot be quoted in a subject tag, so this pins down that every
    ticket gets one, using the primary key the backend only reveals after the
    insert.
    """

    async def test_a_created_ticket_is_given_a_reference(self, store):
        registry = Registry()
        registry.add_resource(
            Resource(
                "tickets", provider=store, sequence=("reference", "T-"),
                audited=False, timeline=False,
                fields=tickets_resource(store).fields,
            )
        )
        await registry.bind()
        try:
            result = await registry.resource("tickets").provider.create(
                {"subject": "Refund", "requester_email": REQUESTER},
                Ctx(identity=Identity(subject="1", email=KIM, display_name="Kim")),
            )
        finally:
            await registry.close()
        assert result.ok
        assert result.record is not None
        assert result.record["reference"] == f"T-{result.record.pk}"
        assert rows_of(store)[0]["reference"] == f"T-{result.record.pk}"

    async def test_a_caller_supplied_reference_is_kept(self, store):
        registry = Registry()
        registry.add_resource(
            Resource(
                "tickets", provider=store, sequence=("reference", "T-"),
                audited=False, timeline=False,
                fields=tickets_resource(store).fields,
            )
        )
        await registry.bind()
        try:
            result = await registry.resource("tickets").provider.create(
                {"subject": "Imported", "requester_email": REQUESTER, "reference": "T-9999"},
                Ctx(identity=Identity(subject="1", email=KIM, display_name="Kim")),
            )
        finally:
            await registry.close()
        assert result.record["reference"] == "T-9999"


class TestThroughTheScreens:
    """The module's own declaration, pointed at memory rather than at
    db.main -- the actions and the reply flow under test are the shipped
    ones."""

    @pytest.fixture
    def ticket_store(self) -> MemoryProvider:
        return MemoryProvider([], searchable_fields=("subject",))

    @pytest.fixture
    def message_store(self) -> MemoryProvider:
        return MemoryProvider([], searchable_fields=("body",))

    @pytest.fixture
    def client(self, ticket_store, message_store, sent):
        from starlette.testclient import TestClient

        from app.main import create_app
        from app.settings import Settings
        from modules.core_helpdesk import _ticket_messages, _tickets
        from tests.conftest import build_registry as base_registry
        from tests.conftest import sign_in

        registry = base_registry()
        tickets = _tickets()
        tickets.provider_ref = ticket_store
        messages = _ticket_messages()
        messages.provider_ref = message_store
        registry.add_resource(tickets)
        registry.add_resource(messages)
        registry.add_resource(notifications_resource(sent))

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

    def test_raising_one_records_who_raised_it(self, client, ticket_store):
        from tests.conftest import csrf_from

        token = csrf_from(client, "/r/tickets/new")
        response = client.post(
            "/r/tickets",
            data={"csrf_token": token, "subject": "Cannot log in",
                  "requester_name": "Robin", "requester_email": REQUESTER},
            follow_redirects=False,
        )
        assert response.status_code in (200, 303)
        stored = rows_of(ticket_store)[0]
        assert stored["created_by"] == KIM
        assert stored["reference"] == f"T-{stored['id']}"
        # "staff" comes from the column's `server_default`, which a real
        # database applies and MemoryProvider does not emulate; see the
        # schema declaration in app/schema.py and the migration for that half
        # of the guarantee.

    def test_take_puts_a_name_on_it_and_opens_it(self, client, ticket_store):
        from tests.conftest import csrf_from

        ticket_store._rows[1] = a_ticket(assignee=None, assignee_name=None, state="new")
        token = csrf_from(client, "/r/tickets/1")
        client.post("/r/tickets/1/action/take", data={"csrf_token": token})
        assert ticket_store._rows[1]["assignee"] == KIM
        assert ticket_store._rows[1]["state"] == "open"

    def test_resolve_closes_it_out(self, client, ticket_store):
        from tests.conftest import csrf_from

        ticket_store._rows[1] = a_ticket()
        token = csrf_from(client, "/r/tickets/1")
        client.post("/r/tickets/1/action/resolve", data={"csrf_token": token})
        assert ticket_store._rows[1]["state"] == "resolved"

    def test_a_reply_is_recorded_and_sent(self, client, ticket_store, message_store, sent):
        from tests.conftest import csrf_from

        ticket_store._rows[1] = a_ticket(subject="Refund", reference="T-1")
        token = csrf_from(client, "/r/tickets/1")
        client.post(
            "/r/tickets/1/action/reply",
            data={"csrf_token": token, "body": "We have issued the refund."},
        )

        message = rows_of(message_store)[0]
        assert message["direction"] == "out"
        assert message["body"] == "We have issued the refund."
        assert message["message_id"]

        # The ticket's threading anchor moves to the reply just sent.
        assert ticket_store._rows[1]["thread_message_id"] == message["message_id"]

        [notification] = rows_of(sent)
        assert notification["recipient"] == REQUESTER
        assert notification["title"] == "[T-1] Refund"
        assert notification["priority"] == "urgent"
