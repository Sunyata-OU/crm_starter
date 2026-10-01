"""Notes on a record.

The design points these pin down: a note is written against a record the
application may only *read*, reading one is governed by the record rather than
by the note, and the people told about it are derived from who has already
spoken there -- there is no follower table to keep in step.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from app import notes
from app.core.registry import StaffDirectory
from app.core.results import Identity
from app.fields.types import BooleanField, DateTimeField, TextAreaField, TextField
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from app.settings import Settings
from app.storage import MemoryFileStore, UploadPolicy
from app.storage import store as file_stores
from tests.conftest import build_registry, csrf_from, sign_in

KIM = Identity(subject="2", email="kim@example.com", display_name="Kim")


def notes_resource(store: MemoryProvider) -> Resource:
    return Resource(
        "notes",
        provider=store,
        fields=[
            TextField("id", in_form=False),
            TextField("resource"),
            TextField("record_id"),
            TextField("kind"),
            TextAreaField("body"),
            TextField("author"),
            TextField("author_id"),
            TextField("mentions"),
            TextField("attachments"),
            DateTimeField("created_at"),
            DateTimeField("edited_at"),
            BooleanField("pinned"),
        ],
        audited=False,
        timeline=False,
    )


def notifications_resource(store: MemoryProvider) -> Resource:
    return Resource(
        "notifications",
        provider=store,
        fields=[
            TextField("id", in_form=False),
            TextField("recipient"),
            TextField("title"),
            TextAreaField("body"),
            TextField("kind"),
            TextField("priority"),
            TextField("resource"),
            TextField("record_id"),
            TextField("url"),
            TextField("actor"),
            TextField("channels"),
            DateTimeField("created_at"),
            DateTimeField("due_at"),
            DateTimeField("read_at"),
            DateTimeField("sent_at"),
            TextField("delivery"),
        ],
        audited=False,
        timeline=False,
    )


def keycloak_users_resource(store: MemoryProvider) -> Resource:
    """A stand-in for the resource a deployment names as its staff directory.

    `notes.staff_directory` only reads the declared resource -- it does not
    care that a real one might be a union over an identity provider -- so an
    ordinary in-memory resource exercises the restriction.
    """
    return Resource(
        "keycloak_users",
        provider=store,
        fields=[
            TextField("id", in_form=False),
            TextField("username"),
            TextField("email"),
            TextField("firstName"),
            TextField("lastName"),
        ],
        audited=False,
        timeline=False,
    )


@pytest.fixture
def note_store() -> MemoryProvider:
    """The raw provider, so a test can read what was actually written.

    Held separately because the registry wraps every provider in the
    capability shim; reaching through the wrapper for its rows would be a test
    asserting on plumbing.
    """
    return MemoryProvider([], searchable_fields=("body",))


@pytest.fixture
def sent() -> MemoryProvider:
    return MemoryProvider([], searchable_fields=("title",))


@pytest.fixture
def registry(note_store, sent):
    registry = build_registry()
    registry.add_resource(notes_resource(note_store))
    registry.add_resource(notifications_resource(sent))
    return registry


@pytest.fixture(autouse=True)
def _reset_directory_cache():
    """`notes.staff_directory`'s TTL cache is module-level state.

    Each test builds its own directory provider, so a directory cached
    by an earlier test in the same process must not answer a later one --
    that would be this suite failing for the same reason a stale cache would
    fail in production, just faster.
    """
    notes._directory_cache = None
    yield
    notes._directory_cache = None


@pytest.fixture
def directory() -> MemoryProvider:
    """The raw directory provider, so a test can name who is staff."""
    return MemoryProvider([], searchable_fields=("email", "username"))


@pytest.fixture
def directory_registry(note_store, sent, directory):
    registry = build_registry()
    registry.add_resource(notes_resource(note_store))
    registry.add_resource(notifications_resource(sent))
    registry.add_resource(keycloak_users_resource(directory))
    registry.staff_directory = StaffDirectory("keycloak_users", roles=("admin",))
    return registry


@pytest.fixture
def directory_client(settings, directory_registry):
    app = create_app(settings=settings, registry=directory_registry)
    with TestClient(app, raise_server_exceptions=False) as c:
        sign_in(c, email="kim@example.com", roles=["admin"])
        yield c


def rows_of(store: MemoryProvider) -> list[dict]:
    return list(store._rows.values())


@pytest.fixture
def settings():
    return Settings(
        environment="test",
        secret_key="test-key-not-for-real-use",
        debug=False,
        template_reload=False,
        csrf_enabled=True,
        auth_providers=["session", "local"],
        modules=[],
        # Delivered inline, so a test can assert on what was stored without
        # racing a background task.
        notify_delivery="inline",
        notify_channels=[],
    )


@pytest.fixture
def client(settings, registry):
    app = create_app(settings=settings, registry=registry)
    with TestClient(app, raise_server_exceptions=False) as c:
        sign_in(c, email="kim@example.com", roles=["user"])
        yield c


def post_note(client: TestClient, body: str, *, resource="contacts", pk="1", **extra):
    token = csrf_from(client, f"/r/{resource}/{pk}")
    return client.post(
        f"/r/{resource}/{pk}/notes",
        data={"csrf_token": token, "body": body, **extra},
    )


class TestWritingOne:
    def test_a_note_appears_in_the_panel(self, client):
        response = post_note(client, "Called them, no answer.")
        assert response.status_code == 200
        assert "Called them, no answer." in response.text

    def test_it_is_written_against_a_record_this_application_only_reads(self, client, note_store):
        # `contacts` is not the notes table: the note addresses it by name, so
        # nothing is written to the record itself.
        post_note(client, "Ring again on Monday.")
        stored = rows_of(note_store)[0]
        assert stored["resource"] == "contacts"
        assert stored["record_id"] == "1"
        assert stored["author_id"] == "kim@example.com"

    def test_an_empty_note_is_refused(self, client):
        response = post_note(client, "   ")
        assert response.status_code == 422
        assert "Write something first." in response.text

    def test_a_note_longer_than_the_limit_is_refused(self, client):
        response = post_note(client, "x" * (notes.MAX_BODY + 1))
        assert response.status_code == 422
        assert "characters" in response.text

    def test_a_record_nobody_may_read_has_no_notes_either(self, settings, registry):
        # `deals` are owner-scoped; lee owns none of them.
        app = create_app(settings=settings, registry=registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            sign_in(client, email="lee@example.com", roles=["user"])
            response = client.post(
                "/r/deals/1/notes",
                data={"body": "should not land"},
                headers={"X-CSRF-Token": client.app.state.crm.csrf.issue("")},
            )
        assert response.status_code in (403, 404)


class TestWhoHearsAboutIt:
    def test_a_mentioned_address_is_notified(self, client, sent):
        post_note(client, "Please look at this @sam@example.com")
        notified = rows_of(sent)
        assert [n["recipient"] for n in notified] == ["sam@example.com"]
        assert notified[0]["kind"] == "mentioned"

    def test_people_who_already_wrote_here_are_told(self, note_store, sent, client):
        # Sam wrote first; Kim answers; Sam hears about it, once.
        note_store._rows[99] = {
            "id": 99, "resource": "contacts", "record_id": "1", "kind": "note",
            "body": "Chased them.", "author": "Sam", "author_id": "sam@example.com",
        }
        post_note(client, "Thanks, I will call.")
        notified = rows_of(sent)
        assert [n["recipient"] for n in notified] == ["sam@example.com"]
        assert notified[0]["kind"] == "info"

    def test_nobody_is_told_about_their_own_note(self, note_store, sent, client):
        note_store._rows[99] = {
            "id": 99, "resource": "contacts", "record_id": "1", "kind": "note",
            "body": "Mine.", "author": "Kim", "author_id": "kim@example.com",
        }
        post_note(client, "Still mine.")
        assert not rows_of(sent)

    def test_a_handle_that_matches_nobody_is_dropped(self, client, sent):
        post_note(client, "@nobody should this reach")
        assert not rows_of(sent)

    def test_a_bare_handle_matches_somebody_already_on_the_record(self, note_store, sent, client):
        note_store._rows[99] = {
            "id": 99, "resource": "contacts", "record_id": "1", "kind": "note",
            "body": "Chased them.", "author": "Sam", "author_id": "sam@example.com",
        }
        post_note(client, "@sam can you confirm?")
        notified = rows_of(sent)
        assert [n["recipient"] for n in notified] == ["sam@example.com"]
        # Mentioned, not merely informed: the mention wins where both apply.
        assert notified[0]["kind"] == "mentioned"

    def test_what_was_named_is_stored_on_the_note(self, client, note_store):
        post_note(client, "cc @sam@example.com")
        stored = rows_of(note_store)[0]
        assert json.loads(stored["mentions"]) == ["sam@example.com"]


class TestAttachments:
    """A file dropped on a record is a note carrying a file.

    Stored through the same file store as everything else, which in the cluster
    is S3 -- so this is exercised against the memory backend and the difference
    is one line of configuration.
    """

    @pytest.fixture
    def files(self):
        store = MemoryFileStore()
        file_stores.clear()
        file_stores.bind("files", store)
        file_stores.default_name = "files"
        yield store
        file_stores.clear()

    def test_a_file_is_stored_and_linked(self, client, files, note_store):
        token = csrf_from(client, "/r/contacts/1")
        response = client.post(
            "/r/contacts/1/notes",
            data={"csrf_token": token, "body": "Signed contract attached."},
            files={"files": ("contract.pdf", b"%PDF-1.4 ...", "application/pdf")},
        )
        assert response.status_code == 200
        assert "contract.pdf" in response.text
        assert files.files, "the bytes should be in the store"

        stored = json.loads(rows_of(note_store)[0]["attachments"])
        assert stored[0]["filename"] == "contract.pdf"
        # The store travels with the file: this deployment reads from two of
        # them, and a link has to say which.
        assert stored[0]["store"] == "files"

    def test_a_file_needs_no_covering_sentence(self, client, files, note_store):
        token = csrf_from(client, "/r/contacts/1")
        response = client.post(
            "/r/contacts/1/notes",
            data={"csrf_token": token, "body": ""},
            files={"files": ("scan.pdf", b"%PDF-1.4", "application/pdf")},
        )
        assert response.status_code == 200
        assert rows_of(note_store)

    def test_a_refused_file_leaves_no_note(self, client, note_store):
        store = MemoryFileStore(policy=UploadPolicy(max_bytes=4))
        file_stores.clear()
        file_stores.bind("files", store)
        file_stores.default_name = "files"
        try:
            token = csrf_from(client, "/r/contacts/1")
            response = client.post(
                "/r/contacts/1/notes",
                data={"csrf_token": token, "body": "Too big."},
                files={"files": ("big.pdf", b"x" * 100, "application/pdf")},
            )
        finally:
            file_stores.clear()
        assert response.status_code == 422
        assert not rows_of(note_store), "the note should not outlive its attachment"

    def test_removing_a_note_takes_its_files_with_it(self, client, files, note_store):
        token = csrf_from(client, "/r/contacts/1")
        client.post(
            "/r/contacts/1/notes",
            data={"csrf_token": token, "body": "Attached."},
            files={"files": ("note.txt", b"hello", "text/plain")},
        )
        note_id = next(iter(note_store._rows))
        client.post(f"/r/contacts/1/notes/{note_id}/delete", data={"csrf_token": token})
        assert not files.files


class TestRemovingAndPinning:
    def _note_id(self, store: MemoryProvider) -> int:
        return next(iter(store._rows))

    def test_a_person_may_remove_their_own(self, client, note_store):
        post_note(client, "Mistyped.")
        note_id = self._note_id(note_store)
        token = csrf_from(client, "/r/contacts/1")
        response = client.post(
            f"/r/contacts/1/notes/{note_id}/delete", data={"csrf_token": token}
        )
        assert response.status_code == 200
        assert not note_store._rows

    def test_somebody_else_may_not(self, note_store, client):
        note_store._rows[99] = {
            "id": 99, "resource": "contacts", "record_id": "1", "kind": "note",
            "body": "Sam wrote this.", "author": "Sam", "author_id": "sam@example.com",
        }
        token = csrf_from(client, "/r/contacts/1")
        response = client.post(
            "/r/contacts/1/notes/99/delete", data={"csrf_token": token}
        )
        assert response.status_code == 403
        assert 99 in note_store._rows

    def test_a_note_on_another_record_cannot_be_reached_through_this_one(
        self, note_store, client
    ):
        note_store._rows[99] = {
            "id": 99, "resource": "contacts", "record_id": "2", "kind": "note",
            "body": "About somebody else.", "author": "Kim", "author_id": "kim@example.com",
        }
        token = csrf_from(client, "/r/contacts/1")
        response = client.post(
            "/r/contacts/1/notes/99/delete", data={"csrf_token": token}
        )
        assert response.status_code == 404
        assert 99 in note_store._rows

    def test_a_pinned_note_is_read_before_the_history(self, note_store, client):
        post_note(client, "Ordinary.")
        note_id = self._note_id(note_store)
        token = csrf_from(client, "/r/contacts/1")
        client.post(f"/r/contacts/1/notes/{note_id}/pin", data={"csrf_token": token})
        post_note(client, "Newer than the pinned one.")
        panel = client.get("/r/contacts/1/timeline").text
        assert panel.index("Ordinary.") < panel.index("Newer than the pinned one.")


class TestWhenThereIsNoDatabase:
    def test_the_panel_is_not_offered(self, settings):
        # A stateless deployment: no db.main, so no panel rather than a
        # panel that cannot answer.
        off = settings.model_copy(update={"timeline": False})
        app = create_app(settings=off, registry=build_registry())
        with TestClient(app, raise_server_exceptions=False) as client:
            sign_in(client, email="kim@example.com", roles=["user"])
            page = client.get("/r/contacts/1").text
            assert "/timeline" not in page

    def test_asking_for_it_anyway_answers_emptily(self, settings):
        off = settings.model_copy(update={"timeline": False})
        app = create_app(settings=off, registry=build_registry())
        with TestClient(app, raise_server_exceptions=False) as client:
            sign_in(client, email="kim@example.com", roles=["user"])
            response = client.get("/r/contacts/1/timeline")
            assert response.status_code == 200
            assert "Add note" not in response.text

    def test_without_a_notes_resource_there_is_no_compose_box(self):
        app = create_app(
            settings=Settings(
                environment="test", secret_key="k", template_reload=False,
                auth_providers=["session"], modules=[],
            ),
            registry=build_registry(),
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            sign_in(client, email="kim@example.com", roles=["user"])
            panel = client.get("/r/contacts/1/timeline").text
            assert "Add note" not in panel


class TestParsing:
    def test_handles_are_found_once_each_in_order(self):
        found = notes.handles_in("@kim and @sam@example.com and @kim again")
        assert found == ["kim", "sam@example.com"]

    def test_a_trailing_full_stop_is_not_part_of_the_handle(self):
        assert notes.handles_in("ask @sam.") == ["sam"]

    def test_an_email_address_in_prose_is_not_a_mention(self):
        assert notes.handles_in("write to sam@example.com") == []

    def test_the_author_identifier_prefers_the_address(self):
        assert notes.author_id(KIM) == "kim@example.com"
        assert notes.author_id(Identity(subject="abc")) == "abc"


class TestMentionsAreRestrictedToTheDirectory:
    """Where a staff directory exists, a mention may only reach somebody in it.

    Before this, ``@anything@anywhere`` in a note body was taken at face
    value -- a note is an internal conversation, and there was no way to stop
    it notifying an address that has never touched the back office.
    """

    def test_a_free_address_outside_the_directory_is_dropped(self, directory_client, sent):
        # Sam is nobody's colleague as far as the directory knows.
        post_note(directory_client, "loop in @sam@example.com")
        assert not rows_of(sent)

    def test_a_directory_member_addressed_directly_is_notified(self, directory, directory_client, sent):
        directory._rows[1] = {"id": 1, "username": "sam", "email": "sam@example.com"}
        post_note(directory_client, "please check this @sam@example.com")
        assert [n["recipient"] for n in rows_of(sent)] == ["sam@example.com"]

    def test_a_bare_handle_matches_the_directory_by_username(self, directory, directory_client, sent):
        directory._rows[1] = {"id": 1, "username": "sam", "email": "sam@example.com"}
        post_note(directory_client, "@sam can you confirm?")
        assert [n["recipient"] for n in rows_of(sent)] == ["sam@example.com"]

    def test_a_participant_not_in_the_directory_is_not_a_mention_target(
        self, note_store, directory_client, sent
    ):
        # Somebody who commented earlier is normally enough to make "@handle"
        # resolve to them -- but not here, because they are not staff. They
        # still get the ordinary "somebody commented" notice every participant
        # gets regardless of @mentions; what must not happen is the mention
        # itself resolving to them, which is a different, addressed message
        # (see notes.notifications_for -- "mentioned" wins over "info" where
        # both apply, so a still-"info" row proves the mention did not match).
        note_store._rows[99] = {
            "id": 99, "resource": "contacts", "record_id": "1", "kind": "note",
            "body": "Chased them.", "author": "Sam", "author_id": "sam@example.com",
        }
        post_note(directory_client, "@sam can you confirm?")
        notified = rows_of(sent)
        assert [n["recipient"] for n in notified] == ["sam@example.com"]
        assert notified[0]["kind"] == "info"

    def test_a_participant_who_is_also_staff_still_resolves(
        self, directory, note_store, directory_client, sent
    ):
        directory._rows[1] = {"id": 1, "username": "sam", "email": "sam@example.com"}
        note_store._rows[99] = {
            "id": 99, "resource": "contacts", "record_id": "1", "kind": "note",
            "body": "Chased them.", "author": "Sam", "author_id": "sam@example.com",
        }
        post_note(directory_client, "@sam can you confirm?")
        assert [n["recipient"] for n in rows_of(sent)] == ["sam@example.com"]

    def test_an_unreachable_directory_does_not_fail_the_note_write(
        self, directory_registry, settings, sent, note_store
    ):
        # A Keycloak that is down is a reason to lose a mention, never a
        # reason to lose what somebody just typed.
        app = create_app(settings=settings, registry=directory_registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            sign_in(client, email="kim@example.com", roles=["admin"])

            # The provider only exists once the registry has bound, which
            # happens on the app's startup -- i.e. now that the client above
            # has entered its context.
            async def broken_list(*args, **kwargs):
                raise RuntimeError("Keycloak is unreachable")

            directory_registry.resource("keycloak_users").provider.list = broken_list

            response = post_note(client, "cc @sam@example.com")
        assert response.status_code == 200
        assert rows_of(note_store), "the note itself must still be saved"
        assert not rows_of(sent), "a directory that could not be read resolves nothing"

    def test_the_directory_is_read_once_and_then_cached(self, directory, directory_client):
        calls = []
        original = directory.list

        async def counting_list(*args, **kwargs):
            calls.append(1)
            return await original(*args, **kwargs)

        directory.list = counting_list
        post_note(directory_client, "first @sam@example.com")
        post_note(directory_client, "second @sam@example.com")
        assert len(calls) == 1


class TestMentionAutocomplete:
    """The ``@`` suggestion route, gated the same as the directory itself."""

    def test_staff_can_search_the_directory(self, directory, directory_client):
        directory._rows[1] = {
            "id": 1, "username": "sam", "email": "sam@example.com",
            "firstName": "Sam", "lastName": "Okafor",
        }
        response = directory_client.get("/r/contacts/1/notes/mentions?q=sam")
        assert response.status_code == 200
        options = response.json()["options"]
        assert options == [{"value": "sam@example.com", "label": "Sam Okafor (sam@example.com)"}]

    def test_a_caller_outside_the_directorys_roles_gets_nothing(self, directory, directory_registry, settings):
        directory._rows[1] = {"id": 1, "username": "sam", "email": "sam@example.com"}
        app = create_app(settings=settings, registry=directory_registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            sign_in(client, email="kim@example.com", roles=["user"])
            response = client.get("/r/contacts/1/notes/mentions?q=sam")
        assert response.status_code == 200
        assert response.json()["options"] == []

    def test_a_record_nobody_may_read_offers_no_suggestions_either(
        self, directory_registry, settings
    ):
        app = create_app(settings=settings, registry=directory_registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            sign_in(client, email="lee@example.com", roles=["user"])
            response = client.get("/r/deals/1/notes/mentions?q=sam")
        assert response.status_code in (403, 404)


class TestWhoMayBrowseTheDirectory:
    def _registry(self, **kwargs):
        registry = build_registry()
        registry.staff_directory = StaffDirectory("people", **kwargs)
        return registry

    def test_a_directory_naming_roles_admits_only_those_roles(self):
        registry = self._registry(roles=("staff",))
        staff = Identity(subject="s", email="s@x.test", roles=frozenset({"staff"}))
        outsider = Identity(subject="o", email="o@x.test", roles=frozenset({"user"}))
        assert notes.may_browse_directory(staff, registry)
        assert not notes.may_browse_directory(outsider, registry)

    def test_a_directory_naming_no_roles_admits_anyone_signed_in(self):
        registry = self._registry()
        anyone = Identity(subject="o", email="o@x.test", roles=frozenset({"user"}))
        assert notes.may_browse_directory(anyone, registry)
        assert not notes.may_browse_directory(Identity(subject="", is_authenticated=False), registry)

    def test_with_no_directory_declared_anyone_signed_in_may(self):
        anyone = Identity(subject="o", email="o@x.test", roles=frozenset({"user"}))
        assert notes.may_browse_directory(anyone, build_registry())


class TestADirectoryWithItsOwnFieldNames:
    async def test_the_declared_fields_are_read(self):
        notes._directory_cache = None
        store = MemoryProvider(
            [{"id": 1, "mail": "kim@example.com", "login": "kim", "first": "Kim", "last": "Lee"}]
        )
        registry = build_registry()
        registry.add_resource(
            Resource(
                "people",
                provider=store,
                fields=[
                    TextField("id", in_form=False),
                    TextField("mail"),
                    TextField("login"),
                    TextField("first"),
                    TextField("last"),
                ],
                audited=False,
                timeline=False,
            )
        )
        registry.staff_directory = StaffDirectory(
            "people",
            email_field="mail",
            username_field="login",
            name_fields=("first", "last"),
            sort_field="login",
        )
        await registry.bind()
        try:
            people = await notes.staff_directory(registry, None)
        finally:
            notes._directory_cache = None
        assert people == [{"email": "kim@example.com", "username": "kim", "label": "Kim Lee"}]
