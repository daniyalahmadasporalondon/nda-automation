// Dashboard smart-search controller — v1 (DETERMINISTIC ONLY).
//
// Wires the dashboard search panel: a free-text input + submit arrow, two quick
// status chips, and a results list. Every result is a real matter from
// state.matters (the same list the Repository tab loads) — we never fabricate
// results and make no AI calls in v1. The pure filters live in
// static/js/modules/dashboard-search.mjs (bridged onto window.DashboardSearch);
// this file is just the DOM glue. Clicking a result reuses the existing
// repository openMatter flow.
const DashboardSearchView = (() => {
  // Resolve the bridged pure filters lazily — the .mjs bridge (global-bridge)
  // is a deferred module that runs after this classic script loads, so we only
  // read window.DashboardSearch inside handlers, never at construction time.
  function lib() {
    return window.DashboardSearch || {};
  }

  function escapeHtml(value) {
    if (typeof window.escapeHtml === "function") return window.escapeHtml(value);
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[ch]);
  }

  function createController({
    root,
    input,
    form,
    chipList,
    resultsList,
    resultsStatus,
    getMatters,
    openMatter,
  }) {
    if (!root) {
      // Dashboard search markup is absent (e.g. an old cached page) — no-op.
      return { refresh() {}, renderChips() {} };
    }

    // The active query state. `mode` is "idle" (nothing run yet), "text", or
    // "chip"; the other fields describe what produced the current results.
    let activeMode = "idle";
    let activeChipId = "";
    let activeQuery = "";

    function matters() {
      const list = typeof getMatters === "function" ? getMatters() : [];
      return Array.isArray(list) ? list : [];
    }

    function renderChips() {
      if (!chipList) return;
      const chips = lib().DASHBOARD_SEARCH_CHIPS || [];
      chipList.innerHTML = chips
        .map((chip) => {
          const pressed = activeMode === "chip" && activeChipId === chip.id;
          return (
            `<button type="button" class="dashboard-search-chip" ` +
            `data-dashboard-search-chip="${escapeHtml(chip.id)}" ` +
            `aria-pressed="${pressed ? "true" : "false"}">` +
            `${escapeHtml(chip.label)}</button>`
          );
        })
        .join("");
    }

    function renderResults(results, { emptyMessage }) {
      if (!resultsList) return;
      if (!results.length) {
        resultsList.innerHTML = "";
        resultsList.hidden = true;
        if (resultsStatus) {
          resultsStatus.hidden = false;
          resultsStatus.textContent = emptyMessage;
        }
        return;
      }
      if (resultsStatus) {
        resultsStatus.hidden = false;
        const noun = results.length === 1 ? "document" : "documents";
        resultsStatus.textContent = `${results.length} ${noun}`;
      }
      const { matterTitle, matterStatusLabel } = lib();
      resultsList.hidden = false;
      resultsList.innerHTML = results
        .map((matter) => {
          const title = matterTitle ? matterTitle(matter) : (matter.subject || "Untitled NDA");
          const statusLabel = matterStatusLabel ? matterStatusLabel(matter) : "";
          const statusMarkup = statusLabel
            ? `<span class="dashboard-search-result-status">${escapeHtml(statusLabel)}</span>`
            : "";
          return (
            `<li class="dashboard-search-result">` +
            `<button type="button" class="dashboard-search-result-button" ` +
            `data-dashboard-search-open="${escapeHtml(matter.id)}">` +
            `<span class="dashboard-search-result-title">${escapeHtml(title)}</span>` +
            statusMarkup +
            `</button></li>`
          );
        })
        .join("");
    }

    // Run the free-text keyword filter. An empty query resets to idle.
    function runTextSearch() {
      const query = input ? input.value : "";
      if (!String(query).trim()) {
        reset();
        return;
      }
      activeMode = "text";
      activeChipId = "";
      activeQuery = query;
      renderChips();
      const results = (lib().filterMattersByText || (() => []))(matters(), query);
      renderResults(results, { emptyMessage: "No documents match your search." });
    }

    // Run a quick chip's backing status filter.
    function runChipSearch(chipId) {
      const chip = (lib().chipById || (() => null))(chipId);
      if (!chip) return;
      // Re-clicking the active chip clears it (toggle off).
      if (activeMode === "chip" && activeChipId === chipId) {
        reset();
        return;
      }
      activeMode = "chip";
      activeChipId = chipId;
      activeQuery = "";
      if (input) input.value = "";
      renderChips();
      const results = (lib().runChip || (() => []))(matters(), chip);
      renderResults(results, { emptyMessage: "No documents are in this stage right now." });
    }

    function reset() {
      activeMode = "idle";
      activeChipId = "";
      activeQuery = "";
      renderChips();
      if (resultsList) {
        resultsList.innerHTML = "";
        resultsList.hidden = true;
      }
      if (resultsStatus) resultsStatus.hidden = true;
    }

    // Re-run whatever filter is active against the freshest matter list. Called
    // when matters reload so the results don't go stale under the user. Also
    // (re)renders the chips: at construction time the .mjs bridge may not have
    // run yet (it is a deferred module), so the first refresh after load is what
    // actually populates the chip row.
    function refresh() {
      renderChips();
      if (activeMode === "text") {
        if (input && !String(input.value).trim()) {
          reset();
          return;
        }
        runTextSearch();
      } else if (activeMode === "chip") {
        const chip = (lib().chipById || (() => null))(activeChipId);
        if (!chip) {
          reset();
          return;
        }
        const results = (lib().runChip || (() => []))(matters(), chip);
        renderResults(results, { emptyMessage: "No documents are in this stage right now." });
      }
    }

    form?.addEventListener("submit", (event) => {
      event.preventDefault();
      runTextSearch();
    });
    input?.addEventListener("input", () => {
      // Clearing the field returns to the idle hint; typing while a chip is
      // active hands control back to free-text on the next submit.
      if (!String(input.value).trim() && activeMode === "text") reset();
    });
    input?.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && input.value) {
        event.preventDefault();
        input.value = "";
        reset();
      }
    });
    chipList?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-dashboard-search-chip]");
      if (!button) return;
      runChipSearch(button.dataset.dashboardSearchChip);
    });
    resultsList?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-dashboard-search-open]");
      if (!button) return;
      const matterId = button.dataset.dashboardSearchOpen;
      if (matterId && typeof openMatter === "function") openMatter(matterId);
    });

    renderChips();
    reset();
    // The chip definitions come from the deferred .mjs bridge, which runs after
    // this classic script. By window "load" every deferred module has executed,
    // so re-render the chips then to guarantee they appear even if no search or
    // matter-load refresh has happened yet.
    window.addEventListener("load", () => renderChips(), { once: true });

    return { refresh, renderChips, reset };
  }

  return { createController };
})();

function createDashboardSearchController(options) {
  return DashboardSearchView.createController(options);
}
