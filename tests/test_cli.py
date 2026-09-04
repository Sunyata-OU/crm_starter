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
