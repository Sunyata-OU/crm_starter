"""Command line interface.

Everything here is meant to answer a question you would otherwise answer by
guessing: what is registered, what can each backend actually do, and is the
configuration in this environment going to work.
"""

from __future__ import annotations

import asyncio
import json

import typer

# Imported for its side effects; see the module docstring.
from app.providers import builtin as _builtin  # noqa: F401
from app.settings import ROOT_DIR as ROOT
from app.settings import get_settings

app = typer.Typer(
    help="A provider-driven CRM starter.",
    no_args_is_help=True,
    add_completion=False,
)


def _echo(text: str = "", **kw) -> None:
    typer.echo(text, **kw)


def _fail(text: str) -> None:
    typer.secho(text, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.command()
def dev(
    host: str = typer.Option("127.0.0.1", help="Interface to bind."),
    port: int = typer.Option(8000, help="Port to listen on."),
    reload: bool = typer.Option(True, help="Restart when files change."),
) -> None:
    """Run the development server."""
    import uvicorn

    settings = get_settings()
    for problem in settings.check():
        typer.secho(f"warning: {problem}", fg=typer.colors.YELLOW, err=True)

    _echo(f"→ http://{host}:{port}")
    uvicorn.run(
        "app.main:get_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
        log_level="info",
    )


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Interface to bind."),
    port: int = typer.Option(8000),
    workers: int = typer.Option(4, help="Worker processes."),
) -> None:
    """Run the production server."""
    import uvicorn

    settings = get_settings()
    problems = settings.check()
    if problems:
        for problem in problems:
            typer.secho(f"error: {problem}", fg=typer.colors.RED, err=True)
        _fail("Refusing to start with the configuration above.")

    uvicorn.run("app.main:get_app", host=host, port=port, workers=workers, factory=True)


