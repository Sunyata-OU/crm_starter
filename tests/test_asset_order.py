"""The order the page loads its scripts in.

Alpine has no deferred start: its bundle ends with

    window.Alpine = Vt; queueMicrotask(() => { Vt.start(); });

so it walks the DOM as soon as its own script finishes, and the microtask
queue is drained between two deferred scripts. Served after Alpine, crm.js
would define window.crm too late: every x-data="crm.filterPanel(...)" throws a
ReferenceError, the component never initialises, x-cloak is never removed, and
the filter panel stays hidden with its "+ Add condition" button inert.

Nothing else in the suite would catch that -- the failure is silent, happens
only in a browser, and leaves the server response byte-for-byte valid. So the
ordering itself is what is asserted here, on a page that mounts an Alpine
component and on one that does not.
"""

from __future__ import annotations

import re

#: Matches the src of any of the page's own scripts, in document order.
SCRIPT_SRC = re.compile(r'<script src="/static/([^"]+)"')


def script_order(html: str) -> list[str]:
    return SCRIPT_SRC.findall(html)


def assert_crm_precedes_alpine(html: str) -> None:
    order = script_order(html)
    assert "crm.js" in order and "alpine.min.js" in order, order
    assert order.index("crm.js") < order.index("alpine.min.js"), (
        "alpine.min.js must load after crm.js, or x-data components that call "
        f"crm.* silently fail to initialise; got {order}"
    )


class TestScriptOrder:
    def test_a_list_page_defines_crm_before_alpine_starts(self, admin):
        """The list toolbar is where x-data="crm.filterPanel(...)" lives."""
        html = admin.get("/r/contacts?view=list").text
        assert "crm.filterPanel(" in html
        assert_crm_precedes_alpine(html)

    def test_the_dashboard_defines_crm_before_alpine_starts(self, admin):
        assert_crm_precedes_alpine(admin.get("/").text)

    def test_every_script_is_deferred(self, admin):
        """Ordering only holds because all three scripts are deferred, which
        runs them in document order after parsing rather than as they arrive."""
        html = admin.get("/r/contacts?view=list").text
        for src in script_order(html):
            tag = f'<script src="/static/{src}" defer></script>'
            assert tag in html, f"{src} is not deferred"
