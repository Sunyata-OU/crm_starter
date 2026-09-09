"""File uploads through the web layer.

Storing a file is tested in ``test_storage.py``; this is about the pipeline
around it -- the column recording a reference rather than bytes, a replaced
file being cleaned up, and the download being as protected as the record.
"""

from __future__ import annotations

import io

import pytest
from starlette.testclient import TestClient

from app.core.registry import Registry
from app.fields.types import FileField, ImageField, TextField
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from app.resources.views import Column, FormView, ListView, Section
from app.settings import Settings
from app.storage import MemoryFileStore
from app.storage import store as file_stores
from app.web.uploads import describe

from .conftest import csrf_from, sign_in

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100fdff03fd0000000049454e44ae426082"
)


@pytest.fixture
def files() -> MemoryFileStore:
    store = MemoryFileStore()
    file_stores.clear()
    file_stores.bind("files", store)
    file_stores.default_name = "files"
    yield store
    file_stores.clear()


@pytest.fixture
def client(files):
    registry = Registry()
    registry.add_resource(
        Resource(
            "documents",
            provider=MemoryProvider(
                [{"id": 1, "name": "Existing", "attachment": None, "photo": None}]
            ),
            audited=False,
            fields=[
                TextField("id", in_form=False),
                TextField("name", required=True, searchable=True),
                FileField("attachment"),
                ImageField("photo"),
            ],
            views=[
                ListView(columns=[Column("name", link=True), "attachment"]),
                FormView([Section("Document", ["name", "attachment", "photo"], columns=1)]),
            ],
        )
    )
    settings = Settings(secret_key="k" * 32, environment="test",
                        template_reload=False, auth_providers=["session"], modules=[])
    with TestClient(create_app(settings=settings, registry=registry),
                    raise_server_exceptions=False) as c:
        sign_in(c, email="admin@x.test", roles=["admin"])
        yield c


def upload(client, pk="1", **files_to_send):
    token = csrf_from(client, f"/r/documents/{pk}/edit")
    return client.post(
        f"/r/documents/{pk}",
        data={"csrf_token": token, "name": "Existing"},
        files=files_to_send,
        follow_redirects=False,
    )


class TestDescribe:
    def test_json_is_read_into_its_parts(self):
        info = describe('{"key": "a/b.pdf", "filename": "b.pdf", "size": 10}')
        assert info["key"] == "a/b.pdf" and info["size"] == 10

    def test_a_bare_path_still_yields_a_key(self):
        # A column written before this feature, or by another system.
        assert describe("legacy/path.pdf")["key"] == "legacy/path.pdf"

    def test_nothing_yields_nothing(self):
        assert describe(None) == {} and describe("") == {}


class TestUploading:
    def test_a_file_is_stored_and_referenced(self, client, files):
        response = upload(client, attachment=("report.pdf", io.BytesIO(b"pdf bytes"), "application/pdf"))
        assert response.status_code == 303
        assert len(files.files) == 1

        record = client.get("/r/documents/1", headers={"Accept": "application/json"}).json()
        info = describe(record["attachment"])
        assert info["filename"] == "report.pdf"
        assert info["size"] == len(b"pdf bytes")

    def test_the_column_holds_a_reference_not_the_bytes(self, client):
        upload(client, attachment=("report.pdf", io.BytesIO(b"pdf bytes"), "application/pdf"))
        record = client.get("/r/documents/1", headers={"Accept": "application/json"}).json()
        assert "pdf bytes" not in record["attachment"]

    def test_a_form_with_no_file_leaves_the_column_alone(self, client, files):
        upload(client, attachment=("report.pdf", io.BytesIO(b"first"), "application/pdf"))
        before = client.get("/r/documents/1", headers={"Accept": "application/json"}).json()

        token = csrf_from(client, "/r/documents/1/edit")
        client.post("/r/documents/1", data={"csrf_token": token, "name": "Renamed"})
        after = client.get("/r/documents/1", headers={"Accept": "application/json"}).json()
        assert after["attachment"] == before["attachment"]
        assert after["name"] == "Renamed"

    def test_replacing_a_file_removes_the_old_one(self, client, files):
        upload(client, attachment=("first.pdf", io.BytesIO(b"first"), "application/pdf"))
        first_key = next(iter(files.files))

        upload(client, attachment=("second.pdf", io.BytesIO(b"second"), "application/pdf"))
        assert first_key not in files.files, "the superseded file should be gone"
        assert len(files.files) == 1

    def test_a_rejected_form_stores_nothing(self, client, files):
        # Validation runs before the file is stored, so a bad form costs no
        # storage at all.
        token = csrf_from(client, "/r/documents/1/edit")
        response = client.post(
            "/r/documents/1",
            data={"csrf_token": token, "name": ""},
            files={"attachment": ("report.pdf", io.BytesIO(b"x"), "application/pdf")},
        )
        assert response.status_code == 422
        assert files.files == {}

    def test_an_image_field_refuses_a_document(self, client, files):
        response = upload(client, photo=("report.pdf", io.BytesIO(b"x"), "application/pdf"))
        assert response.status_code == 415

    def test_an_image_field_accepts_an_image(self, client, files):
        response = upload(client, photo=("photo.png", io.BytesIO(PNG), "image/png"))
        assert response.status_code == 303
        assert len(files.files) == 1

    def test_a_dangerous_extension_is_refused(self, client, files):
        response = upload(client, attachment=("payload.exe", io.BytesIO(b"MZ"), "application/octet-stream"))
        assert response.status_code == 415
        assert files.files == {}


