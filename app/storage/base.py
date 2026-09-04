"""The file storage contract.

Files are the third kind of backing store, alongside data providers and
connections, and they get the same treatment: one interface, several
implementations, named in configuration rather than imported at the point of
use. A deployment moves from local disk to S3 by editing a YAML file.

The interface is deliberately narrow. Storing a file, reading it back, deleting
it, and producing a URL for it are the only things a CRM needs; anything richer
belongs to the backend's own SDK.
"""

from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.core.clock import utcnow
from app.core.errors import CRMError

#: Read and written in chunks so a large file never sits in memory whole.
CHUNK_SIZE = 64 * 1024


class StorageError(CRMError):
    """A file backend failed."""

    status_code = 502
    public_message = "The file store is not responding."


class FileTooLarge(CRMError):
    status_code = 413
    public_message = "That file is too large."


class FileTypeNotAllowed(CRMError):
    status_code = 415
    public_message = "That kind of file is not accepted here."


class FileNotFound(CRMError):
    status_code = 404
    public_message = "That file is no longer available."


@dataclass(frozen=True, slots=True)
class StoredFile:
    """What a backend returns after taking a file.

    ``key`` is what gets written to the record's column; everything else is
    metadata that makes a useful link without a second round trip.
    """

    key: str
    filename: str
    size: int
    content_type: str = "application/octet-stream"
    checksum: str = ""
    stored_at: datetime | None = None
    #: Backend-specific extras -- a bucket name, an ETag.
    meta: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def extension(self) -> str:
        return posixpath.splitext(self.filename)[1].lower()

    @property
    def human_size(self) -> str:
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "filename": self.filename,
            "size": self.size,
            "content_type": self.content_type,
            "checksum": self.checksum,
        }


@runtime_checkable
class FileStore(Protocol):
    """A place files live."""

    name: str

    async def save(
        self, data: AsyncIterator[bytes] | bytes, filename: str, *, content_type: str = "",
        folder: str = "",
    ) -> StoredFile: ...

    def open(self, key: str) -> AsyncIterator[bytes]: ...
    async def delete(self, key: str) -> bool: ...
    async def exists(self, key: str) -> bool: ...
    async def url(self, key: str, *, expires: int = 3600) -> str: ...
    async def health(self) -> tuple[bool, str]: ...


@dataclass(slots=True)
class UploadPolicy:
    """What a store will accept.

    Enforced by the base class rather than by each backend, so a new backend
    cannot forget the size limit.
    """

    max_bytes: int = 25 * 1024 * 1024
    #: Extensions to accept. Empty means anything not explicitly blocked.
    allowed_extensions: frozenset[str] = frozenset()
    #: Always refused, whatever the allow list says. These are the extensions a
    #: browser or server may execute rather than download.
    blocked_extensions: frozenset[str] = frozenset({
        ".exe", ".dll", ".so", ".bat", ".cmd", ".com", ".scr", ".msi",
        ".php", ".phtml", ".jsp", ".asp", ".aspx", ".cgi", ".pl", ".py", ".sh",
        ".html", ".htm", ".svg", ".xhtml",
    })
    allowed_types: frozenset[str] = frozenset()

    def check(self, filename: str, size: int, content_type: str) -> None:
        if size > self.max_bytes:
            raise FileTooLarge(
                f"That file is {size // 1024 // 1024}MB; the limit is "
                f"{self.max_bytes // 1024 // 1024}MB."
            )
        extension = posixpath.splitext(filename)[1].lower()
        if extension in self.blocked_extensions:
            raise FileTypeNotAllowed(f"{extension} files cannot be uploaded.")
        if self.allowed_extensions and extension not in self.allowed_extensions:
            allowed = ", ".join(sorted(self.allowed_extensions))
            raise FileTypeNotAllowed(f"Only these are accepted: {allowed}.")
        if self.allowed_types and content_type not in self.allowed_types:
            raise FileTypeNotAllowed(f"{content_type} is not accepted here.")


