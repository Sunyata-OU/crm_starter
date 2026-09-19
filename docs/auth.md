# Authentication and authorization

## The chain

Several ways of establishing identity coexist. Each is tried in order; the
first to answer wins.

```bash
CRM_AUTH_PROVIDERS=api_token,proxy_header,session
CRM_LOGIN_PROVIDER=local
```

Non-interactive providers go first on purpose: a machine caller presenting a
bearer token should get a 401, never a redirect to a login page.

The distinction that runs through the whole layer:

| A provider... | Means | Effect |
| --- | --- | --- |
| returns `None` | "no credentials of my kind here" | the chain continues |
| raises `AuthError` | "credentials were presented and are wrong" | the chain **stops** |

Collapsing these would let a revoked API token quietly become an anonymous
request and be served whatever is public.

## The four providers

### Local passwords

Checks credentials against a `users` resource — itself an ordinary resource, so
accounts can live in a different database from the CRM data.

Argon2id hashing. Two details worth knowing about:

- A failed lookup still verifies against a dummy hash, so a wrong username and
  a wrong password take the same time. Without it, response timing enumerates
  which addresses have accounts.
- Both failures return the identical message, for the same reason.

## Password management

**Whether there is a password to manage is the provider's answer, not the
application's.** Behind SSO or an authenticating gateway, the credential lives
somewhere else; offering a change-password form there is worse than offering
nothing, because it implies the change will have an effect. So each provider
declares what it can do:

```python
@dataclass(frozen=True)
class AuthCapabilities:
    manages_passwords: bool   # this provider owns the credential
    change: bool              # a user can change their own here
    admin_reset: bool         # an administrator can set someone else's
    self_service_reset: bool  # a forgotten-password link can be issued
    lockout: bool             # repeated failures lock the account
```

`LocalPasswordAuth` sets all of them. Every other shipped provider sets none,
and the account page then says where the password actually lives. The lookup is
`chain.password_manager(identity)`, which matches on the identity's *own*
provider — so in a deployment running both password sign-in and SSO, the person
who arrived through SSO correctly gets no form even though the application
plainly has one for other people.

### The rules

Length first, and no expiry. Character-class requirements reliably produce
`Password1!` and a sticky note, and forced rotation makes people pick worse
passwords — so there is a generous minimum length (`CRM_PASSWORD_MIN_LENGTH`,
default 12), a check against the passwords attackers try first, a rejection of
anything repetitive, and a rejection of the account's own name or address.
Every problem is reported at once rather than one per attempt.

### Lockout, and what it does not cover

After `CRM_LOCKOUT_ATTEMPTS` failures (default 8) within
`CRM_LOCKOUT_WINDOW_MINUTES`, an account stops accepting passwords for
`CRM_LOCKOUT_MINUTES`. The counters live in the user row, not in memory:
per-worker counters would give an attacker one budget of guesses per worker.

Lockout protects **one account from many guesses**. It does nothing about one
guess each against ten thousand accounts, because no single account reaches its
limit — so sign-in is *also* rate limited per source address
(`CRM_LOGIN_BURST`, `CRM_LOGIN_RATE_PER_MINUTE`). Neither is sufficient alone.

### Reset

Three ways in, for three situations:

| Situation | Route |
| --- | --- |
| The user knows their password | `/account/password` |
| They have forgotten it | `/forgot` → an emailed link → `/reset` |
| Nobody can sign in at all | `uv run crm passwd them@example.com` |

An administrator can also reset from the user's detail page. That sets a
temporary password, shown once, and marks the account: the holder is held at
the password page until they choose their own, so a stopgap cannot quietly
become someone's permanent credential.

Reset links carry their own proof rather than a row in a table. The signature
covers the account's current password hash, so a link stops working the moment
the password changes — used once, and superseded by any other reset. No table,
no cleanup job, and no forgotten token that stays valid for a year.

Self-service reset is only offered when the deployment can actually deliver a
link, which means the email channel is configured. A link nobody receives is a
support call, not a feature.

### OIDC / SSO

```bash
CRM_LOGIN_PROVIDER=oidc
CRM_OIDC_ISSUER=https://accounts.google.com
CRM_OIDC_CLIENT_ID=...
CRM_OIDC_CLIENT_SECRET=...
CRM_OIDC_ROLES_CLAIM=groups
```

Authorization code flow with PKCE, configured by discovery URL. The transient
values of a login (state, nonce, verifier) ride in a short signed cookie rather
than server memory, so the flow survives a restart and works across workers.

With a `role_map` configured, only mapped claim values grant a role — an
unexpected group appearing in the directory cannot silently become access here.

**Nested claims.** Not every issuer puts roles at the top level, so
`CRM_OIDC_ROLES_CLAIM` accepts a dotted path:

| Issuer | Claim |
| --- | --- |
| Google, Auth0 | `groups` or `roles` — a plain key |
| Entra | `roles` |
| Keycloak, realm roles | `realm_access.roles` |
| Keycloak, client roles | `resource_access.<client-id>.roles` |

A name with no dots is looked up as a plain key, and a top-level claim whose
name genuinely contains a dot still wins over the nested reading.

