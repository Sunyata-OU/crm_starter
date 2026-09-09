# Helpdesk

`core_helpdesk` is support tickets: a board, a thread per ticket, and a sweep —
a small core helpdesk. Enable it like any other module:

```bash
CRM_MODULES=core_helpdesk
```

It needs `db.main`. Where there is none, the module
is absent rather than broken, the same way `notes` and `tasks` handle it.

**This is the core only.** There is no IMAP ingest here; that is a second
branch. What follows is the shape it plugs into.

## Tickets and messages

`tickets` is the record: reference, subject, requester name and email, state,
priority, an assignee, and where it came from (`staff` or `email`). `source`
is always `staff` on this branch — the only way to raise a ticket is the
"Raise a ticket" form, which is deliberate: there is no public web form in
v1, and the ingest branch is what will ever write `email`.

### Messages versus notes — the separation that matters most

A ticket gets the ordinary activity panel for free, being a record with a
detail view. That is where an internal remark belongs — "waiting on billing to
confirm the refund" — exactly as it would on a company or a deal.

`ticket_messages` is a different table, for a different purpose: the
customer-visible thread, and only that. There is deliberately no visibility
flag on it, no `internal` column that starts unchecked. The way this is made
impossible rather than merely discouraged is that a row in `ticket_messages`
has no sense in which it could be private — it is, by definition, something
that was or can be emailed to the requester. Writing something private means
writing it in the notes panel instead, which never reaches an inbox.
`tests/test_helpdesk.py` pins this down: the resource declares no field that
could carry an "internal" bit, so there is nothing for a form, a script or a
future contributor to get wrong in that direction.

## Reference

Every ticket has a reference — `T-1042` — filled in by
[`SequencingProvider`](providers.md) immediately after the insert that reveals
the primary key it is derived from (`app.providers.sequence`). It is what a
reply's subject is tagged with (`[T-1042] Refund for order 88`), because a
primary key is not a thing a person reads over the phone with confidence they
typed it right, and a subject tag is the one part of an email a mangled or
top-posted reply is most likely to keep intact.

## The board and the thread

The list, board (grouped by state) and detail views are declared the same way
`core_tasks` declares its own — see [`resources.md`](resources.md). The detail
view shows the thread through a `BackrefField` ordered oldest-first, next to
the activity panel for internal notes, with a reply box below it.

**Bodies are text, never HTML.** Both `ticket_messages.body` and the reply box
are plain text fields, rendered through the same auto-escaping template
machinery as every other text field in the application. Inbound mail is,
starting with the ingest branch, attacker-controlled content rendered inside
an authenticated admin session; there is no rich-text renderer here to add
that risk, and there will not be one added for inbound mail either.

## Replying

Sending a reply from the detail view:

1. Writes the outgoing row to `ticket_messages` first — before the send is
   attempted. A reply the requester never received but this application
   believes it sent is worse than a visible duplicate somebody can re-send;
   writing first keeps the thread here matching what was actually attempted.
2. Sets `Message-ID`, `In-Reply-To` and `References` from the ticket's last
   message (either direction), via `app.helpdesk.reply_headers`, so a normal
   mail client threads the reply correctly.
3. Tags the subject `[T-1042]`, via `app.helpdesk.tag_subject`, so a reply is
   identifiable even from a client that strips or mangles the threading
   headers.
4. Sends through the existing email channel (`app.notify`) at `urgent`
   priority with `channels=("email",)`, so `CRM_EMAIL_MIN_PRIORITY` can never
   silently swallow a transactional reply the way it is meant to filter
   ordinary notifications.

## The sweep

`crm helpdesk-sweep` follows `crm tasks-sweep`'s pattern exactly: it compares
columns rather than hooking the write, so every path that can change a
ticket's assignee or state — the form, an inline edit, a bulk action, and
(once it exists) the ingest branch opening one automatically — is covered by
one sweep instead of a notification remembered at each of them.

| It notices | By comparing | And tells |
| --- | --- | --- |
| a hand-over | `assignee` against `notified_assignee` | the new assignee |
| a new unassigned ticket | `assignee` empty, `reminded_at` unset | `CRM_HELPDESK_WATCHERS` |
| a ticket ageing quietly | `created_at` against now, once assigned and `reminded_at` unset | the assignee, and the watchers |

Unlike a task's due-date reminder, "ageing" has no per-ticket date to compare
against — a ticket carries no `due_at` — so it is judged against
`CRM_HELPDESK_STALE_HOURS` from `created_at`. A ticket only ever gets one of
the last two notifications per `reminded_at` reset, the same trade-off
`app.tasks.sweep` makes between its due-date and unassigned branches: an
unassigned ticket is told about immediately, and once it is picked up, the
clock for "ageing" starts being worth mentioning again only after the next
reset (a reopen, in practice).

Run it beside `crm notify-due` and `crm tasks-sweep`, from cron, a scheduler,
or the `notify` profile in `compose.yaml`.

## The ingest seam

`app.helpdesk` holds every piece of logic the inbound-mail branch will need,
with no IMAP or network code anywhere in it, so it is testable as a plain
function today. That branch is expected to call:

* `match_reference(subject)` — the ticket reference named in a `[T-1042]`
  subject tag, if the fetched email's subject carries one.
* `thread_ids(in_reply_to, references)` — the Message-IDs worth checking
  against `ticket_messages.message_id` when a subject tag alone did not
  resolve a ticket (a client that stripped the tag, or a fresh forward).
* `is_duplicate(message_id, known)` — whether a fetched message has already
  been stored, once the ingest branch has looked its Message-ID up.
* `new_ticket_fields(...)` / `new_message_fields(...)` — the rows to insert
  for a ticket opened by an inbound email, and the message itself.
* `next_thread_anchor(current, message_id)` — what `tickets.thread_message_id`
  becomes after the inbound message is stored.

None of these touch a database or a socket. The ingest branch owns fetching
the mail, looking rows up, and deciding what to do with a duplicate or an
unmatched reply; this module only owns the decisions that do not depend on
where the bytes came from.