#: Images only, for an ImageField.
IMAGES_ONLY = UploadPolicy(
    max_bytes=8 * 1024 * 1024,
    allowed_extensions=frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}),
    allowed_types=frozenset({
        "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    }),
)

DOCUMENTS = UploadPolicy(
    max_bytes=25 * 1024 * 1024,
    allowed_extensions=frozenset({
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".csv", ".md", ".rtf", ".odt", ".ods",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip",
    }),
)


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(filename: str, *, fallback: str = "file") -> str:
    """Reduce a submitted name to something safe to put in a path.

    Path separators and traversal segments are the danger: a name like
    ``../../etc/passwd`` must not be able to escape the storage root. The name
    is decorative anyway -- the key is generated -- so being aggressive costs
    nothing.
    """
    name = unicodedata.normalize("NFKD", filename or "")
    name = name.encode("ascii", "ignore").decode()
    name = posixpath.basename(name.replace("\\", "/")).strip()
    name = _UNSAFE.sub("-", name).strip("-.")
    if not name or set(name) <= {"."}:
        name = fallback
    stem, dot, extension = name.rpartition(".")
    if dot and len(extension) <= 10:
        # Trimmed again after the split: the substitution above may have left a
        # separator against the dot, as in "My Report (final).pdf".
        stem = stem[:80].strip("-.") or fallback
        return f"{stem}.{extension.lower()}"
    return name[:90].strip("-.") or fallback


def build_key(filename: str, folder: str = "") -> str:
    """A unique, unguessable key for a stored file.

    Dated folders keep any single directory from growing without bound, and the
    random component means one file's key cannot be used to guess another's.
    """
    now = utcnow()
    unique = uuid.uuid4().hex[:16]
    safe = safe_filename(filename)
    parts = [p for p in (folder.strip("/"), f"{now:%Y/%m}", f"{unique}-{safe}") if p]
    return "/".join(parts)


def guess_type(filename: str, given: str = "") -> str:
    """The content type to record.

    A browser-supplied type is not trusted for anything but a hint; the
    extension decides, because that is what the eventual download uses.
    """
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or given or "application/octet-stream"


class BaseFileStore:
    """Shared behaviour: validation, key generation, checksums.

    Backends implement the four ``_`` methods and inherit the rest, so the
    upload policy is applied identically everywhere.
    """

    name: str = "files"

    def __init__(self, *, policy: UploadPolicy | None = None, base_url: str = "") -> None:
        self.policy = policy or UploadPolicy()
        self.base_url = base_url.rstrip("/")

    # -- to implement -------------------------------------------------------

    async def _write(self, key: str, data: AsyncIterator[bytes]) -> dict[str, Any]:
        raise NotImplementedError

    def _read(self, key: str) -> AsyncIterator[bytes]:
        """Yield the file's bytes.

        Declared without ``async`` because implementations are async
        generators: calling one returns the iterator, it does not await it.
        """
        raise NotImplementedError

    async def _remove(self, key: str) -> bool:
        raise NotImplementedError

    async def _exists(self, key: str) -> bool:
        raise NotImplementedError

    # -- the interface ------------------------------------------------------

    async def save(
        self,
        data: AsyncIterator[bytes] | bytes,
        filename: str,
        *,
        content_type: str = "",
        folder: str = "",
    ) -> StoredFile:
        """Store a file and return what to record against it."""
        resolved_type = guess_type(filename, content_type)
        key = build_key(filename, folder)

        digest = hashlib.sha256()
        size = 0

        async def counted() -> AsyncIterator[bytes]:
            nonlocal size
            async for chunk in _as_stream(data):
                size += len(chunk)
                # Checked while streaming, not after: the point of a size limit
                # is to stop reading, not to discover afterwards that too much
                # was read.
                if size > self.policy.max_bytes:
                    raise FileTooLarge(
                        f"That file is larger than the "
                        f"{self.policy.max_bytes // 1024 // 1024}MB limit."
                    )
                digest.update(chunk)
                yield chunk

        # Name and type are checkable before a byte is read.
        self.policy.check(filename, 0, resolved_type)

        meta = await self._write(key, counted())

        if size == 0:
            await self._remove(key)
            raise FileTypeNotAllowed("That file is empty.")

        return StoredFile(
            key=key,
            filename=safe_filename(filename),
            size=size,
            content_type=resolved_type,
            checksum=digest.hexdigest(),
            stored_at=utcnow(),
            meta=meta or {},
        )

    def open(self, key: str) -> AsyncIterator[bytes]:
        """A stream of the file's bytes."""
        return self._read(key)

    async def delete(self, key: str) -> bool:
        return await self._remove(key)

    async def exists(self, key: str) -> bool:
        return await self._exists(key)

    async def url(self, key: str, *, expires: int = 3600) -> str:
        """Where to link this file.

        The default serves it through the application, so the same permission
        checks apply to the bytes as to the record. A backend able to issue
        signed URLs overrides this.
        """
        return f"{self.base_url}/files/{key}" if self.base_url else f"/files/{key}"

    async def health(self) -> tuple[bool, str]:
        return True, "no health check implemented"

    async def close(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


async def _as_stream(data: AsyncIterator[bytes] | bytes) -> AsyncIterator[bytes]:
    """Accept bytes or a stream, and always yield a stream."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        view = bytes(data)
        for start in range(0, len(view), CHUNK_SIZE):
            yield view[start : start + CHUNK_SIZE]
        return
    async for chunk in data:
        if chunk:
            yield chunk


#: Backend name -> factory, populated by ``@register_store``.
_STORES: dict[str, Callable[..., BaseFileStore]] = {}


def register_store(name: str) -> Callable[[Any], Any]:
    """Register a file store backend under ``name``."""

    def decorator(factory: Any) -> Any:
        _STORES[name] = factory
        return factory

    return decorator


def registered_stores() -> tuple[str, ...]:
    return tuple(sorted(_STORES))


def build_store(kind: str, **options: Any) -> BaseFileStore:
    """Build a store from its configured name."""
    from app.core.errors import ConfigError

    factory = _STORES.get(kind)
    if factory is None:
        known = ", ".join(registered_stores()) or "(none registered)"
        raise ConfigError(f"unknown file store {kind!r}; registered: {known}")
    return factory(**options)
