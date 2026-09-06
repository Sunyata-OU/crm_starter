# Declaring a resource

A resource is one screen's worth of declaration. Everything else is derived.

```python
Resource(
    "deals",
    provider="db.main#deals",
    fields=[...],
    views=[...],       # optional; sensible ones are generated
    actions=[...],     # optional
    policy=...,        # optional; defaults to "any signed-in user"
)
```

`provider` is a `connection#target` reference, an already-built provider
object, or a `Union(...)` of several — see
[Several databases](multiple-databases.md).

## Fields

Every field answers four questions: how a submitted string becomes a Python
value, what that value is validated against, how it renders, and which filter
operators make sense for it.

```python
TextField(
    "name",
    label="Deal name",       # defaults to a humanised field name
    required=True,
    help="Shown under the input",
    placeholder="Acme renewal",

    # where it appears
    in_list=True, in_form=True, in_detail=True, in_filter=False,
    sortable=True, searchable=True, inline_editable=True,

    # who may see or change it
    read_roles=["finance"], write_roles=["manager"],
)
```

### Types

| Group | Types |
| --- | --- |
| Text | `text` `textarea` `richtext` `email` `url` `phone` |
| Numeric | `integer` `decimal` `currency` `percent` `duration` |
| Boolean | `boolean` |
| Temporal | `date` `datetime` `time` |
| Choice | `select` `status` `multiselect` `tags` |
| Structured | `json` `color` `file` `image` |
| Relational | `relation` `backref` |
| Derived | `computed` |

`status` renders as a coloured pill and can group a board. `relation` renders
as a link with a typeahead input. `backref` is the many side — not stored,
resolved by querying the other resource, shown as an embedded list.

```python
RelationField("company_id", resource="companies", display="name")
BackrefField("contacts", resource="contacts", via="company_id",
             columns=("name", "email"))
```

Because a relation is resolved by a query rather than a join, the two resources
may live in entirely different backends.

### Dynamic choices

```python
SelectField("owner", choices=lambda ctx: load_owners(ctx))
```

Resolved per request, never cached across them.

## Views

Omit `views=` and reasonable list, form and detail views are generated from the
field flags. Declare them to take control.

```python
ListView(
    columns=[Column("name", link=True, width="30%"), "amount", "stage"],
    default_sort=["-amount"],
    row_actions=["mark_won"],
    bulk_actions=["mark_won", "delete"],
    inline_edit=True,
)

FormView([
    Section("Deal", ["name", "company_id"], columns=2),
    Section("Notes", ["notes"], columns=1),
], submit_label="Save deal")

BoardView(group_by="stage", card=Card(title="name", badges=["amount"]),
          sum_field="amount")

CalendarView(start_field="due_on", title_field="subject")

ChartView(group_by="stage", measure=Measure(Agg.SUM, "amount"), chart="column")

PivotView(rows=("owner",), columns=("stage",),
          measures=(Measure(Agg.SUM, "amount"),))

# Rows that expand. `parent_field` names this resource's own parent column,
# so the tree recurses; naming `child_resource` instead hangs a different
# resource under each row, one level deep.
TreeView(columns=[Column("name", link=True), "city"], parent_field="parent_id")
TreeView(parent_field="company_id", child_resource="contacts")

# Bars along a time axis. Both dates are required: a bar needs two, and
# inventing the second would draw a schedule the data does not claim.
GanttView(start_field="created_at", end_field="expected_close",
          group_by="stage", progress_field="probability", default_scale="month")

# Pins on a map, from a latitude/longitude pair.
MapView(lat_field="latitude", lon_field="longitude", title_field="name")

# Outstanding work, crossed by who owns it and what kind it is. States are
# computed against today, so nothing has to be swept overnight to stay true.
ActivityView(row_field="owner", activity_field="kind", due_field="due_on",
             done_filter=Condition("done", Op.EQ, False))

# Several views on one page. Each panel is fetched separately, from the same
# handler that serves it as a full page.
DashboardView(panels=[
    Panel(view="chart", span=1),
    Panel(view="activity", resource="activities", span=2),
], columns=2)
```

Views are switchable from the toolbar; the list route picks one from `?view=`.
`?view=kanban` reaches a board and `?view=hierarchy` a tree, because those are
the names people look for.

### How a view is put together

Every kind is the same three pieces, which is what makes adding another one
small:

| Piece | Where |
| --- | --- |
| The spec | a `View` subclass in `app/resources/views.py`, exposing `field_names` |
| The query | a `case` in the `match spec.kind` in `app/web/routes/resource.py` |
| The markup | `app/templates/views/<kind>.html`, overridable per resource |

A tree also has a fragment route, `/r/<resource>/<pk>/children`, which returns
one level at a time: a row's toggle asks for its children at `depth + 1`, and
those rows carry toggles of their own. The server never holds more than one
level, so depth costs nothing until someone opens it. A dashboard panel is the
ordinary view with `?panel=1`, which swaps the page shell for a bare layout --
the handler, the query, the permission check and the template are unchanged.

