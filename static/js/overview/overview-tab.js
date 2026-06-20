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
// PERSISTENT PANE (no appearing/disappearing): the facts -> roster -> footer
// stack is ALWAYS rendered, even before any AI review has run. Instead of an
// empty "No review yet" block, pre-review state is shown HONESTLY in place: the
// roster renders each clause with a muted "Not Reviewed" status (never the
// deterministic verdict — the no-ghost rule) and the footer's Approve / Send
// actions render disabled/grayed. `ai_review_ran` is the single flag driving
// these placeholders; once the review runs, real verdicts + the existing gates
// take over. Explicit AI refresh stays on the EXISTING header button, not here.
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
  // root.innerHTML — so it owns its own markup entirely. We wrap it in a section
  // and relocate the whole wrapper into the pane on each render; `renderFill`
  // repaints the body. The wrapper is appended to whatever `parent` the caller
  // passes — placement is the caller's decision (right below the counterparty
  // block, before the roster).
  //
  // NO outer "Aspora entity" section TITLE is emitted: the Fill tool already
  // carries its own "Aspora entity" label (the entity-picker field), so an outer
  // .ov-section-title here would duplicate it. The .ov-section-fill class carries
  // the section's layout/separator styling directly (see overview.css), so the
  // title element is not needed structurally either.
  function mountFillSection(parent) {
    if (!parent || !fillSection) return;
    const wrapper = document.createElement("section");
    wrapper.className = "ov-section ov-section-fill";
    // Keep the controller's body class on the persistent element it owns.
    fillSection.className = "ov-section-fill-body";
    wrapper.append(fillSection);
    parent.append(wrapper);
    // The Fill controller paints into fillSection (its root) on demand.
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

  // Send for signature — open the existing Review DocuSign composer directly via
  // the global the shell exposes (window.openReviewDocuSignComposer). The footer
  // owns the gate now (Send is disabled pre-review), so this only fires once there
  // is something to send. There is no longer a header Send button to click.
  function onSend() {
    callGlobal("openReviewDocuSignComposer");
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

  // --- data mapping ----------------------------------------------------------
  // Map the in-memory matter + review state onto the component interfaces. The
  // shapes mirror the finalized backend contract (feat/overview-data): clause
  // verdict from `.decision` ('pass'|'review'|'fail'); the 'check' review_state
  // bucket is NOT used for the per-clause verdict.

  function hasAiReview() {
    const matter = state.selectedMatter;
    // Gate verdict surfaces SOLELY on whether the AI review ACTUALLY ran
    // (matter.ai_review_ran), NOT on whether a stored review_result with clause
    // verdicts exists. A deterministic-only matter can carry stored verdicts but
    // ai_review_ran===false; surfacing those verdicts would leak the demoted
    // deterministic result and unlock Approve/Send on a matter the AI never
    // reviewed. So when ai_review_ran is false the roster shows each clause's muted
    // "Not Reviewed" status (no verdict leak) and the footer disables Approve/Send.
    // This is the deterministic-ghost demotion, and it is INTENTIONAL.
    return Boolean(matter && matter.ai_review_ran === true);
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
      // The inbound Gmail sender (matter.sender), shown under the counterparty.
      // Empty for manual uploads -> the facts renderer omits the SENDER line.
      sender: String(matter.sender || "").trim(),
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

  // --- compose ---------------------------------------------------------------

  function render() {
    const container = panel();
    if (!container) return;

    // No matter loaded at all -> let the host's empty pane stand (clear).
    if (!state.selectedMatter) {
      container.innerHTML = "";
      return;
    }

    // PERSISTENT pane: the facts -> roster -> footer stack is ALWAYS rendered.
    // We no longer early-return an empty "No review yet" block when no AI review
    // has run; instead `aiReviewRan` drives HONEST placeholder states inside the
    // roster (each clause reads "Not Reviewed") and the footer (Approve / Send
    // disabled). Absent flag -> reviewed behaviour (the safe fallback).
    const aiReviewRan = hasAiReview();

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
    // (Per-party signatures live ONLY in the card-detail inspector pop-up, which
    // carries its own verified copy — they are deliberately NOT shown here.)
    composeFacts(factsEl, facts);
    mountFillSection(summaryBody);

    summaryBody.append(rosterEl, footerEl);
    composeRoster(rosterEl, clauses, aiReviewRan);
    composeFooter(footerEl, footer, aiReviewRan);
  }

  function composeFacts(el, facts) {
    if (typeof window !== "undefined" && typeof window.renderOverviewFacts === "function") {
      window.renderOverviewFacts(
        el,
        { counterparty: facts.counterparty, sender: facts.sender, facts: facts.facts },
        { onConfirm, onEntityFill },
      );
      return;
    }
    placeholder(el, "Facts", "renderOverviewFacts");
  }

  function composeRoster(el, clauses, aiReviewRan) {
    if (typeof window !== "undefined" && typeof window.renderOverviewRoster === "function") {
      window.renderOverviewRoster(
        el,
        { clauses, currentClauseId: state.selectedReviewClauseId || null, aiReviewRan },
        { onClauseClick },
      );
      return;
    }
    placeholder(el, "Clause roster", "renderOverviewRoster");
  }

  // Footer is ALWAYS rendered. Before an AI review has run (aiReviewRan===false)
  // BOTH actions are disabled/grayed — honest "nothing to approve/send yet" — on
  // top of the EXISTING approve gate (stale-playbook / already-approved). Send is
  // disabled pre-review too. Once the review has run, the existing gates decide.
  function composeFooter(el, footer, aiReviewRan) {
    if (typeof window !== "undefined" && typeof window.renderOverviewFooter === "function") {
      const preReview = aiReviewRan === false;
      const approveDisabled = preReview || footer.approveDisabled;
      const approveReason = preReview ? "Run the AI review to approve." : footer.approveReason;
      window.renderOverviewFooter(
        el,
        { approveDisabled, approveReason, sendDisabled: preReview },
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
