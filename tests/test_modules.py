"""Module discovery, ordering and extension.

The point of modules is that one can add to another's screens without editing
its source, and that removing it cleanly removes its additions. These tests pin
down the loading rules that make that safe.
"""

from __future__ import annotations

import types

import pytest

from app.core.errors import ConfigError, RegistryError
from app.core.modules import LoadedModule, Manifest, load_modules, resolve_order
from app.core.registry import Registry
from app.fields.types import TextField
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from app.resources.views import Column


def module(name: str, depends: tuple[str, ...] = (), optional: bool = False) -> LoadedModule:
    return LoadedModule(
        Manifest(name=name, depends=depends, optional=optional),
        types.SimpleNamespace(register=lambda registry: None),
        f"modules.{name}",
    )


class TestOrdering:
    def test_dependencies_load_first(self):
        order = resolve_order([module("b", ("a",)), module("a")])
        assert [m.name for m in order] == ["a", "b"]

    def test_a_chain_is_ordered(self):
        order = resolve_order([module("c", ("b",)), module("b", ("a",)), module("a")])
        assert [m.name for m in order] == ["a", "b", "c"]

    def test_independent_modules_load_deterministically(self):
        first = [m.name for m in resolve_order([module("z"), module("a"), module("m")])]
        second = [m.name for m in resolve_order([module("m"), module("z"), module("a")])]
        assert first == second

    def test_a_cycle_is_reported_rather_than_guessed_at(self):
        with pytest.raises(ConfigError, match="circular"):
            resolve_order([module("a", ("b",)), module("b", ("a",))])

    def test_a_missing_dependency_names_what_is_available(self):
        with pytest.raises(ConfigError, match="not found"):
            resolve_order([module("a", ("nonexistent",))])


class TestManifest:
    def test_a_dict_manifest_is_accepted(self):
        manifest = Manifest.coerce({"name": "x", "depends": ["y"]}, "fallback")
        assert manifest.name == "x" and manifest.depends == ("y",)

    def test_the_name_defaults_to_the_package(self):
        assert Manifest.coerce({}, "demo_crm").name == "demo_crm"

    def test_an_unknown_key_is_a_clear_error(self):
        # A typo in a manifest should say so, not be silently ignored.
        with pytest.raises(ConfigError, match="unknown keys"):
            Manifest.coerce({"nmae": "typo"}, "x")


class TestLoadingTheRealModules:
    def test_the_shipped_modules_load(self):
        registry = Registry()
        loaded = load_modules(registry, enabled=["demo_crm", "demo_sales"])
        # The platform is always there: enabling a module adds to the default
        # set rather than replacing it.
        assert {m.name for m in loaded} == {
            "core_identity", "core_access", "demo_crm", "demo_sales",
        }

    def test_dependencies_are_pulled_in_automatically(self):
        # demo_sales depends on demo_crm, which was not listed.
        registry = Registry()
        loaded = load_modules(registry, enabled=["demo_sales"])
        assert "demo_crm" in {m.name for m in loaded}

    def test_enabling_a_module_cannot_switch_the_platform_off(self):
        # Naming modules in the settings is additive. The alternative -- a
        # list that replaces the defaults -- makes "CRM_MODULES=demo_crm" an
        # application with no roles, permissions or audit trail, which is a
        # configuration nobody means to write.
        registry = Registry()
        loaded = load_modules(registry, enabled=["demo_crm"])
        assert {"core_identity", "core_access"} <= {m.name for m in loaded}

    def test_only_the_platform_loads_by_default(self):
        # The promise of the layout: an application starts with accounts and
        # access control and nothing else. Every business entity, demo ones
        # included, is opt-in.
        registry = Registry()
        loaded = load_modules(registry)
        assert {m.name for m in loaded} == {"core_identity", "core_access"}
        assert set(registry.resource_names) == {
            "users", "api_tokens", "roles", "permissions", "audit_log", "notifications",
            "jobs",
        }

    def test_an_unknown_module_name_is_reported(self):
        with pytest.raises(ConfigError, match="not found"):
            load_modules(Registry(), enabled=["no_such_module"])

    def test_loading_registers_the_expected_resources(self):
        registry = Registry()
        load_modules(registry, enabled=["demo_crm", "demo_sales"])
        assert {"companies", "contacts", "deals", "activities"} <= set(registry.resource_names)


class TestExtension:
    """A later module adjusting an earlier one's declaration."""

    @pytest.fixture
    def registry(self):
        registry = Registry()
        load_modules(registry, enabled=["demo_crm", "demo_sales"])
        return registry

    def test_a_module_can_add_a_field_to_another_resource(self, registry):
        # demo_sales adds a deals backref to companies, which demo_crm declared.
        assert "deals" in registry.resource("companies")

    def test_a_module_can_change_another_resources_view(self, registry):
        columns = [c.field for c in registry.resource("companies").view("list").columns]
        assert columns.index("industry") == columns.index("name") + 1

    def test_view_edits_apply_to_the_registered_instance(self):
        # Extension must mutate the shared spec, not a copy nobody renders.
        resource = Resource(
            "widgets",
            provider=MemoryProvider([]),
            fields=[TextField("id"), TextField("name")],
        )
        view = resource.view("list")
        view.add_columns(Column("extra"))
        assert "extra" in [c.field for c in resource.view("list").columns]

    def test_adding_a_duplicate_field_is_refused(self, registry):
        with pytest.raises(RegistryError, match="already has"):
            registry.resource("companies").add_field(TextField("name"))

    def test_registering_a_duplicate_resource_is_refused(self, registry):
        with pytest.raises(RegistryError, match="already registered"):
            registry.add_resource(
                Resource("companies", provider=MemoryProvider([]), fields=[TextField("id")])
            )


class TestMenu:
    def test_groups_are_ordered_as_the_manifests_ask(self):
        from app.core.results import Identity

        registry = Registry()
        load_modules(registry, enabled=["demo_crm", "demo_sales"])
        groups = registry.menu(Identity(subject="a", roles=frozenset({"admin"})))
        labels = [g.label for g in groups]
        assert labels.index("Records") < labels.index("Sales") < labels.index("Administration")

    def test_a_resource_the_caller_cannot_read_is_omitted(self):
        from app.core.results import Identity

        registry = Registry()
        load_modules(registry, enabled=["demo_crm"])
        menu = registry.menu(Identity(subject="a", roles=frozenset({"user"})))
        names = {item.resource for group in menu for item in group.items}
        assert "users" not in names, "an admin-only resource must not be advertised"
        assert "contacts" in names
