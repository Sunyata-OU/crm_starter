"""Application settings, read from the environment and an optional .env file."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: A list that arrives from the environment as a comma-separated string.
#:
#: Without ``NoDecode``, pydantic-settings tries to JSON-parse any complex type
#: before validators run, so ``CRM_AUTH_PROVIDERS=api_token,session`` fails
#: outright rather than reaching the validator that would split it. Nobody
#: writes a JSON array in an env var.
type CsvList = Annotated[list[str], NoDecode]

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix="CRM_"
    )

    # -- identity -----------------------------------------------------------
    app_name: str = "CRM"
    app_tagline: str = ""
    debug: bool = False
    environment: str = "development"

    # -- security -----------------------------------------------------------
    #: Signs session cookies and CSRF tokens. Generated per-process if unset,
    #: which is fine for development and fatal for a multi-worker deployment --
    #: hence the production check below.
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    session_cookie: str = "crm_session"
    session_max_age: int = 60 * 60 * 12
    cookie_secure: bool = False
    #: Literal rather than str so a typo is a startup error, not a cookie the
    #: browser quietly refuses to send.
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_enabled: bool = True

    # -- auth ---------------------------------------------------------------
    #: Auth providers to try, in order. The first to return an identity wins.
    #:
    #: ``proxy_header`` is deliberately absent: it does nothing without a list
    #: of trusted networks, so including it by default only produced a warning
    #: on every start. A deployment behind a gateway adds it explicitly.
    auth_providers: CsvList = ["api_token", "session"]
    #: Session-backed provider used by the login form.
    login_provider: str = "local"
    #: Signs everyone in as a developer account. Refused outside development.
    dev_auth: bool = False
    dev_auth_roles: CsvList = ["admin"]

    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    #: Claim holding the user's roles, and a claim-value-to-role mapping.
    oidc_roles_claim: str = "roles"
    oidc_default_roles: list[str] = ["user"]

    # -- passwords ----------------------------------------------------------
    # Only consulted by providers that hold passwords. Under SSO or a gateway,
    # the rules that apply are the identity provider's, not these.
    #
    # Length first, and no expiry: forced rotation reliably produces a worse
    # password with a number on the end. See app/auth/passwords.py.
    password_min_length: int = 12
    #: Wrong guesses before an account locks. 0 disables locking entirely.
    lockout_attempts: int = 8
    lockout_minutes: int = 15
    #: Failures older than this stop counting towards a lock.
    lockout_window_minutes: int = 60
    #: Sign-in attempts per source address: a burst of this many, refilling at
    #: `login_rate_per_minute`. Guards the shape lockout cannot -- one guess
    #: each against many accounts. 0 disables it.
    login_burst: int = 10
    login_rate_per_minute: float = 6.0
    #: Offer "forgot password". Needs the email channel: a link nobody
    #: receives is worse than no link at all.
    password_reset: bool = True
    #: How long a reset link stays valid. One hour is long enough to reach an
    #: inbox and short enough that a forwarded email ages out.
    password_reset_max_age: int = 3600

    #: Networks permitted to assert identity via headers. Empty disables it.
    proxy_trusted_ips: CsvList = []
    proxy_user_header: str = "X-Forwarded-User"
    proxy_email_header: str = "X-Forwarded-Email"
    proxy_roles_header: str = "X-Forwarded-Groups"

    # -- data ---------------------------------------------------------------
    connections_file: str = "connections.yaml"
    #: Optional modules to switch on, by name. Additive: the platform's own
    #: modules load whatever this says, so listing the demo here cannot leave
    #: an application without accounts or access control.
    modules: CsvList = []
    #: Rows the capability shim may hold in memory to emulate a query stage.
    max_local_rows: int = 5000
    default_page_size: int = 25

    # -- notifications ------------------------------------------------------
    #: Channels to deliver through. The in-app bell reads the stored row and is
    #: always available; the rest are opt-in.
    #: How a notification reaches its channels. "background" is a task on this
    #: worker's event loop -- immediate, and lost if the worker stops.
    #: "queue" hands it to the durable job queue, which survives a restart at
    #: the cost of a worker having to be running. "inline" delivers before the
    #: request returns, which is what tests and CLI commands want.
    #: Which configured connection holds the job queue. Its own by default in
    #: name only -- db.main is the same database as everything else -- but
    #: pointing it at a separate one is a one-line change, and the right one to
    #: make when a busy queue's writes start competing with the application's.
    jobs_connection: str = "db.main"
    notify_delivery: str = "background"
    notify_channels: CsvList = ["inapp"]
    notify_base_url: str = "http://localhost:8000"

    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_tls: bool = False
    smtp_sender: str = "crm@localhost"
    #: Email only for this priority and above; a CRM emailing everything is a
    #: CRM people filter into a folder they never read.
    email_min_priority: str = "high"

    notify_webhook_url: str = ""
    notify_webhook_style: str = "slack"

    # -- performance --------------------------------------------------------
    #: A request costing more than this many queries, or taking longer than
    #: this many milliseconds, is logged as slow. Both are thresholds on the
    #: same log line because they catch different faults: too many queries is a
    #: shape problem in the code, while slow with few queries is usually a
    #: missing index or a slow remote call.
    #: How long a worker may reuse its cached permission grants. Every request
    #: consults them, so this is one of the few caches that genuinely matters;
    #: the cost of a longer TTL is that a permission change takes that long to
    #: reach workers other than the one that made it.
    permission_cache_ttl: float = 30.0
    #: How long shutdown waits for in-flight background work. Long enough for
    #: a slow SMTP server, short enough that a deploy is not held up by one.
    shutdown_timeout: float = 10.0
    warn_queries: int = 25
    warn_request_ms: float = 1000.0
    #: Compress responses above this size. Below roughly a kilobyte the header
    #: overhead and the CPU cost outweigh the saving.
    gzip_min_size: int = 1000
    #: zlib level, 1 (fastest) to 9 (smallest).
    gzip_level: int = 6
    #: How long a browser may keep a static asset. The default is short
    #: because the files are served under stable names, with no content hash
    #: to make a long cache safe.
    static_max_age: int = 3600

    # -- ui -----------------------------------------------------------------
    #: Reload templates on every request. Slower, but no restart while editing.
    template_reload: bool = True
    #: Used for anyone without a preference of their own. Not the server's
    #: zone: that is an accident of where it happens to be running.
    timezone: str = "UTC"
    date_format: str = "%d %b %Y"
    datetime_format: str = "%d %b %Y, %H:%M"
    currency_symbol: str = "$"

    @field_validator(
        "auth_providers", "modules", "dev_auth_roles", "proxy_trusted_ips",
        "notify_channels",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value):
        """Accept a comma-separated string, since env vars cannot hold lists."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in ("production", "prod", "live")

    @property
    def connections_path(self) -> Path:
        path = Path(self.connections_file)
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def templates_dir(self) -> Path:
        return APP_DIR / "templates"

    @property
    def static_dir(self) -> Path:
        return APP_DIR / "static"

    def check(self) -> list[str]:
        """Configuration problems worth refusing to start over.

        Returned rather than raised so the CLI can print all of them at once.
        """
        problems: list[str] = []
        if self.is_production:
            if "CRM_SECRET_KEY" not in _env_keys():
                problems.append(
                    "CRM_SECRET_KEY is not set. A generated key changes on every "
                    "restart and differs between workers, which logs everyone out."
                )
            if self.dev_auth:
                problems.append("CRM_DEV_AUTH signs in every visitor and must be off in production.")
            if not self.cookie_secure:
                problems.append("CRM_COOKIE_SECURE should be true when serving over HTTPS.")
            if self.debug:
                problems.append("CRM_DEBUG exposes tracebacks and must be off in production.")
        if self.notify_delivery not in ("inline", "background", "queue"):
            problems.append(
                f"CRM_NOTIFY_DELIVERY is {self.notify_delivery!r}; it must be "
                f"'inline', 'background' or 'queue'."
            )
        if self.notify_delivery == "queue" and self.is_production:
            # Not an error -- it is the durable choice -- but a queue with
            # nothing draining it holds every notification for ever, and the
            # symptom (no emails, no errors) points nowhere near the cause.
            problems.append(
                "CRM_NOTIFY_DELIVERY=queue needs `crm worker` running somewhere, "
                "or notifications will be stored and never delivered."
            )
        if "proxy_header" in self.auth_providers and not self.proxy_trusted_ips:
            problems.append(
                "The proxy_header auth provider is enabled but CRM_PROXY_TRUSTED_IPS is "
                "empty, so any client could assert any identity. It will stay disabled."
            )
        return problems


def _env_keys() -> set[str]:
    import os

    return set(os.environ)


@lru_cache
def get_settings() -> Settings:
    return Settings()
