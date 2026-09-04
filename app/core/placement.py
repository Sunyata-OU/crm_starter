"""Which database holds which table.

A resource says where it lives -- ``provider="db.analytics#orders"`` -- so the
mapping from table to database is already declared; it is just spread across
the modules rather than written down in one place. This gathers it, which is
what lets ``crm migrate`` and ``crm seed`` act on one database at a time
without a second configuration file to keep in step with the first.

The rule for tables nothing claims is the interesting part. Half the schema is
not resource-backed: reset tokens, timeline entries, the join tables. Rather
than make every module annotate them, the **default connection owns whatever
no other connection claims**. So a single-database deployment -- which is
nearly all of them -- needs no placement at all and behaves exactly as before,
while a split one only has to say where the tables that moved went.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from sqlalchemy import MetaData, Table

#: The connection a resource means when it names none, and the one that owns
#: every table no other connection claims.
DEFAULT_CONNECTION = "db.main"

#: Connection types that hold tables. A resource on a REST or queue connection
#: has no table to migrate, so it contributes nothing here.
TABLE_TYPES = frozenset({"sqlalchemy"})


def parse_ref(ref: str, *, default_target: str = "") -> tuple[str, str]:
    """Split ``"db.analytics#orders"`` into its connection and its target."""
    connection, _, target = ref.partition("#")
    return connection.strip(), (target.strip() or default_target)


def _refs(resource) -> Iterator[tuple[str, str]]:
    """Every ``connection#target`` pair one resource involves.

    A resource may reach more than one: a union names a source per backend, and
    a separate write provider names another. All of them need their table to
    exist, so all of them are placed.
    """
    from app.providers.union import Union

    ref = resource.provider_ref
    if isinstance(ref, Union):
        for source in ref.sources:
            yield parse_ref(source.ref, default_target=resource.name)
    elif isinstance(ref, str):
        yield parse_ref(ref, default_target=resource.name)

    write_ref = getattr(resource, "write_provider", None)
    if isinstance(write_ref, str) and write_ref:
        yield parse_ref(write_ref, default_target=resource.name)


@dataclass(frozen=True, slots=True)
class Placement:
    """Where each table lives, and which connections hold any at all."""

    #: Table name -> connection name, for tables a resource explicitly places.
    claimed: dict[str, str]
    #: The connection owning everything unclaimed.
    default: str
    #: Tables whose resource lives somewhere that has no tables at all -- a
    #: Redis keyspace, an HTTP endpoint, a queue. Kept separately because the
    #: "unclaimed belongs to the default" rule would otherwise create a table
    #: in SQL for a resource that is deliberately not in SQL: harmless-looking,
    #: permanently empty, and confusing to whoever finds it.
    elsewhere: frozenset[str] = frozenset()

    def connection_for(self, table: str) -> str:
        return self.claimed.get(table, self.default)

    def is_elsewhere(self, table: str) -> bool:
        """Whether this table belongs to no database at all."""
        return table in self.elsewhere

    def tables_on(self, connection: str, known: Iterable[str]) -> set[str]:
        """Of ``known``, the tables belonging to ``connection``."""
        return {
            t for t in known
            if t not in self.elsewhere and self.connection_for(t) == connection
        }

    @property
    def connections(self) -> tuple[str, ...]:
        """Every connection holding tables, the default one first.

        Ordered so ``crm migrate --all`` brings the platform database up first;
        a secondary database with a foreign key into it is rare but the reverse
        order is never what anyone wants.
        """
        rest = sorted(set(self.claimed.values()) - {self.default})
        return (self.default, *rest)

    def is_split(self) -> bool:
        """Whether more than one database holds tables."""
        return len(self.connections) > 1

    def needs_narrowing(self) -> bool:
        """Whether the full schema is the wrong answer for any one database.

        True when the schema is split, and also when something lives outside a
        database entirely -- one resource on Redis is enough to make "create
        every declared table here" wrong, even with a single database.
        """
        return self.is_split() or bool(self.elsewhere)


def placement(registry, *, default: str = DEFAULT_CONNECTION) -> Placement:
    """Read the table-to-connection map out of a registry's declarations.

    The registry need not be bound: this reads what was declared, not what was
    opened, which is what makes it usable from ``migrations/env.py`` where
    opening a connection would be circular.
    """
    connections = getattr(registry, "connections", None)
    claimed: dict[str, str] = {}
    elsewhere: set[str] = set()

    for resource in registry:
        for connection, target in _refs(resource):
            if not connection or not target:
                continue
            # A connection that holds no tables places nothing to migrate --
            # but it does mean the name is spoken for. Recording that is what
            # stops the default connection adopting it as an unclaimed table.
            if (
                connections is not None
                and connection in connections
                and connections.spec(connection).type not in TABLE_TYPES
            ):
                elsewhere.add(target)
                continue
            claimed[target] = connection

    # A table both placed on a database and named by a non-table connection is
    # on the database: something declared it there deliberately, and the other
    # reference is a second resource reading the same name from elsewhere.
    elsewhere -= set(claimed)
    return Placement(claimed=claimed, default=default, elsewhere=frozenset(elsewhere))


def metadata_for(source: MetaData, tables: set[str]) -> MetaData:
    """A copy of ``source`` holding only ``tables``.

    Alembic compares a whole ``MetaData`` against a whole database, so telling
    it about one database means handing it a metadata containing only that
    database's tables. Anything else and autogenerate proposes creating the
    other database's tables here.
    """
    out = MetaData()
    for name, table in source.tables.items():
        if name in tables:
            table.to_metadata(out)
    return out


def unresolved_foreign_keys(source: MetaData, place: Placement) -> list[str]:
    """Foreign keys pointing at a table in a different database.

    A split schema cannot enforce these, and the database will not say so --
    the constraint simply cannot be created, or is created against a table that
    is not there. Reported rather than raised: splitting a schema and dropping
    the constraint is a legitimate choice, made deliberately.
    """
    problems: list[str] = []
    for table in source.tables.values():
        here = place.connection_for(table.name)
        for fk in _foreign_keys(table):
            there = place.connection_for(fk[1])
            if here != there:
                problems.append(
                    f"{table.name}.{fk[0]} references {fk[1]} on {there!r}, "
                    f"but {table.name} is on {here!r}"
                )
    return problems


def _foreign_keys(table: Table) -> Iterator[tuple[str, str]]:
    for column in table.columns:
        for fk in column.foreign_keys:
            yield column.name, fk.column.table.name