@app.command()
def seed(
    password: str = typer.Option("demo-password", help="Password for every demo account."),
    reset: bool = typer.Option(True, help="Drop existing tables first."),
) -> None:
    """Create the schema for the enabled modules and fill it with sample data."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.connections import ConnectionRegistry
    from app.main import build_registry
    from app.seed import seed as run_seed

    settings = get_settings()
    registry = ConnectionRegistry.from_file(settings.connections_path)
    try:
        spec = registry.spec("db.main")
    except Exception as exc:
        _fail(str(exc))
        return

    url = spec.option("url", required=True)

    # Loading the modules is what puts their tables on the metadata and their
    # seeders within reach, so seeding follows CRM_MODULES exactly. With no
    # demo module enabled this creates the platform tables and the accounts,
    # and stops -- which is the intended starting point for a real project.
    loaded = build_registry(settings).loaded_modules

    async def go():
        engine = create_async_engine(url)
        try:
            return await run_seed(engine, password=password, reset=reset, modules=loaded)
        finally:
            await engine.dispose()

    summary = asyncio.run(go())

    _echo(f"Seeded {url.split('///')[-1] if '///' in url else url}\n")
    for key, count in summary.items():
        if key != "password":
            _echo(f"  {count:>4}  {key}")
    _echo()
    typer.secho("Sign in with:", bold=True)
    _echo(f"  admin@example.com   / {summary['password']}   (admin)")
    _echo(f"  manager@example.com / {summary['password']}   (sees everything)")
    _echo(f"  kim@example.com     / {summary['password']}   (sees only their own records)")
    _echo("\nStart the server with:  uv run crm dev")


@app.command()
def resources(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="List fields too."),
) -> None:
    """List registered resources and their views."""
    from app.main import build_registry

    settings = get_settings()
    registry = build_registry(settings)

    if not len(registry):
        _echo("No resources registered. Add a module under modules/.")
        return

    for resource in registry:
        views = ", ".join(v.name for v in resource.switchable_views())
        typer.secho(f"\n{resource.name}", bold=True, nl=False)
        _echo(f"  ({len(resource.fields)} fields)  provider={resource.provider_ref}")
        _echo(f"  views: {views}")
        if resource.actions:
            _echo(f"  actions: {', '.join(resource.actions)}")
        if verbose:
            for field in resource.fields:
                flags = [
                    name for name, on in (
                        ("list", field.in_list), ("form", field.in_form),
                        ("filter", field.in_filter), ("search", field.searchable),
                        ("inline", field.inline_editable), ("readonly", field.readonly),
                    ) if on
                ]
                _echo(f"    {field.name:<18} {field.type_name:<12} {','.join(flags)}")


@app.command("check-connections")
def check_connections() -> None:
    """Open every configured connection and report whether it works."""
    from app.core.connections import ConnectionRegistry

    settings = get_settings()
    registry = ConnectionRegistry.from_file(settings.connections_path)

    if not registry.names:
        _echo(f"No connections configured in {settings.connections_path}.")
        return

    async def go():
        try:
            return await registry.health_all()
        finally:
            await registry.close_all()

    results = asyncio.run(go())
    failures = 0
    for health in results:
        if health.healthy:
            typer.secho("  ok   ", fg=typer.colors.GREEN, nl=False)
        else:
            failures += 1
            typer.secho("  FAIL ", fg=typer.colors.RED, nl=False)
        _echo(f"{health.name:<16} {health.type:<12} {health.detail}")

    if failures:
        _fail(f"\n{failures} connection(s) failed.")
    _echo("\nAll connections healthy.")


@app.command()
def capabilities() -> None:
    """Show what each resource's backend can do natively.

    Where a column says "emulated", the shim is doing that work in memory --
    correct, but it costs a wider read than the query implies.
    """
    from app.main import build_registry

    settings = get_settings()
    registry = build_registry(settings)

    async def go():
        await registry.bind()
        rows = []
        for resource in registry:
            provider = resource.provider
            native = getattr(provider, "inner_capabilities", provider.capabilities)
            rows.append((resource.name, getattr(provider, "name", "?"), native, provider.capabilities))
        await registry.close()
        return rows

    rows = asyncio.run(go())

    header = f"{'resource':<14}{'provider':<20}{'filter':<11}{'sort':<11}{'page':<11}{'count':<11}{'agg':<11}writes"
    typer.secho(header, bold=True)
    _echo("-" * len(header))
    for name, provider_name, native, caps in rows:
        cells = []
        for attr in ("server_filter", "server_sort", "server_paginate", "total_count", "aggregate"):
            cells.append("native" if getattr(native, attr) else "emulated")
        writes = "read only"
        if caps.writable:
            writes = "queued" if caps.is_async_write else "direct"
        _echo(f"{name:<14}{provider_name:<20}" + "".join(f"{c:<11}" for c in cells) + writes)


@app.command()
def routes() -> None:
    """List every URL the application serves."""
    from app.main import create_app

    application = create_app()
    rows = sorted(set(_walk_routes(application.routes)))
    for path, methods, name in rows:
        _echo(f"{methods:<14} {path:<50} {name}")
    _echo(f"\n{len(rows)} routes.")


def _walk_routes(routes, prefix: str = "") -> list[tuple[str, str, str]]:
    """Flatten a route tree into (path, methods, name).

    Included routers are not flattened into ``app.routes`` -- they are wrapped,
    with the real routes on ``original_router`` and any prefix on the include
    context -- so this has to recurse rather than iterate once.
    """
    found: list[tuple[str, str, str]] = []
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            context = getattr(route, "include_context", None)
            found.extend(
                _walk_routes(nested.routes, prefix + getattr(context, "prefix", ""))
            )
            continue

        raw_methods = getattr(route, "methods", None) or set()
        methods = ",".join(sorted(set(raw_methods) - {"HEAD"}))
        if not methods:
            # A mount or a websocket; not what a reader of this list wants.
            continue
        found.append(
            (prefix + getattr(route, "path", ""), methods, getattr(route, "name", ""))
        )
    return found


@app.command()
def token(
    name: str = typer.Argument(..., help="A label for the token."),
    roles: str = typer.Option("user", help="Comma-separated roles."),
) -> None:
    """Create an API token. The value is shown once and cannot be recovered."""
    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.auth.api_token import generate_token, hash_token
    from app.core.connections import ConnectionRegistry
    from app.schema import api_tokens

    settings = get_settings()
    connections = ConnectionRegistry.from_file(settings.connections_path)
    url = connections.spec("db.main").option("url", required=True)

    raw = generate_token()
    role_list = [r.strip() for r in roles.split(",") if r.strip()]

    async def go():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(api_tokens).values(
                        name=name,
                        token_hash=hash_token(raw),
                        roles=json.dumps(role_list),
                        is_active=True,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(go())

    typer.secho("\nToken created. It cannot be shown again:\n", bold=True)
    typer.secho(f"  {raw}\n", fg=typer.colors.GREEN)
    _echo(f'Use it as:  curl -H "Authorization: Bearer {raw}" http://localhost:8000/api/...')


@app.command()
def migrate(
    revision: str = typer.Argument("head", help="Target revision, or 'base' to undo all."),
    sql: bool = typer.Option(False, "--sql", help="Print the SQL instead of running it."),
) -> None:
    """Bring the database up to date.

    Wraps alembic so the URL comes from connections.yaml rather than being
    configured twice.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    try:
        if revision == "base":
            command.downgrade(cfg, "base", sql=sql)
        else:
            command.upgrade(cfg, revision, sql=sql)
    except Exception as exc:
        _fail(f"migration failed: {exc}")
    if not sql:
        _echo("Database is up to date.")