Getting this wrong used to be invisible: a flat lookup against a nested claim
found nothing and fell through to `default_roles`, so an administrator signed
in successfully and arrived with the rights of a stranger. If SSO users are
landing with only the default role, check this setting first.

### Gateway headers

For deployments behind oauth2-proxy, Cloudflare Access or an authenticating
nginx.

```bash
CRM_PROXY_TRUSTED_IPS=10.0.0.0/8,172.16.0.0/12
```

**This provider stays disabled until a trusted network is configured.** An empty
allowlist with headers honoured is a complete authentication bypass — anyone
could set `X-Forwarded-User` and become anyone — so it fails closed and says so
on the System page.

### API tokens

```bash
uv run crm token ci-runner --roles user --expires-in-days 90
```

or, from the Administration → API tokens screen, **Issue a token**. Either way
the value is shown once; only the SHA-256 hash is stored. SHA-256 rather than
Argon2 is deliberate: a 256-bit random token has nothing to brute-force, and it
is verified on every API request, where a deliberately slow hash is a
self-inflicted denial of service.

There is no ordinary create form on that screen, and there cannot be: a row
typed in by hand would have no hash behind it — a credential that authenticates
nobody and looks exactly like one that works.

**Expiry.** `expires_at` is empty for a token that never expires. That stays
possible and stays a decision: the CLI and the screen both default to a finite
lifetime, because the failure mode of an expiring token is an integration that
stops and gets fixed, while the failure mode of an immortal one is a credential
still working years after the laptop it was pasted on was sold. An expired
token is refused with exactly the message an unknown one gets — telling a
caller that their token exists but has lapsed tells somebody holding a guessed
value that they guessed right.

**Rotation.** *Rotate* issues a new value for an existing token and shows it
once. The row survives, because its label, roles and history are the reason to
keep it; the old value stops working at the instant the new one starts, so
there is no window in which both are valid and none in which neither is.

**Where it was used.** Each authenticated call records `last_used` and
`last_used_ip`, but not on every request: a token used continuously is written
at most every fifteen minutes, since the question the columns answer does not
need minute-by-minute resolution and the API's whole point is being cheap to
call. A *changed* address is written immediately, because that is the
observation somebody might act on. `X-Forwarded-For` is believed only where
`CRM_PROXY_TRUSTED_IPS` names a proxy — the same rule the audit log applies —
and a failed recording never fails the call it was describing.

Bearer-authenticated requests are exempt from CSRF, since they carry no ambient
cookie credentials for a cross-site form to forge.

## Sessions

The signed cookie holds the identity itself rather than a lookup key, so there
is no session store to run and multiple workers work out of the box. The
trade-off is that a session cannot be revoked before it expires — set
`CRM_SESSION_MAX_AGE` accordingly.

Claims larger than 256 bytes are dropped rather than carried; a full token
payload would overflow the 4KB cookie limit.

## Authorization

Access control comes from two places, and they compose. **Structural** rules
live in code -- a resource that is read-only by nature, an ownership column
that always applies. **Operational** rules live in the `permissions` table, so
an administrator changes who may do what without a deployment.

### The permissions table

One row is one grant: *this role, on this resource, may do these operations,
over these rows, with these fields restricted.*

