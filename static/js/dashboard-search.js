// Dashboard smart-search controller — v1.1 (deterministic search + AI summary).
//
// Wires the dashboard search panel: a free-text input + submit arrow, two quick
// status chips, and a results list. Every result is a real matter from
// state.matters (the same list the Repository tab loads) — we never fabricate
// results, and the SEARCH itself makes no AI calls. The pure filters live in
// static/js/modules/dashboard-search.mjs (bridged onto window.DashboardSearch);
// this file is just the DOM glue. Clicking a result reuses the existing
// repository openMatter flow.
//
// v1.1 adds a per-row "Summarize" affordance: it POSTs to
// /api/matters/<id>/summary (via the injected summarizeMatter seam) and renders a
// grounded AI summary inline, in an expandable panel clearly LABELED "AI summary"
// so it is never mistaken for verified fact. On any failure it shows the friendly
// "Summary unavailable right now." message — never a stack trace.
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
    // Async seam: POST to the summary endpoint and resolve the parsed JSON body
    // (plus the HTTP ok flag). Lives in app.js so this controller stays DOM-only
    // and the fetch is mockable. Optional — when absent the Summarize affordance is
    // simply not rendered.
    summarizeMatter,
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
          const id = escapeHtml(matter.id);
          // The Summarize affordance is only rendered when a summarizer was wired
          // in. The collapsed panel below the row holds the inline AI summary.
          const summarizeMarkup = typeof summarizeMatter === "function"
            ? `<button type="button" class="dashboard-search-result-summarize" ` +
              `data-dashboard-search-summarize="${id}">Summarize</button>` +
              `<div class="dashboard-search-result-summary" ` +
              `data-dashboard-search-summary-for="${id}" hidden></div>`
            : "";
          return (
            `<li class="dashboard-search-result">` +
            `<div class="dashboard-search-result-row">` +
            `<button type="button" class="dashboard-search-result-button" ` +
            `data-dashboard-search-open="${id}">` +
            `<span class="dashboard-search-result-title">${escapeHtml(title)}</span>` +
            statusMarkup +
            `</button>` +
            summarizeMarkup +
            `</div>` +
            `</li>`
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

    // Render the inline AI summary panel for one result. Always visibly LABELED as
    // an AI summary (the golden rule: a generated summary is never mistaken for
    // verified fact). `state` is "loading" | "ready" | "error".
    function renderSummaryPanel(panel, state, payload) {
      if (!panel) return;
      const { SUMMARY_LABEL, formatSummaryResult, summaryErrorMessage } = lib();
      const label = SUMMARY_LABEL || "AI summary";
      if (state === "loading") {
        panel.hidden = false;
        panel.dataset.state = "loading";
        panel.innerHTML =
          `<p class="dashboard-search-summary-status" role="status">Summarizing…</p>`;
        return;
      }
      if (state === "error") {
        const message = (summaryErrorMessage || (() => "Summary unavailable right now."))(payload);
        panel.hidden = false;
        panel.dataset.state = "error";
        panel.innerHTML =
          `<p class="dashboard-search-summary-status dashboard-search-summary-status-error" role="status">` +
          `${escapeHtml(message)}</p>`;
        return;
      }
      const formatted = (formatSummaryResult || (() => null))(payload);
      if (!formatted) {
        renderSummaryPanel(panel, "error", payload);
        return;
      }
      panel.hidden = false;
      panel.dataset.state = "ready";
      // Preserve newlines/bullets from the model as readable lines.
      const body = escapeHtml(formatted.summary).replace(/\n/g, "<br>");
      panel.innerHTML =
        `<p class="dashboard-search-summary-label">${escapeHtml(formatted.label || label)}</p>` +
        `<div class="dashboard-search-summary-body">${body}</div>`;
    }

    // POST for a matter's summary and render it inline. Re-clicking Summarize while
    // a panel is open collapses it (toggle); otherwise it (re)runs the summary.
    async function runSummarize(matterId, button) {
      if (!matterId || typeof summarizeMatter !== "function") return;
      const panel = resultsList?.querySelector(
        `[data-dashboard-search-summary-for="${cssEscape(matterId)}"]`,
      );
      if (!panel) return;
      // Toggle off an already-open panel.
      if (!panel.hidden && panel.dataset.state !== "loading") {
        panel.hidden = true;
        panel.innerHTML = "";
        if (button) button.setAttribute("aria-expanded", "false");
        return;
      }
      if (panel.dataset.state === "loading") return;
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-expanded", "true");
      }
      renderSummaryPanel(panel, "loading");
      try {
        const { ok, payload } = await summarizeMatter(matterId);
        renderSummaryPanel(panel, ok ? "ready" : "error", payload);
      } catch (error) {
        renderSummaryPanel(panel, "error", null);
      } finally {
        if (button?.isConnected) button.disabled = false;
      }
    }

    // Minimal CSS.escape fallback for the attribute selector (matter ids are
    // server-issued "matter_<hex>", but stay defensive).
    function cssEscape(value) {
      if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
      return String(value).replace(/["\\\]]/g, "\\$&");
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
      const summarizeButton = event.target.closest("[data-dashboard-search-summarize]");
      if (summarizeButton) {
        runSummarize(summarizeButton.dataset.dashboardSearchSummarize, summarizeButton);
        return;
      }
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
