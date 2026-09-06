/*
 * Client-side behaviour.
 *
 * Everything here is an enhancement over something that already works without
 * it: toasts report what the server said, drag-and-drop is an alternative to
 * editing a field, and the bulk bar is a convenience over per-row actions. No
 * data flows through this file -- HTMX and plain forms do that.
 */
(function () {
  "use strict";

  const crm = {};
  window.crm = crm;

  /* -- toasts -------------------------------------------------------------
     Fired by the server through the HX-Trigger header, so a handler reports
     its outcome without the page having to know which element asked. */

  crm.toast = function (message, level) {
    if (!message) return;
    const host = document.getElementById("toasts");
    if (!host) return;

    const el = document.createElement("div");
    el.className = "toast " + (level || "info");
    el.setAttribute("role", level === "error" ? "alert" : "status");
    el.textContent = message;
    host.appendChild(el);

    const remove = () => {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 200);
    };
    // Errors stay until dismissed; anything else clears itself.
    const life = level === "error" ? 8000 : 4000;
    const timer = setTimeout(remove, life);
    el.addEventListener("click", () => {
      clearTimeout(timer);
      remove();
    });
  };

  document.body.addEventListener("crm:toast", function (event) {
    crm.toast(event.detail.message, event.detail.level);
  });

  /* -- inline editing ----------------------------------------------------- */

  crm.focusCell = function (cell) {
    if (!cell) return;
    const input = cell.querySelector("input, select, textarea");
    if (!input) return;
    input.focus();
    if (input.select) input.select();
  };

  // Escape restores the display cell. Listening on the document rather than
  // per-cell means cells swapped in later are covered without rebinding.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    const cell = event.target.closest && event.target.closest("td.cell-editing");
    if (!cell) return;
    const cancel = cell.querySelector(".cell-cancel");
    if (cancel) cancel.click();
  });

  /* -- tree branches -------------------------------------------------------
     The first click on a row's toggle is HTMX's: it fetches that branch and
     inserts it after the row. Every click after that is this function's, and
     only hides or shows rows already present -- so opening a branch twice
     costs one query, not two.

     Rows are siblings in one table, not nested elements, because a table needs
     them to be. Depth therefore lives in `data-depth` and "my descendants"
     means "the following rows deeper than me, up to the next one that is not".
  */

  crm.toggleBranch = function (button) {
    const row = button.closest("tr");
    if (!row) return;
    const open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.textContent = open ? "\u25be" : "\u25b8";

    const base = Number(row.dataset.depth || 0);
    // While re-opening, a descendant that was left collapsed keeps its own
    // subtree hidden: expanding a parent must not expand everything under it.
    let collapsedAt = null;

    for (let next = row.nextElementSibling; next; next = next.nextElementSibling) {
      const depth = Number(next.dataset.depth || 0);
      if (!next.classList.contains("tree-row") || depth <= base) break;
      if (!open) {
        next.hidden = true;
        continue;
      }
      if (collapsedAt !== null && depth > collapsedAt) continue;
      collapsedAt = null;
      next.hidden = false;
      const toggle = next.querySelector(".tree-toggle");
      if (toggle && toggle.getAttribute("aria-expanded") !== "true") collapsedAt = depth;
    }
  };

  /* -- bulk selection ----------------------------------------------------- */

  crm.toggleAll = function (master) {
    document
      .querySelectorAll('input[name="selected"]')
      .forEach((box) => (box.checked = master.checked));
    crm.updateBulkBar();
  };

  crm.updateBulkBar = function () {
    const selected = document.querySelectorAll('input[name="selected"]:checked');
    const bar = document.querySelector(".bulk-bar");
    const label = document.querySelector("[data-bulk-count]");
    if (label) {
      label.textContent = selected.length + " selected";
    }
    if (bar) {
      bar.classList.toggle("has-selection", selected.length > 0);
    }
  };

  document.addEventListener("change", function (event) {
    if (event.target.name === "selected") crm.updateBulkBar();
  });

  /* -- board drag and drop -------------------------------------------------
     Dropping a card posts the new group value through the ordinary update
     route, so the write goes through the same policy, validation and provider
     as editing the field by hand. */

  let dragged = null;

  document.addEventListener("dragstart", function (event) {
    const card = event.target.closest && event.target.closest(".board-card");
    if (!card || card.getAttribute("draggable") !== "true") return;
    dragged = card;
    card.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", card.dataset.pk);
  });

  document.addEventListener("dragend", function () {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
    document
      .querySelectorAll(".board-cards.drop-target")
      .forEach((el) => el.classList.remove("drop-target"));
  });

  document.addEventListener("dragover", function (event) {
    const zone = event.target.closest && event.target.closest(".board-cards");
    if (!zone || !dragged) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    zone.classList.add("drop-target");
  });

  document.addEventListener("dragleave", function (event) {
    const zone = event.target.closest && event.target.closest(".board-cards");
    if (zone && !zone.contains(event.relatedTarget)) {
      zone.classList.remove("drop-target");
    }
  });

  document.addEventListener("drop", function (event) {
    const zone = event.target.closest && event.target.closest(".board-cards");
    if (!zone || !dragged) return;
    event.preventDefault();
    zone.classList.remove("drop-target");

    const card = dragged;
    const pk = card.dataset.pk;
    const field = zone.dataset.groupField;
    const value = zone.dataset.groupValue;
    const base = zone.dataset.postUrl;
    if (!pk || !field || !base) return;

    // Move it straight away, then reconcile with what the server says. A
    // failed write reloads, which restores the true position.
    zone.appendChild(card);

    const body = new FormData();
    body.append(field, value);
    const token = document.querySelector('#board-form input[name="csrf_token"]');
    if (token) body.append("csrf_token", token.value);

    fetch(base + "/" + encodeURIComponent(pk) + "/field/" + encodeURIComponent(field), {
      method: "POST",
      body: body,
      headers: { "HX-Request": "true" },
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then(function () {
        crm.toast("Moved.", "success");
        crm.refreshBoardCounts();
      })
      .catch(function () {
        crm.toast("That move could not be saved. Reloading.", "error");
        setTimeout(() => window.location.reload(), 1200);
      });
  });

  crm.refreshBoardCounts = function () {
    document.querySelectorAll(".board-column").forEach(function (column) {
      const count = column.querySelectorAll(".board-card").length;
      const label = column.querySelector(".board-count");
      if (label) label.textContent = count;
    });
  };

  /* -- HTMX integration --------------------------------------------------- */

  document.body.addEventListener("htmx:afterSwap", function () {
    crm.updateBulkBar();
  });

  // A server error that produced no swappable content would otherwise look
  // like a button that did nothing at all.
  document.body.addEventListener("htmx:responseError", function (event) {
    const status = event.detail.xhr.status;
    if (status === 422) return; // the form re-rendered with its own messages
    crm.toast("Something went wrong (" + status + ").", "error");
  });

  document.body.addEventListener("htmx:sendError", function () {
    crm.toast("Could not reach the server. Check your connection.", "error");
  });

  /* -- modals -------------------------------------------------------------
     A form opened from a list keeps the list's filters and scroll position,
     which navigating away would lose. */

  crm.closeModal = function () {
    const host = document.getElementById("modal");
    if (!host) return;
    host.innerHTML = "";
    host.hidden = true;
  };

  // HTMX puts content in; this makes the host visible once it has some.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    const host = document.getElementById("modal");
    if (host && event.target === host) {
      host.hidden = host.innerHTML.trim() === "";
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") crm.closeModal();
  });

  /* -- filter builder ------------------------------------------------------
     Rows are assembled here rather than server-side for one reason: which
     operators a row may offer depends on the column chosen *in that row*, and
     which value widget it needs depends on the operator chosen next to it.
     Neither is known when the page is rendered.

     Nothing is submitted from this state directly. Each row emits three plain
     inputs -- fc.N.field, fc.N.op, fc.N.value -- which the server rewrites
     into the readable ?f.field__op=value form and redirects to. The visible
     controls are deliberately unnamed, so a multi-select, an alias picker and
     a text box can all feed the same single value.

     Without JavaScript the <noscript> form still filters. */

  crm.filterPanel = function (schema, applied) {
    return {
      schema: schema || [],
      rows: [],
      seq: 0,

      init() {
        (applied || []).forEach((condition) => this.adopt(condition));
        if (!this.rows.length) this.add();
      },

      /* -- what the current selections mean -- */

      spec(row) {
        return this.schema.find((f) => f.name === row.field) || null;
      },
      opsFor(row) {
        const spec = this.spec(row);
        return spec ? spec.ops : [];
      },
      arity(row) {
        const op = this.opsFor(row).find((o) => o.value === row.op);
        return op ? op.arity : "one";
      },
      aliases(row) {
        const spec = this.spec(row);
        return spec && this.arity(row) !== "none" ? spec.aliases : [];
      },
      /* A range or a list is typed as free text: no browser offers a date
         picker that holds two dates. */
      inputType(row) {
        const spec = this.spec(row);
        return this.arity(row) === "one" && spec ? spec.input : "text";
      },
      placeholder(row) {
        const arity = this.arity(row);
        if (arity === "range") return "from, to";
        if (arity === "many") return "comma, separated";
        return "";
      },
      picksFromList(row) {
        const spec = this.spec(row);
        return !!(spec && spec.choices.length && !spec.relation);
      },

      /* -- rows -- */

      blank(fieldName) {
        const spec = this.schema.find((f) => f.name === fieldName) || this.schema[0];
        return {
          uid: this.seq++,
          field: spec ? spec.name : "",
          op: spec && spec.ops.length ? spec.ops[0].value : "eq",
          value: "",
          values: [],
          alias: "",
          options: [],
        };
      },
      add() {
        if (this.schema.length) this.rows.push(this.blank());
      },
      remove(index) {
        this.rows.splice(index, 1);
        if (!this.rows.length) this.add();
      },
      adopt(condition) {
        const spec = this.schema.find((f) => f.name === condition.field);
        if (!spec) return;
        const row = this.blank(condition.field);
        if (spec.ops.some((o) => o.value === condition.op)) row.op = condition.op;
        const raw = String(condition.value == null ? "" : condition.value);
        if (raw.slice(0, 1) === "@" && raw.slice(0, 2) !== "@@") row.alias = raw;
        else if (this.arity(row) === "many" && this.picksFromList(row)) {
          row.values = raw.split(",").map((s) => s.trim()).filter(Boolean);
        } else row.value = raw;
        this.rows.push(row);
      },
      onFieldChange(row) {
        // The previous operator may not exist on the new column, and the
        // previous value almost certainly means nothing there.
        const ops = this.opsFor(row);
        if (!ops.some((o) => o.value === row.op)) row.op = ops.length ? ops[0].value : "eq";
        row.value = "";
        row.values = [];
        row.alias = "";
        row.options = [];
      },

      /* The single value actually submitted for a row. */
      submitted(row) {
        if (this.arity(row) === "none") return "";
        if (row.alias) return row.alias;
        if (this.arity(row) === "many" && this.picksFromList(row)) return row.values.join(",");
        return row.value;
      },

      /* A relation filters on a key, so the box suggests records from the
         target resource -- the same list its form input offers, fetched here
         rather than through HTMX because these rows are created by Alpine
         and HTMX only wires up markup it swapped in itself. */
      async suggest(row) {
        const spec = this.spec(row);
        if (!spec || !spec.relation) return;
        const url = "/r/" + encodeURIComponent(spec.relation) +
          "/options?limit=20&q=" + encodeURIComponent(row.value || "");
        try {
          const response = await fetch(url, { headers: { Accept: "application/json" } });
          if (!response.ok) return;
          const body = await response.json();
          row.options = body.options || [];
        } catch (error) {
          row.options = [];
        }
      },
    };
  };

  /* -- theme -------------------------------------------------------------- */

  crm.toggleTheme = function () {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    document.cookie = "theme=" + next + ";path=/;max-age=31536000;samesite=lax";
  };

  crm.updateBulkBar();
})();
