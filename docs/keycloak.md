# Keycloak accounts

`keycloak_accounts` is an optional module (`CRM_MODULES=keycloak_accounts`)
that reads the staff roster from a Keycloak realm and lets an administrator
sign in as an account, with a log of who did it and when.

The people who may open a back office are often not rows in a table here: they
are accounts in an identity provider that already creates, disables and gives
them roles. Copying them into a local table makes a second answer to "who has
access". So the Admin API is the backing store, through the `rest` provider.

## Set up

1. Declare the connection. `connections.yaml` ships `api.keycloak`, switched
   off; enable it with `CRM_KEYCLOAK_ENABLED=true`, `CRM_KEYCLOAK_URL`,
   `CRM_KEYCLOAK_CLIENT_ID` and `CRM_KEYCLOAK_CLIENT_SECRET`. The credential
   needs to read users and role membership in the realm.
2. Name the roles that make somebody staff: `CRM_KEYCLOAK_ROLES=admin,manager`.
   They must exist. For **client** roles also set `CRM_KEYCLOAK_CLIENT`.
3. `CRM_MODULES=keycloak_accounts`, then `crm migrate` (adds `impersonation_log`).

## What it adds

* **Sign-in accounts** (`keycloak_users`): the holders of the staff roles,
  read-only. Somebody holding two of the listed roles appears once per role.
  The roster is also declared as the application's
  [staff directory](notes-and-tasks.md), so `@mentions` and unassigned-task
  alerts resolve against it.
* **Impersonation**: an *Impersonate* action and an `impersonation_log` of who
  signed in as whom. `modules.keycloak_accounts.impersonate_action(resolve)`
  attaches the same action to any resource; `resolve(record, ctx, resource)`
  answers the one thing only that resource knows, returning
  `(keycloak_id, username, email)` or `None`.
* **Grant / Revoke impersonation**: hands out and takes back Keycloak's
  `impersonation` role.

## Why impersonation uses your own token

Keycloak's admin console has the same button; it works because the click is on
a page served from Keycloak's own origin. This application is a different
origin, and the impersonation endpoint's SSO cookies are scoped to
`Path=/realms/<realm>/`, so a form post never carries them. What authenticates
the endpoint is a Bearer token, so the redirect page `fetch()`es it with the
**operator's own** access token (`CRM_OIDC_KEEP_ACCESS_TOKEN=true`), never a
shared credential. Keycloak's admin-events log then names the person who
clicked, and Keycloak itself refuses anyone who is not allowed. The same goes
for granting and revoking the role. Without the setting, the actions refuse
and say why.

## What the Admin API cannot do

`GET /admin/realms/<realm>/users` offers a substring `search` and `first`/`max`
paging -- no sorting, no filtering by role, no total. The capability shim does
the rest in Python, so sorting or filtering a realm of several thousand
accounts pulls up to `CRM_MAX_LOCAL_ROWS` of them. Searching is pushed down and
stays cheap.
