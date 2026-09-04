"""Files in memory.

For tests and for a demo that should leave nothing behind. Not for anything
else: the contents vanish with the process and are not shared between workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.storage.base import (
    CHUNK_SIZE,
    BaseFileStore,
    FileNotFound,
    UploadPolicy,
    register_store,
)


class MemoryFileStore(BaseFileStore):
    """Keeps file contents in a dict."""

    name = "memory"

    def __init__(self, *, policy: UploadPolicy | None = None, base_url: str = "") -> None:
        super().__init__(policy=policy, base_url=base_url)
        self.files: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def _write(self, key: str, data: AsyncIterator[bytes]) -> dict[str, Any]:
        buffer = bytearray()
        async for chunk in data:
            buffer.extend(chunk)
        async with self._lock:
            self.files[key] = bytes(buffer)
        return {"stored": "memory"}

    def _read(self, key: str) -> AsyncIterator[bytes]:
        return self._stream(key)

    async def _stream(self, key: str) -> AsyncIterator[bytes]:
        content = self.files.get(key)
        if content is None:
            raise FileNotFound(f"no stored file at {key!r}")
        for start in range(0, len(content), CHUNK_SIZE):
            yield content[start : start + CHUNK_SIZE]

    async def _remove(self, key: str) -> bool:
        async with self._lock:
            return self.files.pop(key, None) is not None

    async def _exists(self, key: str) -> bool:
        return key in self.files

    async def health(self) -> tuple[bool, str]:
        total = sum(len(v) for v in self.files.values())
        return True, f"{len(self.files)} file(s), {total / 1024:.1f}KB in memory"

    def clear(self) -> None:
        self.files.clear()


@register_store("memory")
def build_memory(**options: Any) -> MemoryFileStore:
    return MemoryFileStore(base_url=options.pop("base_url", ""))
