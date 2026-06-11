// Dashboard smart-search controller — v3 (deterministic search + AI summary +
// counterparty grouping + document-lineage view).
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
//
// v3 adds two DETERMINISTIC, structured-data views (NO AI):
//   * a "Find documents by counterparty" chip that renders the real matters grouped
//     under quiet counterparty-name section headers (groupMattersByCounterparty),
//     each reusing the standard result rows + openMatter + Summarize.
//   * a per-row "Relationships" affordance that expands that matter's DOCUMENT
//     LINEAGE inline as a clean timeline (buildArtifactLineage) — purely a factual
//     view of the matter's own artifacts, with the current version marked, and a
//     friendly "No earlier versions yet." for a single-artifact matter.
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
    // Assistant seam: POST a dashboard command/query to /api/dashboard/assistant.
    // The payload is discriminated by `intent`. Search/filter responses are applied
    // to real matters just like the legacy search-intent path; repository answers
    // and action requests render as assistant cards.
    assistantQuery,
    confirmAssistantAction,
    ensureMatters,
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
    let assistantActions = new Map();
    // Monotonic token so a slow AI translation that resolves AFTER a newer
    // submit/reset can't clobber the current results (last-write-wins).
    let searchRunToken = 0;

    function matters() {
      const list = typeof getMatters === "function" ? getMatters() : [];
      return Array.isArray(list) ? list : [];
    }

    async function ensureSearchMatters() {
      if (typeof ensureMatters !== "function") return;
      try {
        await ensureMatters();
      } catch (error) {
        // Search remains graceful when the repository list cannot refresh; the
        // caller will render against the current in-memory list, which may be empty.
      }
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

    // The markup for ONE result row (open-matter button + Summarize + Relationships
    // affordances + their collapsed inline panels). Shared by the flat result list
    // and the v3 counterparty-grouped view so every row behaves identically.
    function resultItemMarkup(matter) {
      const { matterTitle, matterStatusLabel } = lib();
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
          `data-dashboard-search-summarize="${id}">Summarize</button>`
        : "";
      // v3: a per-row "Relationships" affordance that expands the matter's document
      // lineage inline. Always available (it reads structured data the app already
      // has; no AI, no network). Its panel is clearly a factual structured view.
      const relationshipsMarkup =
        `<button type="button" class="dashboard-search-result-relationships" ` +
        `data-dashboard-search-relationships="${id}" aria-expanded="false">Relationships</button>`;
      const summaryPanel = typeof summarizeMatter === "function"
        ? `<div class="dashboard-search-result-summary" ` +
          `data-dashboard-search-summary-for="${id}" hidden></div>`
        : "";
      const lineagePanel =
        `<div class="dashboard-search-result-lineage" ` +
        `data-dashboard-search-lineage-for="${id}" hidden></div>`;
      return (
        `<li class="dashboard-search-result">` +
        `<div class="dashboard-search-result-row">` +
        `<button type="button" class="dashboard-search-result-button" ` +
        `data-dashboard-search-open="${id}">` +
        `<span class="dashboard-search-result-title">${escapeHtml(title)}</span>` +
        statusMarkup +
        `</button>` +
        summarizeMarkup +
        relationshipsMarkup +
        `</div>` +
        summaryPanel +
        lineagePanel +
        `</li>`
      );
    }

    function renderResults(results, { emptyMessage }) {
      assistantActions = new Map();
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
      resultsList.hidden = false;
      resultsList.innerHTML = results.map((matter) => resultItemMarkup(matter)).join("");
    }

    function answerText(answer) {
      if (answer && typeof answer === "object") return String(answer.text || "").trim();
      return String(answer || "").trim();
    }

    function citationFacts(citations) {
      if (!Array.isArray(citations)) return [];
      return citations
        .filter((citation) => citation && typeof citation === "object")
        .map((citation) => {
          const title = String(citation.title || citation.subject || citation.matter_id || "Matter").trim();
          const bits = [];
          if (citation.workflow_phase) bits.push(String(citation.workflow_phase).replace(/_/g, " "));
          if (citation.last_outbound_at) bits.push(String(citation.last_outbound_at).slice(0, 10));
          return bits.length ? `${title} · ${bits.join(" · ")}` : title;
        });
    }

    function normalizeAssistantActions(payload) {
      const action = String(payload?.action || "").trim();
      const intent = String(payload?.intent || "");
      if (!["draft_action_request", "action_request"].includes(intent) || !action) return [];
      const generator = payload.generator && typeof payload.generator === "object" ? payload.generator : {};
      const target = payload.target && typeof payload.target === "object" ? payload.target : {};
      const label = String(payload.label || (action === "open_generator" ? "Open Generator" : "Open")).trim();
      const sideEffects = Array.isArray(payload.side_effects) ? payload.side_effects : [];
      return [{
        id: action,
        label,
        description: action === "open_generator"
          ? "No NDA is generated, saved, sent, or exported until you confirm again in the Generator."
          : sideEffects.length
          ? "This request only opens the relevant controls. It does not run, send, export, import, approve, or delete anything from the dashboard prompt."
          : "This opens the relevant workspace without changing documents or settings.",
        requiresConfirmation: payload.requires_confirmation !== false,
        payload: {
          action,
          generator,
          target,
          prompt: generator?.prefill?.prompt || "",
          sideEffects,
        },
      }];
    }

    function assistantCardMarkup({ type, title, message, facts = [], actions = [] }) {
      const typeLabel = assistantResponseLabel(type);
      const factMarkup = facts.length
        ? `<ul class="dashboard-search-result-summary dashboard-assistant-facts">` +
          facts.map((fact) => `<li>${escapeHtml(fact)}</li>`).join("") +
          `</ul>`
        : "";
      const actionMarkup = actions.length
        ? `<div class="dashboard-search-result-relationships dashboard-assistant-actions">` +
          actions.map((action) => (
            `<button type="button" class="dashboard-search-result-summarize dashboard-assistant-action" ` +
            `data-dashboard-assistant-action="${escapeHtml(action.id)}">` +
            `${escapeHtml(action.requiresConfirmation ? `Confirm: ${action.label}` : action.label)}` +
            `</button>` +
            (action.description
              ? `<p class="dashboard-search-summary-status">${escapeHtml(action.description)}</p>`
              : "")
          )).join("") +
          `</div>`
        : "";
      return (
        `<li class="dashboard-search-result dashboard-assistant-card" ` +
        `data-dashboard-assistant-response="${escapeHtml(type)}">` +
        `<article class="dashboard-assistant-surface">` +
        `<div class="dashboard-assistant-card-head">` +
        `<span class="dashboard-assistant-type">${escapeHtml(typeLabel)}</span>` +
        `<strong>${escapeHtml(title)}</strong>` +
        `</div>` +
        (message ? `<p class="dashboard-assistant-message">${escapeHtml(message)}</p>` : "") +
        factMarkup +
        actionMarkup +
        `</article>` +
        `</li>`
      );
    }

    function assistantResponseLabel(type) {
      switch (type) {
        case "repository_question":
          return "Repository answer";
        case "system_question":
          return "System answer";
        case "draft_action_request":
        case "action_request":
          return "Confirmation required";
        case "unsupported":
          return "Unsupported";
        case "clarification":
          return "Clarification";
        default:
          return "Assistant";
      }
    }

    function renderAssistantCard({ type, title, message, facts, actions, statusText }) {
      activeSpec = null;
      activeInterpreted = "";
      assistantActions = new Map((actions || []).map((action) => [action.id, action]));
      setInterpreted("");
      if (resultsStatus) {
        resultsStatus.hidden = false;
        resultsStatus.textContent = statusText || title;
      }
      if (!resultsList) return true;
      resultsList.hidden = false;
      resultsList.innerHTML = assistantCardMarkup({ type, title, message, facts, actions });
      return true;
    }

    function renderAssistantResponse(payload) {
      if (!payload || typeof payload !== "object") return false;
      const type = String(payload.intent || "").trim();
      const message = String(payload.message || "").trim();
      if (type === "search_filter") {
        const search = payload.search && typeof payload.search === "object" ? payload.search : {};
        const filters = search.filters && typeof search.filters === "object" ? search.filters : null;
        const validate = lib().validateFilterSpec;
        const apply = lib().applyFilterSpec;
        if (!filters || typeof validate !== "function" || typeof apply !== "function") return false;
        const validated = validate(filters);
        const isEmpty = lib().filterSpecIsEmpty;
        if (typeof isEmpty === "function" && isEmpty(validated)) return false;
        activeSpec = validated;
        activeInterpreted = String(search.interpreted || message || "");
        assistantActions = new Map();
        const results = apply(matters(), validated);
        setInterpreted(activeInterpreted);
        renderResults(results, { emptyMessage: "No documents match this assistant response." });
        return true;
      }
      if (type === "repository_question" || type === "system_question") {
        const text = answerText(payload.answer) || message;
        if (!text) return false;
        return renderAssistantCard({
          type,
          title: "Assistant answer",
          message: text,
          facts: citationFacts(payload.citations),
          statusText: "Assistant answer",
        });
      }
      if (type === "draft_action_request" || type === "action_request") {
        const actions = normalizeAssistantActions(payload);
        const hasSideEffects = Array.isArray(payload.side_effects) && payload.side_effects.length > 0;
        return renderAssistantCard({
          type,
          title: "Action needs confirmation",
          message: message || "Confirm before anything changes.",
          facts: [
            String(payload.action || "") === "open_generator"
              ? "No document will be generated, saved, sent, exported, deleted, or approved from this dashboard prompt."
              : hasSideEffects
              ? "No workflow runs until you confirm again in the destination workspace."
              : "No document or setting changes from this dashboard prompt.",
          ],
          actions,
          statusText: "Action needs confirmation",
        });
      }
      if (type === "unsupported") {
        return renderAssistantCard({
          type,
          title: "Unsupported request",
          message: message || "This assistant cannot do that request yet.",
          facts: [],
          actions: [],
          statusText: "Unsupported request",
        });
      }
      if (type === "clarification") {
        const questions = Array.isArray(payload.questions)
          ? payload.questions.map((question) => String(question || "").trim()).filter(Boolean)
          : [];
        return renderAssistantCard({
          type,
          title: "Clarification needed",
          message: message || "I need one more detail before I can help with that.",
          facts: questions,
          actions: [],
          statusText: "Clarification needed",
        });
      }
      return false;
    }

    // v3 "Find documents linked to a counterparty": render the real matters grouped
    // under quiet counterparty-name section headers. Each group lists that
    // counterparty's matters using the SAME result rows (open + Summarize +
    // Relationships) as the flat list. Honest UX: the header shows the derived name
    // as-is (exact for generated NDAs, subject-derived for inbound) — we don't dress
    // it up as a verified legal entity.
    function renderGroupedByCounterparty() {
      if (!resultsList) return;
      const group = (lib().groupMattersByCounterparty || (() => []))(matters());
      const total = group.reduce((sum, entry) => sum + entry.matters.length, 0);
      if (!total) {
        resultsList.innerHTML = "";
        resultsList.hidden = true;
        if (resultsStatus) {
          resultsStatus.hidden = false;
          resultsStatus.textContent = "No documents yet.";
        }
        return;
      }
      if (resultsStatus) {
        resultsStatus.hidden = false;
        const docNoun = total === 1 ? "document" : "documents";
        const cpNoun = group.length === 1 ? "counterparty" : "counterparties";
        resultsStatus.textContent = `${total} ${docNoun} across ${group.length} ${cpNoun}`;
      }
      resultsList.hidden = false;
      resultsList.innerHTML = group
        .map((entry) => {
          const count = entry.matters.length;
          const noun = count === 1 ? "document" : "documents";
          const rows = entry.matters.map((matter) => resultItemMarkup(matter)).join("");
          return (
            `<li class="dashboard-search-group">` +
            `<p class="dashboard-search-group-header">` +
            `<span class="dashboard-search-group-name">${escapeHtml(entry.counterparty)}</span>` +
            `<span class="dashboard-search-group-count">${count} ${noun}</span>` +
            `</p>` +
            `<ul class="dashboard-search-results dashboard-search-group-results">${rows}</ul>` +
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

      if (typeof assistantQuery === "function") {
        const token = ++searchRunToken;
        setInterpreted("");
        if (resultsStatus) {
          resultsStatus.hidden = false;
          resultsStatus.textContent = "Interpreting…";
        }
        if (resultsList) {
          resultsList.hidden = true;
          resultsList.innerHTML = "";
        }
        let assistantOutcome = null;
        const matterLoad = ensureSearchMatters();
        try {
          assistantOutcome = await assistantQuery(query);
        } catch (error) {
          assistantOutcome = null;
        }
        await matterLoad;
        if (token !== searchRunToken) return;
        if (assistantOutcome?.ok && renderAssistantResponse(assistantOutcome.payload)) {
          return;
        }
      }

      // No assistant/search seam wired -> v1 keyword search directly.
      if (typeof searchIntent !== "function") {
        await ensureSearchMatters();
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
      const matterLoad = ensureSearchMatters();
      try {
        outcome = await searchIntent(query);
      } catch (error) {
        outcome = null;
      }
      await matterLoad;
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
    async function runChipSearch(chipId) {
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
      const token = ++searchRunToken;
      setInterpreted("");
      assistantActions = new Map();
      renderChips();
      await ensureSearchMatters();
      if (token !== searchRunToken) return;
      // v3: the "Find documents by counterparty" chip groups every matter instead of
      // filtering by status. Branch on its kind so its rendering path is distinct.
      if (chip.kind === "group") {
        renderGroupedByCounterparty();
        return;
      }
      const results = (lib().runChip || (() => []))(matters(), chip);
      renderResults(results, { emptyMessage: "No documents are in this stage right now." });
    }

    function reset() {
      activeMode = "idle";
      activeChipId = "";
      activeQuery = "";
      activeSpec = null;
      activeInterpreted = "";
      assistantActions = new Map();
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
        if (chip.kind === "group") {
          renderGroupedByCounterparty();
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

    // v3 "Show how documents relate": render a matter's DOCUMENT LINEAGE inline as a
    // clean factual timeline — the artifact version chain (original ->
    // redline/reviewed/generated -> counter) built deterministically from the
    // matter's own artifacts. NOT an AI view: it's structured data the app already
    // has. Fewer than two artifacts -> a friendly "No earlier versions yet." line.
    function renderLineagePanel(panel, matter) {
      if (!panel) return;
      const build = lib().buildArtifactLineage || (() => []);
      const nodes = build(matter);
      panel.hidden = false;
      if (!nodes.length || nodes.length < 2) {
        panel.innerHTML =
          `<p class="dashboard-search-lineage-label">Document history</p>` +
          `<p class="dashboard-search-lineage-empty">No earlier versions yet.</p>`;
        return;
      }
      const items = nodes
        .map((node) => {
          const roleLabel = escapeHtml(node.roleLabel || "Document");
          const versionLabel = Number(node.version) > 0 ? `v${escapeHtml(node.version)}` : "";
          const metaBits = [];
          if (node.actorLabel) metaBits.push(escapeHtml(node.actorLabel));
          const when = formatLineageDate(node.date);
          if (when) metaBits.push(when);
          const meta = metaBits.length
            ? `<span class="dashboard-search-lineage-meta">${metaBits.join(" · ")}</span>`
            : "";
          const currentBadge = node.isCurrent
            ? `<span class="dashboard-search-lineage-current">Current</span>`
            : "";
          const versionMarkup = versionLabel
            ? `<span class="dashboard-search-lineage-version">${versionLabel}</span>`
            : "";
          return (
            `<li class="dashboard-search-lineage-node${node.isCurrent ? " is-current" : ""}">` +
            `<span class="dashboard-search-lineage-dot" aria-hidden="true"></span>` +
            `<span class="dashboard-search-lineage-body">` +
            `<span class="dashboard-search-lineage-role">${roleLabel}${versionMarkup}${currentBadge}</span>` +
            meta +
            `</span>` +
            `</li>`
          );
        })
        .join("");
      panel.innerHTML =
        `<p class="dashboard-search-lineage-label">Document history</p>` +
        `<ol class="dashboard-search-lineage-list">${items}</ol>`;
    }

    // A short human date for a lineage node. Reuses the app's formatMatterDate when
    // present (consistent wording across the UI); else falls back to the YYYY-MM-DD
    // prefix of the ISO timestamp. Returns "" for an unparseable/empty stamp.
    function formatLineageDate(stamp) {
      const value = String(stamp || "").trim();
      if (!value) return "";
      if (typeof window.formatMatterDate === "function") {
        const formatted = window.formatMatterDate(value);
        if (formatted) return escapeHtml(formatted);
      }
      const isoDate = value.slice(0, 10);
      return /^\d{4}-\d{2}-\d{2}$/.test(isoDate) ? escapeHtml(isoDate) : "";
    }

    // Toggle a matter's inline lineage panel. Purely local — no network, no AI — so
    // it just builds + renders (or collapses) the structured view.
    function runRelationships(matterId, button) {
      if (!matterId) return;
      const panel = resultsList?.querySelector(
        `[data-dashboard-search-lineage-for="${cssEscape(matterId)}"]`,
      );
      if (!panel) return;
      // Toggle off an already-open panel.
      if (!panel.hidden) {
        panel.hidden = true;
        panel.innerHTML = "";
        if (button) button.setAttribute("aria-expanded", "false");
        return;
      }
      const matter = matters().find((m) => String(m?.id) === String(matterId));
      if (!matter) return;
      if (button) button.setAttribute("aria-expanded", "true");
      renderLineagePanel(panel, matter);
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
      const relationshipsButton = event.target.closest("[data-dashboard-search-relationships]");
      if (relationshipsButton) {
        runRelationships(relationshipsButton.dataset.dashboardSearchRelationships, relationshipsButton);
        return;
      }
      const assistantButton = event.target.closest("[data-dashboard-assistant-action]");
      if (assistantButton) {
        runAssistantAction(assistantButton.dataset.dashboardAssistantAction, assistantButton);
        return;
      }
      const button = event.target.closest("[data-dashboard-search-open]");
      if (!button) return;
      const matterId = button.dataset.dashboardSearchOpen;
      if (matterId && typeof openMatter === "function") openMatter(matterId);
    });

    async function runAssistantAction(actionId, button) {
      const action = assistantActions.get(String(actionId || ""));
      if (!action || typeof confirmAssistantAction !== "function") return;
      if (button) button.disabled = true;
      try {
        await confirmAssistantAction(action.payload || action);
        if (resultsStatus) {
          resultsStatus.hidden = false;
          resultsStatus.textContent = action.payload?.statusText || "Assistant action opened. Review before making changes.";
        }
      } finally {
        if (button?.isConnected) button.disabled = false;
      }
    }

    renderChips();
    reset();
    // The chip definitions come from the deferred .mjs bridge, which runs after
    // this classic script. By window "load" every deferred module has executed,
    // so re-render the chips then to guarantee they appear even if no search or
    // matter-load refresh has happened yet.
    window.addEventListener("load", () => {
      renderChips();
      const initialQuery = initialQueryFromLocation();
      if (initialQuery && input && !String(input.value || "").trim()) {
        input.value = initialQuery;
        runTextSearch();
      }
    }, { once: true });

    return { refresh, renderChips, reset };
  }

  return { createController };
})();

function initialQueryFromLocation() {
  try {
    const params = new URLSearchParams(window.location.search || "");
    return String(params.get("dashboardSearch") || "").trim();
  } catch (error) {
    return "";
  }
}

function createDashboardSearchController(options) {
  return DashboardSearchView.createController(options);
}
