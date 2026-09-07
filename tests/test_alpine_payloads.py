"""JSON handed to Alpine through an HTML attribute.

`tojson` escapes `<`, `>`, `&` and `'` -- deliberately not `"`, because it is
meant for `<script>` blocks and single-quoted attributes. Put its output in a
double-quoted attribute and the first `"` in the JSON ends the attribute: the
browser sees `x-data="crm.filterPanel([{"` followed by a run of nonsense
attributes, Alpine reports "missing ] after element list", and the panel never
initialises.

Nothing else in the suite catches that. The response is byte-for-byte valid
HTTP, the template renders without raising, and the failure appears only in a
browser's console -- so what is asserted here is that the attribute still
parses back into the data it was built from.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser


class AttributeFinder(HTMLParser):
    """Collects one attribute's values, as the browser's parser would see them."""

    def __init__(self, attribute: str) -> None:
        super().__init__(convert_charrefs=True)
        self.attribute = attribute
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == self.attribute and value:
                self.found.append(value)


def alpine_components(html: str) -> list[str]:
    finder = AttributeFinder("x-data")
    finder.feed(html)
    return finder.found


def call_arguments(expression: str) -> list[object]:
    """Decode the JSON arguments of a `crm.thing(...)` call expression."""
    inner = expression[expression.index("(") + 1 : expression.rindex(")")]
    decoder = json.JSONDecoder()
    args: list[object] = []
    index = 0
    while index < len(inner):
        value, index = decoder.raw_decode(inner, index)
        args.append(value)
        while index < len(inner) and inner[index] in ", ":
            index += 1
    return args


class TestFilterPanelPayload:
    def test_the_filter_panel_attribute_survives_the_html_parser(self, admin):
        html = admin.get("/r/contacts?view=list").text
        panels = [c for c in alpine_components(html) if "crm.filterPanel(" in c]
        assert panels, "no filter panel rendered on the contacts list"

        # The whole call has to be inside the attribute. Broken, the parser
        # returns "crm.filterPanel([{" and the rest becomes stray attributes.
        expression = panels[0]
        assert expression.endswith(")"), expression[:80]

        schema, chips = call_arguments(expression)
        assert isinstance(schema, list) and schema, "the filter schema came through empty"
        assert isinstance(chips, list)
        assert all("name" in field for field in schema)

    def test_a_value_containing_a_quote_does_not_break_out(self, admin):
        """A label with an apostrophe is the case a single-quoted attribute risks."""
        html = admin.get("/r/contacts?view=list&f.name.eq=O'Brien \"the\" & Co").text
        panels = [c for c in alpine_components(html) if "crm.filterPanel(" in c]
        assert panels
        schema, chips = call_arguments(panels[0])
        assert isinstance(schema, list) and isinstance(chips, list)
