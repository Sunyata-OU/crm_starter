"""What a module can ship besides Python registration.

A module's package directory may hold ``migrations/`` (revisions for the
default database that chain onto the framework's), ``templates/`` (which win
over the framework's), and a ``register_cli(app)`` function. These tests build
a throwaway module on disk and prove the framework picks each up.
"""

from __future__ import annotations

import importlib
import sys
import textwrap

import pytest
from typer.testing import CliRunner

from app.core.modules import LoadedModule, Manifest, module_path, module_paths
from app.settings import get_settings

runner = CliRunner()


@pytest.fixture
def shipping_module(tmp_path, monkeypatch):
    """An importable package ``shipping`` with all three extension points."""
    from alembic.script import ScriptDirectory

    pkg = tmp_path / "pkgs" / "shipping"
    (pkg / "migrations").mkdir(parents=True)
    (pkg / "templates" / "shipping").mkdir(parents=True)
    # Chain onto whatever the framework's head is today, so the test does not
    # go stale the next time a framework migration is added.
    from app.cli import ROOT

    heads = ScriptDirectory(str(ROOT / "migrations")).get_heads()
    assert len(heads) == 1, "the framework's own history should have one head"
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            """
            import typer

            MANIFEST = {"name": "shipping", "optional": True}

            def register(registry):
                pass

            def register_cli(app):
                @app.command("shipping-hello")
                def hello():
                    typer.echo("hello from shipping")
            """
        )
    )
    (pkg / "templates" / "shipping" / "page.html").write_text("from the module")
    (pkg / "migrations" / "20990101_0000_shipping.py").write_text(
        textwrap.dedent(
            f"""
            import sqlalchemy as sa
            from alembic import op

            revision = "aaaa00000001"
            down_revision = "{heads[0]}"
            branch_labels = None
            depends_on = None

            def upgrade():
                op.create_table("shipping_things", sa.Column("id", sa.Integer, primary_key=True))

            def downgrade():
                op.drop_table("shipping_things")
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))
    sys.modules.pop("shipping", None)
    module = importlib.import_module("shipping")
    loaded = LoadedModule(Manifest(name="shipping", optional=True), module, "shipping")
    monkeypatch.setattr("app.core.modules.select", lambda **_: [loaded])
    yield loaded
    sys.modules.pop("shipping", None)


class TestPaths:
    def test_a_package_finds_its_directories(self, shipping_module):
        assert module_path(shipping_module, "migrations").name == "migrations"
        assert module_path(shipping_module, "templates").name == "templates"

    def test_a_missing_directory_is_none(self, shipping_module):
        assert module_path(shipping_module, "nothing") is None

    def test_a_single_file_module_ships_nothing(self, tmp_path):
        import types

        single = types.ModuleType("single")
        single.__file__ = str(tmp_path / "single.py")
        loaded = LoadedModule(Manifest(name="single"), single, "single")
        assert module_paths([loaded], "migrations") == []


class TestMigrations:
    def test_a_modules_revisions_run_with_the_frameworks(self, shipping_module, tmp_path, monkeypatch):
        import sqlalchemy as sa

        from app.cli import app as cli

        db = tmp_path / "t.db"
        monkeypatch.setenv("CRM_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
        get_settings.cache_clear()
        try:
            result = runner.invoke(cli, ["migrate"])
        finally:
            get_settings.cache_clear()
        assert result.exit_code == 0, result.output
        tables = sa.inspect(sa.create_engine(f"sqlite:///{db}")).get_table_names()
        assert "shipping_things" in tables
        assert "tasks" in tables, "the framework's own migrations still ran"


class TestTemplates:
    def test_a_modules_templates_are_searched(self, shipping_module, settings):
        from app.main import create_app
        from tests.conftest import build_registry

        registry = build_registry()
        registry.loaded_modules = [shipping_module]
        app = create_app(settings=settings, registry=registry)
        source = app.state.crm.templates.env.loader.get_source(
            app.state.crm.templates.env, "shipping/page.html"
        )[0]
        assert source == "from the module"


class TestCommands:
    def test_a_modules_command_is_registered(self, shipping_module, monkeypatch):
        import typer

        from app.cli import _load_module_commands

        fresh = typer.Typer()

        @fresh.command("other")  # a lone command would be run without its name
        def other():
            pass

        monkeypatch.setattr("app.cli.app", fresh)
        _load_module_commands()
        result = runner.invoke(fresh, ["shipping-hello"])
        assert result.output.strip() == "hello from shipping"

    def test_a_broken_hook_is_reported_not_fatal(self, shipping_module, monkeypatch, capsys):
        import typer

        from app.cli import _load_module_commands

        def boom(app):
            raise RuntimeError("nope")

        monkeypatch.setattr(shipping_module.module, "register_cli", boom)
        monkeypatch.setattr("app.cli.app", typer.Typer())
        _load_module_commands()
        assert "could not add its commands" in capsys.readouterr().err