class TestServing:
    def test_a_stored_file_can_be_downloaded(self, client, files):
        upload(client, attachment=("report.pdf", io.BytesIO(b"pdf bytes"), "application/pdf"))
        key = next(iter(files.files))
        response = client.get(f"/files/{key}")
        assert response.status_code == 200
        assert response.content == b"pdf bytes"

    def test_a_missing_file_is_a_404(self, client):
        # Not a 200 with an empty body: the stream is only built when the file
        # is known to exist, so the status can still be corrected.
        assert client.get("/files/nothing/here.pdf").status_code == 404

    def test_downloads_require_a_signed_in_caller(self, client, files):
        upload(client, attachment=("report.pdf", io.BytesIO(b"secret"), "application/pdf"))
        key = next(iter(files.files))
        client.cookies.clear()
        response = client.get(f"/files/{key}", follow_redirects=False)
        assert response.status_code in (303, 401), "an attachment is as private as its record"

    def test_a_document_is_sent_as_a_download(self, client, files):
        upload(client, attachment=("report.zip", io.BytesIO(b"PK"), "application/zip"))
        key = next(iter(files.files))
        response = client.get(f"/files/{key}")
        assert "attachment" in response.headers["content-disposition"]

    def test_an_image_is_shown_inline(self, client, files):
        upload(client, photo=("photo.png", io.BytesIO(PNG), "image/png"))
        key = next(iter(files.files))
        response = client.get(f"/files/{key}")
        assert "inline" in response.headers["content-disposition"]

    def test_the_response_forbids_type_sniffing(self, client, files):
        upload(client, attachment=("report.pdf", io.BytesIO(b"x"), "application/pdf"))
        key = next(iter(files.files))
        assert client.get(f"/files/{key}").headers["x-content-type-options"] == "nosniff"

    def test_the_cache_is_private(self, client, files):
        # A shared cache must not serve one person's attachment to another.
        upload(client, attachment=("report.pdf", io.BytesIO(b"x"), "application/pdf"))
        key = next(iter(files.files))
        assert "private" in client.get(f"/files/{key}").headers["cache-control"]


class TestAFieldMayNameItsOwnStore:
    """A column populated by another system names the store that system's
    files are in -- that system's own bucket, attached read-only, holding a
    key like ``picture_profile_305.jpg``. Rendered against the default store
    it 404s, which is what a profile photograph does.
    """

    @pytest.fixture
    def stores(self):
        default, elsewhere = MemoryFileStore(), MemoryFileStore()
        elsewhere.files["picture_profile_305.jpg"] = PNG
        file_stores.clear()
        file_stores.bind("files", default)
        file_stores.bind("files.other", elsewhere)
        file_stores.default_name = "files"
        yield elsewhere
        file_stores.clear()

    @pytest.fixture
    def client(self, stores):
        registry = Registry()
        registry.add_resource(
            Resource(
                "people",
                provider=MemoryProvider(
                    [{"id": 1, "picture": "picture_profile_305.jpg"}]
                ),
                audited=False,
                fields=[
                    TextField("id", in_form=False),
                    ImageField("picture", readonly=True, store="files.other"),
                ],
                views=[ListView(columns=[Column("id", link=True), "picture"])],
            )
        )
        settings = Settings(secret_key="k" * 32, environment="test",
                            template_reload=False, auth_providers=["session"],
                            modules=[])
        with TestClient(create_app(settings=settings, registry=registry),
                        raise_server_exceptions=False) as c:
            sign_in(c, email="admin@x.test", roles=["admin"])
            yield c

    def test_the_image_points_at_the_store_that_holds_it(self, client):
        page = client.get("/r/people/1").text
        assert "/files/picture_profile_305.jpg?store=files.other" in page

    def test_and_that_url_serves_the_bytes(self, client):
        response = client.get("/files/picture_profile_305.jpg?store=files.other")
        assert response.status_code == 200 and response.content == PNG

    def test_the_default_store_does_not_have_it(self, client):
        """Which is the failure this replaces: the key was right and the store
        was the wrong one."""
        assert client.get("/files/picture_profile_305.jpg").status_code == 404
