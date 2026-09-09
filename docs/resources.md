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
| Structured | `json` `color` `file` `image` `rows` |
| Relational | `relation` `backref` |
| Derived | `computed` `case` |

`computed` is a value produced from the record rather than read from it. Its
callable is given the record, and — if it declares a second parameter — the
request context with it, which is how a computed label says an instant in the
reader's own hours rather than in UTC:

```python
TextField("when", compute=lambda record, ctx: to_zone(
    record["starts_at"], ctx.identity.timezone).strftime("%d %b %H:%M"))
```

The signature is the request: a one-argument compute keeps working untouched,
and a compute that asks for the context but is called without one (from the
CLI, or a background job) falls back to UTC rather than refusing to render.

`case` is a column that does not exist, expressed in terms of ones that do:
ordered branches, first match wins, compiled into the query as a `CASE`. Use it
for the value a screen is really organised by when no column holds it — a job's
lifecycle, a customer's tier. Because it is in the query rather than in Python,
a list can sort, group and paginate by it. Declare `order=` when the order the
branches must be *matched* in differs from the order they should be *read* in.

A `ListView(group_by=...)` sorts by that field first and draws a header row each
time its value changes, so a grouped list stays one query and still pages.

`rows` is a grid: several records entered at once, each cell the ordinary input
for its column, submitted as parallel arrays (`rows.email` repeated once per
row). Use it as an action's `prompt_field` when the subject of the action is a
*set* of new things. Its input offers a "fill from CSV" picker, but the file is
read in the browser and never uploaded — what is submitted is always the grid,
so every cell goes through the same validation as a single-record form.


`status` renders as a coloured pill and can group a board. `relation` renders
as a link with a typeahead input. `backref` is the many side — not stored,
resolved by querying the other resource, shown as an embedded list.

```python
RelationField("company_id", resource="companies", display="name")
BackrefField("contacts", resource="contacts", via="company_id",
             columns=("name", "email"))
BackrefField("bookings", resource="bookings", via="venue_id",
             order=("-starts_at",))
```

```python
BackrefField("shifts", resource="shifts", via="job_id",
             actions=True, tree="by_week")
```

`actions=True` offers the target's own row actions on each embedded row, so
somebody can be taken off a shift from the shift they are on rather than from
the assignment's page; running one returns to the screen the button was pressed
on. It is off by default, because an embedded list is usually context and a
screen should not grow buttons because another resource gained an action.

`tree=` names a tree view on the target: the embedded rows are drawn with that
view's expanders and open through the target's own `/children` fragment, so a
person's jobs can carry their shifts underneath without this screen knowing
anything about shifts.

`order=` is the order the embedded rows read in; without it they take the
target's own default. Which one is right depends on the record you arrived
from — a booking list is a booking list, a *venue's* bookings are read latest
first — and only the backref knows that.

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

# A day, week, fortnight or month grid. The scale is a request parameter
# (`?scale=week&at=2026-09-03`), so `default_scale` only says where readers
# land -- two people can read the same calendar at different widths.
# `end_field` and `all_day=False` make the block a span with its clock times
# printed in the reader's zone; `color_field` tints it -- by the field's own
# choice colours where it has them, and by a stable hash of the value where it
# does not, which is how "the same job is the same colour" works for a field
# with as many values as there are jobs.
CalendarView(start_field="due_on", title_field="subject", default_scale="month")
CalendarView(start_field="start_time", end_field="end_time", all_day=False,
             title_field="job_id", color_field="job_id", default_scale="week")

ChartView(group_by="stage", measure=Measure(Agg.SUM, "amount"), chart="column")

# A series rather than a ranking. `sort=` orders the groups by their own value
# and turns off ordering by the measure, because the two cannot both win: a
# chart read left to right has to be chronological.
ChartView(group_by="month", measure=Measure(Agg.SUM, "amount"),
          chart="line", sort=("month",))

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
    # A panel may be a *filtered* view -- one work queue rather than the whole
    # table. Anything the list route reads works, because it is the list route:
    # `quick`, an `f.<field>` filter, a sort.
    Panel(view="list", title="Unassigned",
          params={"f.owner": "", "sort": "-created_at"}),
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

### Asking for values first

Most actions need something the record does not carry: why a deal was lost, how
long a ban should run, what to put in the note. `prompt_fields` collects them in
a dialog and hands them to the handler as `params`.

```python
@action("mark_lost", "Mark as lost", style="danger",
        prompt_fields=[
            SelectField("lost_reason", label="Reason", choices=LOST_REASONS,
                        required=True),
            TextAreaField("lost_note", label="Note", rows=3),
        ])
async def mark_lost(records, ctx, resource, *, params):
    reason = params["lost_reason"]
    ...
```

Fields are declared inline, as above, or named as strings when the value *is* a
column on the resource (`prompt_fields=["owner"]`). They are coerced and
validated by the field itself, so a bad date comes back in the dialog with the
error next to it rather than reaching the handler.

Only a handler that declares `params` is given them — an action without prompts
keeps the three-argument signature, and existing handlers need no change.

### Reporting a batch honestly

A bulk action over forty records is forty separate attempts, usually against
something remote. `RowOutcome` says what became of each, and `from_outcomes`
turns that into a message with the right level:

```python
outcomes = [RowOutcome(r.pk, ok, message) for r in records]
return ActionResult.from_outcomes(outcomes, done="Cancelled")
```

All succeeded reads as a success; a mixed batch is a **warning** that names the
first few failures and their reasons; everything failing is an **error** that
does not refresh the list.

When the answer is too big or too important to fade, set `template` and the
result renders in the modal instead of as a toast — a per-row report, a file to
take away — staying on screen until it is dismissed:

```python
result = ActionResult.from_outcomes(outcomes, done="Imported")
result.template = "views/_action_report.html"
result.data = {"headings": ["Email", "Result"], "rows": rows}
return result
``` The alternative — one "Done." over a batch where
three rows were refused — is the kind of thing people discover a week later.

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
