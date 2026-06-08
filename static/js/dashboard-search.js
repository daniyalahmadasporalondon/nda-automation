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
    // v2: the "Showing: <interpreted>" line that explains how the AI read the
    // query. Optional — absent on an old cached page; the box still works.
    interpretedLine,
    getMatters,
    openMatter,
    // Async seam: POST to the summary endpoint and resolve the parsed JSON body
    // (plus the HTTP ok flag). Lives in app.js so this controller stays DOM-only
    // and the fetch is mockable. Optional — when absent the Summarize affordance is
    // simply not rendered.
    summarizeMatter,
    // v2 async seam: POST a natural-language query to the search-intent endpoint
    // and resolve {ok, payload}. payload is either {filters, interpreted} (apply the
    // validated spec) or {fallback:true} (use v1 keyword search). Lives in app.js so
    // this controller stays DOM-only and the fetch is mockable. Optional — when
    // absent free-text uses the v1 deterministic keyword filter directly.
    searchIntent,
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
    // The last validated filter spec + interpreted line an AI text search applied,
    // so a background matter reload can re-apply it deterministically WITHOUT a
    // fresh AI call (no flicker, no extra network). Null when the active text search
    // is the v1 keyword fallback.
    let activeSpec = null;
    let activeInterpreted = "";
    // Monotonic token so a slow AI translation that resolves AFTER a newer
    // submit/reset can't clobber the current results (last-write-wins).
    let searchRunToken = 0;

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

    // Set (or clear) the "Showing: <interpreted>" line that explains how the AI
    // read the query. Hidden whenever there's nothing to explain (idle, chips, or
    // the v1 keyword fallback path).
    function setInterpreted(text) {
      if (!interpretedLine) return;
      const value = String(text || "").trim();
      if (!value) {
        interpretedLine.hidden = true;
        interpretedLine.textContent = "";
        return;
      }
      interpretedLine.hidden = false;
      interpretedLine.textContent = `Showing: ${value}`;
    }

    // The v1 deterministic keyword filter — the always-available fallback. Used
    // directly when no AI seam is wired, and as the graceful fallback whenever the
    // AI endpoint returns {fallback:true}, errors, or yields an empty spec.
    function renderKeywordResults(query) {
      activeSpec = null;
      activeInterpreted = "";
      const results = (lib().filterMattersByText || (() => []))(matters(), query);
      setInterpreted("");
      renderResults(results, { emptyMessage: "No documents match your search." });
    }

    // Run the free-text search. v2: translate the natural-language query into a
    // validated structured filter spec via the AI seam, apply it to the REAL
    // matters deterministically, and show the interpreted line. On any
    // failure/fallback/empty-spec, fall back to the v1 keyword filter so the box
    // always works. An empty query resets to idle.
    async function runTextSearch() {
      const query = input ? input.value : "";
      if (!String(query).trim()) {
        reset();
        return;
      }
      activeMode = "text";
      activeChipId = "";
      activeQuery = query;
      renderChips();

      // No AI seam wired -> v1 keyword search directly.
      if (typeof searchIntent !== "function") {
        renderKeywordResults(query);
        return;
      }

      const token = ++searchRunToken;
      // Brief "Interpreting…" loading state while the translation is in flight.
      setInterpreted("");
      if (resultsStatus) {
        resultsStatus.hidden = false;
        resultsStatus.textContent = "Interpreting…";
      }
      if (resultsList) {
        resultsList.hidden = true;
        resultsList.innerHTML = "";
      }

      let outcome = null;
      try {
        outcome = await searchIntent(query);
      } catch (error) {
        outcome = null;
      }
      // A newer submit/reset superseded this run — drop the stale result.
      if (token !== searchRunToken) return;

      const payload = outcome && outcome.payload ? outcome.payload : {};
      const ok = !!(outcome && outcome.ok);
      const spec = ok && payload && payload.filters != null ? payload.filters : null;

      // Fallback signal, error, or no spec -> v1 keyword search.
      if (!ok || payload.fallback === true || spec == null) {
        renderKeywordResults(query);
        return;
      }

      const validate = lib().validateFilterSpec;
      const apply = lib().applyFilterSpec;
      const validated = typeof validate === "function" ? validate(spec) : spec;
      const isEmpty = lib().filterSpecIsEmpty;
      // An all-null spec means the query didn't map to any dimension — the AI
      // couldn't structure it, so honor the user's words via v1 keyword search.
      if (typeof isEmpty === "function" && isEmpty(validated)) {
        renderKeywordResults(query);
        return;
      }
      if (typeof apply !== "function") {
        renderKeywordResults(query);
        return;
      }

      // Remember the validated spec so a background matter reload re-applies it
      // deterministically (no fresh AI call, no flicker).
      activeSpec = validated;
      activeInterpreted = String(payload.interpreted || "");
      const results = apply(matters(), validated);
      // Prefer the server's interpreted line; else describe nothing (the spec is
      // still applied — we just don't have a phrase for it).
      setInterpreted(activeInterpreted);
      renderResults(results, { emptyMessage: "No documents match this filter." });
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
      // A chip win supersedes any in-flight AI translation.
      searchRunToken += 1;
      setInterpreted("");
      renderChips();
      const results = (lib().runChip || (() => []))(matters(), chip);
      renderResults(results, { emptyMessage: "No documents are in this stage right now." });
    }

    function reset() {
      activeMode = "idle";
      activeChipId = "";
      activeQuery = "";
      activeSpec = null;
      activeInterpreted = "";
      // Supersede any in-flight AI translation so a late resolve can't repopulate.
      searchRunToken += 1;
      setInterpreted("");
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
        // If an AI text search already produced a validated spec, re-apply it to
        // the fresh matters deterministically — no new AI call, no flicker. Only
        // re-run the (async) translation when there's no remembered spec (e.g. the
        // active text search was the v1 keyword fallback).
        if (activeSpec && typeof lib().applyFilterSpec === "function") {
          const results = lib().applyFilterSpec(matters(), activeSpec);
          setInterpreted(activeInterpreted);
          renderResults(results, { emptyMessage: "No documents match this filter." });
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
