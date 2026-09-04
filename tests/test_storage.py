"""File storage.

Every backend implements one interface, so most of this is a shared suite run
against each -- the same arrangement as the provider contract. What differs
between local disk and S3 is where the bytes go, not what the application can
ask of them.
"""

from __future__ import annotations

import pytest

from app.core.errors import ConfigError
from app.storage import build_store, registered_stores
from app.storage.base import (
    IMAGES_ONLY,
    FileNotFound,
    FileTooLarge,
    FileTypeNotAllowed,
    StorageError,
    UploadPolicy,
    build_key,
    safe_filename,
)
from app.storage.local import LocalFileStore
from app.storage.memory import MemoryFileStore

CONTENT = b"the quick brown fox jumps over the lazy dog\n" * 20


async def read_all(store, key: str) -> bytes:
    return b"".join([chunk async for chunk in store.open(key)])


@pytest.fixture(params=["memory", "local"])
def store(request, tmp_path):
    """Each backend, behind the same interface."""
    if request.param == "memory":
        return MemoryFileStore()
    return LocalFileStore(tmp_path / "files")


# -- naming and keys -------------------------------------------------------


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("report.pdf", "report.pdf"),
            ("My Report (final).PDF", "My-Report-final.pdf"),
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32", "system32"),
            ("/absolute/path.txt", "path.txt"),
            ("naïve café.txt", "naive-cafe.txt"),
        ],
    )
    def test_names_are_reduced_to_something_safe(self, given, expected):
        assert safe_filename(given) == expected

    def test_an_empty_name_gets_a_fallback(self):
        assert safe_filename("") == "file"
        assert safe_filename("...") == "file"

    def test_a_very_long_name_is_truncated(self):
        assert len(safe_filename("a" * 500 + ".txt")) <= 95


class TestKeys:
    def test_keys_are_unique_for_the_same_name(self):
        assert build_key("report.pdf") != build_key("report.pdf")

    def test_keys_are_organised_by_date(self):
        from datetime import datetime

        assert f"{datetime.now():%Y/%m}" in build_key("report.pdf")

    def test_a_folder_prefixes_the_key(self):
        assert build_key("x.pdf", folder="contacts/attachment").startswith(
            "contacts/attachment/"
        )

    def test_the_original_name_survives_in_the_key(self):
        assert build_key("quarterly-report.pdf").endswith("-quarterly-report.pdf")


# -- the shared contract ---------------------------------------------------


class TestEveryBackend:
    async def test_a_file_round_trips(self, store):
        stored = await store.save(CONTENT, "report.pdf")
        assert await read_all(store, stored.key) == CONTENT

    async def test_the_metadata_describes_the_file(self, store):
        stored = await store.save(CONTENT, "report.pdf", content_type="application/pdf")
        assert stored.size == len(CONTENT)
        assert stored.filename == "report.pdf"
        assert stored.content_type == "application/pdf"
        assert len(stored.checksum) == 64

    async def test_the_content_type_comes_from_the_extension(self, store):
        # A browser-supplied type is a hint; the extension decides, because
        # that is what the eventual download uses.
        stored = await store.save(b"x", "photo.png", content_type="text/plain")
        assert stored.content_type == "image/png"

    async def test_a_stream_can_be_stored(self, store):
        async def chunks():
            for _ in range(10):
                yield b"abcdefghij"

        stored = await store.save(chunks(), "streamed.bin")
        assert stored.size == 100

    async def test_existence_is_reported(self, store):
        stored = await store.save(CONTENT, "report.pdf")
        assert await store.exists(stored.key)
        assert not await store.exists("nothing/here.pdf")

    async def test_a_file_can_be_deleted(self, store):
        stored = await store.save(CONTENT, "report.pdf")
        assert await store.delete(stored.key)
        assert not await store.exists(stored.key)

    async def test_deleting_something_absent_is_not_an_error(self, store):
        assert await store.delete("nothing/here.pdf") is False

    async def test_reading_something_absent_raises(self, store):
        with pytest.raises(FileNotFound):
            await read_all(store, "nothing/here.pdf")

    async def test_two_files_of_the_same_name_do_not_collide(self, store):
        first = await store.save(b"one", "report.pdf")
        second = await store.save(b"two", "report.pdf")
        assert first.key != second.key
        assert await read_all(store, first.key) == b"one"

    async def test_a_url_is_produced(self, store):
        stored = await store.save(CONTENT, "report.pdf")
        assert stored.key in await store.url(stored.key)

    async def test_health_is_reported(self, store):
        healthy, detail = await store.health()
        assert healthy and isinstance(detail, str)


# -- limits ----------------------------------------------------------------


