"""Serving stored files.

Files are served through the application rather than from a public directory,
because a file attached to a record should be exactly as private as the record.
A storage key is unguessable, but unguessable is not the same as protected.

A backend able to issue signed URLs -- S3 and its relatives -- bypasses this
route entirely, which is the point of using one.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends
from starlette.responses import Response, StreamingResponse

from app.core.errors import NotFound
from app.storage import FileNotFound, StorageError
from app.storage import store as file_stores
from app.web.deps import View, build_view

log = logging.getLogger("crm.files")

router = APIRouter(tags=["files"])

#: Types safe to display in the browser. Everything else downloads, so an
#: uploaded document cannot execute as a page in this application's origin.
INLINE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "application/pdf", "text/plain",
})


@router.get("/files/{key:path}", name="serve_file")
async def serve_file(key: str, view: View = Depends(build_view)) -> Response:
    """Stream a stored file to a signed-in caller."""
    # Files belong to records, and every record in this application requires an
    # identity to read. Anonymous access to attachments would be a hole in that.
    view.require_login()

    store = file_stores.get(view.param("store"))
    if store is None:
        raise NotFound("No file store is configured.")

    # Existence is checked *before* the response begins. Opening a stream only
    # builds a generator; the missing-file error would otherwise not surface
    # until the first chunk was pulled -- by which point a 200 has been sent
    # and the status can no longer be corrected.
    try:
        present = await store.exists(key)
    except StorageError as exc:
        # A key that resolves outside the storage root lands here.
        log.warning("refused a request for %r: %s", key, exc)
        raise NotFound("That file is no longer available.") from exc
    if not present:
        raise NotFound("That file is no longer available.")

    stream = store.open(key)
    filename = key.rsplit("/", 1)[-1]
    content_type = _guess(filename)
    disposition = "inline" if content_type in INLINE_TYPES else "attachment"

    return StreamingResponse(
        _guarded(stream, key),
        media_type=content_type,
        headers={
            # RFC 5987 encoding, so a name with non-ASCII characters survives.
            "Content-Disposition": (
                f"{disposition}; filename*=UTF-8''{quote(filename)}"
            ),
            # Private: a shared cache must not serve one user's attachment to
            # another who happens to request the same URL.
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _guarded(stream, key: str):
    """Turn a mid-stream storage failure into a truncated response, not a 500.

    Headers are already sent by the time the body is being written, so raising
    here cannot produce an error page. Logging and stopping is the honest
    option.
    """
    try:
        async for chunk in stream:
            yield chunk
    except FileNotFound:
        log.warning("stored file %r vanished while being served", key)
    except Exception:
        log.exception("failed while streaming %r", key)


def _guess(filename: str) -> str:
    import mimetypes

    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"
