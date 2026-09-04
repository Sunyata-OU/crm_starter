"""Configuration.

Mostly here because of one bug worth never repeating: every documented
comma-separated environment variable was unparseable, because pydantic-settings
JSON-decodes complex types before validators run.
"""

from __future__ import annotations

import pytest

from app.settings import Settings


class TestCommaSeparatedLists:
    """Nobody writes a JSON array in an environment variable."""

    @pytest.mark.parametrize(
        ("variable", "attribute", "expected"),
        [
            ("CRM_AUTH_PROVIDERS", "auth_providers", ["api_token", "session"]),
            ("CRM_NOTIFY_CHANNELS", "notify_channels", ["api_token", "session"]),
            ("CRM_MODULES", "modules", ["api_token", "session"]),
            ("CRM_DEV_AUTH_ROLES", "dev_auth_roles", ["api_token", "session"]),
            ("CRM_PROXY_TRUSTED_IPS", "proxy_trusted_ips", ["api_token", "session"]),
        ],
    )
    def test_a_csv_env_var_is_split(self, monkeypatch, variable, attribute, expected):
        monkeypatch.setenv(variable, "api_token,session")
        assert getattr(Settings(), attribute) == expected

    def test_whitespace_around_entries_is_ignored(self, monkeypatch):
        monkeypatch.setenv("CRM_AUTH_PROVIDERS", " api_token , session ")
        assert Settings().auth_providers == ["api_token", "session"]

    def test_a_single_value_still_works(self, monkeypatch):
        monkeypatch.setenv("CRM_MODULES", "demo_crm")
        assert Settings().modules == ["demo_crm"]

    def test_an_empty_value_yields_an_empty_list(self, monkeypatch):
        monkeypatch.setenv("CRM_MODULES", "")
        assert Settings().modules == []

    def test_a_real_list_is_still_accepted(self):
        # Constructing Settings directly, as the tests do, must keep working.
        assert Settings(auth_providers=["session"]).auth_providers == ["session"]


class TestProductionChecks:
    def test_a_generated_secret_is_refused_in_production(self, monkeypatch):
        monkeypatch.delenv("CRM_SECRET_KEY", raising=False)
        problems = Settings(environment="production").check()
        assert any("SECRET_KEY" in p for p in problems)

    def test_dev_auth_is_refused_in_production(self):
        problems = Settings(environment="production", dev_auth=True).check()
        assert any("DEV_AUTH" in p for p in problems)

    def test_a_trusting_header_provider_without_an_allowlist_is_reported(self):
        problems = Settings(auth_providers=["proxy_header"], proxy_trusted_ips=[]).check()
        assert any("PROXY_TRUSTED_IPS" in p for p in problems)

    def test_a_development_default_configuration_is_quiet(self):
        assert Settings(environment="development").check() == []


class TestTheConfigurationReference:
    """docs/configuration.md must describe the settings that exist.

    Documentation drifts silently: a setting added without a doc entry is
    invisible to whoever has to deploy this, and a documented variable that no
    longer exists sends them looking for a typo in their own environment. Both
    are cheap to catch here and expensive to catch in a deployment.
    """

    #: Read by connections.yaml rather than by Settings, so they are documented
    #: without appearing on the model.
    EXTERNAL = {
        "DATABASE_URL", "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE",
        "CACHE_BACKEND", "REDIS_URL", "FILE_BACKEND", "FILE_ROOT", "SQL_ECHO",
        # The second database, likewise read by connections.yaml.
        "ARCHIVE_ENABLED", "ARCHIVE_URL",
    }

    def _documented(self) -> set[str]:
        import pathlib
        import re

        text = pathlib.Path("docs/configuration.md").read_text()
        return set(re.findall(r"`CRM_([A-Z0-9_]+)`", text))

    def test_every_setting_is_documented(self):
        undocumented = {n.upper() for n in Settings.model_fields} - self._documented()
        assert not undocumented, f"add these to docs/configuration.md: {sorted(undocumented)}"

    def test_no_documented_setting_has_been_removed(self):
        actual = {n.upper() for n in Settings.model_fields}
        stale = self._documented() - actual - self.EXTERNAL
        assert not stale, f"these no longer exist: {sorted(stale)}"
