"""Notes on a record: the conversation beside the audit trail.

The audit log answers *what changed*. It cannot answer *why*, and it cannot
hold the sentence somebody needs to read before they touch this company again.
That sentence is a note, and this is where notes are written and read.

Three decisions are worth stating.

**A note addresses a record by name, not by foreign key.** ``resource`` and
``record_id`` are strings, exactly as a notification addresses one. That is
what lets a note hang off a record in a database this application only reads --
a company owned by another service, a shift owned by a third -- without a
constraint that could not be created and a write that would not be allowed.

**Reading a note is governed by the record, not by the note.** The route has
already established that this caller may read this record; a note about a
record they can read is a note they may read. So the reads here go through the
provider directly, the same way the timeline reads the audit log.

**Who to tell is derived from who has spoken.** There is no follower table and
no subscribe button: the people notified are the ones who wrote earlier notes
on the same record, plus anyone the new note names. A back office is small
enough for that to be the right answer, and it needs no directory of users --
which matters here, because identity comes from Keycloak and there is no local
account table to look anyone up in.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.query import Condition, ListQuery, Op, Sort, SortDir, and_
from app.core.results import Ctx, Identity, Record
from app.notify.base import Kind, Notification

log = logging.getLogger("crm.notes")

#: The resource notes are stored through. Absent in a deployment with no
#: database of its own, which is the case this module has to survive.
RESOURCE = "notes"

#: How much of a note a notification quotes.
EXCERPT = 240

#: Files one note may carry. A bound rather than a policy: the store already
#: enforces a size limit per file, and this stops a single note becoming an
#: upload session.
MAX_FILES = 5

#: Longest note accepted. Generous, and still a bound: the column is Text, so
#: without one a paste of a log file becomes a permanent part of every page
#: load for that record.
MAX_BODY = 8000

#: ``@`` followed by an address or a handle. The address form is the one that
#: always works; the handle form is resolved against the people already on the
#: record, and against the account table when there is one.
#:
#: The lookbehind is what stops an address written in prose -- "write to
#: sam@example.com" -- being read as a mention of @example.com.
MENTION = re.compile(r"(?<![\w.@-])@([\w.+-]+@[\w-]+\.[\w.-]+|[\w][\w.-]{1,63})")


def resource_for(registry: Any) -> Any | None:
    """The notes resource, or ``None`` where there is nowhere to put one."""
    if registry is not None and registry.has_resource(RESOURCE):
        return registry.resource(RESOURCE)
    return None


def handles_in(body: str) -> list[str]:
    """The ``@handles`` a note names, in the order they appear, once each."""
    seen: list[str] = []
    for match in MENTION.finditer(body or ""):
        handle = match.group(1).rstrip(".")
        if handle.lower() not in {h.lower() for h in seen}:
            seen.append(handle)
    return seen


def author_id(identity: Identity) -> str:
    """The identifier a note is filed under, and notifications addressed to.

    Email first, because that is what ``notifications.recipient`` is compared
    against and what a person recognises as themselves. The subject is the
    fallback for a provider that supplies no address -- unrecognisable, but
    stable, which is the property that matters for "is this note mine?".
    """
    return identity.email or identity.subject


async def for_record(
    notes: Any, resource_name: str, pk: str, ctx: Ctx, *, limit: int = 100
) -> list[Record]:
    """Every note on one record, oldest last."""
    query = ListQuery(
        filter=and_(
            Condition("resource", Op.EQ, resource_name),
            Condition("record_id", Op.EQ, str(pk)),
        ),
        sort=(Sort("created_at", SortDir.DESC),),
        page_size=limit,
        with_total=False,
    )
    page = await notes.provider.list(query, ctx)
    return list(page.items)


async def add(
    notes: Any,
    *,
    resource_name: str,
    pk: str,
    body: str,
    identity: Identity,
    ctx: Ctx,
    mentions: Sequence[str] = (),
    attachments: Sequence[dict[str, Any]] = (),
) -> Record | None:
    """Write one note. Returns the stored row, or ``None`` if it was refused."""
    text = (body or "").strip()
    # A note is what somebody wrote *or* what they attached: a file dropped on
    # a record with no covering sentence is still worth keeping.
    if not text and not attachments:
        return None
    result = await notes.provider.create(
        {
            "resource": resource_name,
            "record_id": str(pk),
            "kind": "note",
            "body": text[:MAX_BODY],
            "author": identity.label[:120],
            "author_id": author_id(identity)[:160],
            "mentions": json.dumps(list(mentions)) if mentions else None,
            "attachments": json.dumps(list(attachments)) if attachments else None,
            "pinned": False,
        },
        ctx,
    )
    return result.record


def stored_mentions(note: Record | dict[str, Any]) -> list[str]:
    """The recipients a stored note named, tolerant of a hand-edited column."""
    raw = note.get("mentions")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(v) for v in raw]
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in loaded] if isinstance(loaded, list) else []


def participants(notes: Iterable[Record], *, exclude: str = "") -> list[str]:
    """Everyone who has written on this record already.

    The nearest thing to a follower list, arrived at without a follower table:
    somebody who has commented on a record has shown they care what happens to
    it.
    """
    out: list[str] = []
    for note in notes:
        who = str(note.get("author_id") or "").strip()
        if who and who != exclude and who not in out:
            out.append(who)
    return out


async def resolve_handles(
    handles: Sequence[str], *, known: Sequence[str] = (), registry: Any = None, ctx: Ctx | None = None
) -> list[str]:
    """Turn ``@handles`` into recipient identifiers.

    An address is taken as written. A bare handle is matched against the people
    already on the record, then against the account table if this deployment
    has one. A handle that matches nothing is dropped rather than guessed at:
    a notification sent to a misspelling is one nobody receives and everybody
    assumes was received.
    """
    resolved: list[str] = []
    for handle in handles:
        if "@" in handle:
            match: str | None = handle
        else:
            match = _match_known(handle, known)
            if match is None and registry is not None:
                match = await _match_users(handle, registry, ctx)
        if match and match not in resolved:
            resolved.append(match)
    return resolved


def _match_known(handle: str, known: Sequence[str]) -> str | None:
    """A handle against identifiers already seen on this record."""
    wanted = handle.lower()
    for identifier in known:
        local = identifier.split("@", 1)[0].lower()
        if local == wanted or identifier.lower() == wanted:
            return identifier
    return None


async def _match_users(handle: str, registry: Any, ctx: Ctx | None) -> str | None:
    """A handle against the account table, where the deployment has one."""
    if not registry.has_resource("users"):
        return None
    users = registry.resource("users")
    query = ListQuery(
        filter=Condition("email", Op.STARTSWITH, f"{handle}@"),
        page_size=2,
        with_total=False,
    )
    try:
        page = await users.provider.list(query, ctx or Ctx.system())
    except Exception:  # a users table that cannot be read is not a mention bug
        log.debug("could not resolve @%s against users", handle, exc_info=True)
        return None
    items = list(page.items)
    # Exactly one, or it is a guess. Two people whose addresses start the same
    # way is precisely when picking one is wrong.
    if len(items) == 1:
        return str(items[0].get("email") or "") or None
    return None


def notifications_for(
    *,
    identity: Identity,
    resource_name: str,
    record_label: str,
    pk: str,
    body: str,
    mentioned: Sequence[str],
    also: Sequence[str] = (),
) -> list[Notification]:
    """What to send about a new note.

    A mention is addressed to you; a note on a record you have spoken about is
    news. They read differently, so they are different notifications -- and the
    mention wins where somebody is both.
    """
    excerpt = body.strip()
    if len(excerpt) > EXCERPT:
        excerpt = excerpt[:EXCERPT].rstrip() + "…"
    actor = author_id(identity)
    who = identity.label
    out: list[Notification] = []
    seen = set()
    for recipient in mentioned:
        if recipient in seen:
            continue
        seen.add(recipient)
        out.append(Notification(
            recipient=recipient,
            title=f"{who} mentioned you on {record_label}",
            body=excerpt,
            kind=Kind.MENTIONED,
            resource=resource_name,
            record_id=str(pk),
            actor=actor,
        ))
    for recipient in also:
        if recipient in seen:
            continue
        seen.add(recipient)
        out.append(Notification(
            recipient=recipient,
            title=f"{who} commented on {record_label}",
            body=excerpt,
            kind=Kind.INFO,
            resource=resource_name,
            record_id=str(pk),
            actor=actor,
        ))
    return out


async def store_files(uploads: Sequence[Any], *, resource_name: str, pk: str) -> list[dict[str, Any]]:
    """Put a note's attachments in the file store and describe them.

    Uploaded before the note row is written, which is the safe order: an
    orphaned object in a bucket is housekeeping, whereas a note referring to
    bytes that were never stored is a broken link somebody has to explain.

    The store's name is recorded beside each file rather than assumed at render
    time -- the deployment may read from several of them, its own uploads and
    another service's bucket, and a link has to say which.
    """
    from app.storage import store as file_stores

    store = file_stores.get("")
    if store is None:
        raise StorageUnavailable("No file store is configured for attachments.")
    name = file_stores.default_name

    saved: list[dict[str, Any]] = []
    for upload in uploads[:MAX_FILES]:
        filename = getattr(upload, "filename", "")
        if not filename:
            continue
        stored = await store.save(
            _stream(upload),
            filename,
            content_type=getattr(upload, "content_type", "") or "",
            folder=f"notes/{resource_name}/{pk}",
        )
        store.policy.check(stored.filename, stored.size, stored.content_type)
        saved.append({**stored.as_dict(), "store": name})
    return saved


async def _stream(upload: Any):
    from app.storage.base import CHUNK_SIZE

    while chunk := await upload.read(CHUNK_SIZE):
        yield chunk


class StorageUnavailable(RuntimeError):
    """Raised when a note carries a file and there is nowhere to put it."""


def attachments_of(note: Record | dict[str, Any]) -> list[dict[str, Any]]:
    """The files on a stored note, as the template wants them."""
    raw = note.get("attachments")
    if not raw:
        return []
    try:
        loaded = raw if isinstance(raw, list) else json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    out: list[dict[str, Any]] = []
    for item in loaded:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        out.append({
            "key": item["key"],
            "filename": item.get("filename") or str(item["key"]).rsplit("/", 1)[-1],
            "size": item.get("size") or 0,
            "content_type": item.get("content_type") or "",
            "store": item.get("store") or "",
        })
    return out


async def discard_files(note: Record | dict[str, Any]) -> None:
    """Delete a removed note's attachments.

    Failures are logged rather than raised: the note is going away either way,
    and a file left in a bucket is a housekeeping problem, not a reason to
    refuse somebody the removal of what they wrote.
    """
    from app.storage import store as file_stores

    for item in attachments_of(note):
        store = file_stores.get(item["store"])
        if store is None:
            continue
        try:
            await store.delete(item["key"])
        except Exception:
            log.warning("could not delete the attachment %r", item["key"], exc_info=True)


def may_remove(note: Record | dict[str, Any], identity: Identity) -> bool:
    """Whether this person may take this note down.

    Their own, or an administrator's judgement. Notes are not silently
    editable by others: a conversation somebody else can rewrite is not a
    record of anything.
    """
    if identity.has_role("admin"):
        return True
    mine = author_id(identity)
    return bool(mine) and str(note.get("author_id") or "") == mine
