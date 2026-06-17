// Overview tab — the FIRST Review-workstation inspector sub-tab (before Clause,
// Structure). A single at-a-glance pane that composes three self-contained
// component renderers, top to bottom:
//
//   renderOverviewFacts  -> counterparty + matter facts (governing law / term /
//                           received), with confirm + entity-name controls.
//   renderOverviewRoster -> every reviewed clause with its verdict; clicking a
//                           clause selects it AND switches to the Clause sub-tab.
//   renderOverviewFooter -> the Approve / Send actions (Approve gated by the
//                           EXISTING stale-playbook logic only).
//
// This file is the SHELL/composer. The three render* functions are built by
// separate teammates and folded in by the integrator; this controller only maps
// the in-memory matter/review state onto their published interfaces and wires
// their callbacks to the EXISTING review-workstation app logic.
//
// EMPTY STATE: before any AI review has run (matter.has_ai_review === false) the
// roster has nothing to show, so the pane renders an `.ov-empty` "No review yet"
// block with a "Refresh with AI" button wired to the existing explicit-refresh
// path (refreshSelectedMatterReview), instead of the roster.
//
// Factory returning { render() } that paints into #studioDetailPanel when the
// Overview sub-tab is active (renderStudioDetail / renderStudioEmpty dispatch).
// Pure DOM + state. Every cross-file dependency (the component renderers, the
// existing handlers, escapeHtml, clause helpers) is resolved LAZILY at render
// time off window, never captured at construction — the global bridge and the
// review-workstation modules load around this controller, so an eager capture
// would freeze a null reference.

