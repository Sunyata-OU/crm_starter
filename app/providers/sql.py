"""A provider backed by a SQL table.

Fully capable: filtering, sorting, searching, pagination, counting and
aggregation all happen in the database, so the capability shim steps aside
entirely and large tables page without loading rows into the application.

Tables are reflected on first use rather than declared, which is what lets a
resource point at an existing schema without a model class being written for it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date as date_type
from datetime import datetime as datetime_type
from datetime import time as time_type
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    Table,
    case,
    delete,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy import (
    and_ as sa_and,
)
from sqlalchemy import (
    not_ as sa_not,
)
from sqlalchemy import (
    or_ as sa_or,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql import ColumnElement, Select
from sqlalchemy.sql.selectable import Subquery

from app.core.connections import ConnectionSpec, register_connection
from app.core.errors import (
    ConfigError,
    ConflictError,
    NotFound,
    ProviderError,
    UnsupportedOperation,
)
from app.core.instrument import instrument_engine
from app.core.query import (
    SEQUENCE_OPS,
    UNARY_OPS,
    Agg,
    AggSpec,
    Condition,
    Filter,
    ListQuery,
    Not,
    Op,
    Page,
    Sort,
    SortDir,
)
from app.core.registry import register_provider_factory
from app.core.results import Ctx, Record, WriteResult
from app.providers.base import FULL, BaseProvider, Rows

# -- connection ------------------------------------------------------------

#: What the provider composes queries onto: a reflected table, or a derived
#: target aliased to look like one. Both carry named columns, which is all any
#: of the query building below asks of them.
TableLike = Table | Subquery



#: Selectables standing in where the database has no table to point at, by
#: name. A module registers one when the rows a screen needs are a join or a
#: calculation over tables this application is not allowed to change -- see
#: :func:`register_selectable`.
_SELECTABLES: dict[str, DerivedTarget] = {}


class DerivedTarget:
    """A named SELECT a resource may name in place of a table.

    ``build`` is handed the reflected tables it asked for and returns a
    statement; the result is aliased under ``name``, so everything downstream --
    filters, sorting, grouping, the shim, the views -- sees a table like any
    other and nothing above the provider knows the difference.
    """

    def __init__(
        self,
        name: str,
        tables: Sequence[str],
        build: Any,
        *,
        description: str = "",
    ) -> None:
        self.name = name
        self.tables = tuple(tables)
        self.build = build
        self.description = description


def register_selectable(
    name: str,
    *,
    tables: Sequence[str],
    build: Any,
    description: str = "",
) -> None:
    """Declare ``name`` as a SELECT over ``tables`` rather than a table itself.

    The reason this exists rather than "create a view": a database read
    through a provider is frequently owned by whatever service migrates it, so
    this application may read it and may not add anything to it. A derived
    target keeps the definition here, in the module that needs it, versioned
    with the screen it feeds -- and read-only by construction, because a
    provider built on one refuses every write.
    """
    _SELECTABLES[name] = DerivedTarget(name, tables, build, description=description)


def selectable(name: str) -> DerivedTarget | None:
    """The derived target registered under ``name``, if there is one."""
    return _SELECTABLES.get(name)


class SQLConnection:
    """An async engine plus the tables reflected from it so far."""

    def __init__(self, engine: AsyncEngine, *, schema: str | None = None) -> None:
        self.engine = engine
        # Here rather than in the factory below, so an engine built by hand --
        # a test, a script, an embedding application -- is counted too.
        instrument_engine(engine)
        self.schema = schema
        self._tables: dict[str, TableLike] = {}

    async def table(self, name: str) -> TableLike:
        """Reflect ``name`` once and cache it."""
        if name in self._tables:
            return self._tables[name]
        if (derived := selectable(name)) is not None:
            return await self._derived(derived)
        from sqlalchemy import MetaData

        metadata = MetaData(schema=self.schema)
        try:
            async with self.engine.connect() as conn:
                await conn.run_sync(metadata.reflect, only=[name], views=True)
        except SQLAlchemyError as exc:
            # A ProviderError, not a ConfigError: the commonest cause is a
            # database that is temporarily unreachable, which is a 502 the
            # operator should look outward for -- not a 500 implying a bug here.
            # A genuinely missing table produces the same class, with a message
            # that says so.
            raise ProviderError(
                f"could not read the definition of table {name!r}: {exc}. "
                f"Is the database reachable, and has the schema been created?"
            ) from exc
        key = f"{self.schema}.{name}" if self.schema else name
        # An explicit None check, not `a or b`: SQLAlchemy raises on the truth
        # value of a Table, since `bool(table)` has no sensible meaning.
        table = metadata.tables.get(key)
        if table is None:
            table = metadata.tables.get(name)
        if table is None:
            raise ConfigError(
                f"table {name!r} was not found in the database. Check the "
                f"resource's provider reference, or run 'crm migrate'."
            )
        self._tables[name] = table
        return table

    async def _derived(self, target: DerivedTarget) -> TableLike:
        """Build a registered SELECT and cache it under its name.

        Its sources are reflected through this same method, so a derived target
        costs exactly the catalogue queries its tables do and shares their
        cache with every other screen on this connection.
        """
        sources = {name: await self.table(name) for name in target.tables}
        try:
            statement = target.build(sources)
        except SQLAlchemyError as exc:
            raise ConfigError(
                f"the derived target {target.name!r} could not be built: {exc}"
            ) from exc
        # An alias, not the SELECT itself: the provider composes filters,
        # ordering and grouping onto whatever it is given, and only something
        # with named columns can carry that.
        built = statement.subquery(target.name)
        self._tables[target.name] = built
        return built

    async def close(self) -> None:
        await self.engine.dispose()


@register_connection(
    "sqlalchemy",
    close=lambda conn: conn.close(),
    check=lambda conn: _check_sql(conn),
)
async def open_sqlalchemy(spec: ConnectionSpec) -> SQLConnection:
    url = spec.option("url", required=True)
    options: dict[str, Any] = {
        "echo": spec.flag("echo", False),
        # Checks a pooled connection before handing it out. Costs one round
        # trip; saves the first request after a database restart or an idle
        # timeout from failing.
        "pool_pre_ping": spec.flag("pool_pre_ping", True),
    }
    # SQLite ignores pool sizing and rejects the arguments, so they are only
    # passed to backends that pool.
    if not url.startswith("sqlite"):
        options.update(
            pool_size=spec.number("pool_size", 10),
            max_overflow=spec.number("max_overflow", 20),
            # Recycle below any proxy or database idle timeout, or a pooled
            # connection is eventually handed out already dead.
            pool_recycle=spec.number("pool_recycle", 1800),
            pool_timeout=spec.number("pool_timeout", 30),
        )
    options.update(spec.option("engine_options") or {})

    engine = create_async_engine(url, **options)
    return SQLConnection(engine, schema=spec.option("schema"))


async def _check_sql(conn: SQLConnection) -> tuple[bool, str]:
    async with conn.engine.connect() as c:
        await c.execute(text("SELECT 1"))
    return True, f"connected to {conn.engine.url.render_as_string(hide_password=True)}"


# -- filter translation ----------------------------------------------------


#: Expressions standing in for columns, by resource. Populated when a provider
#: is built, because a `CaseField` is declared on the resource and compiled
#: here -- the field knows what it means, the provider knows how to say it.
Derived = Mapping[str, Any]


def _column(table: TableLike, name: str, derived: Derived | None = None) -> ColumnElement[Any]:
    if derived and name in derived:
        return derived[name]
    try:
        return table.c[name]
    except KeyError:
        columns = ", ".join(c.name for c in table.c)
        raise ProviderError(
            f"table {table.name!r} has no column {name!r}; columns are: {columns}"
        ) from None


def build_derived(table: TableLike, resource: Any) -> dict[str, Any]:
    """Compile every `CaseField` the resource declares into a CASE expression.

    Done once per provider rather than per query: the branches are static, so
    the expression is too, and rebuilding it on every list would be work for
    nothing.
    """
    from app.fields.types import CaseField

    compiled: dict[str, Any] = {}
    for field in getattr(resource, "fields", ()):
        if not isinstance(field, CaseField):
            continue
        branches = []
        for when, value in field.cases:
            # A string branch is SQL written by the module author -- see
            # `CaseField` on why that door exists and how narrow it is.
            condition = text(when) if isinstance(when, str) else build_filter(table, when)
            if condition is None:
                continue
            # The *rank*, not the label: this column is selected to be sorted
            # and grouped by, and sorting labels would sort them alphabetically.
            branches.append((condition, field.rank(value)))
        if not branches:
            continue
        compiled[field.name] = case(
            *branches, else_=field.rank(field.default)
        ).label(field.name)
    return compiled


def build_condition(table: TableLike, cond: Condition) -> ColumnElement[bool]:
    """Translate one condition into a SQL expression.

    Values are coerced to the column's own type first. A filter value is text
    far more often than it looks: it arrives from a query string, from a CSV
    export repeating a filter, and from the relation-label lookup, which
    collects foreign keys with ``str(value)`` and asks for ``pk IN (...)``.
    SQLite compares ``'335'`` to an integer column happily; PostgreSQL matches
    nothing, so every relation on the page renders as a raw key instead of a
    name -- and silently, because the label lookup swallows its exceptions by
    design so a failed lookup cannot break the list it decorates.
    """
    col = _column(table, cond.field)
    value = _coerce_value(col, cond.op, cond.value)
    match cond.op:
        case Op.EQ:
            return col.is_(None) if value is None else col == value
        case Op.NE:
            return col.is_not(None) if value is None else col != value
        case Op.LT:
            return col < value
        case Op.LTE:
            return col <= value
        case Op.GT:
            return col > value
        case Op.GTE:
            return col >= value
        case Op.IN:
            return col.in_(list(value))
        case Op.NOT_IN:
            return col.not_in(list(value))
        case Op.IS_NULL:
            return col.is_(None)
        case Op.NOT_NULL:
            return col.is_not(None)
        case Op.BETWEEN:
            low, high = tuple(value)
            return col.between(low, high)
        case Op.CONTAINS:
            return col.like(f"%{_escape_like(value)}%", escape="\\")
        case Op.ICONTAINS:
            return col.ilike(f"%{_escape_like(value)}%", escape="\\")
        case Op.STARTSWITH:
            return col.ilike(f"{_escape_like(value)}%", escape="\\")
        case Op.ENDSWITH:
            return col.ilike(f"%{_escape_like(value)}", escape="\\")
    raise UnsupportedOperation(f"operator {cond.op!r} has no SQL translation")


#: How a boolean arrives as text. Spelled out here rather than imported from
#: the field layer, which providers deliberately do not depend on.
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off", "n", "f"})

#: Operators whose value is a search pattern rather than a comparable. Coercing
#: "12" to an int here would turn a substring search into an equality test.
_PATTERN_OPS = frozenset({Op.CONTAINS, Op.ICONTAINS, Op.STARTSWITH, Op.ENDSWITH})


def _coerce_value(column: ColumnElement[Any], op: Op, value: Any) -> Any:
    """Convert a filter value to the column's type, elementwise where needed."""
    if op in _PATTERN_OPS or op in UNARY_OPS:
        return value
    if op in SEQUENCE_OPS:
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_coerce_to_column(column, v) for v in value]
        return value
    return _coerce_to_column(column, value)


