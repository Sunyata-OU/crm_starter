"""The application factory.

Startup order matters and is the reason this is one function rather than
module-level code: modules must register their resources before providers are
bound, and providers must be bound before the first request is served.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from app.auth import current as auth_holder
from app.auth.api_token import ApiTokenAuth
from app.auth.base import BaseAuthProvider
from app.auth.chain import AuthChain, SessionAuth
from app.auth.local import LocalPasswordAuth
from app.auth.oidc import DevAuth, OIDCAuth
from app.auth.passwords import LockoutPolicy, ResetTokens
from app.auth.proxy_header import ProxyHeaderAuth
from app.auth.session import SessionStore
from app.core.connections import ConnectionRegistry
from app.core.errors import ConfigError
from app.core.modules import load_modules
from app.core.registry import Registry

# Imported for its side effects: providers register their connection types and
# factories on import, so a connections.yaml entry cannot be resolved until the
# module implementing it has been loaded.
from app.providers import builtin as _builtin  # noqa: F401
from app.settings import Settings, get_settings
from app.web.csrf import CSRFProtection
from app.web.deps import AppState
from app.web.errors import install_error_handlers
from app.web.flash import FlashStore
from app.web.middleware import CachedStaticFiles, RequestContextMiddleware
from app.web.ratelimit import RateLimiter
from app.web.render import Templates
from app.web.routes import auth as auth_routes
from app.web.routes import files as file_routes
from app.web.routes import notify as notify_routes
from app.web.routes import resource as resource_routes
from app.web.routes import system as system_routes

log = logging.getLogger("crm")


def build_registry(settings: Settings) -> Registry:
    """Load modules and return a registry that is not yet bound to providers."""
    connections = ConnectionRegistry.from_file(settings.connections_path)
    registry = Registry(connections)
    # Before the modules load, so a module declaring a resource may consult it.
    registry.settings = settings
    loaded = load_modules(registry, enabled=settings.modules or None)
    registry.loaded_modules = loaded
    log.info(
        "loaded %d module(s): %s", len(loaded), ", ".join(m.name for m in loaded) or "none"
    )
    return registry


def wire_jobs(settings: Settings, registry: Registry) -> None:
    """Point the job queue at its table and set the notifier's delivery mode.

    The web process binds the queue so it can *enqueue*; it does not run jobs.
    Draining is ``crm worker``, deliberately a separate process: work that must
    survive a deploy should not live in the thing being deployed.
    """
    from app.jobs import handlers as _handlers  # noqa: F401  (registers them)
    from app.jobs import queue
    from app.notify import notifier

    if registry.has_resource("jobs"):
        queue.bind(registry.resource("jobs").provider)
    elif settings.notify_delivery == "queue":
        log.warning(
            "CRM_NOTIFY_DELIVERY=queue needs a 'jobs' resource; deliveries will "
            "run in the background instead"
        )

    notifier.background = settings.notify_delivery != "inline"
    notifier.queue_deliveries = settings.notify_delivery == "queue" and queue.configured


def build_auth(settings: Settings, registry: Registry, sessions: SessionStore) -> AuthChain:
    """Assemble the authentication chain named in settings.

    A provider whose backing resource is missing is skipped with a warning
    rather than crashing startup: a deployment using only SSO has no reason to
    define a local users table.
    """
    providers: list[BaseAuthProvider] = []
    for name in settings.auth_providers:
        match name:
            case "api_token":
                if registry.has_resource("api_tokens"):
                    providers.append(ApiTokenAuth(registry.resource("api_tokens").provider))
                else:
                    log.warning("auth provider 'api_token' needs an 'api_tokens' resource; skipped")
            case "proxy_header":
                provider = ProxyHeaderAuth(
                    trusted_ips=settings.proxy_trusted_ips,
                    user_header=settings.proxy_user_header,
                    email_header=settings.proxy_email_header,
                    roles_header=settings.proxy_roles_header,
                )
                if provider.enabled:
                    providers.append(provider)
                else:
                    log.warning(
                        "auth provider 'proxy_header' has no trusted networks; skipped"
                    )
            case "session":
                providers.append(SessionAuth(sessions))
            case "oidc":
                if settings.oidc_issuer:
                    providers.append(_build_oidc(settings))
                else:
                    log.warning("auth provider 'oidc' has no issuer configured; skipped")
            case "local":
                if registry.has_resource("users"):
                    providers.append(_build_local(settings, registry))
                else:
                    log.warning("auth provider 'local' needs a 'users' resource; skipped")
            case unknown:
                log.warning("unknown auth provider %r in settings; skipped", unknown)

    # The interactive provider the login form posts to must be in the chain even
    # when it is not itself used to read a request.
    have = {p.name for p in providers}
    if settings.login_provider == "local" and registry.has_resource("users") and "local" not in have:
        providers.append(_build_local(settings, registry))
    if settings.login_provider == "oidc" and settings.oidc_issuer and "oidc" not in have:
        providers.append(_build_oidc(settings))

    if settings.dev_auth:
        if settings.is_production:
            raise ConfigError("CRM_DEV_AUTH cannot be enabled in a production environment")
        log.warning("dev auth is on: every visitor is signed in as an administrator")
        providers.append(DevAuth(roles=settings.dev_auth_roles))

    return AuthChain(providers)


def _build_local(settings: Settings, registry: Registry) -> LocalPasswordAuth:
    """Password sign-in, with the policy the settings describe.

    Reset links are only offered when this deployment can actually deliver one:
    a link nobody receives is a support call, not a feature. That means a
    notification channel that leaves the machine, which for a password reset
    means email.
    """
    can_email = "email" in settings.notify_channels
    return LocalPasswordAuth(
        registry.resource("users").provider,
        min_length=settings.password_min_length,
        lockout=LockoutPolicy(
            max_attempts=settings.lockout_attempts,
            lock_minutes=settings.lockout_minutes,
            window_minutes=settings.lockout_window_minutes,
        ),
        reset_tokens=(
            ResetTokens(settings.secret_key, max_age=settings.password_reset_max_age)
            if settings.password_reset and can_email
            else None
        ),
    )


def _build_oidc(settings: Settings) -> OIDCAuth:
    return OIDCAuth(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        secret_key=settings.secret_key,
        scopes=settings.oidc_scopes,
        roles_claim=settings.oidc_roles_claim,
        default_roles=settings.oidc_default_roles,
    )


def build_channels(settings: Settings) -> list[Any]:
    """The delivery channels named in settings.

    A channel that cannot be built is skipped with a warning rather than
    stopping startup: notifications are a convenience, and losing email should
    not take the application down with it.
    """
    from app.notify import build_channel

    built: list[Any] = []
    for name in settings.notify_channels:
        options: dict[str, Any] = {"base_url": settings.notify_base_url}
        if name == "email":
            options.update(
                host=settings.smtp_host, port=settings.smtp_port,
                username=settings.smtp_username, password=settings.smtp_password,
                use_tls=settings.smtp_tls, sender=settings.smtp_sender,
                min_priority=settings.email_min_priority,
            )
        elif name == "webhook":
            if not settings.notify_webhook_url:
                log.warning("notification channel 'webhook' has no URL configured; skipped")
                continue
            options.update(
                url=settings.notify_webhook_url, style=settings.notify_webhook_style
            )
        try:
            built.append(build_channel(name, **options))
        except Exception as exc:
            log.warning("notification channel %r could not be built: %s", name, exc)
    return built


def create_app(settings: Settings | None = None, registry: Registry | None = None) -> FastAPI:
    """Build the application. Accepts a prepared registry so tests can inject one."""
    settings = settings or get_settings()

    for problem in settings.check():
        log.warning("configuration: %s", problem)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reg = app.state.crm.registry
        await reg.bind()
        # Auth providers need bound providers behind them, so the chain is
        # rebuilt here rather than before startup.
        app.state.crm.auth = build_auth(settings, reg, app.state.crm.sessions)
        # Also published process-wide, for the places with no request to hand:
        # a resource action, a CLI command, a background sweep.
        auth_holder.bind(app.state.crm.auth)

        from app.resources.rbac import store as permission_store

        permission_store.ttl = settings.permission_cache_ttl

        from app.notify import notifier

        notifier.use(build_channels(settings))
        if reg.has_resource("notifications"):
            notifier.bind(reg.resource("notifications").provider)
        wire_jobs(settings, reg)
        log.info("ready: %d resources, auth chain %s", len(reg), app.state.crm.auth)
        try:
            yield
        finally:
            # Deliveries first: they may still want to write their outcome to a
            # provider, so the connections have to outlive them.
            drained = await notifier.drain(timeout=settings.shutdown_timeout)
            if drained:
                log.info("finished %d in-flight notification deliveries", drained)
            await reg.close()

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    sessions = SessionStore(
        settings.secret_key,
        cookie_name=settings.session_cookie,
        max_age=settings.session_max_age,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
    app.state.crm = AppState(
        settings=settings,
        registry=registry if registry is not None else build_registry(settings),
        templates=Templates(settings),
        auth=AuthChain([]),
        csrf=CSRFProtection(settings.secret_key, enabled=settings.csrf_enabled),
        sessions=sessions,
        login_limiter=RateLimiter(
            rate=settings.login_rate_per_minute / 60.0,
            burst=settings.login_burst,
        ),
        flash=FlashStore(
            settings.secret_key,
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,
        ),
    )

    # Order matters, and reads bottom-up for the response: the request context
    # is entered first so it wraps everything, including whatever the error
    # handlers return, and compression is applied last to the finished body.
    app.add_middleware(
        GZipMiddleware,
        minimum_size=settings.gzip_min_size,
        # Not the library default of 9. On HTML, level 6 gives within a
        # percent or two of the same ratio for a fraction of the CPU, and
        # compression is otherwise one of the more expensive things a
        # request does.
        compresslevel=settings.gzip_level,
    )
    app.add_middleware(
        RequestContextMiddleware,
        debug=settings.debug,
        warn_queries=settings.warn_queries,
        warn_ms=settings.warn_request_ms,
    )

    install_error_handlers(app)
    app.include_router(system_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(resource_routes.router)
    app.include_router(file_routes.router)
    app.include_router(notify_routes.router)

    if settings.static_dir.exists():
        app.mount(
            "/static",
            CachedStaticFiles(
                directory=settings.static_dir, max_age=settings.static_max_age
            ),
            name="static",
        )

    return app


app_instance: FastAPI | None = None


def get_app() -> FastAPI:
    """The application, built once. Used by uvicorn's import string."""
    global app_instance
    if app_instance is None:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        app_instance = create_app()
    return app_instance
