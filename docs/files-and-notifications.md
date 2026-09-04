# Files and notifications

Two subsystems built the same way as data providers: one interface, several
backends, chosen in configuration rather than in code.

## File storage

A `FileField` or `ImageField` on a resource is all it takes. The column records
a *reference* — storage key, original name, size, type, checksum — and the
bytes go to whichever backend is configured.

```yaml
# connections.yaml
connections:
  files:
    type: files
    backend: local          # or s3
    root: var/files
    limits:
      max_mb: 25
```

Moving to S3, MinIO, R2, Spaces or B2 is the same three lines with a different
`backend:`; they all speak one API, so only the endpoint differs.

```yaml
  files:
    type: files
    backend: s3
    bucket: ${S3_BUCKET}
    endpoint_url: ${S3_ENDPOINT:-}   # set for MinIO, R2, Spaces
    prefix: crm
    signed_urls: true                # serve from the bucket, not this app
```

Credentials are omitted deliberately where an instance role or the environment
can supply them. `aioboto3` is imported lazily, so a deployment on local disk
never installs an AWS SDK — and a missing one produces a sentence saying what
to install rather than an ImportError at startup.

### What the framework enforces

- **A size limit applied while streaming.** The point of a limit is to stop
  reading, not to discover afterwards that too much was read.
- **Executable extensions are always refused** — `.exe`, `.sh`, `.php`, and
  also `.html` and `.svg`, which would otherwise run as a page in this
  application's own origin.
- **Names are reduced before use.** `../../etc/passwd` becomes `passwd`; the
  key is generated anyway, so the submitted name is decorative.
- **Keys cannot escape the root.** Every local path is resolved and checked
  against the storage root before it is touched.
- **A partial upload leaves nothing behind.** Written to a temporary name and
  moved into place, so an interrupted upload never leaves a half-written file
  at the real key.
- **A replaced file is deleted only after the record is saved.** Deleting first
  would lose the old file if the write then failed.

### Serving

Files are served through `/files/{key}` so an attachment is exactly as private
as the record it belongs to — an unguessable key is not the same as a protected
one. Documents download; images and PDFs display inline; `nosniff` and a
`private` cache are set on every response.

A backend able to issue signed URLs bypasses this route, which is the whole
reason to use one.

## Notifications

A notification is a **stored record**; delivering it anywhere else is a
separate step. That split is the design: the in-app bell reads the row, so it
always works, and a failing SMTP server loses nothing.

```python
from app.notify import Notification, Kind, notifier

await notifier.send(
    Notification(
        recipient="sam@example.com",
        kind=Kind.ASSIGNED,
        title="You have been assigned the Acme renewal",
        resource="deals",
        record_id="7",
        actor=ctx.identity.email,     # so Sam is not told about their own action
    ),
    ctx,
)
```

### Channels

| Channel | Notes |
| --- | --- |
| `inapp` | The bell. The stored row *is* the delivery, so it cannot fail. |
| `email` | SMTP. Defaults to `high` and above — a CRM that emails everything gets filtered into a folder nobody reads. |
| `webhook` | Slack, Teams, or anything accepting a POST. `style: slack` or `raw`. |
| `console` | The log. For development, and a fallback worth having. |

```bash
CRM_NOTIFY_CHANNELS=inapp,email
CRM_SMTP_HOST=smtp.example.com
CRM_EMAIL_MIN_PRIORITY=high
```

Adding a channel is a decorator, exactly like a provider or a file backend.

### Reminders

A reminder is a notification with a future `due_at`. It is stored immediately
and stays invisible to the bell until its time comes:

```python
Notification(recipient=..., kind=Kind.DUE, title="Follow up",
             due_at=datetime.now() + timedelta(days=1))
```

Delivery is a sweep, run from cron or a scheduler:

```bash
uv run crm notify-due
```

A sweep rather than a timer, so the application holds no long-lived state and
several workers can run it safely. `crm notify-test <recipient>` checks the
channels are configured before you rely on them.

### Rules the service applies

- **Nobody is told about their own action.** A notification you caused is noise,
  so an `actor` matching the `recipient` is dropped.
- **Stored first, delivered second.** A channel failing never loses the
  notification.
- **One failing channel does not stop the others**, and each records its own
  outcome against the row.
- **A notification belongs to one person**, and nobody else can dismiss it.