| Column | Meaning |
| --- | --- |
| `role` | matches a value in a user's `roles` |
| `resource` | a resource name, or `*` for every one |
| `can_read` / `can_create` / `can_update` / `can_delete` | the operations |
| `row_scope` | `all`, `own` (compares the record's owner to the caller), or `none` |
| `hidden_fields` | never rendered, never exported, never writable |
| `readonly_fields` | visible but locked |

Grants are managed at `/r/permissions` like any other resource -- list, form,
filters, inline editing, audit trail. Two rules make the model safe:

- **Roles add access.** Where several grants apply, the widest wins. Holding a
  second role can never take something away.
- **An empty table changes nothing.** If the table holds no rows at all, the
  structural policy decides. A deployment that never configures permissions
  behaves exactly as it did before the table existed, so an unconfigured
  install cannot lock everyone out.

The second rule turns on a distinction worth stating, because the two cases
look identical from a single resource's point of view and mean opposite
things. An **empty table** is *nobody has configured access control*, and the
structural policy answers. A table with rows in it, **none of which name this
role and this resource**, is *somebody configured access control and did not
grant this* -- which is a refusal. Collapsing the two would mean that granting
one role access to one screen silently fell back to the structural default for
every other screen, which is the opposite of what the person clicking Save
intended.

### When a grant may not widen

The rule above -- grants add access -- is right when the structural policy is
a *default*. It is wrong when the structural policy is an *invariant*.

A screen over tables that another service owns is the case. That service owns
the business rules the tables answer to, so the resource's policy refuses every
write by construction: a write here would bypass the invariants rather than
break them loudly. If a grant could turn `can_update` on for such a resource,
then one row typed into a screen would quietly re-enable exactly the thing the
policy exists to prevent.

So `DbPolicy` takes a `narrow_only` flag:

```python
DbPolicy(base=ReadOnlyPolicy(), narrow_only=True)
```

| | `narrow_only=False` (default) | `narrow_only=True` |
| --- | --- | --- |
| Reads | grants decide, once any are configured | same |
| Writes | a grant may allow what `base` refuses | a write needs **both** the grant and `base` |
| Superuser (`admin`) | bypasses the table entirely | bypasses it **for reads only** |
| Row scope, field lists | the grant's answer | intersected with `base`'s |

The superuser row is the one that matters most and is easiest to get wrong. An
`admin` bypassing the table is correct in the ordinary case -- the table is
there to *grant*, and an administrator already has everything. But a bypass
that also covered writes would mean the whole guarantee held only for
non-administrators, which is no guarantee at all: the account most likely to
be handed a `*` grant is precisely the one that must still not be able to
write through a service-owned screen.

What `narrow_only` still allows is the useful half: **a grant can widen who
reads.** That is how another role gets to see such a screen at all without a
code change and a redeploy.

### Roles and grants a module ships

```bash
uv run crm seed-grants
```

A module declares module-level `ROLES` (rows for the `roles` table, each with
a `name`) and `GRANTS` (rows for `permissions`, each with a `role` and a
`resource`), because what a role is worth is a property of the screens that
module adds. `crm seed-grants` writes down those of the enabled modules,
idempotently: it inserts a role or a grant only where no such row exists, and
never updates or deletes one. A seed that "corrected" rows back to their
shipped defaults on every deploy would make the permissions screen a lie the
moment anybody used it. If two modules declare the same role, or the same
`(role, resource)` pair, the first one loaded wins.

**Your identity provider still decides who holds a role.** These rows decide
only what holding one is worth; nothing here assigns a role to a person. With
Keycloak client roles, the claim path is
`CRM_OIDC_ROLES_CLAIM=resource_access.<client-id>.roles` -- a realm-role path
finds nothing, falls through to `default_roles`, and lands an administrator in
the application with the rights of a stranger.

**Named actions are not covered.** Who may run a particular action is still
decided in code (`roles=("admin",)`), because the grants table has no column
for "may run this particular action" and adding one is a schema change. So a
role that cannot, say, approve a payment holds that only because *nobody* but
`admin` can, not because a grant says so. Worth knowing before promising
somebody a role means read-only.

Grants are cached for 30 seconds. The cache is dropped when a grant is edited
through the application, and expires on its own so a change made by another
worker -- or by hand in SQL -- cannot be ignored indefinitely. The
**Apply changes now** action on the permissions list forces a reload.

```python
Resource("deals",
    policy=DbPolicy(
        # Consulted when the table says nothing about this resource.
        base=OwnerPolicy("owner", identity_attr="email"),
        owner_field="owner",
        identity_attr="email",
    ))
```

### The three mechanisms

They fail differently, which is why they stay separate:

```python
Resource(
    "deals",
    policy=OwnerPolicy("owner", identity_attr="email",
                       bypass_roles=("admin", "manager")),
    fields=[
        CurrencyField("margin", read_roles=["finance"]),
        StatusField("stage", write_roles=["manager"]),
    ],
)
```

| Mechanism | Applies to | Failure |
| --- | --- | --- |
| `policy.allows()` | operations | 403 |
| `policy.scope()` | rows | invisible (404 on direct access) |
| `read_roles` / `write_roles`, and a grant's field lists | fields | omitted / disabled |

`scope()` returns a filter, folded into every query the resource serves. That
is why row-level restrictions hold on detail pages, edit forms, inline cell
saves, actions, CSV exports and the aggregates behind charts — none of which
contain any authorization code.

Note also that permissions are capability-aware: `Resource.can()` checks both
the policy *and* whether the backend supports the operation, so a read-only API
never renders an edit button that could not work.

## Development

```bash
CRM_DEV_AUTH=true    # signs every visitor in as an administrator
```

The application refuses to start with this set when
`CRM_ENVIRONMENT=production`.

## Checking a deployment

`/system` (admin only) shows the assembled chain, each provider's health, and
which are disabled and why. `/whoami` returns the current identity as JSON.


## The audit trail

Every write is recorded: who, what, when, and the before and after of each
changed field. `/r/audit_log` (admin only) is the whole trail; a record's own
history appears on its detail page.

It is implemented as a **provider wrapper**, not as calls in the route
handlers. A write can start from a form, an inline cell, a bulk action, a
custom action, the JSON API or a script, and a rule that has to be remembered
at each of those is one that will eventually be forgotten. Everything already
goes through the provider, so wrapping it makes the trail structural.

Three details worth knowing:

- **The diff is recorded, not the payload.** A form resubmits every field; a
  log claiming fourteen changes when one changed is a log nobody reads. A save
  that changes nothing is recorded as an attempt with no changes.
- **Secrets never reach it.** Anything named like a password, token, key or
  secret is redacted before the entry is built.
- **A queued write is recorded as queued.** The log never claims an outcome the
  backend did not confirm.

A failing audit sink is logged loudly but never fails the operation being
audited -- a broken log must not take the application down with it.

Opt a resource out with `audited=False`. The audit log itself is opted out, or
it would fill with entries about itself.