function createOverviewController({ state, root, fillSection, renderFill }) {
  function panel() {
    return root || document.querySelector("#studioDetailPanel");
  }

  // The merged Overview pane renders the at-a-glance summary AROUND the existing
  // Fill/Aspora-entity tool. `fillSection` is a persistent standalone element
  // (owned by app.js) that the UNTOUCHED reviewFillController paints into via
  // root.innerHTML — so it owns its own markup entirely and the section TITLE must
  // live OUTSIDE it. We wrap it in a titled section and relocate the whole wrapper
  // into the pane on each render; `renderFill` repaints the body. The wrapper is
  // appended to whatever `parent` the caller passes — placement is the caller's
  // decision (right below the counterparty block, before the roster).
  function mountFillSection(parent) {
    if (!parent || !fillSection) return;
    const wrapper = document.createElement("section");
    wrapper.className = "ov-section ov-section-fill";
    const title = document.createElement("h3");
    title.className = "ov-section-title";
    title.textContent = "Aspora entity";
    // Keep the controller's body class on the persistent element it owns.
    fillSection.className = "ov-section-fill-body";
    wrapper.append(title, fillSection);
    parent.append(wrapper);
    // The Fill controller paints into fillSection (its root) on demand. Its
    // root.innerHTML write only touches its own body, leaving the title intact.
    if (typeof renderFill === "function") renderFill();
  }

  function escape(value) {
    return typeof window !== "undefined" && typeof window.escapeHtml === "function"
      ? window.escapeHtml(value)
      : String(value == null ? "" : value);
  }

  // --- existing-logic seams (resolved lazily) --------------------------------
  // Every handler delegates to the canonical review-workstation function or the
  // existing header control, so the Overview tab never re-implements a flow.

  function callGlobal(name, ...args) {
    const fn = typeof window !== "undefined" ? window[name] : undefined;
    if (typeof fn === "function") return fn(...args);
    return undefined;
  }

  // Select a clause AND surface the Clause sub-tab. selectReviewClause already
  // transitions the inspector view to "clause" via nextClauseSelectionState, so
  // this single call satisfies both halves of onClauseClick.
  function onClauseClick(clauseId) {
    if (!clauseId) return;
    callGlobal("selectReviewClause", clauseId, { jump: true });
  }

  // Approve Review — the existing studio approve flow.
  function onApprove() {
    callGlobal("approveSelectedReview");
  }

  // Send for signature — the existing DocuSign trigger. Click the real header
  // button so the existing controller opens its composer (the visibility/enabled
  // gating already lives on that button); clicking a hidden-but-present button
  // still fires its handler.
  function onSend() {
    const button = document.querySelector("#studioSendForSignatureButton");
    if (button) button.click();
  }

  // Confirm the counterparty as shown — the existing override submit with the
  // current name (mirrors the header Confirm button).
  function onConfirm() {
    callGlobal("submitCounterpartyOverride", state.selectedMatter?.counterparty);
  }

  // Set / correct the counterparty (entity name) — the existing override submit
  // with the typed value.
  function onEntityFill(value) {
    callGlobal("submitCounterpartyOverride", value);
  }

  // Explicit AI refresh — the existing endpoint behind the header "Refresh with
  // AI" button.
  function onRefresh() {
    callGlobal("refreshSelectedMatterReview");
  }

  // --- data mapping ----------------------------------------------------------
  // Map the in-memory matter + review state onto the component interfaces. The
  // shapes mirror the finalized backend contract (feat/overview-data): clause
  // verdict from `.decision` ('pass'|'review'|'fail'); the 'check' review_state
  // bucket is NOT used for the per-clause verdict.

  function hasAiReview() {
    const matter = state.selectedMatter;
    // Explicit backend flag wins; fall back to "are there any review clauses"
    // for matters/fixtures that predate has_ai_review.
    if (matter && typeof matter.has_ai_review === "boolean") return matter.has_ai_review;
    const hasResults = typeof window !== "undefined" && typeof window.hasReviewResults === "function"
      ? window.hasReviewResults()
      : Array.isArray(state.reviewClauses) && state.reviewClauses.length > 0;
    return Boolean(hasResults);
  }

  // Normalize a clause's backend decision into the component verdict vocabulary
  // ('pass'|'review'|'fail'). Prefer the canonical clauseStatus (single source of
  // truth) so the Overview verdict matches the Clause lane exactly; fall back to
  // the raw `.decision` when the bridge helper is unavailable.
  function clauseVerdict(clause) {
    const status = typeof window !== "undefined" && typeof window.clauseStatus === "function"
      ? window.clauseStatus(clause)
      : null;
    if (status) {
      if (status.fails) return "fail";
      if (status.needsReview) return "review";
      if (status.passes) return "pass";
    }
    const decision = String(clause?.decision || "").toLowerCase();
    if (decision === "fail") return "fail";
    if (decision === "review") return "review";
    if (decision === "pass") return "pass";
    return "review";
  }

  // The display name: reuse the EXISTING clause-id -> display-name resolution so
  // the Overview shows "Mutuality", "Confidential Information", etc. — identical
  // to the Clause lane. clauseDisplayName reads clause.name/title/label and falls
  // back to the id.
  function clauseName(clause) {
    return typeof window !== "undefined" && typeof window.clauseDisplayName === "function"
      ? window.clauseDisplayName(clause)
      : String(clause?.name || clause?.id || "Clause");
  }

  // Roster data: verdicts only — { id, name, verdict }. No reviewed-tracking
  // (dropped per the product decision; the Overview does not count "how many
  // reviewed").
  function rosterClauses() {
    const source = Array.isArray(state.reviewClauses) ? state.reviewClauses : [];
    return source.map((clause) => ({
      id: clause.id,
      name: clauseName(clause),
      verdict: clauseVerdict(clause),
    }));
  }

  function factsData() {
    const matter = state.selectedMatter || {};
    return {
      counterparty: {
        name: String(matter.counterparty || "").trim(),
        confirmed: matter.counterparty_needs_confirmation === false,
      },
      facts: {
        governingLaw: matter.governing_law || matter.governing_law_label || "",
        term: matter.term_label || "",
        receivedDate: matter.received_at || "",
      },
    };
  }

  // Footer gate. Approve keeps its EXISTING behaviour: gated ONLY by the existing
  // stale-playbook logic (approveBlockReasons) plus the already-approved terminal
  // state. We do NOT add any new "all clauses reviewed" gate. Map the existing
  // reason codes into a boolean + a short human reason for the footer to surface.
  function footerData() {
    const matter = state.selectedMatter || {};

    // Already approved -> Approve is a no-op terminal state.
    const approved = typeof window !== "undefined" && typeof window.isMatterApproved === "function"
      ? window.isMatterApproved(matter)
      : String(matter.status || "").trim().toLowerCase() === "approved";
    if (approved) {
      return { approveDisabled: true, approveReason: "Review already approved." };
    }

    // The existing local prediction of the server's blocks_approval codes (only
    // "stale_playbook" today, unioned with the last server-returned blocks).
    const reasons = typeof window !== "undefined" && typeof window.approveBlockReasons === "function"
      ? window.approveBlockReasons(matter)
      : [];
    const approveDisabled = Array.isArray(reasons) && reasons.length > 0;
    let approveReason = "";
    if (approveDisabled) {
      const firstCode = reasons[0];
      approveReason = typeof window !== "undefined" && typeof window.approveBlockReasonLabel === "function"
        ? window.approveBlockReasonLabel(firstCode)
        : String(firstCode || "Approval is blocked.");
    }
    return { approveDisabled, approveReason };
  }

  // --- empty state -----------------------------------------------------------

  function renderEmpty(container) {
    container.innerHTML = `
      <div class="ov-tab ov-tab-empty">
        <div class="ov-empty" role="status">
          <p class="ov-empty-title">No review yet</p>
          <p class="ov-empty-hint">Run the AI review to see clause verdicts, facts, and the approval checklist here.</p>
          <button type="button" class="ov-empty-refresh" data-ov-action="refresh">Refresh with AI</button>
        </div>
      </div>
    `;
    const refreshButton = container.querySelector("[data-ov-action='refresh']");
    if (refreshButton) {
      refreshButton.addEventListener("click", () => onRefresh());
    }
    // The Fill (Aspora-entity) tool scans loaded paragraphs for blanks and is
    // useful even before any AI review has run, so it stays available below the
    // empty notice (mirrors the old separate-Fill-tab empty behaviour).
    const tab = container.querySelector(".ov-tab");
    mountFillSection(tab);
  }

  // --- compose ---------------------------------------------------------------

  function render() {
    const container = panel();
    if (!container) return;

    // No matter loaded at all -> let the host's empty pane stand (clear).
    if (!state.selectedMatter) {
      container.innerHTML = "";
      return;
    }

    if (!hasAiReview()) {
      renderEmpty(container);
      return;
    }

    container.innerHTML = '<div class="ov-tab"></div>';
    const tab = container.querySelector(".ov-tab");

    const clauses = rosterClauses();
    const facts = factsData();
    const footer = footerData();

    // SUMMARY section: the at-a-glance Overview (counterparty/facts, clause roster,
    // approve/send footer), wrapped in a titled ov-section. The three component
    // renderers paint into their own child containers, top to bottom. They are
    // folded in by the integrator; until then a missing renderer degrades to a
    // labelled placeholder so the shell still mounts (no hard crash on a not-yet-
    // present component).
    const summarySection = document.createElement("section");
    summarySection.className = "ov-section ov-section-summary";
    const summaryTitle = document.createElement("h3");
    summaryTitle.className = "ov-section-title";
    summaryTitle.textContent = "Summary";
    const summaryBody = document.createElement("div");
    summaryBody.className = "ov-section-summary-body";
    summarySection.append(summaryTitle, summaryBody);
    tab.append(summarySection);

    const factsEl = document.createElement("div");
    factsEl.className = "ov-block ov-block-facts";
    const rosterEl = document.createElement("div");
    rosterEl.className = "ov-block ov-block-roster";
    const footerEl = document.createElement("div");
    footerEl.className = "ov-block ov-block-footer";
    summaryBody.append(factsEl);

    // ASPORA ENTITY section: the existing Fill tool, relocated to sit RIGHT BELOW
    // the counterparty/facts block and BEFORE the clause roster. It mounts into the
    // summary body, between facts and roster, so the order reads:
    //   counterparty/facts -> Aspora-entity Fill -> clause roster -> approve/send.
    composeFacts(factsEl, facts);
    mountFillSection(summaryBody);

    summaryBody.append(rosterEl, footerEl);
    composeRoster(rosterEl, clauses);
    composeFooter(footerEl, footer);
  }

  function composeFacts(el, facts) {
    if (typeof window !== "undefined" && typeof window.renderOverviewFacts === "function") {
      window.renderOverviewFacts(
        el,
        { counterparty: facts.counterparty, facts: facts.facts },
        { onConfirm, onEntityFill },
      );
      return;
    }
    placeholder(el, "Facts", "renderOverviewFacts");
  }

  function composeRoster(el, clauses) {
    if (typeof window !== "undefined" && typeof window.renderOverviewRoster === "function") {
      window.renderOverviewRoster(
        el,
        { clauses, currentClauseId: state.selectedReviewClauseId || null },
        { onClauseClick },
      );
      return;
    }
    placeholder(el, "Clause roster", "renderOverviewRoster");
  }

  function composeFooter(el, footer) {
    if (typeof window !== "undefined" && typeof window.renderOverviewFooter === "function") {
      window.renderOverviewFooter(
        el,
        { approveDisabled: footer.approveDisabled, approveReason: footer.approveReason },
        { onApprove, onSend },
      );
      return;
    }
    placeholder(el, "Footer", "renderOverviewFooter");
  }

  // Shell-only fallback used until each component file is folded in by the
  // integrator. Never shown end-to-end once the four files ship together.
  function placeholder(el, label, fnName) {
    el.innerHTML = `<div class="ov-placeholder" data-ov-pending="${escape(fnName)}">${escape(label)} (pending ${escape(fnName)})</div>`;
  }

  return { render };
}

// Classic-script global export so app.js (also a classic script) can construct
// the controller at load time, mirroring createFillController.
if (typeof window !== "undefined") {
  window.createOverviewController = createOverviewController;
}
