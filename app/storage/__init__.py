"""File storage.

One interface, several backends, chosen in ``connections.yaml`` rather than in
code -- the same arrangement as data providers. Moving from local disk to S3 is
a configuration change.

Importing this package registers the built-in backends.
"""

from __future__ import annotations

from app.core.connections import ConnectionSpec, register_connection
from app.storage.base import (  # noqa: F401
    DOCUMENTS,
    IMAGES_ONLY,
    BaseFileStore,
    FileNotFound,
    FileStore,
    FileTooLarge,
    FileTypeNotAllowed,
    StorageError,
    StoredFile,
    UploadPolicy,
    build_store,
    registered_stores,
    safe_filename,
)
from app.storage.local import LocalFileStore  # noqa: F401
from app.storage.memory import MemoryFileStore  # noqa: F401
from app.storage.s3 import S3FileStore  # noqa: F401

__all__ = [
    "DOCUMENTS",
    "IMAGES_ONLY",
    "BaseFileStore",
    "FileNotFound",
    "FileStore",
    "FileTooLarge",
    "FileTypeNotAllowed",
    "LocalFileStore",
    "MemoryFileStore",
    "S3FileStore",
    "StorageError",
    "StoredFile",
    "UploadPolicy",
    "build_store",
    "registered_stores",
    "safe_filename",
    "store",
]


@register_connection(
    "files",
    close=lambda handle: handle.close(),
    check=lambda handle: handle.health(),
)
async def open_file_store(spec: ConnectionSpec) -> BaseFileStore:
    """Open a file store from a ``type: files`` connection.

    The backend is named by ``backend:``, defaulting to local disk, so the
    common case needs two lines of configuration.
    """
    options = dict(spec.options)
    backend = str(options.pop("backend", "local"))
    return build_store(backend, **options)


class StoreRegistry:
    """The file stores available to the application.

    A process-wide handle, like the permission store and the audit sink,
    because a field rendering a link has no registry to hand.
    """

    def __init__(self) -> None:
        self._stores: dict[str, BaseFileStore] = {}
        self.default_name = "files"

    def bind(self, name: str, store_instance: BaseFileStore) -> None:
        self._stores[name] = store_instance

    def get(self, name: str = "") -> BaseFileStore | None:
        return self._stores.get(name or self.default_name)

    def require(self, name: str = "") -> BaseFileStore:
        found = self.get(name)
        if found is None:
            from app.core.errors import ConfigError

            known = ", ".join(sorted(self._stores)) or "(none configured)"
            raise ConfigError(
                f"no file store named {name or self.default_name!r}; "
                f"configured stores: {known}. Add one to connections.yaml."
            )
        return found

    @property
    def configured(self) -> bool:
        return bool(self._stores)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._stores))

    def clear(self) -> None:
        self._stores.clear()


#: Bound by the registry during startup.
store = StoreRegistry()