Maps are the one view that reaches outside: tiles come from
`CRM_MAP_TILE_URL`, which defaults to public OpenStreetMap. Empty it and the
map becomes a list of located records, which is what an air-gapped install
wants.

## Actions

```python
@action("mark_won", "Mark as won", style="primary",
        confirm="Mark this deal as won?",
        available=lambda record, identity: record["stage"] != "won")
async def mark_won(records, ctx, resource):
    for r in records:
        await resource.provider.update(r.pk, {"stage": "won"}, ctx)
    return ActionResult(message=f"Marked {len(records)} as won.")
```

The same handler serves a row button, a detail-page button and a bulk
operation — `records` is a list either way. `resource.registry` reaches other
resources, so an action can write through a different provider entirely.

## Policies

```python
Policy()          # any signed-in user, all operations
PublicPolicy()    # readable without signing in
ReadOnlyPolicy()  # no writes at all
RolePolicy(read=["staff"], write=["manager"])
OwnerPolicy("owner", identity_attr="email", bypass_roles=("admin", "manager"))
```

`OwnerPolicy` is the row-scoping case. It returns a filter, which is folded
into every query the resource serves — lists, detail reads, exports and the
aggregates behind charts alike.

Write your own by subclassing:

```python
class RegionPolicy(Policy):
    def scope(self, identity):
        region = identity.claims.get("region")
        return Condition("region", Op.EQ, region) if region else DENY_ALL
```

Return a filter, not a boolean, and it applies everywhere automatically.

## Search and filters

```python
search=SearchSpec(
    fields=("name", "notes"),               # what free-text search looks in
    filters=("stage", "owner", "source"),   # what the filter panel offers
    group_by=("stage", "owner"),
    value_aliases={"my_region": lambda identity: identity.claims["region"]},
)
```

Filters travel in the URL — `?f.stage=won&f.amount__gte=1000&q=acme` — so a
filtered list is a shareable link and the back button works. A parameter naming
an unknown field, an operator the field does not support, or a column the
caller may not read is dropped rather than passed through.

### Operators

Each field type declares the operators that make sense for it, and only those
are accepted:

| Operator | Reads as | Typical field |
| --- | --- | --- |
| `eq`, `ne` | is, is not | any |
| `lt`, `lte`, `gt`, `gte` | is before, is at most, is after, is at least | numbers, dates |
| `between` | is between | numbers, dates — `?f.amount__between=1000,5000` |
| `in`, `not_in` | is any of, is none of | choices — `?f.stage__in=won,lost` |
| `icontains`, `startswith`, `endswith` | contains, starts with, ends with | text |
| `is_null`, `not_null` | is empty, is not empty | any — the value is ignored |

A bare `?f.stage=won` means `eq`, which is the common case.

### The filter builder

The **Filters** panel above a list builds these URLs. Each row is a column, an
operator and a value, and all three menus depend on each other — "is before"
belongs to a date and not to a checkbox — so the panel is assembled in the
browser from a schema the page ships with it.

It submits indexed triples (`fc.0.field`, `fc.0.op`, `fc.0.value`), because an
HTML `<select>` cannot rewrite the `name` of the input beside it. The server
rewrites those into the readable form above and redirects, so the encoding
never reaches the address bar, the back button or a shared link. Without
JavaScript the panel falls back to one box per column.

### Value aliases

A filter value written `@name` is resolved per request, against the caller and
their timezone:

| Alias | Means |
| --- | --- |
| `@me` | the caller's email, else their subject |
| `@today`, `@yesterday`, `@tomorrow` | a calendar date **in the caller's zone** |
| `@week_start`, `@week_end` | Monday and Sunday of this week |
| `@month_start`, `@month_end`, `@year_start` | period boundaries |
| `@now` | the current instant, UTC |

So `?f.owner=@me&f.closed_on__between=@month_start,@month_end` is one link that
means "my deals closing this month" — for whoever opens it, whenever they do.
That is what makes a saved filter worth saving.

Aliases resolve to Python values, not strings, and the field does the final
coercion; `@today` therefore works on a date column and a datetime one alike.
An alias nobody defines drops its condition rather than filtering on the
literal text `"@yesteday"`, which would look like a working filter over an
empty result. Write `@@` for a literal leading at-sign.

`value_aliases` adds resource-specific ones, and shadows a built-in of the same
name — useful when a resource's owner column holds a login name rather than the
email `@me` resolves to by default.

## Menu placement

```python
icon="◈", menu_group="Sales", menu_order=10, in_menu=True
```

Group ordering comes from the module manifest:

```python
MANIFEST = {"name": "demo_sales", "menu_groups": {"Sales": 20}}
```
