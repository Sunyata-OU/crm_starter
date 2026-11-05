"""Keeping a generated PDF in the file store, and attaching one to an email."""

from __future__ import annotations

from app.pdf import PDF_CONTENT_TYPE
from app.storage import StoredFile
from app.storage import store as file_stores


async def save_pdf(
    content: bytes, filename: str, *, folder: str = "documents", store: str = ""
) -> StoredFile:
    """Save ``content`` in the named (default) file store and return its handle."""
    return await file_stores.require(store).save(
        content, filename, content_type=PDF_CONTENT_TYPE, folder=folder
    )