def _coerce_to_column(column: ColumnElement[Any], value: Any) -> Any:
    """Convert one text value to the type its column compares against.

    Text is what reaches a query from a URL, a form or another provider's keys;
    comparing it to a typed column returns nothing on strict backends such as
    PostgreSQL. A value that will not convert is passed through untouched, so a
    genuinely bad filter reaches the backend as itself rather than being
    silently rewritten into one that matches something else.
    """
    if not isinstance(value, str):
        return value
    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return value

    if python_type is bool:
        lowered = value.strip().casefold()
        if lowered in _TRUE_TOKENS:
            return True
        if lowered in _FALSE_TOKENS:
            return False
        return value
    if python_type is int:
        try:
            return int(value)
        except ValueError:
            return value
    if python_type is Decimal:
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    return _to_temporal(python_type, value)


def _to_temporal(python_type: Any, value: str) -> Any:
    """Read an ISO string as the temporal type a column compares against.

    A date given for a timestamp column widens to midnight -- what a caller
    bounding a day meant by it -- and a timestamp given for a date column keeps
    the date and drops the clock. A non-temporal column, or a string that will
    not parse, comes back untouched, so the backend names the type it cannot
    compare rather than this function inventing a date.
    """
    try:
        if python_type is date_type:
            return date_type.fromisoformat(value[:10])
        if python_type is datetime_type:
            return datetime_type.fromisoformat(value.replace(" ", "T").replace("Z", "+00:00"))
        if python_type is time_type:
            return time_type.fromisoformat(value)
    except ValueError:
        return value
    return value


