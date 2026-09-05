# Writing a provider

A provider is the only component that knows where a resource's data actually
lives. Implementing one means answering the query language; everything above —
fields, views, routes, templates — comes for free.

## The interface

```python
class Provider(Protocol):
    name: str
    capabilities: Capabilities

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]: ...
    async def get(self, pk: Any, ctx: Ctx) -> Record | None: ...
    async def create(self, data: dict, ctx: Ctx) -> WriteResult: ...
    async def update(self, pk: Any, data: dict, ctx: Ctx) -> WriteResult: ...
    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult: ...
    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> list[dict]: ...
    async def update_if(
        self, pk: Any, data: dict, expect: dict, ctx: Ctx
    ) -> WriteResult | None: ...
    async def warm(self) -> None: ...
    async def health(self) -> tuple[bool, str]: ...
```

Subclass `BaseProvider` and implement only what your backend supports; the rest
refuse politely.

Two of those are worth explaining, because their defaults are deliberate.

### `warm()` — pay the startup cost at startup

Whatever is the same for every request and expensive the first time: reflecting
a table, resolving a schema, opening a channel. Left until the first request,
it lands on whoever happens to arrive first and shows up as one slow page after
every deploy.

```python
async def warm(self) -> None:
    await self.table()          # reflect now, not on the first request
```

The registry calls this for every resource concurrently once providers are
bound, and tolerates failure: a backend that is temporarily unreachable delays
its own resource, it does not stop the application starting. It is logged,
though — a warning here is usually the first sign of a misspelled table name,
and finding that at startup beats finding it as a 502 an hour later.

### `update_if()` — a write that loses the race, safely

Update only while the stored record still matches `expect`; return `None` when
it does not, because somebody else got there first.

```python
async def update_if(self, pk, data, expect, ctx):
    stmt = update(table).where(table.c.id == pk, table.c.sent_at.is_(None))
    result = await conn.execute(stmt.values(**data))
    return None if result.rowcount == 0 else WriteResult.success(...)
```

This is what makes background work safe to run from more than one place. The
sweep behind `crm notify-due` claims each row by writing the very column its
query filters on, so the winner's write removes the row from every other
sweeper's result set and a reminder cannot be delivered twice. The same
primitive is what an edit form needs to say "this record changed while you were
looking at it" instead of silently overwriting a colleague.

**Do not emulate it with a read and a write.** The default raises
`UnsupportedOperation`, and that is the right answer for a backend that cannot
express a conditional write: read-then-write passes every test and still loses
rows in production, which is worse than an honest refusal. The shim does not
emulate it either, for the same reason.

## Declare capabilities honestly

This is the one rule that matters.

```python
self.capabilities = Capabilities(
    read=True,
    server_filter=True,
    filter_ops=frozenset({Op.EQ, Op.IN}),  # only what you really support
    server_sort=False,                     # the shim will sort
)
```

Declaring `False` is not a failure — it is a request for the shim to handle
that concern, and it will. Declaring `True` is a **promise**, because the shim
then steps aside entirely. A provider claiming `server_filter=True` while
ignoring an operator returns silently wrong results that nothing will catch.

If in doubt, declare less. The cost is a wider read; the cost of over-claiming
is incorrect data on screen.

## The contract suite

```python
# tests/contract/test_provider_contract.py
CAPABILITY_PROFILES = {
    "native": ...,           # your provider as it is
    "shimmed-dumb": ...,     # with every capability stripped
    "shimmed-partial": ...,  # with some stripped
}
```

Add your provider to this suite. It runs the same ~90 assertions against each
profile and requires identical results. If your provider passes natively but
fails when shimmed, its capability declaration is lying.

## Registering it

Two decorators wire a new backend in:

```python
@register_connection("mybackend", close=..., check=...)
async def open_mybackend(spec: ConnectionSpec):
    return MyClient(spec.option("url", required=True))

@register_provider_factory("mybackend")
def build(handle, target: str, resource) -> MyProvider:
    return MyProvider(handle, target, pk_field=resource.pk)
```

Then in `connections.yaml`:

```yaml
connections:
  my.backend:
    type: mybackend
    url: ${MY_BACKEND_URL}
```

And on a resource: `provider="my.backend#some_target"`. Whatever follows the
`#` reaches your factory as `target` — a table name, an endpoint path, a
routing key.

## Shipped providers

| Provider | Notes |
| --- | --- |
| `SQLProvider` | Any SQLAlchemy-supported database. Tables are reflected, so no model layer is needed. Fully capable. |
| `RestProvider` | Declarative mapping of an HTTP API: where rows live, how pages work, which filters push down. |
| `AMQPWriteProvider` | Publishes commands. Writes return `PENDING`; reads are refused rather than faked. |
| `MemoryProvider` | Tests, demos, prototypes. Capabilities configurable, which is how the shim is tested. |
| `CompositeProvider` | Reads from one, writes to another. Declare with `resource.write_provider = "mq.events#thing"`. |
| `UnionProvider` | Reads one resource from several backends at once. Declare with `provider=Union(...)`; see [Several databases](multiple-databases.md). |
| `ReadOnly` | Wraps any provider to refuse writes. |
| `AuditingProvider` | Wraps any provider to record every write, with before/after values. Applied automatically; not something a resource declares. |

## REST connection authentication

```yaml
connections:
  crm_api:
    type: rest
    options:
      base_url: https://api.example.com
      auth:
        type: oauth2                       # bearer | header | basic | oauth2
        token_url: https://issuer/oauth/token
        client_id: ${API_CLIENT_ID}
        client_secret: ${API_CLIENT_SECRET}
        scope: read:things                 # optional
        client_auth: basic                 # basic (default) | body
        extra: {audience: https://api.example.com}   # Auth0 wants this
        leeway: 30                         # renew this many seconds early
```

`bearer`, `header` and `basic` are a header computed once, when the connection
opens. `oauth2` is client-credentials, and is the only one with a lifecycle: the
token is fetched at first use, reused until shortly before it expires, and
renewed once if the API rejects it with a 401. Concurrent requests against a
cold connection share one token fetch rather than each opening their own.

An unrecognised `type` is refused when the connection opens. Ignoring it would
build an unauthenticated client, and the resulting 401 from the API reads like
wrong credentials rather than a misspelled type name.

## Asynchronous writes

If your backend accepts a write without confirming it, say so:

```python
self.capabilities = Capabilities(write=True, write_mode="async")

async def create(self, data, ctx):
    await self.publish(data)
    return WriteResult.pending(message="Queued for processing.")
```

The form pipeline will render a queued badge with the correlation reference
rather than claiming the record was saved. Nothing else needs to change.
