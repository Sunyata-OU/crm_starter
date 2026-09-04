"""Which database holds which table, and what the CLI does with the answer.

The mapping is derived rather than configured, so the risk is not that it is
wrong in an obvious way -- it is that it is subtly right for the default case
and wrong the moment a deployment splits. These assertions pin both halves: a
single-database registry must produce no placement at all, and a split one must
place every table exactly once.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table

from app.core.connections import ConnectionRegistry, ConnectionSpec
from app.core.placement import (
    DEFAULT_CONNECTION,
    metadata_for,
    placement,
    unresolved_foreign_keys,
)
from app.core.registry import Registry
from app.fields.types import TextField
from app.providers.union import Union, UnionSource
from app.resources.resource import Resource


def _connections(**types: str) -> ConnectionRegistry:
    registry = ConnectionRegistry()
    for name, kind in types.items():
        registry.add(ConnectionSpec(name.replace("__", "."), kind, {"url": "sqlite://"}))
    return registry


def _resource(name: str, provider) -> Resource:
    return Resource(name, provider=provider, fields=[TextField("id"), TextField("name")])


def _registry(*resources, connections=None) -> Registry:
    registry = Registry(connections or _connections(db__main="sqlalchemy"))
    for resource in resources:
        registry.add_resource(resource)
    return registry


class TestPlacement:
    def test_a_single_database_places_nothing(self):
        """The common case must stay the case that needs no configuration."""
        place = placement(_registry(_resource("deals", "db.main#deals")))
        assert place.claimed == {"deals": "db.main"}
        assert place.is_split() is False
        assert place.connections == (DEFAULT_CONNECTION,)

    def test_a_second_database_is_noticed(self):
        connections = _connections(db__main="sqlalchemy", db__archive="sqlalchemy")
        place = placement(
            _registry(
                _resource("deals", "db.main#deals"),
                _resource("old_deals", "db.archive#archived_deals"),
                connections=connections,
            )
        )
        assert place.connection_for("archived_deals") == "db.archive"
        assert place.is_split() is True

    def test_the_platform_database_is_listed_first(self):
        """``migrate --all`` walks these in order, and order matters."""
        connections = _connections(
            db__main="sqlalchemy", db__archive="sqlalchemy", db__reporting="sqlalchemy"
        )
        place = placement(
            _registry(
                _resource("a", "db.reporting#a"),
                _resource("b", "db.archive#b"),
                connections=connections,
            )
        )
        assert place.connections[0] == DEFAULT_CONNECTION

    def test_unclaimed_tables_belong_to_the_default(self):
        """Half the schema is not resource-backed and must still be created."""
        place = placement(_registry(_resource("deals", "db.main#deals")))
        assert place.connection_for("password_resets") == DEFAULT_CONNECTION

    def test_a_union_places_every_source(self):
        connections = _connections(db__main="sqlalchemy", db__archive="sqlalchemy")
        place = placement(
            _registry(
                _resource(
                    "all_deals",
                    Union(
                        UnionSource("db.main#deals", label="live"),
                        UnionSource("db.archive#archived_deals", label="archive"),
                    ),
                ),
                connections=connections,
            )
        )
        assert place.connection_for("deals") == "db.main"
        assert place.connection_for("archived_deals") == "db.archive"

    def test_a_connection_that_holds_no_tables_is_ignored(self):
        """A REST-backed resource has no table, so it places none.

        Without this, ``migrate --all`` would try to run alembic against an
        HTTP endpoint.
        """
        connections = _connections(db__main="sqlalchemy", api__ref="rest")
        place = placement(
            _registry(
                _resource("deals", "db.main#deals"),
                _resource("tickets", "api.ref#/tickets"),
                connections=connections,
            )
        )
        assert "/tickets" not in place.claimed
        assert place.connections == (DEFAULT_CONNECTION,)

    def test_a_resource_with_a_live_provider_places_nothing(self):
        from app.providers.memory import MemoryProvider

        place = placement(_registry(_resource("things", MemoryProvider([]))))
        assert place.claimed == {}

    def test_tables_on_selects_only_that_connection(self):
        connections = _connections(db__main="sqlalchemy", db__archive="sqlalchemy")
        place = placement(
            _registry(
                _resource("deals", "db.main#deals"),
                _resource("old", "db.archive#archived_deals"),
                connections=connections,
            )
        )
        known = ["deals", "archived_deals", "users"]
        assert place.tables_on("db.archive", known) == {"archived_deals"}
        # Everything unclaimed lands on the default, which is what makes a
        # partial declaration safe.
        assert place.tables_on("db.main", known) == {"deals", "users"}


class TestMetadataSubset:
    """Alembic compares a whole metadata, so narrowing it is how it is told
    about one database rather than all of them."""

    @pytest.fixture
    def schema(self) -> MetaData:
        md = MetaData()
        Table("deals", md, Column("id", Integer, primary_key=True))
        Table("archived_deals", md, Column("id", Integer, primary_key=True))
        Table("users", md, Column("id", Integer, primary_key=True), Column("email", String(60)))
        return md

    def test_keeps_only_the_named_tables(self, schema):
        subset = metadata_for(schema, {"archived_deals"})
        assert set(subset.tables) == {"archived_deals"}

    def test_leaves_the_source_alone(self, schema):
        metadata_for(schema, {"users"})
        assert set(schema.tables) == {"deals", "archived_deals", "users"}

    def test_copies_the_columns(self, schema):
        subset = metadata_for(schema, {"users"})
        assert {c.name for c in subset.tables["users"].columns} == {"id", "email"}


class TestForeignKeysAcrossDatabases:
    """A constraint that spans two databases is not a constraint.

    Reported rather than raised: splitting a schema and losing the constraint
    is a legitimate decision, but it should be a decision.
    """

    @pytest.fixture
    def schema(self) -> MetaData:
        from sqlalchemy import ForeignKey

        md = MetaData()
        Table("companies", md, Column("id", Integer, primary_key=True))
        Table(
            "archived_deals", md,
            Column("id", Integer, primary_key=True),
            Column("company_id", Integer, ForeignKey("companies.id")),
        )
        return md

    def test_a_key_within_one_database_is_fine(self, schema):
        from app.core.placement import Placement

        place = Placement(claimed={}, default="db.main")
        assert unresolved_foreign_keys(schema, place) == []

    def test_a_key_across_two_is_reported(self, schema):
        from app.core.placement import Placement

        place = Placement(claimed={"archived_deals": "db.archive"}, default="db.main")
        problems = unresolved_foreign_keys(schema, place)
        assert len(problems) == 1
        assert "archived_deals.company_id references companies" in problems[0]


class TestResourcesThatLiveOutsideADatabase:
    """A resource on Redis, an HTTP endpoint or a queue has no table.

    The "unclaimed tables belong to the default connection" rule is what makes
    a single-database deployment need no configuration, and it is exactly the
    rule that gets this wrong: left alone, it adopts the name and creates a
    permanently empty table in SQL for something deliberately not in SQL.
    """

    def _split_registry(self):
        connections = _connections(db__main="sqlalchemy", redis__jobs="redis")
        return _registry(
            _resource("deals", "db.main#deals"),
            _resource("jobs", "redis.jobs#jobs"),
            connections=connections,
        )

    def test_the_table_is_not_adopted_by_the_default(self):
        place = placement(self._split_registry())
        assert place.is_elsewhere("jobs")
        assert "jobs" not in place.tables_on("db.main", ["deals", "jobs", "users"])

    def test_the_rest_of_the_schema_is_unaffected(self):
        place = placement(self._split_registry())
        assert place.tables_on("db.main", ["deals", "jobs", "users"]) == {"deals", "users"}

    def test_one_such_resource_is_enough_to_need_narrowing(self):
        """Even with a single database: "create every declared table here" is
        already the wrong answer."""
        place = placement(self._split_registry())
        assert place.is_split() is False, "one database is still one database"
        assert place.needs_narrowing() is True

    def test_an_ordinary_deployment_still_narrows_nothing(self):
        place = placement(_registry(_resource("deals", "db.main#deals")))
        assert place.needs_narrowing() is False

    def test_a_table_placed_on_a_database_wins_over_a_reference_from_outside(self):
        """Two resources may read the same name from different places; if one
        of them is a real table, the table is real."""
        connections = _connections(db__main="sqlalchemy", api__ref="rest")
        registry = _registry(
            _resource("deals", "db.main#deals"),
            _resource("deals_api", "api.ref#deals"),
            connections=connections,
        )
        place = placement(registry)
        assert place.is_elsewhere("deals") is False
        assert place.connection_for("deals") == "db.main"