class TestUploadPolicy:
    async def test_a_file_over_the_limit_is_refused(self):
        store = MemoryFileStore(policy=UploadPolicy(max_bytes=100))
        with pytest.raises(FileTooLarge):
            await store.save(b"x" * 200, "big.bin")

    async def test_the_limit_is_enforced_while_reading(self):
        """The point of a size limit is to stop reading, not to discover
        afterwards that too much was read."""
        read = 0

        async def endless():
            nonlocal read
            while True:
                read += 1024
                yield b"x" * 1024

        store = MemoryFileStore(policy=UploadPolicy(max_bytes=4096))
        with pytest.raises(FileTooLarge):
            await store.save(endless(), "endless.bin")
        assert read < 100_000, "the stream should have been abandoned early"

    async def test_an_executable_extension_is_always_refused(self):
        store = MemoryFileStore()
        for name in ("payload.exe", "shell.sh", "page.php", "vector.svg"):
            with pytest.raises(FileTypeNotAllowed):
                await store.save(b"x", name)

    async def test_html_is_refused_because_it_would_run_in_our_origin(self):
        store = MemoryFileStore()
        with pytest.raises(FileTypeNotAllowed):
            await store.save(b"<script>", "page.html")

    async def test_an_allow_list_excludes_everything_else(self):
        store = MemoryFileStore(policy=UploadPolicy(allowed_extensions=frozenset({".pdf"})))
        await store.save(b"x", "fine.pdf")
        with pytest.raises(FileTypeNotAllowed):
            await store.save(b"x", "other.txt")

    async def test_the_image_policy_accepts_images_only(self):
        store = MemoryFileStore(policy=IMAGES_ONLY)
        await store.save(b"x", "photo.png")
        with pytest.raises(FileTypeNotAllowed):
            await store.save(b"x", "document.pdf")

    async def test_an_empty_file_is_refused(self):
        store = MemoryFileStore()
        with pytest.raises(FileTypeNotAllowed):
            await store.save(b"", "empty.txt")


# -- local disk specifics --------------------------------------------------


class TestLocalStore:
    async def test_files_land_under_the_root(self, tmp_path):
        store = LocalFileStore(tmp_path / "files")
        stored = await store.save(CONTENT, "report.pdf")
        assert (tmp_path / "files" / stored.key).is_file()

    @pytest.mark.parametrize(
        "key", ["../escape.txt", "../../etc/passwd", "a/../../../etc/passwd"]
    )
    async def test_a_key_cannot_escape_the_root(self, tmp_path, key):
        store = LocalFileStore(tmp_path / "files")
        with pytest.raises(StorageError, match="outside"):
            store.path_for(key)

    async def test_a_partial_upload_leaves_nothing_behind(self, tmp_path):
        """Written to a temporary name and moved, so a failure never leaves a
        half-written file at the real key."""

        async def failing():
            yield b"some bytes"
            raise RuntimeError("the connection dropped")

        store = LocalFileStore(tmp_path / "files")
        with pytest.raises(RuntimeError):
            await store.save(failing(), "report.pdf")
        assert list((tmp_path / "files").rglob("*.part")) == []
        assert list((tmp_path / "files").rglob("*.pdf")) == []

    async def test_empty_folders_are_tidied_after_a_delete(self, tmp_path):
        store = LocalFileStore(tmp_path / "files")
        stored = await store.save(CONTENT, "report.pdf", folder="contacts")
        await store.delete(stored.key)
        assert not (tmp_path / "files" / "contacts").exists()
        assert (tmp_path / "files").exists(), "the root itself must survive"

    async def test_health_reports_an_unwritable_root(self, tmp_path):
        root = tmp_path / "locked"
        root.mkdir()
        root.chmod(0o500)
        try:
            healthy, detail = await LocalFileStore(root, create=False).health()
            assert not healthy
            assert "writable" in detail
        finally:
            root.chmod(0o700)


# -- the registry ----------------------------------------------------------


class TestBackendRegistry:
    def test_the_builtin_backends_are_registered(self):
        assert {"local", "memory", "s3"} <= set(registered_stores())

    def test_a_backend_is_built_by_name(self, tmp_path):
        assert isinstance(build_store("local", root=str(tmp_path)), LocalFileStore)

    def test_an_unknown_backend_names_what_is_available(self):
        with pytest.raises(ConfigError, match="unknown file store"):
            build_store("nonexistent")

    def test_limits_come_from_configuration(self, tmp_path):
        store = build_store("local", root=str(tmp_path), limits={"max_mb": 2})
        assert store.policy.max_bytes == 2 * 1024 * 1024


class TestS3Configuration:
    """Built without contacting anything: the SDK is imported lazily."""

    def test_a_bucket_is_required(self):
        with pytest.raises(ConfigError):
            build_store("s3", bucket="")

    def test_an_endpoint_makes_it_work_with_compatible_services(self):
        store = build_store(
            "s3", bucket="crm", endpoint_url="https://minio.internal:9000", region="us-east-1"
        )
        assert store.endpoint_url == "https://minio.internal:9000"

    def test_a_prefix_is_applied_to_keys(self):
        store = build_store("s3", bucket="crm", prefix="crm/files")
        assert store._object_key("a/b.pdf") == "crm/files/a/b.pdf"

    def test_signed_urls_are_on_by_default(self):
        # Serving from the bucket rather than through this process is the whole
        # reason to use S3.
        assert build_store("s3", bucket="crm").signed_urls is True

    async def test_a_missing_sdk_explains_what_to_install(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "aioboto3":
                raise ImportError("no module named aioboto3")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        store = build_store("s3", bucket="crm")
        healthy, detail = await store.health()
        assert not healthy
        assert "aioboto3" in detail