@app.command("make-migration")
def make_migration(
    message: str = typer.Argument(..., help="What this change does."),
    empty: bool = typer.Option(False, "--empty", help="Write a blank migration to fill in."),
) -> None:
    """Generate a migration from the difference between code and database.

    Read what it produces before committing it: autogenerate is good at columns
    and indexes, and poor at anything that needs data moved.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    try:
        command.revision(cfg, message=message, autogenerate=not empty)
    except Exception as exc:
        _fail(f"could not generate a migration: {exc}")


@app.command("notify-due")
def notify_due(
    limit: int = typer.Option(200, help="Most notifications to deliver in one pass."),
) -> None:
    """Deliver scheduled notifications that have come due.

    Run from cron or a scheduler, as often as your reminders need to be
    punctual. It is a sweep rather than a timer, so it holds no state and
    several instances can run without stepping on one another.
    """
    from app.main import build_channels, build_registry
    from app.notify import notifier

    settings = get_settings()
    registry = build_registry(settings)

    async def go():
        await registry.bind()
        notifier.use(build_channels(settings))
        if not registry.has_resource("notifications"):
            _fail("No 'notifications' resource is registered.")
        notifier.bind(registry.resource("notifications").provider)
        # Delivered inline: a command that exits before its background tasks
        # finish would deliver nothing.
        notifier.background = False
        try:
            return await notifier.deliver_due(limit=limit)
        finally:
            await registry.close()

    delivered = asyncio.run(go())
    _echo(f"Delivered {delivered} notification(s).")


@app.command("notify-test")
def notify_test(
    recipient: str = typer.Argument(..., help="Who to notify."),
    title: str = typer.Option("Test notification", help="The message."),
    priority: str = typer.Option("high", help="low, normal, high or urgent."),
) -> None:
    """Send one notification, to check the channels are configured."""
    from app.main import build_channels, build_registry
    from app.notify import Notification, notifier

    settings = get_settings()
    registry = build_registry(settings)

    async def go():
        await registry.bind()
        notifier.use(build_channels(settings))
        notifier.background = False
        if registry.has_resource("notifications"):
            notifier.bind(registry.resource("notifications").provider)
        try:
            for name, healthy, detail in await notifier.health():
                mark = "ok  " if healthy else "FAIL"
                _echo(f"  {mark} {name:<10} {detail}")
            await notifier.send(
                Notification(recipient=recipient, title=title, priority=priority)
            )
        finally:
            await registry.close()

    asyncio.run(go())
    _echo(f"\nSent to {recipient}.")


@app.command()
def passwd(
    email: str = typer.Argument(..., help="The account to set a password for."),
    password: str = typer.Option("", help="Leave empty to generate one."),
    must_change: bool = typer.Option(
        True, help="Require the holder to choose their own at the next sign-in."
    ),
) -> None:
    """Set an account's password.

    The way back in when nobody can sign in to use the reset action, so it
    deliberately needs no running server and no existing session -- only the
    database and the configuration.
    """
    from app.auth.base import AuthError
    from app.auth.local import generate_password
    from app.auth.session import SessionStore
    from app.main import build_auth, build_registry

    settings = get_settings()
    registry = build_registry(settings)

    async def go():
        await registry.bind()
        try:
            chain = build_auth(settings, registry, SessionStore(settings.secret_key))
            provider = next(
                (p for p in chain if p.capabilities.manages_passwords), None
            )
            if provider is None:
                return None, (
                    "No configured auth provider stores passwords. Under SSO or "
                    "behind a gateway, change the password with your identity "
                    "provider instead."
                )
            user = await provider.find_user(email)
            if user is None:
                return None, f"No account with the address {email!r}."
            chosen = password or generate_password()
            try:
                await provider.set_password(str(user.pk), chosen, must_change=must_change)
            except AuthError as exc:
                # The password policy, reported as a message rather than a
                # traceback: it is the user's input that was wrong, not the code.
                return None, exc.message
            return chosen, ""
        finally:
            await registry.close()

    chosen, problem = asyncio.run(go())
    if problem:
        _fail(problem)
        return

    if password:
        _echo(f"Password set for {email}.")
    else:
        _echo(f"Password for {email}:  {chosen}")
        _echo("Shown once. It is stored only as a hash.")
    if must_change:
        _echo("They will be asked to choose their own at the next sign-in.")


@app.command("new-resource")
def new_resource(
    name: str = typer.Argument(..., help="Plural resource name, e.g. invoices."),
    module: str = typer.Option("", help="Module to add it to. Defaults to a new one."),
) -> None:
    """Print a resource declaration to start from."""
    module_name = module or f"crm_{name}"
    singular = name[:-1] if name.endswith("s") else name

    _echo(f'''"""{name.capitalize()}."""

from app.core.registry import Registry
from app.fields.types import DateField, SelectField, TextAreaField, TextField
from app.resources.resource import Resource
from app.resources.views import Column, FormView, ListView, Section

MANIFEST = {{
    "name": "{module_name}",
    "label": "{name.capitalize()}",
    "depends": ("core_identity",),
}}


def register(registry: Registry) -> None:
    registry.add_resource(
        Resource(
            "{name}",
            provider="db.main#{name}",
            icon="◆",
            menu_group="Records",
            display_field="name",
            default_sort=["name"],
            fields=[
                TextField("id", in_form=False, in_list=False, in_detail=False),
                TextField("name", required=True, searchable=True, inline_editable=True),
                SelectField("status", choices=["draft", "open", "closed"], in_filter=True),
                DateField("due_on", label="Due"),
                TextAreaField("notes"),
            ],
            views=[
                ListView(
                    columns=[Column("name", link=True), "status", "due_on"],
                    bulk_actions=["delete"],
                ),
                FormView([
                    Section("{singular.capitalize()}", ["name", "status", "due_on"], columns=2),
                    Section("Detail", ["notes"], columns=1),
                ]),
            ],
        )
    )
''')
    _echo(f"# Save as modules/{module_name}/__init__.py, then add a '{name}' table.",
          err=True)


if __name__ == "__main__":
    app()
