"""Handling uploaded files.

A form submission carrying a file needs three things that ordinary fields do
not: the bytes have to go somewhere before the record is written, the column
records a reference rather than the content, and a replaced file should not be
left behind occupying space forever.

Kept here rather than in the form engine because the form engine has no
business knowing about HTTP or storage; it receives values that are already
plain.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from starlette.datastructures import UploadFile

from app.core.results import Identity
from app.fields.base import Field
from app.fields.types import FileField, ImageField
from app.resources.resource import Resource
from app.storage import IMAGES_ONLY, StoredFile
from app.storage import store as file_stores

log = logging.getLogger("crm.uploads")


def file_fields(resource: Resource, identity: Identity) -> list[Field]:
    """The upload fields this caller may write."""
    writable = set(resource.policy.writable_fields(identity, resource))
    return [
        f for f in resource.fields
        if isinstance(f, FileField) and f.name in writable
    ]


def has_uploads(resource: Resource) -> bool:
    return any(isinstance(f, FileField) for f in resource.fields)


async def store_uploads(
    form: Mapping[str, Any],
    resource: Resource,
    identity: Identity,
    *,
    previous: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Store any uploaded files and return what to write, plus what to clean up.

    Returns ``(values, replaced_keys)``. ``values`` maps a field name to the
    JSON reference stored in its column; ``replaced_keys`` lists the keys of
    files that this write supersedes, which the caller deletes *after* the
    record is saved -- deleting first would lose the old file if the write then
    failed.
    """
    values: dict[str, str] = {}
    replaced: list[str] = []

    for field in file_fields(resource, identity):
        submitted = form.get(field.name)

        # A file input with nothing chosen still submits an empty part.
        if not isinstance(submitted, UploadFile) or not submitted.filename:
            # An explicit "remove" checkbox clears the column.
            if form.get(f"{field.name}__clear"):
                values[field.name] = ""
                if previous and (old := _key_of(previous.get(field.name))):
                    replaced.append(old)
            continue

        store = file_stores.require(field.options.get("store", ""))
        policy = IMAGES_ONLY if isinstance(field, ImageField) else store.policy

        stored = await store.save(
            _stream(submitted),
            submitted.filename,
            content_type=submitted.content_type or "",
            folder=f"{resource.name}/{field.name}",
        )
        _check(policy, stored)

        values[field.name] = json.dumps(stored.as_dict())
        if previous and (old := _key_of(previous.get(field.name))):
            replaced.append(old)

    return values, replaced


def _check(policy, stored: StoredFile) -> None:
    """Apply a field's own policy on top of the store's.

    The store already enforced its limits while streaming; this catches the
    narrower rules an ImageField adds.
    """
    policy.check(stored.filename, stored.size, stored.content_type)


async def _stream(upload: UploadFile):
    from app.storage.base import CHUNK_SIZE

    while chunk := await upload.read(CHUNK_SIZE):
        yield chunk


def _key_of(value: Any) -> str:
    """The storage key inside a stored column value."""
    return describe(value).get("key", "")


def describe(value: Any) -> dict[str, Any]:
    """Read a file column into its parts.

    Accepts the JSON this module writes, and a bare string, so a column
    populated before this feature existed -- or by another system -- still
    renders as a link.
    """
    if not value:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value)
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return {"key": text, "filename": text}
        return parsed if isinstance(parsed, dict) else {"key": text}
    return {"key": text, "filename": text.rsplit("/", 1)[-1]}


async def discard(keys: list[str], field_store: str = "") -> None:
    """Delete superseded files.

    Failures are logged, never raised: the record has already been saved by
    this point, and an orphaned file is a housekeeping problem rather than a
    reason to show the user an error.
    """
    if not keys:
        return
    store = file_stores.get(field_store)
    if store is None:
        return
    for key in keys:
        try:
            await store.delete(key)
        except Exception:
            log.warning("could not delete the replaced file %r", key, exc_info=True)
