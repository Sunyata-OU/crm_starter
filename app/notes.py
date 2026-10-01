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

**A mention may only reach back-office staff.** `@someone@gmail.com` in a note
body used to be taken at face value -- a note is an internal conversation, but
the parser could not tell a colleague's handle from an arbitrary address typed
by whoever is signed in. Where a directory exists (``Registry.staff_directory``, which a module
sets), a mention only
resolves to somebody in it -- see `resolve_handles` and `staff_directory`.
Where there is none -- a plain install, or a stateless one -- the original
behaviour survives unchanged: an address is taken as written, and a handle is
matched against the people already on the record and then the `users` table.
"""

from __future__ import annotations

import json
import logging
import re
import time as time_module
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.query import Condition, ListQuery, Op, Sort, SortDir, and_
from app.core.results import Ctx, Identity, Record
from app.notify.base import Kind, Notification

log = logging.getLogger("crm.notes")

#: How long a fetched staff directory is trusted before being re-read. Every
#: note write and every autocomplete keystroke wants this; an identity provider's admin
#: API is not sized for either, and "who may be mentioned" does not need to be
#: current to the second.
DIRECTORY_TTL = 300.0

#: (expiry, people). One entry, keyed implicitly: a deployment has at most one
#: staff directory, so there is nothing to key on. A module-level cache
#: rather than an object living on the registry because `resolve_handles` is
#: called from wherever a note is written, several calls deep from anything
#: holding a registry reference of its own.
_directory_cache: tuple[float, list[dict[str, str]]] | None = None

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

#: ``@`` followed by an address or a handle. Extraction only -- what either
#: form resolves *to* is `resolve_handles`'s decision, and differs by
#: deployment: a bare address is trusted as written only where there is no
#: staff directory to check it against.
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

    Where this deployment has a staff directory (see `staff_directory`), a
    mention -- address or bare handle alike -- only resolves to somebody in
    it: a note is an internal conversation, and a free-form ``@address``
    notifying anybody who can be typed is not a feature, it is a way to spam
    an inbox that happens to belong to whoever is signed in.

    Where there is no directory, the original rule applies: an address is
    taken as written, and a bare handle is matched against the people already
    on the record, then against the account table if this deployment has one.

    Either way, a handle that matches nothing is dropped rather than guessed
    at: a notification sent to a misspelling is one nobody receives and
    everybody assumes was received.
    """
    people = await staff_directory(registry, ctx) if registry is not None else None
    resolved: list[str] = []
    for handle in handles:
        match: str | None
        if people is not None:
            match = _match_directory(handle, people, known)
        elif "@" in handle:
            match = handle
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


async def staff_directory(registry: Any, ctx: Ctx | None) -> list[dict[str, str]] | None:
    """Who may be @mentioned, or ``None`` where this deployment has nobody to check against.

    ``None`` -- no ``Registry.staff_directory`` declared -- is different from an *empty
    list*: the former tells `resolve_handles` to fall back to its original,
    unrestricted behaviour, so an install that never had a directory is not
    broken by one being introduced elsewhere. The latter means the directory
    exists but could not be read just now, and every mention on this write
    resolves to nobody rather than falling back -- a directory that is merely
    unreachable must not become a hole that lets a mention through it would
    otherwise have refused.

    Cached for `DIRECTORY_TTL`, because this is consulted on every note write
    and every autocomplete keystroke.
    """
    global _directory_cache
    declared = getattr(registry, "staff_directory", None)
    if declared is None or not registry.has_resource(declared.resource):
        return None
    now = time_module.monotonic()
    if _directory_cache is not None and _directory_cache[0] > now:
        return _directory_cache[1]
    resource = registry.resource(declared.resource)
    try:
        # Sorted, because a directory that is a union of several sources has to
        # merge every row of every source before it can know which come first
        # when unsorted -- which trips the row budget on a directory of any
        # size and would leave this empty.
        page = await resource.provider.list(
            ListQuery(
                sort=(Sort(declared.sort_field, SortDir.ASC),), page_size=1000, with_total=False
            ),
            ctx or Ctx.system(),
        )
    except Exception:
        log.warning("could not read the staff directory to resolve @mentions", exc_info=True)
        return []
    people: list[dict[str, str]] = []
    for row in page.items:
        email = str(row.get(declared.email_field) or "").strip()
        username = str(row.get(declared.username_field) or "").strip()
        if not email and not username:
            continue
        name = " ".join(str(row.get(f) or "") for f in declared.name_fields).strip()
        people.append({"email": email, "username": username, "label": name or username or email})
    _directory_cache = (now + DIRECTORY_TTL, people)
    return people


def may_browse_directory(identity: Identity, registry: Any = None) -> bool:
    """Whether this caller may search who can be @mentioned.

    The suggestions route hands back names and addresses, so it is gated like
    the roster itself: by holding one of ``StaffDirectory.roles``. An
    authenticated caller outside that set gets no matches rather than a way to
    enumerate staff that the roster's own screen would refuse them.

    With no directory declared, or one that names no roles, any signed-in
    caller may -- consistent with `staff_directory` returning ``None`` and
    mentions falling back to their original, broader behaviour.
    """
    if not identity.is_authenticated:
        return False
    declared = getattr(registry, "staff_directory", None)
    if declared is None or not declared.roles:
        return True
    return identity.has_role(*declared.roles)


async def mention_suggestions(
    registry: Any, ctx: Ctx | None, query: str, *, limit: int = 20
) -> list[dict[str, str]]:
    """Directory entries matching ``query``, for the ``@`` autocomplete.

    An empty list where there is no directory: the route this backs is only
    ever useful with one, and offering to search a `users` table that was
    never meant to be enumerated this way is not this function's decision to
    make silently.
    """
    people = await staff_directory(registry, ctx)
    if not people:
        return []
    wanted = query.strip().lower()
    if not wanted:
        return people[:limit]
    return [
        p for p in people
        if wanted in p["email"].lower() or wanted in p["username"].lower()
        or wanted in p["label"].lower()
    ][:limit]


def _match_directory(handle: str, people: Sequence[dict[str, str]], known: Sequence[str]) -> str | None:
    """A handle -- address or bare -- against the staff directory.

    "Already on this record" only counts as a mention target when that person
    is themselves staff: otherwise typing ``@`` plus whatever an outside
    participant's author label happens to be would resolve just because they
    had spoken here before, which is the same hole restricting mentions to the
    directory closes everywhere else.
    """
    wanted = handle.lower().lstrip("@")
    for person in people:
        email, username = person["email"].lower(), person["username"].lower()
        if wanted in (email, username, email.split("@", 1)[0]):
            return person["email"] or person["username"]
    matched = _match_known(handle, known)
    if matched and any(
        matched.lower() in (p["email"].lower(), p["username"].lower()) for p in people
    ):
        return matched
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
