"""The command line interface.

These exist because both bugs this file would have caught were invisible in
normal use: `check-connections` reported every connection as an unknown type
because no provider module had been imported, and `routes` listed four of
twenty-nine because included routers are nested rather than flattened.
"""

from __future__ import annotations

from typer.testing import CliRunner

from app.cli import _walk_routes
from app.cli import app as cli
from app.core.connections import registered_connection_types
from app.settings import get_settings

runner = CliRunner()


class TestProviderRegistration:
    """Connection types register on import, so something must import them."""

    def test_the_builtin_types_are_available(self):
        from app.providers import builtin

        types = builtin.ensure_registered()
        assert {"sqlalchemy", "rest", "amqp"} <= set(types)

    def test_importing_the_cli_is_enough(self):
        # A command that resolves connections.yaml must not need the caller to
        # have imported a provider module by hand.
        assert "sqlalchemy" in registered_connection_types()


class TestRouteWalking:
    def test_included_routers_are_flattened(self):
        from app.main import create_app
        from app.settings import Settings

        from .conftest import build_registry

        application = create_app(
            settings=Settings(secret_key="k" * 32, environment="test", modules=[]),
            registry=build_registry(),
        )
        found = _walk_routes(application.routes)
        paths = {path for path, _, _ in found}
        assert "/r/{resource_name}" in paths, "resource routes must not be missed"
        assert "/login" in paths
        assert len(found) > 20

    def test_mounts_are_skipped(self):
        from app.main import create_app
        from app.settings import Settings

        from .conftest import build_registry

        application = create_app(
            settings=Settings(secret_key="k" * 32, environment="test", modules=[]),
            registry=build_registry(),
        )
        assert all(methods for _, methods, _ in _walk_routes(application.routes))


class TestCommands:
    def test_help_lists_the_documented_commands(self):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for command in ("dev", "seed", "resources", "check-connections", "capabilities", "routes"):
            assert command in result.output

    def test_new_resource_prints_valid_python(self):
        import ast

        result = runner.invoke(cli, ["new-resource", "orders"])
        assert result.exit_code == 0
        ast.parse(result.output)

    def test_new_resource_output_names_the_resource(self):
        result = runner.invoke(cli, ["new-resource", "invoices"])
        assert '"invoices"' in result.output
        assert 'provider="db.main#invoices"' in result.output

    def test_routes_lists_the_resource_routes(self):
        result = runner.invoke(cli, ["routes"])
        assert result.exit_code == 0
        assert "/r/{resource_name}" in result.output
        assert "list_records" in result.output

    def test_resources_lists_what_is_registered(self):
        # No modules enabled means the platform and nothing else.
        result = runner.invoke(cli, ["resources"])
        assert result.exit_code == 0
        assert "users" in result.output and "roles" in result.output
        assert "contacts" not in result.output

    def test_resources_lists_the_modules_that_are_enabled(self, monkeypatch):
        monkeypatch.setenv("CRM_MODULES", "demo_crm,demo_sales")
        get_settings.cache_clear()
        try:
            result = runner.invoke(cli, ["resources"])
        finally:
            get_settings.cache_clear()
        assert result.exit_code == 0
        assert "contacts" in result.output and "deals" in result.output

    def test_resources_verbose_lists_fields(self):
        result = runner.invoke(cli, ["resources", "-v"])
        assert result.exit_code == 0
        assert "email" in result.output


class TestPerDatabaseMigrations:
    """Each database gets its own history and its own version table.

    Both halves matter and they fail differently. Sharing a version directory
    means one database's migrations run against another; sharing a version
    table means two databases at different revisions each believe they are at
    the other's.
    """

    def test_the_default_database_keeps_alembics_own_layout(self):
        """An existing single-database deployment must see no change at all."""
        from app.cli import _version_dir
        from app.core.placement import DEFAULT_CONNECTION

        assert _version_dir(DEFAULT_CONNECTION).name == "versions"

    def test_a_second_database_gets_its_own_directory(self):
        from app.cli import _version_dir

        assert _version_dir("db.archive").name == "versions_db_archive"

    def test_secondary_histories_sit_beside_the_main_one_not_inside_it(self):
        """Alembic walks a version location recursively.

        A subdirectory of ``versions/`` would be read back into the history it
        was meant to be separate from, which is the sort of thing that works
        until the second migration.
        """
        from app.cli import _version_dir
        from app.core.placement import DEFAULT_CONNECTION

        main = _version_dir(DEFAULT_CONNECTION)
        other = _version_dir("db.archive")
        assert main not in other.parents
        assert other.parent == main.parent

    def test_the_version_table_is_named_per_connection(self):
        import re

        # The same derivation env.py applies, asserted here so the two cannot
        # drift without a test failing.
        def version_table(name: str) -> str:
            if name == "db.main":
                return "alembic_version"
            return "alembic_version_" + re.sub(r"[^0-9a-zA-Z_]+", "_", name).strip("_").lower()

        assert version_table("db.main") == "alembic_version"
        assert version_table("db.archive") == "alembic_version_db_archive"

    def test_migrating_an_unknown_connection_says_so(self):
        result = runner.invoke(cli, ["migrate", "--connection", "db.nowhere"])
        assert result.exit_code == 1
        assert "unknown connection" in result.output

    def test_migrating_a_connection_with_no_tables_is_refused(self, tmp_path, monkeypatch):
        """A REST connection has nothing alembic can do."""
        config = tmp_path / "connections.yaml"
        config.write_text(
            "connections:\n"
            "  db.main:\n    type: sqlalchemy\n    url: sqlite+aiosqlite://\n"
            "  api.ref:\n    type: rest\n    base_url: http://example.test\n"
        )
        monkeypatch.setenv("CRM_CONNECTIONS_FILE", str(config))
        get_settings.cache_clear()
        try:
            result = runner.invoke(cli, ["migrate", "--connection", "api.ref"])
            assert result.exit_code == 1
            assert "holds no tables" in result.output
        finally:
            get_settings.cache_clear()

    def test_check_connections_loads_modules_first(self, tmp_path, monkeypatch):
        """A module may contribute a connection type of its own.

        Without the import, that type is unknown and its connection is reported
        as broken when the only thing wrong is that nobody loaded the code --
        which sends you looking at the server rather than at the setting.
        """
        import app.cli as cli_module

        called: list[object] = []

        def fake_select(enabled=None):
            called.append(enabled)
            return []

        monkeypatch.setattr(cli_module, "get_settings", get_settings)
        monkeypatch.setattr("app.core.modules.select", fake_select)

        config = tmp_path / "connections.yaml"
        config.write_text(
            "connections:\n  db.main:\n    type: sqlalchemy\n    url: sqlite+aiosqlite://\n"
        )
        monkeypatch.setenv("CRM_CONNECTIONS_FILE", str(config))
        get_settings.cache_clear()
        try:
            runner.invoke(cli, ["check-connections"])
        finally:
            get_settings.cache_clear()
        assert called, "check-connections did not load modules before reading connections"
