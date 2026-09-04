"""Files on local disk.

The default, and the right answer for a single-server deployment. Files live
under one root directory; keys are relative paths beneath it.

The security concern is path traversal: a key is user-influenced data, and a
key like ``../../etc/passwd`` must not be able to reach outside the root. Every
path is therefore resolved and checked against the root before it is touched.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.storage.base import (
    CHUNK_SIZE,
    BaseFileStore,
    FileNotFound,
    StorageError,
    UploadPolicy,
    register_store,
)


class LocalFileStore(BaseFileStore):
    """Stores files under a directory on this machine."""

    name = "local"

    def __init__(
        self,
        root: str | os.PathLike[str] = "var/files",
        *,
        policy: UploadPolicy | None = None,
        base_url: str = "",
        create: bool = True,
    ) -> None:
        super().__init__(policy=policy, base_url=base_url)
        self.root = Path(root).expanduser().resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        """The absolute path for a key, refusing anything outside the root.

        ``resolve()`` collapses ``..`` segments and follows symlinks, so the
        containment check happens on the real destination rather than on the
        text of the path.
        """
        candidate = (self.root / key.lstrip("/")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageError(f"refusing to touch {key!r}: it resolves outside the file root")
        return candidate

    # -- the backend --------------------------------------------------------

    async def _write(self, key: str, data: AsyncIterator[bytes]) -> dict[str, Any]:
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary name and moved into place, so a failed or
        # interrupted upload never leaves a half-written file at the real key.
        staging = destination.with_name(destination.name + ".part")

        try:
            handle = await asyncio.to_thread(staging.open, "wb")
            try:
                async for chunk in data:
                    await asyncio.to_thread(handle.write, chunk)
            finally:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(os.replace, staging, destination)
        except Exception:
            await asyncio.to_thread(staging.unlink, True)
            raise

        return {"path": str(destination)}

    def _read(self, key: str) -> AsyncIterator[bytes]:
        return self._stream(key)

    async def _stream(self, key: str) -> AsyncIterator[bytes]:
        path = self.path_for(key)
        if not path.is_file():
            raise FileNotFound(f"no stored file at {key!r}")

        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def _remove(self, key: str) -> bool:
        path = self.path_for(key)
        if not path.exists():
            return False
        await asyncio.to_thread(path.unlink)
        # Tidy the dated folders a key created, but never the root itself.
        parent = path.parent
        while parent != self.root and not any(parent.iterdir()):
            await asyncio.to_thread(parent.rmdir)
            parent = parent.parent
        return True

    async def _exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    async def health(self) -> tuple[bool, str]:
        try:
            if not self.root.is_dir():
                return False, f"{self.root} is not a directory"
            if not os.access(self.root, os.W_OK):
                return False, f"{self.root} is not writable"
            usage = shutil.disk_usage(self.root)
            free = usage.free / 1024 / 1024 / 1024
            return True, f"{self.root} ({free:.1f}GB free)"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


@register_store("local")
def build_local(**options: Any) -> LocalFileStore:
    policy = options.pop("policy", None)
    return LocalFileStore(
        options.pop("root", "var/files"),
        policy=_policy_from(options.pop("limits", None)) if policy is None else policy,
        base_url=options.pop("base_url", ""),
    )


def _policy_from(limits: dict[str, Any] | None) -> UploadPolicy | None:
    """Build an upload policy from the connection's YAML options."""
    if not limits:
        return None
    fields: dict[str, Any] = {}
    if "max_mb" in limits:
        fields["max_bytes"] = int(limits["max_mb"]) * 1024 * 1024
    if "allowed_extensions" in limits:
        fields["allowed_extensions"] = frozenset(
            e if e.startswith(".") else f".{e}" for e in limits["allowed_extensions"]
        )
    if "allowed_types" in limits:
        fields["allowed_types"] = frozenset(limits["allowed_types"])
    return UploadPolicy(**fields) if fields else None