def _escape_like(value: Any) -> str:
    """Neutralise LIKE wildcards in user input.

    Without this, a search for "50%" would match far more than intended -- a
    correctness bug, and on a large table a performance one.
    """
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_filter(table: TableLike, f: Filter | None) -> ColumnElement[bool] | None:
    """Translate a filter tree into a SQL WHERE expression."""
    if f is None:
        return None
    if isinstance(f, Condition):
        return build_condition(table, f)
    if isinstance(f, Not):
        inner = build_filter(table, f.child)
        return None if inner is None else sa_not(inner)
    parts = [p for p in (build_filter(table, c) for c in f.children) if p is not None]
    if not parts:
        return None
    return sa_and(*parts) if f.op == "and" else sa_or(*parts)


def build_search(table: TableLike, term: str, fields: tuple[str, ...]) -> ColumnElement[bool] | None:
    """Every word must appear in at least one searched column."""
    words = [w for w in term.split() if w]
    if not words or not fields:
        return None
    columns = [_column(table, name) for name in fields if name in table.c]
    if not columns:
        return None
    clauses = [
        sa_or(*[c.ilike(f"%{_escape_like(word)}%", escape="\\") for c in columns])
        for word in words
    ]
    return sa_and(*clauses)


def build_order(
    table: TableLike, sorts: tuple[Sort, ...], derived: Derived | None = None
) -> list[ColumnElement[Any]]:
    order: list[ColumnElement[Any]] = []
    for s in sorts:
        if s.field not in table.c and not (derived and s.field in derived):
            continue
        col = _column(table, s.field, derived)
        # NULLs last in both directions matches what the local engine does, so
        # a list looks the same whichever backend serves it.
        order.append(col.desc().nullslast() if s.dir is SortDir.DESC else col.asc().nullslast())
    return order


