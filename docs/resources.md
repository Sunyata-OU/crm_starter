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
```

Views are switchable from the toolbar; the list route picks one from `?view=`.

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
)
```

Filters travel in the URL — `?f.stage=won&f.amount__gte=1000&q=acme` — so a
filtered list is a shareable link and the back button works. A parameter naming
an unknown field, or an operator the field does not support, is dropped rather
than passed through.

## Menu placement

```python
icon="◈", menu_group="Sales", menu_order=10, in_menu=True
```

Group ordering comes from the module manifest:

```python
MANIFEST = {"name": "demo_sales", "menu_groups": {"Sales": 20}}
```
