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

  /* -- theme -------------------------------------------------------------- */

  crm.toggleTheme = function () {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    document.cookie = "theme=" + next + ";path=/;max-age=31536000;samesite=lax";
  };

  crm.updateBulkBar();
})();