# -- provider --------------------------------------------------------------


class SQLProvider(BaseProvider):
    """Reads and writes one table through an async engine."""

    def __init__(
        self,
        connection: SQLConnection,
        table_name: str,
        *,
        name: str = "",
        pk_field: str = "id",
        searchable_fields: tuple[str, ...] = (),
        read_only: bool = False,
        resource: Any = None,
    ) -> None:
        self.connection = connection
        self.table_name = table_name
        #: The resource, kept only to compile its derived expressions once the
        #: table has been reflected -- which cannot happen at construction.
        self.resource = resource
        self._derived: dict[str, Any] | None = None
        self.name = name or f"sql:{table_name}"
        self.pk_field = pk_field
        self.capabilities = FULL.replace(
            searchable_fields=searchable_fields,
            **({"write": False, "delete": False} if read_only else {}),
        )
        self._table: TableLike | None = None

    async def table(self) -> TableLike:
        if self._table is None:
            self._table = await self.connection.table(self.table_name)
        return self._table

    async def _writable_table(self) -> Table:
        """The table a write goes to, which a derived target is not.

        A derived target is a SELECT: the factory already builds a read-only
        provider over one, so nothing should reach here. Saying it again where
        the type narrows costs a line and turns "somebody bypassed the
        capability" from an AttributeError deep in SQLAlchemy into the refusal
        it was meant to be.
        """
        table = await self.table()
        if not isinstance(table, Table):
            raise UnsupportedOperation(
                f"{self.table_name!r} is a derived target and cannot be written to"
            )
        return table

    async def derived(self) -> dict[str, Any]:
        """Expressions standing in for columns the table does not have."""
        if self._derived is None:
            self._derived = (
                build_derived(await self.table(), self.resource) if self.resource else {}
            )
        return self._derived

    def _pk_column(self, table: TableLike) -> ColumnElement[Any]:
        if self.pk_field in table.c:
            return table.c[self.pk_field]
        # Fall back to the declared primary key when the resource did not name
        # one. A derived target has none to declare, so the resource naming a
        # key column is the only thing that can address its rows -- and saying
        # that is more use than an AttributeError from SQLAlchemy.
        # A Table answers with a PrimaryKeyConstraint and a subquery with a
        # bare ColumnSet, so ask for the columns and settle for the thing
        # itself -- an empty set, in the derived case, which is the point.
        declared = getattr(table, "primary_key", None)
        columns = getattr(declared, "columns", declared)
        primary = list(columns) if columns is not None else []
        if not primary:
            raise ProviderError(
                f"{table.name!r} has no primary key to address rows by; "
                f"the resource must name one that its columns include"
            )
        return primary[0]

    def _selected(
        self, table: TableLike, q: ListQuery, derived: Derived | None = None
    ) -> Sequence[ColumnElement[Any]]:
        """Columns to select, always including the primary key.

        Narrowing to the fields a view needs matters on wide tables; keeping
        the key means the rendered rows can still link to their detail page.
        Derived expressions ride along: they are the reason a screen can group
        by something no column holds.
        """
        extra = list((derived or {}).values())
        if not q.fields:
            return [*table.c, *extra]
        wanted = set(q.fields) | {self.pk_field}
        chosen = [c for c in table.c if c.name in wanted]
        chosen += [e for name, e in (derived or {}).items() if name in wanted]
        return chosen or [*table.c, *extra]

    def _apply_where(self, stmt: Select, table: TableLike, q: ListQuery) -> Select:
        where = build_filter(table, q.effective_filter)
        if where is not None:
            stmt = stmt.where(where)
        if q.search:
            search = build_search(table, q.search, self.capabilities.searchable_fields)
            if search is not None:
                stmt = stmt.where(search)
        return stmt

    # -- reads --------------------------------------------------------------

    async def warm(self) -> None:
        """Reflect the table now rather than on the first request.

        Reflection is a handful of catalogue queries per table. Left until the
        first request, they land on a user and show up as one slow page after
        every deploy; done here they cost a moment of startup and confirm the
        table exists while there is still a log nobody is waiting on.
        """
        await self.table()

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        table = await self.table()
        derived = await self.derived()
        stmt = select(*self._selected(table, q, derived))
        stmt = self._apply_where(stmt, table, q)

        # Without a deterministic order, pagination can repeat or skip rows
        # between requests, so fall back to the primary key.
        order = build_order(table, q.sort, derived) or [self._pk_column(table)]
        stmt = stmt.order_by(*order)

        stmt = stmt.limit(q.page_size).offset(q.offset)

        try:
            async with self.connection.engine.connect() as conn:
                rows = (await conn.execute(stmt)).mappings().all()
                total = None
                if q.with_total:
                    count_stmt = self._apply_where(
                        select(func.count()).select_from(table), table, q
                    )
                    total = (await conn.execute(count_stmt)).scalar_one()
        except SQLAlchemyError as exc:
            raise ProviderError(f"query against {self.table_name!r} failed: {exc}") from exc

        items = [self._record(dict(row)) for row in rows]
        return Page(
            items=items,
            page=q.page,
            page_size=q.page_size,
            total=total,
            has_more=(q.offset + len(items) < total) if total is not None else len(items) >= q.page_size,
        )

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        table = await self.table()
        column = self._pk_column(table)
        stmt = select(table).where(column == self._coerce_pk(column, pk))
        try:
            async with self.connection.engine.connect() as conn:
                row = (await conn.execute(stmt)).mappings().first()
        except SQLAlchemyError as exc:
            raise ProviderError(f"lookup in {self.table_name!r} failed: {exc}") from exc
        return self._record(dict(row)) if row is not None else None

    @staticmethod
    def _coerce_pk(column: ColumnElement[Any], pk: Any) -> Any:
        """Convert a path parameter to the key column's type."""
        return _coerce_to_column(column, pk)

    async def aggregate(self, spec: AggSpec, ctx: Ctx) -> Rows:
        table = await self.table()
        group_cols = [_column(table, f) for f in spec.group_by]

        measures: list[ColumnElement[Any]] = []
        for m in spec.measures:
            match m.agg:
                case Agg.COUNT:
                    measures.append(func.count().label(m.alias))
                case Agg.SUM:
                    measures.append(func.sum(_column(table, m.field or "")).label(m.alias))
                case Agg.AVG:
                    measures.append(func.avg(_column(table, m.field or "")).label(m.alias))
                case Agg.MIN:
                    measures.append(func.min(_column(table, m.field or "")).label(m.alias))
                case Agg.MAX:
                    measures.append(func.max(_column(table, m.field or "")).label(m.alias))

        # select_from is not optional here. With no group-by columns and a
        # bare count(), the statement references no column of the table, so
        # SQLAlchemy cannot infer a FROM clause -- it emits "SELECT count(*)",
        # which returns 1 rather than the number of rows. Naming the table
        # explicitly is what makes an ungrouped aggregate correct.
        stmt = select(*group_cols, *measures).select_from(table)
        where = build_filter(table, spec.effective_filter)
        if where is not None:
            stmt = stmt.where(where)
        if group_cols:
            stmt = stmt.group_by(*group_cols)
        if spec.sort:
            stmt = stmt.order_by(*build_order(table, spec.sort))
        if spec.limit:
            stmt = stmt.limit(spec.limit)

        try:
            async with self.connection.engine.connect() as conn:
                rows = (await conn.execute(stmt)).mappings().all()
        except SQLAlchemyError as exc:
            raise ProviderError(f"aggregate over {self.table_name!r} failed: {exc}") from exc
        return [dict(r) for r in rows]

    # -- writes -------------------------------------------------------------

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        table = await self._writable_table()
        values = self._known_columns(table, data)
        if not values:
            return WriteResult.failure("Nothing to save.")
        stmt = insert(table).values(**values)
        try:
            async with self.connection.engine.begin() as conn:
                if self.connection.engine.dialect.insert_returning:
                    row = (await conn.execute(stmt.returning(*table.c))).mappings().first()
                    return WriteResult.success(self._record(dict(row)) if row else None)
                result = await conn.execute(stmt)
                new_pk = result.inserted_primary_key
                key = new_pk[0] if new_pk else values.get(self.pk_field)
        except IntegrityError as exc:
            raise ConflictError(_readable_integrity_error(exc)) from exc
        except SQLAlchemyError as exc:
            raise ProviderError(f"insert into {self.table_name!r} failed: {exc}") from exc
        created = await self.get(key, ctx)
        return WriteResult.success(created)

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        table = await self._writable_table()
        column = self._pk_column(table)
        key = self._coerce_pk(column, pk)
        values = self._known_columns(table, data, exclude_pk=True)
        if not values:
            existing = await self.get(key, ctx)
            if existing is None:
                raise NotFound(f"no record with {self.pk_field}={pk!r}")
            return WriteResult.success(existing, message="No changes.")

        stmt = update(table).where(column == key).values(**values)
        try:
            async with self.connection.engine.begin() as conn:
                result = await conn.execute(stmt)
                if result.rowcount == 0:
                    raise NotFound(f"no record with {self.pk_field}={pk!r}")
        except IntegrityError as exc:
            raise ConflictError(_readable_integrity_error(exc)) from exc
        except SQLAlchemyError as exc:
            raise ProviderError(f"update of {self.table_name!r} failed: {exc}") from exc
        return WriteResult.success(await self.get(key, ctx))

    async def update_if(
        self, pk: Any, data: dict[str, Any], expect: dict[str, Any], ctx: Ctx
    ) -> WriteResult | None:
        """A conditional UPDATE: the expectation becomes part of the WHERE.

        The database decides, in one statement, whether this writer wins. No
        row matched means the record no longer looks the way the caller
        expected -- another process got there first -- which is reported as
        None rather than as an error, because losing the race is a normal
        outcome and the caller's next move is to move on, not to retry.
        """
        table = await self._writable_table()
        column = self._pk_column(table)
        key = self._coerce_pk(column, pk)
        values = self._known_columns(table, data, exclude_pk=True)
        if not values:
            raise ProviderError("a conditional update needs at least one value to write")

        conditions = [column == key]
        for name, value in expect.items():
            if name not in table.c:
                raise ProviderError(
                    f"cannot condition an update on {name!r}: no such column in "
                    f"{self.table_name!r}"
                )
            expected = _adapt(table.c[name], value)
            # IS NULL, not "= NULL", which matches nothing in SQL.
            conditions.append(
                table.c[name].is_(None) if expected is None else table.c[name] == expected
            )

        stmt = update(table).where(*conditions).values(**values)
        try:
            async with self.connection.engine.begin() as conn:
                result = await conn.execute(stmt)
        except IntegrityError as exc:
            raise ConflictError(_readable_integrity_error(exc)) from exc
        except SQLAlchemyError as exc:
            raise ProviderError(f"update of {self.table_name!r} failed: {exc}") from exc

        if result.rowcount == 0:
            return None
        return WriteResult.success(await self.get(key, ctx))

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        table = await self._writable_table()
        column = self._pk_column(table)
        key = self._coerce_pk(column, pk)
        existing = await self.get(key, ctx)
        if existing is None:
            raise NotFound(f"no record with {self.pk_field}={pk!r}")
        try:
            async with self.connection.engine.begin() as conn:
                await conn.execute(delete(table).where(column == key))
        except IntegrityError as exc:
            raise ConflictError(
                "That record is still referenced by other records, so it cannot be deleted."
            ) from exc
        except SQLAlchemyError as exc:
            raise ProviderError(f"delete from {self.table_name!r} failed: {exc}") from exc
        return WriteResult.success(existing)

    def _known_columns(
        self, table: TableLike, data: dict[str, Any], *, exclude_pk: bool = False
    ) -> dict[str, Any]:
        """Keep only keys that are real columns, adapted to their types.

        Two jobs. Forms carry extra keys -- CSRF tokens, submit buttons,
        backref fields -- and passing those to SQLAlchemy raises rather than
        being ignored. And callers that never went through the form engine (an
        action, a seed script, the API) may hand over an ISO string where the
        column expects a date object, which strict drivers reject outright.
        """
        skip = {self.pk_field} if exclude_pk else set()
        return {
            key: _adapt(table.c[key], value)
            for key, value in data.items()
            if key in table.c and key not in skip
        }

    async def health(self) -> tuple[bool, str]:
        try:
            table = await self.table()
            async with self.connection.engine.connect() as conn:
                count = (await conn.execute(select(func.count()).select_from(table))).scalar_one()
            return True, f"{self.table_name}: {count} rows"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def _adapt(column: Any, value: Any) -> Any:
    """Coerce a value into what this column's type accepts.

    Only string-to-temporal conversion is needed in practice; SQLAlchemy
    handles everything else. Shares :func:`_to_temporal` with the filter path,
    so a date written and a date filtered on are read the same way.
    """
    if value is None or not isinstance(value, str):
        return value
    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return value
    return _to_temporal(python_type, value)


def _readable_integrity_error(exc: IntegrityError) -> str:
    """Turn a driver constraint message into something a user can act on."""
    detail = str(getattr(exc, "orig", exc))
    lowered = detail.lower()
    if "unique" in lowered or "duplicate" in lowered:
        return "A record with those details already exists."
    if "foreign key" in lowered:
        return "That refers to a record which does not exist."
    if "not null" in lowered:
        return "A required value is missing."
    return "That change conflicts with a constraint on the data."


@register_provider_factory("sqlalchemy")
def build_sql_provider(handle: SQLConnection, target: str, resource) -> SQLProvider:
    """Wire a resource declaring ``db.name#table`` to that table.

    ``target`` may also name a registered derived target, in which case the
    provider is read-only: there is no row behind a computed one to write back
    to, and the databases these are used over are not ours to write to anyway.
    """
    return SQLProvider(
        handle,
        target,
        name=f"sql:{target}",
        pk_field=resource.pk,
        searchable_fields=resource.searchable_fields(),
        read_only=selectable(target) is not None,
        resource=resource,
    )
