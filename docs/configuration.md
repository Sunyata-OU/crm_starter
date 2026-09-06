# Configuration

Two files and an environment. Knowing which is which saves a lot of searching:

| Where | What belongs there |
| --- | --- |
| environment / `.env` | everything below — behaviour, secrets, limits |
| `connections.yaml` | *what to connect to*: databases, caches, file stores, brokers |
| `modules/` | *what the application is*: resources, fields, views |

Every variable is prefixed `CRM_` and read once at startup. `crm serve` refuses
to start on a configuration that is wrong in a way it can detect — see
[Production checks](#production-checks).

## Identity

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_APP_NAME` | `CRM` | Shown in the navigation and page titles. |
| `CRM_APP_TAGLINE` | — | Optional subtitle on the sign-in page. |
| `CRM_ENVIRONMENT` | `development` | `production` enables the startup checks and forbids dev auth. |
| `CRM_DEBUG` | `false` | Also what adds `X-Query-Count` and `Server-Timing` to responses. |

## Security

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_SECRET_KEY` | generated | **Set this in production.** Signs sessions, CSRF tokens, flash messages and reset links. Generated per process when unset, so with several workers each signs differently and nobody stays signed in. |
| `CRM_SESSION_COOKIE` | `crm_session` | Cookie name. |
| `CRM_SESSION_MAX_AGE` | `43200` | Seconds. Also the upper bound on how long a compromised session lives, since a cookie session cannot be revoked early. |
| `CRM_COOKIE_SECURE` | `false` | Set true behind HTTPS. |
| `CRM_COOKIE_SAMESITE` | `lax` | `lax`, `strict` or `none`. A typo is a startup error rather than a cookie the browser silently drops. |
| `CRM_CSRF_ENABLED` | `true` | Off only for tests. |

## Authentication

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_AUTH_PROVIDERS` | `api_token,session` | Tried in order; the first to return an identity wins. `proxy_header` is deliberately absent — it does nothing without trusted networks. |
| `CRM_LOGIN_PROVIDER` | `local` | Which provider the sign-in form posts to: `local` or `oidc`. |
| `CRM_DEV_AUTH` | `false` | Signs every visitor in as an administrator. Refused when the environment is production. |
| `CRM_DEV_AUTH_ROLES` | `admin` | Roles that account gets. |
| `CRM_OIDC_ISSUER` | — | Discovery URL. |
| `CRM_OIDC_CLIENT_ID` / `CRM_OIDC_CLIENT_SECRET` | — | |
| `CRM_OIDC_SCOPES` | `openid email profile` | |
| `CRM_OIDC_ROLES_CLAIM` | `roles` | Claim carrying group membership. |
| `CRM_OIDC_DEFAULT_ROLES` | `user` | Given to anyone the claim does not map. |
| `CRM_PROXY_TRUSTED_IPS` | — | Networks allowed to assert identity by header. **Without this the provider stays off**, because otherwise any client could set the header and be anyone. |
| `CRM_PROXY_USER_HEADER` | `X-Forwarded-User` | |
| `CRM_PROXY_EMAIL_HEADER` | `X-Forwarded-Email` | |
| `CRM_PROXY_ROLES_HEADER` | `X-Forwarded-Groups` | |

## Passwords

Only consulted by an auth provider that actually holds passwords. Under SSO or
behind a gateway the rules that apply are the identity provider's, and these do
nothing. See [`auth.md`](auth.md#password-management).

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_PASSWORD_MIN_LENGTH` | `12` | Length is the rule that matters; there are deliberately no character-class requirements and no expiry. |
| `CRM_LOCKOUT_ATTEMPTS` | `8` | Wrong guesses before an account locks. `0` disables locking. |
| `CRM_LOCKOUT_MINUTES` | `15` | How long a lock lasts. |
| `CRM_LOCKOUT_WINDOW_MINUTES` | `60` | Failures older than this stop counting, so occasional typos never accumulate into a lockout. |
| `CRM_LOGIN_BURST` | `10` | Sign-in attempts per source address before rate limiting bites. Guards the shape lockout cannot: one guess each against many accounts. |
| `CRM_LOGIN_RATE_PER_MINUTE` | `6` | Refill rate for that budget. |
| `CRM_PASSWORD_RESET` | `true` | Offer "forgot password". Needs the email channel — a link nobody receives is a support call, not a feature. |
| `CRM_PASSWORD_RESET_MAX_AGE` | `3600` | Seconds a reset link stays valid. Long enough to reach an inbox, short enough that a forwarded email ages out. |

## Data

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_DATABASE_URL` | `sqlite+aiosqlite:///./crm.db` | Read by `connections.yaml`, not directly by the app. |
| `CRM_CONNECTIONS_FILE` | `connections.yaml` | Where the connection definitions live. |
| `CRM_MODULES` | — | Optional modules to enable, comma separated. Additive: `core_identity` and `core_access` load regardless. |
| `CRM_MAX_LOCAL_ROWS` | `5000` | Rows the capability shim may hold in memory to emulate a query stage. Exceeding it raises rather than truncating. |
| `CRM_DEFAULT_PAGE_SIZE` | `25` | |
| `CRM_DB_POOL_SIZE` / `CRM_DB_MAX_OVERFLOW` / `CRM_DB_POOL_RECYCLE` | `10` / `20` / `1800` | Read by `connections.yaml`. See [`scaling.md`](scaling.md#one-host). |
| `CRM_ARCHIVE_ENABLED` | `false` | Turns on the second database `connections.yaml` defines. A disabled connection is never opened, so this costs nothing until you want it. |
| `CRM_ARCHIVE_URL` | `sqlite+aiosqlite:///./crm-archive.db` | Its URL. Each database keeps its own migration history; see [`multiple-databases.md`](multiple-databases.md). |

## Caching

Off by default. See [`scaling.md`](scaling.md#caching) for what is cached and
what deliberately is not.

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_CACHE_BACKEND` | `none` | `none`, `memory` or `redis`. |
| `CRM_REDIS_URL` | `redis://localhost:6379/0` | Needs the `redis` package: `uv add redis`. |

## Files

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_FILE_BACKEND` | `local` | `local`, `memory` or `s3`. Local disk does not work across hosts. |
| `CRM_FILE_ROOT` | `var/files` | Where the local backend writes. |

S3 credentials and bucket go in `connections.yaml`.

## Notifications

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_NOTIFY_CHANNELS` | `inapp` | `inapp`, `email`, `webhook`, `console`. The bell always works; the rest are opt-in. |
| `CRM_JOBS_CONNECTION` | `db.main` | Which configured connection holds the job queue. Point it at a second database to keep a busy queue's writes off the one serving requests; `crm migrate --all` then covers both. |
| `CRM_NOTIFY_DELIVERY` | `background` | How a notification reaches its channels. `background` is a task on this worker's event loop — immediate, and lost if the worker stops. `queue` hands it to the durable job queue, which survives a restart **but needs `crm worker` running**. `inline` delivers before the request returns. |
| `CRM_NOTIFY_BASE_URL` | `http://localhost:8000` | Used to build links in messages that leave the application. |
| `CRM_SMTP_HOST` / `CRM_SMTP_PORT` | `localhost` / `25` | |
| `CRM_SMTP_USERNAME` / `CRM_SMTP_PASSWORD` | — | |
| `CRM_SMTP_TLS` | `false` | |
| `CRM_SMTP_SENDER` | `crm@localhost` | |
| `CRM_EMAIL_MIN_PRIORITY` | `high` | Email only at this priority and above. A CRM that emails everything is one people filter into a folder they never read. |
| `CRM_NOTIFY_WEBHOOK_URL` | — | |
| `CRM_NOTIFY_WEBHOOK_STYLE` | `slack` | |

## Performance

Covered in [`scaling.md`](scaling.md).

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_WARN_QUERIES` | `25` | A request costing more queries than this logs a line naming the statement that repeated most. |
| `CRM_WARN_REQUEST_MS` | `1000` | The same line, for elapsed time. Two thresholds because they catch different faults. |
| `CRM_PERMISSION_CACHE_TTL` | `30` | Without a shared cache, this is how long a permission change takes to reach other workers. |
| `CRM_GZIP_MIN_SIZE` | `1000` | Bytes. Below roughly a kilobyte, compression costs more than it saves. |
| `CRM_GZIP_LEVEL` | `6` | Not zlib's default of 9: on HTML that is 2% smaller for four times the CPU. |
| `CRM_STATIC_MAX_AGE` | `3600` | Short because assets have stable names. Raise it once filenames carry a content hash. |
| `CRM_SHUTDOWN_TIMEOUT` | `10` | Seconds to wait for in-flight background deliveries before exiting. |

## Presentation

| Variable | Default | Notes |
| --- | --- | --- |
| `CRM_TEMPLATE_RELOAD` | `true` | Turn off in production; `crm serve` insists. |
| `CRM_TIMEZONE` | `UTC` | For anyone with no preference of their own. Not the server's zone, which is an accident of where it runs. |
| `CRM_DATE_FORMAT` | `%d %b %Y` | |
| `CRM_DATETIME_FORMAT` | `%d %b %Y, %H:%M` | |
| `CRM_CURRENCY_SYMBOL` | `$` | |
| `CRM_MAP_TILE_URL` | OpenStreetMap | Tiles for map views. The public OSM server is fine while you are building and against its usage policy for a deployment of any size -- point it at your own or a commercial one. Empty turns a map into a list of located records, which is also what an air-gapped install wants: tiles are the only thing on any page that leaves the network. |
| `CRM_MAP_ATTRIBUTION` | `© OpenStreetMap contributors` | Credit shown on the map. Change it with the tile server. |

## Production checks

`Settings.check()` runs at startup and is what `crm serve` refuses on. It
reports a generated secret key, dev auth left on, `proxy_header` without
trusted networks, insecure cookies, and template reload — each of which is
correct in development and wrong in production, which is exactly the class of
mistake that survives review and fails in deployment.

```bash
uv run crm serve      # refuses, and says which settings are the problem
```
