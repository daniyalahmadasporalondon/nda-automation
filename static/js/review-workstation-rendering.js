let reviewDocumentRenderRequestSequence = 0;

function reviewWorkstationModel() {
  return window.ReviewWorkstationModel || null;
}

// Runs the shipped RedlineEditContract sanitizer over the RAW server redlines so
// malformed edits (unknown action, missing paragraph_id, null/typeless inline
// diff ops) are dropped before they can throw in the render chain. Fail-open: if
// the contract bridge is missing the raw list is returned untouched -- this is a
// resilience guard, never a reason to withhold an otherwise-normal review.
function sanitizeReviewRedlines(rawEdits) {
  const contract = window.RedlineEditContract;
  if (!contract || typeof contract.normalizeRedlineEdits !== "function") {
    return Array.isArray(rawEdits) ? rawEdits : [];
  }
  try {
    return contract.normalizeRedlineEdits(rawEdits);
  } catch (error) {
    try {
      console.error("sanitizeReviewRedlines: sanitizer threw; falling back to raw edits", error);
    } catch (_loggingError) {
      // ignore logging failure
    }
    return Array.isArray(rawEdits) ? rawEdits : [];
  }
}

function renderResult(result, reviewedText) {
  pendingReviewSendMatterId = null;
  state.reviewDocumentRender = reviewDocumentRenderState(result);
  state.latestReviewResult = result;
  state.documentViewMode = defaultDocumentViewModeForReviewResult(result, state.reviewDocumentRender);
  syncDocumentViewModeButtons();
  state.reviewClauses = result.clauses || [];
  state.reviewParagraphs = result.paragraphs || [];
  state.reviewOriginalParagraphs = snapshotReviewParagraphs(state.reviewParagraphs);
  state.reviewExportOriginalParagraphs = snapshotReviewParagraphs(state.reviewParagraphs);
  // SANITIZE server redlines on the live path before anything renders them. The
  // RedlineEditContract sanitizer (modules/redline-edit-contract.mjs) drops
  // unknown actions, requires a paragraph_id, and filters malformed
  // inline_diff_operations (null / typeless ops) -- exactly the shapes that used
  // to throw synchronously deeper in the render chain and blank the whole
  // workstation. It was written for this and was never called here. Fail-open:
  // if the contract bridge is somehow unavailable, fall back to the raw edits so
  // a normal review is never withheld.
  state.reviewRedlines = sanitizeReviewRedlines(result.redline_edits) || [];
  state.reviewComments = [];
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  state.exportRedlineDecisions = {};
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  state.redlineDraft = null;
  state.redlineDraftDirty = false;
  // FIX 1: a fresh review result is the authoritative model; any prior pending
  // source-textarea edit has been superseded, so clear the dirty guard so the
  // textarea can re-sync from the new model.
  state.sourceTextDirty = false;
  state.reviewedClauseIds = {};
  state.reasoningTrailOpen = {};
  state.reviewResolution = null;
  state.approveServerBlocks = [];
  resetReviewEditHistory();
  state.reviewSourceText = reviewedText || studioNdaText.value.trim();
  state.clauseJumpIndexes = {};
  state.selectedReviewClauseId =
    state.reviewClauses.find((clause) => clauseStatus(clause).requiresAttention)?.id || state.reviewClauses[0]?.id || null;
  renderStudioResult(result);
  updateExportButtonState();
  requestMatterDocumentRenderPreview();
}

function defaultDocumentViewModeForReviewResult(result, renderState) {
  return reviewResultPrefersOriginalSurface(result, renderState) ? VIEW_MODE_ORIGINAL : VIEW_MODE_REDLINE;
}

function syncDocumentViewModeButtons() {
  if (typeof updateDocumentViewModeButtons === "function") {
    updateDocumentViewModeButtons();
  }
}

function reviewResultPrefersOriginalSurface(result, renderState) {
  if (renderState?.sourceFallback) return true;
  return sourceFidelityPrefersOriginalSurface(result?.source_fidelity);
}

function sourceFidelityPrefersOriginalSurface(sourceFidelity) {
  if (!sourceFidelity || typeof sourceFidelity !== "object") return false;
  const preferredMode = stringValue(sourceFidelity.preferred_render_mode || sourceFidelity.preferredRenderMode).toLowerCase();
  if (["source_pdf_preview", "original_pdf_preview", "source_preview", "original"].includes(preferredMode)) {
    return true;
  }
  const pdfFidelity = sourceFidelity.pdf_fidelity && typeof sourceFidelity.pdf_fidelity === "object"
    ? sourceFidelity.pdf_fidelity
    : {};
  const layoutMode = stringValue(pdfFidelity.layout_mode || pdfFidelity.layoutMode).toLowerCase();
  return layoutMode === "original_pdf_page_preview" || pdfFidelity.requires_source_preview === true;
}

function snapshotReviewParagraphs(paragraphs) {
  return (paragraphs || []).map((paragraph) => {
    const snapshot = {
      id: paragraph.id,
      index: paragraph.index,
      text: String(paragraph.text || ""),
    };
    if (paragraph.source_index !== undefined) snapshot.source_index = paragraph.source_index;
    if (paragraph.source_part !== undefined) snapshot.source_part = paragraph.source_part;
    // Capture paragraph-level formatting so a format-only change (alignment/font/
    // size with identical text) is diffable against this baseline. fontSize MUST be
    // captured: the extractor now records a paragraph's point size, and
    // paragraphFormatOps diffs paragraph.fontSize against the baseline -- omitting it
    // here makes every freshly-loaded paragraph read as a spurious "size N" change.
    if (paragraph.alignment !== undefined) snapshot.alignment = paragraph.alignment;
    if (paragraph.font !== undefined) snapshot.font = paragraph.font;
    if (paragraph.fontSize !== undefined) snapshot.fontSize = paragraph.fontSize;
    if (Array.isArray(paragraph.runs)) snapshot.runs = paragraph.runs.map((run) => ({ ...run }));
    return snapshot;
  });
}

function manualRedlineBaselineParagraphs() {
  return state.reviewExportOriginalParagraphs.length
    ? state.reviewExportOriginalParagraphs
    : state.reviewOriginalParagraphs;
}

function paragraphsAlignWithBaseline(paragraphs, baseline) {
  if (!Array.isArray(paragraphs) || !Array.isArray(baseline) || !baseline.length) return false;
  if (paragraphs.length !== baseline.length) return false;
  return paragraphs.every((paragraph, index) => String(paragraph.id || "") === String(baseline[index]?.id || ""));
}

function renderStudioEmpty() {
  state.latestReviewResult = null;
  state.reviewDocumentRender = null;
  reviewDocumentRenderRequestSequence += 1;
  showStudioSourceEditor();
  renderReviewRefreshNotice(null);
  studioMatchSummary.textContent = `0/${getClauseTotal()}`;
  studioResultMark.textContent = "-";
  studioResultMark.className = "";
  studioOverallTitle.textContent = "Awaiting review";
  studioResultMeta.textContent = "No clause review has run yet.";
  resetReviewEditHistory();
  if (state.reviewInspectorView === "overview") {
    // The Overview controller renders its own "No review yet" empty state when no
    // AI review has run, so it owns the pane here too. The merged Overview pane
    // also relocates + renders the Fill (Aspora-entity) tool into its bottom
    // section, so there is no separate "fill" branch any more.
    reviewOverviewController.render();
  } else if (state.reviewInspectorView === "structure") {
    reviewStructureController.render();
  } else {
    studioDetailPanel.innerHTML = "";
  }
  updateReviewInspectorTabs();
  updateExportButtonState();
  renderStudioClauseLane();
}

function updateExportButtonState() {
  // While a background AI review runs for the selected matter, the rendered review
  // is mid-flight: block Download/Send (and Approve) until it resolves. Treated
  // alongside staleReview as a "review not ready to act on" gate.
  //
  // This runs at LOAD time too (emptyState -> renderStudioEmpty), which can fire
  // BEFORE the global-bridge module defines window.MatterUtils. Guard the lookup so
  // the first paint never throws a ReferenceError (mirrors how the existing
  // MatterUtils calls below sit behind the studioSendButton early-return).
  const reviewInProgress = typeof MatterUtils !== "undefined"
    && MatterUtils.reviewInProgress(state.selectedMatter);
  const canExport = state.reviewClauses.length
    && (studioNdaText.value.trim() || state.reviewSourceText.trim())
    && !reviewInProgress;
  const staleReview = Boolean(state.selectedMatter?.review_refresh?.stale) || reviewInProgress;
  if (studioExportButton) {
    studioExportButton.disabled = !canExport || staleReview;
    studioExportButton.title = reviewInProgress
      ? "Reviewing… download available once the AI review finishes"
      : staleReview ? "Refresh review before downloading" : "Download";
  }
  if (!studioSendButton) {
    updateRedlineDraftControls();
    return;
  }
  const hasSendableMatter = Boolean(state.selectedMatter?.id);
  studioSendButton.hidden = !hasSendableMatter;
  const sendBlockReason = state.selectedMatter?.id ? MatterUtils.gmailSendBlock(state.selectedMatter, state.gmailStatus) : "";
  const sendLabel = sendBlockReason ? MatterUtils.gmailSendButtonLabel(sendBlockReason) : "Send Redline";
  const sendReadiness = reviewWorkstationModel()?.gmailSendReadiness({
    blockedLabel: sendLabel,
    canExport,
    hasSendableMatter,
    sendBlockReason,
    staleReview,
  }) || {
    ariaDisabled: String(!(canExport && hasSendableMatter && !staleReview)),
    canSend: Boolean(canExport && hasSendableMatter && !sendBlockReason && !staleReview),
    interactive: Boolean(canExport && hasSendableMatter && !staleReview),
    label: sendLabel,
    title: staleReview ? "Refresh review before sending a redline" : sendBlockReason || sendLabel,
  };
  // Keep the button clickable once a review has run, even when blocked, so a
  // click can surface *why* sending is blocked (openReviewSendComposer writes the
  // reason to the file-meta line) instead of leaving a silent, dead icon. The
  // .blocked class + aria-disabled mark it not-ready without swallowing the click.
  studioSendButton.disabled = !sendReadiness.interactive;
  studioSendButton.classList.toggle("blocked", sendReadiness.interactive && Boolean(sendBlockReason));
  studioSendButton.setAttribute("aria-disabled", sendReadiness.ariaDisabled);
  if (staleReview) {
    pendingReviewSendMatterId = null;
    setStudioSendButtonLabel(sendReadiness.label, sendReadiness.title);
  } else if (!sendReadiness.canSend) {
    pendingReviewSendMatterId = null;
    setStudioSendButtonLabel(sendReadiness.label, sendReadiness.title);
  } else {
    pendingReviewSendMatterId = null;
    setStudioSendButtonLabel(sendReadiness.label);
  }
  if (studioReviewedButton) {
    // Offer "Reviewed" only while the sole thing blocking send is the
    // human-review gate and it has not been signed off yet.
    const matter = state.selectedMatter;
    const reviewBlocked = Boolean(
      canExport && hasSendableMatter && matter
      && MatterUtils.needsHumanReview(matter) && !matter.human_reviewed,
    );
    studioReviewedButton.hidden = !reviewBlocked;
    if (reviewBlocked) {
      updateReviewedButtonScope();
      // No-jump header: the "Mark N clauses reviewed" count is derived from the
      // DETERMINISTIC first-pass, which is meaningless until an AI review has run.
      // Rather than HIDE the button on an unreviewed matter (a layout jump), keep
      // it in place and GRAY/disable it. Layer the ai_review_ran gate ON TOP of the
      // existing visibility: when shown, it is interactive only once the AI review
      // has actually run (aiReviewRan() -> matter.ai_review_ran === true). A
      // deterministic-only matter (ai_review_ran === false) stays disabled.
      const reviewed = aiReviewRan(matter);
      studioReviewedButton.disabled = !reviewed;
      studioReviewedButton.setAttribute("aria-disabled", String(!reviewed));
      if (!reviewed) {
        studioReviewedButton.title = "Run the AI review before marking clauses reviewed.";
      }
    }
  }
  if (typeof studioMarkExecutedButton !== "undefined" && studioMarkExecutedButton) {
    // The understated manual mark-executed affordance: shown only on a saved matter
    // that is NOT already executed (DocuSign completion or a prior mark would set
    // matterIsExecuted). It is the SECONDARY path for an NDA signed outside DocuSign,
    // so it never competes with the normal send/sign flow — quiet link-style button.
    const matter = state.selectedMatter;
    const showMark = Boolean(matter?.id) && !matterIsExecuted(matter);
    studioMarkExecutedButton.hidden = !showMark;
  }
  if (typeof studioRefreshStatusButton !== "undefined" && studioRefreshStatusButton) {
    // The on-demand "Refresh status" self-heal affordance: shown ONLY while a matter
    // has an ACTIVE (sent, non-terminal) DocuSign envelope — i.e. it is out for
    // signature but not yet completed/declined/voided. That is exactly the window in
    // which a missed completion webhook could leave it stuck, so a manual re-sync is
    // useful. Once terminal (executed via webhook or otherwise) the button hides.
    // Gate via the controller's hasActiveEnvelope() so the FE reads the SAME nested-
    // first envelope view the badge uses. Mirrors the mark-executed placement.
    const matter = state.selectedMatter;
    const controller = (typeof docusignSendController !== "undefined") ? docusignSendController : null;
    const active = Boolean(matter?.id)
      && controller
      && typeof controller.hasActiveEnvelope === "function"
      && controller.hasActiveEnvelope(matter);
    studioRefreshStatusButton.hidden = !active;
  }
  updateApproveReviewControl();
  updateRedlineDraftControls();
  // Keep the DocuSign "Send for signature" trigger + signature badge in sync with
  // the selected matter's state (visibility + sent/awaiting/signed label).
  if (typeof syncDocuSignTriggerButton === "function") syncDocuSignTriggerButton();
}

// Surface the header "Reviewed" button's scope: it flips EVERY needs-review
// clause at once, so the label/title state how many clauses a click affects.
// When all needs-review clauses are already acknowledged a click would un-review
// them, so the label disambiguates that toggle-OFF direction.
function updateReviewedButtonScope() {
  if (!studioReviewedButton) return;
  const ids = reviewClauseIds();
  const count = ids.length;
  if (!count) {
    studioReviewedButton.textContent = "Reviewed";
    studioReviewedButton.title = "Confirm you've checked the flagged clauses — this enables Send Redline";
    return;
  }
  const allAcknowledged = ids.every((clauseId) => clauseReviewAcknowledged(clauseId));
  const noun = `${count} ${count === 1 ? "clause" : "clauses"}`;
  if (allAcknowledged) {
    studioReviewedButton.textContent = `Unmark ${noun} reviewed`;
    studioReviewedButton.title = `Mark ${noun} as needing review again`;
  } else {
    studioReviewedButton.textContent = `Mark ${noun} reviewed`;
    studioReviewedButton.title = `Mark all ${noun} that need human review as reviewed — this enables Send Redline`;
  }
}

function setStudioSendButtonLabel(label = "Send Redline", title = label) {
  if (!studioSendButton) return;
  const effectiveLabel = label || "Send Redline";
  studioSendButton.setAttribute("aria-label", effectiveLabel);
  studioSendButton.title = title || effectiveLabel;
  studioSendButton.classList.toggle("confirming", effectiveLabel === "Confirm Send");
  studioSendButton.classList.toggle("sending", effectiveLabel === "Sending");
  const textNode = studioSendButton.querySelector(".send-button-label, .sr-only");
  if (textNode) {
    textNode.textContent = effectiveLabel;
  }
}

// ---------------------------------------------------------------------------
// Review-workspace shimmer skeletons.
//
// While a background AI review runs we replace the empty/stale split workspace
// with GENERIC shimmer skeletons that mirror the layout (a document-pane
// paragraph stack on the left + a fixed-count clause-row stack in the inspector
// on the right), PAIRED with truthful duration copy. The skeleton count is
// deliberately fixed and generic — it never previews the real clause count, so
// it can never imply "N clauses found" before the review returns. The animation
// is gated behind prefers-reduced-motion in CSS, not here.
//
// HONESTY: the copy ("Reviewing… this can take up to a minute.") pairs with the
// shimmer so it never implies an instant result on a 30–120s job. If the review
// pipeline exposes a per-stage/per-clause progress signal we surface it; today
// it does not (the async backend reports only in_progress / completed / failed),
// so we use the honest "still analysing" duration copy and never fake a bar.
//
// The skeleton is REMOVED the moment real content arrives: setReviewWorkspaceSkeleton(false)
// runs from exitReviewInFlightUi (poll terminal) before the result is rendered.

// A generic document-pane skeleton: a short fixed stack of paragraph blocks. The
// count is a neutral constant (NOT the document's real paragraph count).
function reviewSkeletonDocumentMarkup() {
  const para = (lines) => `
    <div class="review-skeleton-para">
      ${lines.map((cls) => `<div class="skeleton-block skeleton-line ${cls}"></div>`).join("")}
    </div>`;
  return `
    <div class="review-skeleton-doc" aria-hidden="true">
      ${para(["long", "long", "medium"])}
      ${para(["long", "medium"])}
      ${para(["long", "long", "long", "short"])}
      ${para(["medium", "long"])}
    </div>`;
}

// A generic inspector skeleton: a fixed small number of clause-style rows (a
// verdict pill + two text lines). Fixed count — never the real result count.
function reviewSkeletonInspectorMarkup() {
  const row = () => `
    <div class="review-skeleton-row">
      <div class="skeleton-block skeleton-pill"></div>
      <div class="skeleton-row-body">
        <div class="skeleton-block skeleton-line medium"></div>
        <div class="skeleton-block skeleton-line long"></div>
      </div>
    </div>`;
  return `
    <div class="review-skeleton-inspector" aria-hidden="true">
      ${row()}${row()}${row()}${row()}
    </div>`;
}

// Show/hide the review-workspace skeleton. When active, overlay the document
// pane (inside .studio-page-wrap) with a paragraph-stack skeleton + honest copy,
// and render clause-row skeletons into the inspector panel. When inactive, the
// overlays are removed so the real rendered content stands. Idempotent; guarded
// so a load order / test harness without the DOM is a no-op rather than a throw.
function setReviewWorkspaceSkeleton(active) {
  const pageWrap = document.querySelector(".studio-page-wrap");
  if (pageWrap) {
    let overlay = pageWrap.querySelector(".review-skeleton");
    if (active) {
      if (!overlay) {
        overlay = document.createElement("div");
        overlay.className = "review-skeleton";
        overlay.setAttribute("role", "status");
        overlay.setAttribute("aria-live", "polite");
        pageWrap.appendChild(overlay);
      }
      overlay.innerHTML = `
        <div class="review-skeleton-copy">
          <span class="skeleton-dot" aria-hidden="true"></span>
          <span>Reviewing… this can take up to a minute.</span>
        </div>
        ${reviewSkeletonDocumentMarkup()}`;
    } else if (overlay) {
      overlay.remove();
    }
  }

  // Inspector skeletons. The Structure sub-view owns its OWN in-progress skeleton
  // (contract-structure-view.js renders it from the same reviewInProgress signal),
  // so when Structure is active we re-render the detail pane to let it paint that.
  // For the Clause sub-view we paint the generic clause-row skeleton here directly.
  // The Overview pane keeps its persistent facts/roster placeholders. The skeleton
  // is dropped on the next real renderStudioDetail()/renderStudioResult().
  if (typeof studioDetailPanel !== "undefined" && studioDetailPanel) {
    const view = state.reviewInspectorView || "clause";
    if (active && view === "structure") {
      if (typeof renderStudioDetail === "function") renderStudioDetail();
    } else if (active && view === "clause") {
      studioDetailPanel.innerHTML = reviewSkeletonInspectorMarkup();
    } else if (!active) {
      const inspectorSkeleton = studioDetailPanel.querySelector(".review-skeleton-inspector");
      if (inspectorSkeleton) inspectorSkeleton.remove();
      // Re-render so the Structure tab drops its in-progress skeleton for the real map.
      if (view === "structure" && typeof renderStudioDetail === "function") renderStudioDetail();
    }
  }
}

function renderStudioResult(result) {
  const clauses = result.clauses || [];
  // A finished review supersedes any in-flight skeleton overlay; drop it before
  // painting the real result so a stale skeleton never lingers over content.
  if (typeof setReviewWorkspaceSkeleton === "function") setReviewWorkspaceSkeleton(false);
  renderReviewOverlayBanner();
  renderStudioSummary(clauses);
  renderStudioClauseLane();
  renderStudioDetail();
  renderStudioDocumentHighlights();
}

// --- Additive review-overlay surfacing ---------------------------------------
// A review OVERLAY can ELEVATE a clean AI "pass" to "review" and BLOCK send. As of
// the overlay-retirement, the only remaining overlay is the law/forum mismatch
// detector — the three structural-override coverage detectors (notwithstanding-
// carveout, incorporation-by-reference, definition-poison) were removed once the
// strengthened AI reviewer + verifier covered those traps, so apply_review_overlays
// now writes only the law/forum channel. The overlay writes its reason(s) onto the
// matter's review_state in matter_view.public_matter — NOT onto the AI review_result
// that drives the per-clause pills — so without this surface the reviewer sees every
// clause green and no explanation for the block. These helpers read the overlay
// channel off state.selectedMatter.review_state and (1) render a matter-level banner
// listing every active flag and (2) attach each clause-targeted reason to its clause
// as a review finding. ALL overlay sources share the same overlay_review_reasons
// channel, so this is ONE code path — no per-detector special-casing. When NO overlay
// fires (the common case now), collectOverlayFindings() returns [] and the banner
// stays hidden — no JS error on an empty overlay channel. SECURITY: overlay messages
// may carry document-derived text, so every interpolated value is escapeHtml()'d.

// Map an overlay reason_code -> the clause id it should attach to. An overlay that
// is not clause-targeted (absent / unknown code) is covered by the banner alone.
const OVERLAY_REASON_CODE_TO_CLAUSE_ID = {
  law_forum_mismatch: "governing_law",
};

// The matter's overlay-carrying review_state. This comes from the board/detail
// payload (public_matter), where apply_review_overlays ran — NOT from the AI
// review_result. Falls back gracefully to null when no matter is loaded.
function matterOverlayReviewState() {
  const reviewState = state.selectedMatter?.review_state;
  return reviewState && typeof reviewState === "object" ? reviewState : null;
}

// Collect EVERY active overlay finding from the single shared channel, normalised to
// { code, message, clauseId }. Sources unified here:
//   * overlay_review_reasons[] (+ scalar overlay_review_reason) — every detector
//   * law_forum_mismatch_reason — the law/forum overlay's specific reason field
// reason_codes[] is consulted only to TARGET a clause (code -> clause id); a message
// with no matching code is still surfaced (banner +, if law/forum, the gov-law clause).
function collectOverlayFindings() {
  const reviewState = matterOverlayReviewState();
  if (!reviewState) return [];
  const codes = Array.isArray(reviewState.reason_codes) ? reviewState.reason_codes.map((code) => String(code || "")) : [];
  const messages = [];
  const seen = new Set();
  const pushMessage = (raw, preferredClauseId) => {
    const message = String(raw || "").trim();
    if (!message || seen.has(message)) return;
    seen.add(message);
    // Target a clause: an explicit preferred id (law/forum), else the first reason
    // code that maps to a clause. Untargeted reasons get clauseId === null and are
    // covered by the banner alone.
    let clauseId = preferredClauseId || null;
    if (!clauseId) {
      for (const code of codes) {
        if (OVERLAY_REASON_CODE_TO_CLAUSE_ID[code]) {
          clauseId = OVERLAY_REASON_CODE_TO_CLAUSE_ID[code];
          break;
        }
      }
    }
    messages.push({ message, clauseId });
  };
  // 1) The shared additive channel (every overlay/detector writes here).
  const reasons = Array.isArray(reviewState.overlay_review_reasons) ? reviewState.overlay_review_reasons : [];
  reasons.forEach((reason) => pushMessage(reason));
  if (typeof reviewState.overlay_review_reason === "string") pushMessage(reviewState.overlay_review_reason);
  // 2) The law/forum overlay's specific reason field, always targeted at the
  //    governing-law clause (deduped against the shared channel above).
  if (reviewState.law_forum_mismatch && reviewState.law_forum_mismatch_reason) {
    pushMessage(reviewState.law_forum_mismatch_reason, OVERLAY_REASON_CODE_TO_CLAUSE_ID.law_forum_mismatch);
  }
  return messages;
}

// All overlay findings that should attach to a specific clause id, in order.
function overlayFindingsForClause(clauseId) {
  if (!clauseId) return [];
  return collectOverlayFindings().filter((finding) => finding.clauseId === clauseId);
}

// Render the matter-level banner. Lists every active overlay flag in plain English.
// Hidden (and emptied) when no overlay has fired. Every value is escaped.
function renderReviewOverlayBanner() {
  const banner = document.getElementById("studioOverlayBanner");
  if (!banner) return;
  const findings = collectOverlayFindings();
  if (!findings.length) {
    banner.hidden = true;
    banner.innerHTML = "";
    return;
  }
  const count = findings.length;
  const heading = count === 1
    ? "1 additional review check flagged this NDA"
    : `${count} additional review checks flagged this NDA`;
  const items = findings
    .map((finding) => `<li>${escapeHtml(finding.message)}</li>`)
    .join("");
  banner.innerHTML = `
    <div class="studio-overlay-banner-head">
      <strong>${escapeHtml(heading)}</strong>
      <span>Needs review before send.</span>
    </div>
    <ul class="studio-overlay-banner-list">${items}</ul>
  `;
  banner.hidden = false;
}

// Render the per-clause overlay-finding block for the clause detail panel, so a
// clause an overlay flagged no longer renders a bare green/pass with no reason.
// Returns "" when no overlay targets this clause. Every value is escaped.
function renderClauseOverlayFindingsBlock(clause) {
  const findings = overlayFindingsForClause(clause?.id);
  if (!findings.length) return "";
  const items = findings
    .map((finding) => `<p>${escapeHtml(finding.message)}</p>`)
    .join("");
  return `
    <div class="studio-detail-block clause-overlay-finding" data-card-section="overlay-finding">
      <small>Additional review check</small>
      ${items}
    </div>
  `;
}

function renderStudioSummary(clauses) {
  // Verdict gate: this overall PASS/FAIL/REVIEW mark + tally is the most
  // authoritative-looking surface in the studio. It must NEVER show a verdict the
  // AI never issued. On a deterministic-only matter (ai_review_ran === false) the
  // backend aggregate is absent and the code below would fall back to a JS
  // clauseStatus() recount — a "deterministic ghost". Gate the whole summary on
  // aiReviewRan() (matter.ai_review_ran === true is the sole discriminator) and
  // render the same "Awaiting review" Pending state as renderStudioEmpty() instead.
  // A deterministic-only matter never surfaces a verdict here.
  if (!aiReviewRan()) {
    studioMatchSummary.textContent = `0/${getClauseTotal(clauses)}`;
    studioResultMark.textContent = "-";
    studioResultMark.className = "";
    studioOverallTitle.textContent = "Not reviewed";
    studioResultMeta.textContent = "No AI review has run yet. Run Review to see verdicts.";
    return;
  }
  // The overall verdict is NOT re-derived from JS clause counts here. The backend
  // ran the canonical aggregate (aggregate_review_state -> review_state, including
  // the document-level send gates) and attaches it as latestReviewResult.review_state.
  // CONSUME that authoritative state/.label/.blocks_send for the overall PASS/FAIL/
  // REVIEW mark and title. The pass/total numerator below is a display tally only;
  // it never decides the overall verdict.
  const reviewState = state.latestReviewResult?.review_state;
  const counts = reviewState?.counts;
  const passedCount = reviewStateCount(counts, "pass", clauses.filter((clause) => clauseStatus(clause).passes).length);
  // FE-only overlay: once every needs-review clause is acknowledged, the authoritative
  // "review" verdict reads as REVIEWED. The backend has no notion of this local ack,
  // so it is layered on top of (never replaces) the authoritative state.
  const authoritativeState = String(reviewState?.state || "").toLowerCase();
  const isFail = authoritativeState
    ? authoritativeState === "check"
    : clauses.some((clause) => clauseStatus(clause).fails);
  const isReview = !isFail && (authoritativeState
    ? authoritativeState === "review" || Boolean(reviewState?.blocks_send)
    : clauses.some((clause) => clauseStatus(clause).needsReview));
  const humanReviewComplete = isReview && humanReviewAcknowledged();
  const reviewCount = reviewStateCount(counts, "review", clauses.filter((clause) => clauseStatus(clause).needsReview).length);
  const failedCount = reviewStateCount(counts, "check", clauses.filter((clause) => clauseStatus(clause).fails).length);
  const unresolvedReviewCount = humanReviewComplete ? 0 : reviewCount;
  studioMatchSummary.textContent = `${passedCount}/${getClauseTotal(clauses)}`;
  studioResultMark.textContent = isFail ? "FAIL" : humanReviewComplete ? "REVIEWED" : isReview ? "REVIEW" : "PASS";
  studioResultMark.className = isFail ? "check" : humanReviewComplete ? "pass" : isReview ? "review" : "pass";
  studioOverallTitle.textContent = isFail
    ? "Does not meet requirements"
    : isReview && !humanReviewComplete
      ? "Needs review"
      : humanReviewComplete
        ? "Reviewed"
      : "Meets requirements";
  const warning = reviewWarningSummary();
  studioResultMeta.textContent = warning || summaryStatusText(failedCount, unresolvedReviewCount, { humanReviewComplete });
}

function summaryStatusText(failedCount, reviewCount, { humanReviewComplete = false } = {}) {
  const reviewedMessage = "All human-review clauses have been reviewed.";
  if (failedCount && reviewCount) {
    return `${failedCount} ${failedCount === 1 ? "clause needs" : "clauses need"} fixing; ${reviewCount} ${reviewCount === 1 ? "needs" : "need"} human review.`;
  }
  if (failedCount) {
    const failedMessage = `${failedCount} hard ${failedCount === 1 ? "clause has" : "clauses have"} failed.`;
    return humanReviewComplete ? `${failedMessage} ${reviewedMessage}` : failedMessage;
  }
  if (reviewCount) {
    return `${reviewCount} ${reviewCount === 1 ? "clause needs" : "clauses need"} human review before send.`;
  }
  if (humanReviewComplete) {
    return reviewedMessage;
  }
  return "All hard clauses are currently satisfied.";
}

function reviewStateCount(counts, key, fallback) {
  if (!counts || typeof counts !== "object") return fallback;
  const value = Number(counts[key]);
  return Number.isFinite(value) ? value : fallback;
}

function reviewWarningSummary() {
  const trust = state.latestReviewResult?.evidence_trust;
  if (trust?.status === "flagged") {
    const firstError = Array.isArray(trust.errors) && trust.errors.length ? ` ${trust.errors[0]}` : "";
    return `Evidence provenance warning.${firstError}`;
  }
  const warnings = Array.isArray(state.latestReviewResult?.review_warnings) ? state.latestReviewResult.review_warnings : [];
  const firstWarning = warnings.find((warning) => warning?.message);
  return firstWarning?.message || "";
}

function renderClauseExportState(clause, canDecide, included) {
  if (!canDecide || included) return "";
  return '<span class="studio-export-state ignored">Ignored in export</span>';
}

function renderClauseCommentState(clause) {
  if (!hasReviewResults() || !clauseReviewComment(clause.id)) return "";
  return '<span class="studio-comment-state">Comment</span>';
}

function reviewedClauseMap() {
  if (!state.reviewedClauseIds || typeof state.reviewedClauseIds !== "object") {
    state.reviewedClauseIds = {};
  }
  return state.reviewedClauseIds;
}

function reviewClauseIds() {
  return state.reviewClauses
    .filter((clause) => clauseStatus(clause).needsReview)
    .map((clause) => clause.id)
    .filter(Boolean);
}

function clauseReviewAcknowledged(clauseId) {
  const reviewedMap = reviewedClauseMap();
  if (Object.prototype.hasOwnProperty.call(reviewedMap, clauseId)) {
    return reviewedMap[clauseId] === true;
  }
  return Boolean(state.selectedMatter?.human_reviewed);
}

function humanReviewAcknowledged() {
  const ids = reviewClauseIds();
  return ids.length > 0 && ids.every((clauseId) => clauseReviewAcknowledged(clauseId));
}

function renderActiveClauseStatusToggle(clause, status) {
  const reviewed = status.needsReview && clauseReviewAcknowledged(clause.id);
  const label = verdictPillLabel(status, reviewed);
  if (!status.needsReview) {
    return `<span class="active-clause-status ${escapeHtml(status.tone)}">${escapeHtml(label)}</span>`;
  }
  return `
    <button
      class="active-clause-status ${escapeHtml(status.tone)} ${reviewed ? "reviewed" : ""}"
      type="button"
      data-review-action="mark-reviewed"
      data-review-clause-id="${escapeHtml(clause.id)}"
      aria-pressed="${reviewed ? "true" : "false"}"
      title="${escapeHtml(reviewed ? "Mark as needs review" : "Mark reviewed")}"
    >${escapeHtml(label)}</button>
  `;
}

function verdictPillLabel(status, reviewed = false) {
  if (reviewed) return "Reviewed";
  if (status.fails) return "Fail";
  if (status.needsReview) return "Needs Review";
  if (status.passes) return "Pass";
  return status.issueLabel || "Needs review";
}

function renderClauseCommentBlock(clause) {
  if (!hasReviewResults()) return "";
  const comment = clauseReviewComment(clause.id);
  return `
    <div class="studio-detail-block comment-block">
      <small>Attach comment</small>
      <textarea class="review-comment-input" data-review-comment-clause-id="${escapeHtml(clause.id)}" rows="4" placeholder="Leave a comment for Word export">${escapeHtml(comment?.text || "")}</textarea>
    </div>
  `;
}

function getClauseTotal(clauses = []) {
  return clauses.length || state.playbookClauses.length || 0;
}

function hasReviewResults() {
  return reviewWorkstationModel()?.hasReviewResults(state) ?? state.reviewClauses.length > 0;
}

function defaultExportClauseDecisions(clauses, redlines) {
  if (reviewWorkstationModel()) return reviewWorkstationModel().defaultExportClauseDecisions(clauses, redlines);
  const clausesWithRedlines = new Set((redlines || []).map((edit) => edit.clause_id).filter(Boolean));
  return Object.fromEntries((clauses || []).map((clause) => [
    clause.id,
    clausesWithRedlines.has(clause.id),
  ]));
}

function defaultRedlineTemplateSelections(redlines) {
  if (reviewWorkstationModel()) return reviewWorkstationModel().defaultRedlineTemplateSelections(redlines);
  const selections = {};
  (redlines || []).forEach((edit) => {
    const selected = (edit.template_options || []).find((option) => option.selected) || (edit.template_options || [])[0];
    if (selected?.id) selections[edit.id] = selected.id;
  });
  return selections;
}

function applyMatterRedlineDraft(draft) {
  state.redlineDraft = draft && typeof draft === "object" ? draft : null;
  state.redlineDraftDirty = false;
  if (!state.redlineDraft) {
    resetReviewEditHistory();
    updateRedlineDraftControls();
    return;
  }
  applyDraftClauseDecisions(state.redlineDraft.clause_decisions);
  applyDraftRedlineDecisions(state.redlineDraft.redline_decisions);
  applyDraftTemplateSelections(state.redlineDraft.template_selections);
  applyDraftReviewedClauseIds(state.redlineDraft.reviewed_clause_ids);
  applyDraftManualRedlines(state.redlineDraft.manual_redline_edits);
  applyDraftReviewComments(state.redlineDraft.review_comments);
  renderStudioResult({ clauses: state.reviewClauses });
  resetReviewEditHistory();
  updateRedlineDraftControls();
}

function resetCurrentRedlineDraftToDefaults() {
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  state.exportRedlineDecisions = {};
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  state.reviewedClauseIds = {};
  state.reviewComments = [];
  state.reviewParagraphs = state.reviewParagraphs.map((paragraph) => {
    const original = manualRedlineBaselineParagraphs().find((item) => item.id === paragraph.id);
    return original ? { ...paragraph, text: original.text } : paragraph;
  });
  syncReviewSourceFromParagraphs();
  state.redlineDraft = null;
  state.redlineDraftDirty = false;
  resetReviewEditHistory();
  renderStudioResult({ clauses: state.reviewClauses });
  updateRedlineDraftControls();
}

function applyDraftClauseDecisions(decisions) {
  if (!decisions || typeof decisions !== "object") return;
  Object.entries(decisions).forEach(([clauseId, included]) => {
    if (state.reviewClauses.some((clause) => clause.id === clauseId)) {
      state.exportClauseDecisions[clauseId] = Boolean(included);
    }
  });
}

function applyDraftRedlineDecisions(decisions) {
  if (!decisions || typeof decisions !== "object") return;
  const validRedlineIds = new Set(state.reviewRedlines.map((edit) => edit.id));
  Object.entries(decisions).forEach(([redlineId, included]) => {
    if (validRedlineIds.has(redlineId)) {
      state.exportRedlineDecisions[redlineId] = Boolean(included);
    }
  });
}

function applyDraftReviewedClauseIds(reviewedIds) {
  state.reviewedClauseIds = {};
  if (!reviewedIds || typeof reviewedIds !== "object") return;
  Object.entries(reviewedIds).forEach(([clauseId, reviewed]) => {
    if (state.reviewClauses.some((clause) => clause.id === clauseId)) {
      state.reviewedClauseIds[clauseId] = reviewed === true;
    }
  });
}

function applyDraftTemplateSelections(selections) {
  if (!selections || typeof selections !== "object") return;
  const validRedlineIds = new Set(state.reviewRedlines.map((edit) => edit.id));
  Object.entries(selections).forEach(([editId, optionId]) => {
    if (validRedlineIds.has(editId) && optionId) {
      state.redlineTemplateSelections[editId] = String(optionId);
    }
  });
}

function applyDraftManualRedlines(manualRedlines) {
  if (!Array.isArray(manualRedlines) || !manualRedlines.length) return;
  const redlineByParagraph = new Map();
  manualRedlines.forEach((redline) => {
    if (redline?.paragraph_id) redlineByParagraph.set(String(redline.paragraph_id), redline);
  });
  state.reviewParagraphs = state.reviewParagraphs.map((paragraph) => {
    const redline = redlineByParagraph.get(String(paragraph.id));
    if (!redline) return paragraph;
    const replacement = redline.action === REDLINE_DELETE_PARAGRAPH ? "" : String(redline.replacement_text || "");
    return { ...paragraph, text: replacement };
  });
  syncReviewSourceFromParagraphs();
}

function applyDraftReviewComments(reviewComments) {
  state.reviewComments = normalizeReviewComments(reviewComments);
}

function normalizeReviewComments(reviewComments) {
  if (!Array.isArray(reviewComments)) return [];
  return reviewComments
    .filter((comment) => comment && typeof comment === "object" && String(comment.text || "").trim())
    .map((comment) => ({
      ...comment,
      id: String(comment.id || `comment-${comment.clause_id || comment.paragraph_id || Date.now()}`),
      scope: String(comment.scope || (comment.selected_text ? "selection" : comment.clause_id ? "clause" : "paragraph")),
      text: String(comment.text || "").trim(),
    }));
}

function currentReviewComments() {
  return normalizeReviewComments(state.reviewComments)
    .map((comment) => (comment.scope === "clause" || (comment.clause_id && !comment.paragraph_id)
      ? { ...comment, ...reviewCommentTargetForClause(comment.clause_id) }
      : { ...comment, ...reviewCommentTargetForParagraph(comment.paragraph_id) }))
    .filter((comment) => String(comment.text || "").trim() && (comment.paragraph_id || comment.clause_id));
}

function clauseReviewComment(clauseId) {
  return normalizeReviewComments(state.reviewComments).find((comment) => comment.clause_id === clauseId) || null;
}

function setClauseReviewComment(clauseId, text) {
  const clause = state.reviewClauses.find((item) => item.id === clauseId);
  if (!clause) return;
  const existing = clauseReviewComment(clauseId);
  const trimmedText = String(text || "").trim();
  state.reviewComments = normalizeReviewComments(state.reviewComments)
    .filter((comment) => comment.clause_id !== clauseId);
  if (trimmedText) {
    state.reviewComments.push({
      ...(existing || {}),
      ...reviewCommentTargetForClause(clauseId),
      author: existing?.author || "Reviewer",
      clause_id: clauseId,
      clause_name: clause.name || clauseId,
      created_at: existing?.created_at || new Date().toISOString(),
      id: existing?.id || `comment-${clauseId}`,
      scope: "clause",
      text: trimmedText,
    });
  }
  markRedlineDraftDirty();
  renderStudioClauseLane();
  updateExportButtonState();
}

function reviewCommentTargetForClause(clauseId) {
  const clause = state.reviewClauses.find((item) => item.id === clauseId);
  const targetParagraphId = firstClauseParagraphId(clauseId, clause);
  const paragraph = state.reviewParagraphs.find((item) => item.id === targetParagraphId);
  const target = {};
  if (targetParagraphId) target.paragraph_id = targetParagraphId;
  if (paragraph?.index !== undefined) target.paragraph_index = paragraph.index;
  if (paragraph?.source_index !== undefined) target.source_index = paragraph.source_index;
  return target;
}

function reviewCommentTargetForParagraph(paragraphId) {
  const paragraph = state.reviewParagraphs.find((item) => item.id === paragraphId);
  const target = {};
  if (paragraph?.id) target.paragraph_id = paragraph.id;
  if (paragraph?.index !== undefined) target.paragraph_index = paragraph.index;
  if (paragraph?.source_index !== undefined) target.source_index = paragraph.source_index;
  return target;
}

function setParagraphReviewComment(paragraphId, text) {
  const paragraph = state.reviewParagraphs.find((item) => item.id === paragraphId);
  if (!paragraph) return;
  const commentId = `comment-paragraph-${paragraphId}`;
  upsertReviewComment({
    ...reviewCommentTargetForParagraph(paragraphId),
    author: "Reviewer",
    created_at: new Date().toISOString(),
    id: commentId,
    scope: "paragraph",
    text,
  });
}

function setSelectedTextReviewComment(paragraphId, selectionInfo, text) {
  const paragraph = state.reviewParagraphs.find((item) => item.id === paragraphId);
  if (!paragraph || !selectionInfo?.selectedText) return;
  const commentId = `comment-selection-${paragraphId}-${selectionInfo.startOffset}-${selectionInfo.endOffset}`;
  upsertReviewComment({
    ...reviewCommentTargetForParagraph(paragraphId),
    author: "Reviewer",
    created_at: new Date().toISOString(),
    id: commentId,
    scope: "selection",
    selected_text: selectionInfo.selectedText,
    selection_end: selectionInfo.endOffset,
    selection_start: selectionInfo.startOffset,
    text,
  });
}

// Snapshot the whole comment set onto the shared viewer undo stack before a
// discrete comment change, so the Undo button reverts add / edit / reply /
// resolve / delete just like it reverts text edits. (Clause-lane comments are
// keystroke-driven and keep native textarea undo, so they are not snapshotted.)
function pushReviewCommentsHistory() {
  if (typeof pushReviewEditHistoryEntry !== "function") return;
  pushReviewEditHistoryEntry({
    type: "review_comments",
    snapshot: normalizeReviewComments(state.reviewComments).map((comment) => ({ ...comment })),
  });
}

function upsertReviewComment(comment) {
  pushReviewCommentsHistory();
  const trimmedText = String(comment.text || "").trim();
  state.reviewComments = normalizeReviewComments(state.reviewComments).filter((item) => item.id !== comment.id);
  if (trimmedText) {
    state.reviewComments.push({
      ...comment,
      text: trimmedText,
    });
  }
  markRedlineDraftDirty();
  renderStudioDocumentHighlights();
  renderStudioClauseLane();
  updateExportButtonState();
}

function firstClauseParagraphId(clauseId, clause) {
  const matched = Array.isArray(clause?.matched_paragraph_ids)
    ? clause.matched_paragraph_ids.find(Boolean)
    : "";
  if (matched) return String(matched);
  const redline = state.reviewRedlines.find((edit) => edit.clause_id === clauseId && edit.paragraph_id);
  return redline?.paragraph_id ? String(redline.paragraph_id) : "";
}

function clauseExportIncluded(clauseId) {
  return reviewWorkstationModel()?.clauseExportIncluded(state, clauseId) ?? state.exportClauseDecisions[clauseId] !== false;
}

function redlineExportIncluded(edit) {
  if (reviewWorkstationModel()) return reviewWorkstationModel().redlineExportIncluded(state, edit);
  if (edit && edit.id && Object.prototype.hasOwnProperty.call(state.exportRedlineDecisions, edit.id)) {
    return state.exportRedlineDecisions[edit.id] !== false;
  }
  return clauseExportIncluded(edit.clause_id);
}

function effectiveReviewRedlines() {
  return reviewWorkstationModel()
    ? reviewWorkstationModel().effectiveReviewRedlines(state)
    : state.reviewRedlines.filter(redlineExportIncluded).map(applyTemplateSelectionToRedline);
}

function applyTemplateSelectionToRedline(edit) {
  if (reviewWorkstationModel()) {
    return reviewWorkstationModel().applyTemplateSelectionToRedline(edit, state.redlineTemplateSelections);
  }
  const selectedOptionId = state.redlineTemplateSelections[edit.id];
  const selectedOption = (edit.template_options || []).find((option) => option.id === selectedOptionId);
  if (!selectedOption) return { ...edit };

  const nextEdit = {
    ...edit,
    template_options: (edit.template_options || []).map((option) => ({
      ...option,
      selected: option.id === selectedOption.id,
    })),
  };
  const selectedReplacement = selectedOption.replacement_text || selectedOption.text || "";
  const selectedInsert = selectedOption.insert_text || selectedOption.replacement_text || selectedOption.text || "";
  if (edit.action === REDLINE_INSERT_AFTER_PARAGRAPH) {
    if (selectedInsert.trim()) nextEdit.insert_text = selectedInsert;
    if (selectedReplacement.trim()) nextEdit.replacement_text = selectedReplacement;
  } else if (selectedReplacement.trim()) {
    nextEdit.replacement_text = selectedReplacement;
  }
  if (Array.isArray(selectedOption.inline_diff_operations)) {
    nextEdit.inline_diff_operations = selectedOption.inline_diff_operations;
  } else {
    delete nextEdit.inline_diff_operations;
  }
  return nextEdit;
}

function getDisplayClauses() {
  return hasReviewResults()
    ? state.reviewClauses
    : state.playbookClauses.map((clause) => ({ ...clause, status: "idle" }));
}

function getSelectedReviewClause() {
  return reviewWorkstationModel()?.selectedReviewClause(state)
    || state.reviewClauses.find((item) => item.id === state.selectedReviewClauseId);
}

function getSelectedRedlineEdits() {
  return effectiveReviewRedlines().filter((edit) => edit.clause_id === state.selectedReviewClauseId);
}

function bindClauseSelection(container, selector, datasetKey) {
  container.querySelectorAll(selector).forEach((item) => {
    item.addEventListener("click", () => {
      selectReviewClause(item.dataset[datasetKey], { jump: true });
    });
  });
}

function bindExportDecisionControls(container) {
  container.querySelectorAll("[data-export-clause-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setClauseExportDecision(button.dataset.exportClauseId, button.dataset.exportDecision === "include");
    });
  });
  container.querySelectorAll("[data-export-redline-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setRedlineExportDecision(button.dataset.exportRedlineId, button.dataset.exportDecision === "include");
    });
  });
}

function bindReviewAcknowledgementControls(container) {
  container.querySelectorAll("[data-review-action='mark-reviewed']").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      markMatterReviewed({ sourceButton: button });
    });
  });
}

function setRedlineExportDecision(redlineId, included) {
  if (!redlineId) return;
  const edit = state.reviewRedlines.find((item) => item.id === redlineId);
  const hadPrevious = Object.prototype.hasOwnProperty.call(state.exportRedlineDecisions, redlineId);
  const previousIncluded = state.exportRedlineDecisions[redlineId];
  const currentIncluded = edit ? redlineExportIncluded(edit) : previousIncluded !== false;
  if (currentIncluded !== included) {
    pushReviewEditHistoryEntry({
      editId: redlineId,
      hadPrevious,
      previousIncluded,
      type: "redline_export_decision",
    });
  }
  state.exportRedlineDecisions[redlineId] = included;
  if (edit?.clause_id) state.selectedReviewClauseId = edit.clause_id;
  markRedlineDraftDirty();
  renderStudioResult({ clauses: state.reviewClauses });
  if (included && edit?.clause_id) {
    const clause = state.reviewClauses.find((item) => item.id === edit.clause_id);
    requestAnimationFrame(() => jumpToClauseSource(clause));
  }
  updateExportButtonState();
}

function setClauseExportDecision(clauseId, included) {
  const hadPrevious = Object.prototype.hasOwnProperty.call(state.exportClauseDecisions, clauseId);
  const previousIncluded = state.exportClauseDecisions[clauseId];
  const currentIncluded = clauseExportIncluded(clauseId);
  if (currentIncluded !== included) {
    pushReviewEditHistoryEntry({
      clauseId,
      hadPrevious,
      previousIncluded,
      type: "clause_export_decision",
    });
  }
  state.exportClauseDecisions[clauseId] = included;
  state.selectedReviewClauseId = clauseId;
  markRedlineDraftDirty();
  renderStudioResult({ clauses: state.reviewClauses });
  if (included) {
    const clause = state.reviewClauses.find((item) => item.id === clauseId);
    requestAnimationFrame(() => jumpToClauseSource(clause));
  }
  updateExportButtonState();
}

function setRedlineTemplateSelection(editId, optionId) {
  const hadPrevious = Object.prototype.hasOwnProperty.call(state.redlineTemplateSelections, editId);
  const previousOptionId = state.redlineTemplateSelections[editId];
  // The checked radio tracks state.redlineTemplateSelections directly (Option B), so a
  // click that does not change the staged option is a true no-op — the highlight
  // already shows it and nothing about the export would change.
  if (previousOptionId === optionId) return;
  pushReviewEditHistoryEntry({
    editId,
    hadPrevious,
    previousOptionId,
    type: "redline_template_selection",
  });
  state.redlineTemplateSelections[editId] = optionId;
  markRedlineDraftDirty();
  renderStudioResult({ clauses: state.reviewClauses });
  // Picking a different template option changes the proposed wording, which can
  // change the clause verdict. We no longer auto-run the AI single-clause check
  // here: AI review is gated behind the explicit "Refresh with AI" action. Flag
  // the review as possibly stale so the indicator + button surface instead.
  const edit = state.reviewRedlines.find((item) => item.id === editId);
  if (edit?.clause_id && state.selectedMatter?.id && typeof markReviewMayBeStaleFromEdit === "function") {
    markReviewMayBeStaleFromEdit();
  }
}

// Build an editedParagraphs overlay for a template-option selection so that
// scheduleClauseReassess evaluates the PROPOSED text rather than the stale
// source text.  Returns undefined when the overlay cannot be computed (e.g.
// insert-after action or missing paragraph), letting the caller fall back to
// the full edited_text path.
function _buildEditedParagraphsForTemplateOption(edit, optionId) {
  if (!edit || !Array.isArray(state.reviewParagraphs) || !state.reviewParagraphs.length) return undefined;

  const selectedOption = (edit.template_options || []).find((opt) => opt.id === optionId);
  if (!selectedOption) return undefined;

  // INSERT_AFTER adds a new paragraph rather than replacing an existing one.
  // Building a fully-correct overlay for that case is complex (paragraph
  // ordering, index assignment); skip it here so we fall back to the stale
  // edited_text path rather than sending wrong data.  This is a known
  // limitation — tracked as a follow-up (insert-after reassess).
  if (edit.action === REDLINE_INSERT_AFTER_PARAGRAPH) return undefined;

  // Resolve the target paragraph: use the edit's own paragraph_id first, then
  // fall back to the clause's first matched paragraph (mirrors the viewer path).
  const targetParagraphId = edit.paragraph_id
    || (() => {
      const clause = state.reviewClauses.find((c) => c.id === edit.clause_id);
      return Array.isArray(clause?.matched_paragraph_ids) ? clause.matched_paragraph_ids[0] : undefined;
    })();
  if (!targetParagraphId) return undefined;

  // Compute the replacement text exactly as applyTemplateSelectionToRedline does.
  const proposedText = selectedOption.replacement_text || selectedOption.text || "";
  if (!proposedText.trim()) return undefined;

  // Build a shallow copy of all paragraphs, overlaying only the target paragraph's
  // text with the proposed wording.  Never mutates the live state.reviewParagraphs
  // entries — spreads produce new objects.
  return state.reviewParagraphs.map((p) => {
    const base = { id: p.id, index: p.index, source_index: p.source_index, text: p.text };
    if (String(p.id) === String(targetParagraphId)) {
      base.text = proposedText;
    }
    return base;
  });
}

// The STAGED EXPORT option id for an edit: the value state.redlineTemplateSelections
// resolves to (seeded with the backend default, overwritten on an explicit pick),
// falling back to the edit's own selected option. This is the SAME option
// applyTemplateSelectionToRedline stages for the Fixed-clause preview and the exported
// DOCX — so binding the checked radio to it (Option B) guarantees the checked state
// and the exported law can never disagree.
function selectedRedlineTemplateOptionId(edit) {
  return state.redlineTemplateSelections?.[edit.id]
    || (edit.template_options || []).find((option) => option.selected)?.id
    || "";
}

// The "Dynamic" engine badge was removed from the UI (product decision). The
// dynamic/native split still drives review behaviour — it's just no longer
// surfaced as a pill in the navigator or the active-clause heading. Kept as a
// no-op so the call sites need no change; restore the span here to bring it back.
function clauseEngineBadge() {
  return "";
}

// ── Governing-law <-> picked-entity concurrence ─────────────────────────────
// The Fill tool's chosen Aspora entity carries a registry governing law; the
// document states its own. When both are known and differ, the Governing Law
// clause surfaces a NON-authoritative hint (a note banner + a one-click redline
// picker) in real time. This is purely advisory: it never overrides the backend
// verdict and never re-runs the backend review. The backend/AI engine is the
// source of truth for the clause status (see clauseDisplayStatus).

// Apply a governing-law fix from the concurrence picker: replace the matched
// governing-law paragraph with a clean approved sentence (shown as a tracked redline
// in the document) and re-render so the concurrence re-evaluates live.
function applyGoverningLawRedline(lawPhrase, lawLabel) {
  const gl = state.reviewClauses.find((clause) => clause.id === "governing_law");
  const paraId = gl && Array.isArray(gl.matched_paragraph_ids) ? gl.matched_paragraph_ids[0] : "";
  const para = paraId ? state.reviewParagraphs.find((item) => item.id === paraId) : null;
  if (!para) return;
  const phrase = String(lawPhrase || lawLabel || "").trim();
  if (!phrase) return;
  const newText = `This Agreement shall be governed by the laws of ${phrase}.`;
  if (newText === para.text) return;
  if (typeof pushReviewEditHistoryEntry === "function") {
    pushReviewEditHistoryEntry({ paragraphId: para.id, previousText: para.text, type: "paragraph_text" });
  }
  para.text = newText;
  para.clauseRedlineWholeParagraph = true;  // render this clause redline as a clean whole-paragraph replacement
  if (typeof syncReviewSourceFromParagraphs === "function") syncReviewSourceFromParagraphs();
  if (typeof markRedlineDraftDirty === "function") markRedlineDraftDirty();
  if (typeof markSourceEdited === "function") markSourceEdited("Governing law redline", { preserveSourceDocument: true });
  if (typeof renderStudioDocumentHighlights === "function") renderStudioDocumentHighlights();
  renderStudioClauseLane();
  renderStudioDetail();
}
// Escape a playbook-sourced match term so it can be embedded in a RegExp safely
// (the terms are author-controlled approved-option labels/aliases, but they may
// carry regex metacharacters like the comma+space in "Ontario, Canada").
function escapeGoverningLawTerm(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Build the [label, matchTerms[]] list of jurisdictions to detect in the document
// DIRECTLY from the loaded governing_law clause's playbook-sourced approved
// options — never a hardcoded jurisdiction list. Each option contributes its
// label, value, id, and aliases as case-insensitive match terms, so a
// jurisdiction added/removed/renamed in the playbook is recognized automatically.
// The canonical home is clause.rules.approved_options ({id,label,value,aliases});
// clause.approved_options (a flattened mirror) and clause.approved_laws (a flat
// label list) are tolerated as fallbacks so detection works across review-result
// shapes. Returns [] when no governing_law clause / options are loaded.
function documentGoverningLaws() {
  const clauses = Array.isArray(state.reviewClauses) ? state.reviewClauses : [];
  const clause = clauses.find((item) => item && item.id === "governing_law");
  if (!clause) return [];
  const rules = clause.rules && typeof clause.rules === "object" ? clause.rules : null;
  const optionSource = (rules && Array.isArray(rules.approved_options) ? rules.approved_options : null)
    || (Array.isArray(clause.approved_options) ? clause.approved_options : null)
    || (Array.isArray(clause.approved_laws) ? clause.approved_laws : null)
    || [];
  const laws = [];
  for (const option of optionSource) {
    let label = "";
    const terms = [];
    if (option && typeof option === "object") {
      label = String(option.label || option.value || option.id || "").trim();
      for (const key of ["label", "value", "id"]) {
        const term = String(option[key] || "").trim();
        if (term) terms.push(term);
      }
      if (Array.isArray(option.aliases)) {
        for (const alias of option.aliases) {
          const term = String(alias || "").trim();
          if (term) terms.push(term);
        }
      }
    } else {
      // Flat approved_laws entry: the string is both the label and the only term.
      label = String(option || "").trim();
      if (label) terms.push(label);
    }
    if (!label || !terms.length) continue;
    laws.push([label, terms]);
  }
  return laws;
}

function detectDocumentGoverningLaw() {
  const laws = documentGoverningLaws();
  if (!laws.length) return "";
  const paragraphs = Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [];
  for (const paragraph of paragraphs) {
    const text = String((paragraph && paragraph.text) || "");
    // Operative governing-law language only — never an "incorporated under the
    // laws of X" recital, which names a party's jurisdiction, not the contract's.
    if (!/governing\s+law|governed\s+by|construed\s+in\s+accordance/i.test(text)) continue;
    for (const [label, terms] of laws) {
      // Word-boundary, case-insensitive match on any of the option's
      // label/value/id/aliases. \b around an escaped multi-word term still
      // anchors on the outer word characters (e.g. "England and Wales").
      if (terms.some((term) => new RegExp(`\\b${escapeGoverningLawTerm(term)}\\b`, "i").test(text))) {
        return label;
      }
    }
  }
  return "";
}

// The picked Aspora entity's governing-law label, independent of whether the
// document law conflicts with it. governingLawConflict() only exposes the entity
// law when there is a MISMATCH (it returns null on concurrence), so the
// jurisdiction-options recommendation cannot read it from there — it must read
// the entity law directly so the "— recommended" marker + visual selection track
// the picked entity even when the document already matches.
function pickedEntityLawLabel() {
  const p = state.reviewPickedAspora;
  return p && p.lawLabel ? String(p.lawLabel).trim() : "";
}

function governingLawConflict() {
  // Driven by the Fill-tool pick: review-fill.js sets state.reviewPickedAspora =
  // { name, lawLabel } from the chosen Aspora entity's registry governing law.
  // No fetch / auto-detect at render time — that mechanism is the proven one.
  const picked = state.reviewPickedAspora;
  const entityLaw = picked && picked.lawLabel ? String(picked.lawLabel).trim() : "";
  if (!entityLaw) return null;
  const docLaw = detectDocumentGoverningLaw();
  if (!docLaw) return null;
  if (docLaw.toLowerCase() === entityLaw.toLowerCase()) return null;
  return { entityName: (picked && picked.name) || "the selected entity", entityLaw, docLaw };
}

// The clause verdict shown in the UI. The backend/AI verdict is the SOURCE OF
// TRUTH: this defers to clauseStatus(clause) for every clause, including
// Governing Law. The client-only entity-vs-doc concurrence signal
// (governingLawConflict()) is surfaced separately as a NON-authoritative hint
// (the concurrence note banner + redline picker in renderStudioDetail) — it must
// never force a FAIL the backend did not call, which would be a "deterministic
// ghost" overriding the real engine.
function clauseDisplayStatus(clause) {
  return clauseStatus(clause);
}

let concurrenceRefreshFrame = null;
// Re-render only the navigator + detail panel (never the editable document, to keep
// the caret) so the concurrence verdict updates live. Coalesced to one frame.
function refreshGoverningLawConcurrence() {
  if (concurrenceRefreshFrame) return;
  concurrenceRefreshFrame = requestAnimationFrame(() => {
    concurrenceRefreshFrame = null;
    if (typeof renderStudioClauseLane === "function") renderStudioClauseLane();
    // The jurisdiction-options recommendation + visual selection live in the
    // governing-law clause detail, but the entity is changed from the "fill"
    // sub-view. Re-render the detail on any entity change so the recommendation
    // tracks the picked entity live — gated only on the governing-law clause
    // being the selected one, NOT on the active sub-view. renderStudioDetail()
    // self-dispatches by reviewInspectorView, so this paints the clause detail
    // when the clause view is active and is otherwise harmless.
    if (state.selectedReviewClauseId === "governing_law"
      && typeof renderStudioDetail === "function") {
      renderStudioDetail();
    }
  });
}

function renderStudioClauseLane() {
  if (!studioClauseLane) return;

  const sourceClauses = getDisplayClauses();

  if (!sourceClauses.length) {
    studioClauseLane.innerHTML = '<div class="studio-empty">Loading clauses</div>';
    return;
  }

  const clauseMarkup = sourceClauses
    .map((clause) => {
      const selected = clause.id === state.selectedReviewClauseId ? "selected" : "";
      const status = clauseDisplayStatus(clause);
      const displayName = clauseDisplayName(clause);
      const clauseRedlines = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id);
      const redlineCount = hasReviewResults() ? clauseRedlines.length : 0;
      const allRedlinesIgnored = redlineCount > 0 && clauseRedlines.every((edit) => !redlineExportIncluded(edit));
      const reviewed = hasReviewResults() && clauseReviewAcknowledged(clause.id);
      const comment = hasReviewResults() && Boolean(clauseReviewComment(clause.id));
      const stateLabel = reviewed
        ? "Reviewed"
        : allRedlinesIgnored
          ? "Ignored"
          : redlineCount
            ? `${redlineCount} proposed ${redlineCount === 1 ? "redline" : "redlines"}`
            : status.issueLabel;
      const selectable = hasReviewResults()
        ? `
          <button class="studio-clause-select" type="button" data-studio-lane-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}" aria-label="${escapeHtml(`${displayName}: ${stateLabel}`)}" title="${escapeHtml(`${displayName}: ${stateLabel}`)}">
            <span class="studio-clause-dot ${status.dotTone}"></span>
            <span class="studio-clause-title">${escapeHtml(displayName)}</span>
            ${clauseEngineBadge(clause)}
            ${comment ? '<span class="studio-comment-state">Comment</span>' : ""}
          </button>
        `
        : `
          <div class="studio-clause-select">
            <span class="studio-clause-dot ${status.dotTone}"></span>
            <span class="studio-clause-title">${escapeHtml(displayName)}</span>
            ${clauseEngineBadge(clause)}
          </div>
        `;
      return `
        <article class="studio-clause-item ${selected} ${status.tone} ${reviewed ? "reviewed" : ""} ${allRedlinesIgnored ? "ignored" : ""}">
          ${selectable}
        </article>
      `;
    })
    .join("");

  studioClauseLane.innerHTML = clauseMarkup;

  bindClauseSelection(studioClauseLane, "[data-studio-lane-id]", "studioLaneId");
  bindClauseNavigatorScrollControls();
}

function bindClauseNavigatorScrollControls() {
  const scrollNode = document.querySelector(".studio-clause-scroll");
  const previousButton = document.querySelector("[data-clause-scroll='prev']");
  const nextButton = document.querySelector("[data-clause-scroll='next']");
  if (!scrollNode || !previousButton || !nextButton) return;

  const updateButtons = () => {
    const maxScroll = Math.max(0, scrollNode.scrollWidth - scrollNode.clientWidth);
    previousButton.disabled = scrollNode.scrollLeft <= 1;
    nextButton.disabled = scrollNode.scrollLeft >= maxScroll - 1;
  };
  previousButton.onclick = () => {
    scrollNode.scrollBy({ left: -Math.max(160, Math.round(scrollNode.clientWidth * 0.75)), behavior: "smooth" });
  };
  nextButton.onclick = () => {
    scrollNode.scrollBy({ left: Math.max(160, Math.round(scrollNode.clientWidth * 0.75)), behavior: "smooth" });
  };
  scrollNode.onscroll = updateButtons;
  requestAnimationFrame(updateButtons);
}

function renderClauseVerdictHeader(clause, status) {
  return `
    <div class="studio-detail-heading active-clause-heading clause-verdict-header">
      <div>
        <small>Clause</small>
        <h3>${escapeHtml(clauseDisplayName(clause))}${clauseEngineBadge(clause)}</h3>
      </div>
      <div class="clause-verdict-meta">
        ${renderActiveClauseStatusToggle(clause, status)}
      </div>
    </div>
  `;
}

function renderClauseAssessmentSection(clause) {
  const assessment = clauseAssessmentText(clause);
  return `
    <div class="studio-detail-block assessment-block" data-card-section="assessment">
      <small>Assessment</small>
      <p>${linkifyParagraphRefs(assessment)}</p>
    </div>
  `;
}

function clauseAssessmentText(clause) {
  return String(
    clause?.reason
      || clause?.finding
      || clause?.decision_reason
      || clause?.issue_label
      || "Clause review available.",
  ).trim();
}

// --- AI-referenced paragraphs ------------------------------------------------
// A clause assessment names the paragraphs the AI relied on (e.g. "p15", "p34-p39").
// Those references come from the model's own prose, so surfacing them stays within
// the AI-first review (no deterministic locator). Every reference is validated
// against the document's real paragraph ids, then rendered as a clickable link in
// the assessment and highlighted on the document so a reviewer can jump straight to
// the paragraphs the AI flagged as its reason for needing review.
function validParagraphIdSet() {
  const ids = new Set();
  (Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : []).forEach((paragraph) => {
    const id = String((paragraph && paragraph.id) || "").trim();
    if (id) ids.add(id);
  });
  return ids;
}

// --- Structure-index reference resolution ------------------------------------
// Prose references ("Paragraph 11", "Clause 5", "Schedule 3", "Annex A") are
// resolved through the shared contract structure index, NOT by assuming the
// printed number equals the paragraph-block position. The index's
// alias_to_section_id maps a printed-numbering alias key (e.g. "number:11",
// "clause:5", "schedule:3", "annex:a") to a section id whose first paragraph
// (paragraph_ids[0] on the reduced record) is the document paragraph that section
// begins at. The number printed in the document (section.number / .label) is the
// document's REAL Word numbering, so a "Paragraph 11" whose block index is something
// else still lands on the right paragraph. Ambiguous keys (a number that recurs across
// restarted numbering) are intentionally absent from the binding map, so they resolve
// to nothing — which is the accuracy-or-nothing behaviour the linkifier wants (leave
// them as plain text). "Exhibit N" is an ATTACHMENT reference (like Schedule/Annex/
// Appendix): the backend never emits an "exhibit:N" alias and, treated as an attachment
// kind, never borrows a body "number:N" heading — so "Exhibit N" resolves the same way
// on FE and BE (both decline to bridge it onto a Section-N). See the namespace guard in
// resolveStructureReferenceParagraphId, which mirrors reference_resolver's attachment
// rules exactly.
//
// The bare "pN" token is a DIRECT paragraph id, never a printed number: it is still
// validated against the real paragraph ids (validParagraphIdSet), not the index.

// The structure-reference word -> the canonical alias kind the backend index uses.
// "paragraph"/"para"/"¶" carry no structural kind, so they resolve only via the
// printed-number key. Kind strings for body/attachment words MUST match the backend's
// EXPLICIT_KIND_LABELS in contract_structure.py — the index only emits a "<kind>:<number>"
// alias for those. "exhibit" is NOT a parser/alias kind, but it IS an attachment-kind for
// the namespace guard (see REFERENCE_KIND_NAMESPACE_FE / resolveStructureReferenceParagraphId):
// like Schedule/Annex/Appendix it never appends a "number:N" body fallback, so an
// "Exhibit N" reference declines to bridge onto a Section-N, the SAME outcome the backend
// reaches (its prose path maps exhibit -> an attachment kind for the identical guard).
function structureReferenceKind(word) {
  const key = String(word || "").trim().toLowerCase().replace(/\.$/, "");
  const kinds = {
    annex: "annex",
    annexes: "annex",
    annexure: "annexure",
    annexures: "annexure",
    appendices: "appendix",
    appendix: "appendix",
    article: "article",
    articles: "article",
    clause: "clause",
    clauses: "clause",
    exhibit: "exhibit",
    exhibits: "exhibit",
    paragraph: "",
    paragraphs: "",
    para: "",
    paras: "",
    "¶": "",
    schedule: "schedule",
    schedules: "schedule",
    section: "section",
    sections: "section",
  };
  return Object.prototype.hasOwnProperty.call(kinds, key) ? kinds[key] : null;
}

// Mirror of reference_resolver.REFERENCE_KIND_NAMESPACES (read-only backend source of
// truth) PLUS "exhibit" as an attachment kind. Schedules/annexes/appendices/exhibits are
// attachments numbered in their own space; clauses/articles/sections are in-body. The
// kind-agnostic "number:N" fallback must never bridge these namespaces (a "Schedule 2"
// borrowing a "Section 2", or vice versa, is the latent governing-law false-clear). A
// kind not in this map (bare paragraph/¶, or "" kind) has no namespace and is treated as
// in-body via NUMERIC_FALLBACK_NAMESPACE_FE — exactly the backend's _kind_namespace.
const REFERENCE_KIND_NAMESPACE_FE = {
  annex: "attachment",
  annexure: "attachment",
  appendix: "attachment",
  schedule: "attachment",
  exhibit: "attachment",
  article: "body",
  clause: "body",
  section: "body",
};
// A section detected without an explicit kind (bare numbered/heading) is in-body — the
// clauses/sections a "Section N" reference means. Mirrors NUMERIC_FALLBACK_NAMESPACE.
const NUMERIC_FALLBACK_NAMESPACE_FE = "body";

// reference_resolver._kind_namespace: the namespace ("body"/"attachment") of a ref kind,
// or null when the kind carries no namespace of its own.
function referenceKindNamespace(kind) {
  const key = String(kind || "").toLowerCase();
  return Object.prototype.hasOwnProperty.call(REFERENCE_KIND_NAMESPACE_FE, key)
    ? REFERENCE_KIND_NAMESPACE_FE[key]
    : null;
}

// reference_resolver._numeric_fallback_namespace_matches: guard the kind-agnostic
// "number:N" match against a cross-namespace target. A bare numbered/heading section has
// no namespace of its own and is treated as in-body; if the matched section instead
// carries an explicit attachment kind (a schedule/annex/appendix scraped with only a
// number:N alias), it must NOT satisfy a body reference — that is the Schedule-N <->
// Section-N collision. A null reference namespace (bare paragraph/¶) matches anything.
function numericFallbackNamespaceMatches(referenceNamespace, sectionRecord) {
  let targetNamespace = referenceKindNamespace(
    sectionRecord && typeof sectionRecord === "object" ? sectionRecord.kind : "",
  );
  if (targetNamespace === null) targetNamespace = NUMERIC_FALLBACK_NAMESPACE_FE;
  if (referenceNamespace === null) return true;
  return targetNamespace === referenceNamespace;
}

// The shared structure index (reference_index) for the current review, preferring
// the backend-supplied one and falling back to the FE builder when absent — exactly
// the source the Structure tab uses, so prose links and the Structure tab agree.
function structureReferenceIndex() {
  const direct = state.latestReviewResult?.contract_structure?.reference_index;
  if (direct && typeof direct === "object") return direct;
  const paragraphs = Array.isArray(state.reviewParagraphs) && state.reviewParagraphs.length
    ? state.reviewParagraphs
    : (Array.isArray(state.latestReviewResult?.paragraphs) ? state.latestReviewResult.paragraphs : []);
  if (!paragraphs.length || typeof buildStructureFromParagraphs !== "function") return null;
  const built = buildStructureFromParagraphs(paragraphs);
  return built && typeof built === "object" ? built.reference_index : null;
}

// Resolve a structure reference (kind + printed number) to the START paragraph id of
// the matching section, via the shared index. Returns "" (accuracy-or-nothing) when
// the reference does not resolve to a real section start paragraph. The bare-token
// "pN" form does NOT go through here — it is a direct paragraph id.
//
// The reduced reference_index record (backend _resolver_section_record / FE
// resolverSectionRecord) carries `paragraph_ids` and an optional `source`, but NOT
// `start_paragraph_id`. So the section start is paragraph_ids[0] — exactly what the
// backend resolver uses. Reading a non-existent start_paragraph_id off the reduced
// record resolves to "" in production and silently linkifies nothing.
//
// Source-backed gate (accuracy-or-nothing): a section the parser only inferred from a
// flat/PDF doc (an address line or table-cell digit scraped as a clause number) has no
// `source`. Linking "Clause 1" to such a phantom would jump to e.g. "1 Sheldon Square",
// so a reference is only resolved when its section is source-backed. On messy docs this
// yields NO link rather than a WRONG link. Bare pN tokens / ranges bypass this entirely.
function resolveStructureReferenceParagraphId(kind, number, index = structureReferenceIndex()) {
  if (!index || typeof index !== "object") return "";
  const aliasLookup = index.alias_to_section_id || {};
  const sectionsById = index.sections_by_id || {};
  const normalizedNumber = String(number || "").trim().toLowerCase();
  if (!normalizedNumber) return "";
  // A structural word tries its kind key first, then the bare printed-number key; a
  // plain paragraph/¶ reference (kind === "") only carries the printed-number key.
  // Resolution is STRICTLY through alias_to_section_id, which the backend has already
  // pruned of ambiguous keys — but the kind-agnostic "number:N" fallback still needs
  // the SAME namespace guard reference_resolver._resolve_reference_item applies, so the
  // FE resolves every reference exactly the way the backend does:
  //   (a) an ATTACHMENT-kind reference (schedule/annex/annexure/appendix/exhibit) does
  //       NOT append the "number:N" fallback — it must match its explicit kind alias;
  //   (b) a body/number reference rejects a "number:N" match when the matched section is
  //       attachment-namespaced (numericFallbackNamespaceMatches).
  // Together these stop "Section 2" linking to a "Schedule 2" (and the inverse).
  const referenceNamespace = referenceKindNamespace(kind);
  const aliasKeys = [];
  if (kind) aliasKeys.push(`${kind}:${normalizedNumber}`);
  if (referenceNamespace !== "attachment") aliasKeys.push(`number:${normalizedNumber}`);
  let sectionId = "";
  for (const aliasKey of aliasKeys) {
    const candidateId = aliasLookup[aliasKey];
    if (!candidateId) continue;
    if (
      aliasKey.startsWith("number:") &&
      !numericFallbackNamespaceMatches(referenceNamespace, sectionsById[candidateId])
    ) {
      continue;
    }
    sectionId = candidateId;
    break;
  }
  const record = sectionId ? sectionsById[sectionId] : null;
  if (!record) return "";
  // Source-backed only: a parser-invented (source-less) section is never a link target.
  if (!record.source || typeof record.source !== "object" || !Object.keys(record.source).length) {
    return "";
  }
  const paragraphIds = Array.isArray(record.paragraph_ids) ? record.paragraph_ids : [];
  return paragraphIds.length ? String(paragraphIds[0] || "") : "";
}

// One regex for every structure/prose reference word + its identifier (a number,
// letter, roman numeral, or dotted/parenthetical suffix such as "3(a)"). The bare
// "pN" token is handled separately because it is a direct paragraph id.
const STRUCTURE_REFERENCE_RE =
  /\b(paragraphs?|paras?\.?|clauses?|articles?|sections?|schedules?|exhibits?|annexures?|annexes|annex|appendices|appendix)\s+([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*(?:\([A-Za-z0-9]+\))?)\b|(¶)\s*(\d+)/gi;

function referencedParagraphIds(text) {
  const valid = validParagraphIdSet();
  if (!valid.size || !text) return [];
  const found = [];
  const seen = new Set();
  const add = (id) => {
    if (valid.has(id) && !seen.has(id)) {
      seen.add(id);
      found.push(id);
    }
  };
  const source = String(text);
  // Expand ranges first ("p34-p39" -> p34..p39), capped so a typo cannot blow up.
  source.replace(/\bp(\d+)\s*[-–—]\s*p?(\d+)\b/gi, (match, a, b) => {
    const start = parseInt(a, 10);
    const end = parseInt(b, 10);
    if (start <= end && end - start <= 200) {
      for (let n = start; n <= end; n += 1) add(`p${n}`);
    }
    return match;
  });
  // Then standalone token references ("p11") — direct paragraph ids, validated
  // against the real id set (NOT the printed-number structure index).
  source.replace(/\bp(\d+)\b/gi, (match, n) => {
    add(`p${n}`);
    return match;
  });
  // Then prose + structural references ("Paragraph 11", "Clause 5", "Schedule 3",
  // "Annex A", "¶11"). These carry the document's PRINTED numbering, so they resolve
  // through the shared structure index to the matching section's start paragraph id
  // (which add() then validates). A reference that does not resolve is dropped.
  const index = structureReferenceIndex();
  STRUCTURE_REFERENCE_RE.lastIndex = 0;
  let match = STRUCTURE_REFERENCE_RE.exec(source);
  while (match) {
    const word = match[1] || match[3];
    const number = match[2] || match[4];
    const kind = structureReferenceKind(word);
    if (kind !== null) {
      add(resolveStructureReferenceParagraphId(kind, number, index));
    }
    match = STRUCTURE_REFERENCE_RE.exec(source);
  }
  return found;
}

function linkifyParagraphRefs(text) {
  const escaped = escapeHtml(text);
  const valid = validParagraphIdSet();
  if (!valid.size) return escaped;
  const withRanges = escaped.replace(/\bp(\d+)\s*[-–—]\s*p?(\d+)\b/gi, (match, a, b) => {
    const ids = paragraphRangeIds(a, b).filter((id) => valid.has(id));
    if (!ids.length) return match;
    return `<button type="button" class="para-ref" data-para-ref="${ids[0]}" data-para-ref-range="${ids.join(" ")}">${match}</button>`;
  });
  // Prose + structural references ("Paragraph 11", "Clause 5", "Schedule 3",
  // "Annex A", "¶11"). These carry the document's PRINTED numbering, so each is
  // resolved through the shared structure index to its section's start paragraph id
  // (accuracy-or-nothing: a reference that does not resolve is left as plain text,
  // never linked to a guessed paragraph). The "...<\/button>" guard skips text
  // already inside a range button; running this BEFORE the bare-token pass consumes
  // the matched phrase as a unit so the token pass cannot re-fire inside it.
  const index = structureReferenceIndex();
  const withProse = withRanges.replace(
    new RegExp(`${STRUCTURE_REFERENCE_RE.source}(?![^<]*<\\/button>)`, "gi"),
    (match, word, number, pilcrow, pilcrowNumber) => {
      const kind = structureReferenceKind(word || pilcrow);
      if (kind === null) return match;
      const id = resolveStructureReferenceParagraphId(kind, number || pilcrowNumber, index);
      return id && valid.has(id)
        ? `<button type="button" class="para-ref" data-para-ref="${id}">${match}</button>`
        : match;
    },
  );
  return withProse.replace(/\bp(\d+)\b(?![^<]*<\/button>)/gi, (match, n) => {
    const id = `p${n}`;
    return valid.has(id)
      ? `<button type="button" class="para-ref" data-para-ref="${id}">${match}</button>`
      : match;
  });
}

function paragraphRangeIds(a, b) {
  const start = parseInt(a, 10);
  const end = parseInt(b, 10);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || end - start > 200) return [];
  const ids = [];
  for (let index = start; index <= end; index += 1) ids.push(`p${index}`);
  return ids;
}

// Paint the selected clause's AI-referenced paragraphs so a reviewer can go back to
// exactly the paragraphs the AI cited as its reason. Cleared + reapplied per render.
function highlightSelectedClauseRefs() {
  if (!studioDocumentRender) return;
  const clause = state.reviewClauses.find((item) => item.id === state.selectedReviewClauseId);
  if (!clause) return;
  const status = clauseStatus(clause);
  const toneClass = status.fails ? "verify" : status.needsReview ? "review" : "match";
  let appliedSpan = false;
  clauseEvidenceItems(clause).forEach((item) => {
    appliedSpan = applyClauseEvidenceHighlight(clause.id, item, toneClass) || appliedSpan;
  });
  if (appliedSpan) return;
  const text = `${clause.finding || ""} ${clause.reason || ""} ${clause.rationale || ""}`;
  referencedParagraphIds(text).forEach((id) => {
    const item = { paragraph_id: id, quote: "" };
    appliedSpan = applyClauseEvidenceHighlight(clause.id, item, toneClass) || appliedSpan;
  });
}

function renderStudioDetail() {
  updateReviewInspectorTabs();
  if (state.reviewInspectorView === "overview") {
    // Merged Overview pane: renders the Overview summary AND relocates + renders
    // the Fill (Aspora-entity) tool into its bottom section. No separate "fill".
    reviewOverviewController.render();
    return;
  }
  if (state.reviewInspectorView === "structure") {
    reviewStructureController.render();
    return;
  }
  const clause = getSelectedReviewClause();
  if (!clause) {
    studioDetailPanel.innerHTML = "";
    return;
  }
  const status = clauseDisplayStatus(clause);
  const verdictHeader = renderClauseVerdictHeader(clause, status);
  // Additive review-overlay finding(s) targeted at THIS clause (e.g. the law/forum
  // mismatch attaches to governing_law). Rendered first in the stack so a clause an
  // overlay flagged never shows its bare AI pass with no reason. Empty when no
  // overlay targets this clause.
  const overlayFinding = renderClauseOverlayFindingsBlock(clause);
  const assessment = renderClauseAssessmentSection(clause);
  const documentEvidence = renderClauseDocumentEvidenceBlock(clause);
  const playbookPosition = renderClausePlaybookPositionBlock(clause);
  const proposedChange = renderProposedChangeBlock(clause, status);
  const proposedRedlines = renderProposedRedlinesBlock(clause);
  const actions = renderClauseActionsBlock(clause, status);
  const reasoningTrail = renderReasoningTrailBlock(clause);
  // Governing-law concurrence hint + unified entity-aware picker (Issue 1).
  // NON-authoritative: this note (and the picker below) is an advisory client
  // signal only. The clause verdict (dot/pill/headline) comes from the backend
  // via clauseDisplayStatus and is NOT overridden here.
  const glConflict = clause.id === "governing_law" ? governingLawConflict() : null;
  // Reuse the existing .gl-concurrence-fail styling (owned by styles.css); the
  // copy is advisory and the .gl-concurrence-note marker class lets the wording be
  // recognised as a non-authoritative hint without restyling.
  const concurrenceBanner = glConflict
    ? `<div class="studio-detail-block gl-concurrence-fail gl-concurrence-note">
        <small>Entity concurrence note</small>
        <p>The document's governing law (<strong>${escapeHtml(glConflict.docLaw)}</strong>) does not match the selected entity <strong>${escapeHtml(glConflict.entityName)}</strong>, which is governed by <strong>${escapeHtml(glConflict.entityLaw)}</strong>. This is an advisory check against the picked entity — the clause verdict above reflects the AI review.</p>
      </div>`
    : "";
  // On a govlaw conflict, surface the one-click remediation picker: one button
  // per approved law, with the selected entity's law marked "— recommended".
  // Clicking applies a clean whole-paragraph redline via applyGoverningLawRedline
  // (delegated handler in app.js).
  //
  // GOVLAW OPTIONS DEDUP: when the backend emitted a governing-law redline_edit
  // carrying template_options, the connected proposed-edit card already renders
  // those jurisdiction options (renderRedlineTemplateOptions), so this detached
  // picker would show the SAME options a second time. Suppress only the duplicate
  // option display in that case — the concurrence detection and advisory note are
  // untouched. When there is NO backend govlaw redline_edit to host the options,
  // keep this picker so the redline-to-recommended-law capability is preserved.
  const glCardHostsOptions = Boolean(glConflict) && state.reviewRedlines.some(
    (edit) => String(edit?.clause_id || "") === "governing_law"
      && (edit.template_options || []).length > 1,
  );
  const glRedlinePicker = glConflict && !glCardHostsOptions
    ? `<div class="studio-detail-block">
        <div class="redline-options">
          <span class="redline-options-title">Redline governing law to</span>
          ${(Array.isArray(clause.approved_laws) ? clause.approved_laws : []).map((label) => {
            const phrase = (clause.law_phrases && clause.law_phrases[label]) || label;
            // Same picked-entity source as the connected jurisdiction-options card
            // so both pickers mark the same recommended law (falls back to the
            // conflict's entity law, which is identical here, if state is absent).
            const recommendedLaw = (pickedEntityLawLabel() || glConflict.entityLaw).toLowerCase();
            const recommended = String(label).trim().toLowerCase() === recommendedLaw;
            const optionText = `This Agreement shall be governed by the laws of ${phrase}.`;
            return `<button class="redline-option ${recommended ? "selected" : ""}" type="button" data-gl-redline-law="${escapeHtml(label)}" data-gl-redline-phrase="${escapeHtml(phrase)}" aria-pressed="${recommended ? "true" : "false"}">
              <span class="redline-option-dot" aria-hidden="true"></span>
              <span class="redline-option-copy">
                <strong>${escapeHtml(label)}${recommended ? " — recommended" : ""}</strong>
                <span>${escapeHtml(optionText)}</span>
              </span>
            </button>`;
          }).join("")}
        </div>
      </div>`
    : "";
  studioDetailPanel.innerHTML = `
    ${verdictHeader}
    <div class="studio-detail-stack">
      ${overlayFinding}
      ${concurrenceBanner}
      ${glRedlinePicker}
      ${assessment}
      ${documentEvidence}
      ${playbookPosition}
      ${proposedChange}
      ${proposedRedlines}
      ${actions}
      ${reasoningTrail}
    </div>
  `;
  bindExportDecisionControls(studioDetailPanel);
  bindTemplateOptionControls(studioDetailPanel);
  bindReviewAcknowledgementControls(studioDetailPanel);
  bindReviewCommentControls(studioDetailPanel);
  bindParagraphReferenceControls(studioDetailPanel);
  bindReasoningTrailControls(studioDetailPanel);
  // gl-redline picker clicks are handled by the delegated [data-gl-redline-law]
  // listener in app.js (the proven wiring) — no per-render binding here, which
  // would double-apply applyGoverningLawRedline on a single click.
}

function renderAiCitation(span) {
  if (typeof span === "string") {
    return `
      <figure class="ai-citation-item">
        <blockquote>${escapeHtml(span)}</blockquote>
      </figure>
    `;
  }
  const paragraphId = span && typeof span === "object" ? String(span.paragraph_id || "").trim() : "";
  const quote = span && typeof span === "object" ? String(span.quote || "").trim() : "";
  const relevance = span && typeof span === "object" ? String(span.relevance || "").trim() : "";
  const paragraphLabel = paragraphId ? paragraphDisplayLabel(paragraphId) : "";
  return `
    <figure class="ai-citation-item">
      ${paragraphLabel || relevance ? `<figcaption>${escapeHtml([paragraphLabel, relevance].filter(Boolean).join(" · "))}</figcaption>` : ""}
      <blockquote>${escapeHtml(quote || "Citation recorded without quote text.")}</blockquote>
    </figure>
  `;
}

function renderClauseDocumentEvidenceBlock(clause) {
  const items = clauseEvidenceItems(clause);
  const grounding = typeof clause?.grounding === "object" && clause.grounding ? clause.grounding : null;
  const groundingStatus = String(grounding?.status || "").trim().toLowerCase();
  const absent = isClauseAbsentFromDocument(clause, items, groundingStatus);
  if (absent) {
    return `
      <div class="studio-detail-block studio-detail-evidence in-document-block" data-card-section="document">
        <small>In the document</small>
        <p>Not present in the document.</p>
      </div>
    `;
  }
  if (!items.length) {
    const ungrounded = groundingStatus === "ungrounded";
    return `
      <div class="studio-detail-block studio-detail-evidence in-document-block ${ungrounded ? "ungrounded" : "muted"}" data-card-section="document">
        <small>In the document</small>
        <p>${escapeHtml(ungrounded
          ? "No grounded quote was recorded for this finding. Confirm against the document before sending."
          : "No matching paragraph identified.")}</p>
      </div>
    `;
  }
  return `
    <div class="studio-detail-block studio-detail-evidence in-document-block" data-card-section="document">
      <small>In the document</small>
      <div class="document-evidence-list">
        ${items.map((item) => renderDocumentEvidenceItem(item)).join("")}
      </div>
    </div>
  `;
}

function renderDocumentEvidenceItem(item) {
  const paragraphId = String(item.paragraph_id || "").trim();
  const label = paragraphId ? paragraphDisplayLabel(paragraphId) : "Cited evidence";
  const quote = String(item.quote || item.text || "").trim();
  return `
    <figure class="document-evidence-item">
      <figcaption>
        <span>${escapeHtml(label)}</span>
        ${paragraphId ? `<button type="button" class="para-ref evidence-jump" data-para-ref="${escapeHtml(paragraphId)}">Jump</button>` : ""}
      </figcaption>
      <blockquote>${escapeHtml(quote || "Citation recorded without quote text.")}</blockquote>
    </figure>
  `;
}

function isClauseAbsentFromDocument(clause, items, groundingStatus) {
  if (groundingStatus === "absence") return true;
  if (items.length) return false;
  const issueType = String(clause?.issue_type || "").trim().toLowerCase();
  const type = String(clause?.type || "").trim().toLowerCase();
  const status = clauseStatus(clause);
  return issueType === "missing" || (type === "prohibited" && status.passes);
}

function clauseEvidenceItems(clause) {
  const items = [];
  const seen = new Set();
  const add = (item) => {
    const paragraphId = String(item?.paragraph_id || "").trim();
    const quote = String(item?.quote || item?.matched_text || item?.text || "").trim();
    const key = `${paragraphId}:${quote}`;
    if ((!paragraphId && !quote) || seen.has(key)) return;
    seen.add(key);
    items.push({
      paragraph_id: paragraphId,
      quote,
      spans: Array.isArray(item?.spans || item?.match_spans) ? (item.spans || item.match_spans) : [],
    });
  };
  const structured = Array.isArray(clause?.structured_evidence) ? clause.structured_evidence : [];
  structured.forEach((record) => add({
    paragraph_id: record?.paragraph_id,
    quote: record?.matched_text || record?.text,
    spans: record?.match_spans,
  }));
  const citation = typeof clause?.citation === "object" && clause.citation ? clause.citation : null;
  if (!items.length && citation) add({
    paragraph_id: citation.paragraph_id,
    quote: citation.quote,
    spans: citation.start != null && citation.end != null
      ? [{ start: citation.start, end: citation.end, text: citation.quote, term: citation.quote }]
      : [],
  });
  const analysis = clause && typeof clause.ai_review_analysis === "object" ? clause.ai_review_analysis : null;
  if (!items.length) {
    (Array.isArray(analysis?.cited_spans) ? analysis.cited_spans : []).forEach((span) => {
      if (typeof span === "string") {
        add({ quote: span });
      } else {
        add({ paragraph_id: span?.paragraph_id, quote: span?.quote || span?.text });
      }
    });
  }
  if (!items.length) {
    (Array.isArray(clause?.evidence_paragraphs) ? clause.evidence_paragraphs : [])
      .filter((paragraph) => paragraph && paragraph.text)
      .forEach((paragraph) => add({
        paragraph_id: paragraph.id,
        quote: paragraph.text,
      }));
  }
  return items.slice(0, 5);
}

function bindParagraphReferenceControls(container) {
  container.querySelectorAll("[data-para-ref]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const range = String(button.dataset.paraRefRange || "").split(/\s+/).filter(Boolean);
      jumpToParagraph(range[0] || button.dataset.paraRef);
    });
  });
}

function paragraphDisplayLabel(paragraphId) {
  const normalizedId = String(paragraphId || "");
  if (normalizedId.startsWith("draft-proposed-")) return "Proposed draft";
  if (normalizedId.startsWith("draft-original-")) return "Original text";
  if (normalizedId.startsWith("draft-anchor-")) return "Anchor text";
  if (normalizedId.startsWith("draft-action-")) return "Draft action";
  const paragraph = state.reviewParagraphs.find((item) => String(item.id || "") === String(paragraphId || ""));
  const index = paragraph?.index || paragraph?.source_index;
  return index ? `Paragraph ${index}` : paragraphId;
}

// Resolve a dynamic clause's fallback/standard-position block from the result,
// independent of exactly where the backend hangs it. A dynamic clause type is
// self-describing in the Playbook (fallback: { wording, approved_positions,
// redline_action }); the review result passes that through so the Review tab
// can show the playbook position for a clause the code has never seen. Tolerant
// of the block living at clause.fallback, clause.playbook.fallback, or a
// flattened clause.fallback_wording so rendering does not depend on the final
// #10 contract shape. Returns null when there is nothing to show.
function clauseFallback(clause) {
  if (!clause || typeof clause !== "object") return null;
  const playbook = clause.playbook && typeof clause.playbook === "object" ? clause.playbook : null;
  const raw = (clause.fallback && typeof clause.fallback === "object" ? clause.fallback : null)
    || (playbook && typeof playbook.fallback === "object" ? playbook.fallback : null);
  const wording = String((raw && raw.wording) || clause.fallback_wording || "").trim();
  const approvedSource = (raw && Array.isArray(raw.approved_positions) ? raw.approved_positions : null)
    || (Array.isArray(clause.approved_positions) ? clause.approved_positions : null)
    || (Array.isArray(clause.approved_options) ? clause.approved_options : null)
    || (Array.isArray(clause.approved_laws) ? clause.approved_laws : []);
  const approvedPositions = approvedSource
    .map((position) => {
      if (position && typeof position === "object") {
        return String(position.label || position.name || position.id || position.value || "").trim();
      }
      return String(position || "").trim();
    })
    .filter(Boolean);
  // 2.1: the Playbook's preferred position. Native clauses express this through
  // preferred_position / requirement / expected_value rather than a dynamic
  // fallback block, so surface those too. Tolerant of where the backend hangs it
  // (clause.playbook.preferred_position or flat) so the block does not depend on
  // the final contract shape.
  const preferred = String(
    (playbook && (playbook.preferred_position || playbook.position))
      || clause.preferred_position
      || clause.expected_position
      || clause.requirement
      || "",
  ).trim();
  if (!wording && !approvedPositions.length && !preferred) return null;
  return { approvedPositions, preferred, wording };
}

function renderClausePlaybookPositionBlock(clause) {
  const fallback = clauseFallback(clause);
  const requiredPosition = String(fallback?.preferred || fallback?.wording || clause?.requirement || "").trim();
  const approvedPositions = Array.isArray(fallback?.approvedPositions) ? fallback.approvedPositions : [];
  const rulePurpose = String(clause?.rationale || clause?.evidence_guidance || clause?.instructions || "").trim();
  const hasContent = requiredPosition || approvedPositions.length || rulePurpose;
  const approved = approvedPositions.length
    ? `
      <div class="playbook-position-field">
        <span class="detail-field-label">Approved alternatives</span>
        <ul>${approvedPositions.map((position) => `<li>${escapeHtml(position)}</li>`).join("")}</ul>
      </div>
    `
    : "";
  return `
    <div class="studio-detail-block playbook-position-block" data-card-section="playbook">
      <small>Playbook position</small>
      ${hasContent ? `
        ${requiredPosition ? `
          <div class="playbook-position-field">
            <span class="detail-field-label">Required position</span>
            <p>${escapeHtml(requiredPosition)}</p>
          </div>
        ` : ""}
        ${approved}
        ${rulePurpose ? `
          <div class="playbook-position-field">
            <span class="detail-field-label">Rule purpose</span>
            <p>${escapeHtml(rulePurpose)}</p>
          </div>
        ` : ""}
      ` : "<p>No playbook position recorded.</p>"}
    </div>
  `;
}

function clauseApprovedAlternatives(clause, change = null) {
  const fromChange = Array.isArray(change?.approved_alternatives) ? change.approved_alternatives : [];
  const fallback = clauseFallback(clause);
  const acceptableLanguage = String(clause?.acceptable_language || "").trim();
  return uniqueStrings([
    ...fromChange,
    ...(Array.isArray(fallback?.approvedPositions) ? fallback.approvedPositions : []),
    ...(acceptableLanguage ? [acceptableLanguage] : []),
  ]);
}

function renderClausePlaybookPositionBlockLegacy(clause) {
  const fallback = clauseFallback(clause);
  if (!fallback) return "";
  const preferred = fallback.preferred
    ? `
      <div class="playbook-position-preferred">
        <small>Preferred position</small>
        <p>${escapeHtml(fallback.preferred)}</p>
      </div>
    `
    : "";
  const approved = fallback.approvedPositions.length
    ? `
      <div class="playbook-position-approved">
        <small>Approved positions</small>
        <ul>${fallback.approvedPositions.map((position) => `<li>${escapeHtml(position)}</li>`).join("")}</ul>
      </div>
    `
    : "";
  const wording = fallback.wording
    ? `<p class="playbook-position-wording">${escapeHtml(fallback.wording)}</p>`
    : "";
  return `
    <div class="studio-detail-block playbook-position-block">
      <small>Playbook position</small>
      ${preferred}
      ${wording}
      ${approved}
    </div>
  `;
}

// Structured-evidence + audit-trace scaffolding for the evidence-grounded
// findings work (task #16): they render structured evidence signals and the
// audit trace off the clause result, now surfaced inside the collapsible
// Reasoning trail. The former reason-code block was removed — reason_codes is an
// internal engine token (e.g. ai_first_fail) the backend still emits for
// telemetry, but it is meaningless to a reviewer so the panel never renders it.
function renderEvidenceSignalsBlock(clause) {
  const records = Array.isArray(clause?.structured_evidence)
    ? clause.structured_evidence.filter((record) => record && record.paragraph_id)
    : [];
  const quotes = records
    .slice(0, 5)
    .map((record) => ({
      ref: String(record.paragraph_index || record.source_index || record.paragraph_id || "").trim(),
      text: String(record.matched_text || record.text || "").trim(),
    }))
    .filter((quote) => quote.text);
  if (!quotes.length) return "";
  return `
      <div class="assessment-evidence-quotes">
        ${quotes.map((quote) => `
          <p class="assessment-evidence-quote">${quote.ref ? `<span class="assessment-evidence-ref">¶${escapeHtml(quote.ref)}</span> ` : ""}${escapeHtml(quote.text)}</p>
        `).join("")}
      </div>
  `;
}

// Steps shown in the Reasoning trail: DEEPER reasoning only. The "Decision"
// step is excluded because the decision + its reasoning are folded into the
// first-class Assessment headline, and the "AI assessment normalization" step is
// excluded as pure contract plumbing that means nothing to a reviewer. When the
// model returns its own structured reasoning (locate/read/apply/cite/decide) the
// backend emits those as the steps instead, and they flow straight through.
const AUDIT_TRACE_PLUMBING_STEP_NAMES = new Set(["decision", "ai assessment normalization"]);

function auditTraceTrailSteps(clause) {
  const trace = clause?.audit_trace && typeof clause.audit_trace === "object" ? clause.audit_trace : null;
  const steps = Array.isArray(trace?.steps) ? trace.steps.filter((step) => step && step.name) : [];
  return steps.filter(
    (step) => !AUDIT_TRACE_PLUMBING_STEP_NAMES.has(String(step.name).trim().toLowerCase()),
  );
}

function renderAuditTraceBlock(clause) {
  const steps = auditTraceTrailSteps(clause);
  if (!steps.length) return "";
  return `
    <div class="audit-trace-block">
      <span class="detail-field-label">Ordered reasoning</span>
      <ol class="audit-trace-list">
        ${steps.map((step) => `
          <li>
            <strong>${escapeHtml(step.name)}</strong>
            <span>${escapeHtml(step.outcome || "")}</span>
            ${step.details ? `<p>${escapeHtml(step.details)}</p>` : ""}
          </li>
        `).join("")}
      </ol>
    </div>
  `;
}

// 2.3 (#22): the collapsible Reasoning trail. Holds the DEEPER reasoning detail
// only — structured evidence signals + the remaining audit-trace steps. It does
// NOT render reason codes (an internal engine token, meaningless to a reviewer),
// the Decision step (folded into the Assessment headline), or the normalization
// step (contract plumbing). Returns "" when nothing is left to show, so a clause
// with no deeper detail shows no trail. Collapsed by default; the open/closed
// choice is remembered per clause across re-renders via state.reasoningTrailOpen.
function renderReasoningTrailBlock(clause) {
  const auditTrace = renderAuditTraceBlock(clause);
  const grounding = renderGroundingAuditBlock(clause);
  const open = reasoningTrailOpenForClause(clause?.id) ? " open" : "";
  return `
    <details class="studio-detail-block reasoning-trail-block" data-card-section="reasoning" data-reasoning-trail-clause-id="${escapeHtml(clause?.id || "")}"${open}>
      <summary class="reasoning-trail-summary">
        <span>Reasoning trail</span>
      </summary>
      <div class="reasoning-trail-body">
        ${grounding}
        ${auditTrace || '<p class="action-muted">No ordered audit steps were recorded.</p>'}
      </div>
    </details>
  `;
}

// Plain-English labels for the backend grounding.status enum (evidence_grounding.py
// emits grounded | ungrounded | not_recorded). Surface the reviewer phrase, never the
// raw token.
function groundingStatusLabel(status) {
  const labels = {
    grounded: "Backed by evidence in the document",
    ungrounded: "No matching evidence found",
    not_recorded: "Evidence check not recorded",
  };
  const key = String(status || "").trim().toLowerCase().replace(/\s+/g, "_");
  return labels[key]
    || (typeof window !== "undefined" && typeof window.humanizeId === "function"
      ? window.humanizeId(status)
      : String(status || "").replace(/_/g, " "));
}

function renderGroundingAuditBlock(clause) {
  const grounding = clause?.grounding && typeof clause.grounding === "object" ? clause.grounding : {};
  const evidenceCount = Array.isArray(clause?.structured_evidence) ? clause.structured_evidence.length : 0;
  const status = String(grounding.status || "").trim() || (evidenceCount ? "grounded" : "not_recorded");
  const paragraphIds = Array.isArray(clause?.matched_paragraph_ids) ? clause.matched_paragraph_ids : [];
  // Run each opaque paragraph id (e.g. "p15") through the shared display labeller so
  // the reviewer reads "Paragraph 15", not the internal token.
  const paragraphLabels = paragraphIds.map((id) => escapeHtml(paragraphDisplayLabel(id)));
  return `
    <div class="grounding-audit-block">
      <span class="detail-field-label">Evidence check</span>
      <p>Status: ${escapeHtml(groundingStatusLabel(status))}. Evidence records: ${escapeHtml(evidenceCount)}.${paragraphLabels.length ? ` Paragraphs: ${paragraphLabels.join(", ")}.` : ""}</p>
    </div>
  `;
}

function reasoningTrailOpenForClause(clauseId) {
  if (!clauseId) return false;
  const open = state.reasoningTrailOpen;
  return Boolean(open && typeof open === "object" && open[clauseId] === true);
}

function bindReasoningTrailControls(container) {
  container.querySelectorAll("[data-reasoning-trail-clause-id]").forEach((details) => {
    details.addEventListener("toggle", () => {
      const clauseId = details.dataset.reasoningTrailClauseId;
      if (!clauseId) return;
      if (!state.reasoningTrailOpen || typeof state.reasoningTrailOpen !== "object") {
        state.reasoningTrailOpen = {};
      }
      state.reasoningTrailOpen[clauseId] = details.open;
    });
  });
}

function renderEvidenceBlock(clause) {
  const evidenceParagraphs = Array.isArray(clause.evidence_paragraphs)
    ? clause.evidence_paragraphs.filter((paragraph) => paragraph && paragraph.text)
    : [];
  if (evidenceParagraphs.length) {
    return `
      <div class="studio-detail-block studio-detail-evidence">
        <small>Evidence</small>
        <div class="evidence-list">
          ${evidenceParagraphs.map((paragraph, index) => {
            const paragraphNumber = paragraph.index || paragraph.source_index || index + 1;
            return `
              <figure class="evidence-item">
                <figcaption>Paragraph ${escapeHtml(paragraphNumber)}</figcaption>
                <p>${escapeHtml(paragraph.text)}</p>
              </figure>
            `;
          }).join("")}
        </div>
      </div>
    `;
  }
  if (clause.matched_text) {
    return `<div class="studio-detail-block studio-detail-evidence"><small>Evidence</small><p>${escapeHtml(clause.matched_text)}</p></div>`;
  }
  return '<div class="studio-detail-block studio-detail-evidence muted"><small>Evidence</small><p>No matching paragraph identified.</p></div>';
}

function renderProposedChangeBlock(clause, status = clauseDisplayStatus(clause)) {
  const change = proposedChangeForClause(clause);
  if (status.passes) {
    return `
      <div class="studio-detail-block recommended-change-block match" data-card-section="recommended-change">
        <small>Recommended change</small>
        <p>No change needed.</p>
      </div>
    `;
  }
  if (status.needsReview) {
    return renderNeedsReviewRecommendedChange(clause, change);
  }
  if (!change) {
    return `
      <div class="studio-detail-block recommended-change-block fail" data-card-section="recommended-change">
        <small>Recommended change</small>
        <p>Review this finding and prepare an explicit redline before export or send.</p>
      </div>
    `;
  }
  const action = String(change.action || "").trim();
  const safety = change.safety && typeof change.safety === "object" ? change.safety : {};
  const sourceText = String(change.source_text || "").trim();
  const proposedText = String(change.proposed_text || "").trim();
  const why = whyThisEdit(change, clause);
  const safetyReason = String(safety.reason || "").trim();
  const actionClass = action.replace(/[^a-z0-9_-]/gi, "-") || "unknown";
  // The connected proposed-edit card (renderProposedRedlinesBlock) now owns the
  // redline preview. When this clause has a real redline edit hosting that card,
  // do NOT re-render the inline diff here — that would show the same redline text
  // twice. Keep only the "why this edit" framing; the card carries the redline.
  const hasHostingRedline = state.reviewRedlines.some((edit) => edit.clause_id === clause.id);
  const changeText = hasHostingRedline ? "" : renderProposedChangeText(sourceText, proposedText, action, change);
  return `
    <div class="studio-detail-block recommended-change-block proposed-change-card ${actionClass} fail" data-card-section="recommended-change">
      <small>Recommended change</small>
      ${changeText}
      ${why ? `<p class="proposed-change-guidance"><strong>Why this edit</strong>${escapeHtml(why)}</p>` : ""}
      ${safetyReason ? `<p class="proposed-change-safety-note">${escapeHtml(safetyReason)}</p>` : ""}
    </div>
  `;
}

function renderNeedsReviewRecommendedChange(clause, change = null) {
  // Gate the fabricated suggested-edit / recommended-option / approved-alternatives
  // scaffold on the SAME truth source the Actions block trusts: a clause only has a
  // genuine redline edit (insert for not_present+missing, replace for
  // check+present_but_wrong) when state.reviewRedlines carries an edit for it. A
  // plain decision==="review" clause has NO such edit — so the suggested-edit,
  // recommended-option, and approved-alternatives sub-blocks (derived from the
  // playbook's carve-out tokens, not real replacement wording) are fabricated and
  // contradict the "No redline action is available for this clause." Actions block.
  // Suppress the whole fabricated recommended-change block in that case and render
  // the clause cleanly — the assessment, verdict pill, and mark-reviewed affordance
  // live in their own blocks, so the reviewer can still resolve and mark it reviewed.
  const hasRealRedline = state.reviewRedlines.some((edit) => edit.clause_id === clause.id);
  if (!hasRealRedline) {
    return `
      <div class="studio-detail-block recommended-change-block review" data-card-section="recommended-change">
        <small>Recommended change</small>
        <p class="proposed-change-empty">No automatic redline is available for this clause. Resolve it using the verdict pill above, then mark it reviewed.</p>
      </div>
    `;
  }

  const question = reviewResolutionQuestion(clause, change);
  const suggested = reviewSuggestedRedline(clause, change);
  const recommended = recommendedOptionForReview(clause, change);
  const alternatives = clauseApprovedAlternatives(clause, change);

  // The interactive jurisdiction/template picker now lives INSIDE the connected
  // proposed-edit card (renderProposedRedlinesBlock -> renderRedlineTemplateOptions).
  // So when this clause has a redline edit carrying multiple template_options, the
  // card hosts the options and this card must NOT render them a second time. The
  // static approved-alternatives list is still shown when there is no such edit to
  // host the options.
  const clauseRedlines = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id);
  const editWithOptions = clauseRedlines.find((edit) => (edit.template_options || []).length > 1);
  const alternativesBlock = editWithOptions
    ? ""
    : (alternatives.length ? `
        <div class="approved-alternatives">
          <span class="detail-field-label">Approved alternatives</span>
          <ul>${alternatives.map((alternative) => `<li>${escapeHtml(alternative)}</li>`).join("")}</ul>
        </div>
      ` : "");

  return `
    <div class="studio-detail-block recommended-change-block proposed-change-card review" data-card-section="recommended-change">
      <small>Recommended change</small>
      <p class="proposed-change-summary">${escapeHtml(question)}</p>
      ${suggested ? `
        <div class="review-suggested-edit">
          <span class="detail-field-label">Suggested edit (confirm required)</span>
          <blockquote>${escapeHtml(suggested)}</blockquote>
        </div>
      ` : `
        <p class="proposed-change-empty">No safe wording was selected automatically. Choose the final wording before export or send.</p>
      `}
      ${recommended ? `
        <p class="recommended-option"><span>Recommended option</span>${escapeHtml(recommended.option)}${recommended.reason ? `: ${escapeHtml(recommended.reason)}` : ""}</p>
      ` : ""}
      ${alternativesBlock}
    </div>
  `;
}

function reviewResolutionQuestion(clause, change = null) {
  return String(change?.resolution_question || clause?.resolution_question || "").trim()
    || "What wording or approved playbook position should resolve this clause?";
}

function reviewSuggestedRedline(clause, change = null) {
  const value = String(
    change?.suggested_redline
      || clause?.suggested_redline
      || change?.proposed_text
      || "",
  ).trim();
  if (value) return value;
  const fix = String(clause?.what_to_fix || "").trim();
  if (fix && !/^confirm the clause position/i.test(fix)) return fix;
  // Terminal fallback: the playbook's acceptable language is a safe suggestion to
  // confirm when the AI/builder produced no specific redline.
  return String(clause?.acceptable_language || "").trim();
}

function recommendedOptionForReview(clause, change = null) {
  const option = change?.recommended_option && typeof change.recommended_option === "object"
    ? change.recommended_option
    : clause?.recommended_option && typeof clause.recommended_option === "object"
      ? clause.recommended_option
      : null;
  if (!option) return null;
  const label = String(option.option || "").trim();
  const reason = String(option.reason || "").trim();
  return label ? { option: label, reason } : null;
}

function whyThisEdit(change, clause) {
  const rationale = String(change?.playbook_rationale || "").trim();
  if (rationale) return rationale;
  const safetyReason = String(change?.safety?.reason || "").trim();
  if (safetyReason) return safetyReason;
  return String(clause?.redline_rationale?.explanation || "").trim();
}

function proposedChangeOutcome(change, clause, status, action, requiresApproval) {
  const rawDecision = String(change.decision || clause?.decision || "").trim().toLowerCase();
  const isReview = rawDecision === "review" || status?.needsReview || action === "needs_human_choice" || action === "comment_only";
  const isFail = rawDecision === "fail" || status?.fails;
  if (isReview && !isFail) {
    return {
      description: requiresApproval
        ? "Human judgment is required before any wording changes are exported or sent."
        : "Review the finding before deciding whether to change the document.",
      label: "Review outcome",
      title: action === "comment_only" ? "Reviewer comment only" : "Human judgment needed",
      tone: "review",
    };
  }
  return {
    description: requiresApproval
      ? "A concrete change is available, but it still waits for reviewer approval."
      : "A concrete change is ready for reviewer verification.",
    label: "Fail outcome",
    title: proposedChangeActionHeadline(action),
    tone: "fail",
  };
}

function proposedChangeActionHeadline(action) {
  switch (action) {
    case "replace":
      return "Redline replacement available";
    case "insert":
      return "Insertion available";
    case "delete":
      return "Deletion available";
    case "comment_only":
      return "Reviewer comment only";
    case "needs_human_choice":
      return "Human wording choice needed";
    default:
      return "Proposed change available";
  }
}

function proposedChangeForClause(clause) {
  if (!clause) return null;
  const clauseId = String(clause.id || "");
  // When the clause's redline carries multiple template_options, the live
  // selection (state.redlineTemplateSelections) is authoritative — derive the
  // change from it so picking an option changes the card. Otherwise the stale
  // baked-in clause.proposed_change / server proposed_changes would win.
  const optionRedline = state.reviewRedlines.find(
    (edit) => String(edit?.clause_id || "") === clauseId && (edit.template_options || []).length > 1,
  );
  if (optionRedline) return proposedChangeFromRedline(clause, optionRedline);
  if (clause.proposed_change && typeof clause.proposed_change === "object") return clause.proposed_change;
  const changes = Array.isArray(state.latestReviewResult?.proposed_changes)
    ? state.latestReviewResult.proposed_changes
    : [];
  const serverChange = changes.find((change) => String(change?.clause_id || "") === clauseId);
  if (serverChange) return serverChange;
  const redline = state.reviewRedlines.find((edit) => String(edit?.clause_id || "") === clauseId);
  return redline ? proposedChangeFromRedline(clause, redline) : null;
}

function proposedChangeFromRedline(clause, redline) {
  const selectedEdit = applyTemplateSelectionToRedline(redline);
  const action = selectedEdit.action === REDLINE_INSERT_AFTER_PARAGRAPH
    ? "insert"
    : selectedEdit.action === REDLINE_DELETE_PARAGRAPH
      ? "delete"
      : "replace";
  const rationale = selectedEdit.redline_rationale && typeof selectedEdit.redline_rationale === "object"
    ? String(selectedEdit.redline_rationale.explanation || "").trim()
    : String(clause?.redline_rationale?.explanation || "").trim();
  return {
    action,
    clause_id: String(clause?.id || ""),
    clause_name: String(clause?.name || clause?.id || ""),
    decision: String(clause?.decision || ""),
    evidence: selectedEdit.redline_rationale?.basis || {},
    // Carry the backend's punctuation-aware inline diff for the selected option
    // so the card renders the same clean redline the document view does.
    inline_diff_operations: Array.isArray(selectedEdit.inline_diff_operations)
      ? selectedEdit.inline_diff_operations
      : null,
    issue_summary: String(clause?.reason || clause?.finding || clause?.issue_label || "").trim(),
    paragraph_id: selectedEdit.paragraph_id,
    playbook_rationale: rationale,
    proposed_text: selectedEdit.action === REDLINE_DELETE_PARAGRAPH
      ? ""
      : String(selectedEdit.insert_text || selectedEdit.replacement_text || ""),
    redline_edit_id: String(selectedEdit.id || ""),
    redline_action: String(selectedEdit.action || ""),
    safety: {
      reason: "Reviewer must approve before export.",
      requires_human_approval: true,
      status: "proposed_redline_available",
    },
    source_text: String(selectedEdit.original_text || selectedEdit.anchor_text || ""),
  };
}

function renderProposedChangeText(sourceText, proposedText, action, change = null) {
  // INSERT / missing clause: only the proposed insertion -- nothing is being replaced, so do
  // not show a (mismatched) source block.
  if (action === "insert") {
    if (!proposedText) return "";
    return `
      <figure class="proposed-change-insertion">
        <figcaption>Proposed insertion</figcaption>
        <blockquote><span class="redline-insertion">${escapeHtml(proposedText)}</span></blockquote>
      </figure>
    `;
  }
  // DELETE: the source text struck through.
  if (action === "delete") {
    if (!sourceText) return "";
    return `
      <figure class="proposed-change-deletion">
        <figcaption>Proposed deletion</figcaption>
        <blockquote><span class="inline-del">${escapeHtml(sourceText)}</span></blockquote>
      </figure>
    `;
  }
  // REPLACE: a real inline redline (struck source + inserted proposed) when both exist.
  if (sourceText && proposedText) {
    const redline = renderCardReplacementRedline(sourceText, proposedText, change);
    if (redline) {
      return `<figure class="proposed-change-redline"><figcaption>Redline</figcaption><blockquote>${redline}</blockquote></figure>`;
    }
  }
  // Fallbacks: nothing usable, or the inline-diff renderer is unavailable.
  if (!sourceText && !proposedText) {
    if (action === "needs_human_choice") {
      return '<p class="proposed-change-empty">No safe replacement wording was chosen. Pick the final wording manually. No automatic edit will be applied.</p>';
    }
    if (action === "comment_only") {
      return '<p class="proposed-change-empty">No safe redline text was generated. Treat this as a reviewer comment. No automatic edit will be applied.</p>';
    }
    return "";
  }
  return `
    <div class="proposed-change-text-grid">
      ${sourceText ? `
        <figure>
          <figcaption>Source text</figcaption>
          <blockquote>${escapeHtml(sourceText)}</blockquote>
        </figure>
      ` : ""}
      ${proposedText ? `
        <figure>
          <figcaption>Proposed text</figcaption>
          <blockquote>${escapeHtml(proposedText)}</blockquote>
        </figure>
      ` : ""}
    </div>
  `;
}

// Render a struck-old / inserted-new inline redline, reusing the existing inline-diff
// machinery (redline-rendering.js). Prefers the backend's pre-computed, punctuation-aware
// edit.inline_diff_operations (the same ops the document view renders) so e.g. "the laws of"
// is not over-struck by the whitespace-only tokenizer; falls back to wordDiffOperations only
// when no backend diff is present. Returns "" if the renderer is not reachable, so the caller
// falls back to the two-block source/proposed display.
function renderCardReplacementRedline(sourceText, proposedText, change = null) {
  if (typeof renderDiffOperations !== "function") return "";
  try {
    const backendOps = change && Array.isArray(change.inline_diff_operations)
      ? change.inline_diff_operations
      : null;
    if (backendOps && backendOps.length) {
      return renderDiffOperations(backendOps);
    }
    if (typeof wordDiffOperations === "function") {
      return renderDiffOperations(wordDiffOperations(sourceText, proposedText));
    }
    if (typeof fullReplacementOperations === "function") {
      return renderDiffOperations(fullReplacementOperations(sourceText, proposedText));
    }
  } catch (_e) {
    return "";
  }
  return "";
}

function renderProposedChangeEvidence(evidence) {
  const quote = String(evidence.quote || "").trim();
  if (!quote) return "";
  const paragraphId = String(evidence.paragraph_id || "").trim();
  const label = paragraphId ? paragraphDisplayLabel(paragraphId) : "";
  return `
    <figure class="proposed-change-evidence">
      <figcaption>${escapeHtml(label ? `Evidence · ${label}` : "Evidence")}</figcaption>
      <blockquote>${escapeHtml(quote)}</blockquote>
    </figure>
  `;
}

function proposedChangeActionLabel(action) {
  switch (action) {
    case "replace":
      return "Replace text";
    case "insert":
      return "Insert text";
    case "delete":
      return "Delete text";
    case "comment_only":
      return "Comment only";
    case "needs_human_choice":
      return "Needs human choice";
    default:
      // Unknown/new action code: a safe generic phrase, never the raw token.
      return "Proposed change";
  }
}

function proposedChangeGuidance(action, requiresApproval) {
  const approval = requiresApproval ? " Reviewer approval is required before export or send." : "";
  switch (action) {
    case "replace":
      return `Compare source and proposed wording, then approve or edit the replacement.${approval}`;
    case "insert":
      return `Confirm where the inserted wording belongs before approving the redline.${approval}`;
    case "delete":
      return `Confirm the deleted wording can be removed before approving the redline.${approval}`;
    case "comment_only":
      return "Use this as reviewer guidance. No redline will be applied automatically.";
    case "needs_human_choice":
      return "Choose final wording manually. No automatic edit will be applied.";
    default:
      return `Review the suggested outcome before changing the document.${approval}`;
  }
}

function proposedChangeSafetyLabel(status) {
  switch (status) {
    case "proposed_redline_available":
      return "Proposed redline available";
    case "comment_only":
      return "Comment only";
    case "needs_human_choice":
      return "Needs human choice";
    default:
      // Unknown/new safety code: a safe generic phrase, never the raw token.
      return "Reviewer approval needed";
  }
}

function proposedChangeConfidence(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  if (number <= 1) return `${Math.round(number * 100)}%`;
  return `${Math.round(number)}%`;
}

// The Actions block no longer renders the redline itself — the connected
// proposed-edit card (renderDetailRedlineEdit, hosted by renderProposedRedlinesBlock)
// is the SINGLE proposed-edit display, including its Include/Ignore controls. This
// block keeps only the human-workflow affordances: the needs-review hint and the
// reviewer comment textarea, so the redline text is never shown twice.
function renderClauseActionsBlock(clause, status = clauseDisplayStatus(clause)) {
  const redlines = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id);
  const comment = clauseReviewComment(clause.id);
  // The verdict and the auto-redline are SEPARATE concerns: a clause can FAIL (and
  // still block send) even when the auto-fixer produced no replacement wording. When
  // the backend flags that (manual_redline_needed), tell the reviewer to redline it
  // by hand instead of leaving the bare "no redline available" line that reads like
  // nothing is wrong.
  const manualRedlineNeeded = Boolean(clause?.manual_redline_needed) && !status.passes;
  return `
    <div class="studio-detail-block clause-actions-block" data-card-section="actions">
      <small>Actions</small>
      ${redlines.length ? `
        <p class="action-muted">Use the Include/Ignore controls on the proposed edit above to choose what is exported.</p>
      ` : manualRedlineNeeded ? `
        <p class="action-warning" data-manual-redline-needed>Auto-fix unavailable — no standard replacement wording was found for this clause. Redline it manually before approving or sending.</p>
      ` : `
        <p class="action-muted">${escapeHtml(status.passes ? "No redline action required." : "No redline action is available for this clause.")}</p>
      `}
      ${status.needsReview ? `
        <p class="action-muted">Review the assessment above, then use the verdict pill to mark this clause reviewed.</p>
      ` : ""}
      <div class="clause-comment-action">
        <label class="detail-field-label" for="review-comment-${escapeHtml(clause.id)}">Attach comment</label>
        ${renderClauseCommentTargetLabel(clause)}
        <textarea id="review-comment-${escapeHtml(clause.id)}" class="review-comment-input" data-review-comment-clause-id="${escapeHtml(clause.id)}" rows="4" placeholder="Leave a comment for Word export">${escapeHtml(comment?.text || "")}</textarea>
      </div>
    </div>
  `;
}

// Name the Word paragraph the clause comment will attach to. setClauseReviewComment
// resolves the same target via firstClauseParagraphId, so the label mirrors where
// the comment actually lands: a numbered paragraph when one matched, or the clause
// heading fallback when firstClauseParagraphId returns "".
function renderClauseCommentTargetLabel(clause) {
  const targetParagraphId = firstClauseParagraphId(clause.id, clause);
  const message = targetParagraphId
    ? `Comment will attach to ${paragraphDisplayLabel(targetParagraphId)}`
    : "No matching paragraph; comment will attach to the clause heading";
  return `<p class="comment-target-label">${escapeHtml(message)}</p>`;
}

// The single proposed-edit display in the detail panel: the connected card per
// redline edit. Renders nothing when the clause has no redline — the Recommended
// change block already carries the no-redline messaging (resolution question or
// "prepare an explicit redline"), so there is no empty placeholder here.
function renderProposedRedlinesBlock(clause) {
  const redlines = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id);
  if (!redlines.length) return "";
  // 2.4: the rationale can land on the edit (edit.redline_rationale) or, per the
  // "per clause" contract, on the clause itself. Resolve the clause-level one
  // once here and pass it as the per-edit fallback.
  const clauseRationale = clause && typeof clause.redline_rationale === "object"
    ? clause.redline_rationale
    : null;
  return `
    <div class="studio-detail-block proposed-redline-block">
      <small>${redlines.length === 1 ? "Proposed redline" : "Proposed redlines"}</small>
      <div class="detail-redline-list">
        ${redlines.map((edit) => renderDetailRedlineEdit(edit, clauseRationale)).join("")}
      </div>
    </div>
  `;
}

// The single connected proposed-edit card. One unit hosts everything for an edit:
// the action label + Include/Ignore decision, the red/green inline redline preview,
// the clean "fixed clause" final text, the jurisdiction/template options (when the
// backend supplied template_options), and the rationale. The whole card re-renders
// when a different option is selected (setRedlineTemplateSelection -> renderStudioDetail),
// so the preview + fixed clause always reflect the live selection. This card is the
// SINGLE proposed-edit display in the detail panel — there is no second caption.
function renderDetailRedlineEdit(edit, clauseRationale = null) {
  const included = redlineExportIncluded(edit);
  const selectedEdit = applyTemplateSelectionToRedline(edit);
  return `
    <div class="detail-redline-edit ${included ? "included" : "ignored"}">
      <div class="detail-redline-head">
        <span class="redline-label">${escapeHtml(redlineActionLabel(selectedEdit))}</span>
        <span class="detail-export-controls" role="group" aria-label="Redline decision">
          <button class="export-choice ${included ? "active" : ""}" type="button" data-export-redline-id="${escapeHtml(edit.id)}" data-export-decision="include" aria-pressed="${included ? "true" : "false"}">Include</button>
          <button class="export-choice ${!included ? "active" : ""}" type="button" data-export-redline-id="${escapeHtml(edit.id)}" data-export-decision="ignore" aria-pressed="${!included ? "true" : "false"}">Ignore</button>
        </span>
      </div>
      ${renderRedlineEditPreview(selectedEdit)}
      ${renderFixedClausePreview(selectedEdit)}
      ${renderRedlineTemplateOptions(selectedEdit)}
      ${renderRedlineRationaleBlock(selectedEdit, clauseRationale)}
    </div>
  `;
}

// The redline preview inside the card. REUSE the shared inline-diff helpers
// (renderCardReplacementRedline -> renderDiffOperations) so the red/green diff is
// identical to the document view; never duplicate divergent diff logic here.
// Keeps the .redline-original / .redline-replacement / .inline-del / .inline-ins
// classes the rest of the UI (and the tests) depend on.
function renderRedlineEditPreview(selectedEdit) {
  if (selectedEdit.action === REDLINE_INSERT_AFTER_PARAGRAPH) {
    return `
      ${renderRedlineAnchor(selectedEdit)}
      ${renderRedlineReplacement(selectedEdit, "p")}
    `;
  }
  if (selectedEdit.action === REDLINE_DELETE_PARAGRAPH) {
    return `
      <p class="redline-original">${escapeHtml(selectedEdit.original_text || "")}</p>
      ${renderRedlineReplacement(selectedEdit, "p")}
    `;
  }
  const original = String(selectedEdit.original_text || "").trim();
  const replacement = String(redlineEditContract()?.redlineReplacementText(selectedEdit)
    || selectedEdit.replacement_text || "").trim();
  // Prefer the shared word-level inline redline (struck source + inserted new) so
  // the preview reads as one connected diff; fall back to the plain struck-original
  // + clean-replacement lines when the diff renderer is unavailable.
  if (original && replacement) {
    const inline = renderCardReplacementRedline(original, replacement, selectedEdit);
    if (inline) {
      return `<p class="redline-original redline-inline-diff" data-redline-replacement>${inline}</p>`;
    }
  }
  return `
    <p class="redline-original">${escapeHtml(selectedEdit.original_text || "")}</p>
    ${renderRedlineReplacement(selectedEdit, "p")}
  `;
}

// The clean, final wording the selected edit produces (no diff markup) — what the
// clause reads as once the redline is accepted. Updates immediately when a
// different template option is picked, because selectedEdit is the live
// applyTemplateSelectionToRedline result.
function renderFixedClausePreview(selectedEdit) {
  if (selectedEdit.action === REDLINE_DELETE_PARAGRAPH) return "";
  const fixedText = String(
    redlineEditContract()?.redlineInsertedText(selectedEdit)
      || selectedEdit.replacement_text
      || selectedEdit.insert_text
      || selectedEdit.text
      || "",
  ).trim();
  if (!fixedText) return "";
  return `
    <div class="fixed-clause-preview">
      <span class="redline-label">Fixed clause</span>
      <p class="fixed-clause-text">${escapeHtml(fixedText)}</p>
    </div>
  `;
}

// "Why this redline" beside each suggested edit (task 2.4). Prefers the
// backend's redline_rationale = { explanation, basis: { quote, paragraph_id } }
// (sourced from the Playbook fallback wording + the clause citation), and falls
// back to the locally derived sentence when that field has not landed yet, so a
// rationale line is always present.
function renderRedlineRationaleBlock(edit, clauseRationale = null) {
  const rationale = (edit && typeof edit.redline_rationale === "object" ? edit.redline_rationale : null)
    || (clauseRationale && typeof clauseRationale === "object" ? clauseRationale : null);
  const explanation = rationale ? String(rationale.explanation || "").trim() : "";
  const basis = rationale && typeof rationale.basis === "object" ? rationale.basis : null;
  const basisQuote = basis ? String(basis.quote || "").trim() : "";
  const basisParagraphId = basis ? String(basis.paragraph_id || "").trim() : "";
  const basisLabel = basisParagraphId ? paragraphDisplayLabel(basisParagraphId) : "";
  const basisBlock = basisQuote
    ? `
      <figure class="redline-rationale-basis">
        <figcaption>${escapeHtml(basisLabel ? `Why · ${basisLabel}` : "Why")}</figcaption>
        <blockquote>${escapeHtml(basisQuote)}</blockquote>
      </figure>
    `
    : "";
  return `
    <div class="redline-rationale">
      <div class="redline-rationale-head">
        <strong>Redline Rationale</strong>
      </div>
      <p>${escapeHtml(explanation || redlineRationaleFallback(edit))}</p>
      ${basisBlock}
    </div>
  `;
}

function redlineRationaleFallback(edit) {
  const selectedOption = (edit.template_options || []).find((option) => option.selected);
  const optionLabel = selectedOption ? displayRedlineOptionLabel(selectedOption) : "";
  const action = String(edit.action || "").trim();
  if (optionLabel) {
    return `This applies the ${optionLabel} playbook wording to address the flagged clause.`;
  }
  if (action === REDLINE_DELETE_PARAGRAPH) {
    return "This removes language that is outside the playbook position for this clause.";
  }
  if (action === REDLINE_INSERT_AFTER_PARAGRAPH) {
    return "This adds playbook wording where the document needs an express clause.";
  }
  return "This replaces the flagged wording with the playbook position for this clause.";
}

function renderRedlineAnchor(edit) {
  const paragraphLabel = edit.paragraph_index ? `Paragraph ${edit.paragraph_index}` : "Selected paragraph";
  const anchorText = edit.anchor_text || "";
  return `
    <p class="redline-anchor">
      <strong>${escapeHtml(paragraphLabel)}</strong>
      ${escapeHtml(anchorText)}
    </p>
  `;
}

function renderRedlineTemplateOptions(edit) {
  const options = edit.template_options || [];
  if (options.length <= 1) return "";

  // Entity-aware: for the governing-law clause the recommended option is the one
  // matching the PICKED Aspora entity's law — read directly via
  // pickedEntityLawLabel() so it tracks the entity even when the document already
  // concurs (governingLawConflict() returns null on concurrence, so it cannot be
  // the source). Display-only: never alters the concurrence verdict.
  const isGovLaw = String(edit.clause_id || "") === "governing_law";
  const recommendedLaw = isGovLaw ? pickedEntityLawLabel().toLowerCase() : "";

  // OPTION B — the recommendation is ADVISORY ONLY. The CHECKED radio (.selected /
  // aria-checked) ALWAYS tracks the STAGED EXPORT selection: the exact option that
  // selectedRedlineTemplateOptionId() resolves from state.redlineTemplateSelections,
  // which is what applyTemplateSelectionToRedline (Fixed-clause preview + exported
  // DOCX) uses. So the checked radio and the exported law can never disagree.
  //
  // The entity recommendation is surfaced ONLY as the "— recommended" TEXT label
  // beside its option (below); it does NOT move the checked state. The two signals
  // are decoupled: CHECKED = what will export; "— recommended" = the entity's law.
  const visualSelectedId = selectedRedlineTemplateOptionId(edit);

  return `
    <div class="redline-options" role="radiogroup" aria-label="Jurisdiction options">
      <span class="redline-options-title">Jurisdiction options</span>
      ${options.map((option) => {
        const label = displayRedlineOptionLabel(option);
        // Exactly one recommended option: the entity match when an entity is
        // picked (it takes precedence), else the backend default.
        const recommended = recommendedLaw
          ? (String(label).trim().toLowerCase() === recommendedLaw)
          : Boolean(option.selected);
        const isVisualSelected = String(option.id || "") === String(visualSelectedId);
        return `
        <button class="redline-option ${isVisualSelected ? "selected" : ""}" type="button" role="radio" data-redline-edit-id="${escapeHtml(edit.id)}" data-redline-option-id="${escapeHtml(option.id || "")}" aria-checked="${isVisualSelected ? "true" : "false"}" aria-pressed="${isVisualSelected ? "true" : "false"}">
          <span class="redline-option-dot" aria-hidden="true"></span>
          <span class="redline-option-copy">
            <strong>${escapeHtml(label)}${recommended ? " — recommended" : ""}</strong>
            <span>${escapeHtml(option.text || option.replacement_text || option.insert_text || "")}</span>
          </span>
        </button>
      `;
      }).join("")}
    </div>
  `;
}

function displayRedlineOptionLabel(option) {
  const label = String(option?.label || "Option").replace(/\s*[-–—]\s*default\s*$/i, "").trim();
  return label || "Option";
}

function bindTemplateOptionControls(container) {
  container.querySelectorAll("[data-redline-edit-id][data-redline-option-id], [data-redline-template-edit-id][data-redline-option-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const editId = button.dataset.redlineEditId || button.dataset.redlineTemplateEditId;
      setRedlineTemplateSelection(editId, button.dataset.redlineOptionId);
    });
  });
}

function bindReviewCommentControls(container) {
  container.querySelectorAll("[data-review-comment-clause-id]").forEach((input) => {
    input.addEventListener("input", () => {
      setClauseReviewComment(input.dataset.reviewCommentClauseId, input.value);
    });
  });
}

function bindParagraphCommentControls(container) {
  container.querySelectorAll("[data-add-paragraph-comment-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openCommentCard(button.dataset.addParagraphCommentId, { compose: "paragraph" });
    });
  });
  container.querySelectorAll("[data-add-selection-comment-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const paragraphId = button.dataset.addSelectionCommentId;
      const selectionInfo = selectedTextInParagraph(paragraphId);
      if (selectionInfo?.selectedText) {
        // Selected text -> start a new selection-scoped comment.
        openCommentCard(paragraphId, { compose: "selection", selectionInfo });
        return;
      }
      // No active selection: never a dead end. Open existing threads if there
      // are any, otherwise compose a paragraph-level comment.
      if (paragraphCommentThreads(paragraphId).length) {
        openCommentCard(paragraphId, { mode: "read" });
      } else {
        openCommentCard(paragraphId, { compose: "paragraph" });
      }
    });
  });
  // Clicking the comment-count badge opens the thread(s) for read / edit / reply / resolve.
  container.querySelectorAll("[data-edit-paragraph-comments-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openCommentCard(button.dataset.editParagraphCommentsId, { mode: "read" });
    });
  });
}

function closeParagraphCommentComposers() {
  detachCommentCardListeners();
  studioDocumentRender?.querySelectorAll(".paragraph-comment-composer, .comment-thread-card").forEach((composer) => {
    composer.closest(".studio-doc-paragraph")?.classList.remove("has-comment-composer");
    composer.remove();
  });
}

function clearSelectionCommentAffordances() {
  studioDocumentRender?.querySelectorAll(".studio-doc-paragraph.has-selection").forEach((paragraph) => {
    paragraph.classList.remove("has-selection");
    paragraph.querySelector(".paragraph-comment-tools")?.removeAttribute("style");
  });
}

// ---- Word-style comment threads -------------------------------------------
// A "thread" is one root comment (no parent_id) plus its replies (parent_id ===
// root.id). The card shows every thread anchored to a paragraph, each with the
// author, the text, an Edit/Delete menu, a Resolve toggle and a reply box.

const COMMENT_KEBAB_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg>';
const COMMENT_CHECK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M20 6 9 17l-5-5"/></svg>';
const COMMENT_SEND_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>';

let commentCardOutsideHandler = null;
let commentCardResizeHandler = null;

function detachCommentCardListeners() {
  if (commentCardOutsideHandler) {
    document.removeEventListener("mousedown", commentCardOutsideHandler, true);
    commentCardOutsideHandler = null;
  }
  if (commentCardResizeHandler) {
    window.removeEventListener("resize", commentCardResizeHandler);
    commentCardResizeHandler = null;
  }
}

// Word docks comments in the page margin. Our document page is a centred,
// max-width column inside a full-width panel, so on a wide view there is a grey
// gutter on either side. When the right gutter is wide enough we float the card
// into it (absolutely, relative to its paragraph, so it scrolls in step and
// never pushes the text); otherwise we leave it inline beneath the paragraph.
const COMMENT_CARD_MARGIN_GAP = 14;
const COMMENT_CARD_MIN_MARGIN_WIDTH = 120;
const COMMENT_CARD_MAX_WIDTH = 340;

function dockCommentCardInMargin(card, paragraph) {
  const page = paragraph.closest(".studio-page");
  const wrap = paragraph.closest(".studio-page-wrap");
  const resetInline = () => {
    card.classList.remove("is-margin-docked");
    card.style.position = "";
    card.style.top = "";
    card.style.left = "";
    card.style.width = "";
    card.style.marginTop = "";
  };
  if (!page || !wrap) { resetInline(); return false; }

  const pageRect = page.getBoundingClientRect();
  const wrapStyle = window.getComputedStyle(wrap);
  const wrapPadRight = parseFloat(wrapStyle.paddingRight) || 0;
  const wrapInnerRight = wrap.getBoundingClientRect().right - wrapPadRight;
  const rightGutter = wrapInnerRight - pageRect.right;
  if (rightGutter < COMMENT_CARD_MIN_MARGIN_WIDTH + COMMENT_CARD_MARGIN_GAP) {
    resetInline();
    return false;
  }

  const cardWidth = Math.min(COMMENT_CARD_MAX_WIDTH, rightGutter - COMMENT_CARD_MARGIN_GAP - 8);
  const paraRect = paragraph.getBoundingClientRect();
  card.classList.add("is-margin-docked");
  card.style.position = "absolute";
  card.style.top = "0px";
  card.style.left = `${Math.round(pageRect.right + COMMENT_CARD_MARGIN_GAP - paraRect.left)}px`;
  card.style.width = `${Math.round(cardWidth)}px`;
  card.style.marginTop = "0";
  return true;
}

function paragraphCommentThreads(paragraphId) {
  // Clause-scoped comments may also carry a paragraph_id (their clause's anchor
  // paragraph); they belong to the clause lane, not the in-document thread card.
  const all = normalizeReviewComments(state.reviewComments)
    .filter((comment) => comment.paragraph_id === paragraphId && !comment.clause_id);
  const byCreated = (a, b) => String(a.created_at || "").localeCompare(String(b.created_at || ""));
  return all
    .filter((comment) => !comment.parent_id)
    .sort(byCreated)
    .map((root) => ({
      root,
      replies: all.filter((comment) => comment.parent_id === root.id).sort(byCreated),
    }));
}

function commentAuthorName(comment) {
  return String(comment?.author || "Reviewer").trim() || "Reviewer";
}

function commentAuthorInitials(comment) {
  const name = commentAuthorName(comment);
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("");
  return (initials || name[0] || "R").toUpperCase();
}

function formatCommentTimestamp(value) {
  const iso = String(value || "").trim();
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  try {
    return `${date.toLocaleDateString(undefined, { day: "numeric", month: "short" })}, ${date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
  } catch (error) {
    return iso;
  }
}

function nextCommentReplyId(rootId) {
  const base = `comment-reply-${rootId}-`;
  let max = 0;
  normalizeReviewComments(state.reviewComments).forEach((comment) => {
    if (typeof comment.id === "string" && comment.id.startsWith(base)) {
      const value = Number(comment.id.slice(base.length));
      if (Number.isFinite(value) && value > max) max = value;
    }
  });
  return `${base}${max + 1}`;
}

function addCommentReply(rootId, text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return;
  const root = normalizeReviewComments(state.reviewComments).find((comment) => comment.id === rootId);
  if (!root) return;
  upsertReviewComment({
    ...reviewCommentTargetForParagraph(root.paragraph_id),
    author: "Reviewer",
    created_at: new Date().toISOString(),
    id: nextCommentReplyId(rootId),
    parent_id: rootId,
    scope: "reply",
    text: trimmed,
  });
}

function editReviewCommentText(commentId, text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return;
  const existing = normalizeReviewComments(state.reviewComments).find((comment) => comment.id === commentId);
  if (!existing) return;
  upsertReviewComment({ ...existing, text: trimmed });
}

function removeReviewCommentThread(commentId) {
  const all = normalizeReviewComments(state.reviewComments);
  const target = all.find((comment) => comment.id === commentId);
  if (!target) return;
  pushReviewCommentsHistory();
  const removeIds = new Set([commentId]);
  if (!target.parent_id) {
    // Deleting a thread root removes its replies too.
    all.forEach((comment) => {
      if (comment.parent_id === commentId) removeIds.add(comment.id);
    });
  }
  state.reviewComments = all.filter((comment) => !removeIds.has(comment.id));
  markRedlineDraftDirty();
  renderStudioDocumentHighlights();
  renderStudioClauseLane();
  updateExportButtonState();
}

function toggleReviewCommentResolved(rootId) {
  const existing = normalizeReviewComments(state.reviewComments).find((comment) => comment.id === rootId);
  if (!existing) return;
  upsertReviewComment({ ...existing, resolved: !existing.resolved });
}

// Highlight only the specific commented words in the document. Walks the
// paragraph's editable text nodes (the same textContent-offset model the app
// uses for selection restore via editableTextPositionForOffset), validates the
// stored offsets against selected_text, and wraps exactly that span in a purple
// <mark>. Re-applied on every render; the paragraph background is untouched.
function normalizeCommentWS(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function applyCommentTextHighlights() {
  if (!studioDocumentRender) return;
  const activeEditable = document.activeElement?.closest?.("[data-editable-paragraph-id]");
  normalizeReviewComments(state.reviewComments)
    .filter((comment) => comment.paragraph_id && !comment.clause_id && !comment.parent_id)
    .forEach((comment) => {
      const paragraph = studioDocumentRender.querySelector(
        `[data-paragraph-id="${cssEscape(comment.paragraph_id)}"]`,
      );
      const editable = paragraph?.querySelector("[data-editable-paragraph-id]");
      if (!editable || editable === activeEditable) return;
      highlightCommentRange(editable, comment);
    });
}

function highlightCommentRange(editable, comment) {
  const walker = document.createTreeWalker(editable, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let fullText = "";
  let node;
  while ((node = walker.nextNode())) {
    nodes.push({ node, start: fullText.length });
    fullText += node.textContent;
  }
  if (!fullText) return;

  const selected = String(comment.selected_text || "");
  let start = -1;
  let end = -1;
  if (comment.scope === "selection" || selected) {
    const storedStart = Number(comment.selection_start);
    const storedEnd = Number(comment.selection_end);
    if (
      Number.isFinite(storedStart) && Number.isFinite(storedEnd)
      && storedStart >= 0 && storedEnd > storedStart && storedEnd <= fullText.length
      && (!selected || normalizeCommentWS(fullText.slice(storedStart, storedEnd)) === normalizeCommentWS(selected))
    ) {
      start = storedStart;
      end = storedEnd;
    } else if (selected) {
      const idx = fullText.indexOf(selected);
      if (idx >= 0) {
        start = idx;
        end = idx + selected.length;
      }
    }
  } else {
    // Paragraph-scope comment with no specific range: highlight the whole text.
    start = 0;
    end = fullText.length;
  }
  if (start < 0 || end <= start) return;

  nodes.forEach(({ node: textNode, start: nodeStart }) => {
    const nodeEnd = nodeStart + textNode.textContent.length;
    const from = Math.max(start, nodeStart);
    const to = Math.min(end, nodeEnd);
    if (to <= from) return;
    try {
      const range = document.createRange();
      range.setStart(textNode, from - nodeStart);
      range.setEnd(textNode, to - nodeStart);
      const mark = document.createElement("mark");
      mark.className = "comment-word-highlight";
      range.surroundContents(mark);
    } catch (error) {
      /* a range that can't be wrapped is skipped rather than throwing */
    }
  });
}

function applyClauseEvidenceHighlight(clauseId, item, toneClass) {
  const paragraphId = String(item?.paragraph_id || "").trim();
  if (!paragraphId || !studioDocumentRender) return false;
  const frame = studioDocumentRender.querySelector(`[data-paragraph-id="${cssEscape(paragraphId)}"]`);
  if (!frame) return false;
  const editable = frame.querySelector("[data-editable-paragraph-id]") || frame;
  const paragraph = state.reviewParagraphs.find((entry) => String(entry.id || "") === paragraphId);
  const paragraphStart = Number(paragraph?.start);
  const spans = Array.isArray(item?.spans) ? item.spans : [];
  const quote = String(item?.quote || "").trim();

  // (T5c) Under TRACKED CHANGES the start-offset math is unreliable: the spans'
  // start/end were computed against the clean source text, but a faithful surface
  // rendered with renderChanges:true interleaves <ins>/<del> markup, so the
  // character offsets no longer line up (a deleted run still contributes text the
  // span offsets did not account for). When this paragraph carries tracked-change
  // descendants, PREFER the quote-substring path (which finds the visible text
  // wherever it lands) over the offset math. Falls back to offsets only if there is
  // no usable quote.
  const hasTrackedChanges = typeof editable.querySelector === "function"
    && Boolean(editable.querySelector("ins, del"));

  const applyByQuote = () => {
    if (!quote) return false;
    const fullText = editable.textContent || "";
    const index = fullText.toLowerCase().indexOf(quote.toLowerCase());
    if (index < 0) return false;
    return highlightClauseTextRange(editable, index, index + quote.length, clauseId, toneClass);
  };

  const applyBySpans = () => {
    let applied = false;
    spans.forEach((span) => {
      const start = Number(span?.start);
      const end = Number(span?.end);
      if (Number.isFinite(start) && Number.isFinite(end) && Number.isFinite(paragraphStart)) {
        applied = highlightClauseTextRange(editable, start - paragraphStart, end - paragraphStart, clauseId, toneClass) || applied;
      }
    });
    return applied;
  };

  if (hasTrackedChanges) {
    if (applyByQuote()) return true;
    if (applyBySpans()) return true;
  } else {
    if (applyBySpans()) return true;
    if (applyByQuote()) return true;
  }

  frame.classList.add(toneClass);
  return true;
}

function highlightClauseTextRange(editable, start, end, clauseId, toneClass) {
  const from = Math.max(0, Number(start));
  const to = Math.max(from, Number(end));
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return false;
  const walker = document.createTreeWalker(editable, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let fullText = "";
  let node;
  while ((node = walker.nextNode())) {
    nodes.push({ node, start: fullText.length });
    fullText += node.textContent;
  }
  if (to > fullText.length) return false;
  let applied = false;
  nodes.forEach(({ node: textNode, start: nodeStart }) => {
    const nodeEnd = nodeStart + textNode.textContent.length;
    const rangeStart = Math.max(from, nodeStart);
    const rangeEnd = Math.min(to, nodeEnd);
    if (rangeEnd <= rangeStart) return;
    try {
      const range = document.createRange();
      range.setStart(textNode, rangeStart - nodeStart);
      range.setEnd(textNode, rangeEnd - nodeStart);
      const mark = document.createElement("mark");
      mark.className = `clause-evidence-highlight ${toneClass}`;
      mark.dataset.clauseEvidenceId = clauseId;
      mark.addEventListener("click", (event) => {
        event.stopPropagation();
        selectReviewClause(clauseId, { jump: false });
      });
      range.surroundContents(mark);
      applied = true;
    } catch (error) {
      /* a range that can't be wrapped is skipped rather than throwing */
    }
  });
  return applied;
}

function openCommentCard(paragraphId, opts = {}) {
  const paragraph = studioDocumentRender?.querySelector(
    `[data-paragraph-id="${cssEscape(paragraphId)}"]`,
  );
  if (!paragraph) return;

  clearSelectionCommentAffordances();
  closeParagraphCommentComposers();
  paragraph.classList.add("has-comment-composer");

  const card = document.createElement("div");
  card.className = "comment-thread-card";
  card.setAttribute("contenteditable", "false");
  card.addEventListener("click", (event) => event.stopPropagation());

  const threads = paragraphCommentThreads(paragraphId);
  threads.forEach(({ root, replies }) => {
    card.append(buildCommentThread(paragraphId, root, replies));
  });

  const composeScope = opts.compose;
  if (composeScope || threads.length === 0) {
    card.append(buildCommentComposeBox(paragraphId, composeScope || "paragraph", opts.selectionInfo || null));
  }

  paragraph.append(card);

  const docked = dockCommentCardInMargin(card, paragraph);

  detachCommentCardListeners();
  commentCardOutsideHandler = (event) => {
    if (!card.contains(event.target)) closeParagraphCommentComposers();
  };
  document.addEventListener("mousedown", commentCardOutsideHandler, true);
  if (docked) {
    commentCardResizeHandler = () => dockCommentCardInMargin(card, paragraph);
    window.addEventListener("resize", commentCardResizeHandler);
  }

  requestAnimationFrame(() => {
    const focusTarget = card.querySelector(composeScope ? ".comment-compose-input" : ".comment-reply-input");
    if (composeScope && focusTarget) focusTarget.focus({ preventScroll: true });
  });
}

function buildCommentThread(paragraphId, root, replies) {
  const thread = document.createElement("div");
  thread.className = "comment-thread";
  if (root.resolved) thread.classList.add("resolved");

  thread.append(buildCommentEntry(paragraphId, root, true));
  replies.forEach((reply) => thread.append(buildCommentEntry(paragraphId, reply, false)));

  const replyBox = document.createElement("div");
  replyBox.className = "comment-reply-box";
  const replyInput = document.createElement("textarea");
  replyInput.className = "comment-reply-input";
  replyInput.rows = 1;
  replyInput.placeholder = "Reply";
  const replySend = document.createElement("button");
  replySend.type = "button";
  replySend.className = "comment-reply-send";
  replySend.setAttribute("aria-label", "Send reply");
  replySend.innerHTML = COMMENT_SEND_ICON;
  const sendReply = () => {
    const value = replyInput.value.trim();
    if (!value) { replyInput.focus(); return; }
    addCommentReply(root.id, value);
    setFileMeta("Reply added");
    openCommentCard(paragraphId, { mode: "read" });
  };
  replySend.addEventListener("click", (event) => { event.stopPropagation(); sendReply(); });
  replyInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      sendReply();
    }
  });
  replyBox.append(replyInput, replySend);
  thread.append(replyBox);
  return thread;
}

function buildCommentEntry(paragraphId, comment, isRoot) {
  const entry = document.createElement("div");
  entry.className = isRoot ? "comment-entry comment-entry-root" : "comment-entry comment-entry-reply";

  const avatar = document.createElement("div");
  avatar.className = "comment-avatar";
  avatar.textContent = commentAuthorInitials(comment);
  entry.append(avatar);

  const body = document.createElement("div");
  body.className = "comment-body";

  const head = document.createElement("div");
  head.className = "comment-head";
  const author = document.createElement("span");
  author.className = "comment-author";
  author.textContent = commentAuthorName(comment);
  const time = document.createElement("span");
  time.className = "comment-time";
  time.textContent = formatCommentTimestamp(comment.created_at);
  head.append(author, time);

  const entryActions = document.createElement("div");
  entryActions.className = "comment-entry-actions";

  if (isRoot) {
    const resolveBtn = document.createElement("button");
    resolveBtn.type = "button";
    resolveBtn.className = comment.resolved ? "comment-resolve-btn is-resolved" : "comment-resolve-btn";
    resolveBtn.title = comment.resolved ? "Reopen" : "Resolve";
    resolveBtn.setAttribute("aria-label", resolveBtn.title);
    resolveBtn.innerHTML = COMMENT_CHECK_ICON;
    resolveBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      const wasResolved = comment.resolved;
      toggleReviewCommentResolved(comment.id);
      setFileMeta(wasResolved ? "Comment reopened" : "Comment resolved");
      openCommentCard(paragraphId, { mode: "read" });
    });
    entryActions.append(resolveBtn);
  }

  const menuWrap = document.createElement("div");
  menuWrap.className = "comment-menu-wrap";
  const menuBtn = document.createElement("button");
  menuBtn.type = "button";
  menuBtn.className = "comment-menu-btn";
  menuBtn.setAttribute("aria-label", "Comment options");
  menuBtn.innerHTML = COMMENT_KEBAB_ICON;
  const menu = document.createElement("div");
  menu.className = "comment-menu";
  menu.hidden = true;
  const editItem = document.createElement("button");
  editItem.type = "button";
  editItem.className = "comment-menu-item";
  editItem.textContent = "Edit";
  const deleteItem = document.createElement("button");
  deleteItem.type = "button";
  deleteItem.className = "comment-menu-item comment-menu-item-danger";
  deleteItem.textContent = "Delete";
  menu.append(editItem, deleteItem);
  menuBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    const wasHidden = menu.hidden;
    entry.closest(".comment-thread-card")?.querySelectorAll(".comment-menu").forEach((other) => {
      other.hidden = true;
    });
    menu.hidden = !wasHidden;
  });
  editItem.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden = true;
    enterCommentEditMode(paragraphId, comment, body);
  });
  deleteItem.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden = true;
    removeReviewCommentThread(comment.id);
    setFileMeta("Comment removed");
    if (paragraphCommentThreads(paragraphId).length) {
      openCommentCard(paragraphId, { mode: "read" });
    } else {
      detachCommentCardListeners();
    }
  });
  menuWrap.append(menuBtn, menu);
  entryActions.append(menuWrap);
  head.append(entryActions);
  body.append(head);

  const textEl = document.createElement("div");
  textEl.className = "comment-text";
  textEl.textContent = comment.text || "";
  body.append(textEl);

  entry.append(body);
  return entry;
}

function enterCommentEditMode(paragraphId, comment, body) {
  const textEl = body.querySelector(".comment-text");
  if (!textEl) return;

  const editor = document.createElement("div");
  editor.className = "comment-edit";
  const input = document.createElement("textarea");
  input.className = "comment-edit-input";
  input.rows = 2;
  input.value = comment.text || "";

  const row = document.createElement("div");
  row.className = "comment-edit-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "comment-edit-save";
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "comment-edit-cancel";
  cancel.textContent = "Cancel";
  row.append(save, cancel);
  editor.append(input, row);
  textEl.replaceWith(editor);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);

  save.addEventListener("click", (event) => {
    event.stopPropagation();
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    editReviewCommentText(comment.id, value);
    setFileMeta("Comment updated");
    openCommentCard(paragraphId, { mode: "read" });
  });
  cancel.addEventListener("click", (event) => {
    event.stopPropagation();
    openCommentCard(paragraphId, { mode: "read" });
  });
}

function buildCommentComposeBox(paragraphId, scope, selectionInfo) {
  const box = document.createElement("div");
  box.className = "comment-compose";

  const input = document.createElement("textarea");
  input.className = "comment-compose-input";
  input.rows = 2;
  input.placeholder = "Add a comment";
  box.append(input);

  const row = document.createElement("div");
  row.className = "comment-compose-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "comment-compose-save";
  save.textContent = "Comment";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "comment-compose-cancel";
  cancel.textContent = "Cancel";
  row.append(save, cancel);
  box.append(row);

  save.addEventListener("click", (event) => {
    event.stopPropagation();
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    if (scope === "selection" && selectionInfo?.selectedText) {
      setSelectedTextReviewComment(paragraphId, selectionInfo, value);
    } else {
      setParagraphReviewComment(paragraphId, value);
    }
    setFileMeta("Comment saved for Word export");
    openCommentCard(paragraphId, { mode: "read" });
  });
  cancel.addEventListener("click", (event) => {
    event.stopPropagation();
    closeParagraphCommentComposers();
  });
  return box;
}

function selectedTextInParagraph(paragraphId) {
  const editable = studioDocumentRender?.querySelector(
    `[data-editable-paragraph-id="${cssEscape(paragraphId)}"]`,
  );
  const paragraphFrame = studioDocumentRender?.querySelector(
    `[data-paragraph-id="${cssEscape(paragraphId)}"]`,
  );
  const selection = window.getSelection();
  if (!paragraphFrame || !selection || !selection.rangeCount) return null;
  const range = selection.getRangeAt(0);
  if (
    selection.isCollapsed
    || !paragraphFrame.contains(range.startContainer)
    || !paragraphFrame.contains(range.endContainer)
  ) {
    return null;
  }

  if (editable?.contains(range.startContainer) && editable.contains(range.endContainer)) {
    const startOffset = editableSelectionTextOffset(editable, range.startContainer, range.startOffset);
    const endOffset = editableSelectionTextOffset(editable, range.endContainer, range.endOffset);
    const selectedText = editableParagraphText(editable).slice(startOffset, endOffset).trim();
    if (!selectedText) return null;
    return {
      endOffset,
      selectedText,
      startOffset,
    };
  }

  const selectedText = normalizeSelectedCommentText(selection.toString());
  if (!selectedText) return null;
  const offsets = selectedTextOffsetsInParagraph(currentParagraphText(paragraphId), selectedText);
  return {
    endOffset: offsets.endOffset,
    selectedText,
    startOffset: offsets.startOffset,
  };
}

function normalizeSelectedCommentText(value) {
  return String(value || "")
    .replace(/\u00a0/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function selectedTextOffsetsInParagraph(paragraphText, selectedText) {
  const sourceText = String(paragraphText || "");
  const exactStart = sourceText.indexOf(selectedText);
  if (exactStart >= 0) {
    return {
      endOffset: exactStart + selectedText.length,
      startOffset: exactStart,
    };
  }

  const sourceIndex = createSelectionSearchIndex(sourceText);
  const normalizedSelection = normalizeSelectedCommentText(selectedText);
  const normalizedStart = sourceIndex.normalized.indexOf(normalizedSelection);
  if (normalizedStart >= 0) {
    const normalizedEnd = Math.min(
      normalizedStart + normalizedSelection.length - 1,
      sourceIndex.map.length - 1,
    );
    return {
      endOffset: sourceIndex.map[normalizedEnd] + 1,
      startOffset: sourceIndex.map[normalizedStart],
    };
  }

  return {
    endOffset: Math.min(sourceText.length, selectedText.length),
    startOffset: 0,
  };
}

function createSelectionSearchIndex(value) {
  let normalized = "";
  const map = [];
  let previousWasSpace = false;
  String(value || "").split("").forEach((char, index) => {
    if (/\s/.test(char)) {
      if (normalized && !previousWasSpace) {
        normalized += " ";
        map.push(index);
      }
      previousWasSpace = true;
      return;
    }
    normalized += char;
    map.push(index);
    previousWasSpace = false;
  });
  return { map, normalized: normalized.trim() };
}

function renderStudioDocumentHighlights() {
  if (!studioDocumentRender) return;

  if (!state.reviewClauses.length) {
    notifyPdfMarkupLeaveOriginal();
    showStudioSourceEditor();
    return;
  }

  if (!state.reviewParagraphs.length) {
    notifyPdfMarkupLeaveOriginal();
    showStudioSourceEditor();
    return;
  }
  const viewMode = state.documentViewMode || VIEW_MODE_REDLINE;

  if (viewMode === VIEW_MODE_ORIGINAL) {
    // "Original" is the faithful page-image view: show the rendered surface
    // full-width as the focus and suppress the text reconstruction entirely.
    studioDocumentRender.innerHTML = renderOriginalDocumentSurface(state.reviewDocumentRender);
    bindOriginalViewFallbackControls();
    showStudioDocumentRender();
    // Overlay the interactive PDF markup layer (toolbar + annotations) on the
    // freshly-painted page-image surface. The controller self-gates to a matter
    // being loaded and re-loads only when the matter changes.
    notifyPdfMarkupOriginalRendered();
    // OPTIONAL faithful-DOCX upgrade (feature-flagged, default OFF). For a
    // DOCX-source matter we hold the real .docx bytes; when the flag is on we
    // render the ACTUAL document (styles, tables, numbering, w:ins/w:del tracked
    // changes) over this surface instead of the page-image/reconstruction. This
    // is the LAST thing in the Original branch so the existing surface is already
    // painted: if the faithful render is disabled, unavailable, or fails for any
    // reason it simply leaves the existing surface in place (never blank). It does
    // not touch the structured/redline views, the overview panel, or
    // insert-into-blanks -- those live in the non-Original modes below.
    maybeUpgradeOriginalSurfaceToFaithfulDocx();
    return;
  }
  // Any non-Original render means we have left the Original view: drop the
  // markup toolbar/overlays so they never bleed into the other modes.
  notifyPdfMarkupLeaveOriginal();

  // OUTER ERROR BOUNDARY. Even with the per-paragraph boundary and the redline
  // sanitizer in place, the reconstruction + DOM bind can still throw for a
  // reason we did not anticipate (a malformed clause, a bad render-surface, a
  // binding failure). If it does, we must NOT leave the pane on the blank
  // skeleton -- paint a recoverable error surface and still reveal the pane so
  // the user sees a recoverable state instead of an empty workstation.
  try {
    const documentHtml = renderReviewDocument({
      clauses: state.reviewClauses,
      comments: currentReviewComments(),
      originalParagraphs: manualRedlineBaselineParagraphs(),
      paragraphs: state.reviewParagraphs,
      redlines: effectiveReviewRedlines(),
      selectedClauseId: state.selectedReviewClauseId,
      viewMode,
    });
    studioDocumentRender.innerHTML = `${renderPdfDocumentSurface(state.reviewDocumentRender)}${documentHtml}`;

    studioDocumentRender.querySelectorAll("[data-clause-ids]").forEach((paragraph) => {
      paragraph.addEventListener("click", (event) => {
        if (event.target.closest("[data-editable-paragraph-id]")) return;
        const clauseId = paragraph.dataset.clauseIds.split(" ").filter(Boolean)[0];
        if (clauseId) selectReviewClause(clauseId, { jump: false });
      });
    });
    bindViewerParagraphEditing();
    if (typeof bindFormatToolbar === "function") bindFormatToolbar();
    bindParagraphCommentControls(studioDocumentRender);
    applyCommentTextHighlights();

    showStudioDocumentRender();
    notifyFillHighlights();
    highlightSelectedClauseRefs();

    // OPTIONAL faithful-DOCX upgrade of the non-Original views (redline/clean),
    // feature-flagged (default OFF) exactly like the Original branch. The
    // reconstruction above is already painted and bound as the never-blank,
    // fully-interactive FLOOR; this LAST step renders the ACTUAL reviewed .docx
    // (styles, tables, numbering, w:ins/w:del) over it ONLY when the flag is on,
    // the library is available, the source is a faithful candidate, the mapping
    // guard passes, AND the bytes actually paint. On ANY failure -- flag off,
    // 404 (the /reviewed-docx endpoint is owned by a separate backend lane and may
    // not exist yet), parse error, empty render, or an ABORTED 1:1 mapping -- it
    // leaves the painted reconstruction untouched. Side-by-Side is deliberately
    // NOT faithful-rendered: it stays reconstruction-based (its diff columns have
    // no faithful equivalent), so we only upgrade redline + clean.
    if (viewMode === VIEW_MODE_REDLINE || viewMode === VIEW_MODE_CLEAN) {
      maybeUpgradeSurfaceToFaithfulDocx(viewMode);
    }
  } catch (error) {
    try {
      console.error("renderStudioDocumentHighlights: document render failed; painting recoverable error surface", error);
    } catch (_loggingError) {
      // never let a logging failure swallow the recovery
    }
    paintStudioDocumentRenderError(error);
  }
}

// Recoverable error surface for the OUTER document-render boundary. Painted into
// the document pane (and the pane is still revealed) so a render failure shows a
// readable, recoverable message instead of leaving the workstation blank.
function paintStudioDocumentRenderError(error) {
  if (!studioDocumentRender) return;
  const message = renderDocumentErrorMessage({ error })
    || "The document could not be displayed. Reload or reopen this matter to try again.";
  studioDocumentRender.innerHTML = `
    <div class="studio-doc-render-error" role="alert">
      <strong>The document could not be displayed.</strong>
      <p>${escapeHtml(message)}</p>
      <p>The review data is intact -- reload or reopen this matter to try again.</p>
    </div>
  `;
  showStudioDocumentRender();
}

// Bridge to the Fill controller (constructed in app.js): keep its name/address
// highlights painted on every text render so they persist across tabs and views.
// Guarded so the rendering module stays usable when the controller is absent.
function notifyFillHighlights() {
  if (typeof reviewFillController !== "undefined" && reviewFillController
    && typeof reviewFillController.highlightDocument === "function") {
    reviewFillController.highlightDocument();
  }
}

function bindOriginalViewFallbackControls() {
  studioDocumentRender.querySelectorAll("[data-original-fallback-view-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      setDocumentViewMode(button.dataset.originalFallbackViewMode || VIEW_MODE_REDLINE, { render: true });
    });
  });
}

// Bridges to the interactive PDF markup controller (constructed in app.js).
// Guarded so the rendering module stays usable even if the controller is absent
// (e.g. an isolated render unit test that does not boot the full app).
function notifyPdfMarkupOriginalRendered() {
  if (typeof pdfMarkupController !== "undefined" && pdfMarkupController) {
    pdfMarkupController.onOriginalSurfaceRendered();
  }
}

function notifyPdfMarkupLeaveOriginal() {
  if (typeof pdfMarkupController !== "undefined" && pdfMarkupController) {
    pdfMarkupController.onLeaveOriginal();
  }
}

// True for a DOCX-source matter (real .docx bytes we can render faithfully today).
// Mirrors how sourcePdfRenderCandidate() sniffs the source filename extension.
function matterIsDocxSource(matter) {
  const filename = String(matter?.source_filename || matter?.attachment_filename || "").trim();
  return /\.docx$/i.test(filename);
}

// True for a PDF-source matter (no native DOCX bytes; needs a canonical DOCX built
// by the backend before it can be rendered faithfully).
function matterIsPdfSource(matter) {
  const filename = String(matter?.source_filename || matter?.attachment_filename || "").trim();
  return /\.pdf$/i.test(filename);
}

// PURE selection/precedence function: given a matter, the normalized render-state,
// and the faithful-render capability flags, decide HOW the Original surface should
// be rendered. Returns exactly one of:
//   { render: "faithful_docx", url }  -> render the real DOCX bytes from `url`
//   { render: "page_image" }          -> keep the already-painted base surface (no-op)
//   { render: "reconstruction" }      -> the never-blank floor (text reconstruction)
//
// Precedence (first match wins):
//   1. faithful_docx -- only when the flag is ON *and* the vendored library is
//      available *and* a same-origin DOCX URL exists for this source:
//        - DOCX source  -> /api/matters/<id>/source        (native bytes; today)
//        - PDF source   -> /api/matters/<id>/working-docx   (canonical DOCX) but
//          ONLY when renderState.workingDocxReady === true. That flag + endpoint
//          are owned by a separate backend lane and default absent/false, so this
//          branch is INERT until the backend ships them; PDF matters fall through.
//   2. page_image -- the base surface is already painted, so faithful is a no-op.
//   3. reconstruction -- the never-blank floor.
//
// Pure over its arguments (no globals) so it is unit-testable; the caller passes
// the live flag/library capability + render-state in.
//
// VIEW MODE (Phase 2): the optional fourth argument selects the DOCX bytes to
// render so the faithful surface MATCHES the view the reconstruction would paint:
//   * "original" (or omitted): the SOURCE document, byte-identical to Phase 1
//       (/source for DOCX, /working-docx for a PDF-source canonical DOCX).
//   * "redline":  the REVIEWED document with tracked changes shown -- the backend
//       composes the manual edits + clause redlines onto the real .docx and serves
//       it at /api/matters/<id>/reviewed-docx?changes=tracked.
//   * "clean":    the same reviewed document with changes ACCEPTED, served at
//       /api/matters/<id>/reviewed-docx?changes=accepted.
// The /reviewed-docx endpoint is owned by a separate backend lane and may 404
// until it ships; the caller's renderer degrades to the reconstruction on any
// failure (never blank), so a missing endpoint is safe. Side-by-Side never reaches
// here -- its caller keeps it reconstruction-based.
function selectFaithfulRenderPlan(matter, renderState, capability, viewMode) {
  const flagEnabled = Boolean(capability && capability.flagEnabled);
  const libraryAvailable = Boolean(capability && capability.libraryAvailable);
  const matterId = matter && matter.id;
  const mode = String(viewMode || VIEW_MODE_ORIGINAL);

  // AUTO-ON for a CONVERTED PDF matter. A PDF source that has a canonical working
  // DOCX (Approach C, incl. the retro-conversion backfill) is exactly the matter
  // whose anchors only bind on the faithful DOCX surface -- the page-image view emits
  // no per-paragraph data-paragraph-id targets, so the clause navigator is dead there.
  // PREFER faithful for it regardless of the off-by-default nda.faithfulDocxRender
  // flag. The flag default still governs every other matter: a DOCX source and a PDF
  // source WITHOUT a working DOCX (workingDocxReady !== true) keep the flag's default,
  // because workingDocxAutoOn is false for them.
  const workingDocxAutoOn = Boolean(
    matterIsPdfSource(matter) && renderState && renderState.workingDocxReady === true
  );
  const faithfulEnabled = flagEnabled || workingDocxAutoOn;

  if (faithfulEnabled && libraryAvailable && matterId) {
    const encodedId = encodeURIComponent(matterId);
    // Non-Original (redline/clean): render the REVIEWED docx so tracked changes /
    // accepted changes match the chosen view. Works for both DOCX-source and a
    // PDF-source matter once a canonical working DOCX exists, because the backend
    // composes redlines onto whichever real .docx it holds. Gated the same as the
    // source paths (DOCX always; PDF only once workingDocxReady).
    if (mode === VIEW_MODE_REDLINE || mode === VIEW_MODE_CLEAN) {
      const changes = mode === VIEW_MODE_CLEAN ? "accepted" : "tracked";
      const eligible = matterIsDocxSource(matter)
        || (matterIsPdfSource(matter) && renderState && renderState.workingDocxReady === true);
      if (eligible) {
        return { render: "faithful_docx", url: `/api/matters/${encodedId}/reviewed-docx?changes=${changes}` };
      }
      return { render: "reconstruction" };
    }

    // Original (or any non-redline/clean caller): the SOURCE document, exactly as
    // Phase 1 shipped it.
    if (matterIsDocxSource(matter)) {
      return { render: "faithful_docx", url: `/api/matters/${encodedId}/source` };
    }
    // PDF-source faithful render is gated on the backend having produced a
    // canonical "working" DOCX. Until renderState.workingDocxReady is true this
    // branch is dormant and PDF matters fall through to page_image.
    if (matterIsPdfSource(matter) && renderState && renderState.workingDocxReady === true) {
      return { render: "faithful_docx", url: `/api/matters/${encodedId}/working-docx` };
    }
  }

  // The base Original surface (page image / source preview) is already painted, so
  // there is nothing to upgrade -- this is a no-op, not a blank.
  return { render: "page_image" };
}

// COLD-START catch-22 fix.
//
// selectFaithfulRenderPlan() gates faithful rendering on capability.libraryAvailable
// (window.docx + window.JSZip present) SYNCHRONOUSLY. But the docx-preview vendor
// libs are LAZY-LOADED -- they only inject inside renderFaithfulDocx ->
// ensureFaithfulDocxLibs, which the plan gate would never let run on a cold page.
// So on a fresh load libraryAvailable() is false forever, the plan stays
// page_image/reconstruction, and the faithful upgrade never engages even though the
// vendored scripts are reachable and lazy-injectable.
//
// This helper closes the loop: when the flag is ENABLED but the library is not yet
// loaded, kick the lazy-load (faithful.ensureLibs) ONCE and, on success, re-invoke
// the upgrade -- by which point libraryAvailable() is true so the plan resolves to
// faithful_docx and the surface actually engages. On load failure we do NOT
// re-invoke: the already-painted reconstruction/page-image floor stands (NEVER
// blank). A per-call sequence guard drops a stale re-upgrade if the matter/view
// changed while the libs were loading. Returns true when a load was kicked (so the
// caller knows the synchronous pass is intentionally a no-op pending the reload),
// false otherwise (flag off, already loaded, or no ensureLibs hook).
function ensureFaithfulLibsThenReupgrade(faithful, reupgrade) {
  if (!faithful || typeof faithful.enabled !== "function" || typeof faithful.ensureLibs !== "function") {
    return false;
  }
  if (!faithful.enabled()) return false; // flag off: nothing to load
  // Already loaded -> the plan would have engaged synchronously; no kick needed.
  if (typeof faithful.libraryAvailable === "function" && faithful.libraryAvailable()) return false;
  const sequence = reviewDocumentRenderRequestSequence;
  const matterId = state.selectedMatter?.id || null;
  // ensureLibs() is already async and returns a promise; call it directly so the
  // lazy <script> injection STARTS now (the kick is synchronous), then re-upgrade
  // when it resolves. Promise.resolve() wraps it so a synchronous throw is still
  // caught by .catch and never escapes as an unhandled error.
  Promise.resolve()
    .then(() => faithful.ensureLibs())
    .then(() => {
      // Drop a stale reload: the view re-rendered or the matter changed while the
      // vendored scripts were in flight. The fresh render path will retry on its own.
      if (sequence !== reviewDocumentRenderRequestSequence) return;
      if ((state.selectedMatter?.id || null) !== matterId) return;
      if (typeof reupgrade === "function") reupgrade();
    })
    .catch((error) => {
      // Lazy-load failed (404 / offline / parse): the painted floor stands. ensureLibs
      // resets its own promise cache on failure, so a later render can retry. Never blank.
      try {
        // eslint-disable-next-line no-console
        console.error("ensureFaithfulLibsThenReupgrade: faithful lib lazy-load failed; keeping painted surface", error);
      } catch (_loggingError) {
        // ignore logging failure
      }
    });
  return true;
}

// Feature-flagged faithful-DOCX upgrade of the freshly-painted Original surface.
// Delegates the source/precedence decision to the pure selectFaithfulRenderPlan();
// only a { render:"faithful_docx", url } plan does any work. Fetches the real DOCX
// bytes from the plan's owner-scoped URL and renders them with docx-preview. On
// ANY failure it leaves the already-painted existing surface untouched -- the pane
// is never blanked. A request sequence + matter-id recheck drops a stale async
// upgrade if the user has since changed view mode or matter.
function maybeUpgradeOriginalSurfaceToFaithfulDocx() {
  const faithful = (typeof window !== "undefined" && window.FaithfulDocxRender) || null;
  if (!faithful || typeof faithful.render !== "function") return;

  const plan = selectFaithfulRenderPlan(state.selectedMatter, state.reviewDocumentRender, {
    flagEnabled: typeof faithful.enabled === "function" ? faithful.enabled() : false,
    libraryAvailable: typeof faithful.libraryAvailable === "function" ? faithful.libraryAvailable() : true,
  });
  if (plan.render !== "faithful_docx") {
    // COLD START: the plan can be page_image purely because the vendored libs have
    // not lazy-loaded yet. Kick the lazy-load and re-run this upgrade once they
    // resolve (then the plan engages). If the load fails the painted surface stands.
    ensureFaithfulLibsThenReupgrade(faithful, maybeUpgradeOriginalSurfaceToFaithfulDocx);
    return; // page_image/reconstruction: keep the painted surface for now
  }

  const matterId = state.selectedMatter?.id;
  if (!matterId) return;
  const sequence = reviewDocumentRenderRequestSequence;
  const url = plan.url;

  // Render into a detached host first; only swap it into the live surface once we
  // know it produced real content, so a failed/empty faithful render can never
  // wipe the existing surface mid-flight.
  const host = document.createElement("div");
  host.className = "review-faithful-docx-surface";

  Promise.resolve(faithful.render(host, { url }))
    .then((result) => {
      // Drop a stale upgrade: the view re-rendered, the matter changed, or we left
      // the Original view while the bytes were in flight.
      if (sequence !== reviewDocumentRenderRequestSequence) return;
      if (state.selectedMatter?.id !== matterId) return;
      if ((state.documentViewMode || VIEW_MODE_REDLINE) !== VIEW_MODE_ORIGINAL) return;
      if (!studioDocumentRender) return;
      if (!result || !result.ok) return; // fall back: keep the existing surface
      const wrapper = document.createElement("section");
      wrapper.className = "review-original-surface review-faithful-original ready";
      wrapper.setAttribute("data-review-render-surface", "");
      wrapper.setAttribute("data-original-surface", "");
      wrapper.setAttribute("data-faithful-docx", "");
      wrapper.setAttribute("data-render-status", "ready");
      wrapper.setAttribute("aria-label", "Original document faithful preview");
      wrapper.appendChild(host);
      studioDocumentRender.innerHTML = "";
      studioDocumentRender.appendChild(wrapper);
      showStudioDocumentRender();
    })
    .catch((error) => {
      // Belt-and-braces: render() is contracted never to throw, but if it somehow
      // does we still keep the existing surface rather than blank the pane.
      try {
        // eslint-disable-next-line no-console
        console.error("maybeUpgradeOriginalSurfaceToFaithfulDocx: faithful upgrade failed; keeping existing surface", error);
      } catch (_loggingError) {
        // ignore logging failure
      }
    });
}

// === Phase 2: faithful redline/clean upgrade + interactive mapping ===========
//
// Feature-flagged faithful-DOCX upgrade of a NON-Original view (redline/clean).
// The reconstruction is already painted + bound (the never-blank floor). This
// fetches the REVIEWED .docx (tracked or accepted, per viewMode), renders it into
// a detached host, then MAPS the rendered .docx paragraphs 1:1 onto
// state.reviewParagraphs so every interaction (clause-click, comments, evidence
// highlights, text + formatting edits) works on the real document. If the mapping
// GUARD aborts -- or the render fails/empties, or a stale request returns -- the
// painted reconstruction is left untouched. A wrong mapping is far worse than no
// faithful render, so we abort to reconstruction rather than risk mis-attaching
// redlines/comments to the wrong clause.
function maybeUpgradeSurfaceToFaithfulDocx(viewMode) {
  const faithful = (typeof window !== "undefined" && window.FaithfulDocxRender) || null;
  if (!faithful || typeof faithful.render !== "function") return;

  // STALE-BYTES GUARD: the faithful redline/clean surface is fetched from
  // /reviewed-docx, whose bytes are composed from the PERSISTED reviewer_decisions /
  // manual edits (the last SAVED draft). If the user has unsaved in-session edits
  // (redlineDraftDirty), swapping those persisted bytes in OVER the live
  // reconstruction would HIDE the user's edit on screen while export still sends the
  // live state -- the user would see pre-edit but export post-edit. So while the
  // draft is dirty we keep the live reconstruction (the never-blank, fully-correct
  // floor) and do not fetch/swap the persisted faithful surface. It re-engages on the
  // next render after the draft is saved (redlineDraftDirty back to false). We also
  // skip the cold-start lazy-load kick here: there is nothing to upgrade TO yet.
  if (state.redlineDraftDirty) return;

  const plan = selectFaithfulRenderPlan(
    state.selectedMatter,
    state.reviewDocumentRender,
    {
      flagEnabled: typeof faithful.enabled === "function" ? faithful.enabled() : false,
      libraryAvailable: typeof faithful.libraryAvailable === "function" ? faithful.libraryAvailable() : true,
    },
    viewMode,
  );
  if (plan.render !== "faithful_docx") {
    // COLD START: same catch-22 as the Original path -- the plan can be
    // reconstruction/page_image only because the vendored libs are not lazy-loaded
    // yet. Kick the load and re-run THIS view's upgrade once they resolve. On load
    // failure the painted reconstruction floor stands (never blank).
    ensureFaithfulLibsThenReupgrade(faithful, () => maybeUpgradeSurfaceToFaithfulDocx(viewMode));
    return; // reconstruction/page_image: keep the painted floor for now
  }

  const matterId = state.selectedMatter?.id;
  if (!matterId) return;
  const sequence = reviewDocumentRenderRequestSequence;
  const url = plan.url;

  // Render into a detached host first; only swap it in once the bytes paint AND the
  // mapping guard commits, so a failed/empty/mis-mapped faithful render can never
  // wipe or corrupt the painted reconstruction mid-flight.
  const host = document.createElement("div");
  host.className = "review-faithful-docx-surface";

  // CLEAN view renders ACCEPTED text, not tracked-change markup. docx-preview's
  // renderChanges defaults ON in our faithfulDocxRenderOptions(), which would draw
  // <ins>/<del> even for the Clean view. The backend serves accepted bytes for
  // changes=accepted, but force renderChanges:false here as belt-and-suspenders so
  // Clean is clean regardless of what bytes arrive. Redline keeps the default (ON).
  const renderOptions = viewMode === VIEW_MODE_CLEAN ? { renderChanges: false } : undefined;

  Promise.resolve(faithful.render(host, { url }, renderOptions))
    .then((result) => {
      // Drop a stale upgrade: the view re-rendered, the matter changed, or we left
      // this view mode while the bytes were in flight.
      if (sequence !== reviewDocumentRenderRequestSequence) return;
      if (state.selectedMatter?.id !== matterId) return;
      if ((state.documentViewMode || VIEW_MODE_REDLINE) !== viewMode) return;
      if (!studioDocumentRender) return;
      if (!result || !result.ok) {
        // The reviewed-docx (tracked/accepted) bytes could not be obtained -- most
        // commonly a 409 (no approved/reviewed-redline artifact for this matter yet,
        // so the backend has nothing to compose) but also any 404 / parse / empty
        // render. Rather than dropping all the way to the PLAIN reconstruction, fall
        // back to the FAITHFUL Clean/Original surface so the reviewer still sees the
        // true document. Never blank: if even that yields no bytes, the reconstruction
        // floor stands.
        faithfulMappingTelemetry(`redline_bytes_unavailable:${result?.reason || "unknown"}`);
        attemptFaithfulRedlineFallback(faithful, viewMode, matterId, sequence);
        return;
      }

      // MAP the rendered .docx paragraphs onto the review model. Aborts (returns
      // false) on any guard failure, in which case we DON'T keep the plain
      // reconstruction -- we fall back to the faithful Clean/Original surface (the
      // overlay map is what is unsafe, not the faithful render itself).
      const mapped = bindFaithfulDocxInteractions(host, viewMode);
      if (!mapped) {
        attemptFaithfulRedlineFallback(faithful, viewMode, matterId, sequence);
        return; // guard aborted -> faithful Clean/Original (never the plain reconstruction)
      }

      const wrapper = document.createElement("section");
      wrapper.className = "review-faithful-surface review-faithful-redline ready";
      wrapper.setAttribute("data-review-render-surface", "");
      wrapper.setAttribute("data-faithful-docx", "");
      wrapper.setAttribute("data-faithful-view-mode", String(viewMode));
      wrapper.setAttribute("data-render-status", "ready");
      wrapper.setAttribute("aria-label", `Reviewed document faithful preview (${viewMode === VIEW_MODE_CLEAN ? "clean" : "redline"})`);
      wrapper.appendChild(host);
      studioDocumentRender.innerHTML = "";
      studioDocumentRender.appendChild(wrapper);
      showStudioDocumentRender();
      // The interaction binders are DOM-walkers scoped to studioDocumentRender, so
      // re-run the surface-level ones that paint onto the now-live faithful DOM.
      notifyFillHighlights();
      highlightSelectedClauseRefs();
    })
    .catch((error) => {
      // Belt-and-braces: render()/mapping are contracted never to throw, but if one
      // somehow does we still try the faithful Clean/Original fallback (and, failing
      // that, keep the reconstruction) rather than blank or corrupt the pane.
      faithfulMappingTelemetry("upgrade_threw");
      try {
        // eslint-disable-next-line no-console
        console.error("maybeUpgradeSurfaceToFaithfulDocx: faithful upgrade failed; trying faithful fallback then reconstruction", error);
      } catch (_loggingError) {
        // ignore logging failure
      }
      try {
        attemptFaithfulRedlineFallback(faithful, viewMode, matterId, sequence);
      } catch (_fallbackError) {
        // never let the fallback itself break the never-blank floor
      }
    });
}

// REDLINE/CLEAN -> FAITHFUL fallback. When the tracked/accepted reviewed-docx
// surface can't be obtained (409 no-artifact / 404 / parse / empty / mapping abort),
// we render the FAITHFUL document anyway -- read-only, no interactive redline
// overlay -- rather than dropping to the PLAIN reconstruction/page-image. The
// reviewer still sees the byte-faithful document (styles, tables, numbering); a small
// honest note explains that tracked redlines live on the Clean/Original tabs.
//
// WHY read-only / no overlay: layering tracked-change redlines on top of the faithful
// SOURCE surface is the known-unsafe path -- docx-preview surfaces tracked-DELETION
// text that our review model has resolved away, so a 1:1 source_index overlay drifts
// and could MIS-ATTACH redlines/comments. So the fallback NEVER runs
// bindFaithfulDocxInteractions; it paints a faithful read-only surface only.
//
// Candidate order (first that paints wins):
//   1. From REDLINE: the faithful CLEAN (accepted-changes) reviewed-docx -- if a
//      reviewed artifact DOES exist but only the tracked composition failed, accepted
//      bytes may still resolve. (Skipped when the failing view IS clean.)
//   2. The faithful ORIGINAL source document (/source for DOCX, /working-docx for a
//      converted PDF) -- always present for a native DOCX, so this is the reliable floor.
// If NONE paint (no DOCX bytes at all -- a true empty/scanned case), we do nothing and
// the already-painted reconstruction stands (never blank).
//
// `faithful` is the window.FaithfulDocxRender bridge; matterId + sequence are the
// staleness keys captured by the caller so a view/matter change mid-flight drops the swap.
function attemptFaithfulRedlineFallback(faithful, failedViewMode, matterId, sequence) {
  if (!faithful || typeof faithful.render !== "function") return;
  if (!studioDocumentRender) return;

  const matter = state.selectedMatter;
  const renderState = state.reviewDocumentRender;
  if (!matter || !matterId) return;
  const encodedId = encodeURIComponent(matterId);

  // Build the ordered candidate list. Each entry: { url, renderChanges, label }.
  const candidates = [];
  // (1) Faithful CLEAN (accepted) -- only when we failed on the REDLINE view.
  if (failedViewMode !== VIEW_MODE_CLEAN) {
    const cleanEligible = matterIsDocxSource(matter)
      || (matterIsPdfSource(matter) && renderState && renderState.workingDocxReady === true);
    if (cleanEligible) {
      candidates.push({
        url: `/api/matters/${encodedId}/reviewed-docx?changes=accepted`,
        renderChanges: false,
        label: "clean",
      });
    }
  }
  // (2) Faithful ORIGINAL source document -- the reliable floor for a native DOCX.
  if (matterIsDocxSource(matter)) {
    candidates.push({ url: `/api/matters/${encodedId}/source`, renderChanges: false, label: "original" });
  } else if (matterIsPdfSource(matter) && renderState && renderState.workingDocxReady === true) {
    candidates.push({ url: `/api/matters/${encodedId}/working-docx`, renderChanges: false, label: "original" });
  }

  if (!candidates.length) return; // no faithful bytes available -> reconstruction stands.

  // Try the candidates in order; the first that paints into a detached host wins.
  const tryCandidate = (index) => {
    if (index >= candidates.length) return; // exhausted -> reconstruction stands (never blank).
    // Staleness recheck before each attempt: the user may have moved on.
    if (sequence !== reviewDocumentRenderRequestSequence) return;
    if (state.selectedMatter?.id !== matterId) return;
    if ((state.documentViewMode || VIEW_MODE_REDLINE) !== failedViewMode) return;

    const candidate = candidates[index];
    const host = document.createElement("div");
    host.className = "review-faithful-docx-surface";

    Promise.resolve(faithful.render(host, { url: candidate.url }, { renderChanges: candidate.renderChanges }))
      .then((result) => {
        // Re-check staleness AFTER the async render resolves.
        if (sequence !== reviewDocumentRenderRequestSequence) return;
        if (state.selectedMatter?.id !== matterId) return;
        if ((state.documentViewMode || VIEW_MODE_REDLINE) !== failedViewMode) return;
        if (!studioDocumentRender) return;
        if (!result || !result.ok) {
          tryCandidate(index + 1); // this candidate yielded no bytes -> try the next.
          return;
        }
        // Painted. Swap in a READ-ONLY faithful surface (no interactive redline
        // overlay) with an honest note that tracked redlines live on the other tabs.
        const wrapper = document.createElement("section");
        wrapper.className = "review-faithful-surface review-faithful-redline review-faithful-redline-fallback ready";
        wrapper.setAttribute("data-review-render-surface", "");
        wrapper.setAttribute("data-faithful-docx", "");
        wrapper.setAttribute("data-faithful-view-mode", String(failedViewMode));
        wrapper.setAttribute("data-faithful-fallback", candidate.label);
        wrapper.setAttribute("data-render-status", "ready");
        wrapper.setAttribute("aria-label", `Faithful document preview (tracked redlines unavailable; showing ${candidate.label})`);
        wrapper.appendChild(faithfulRedlineFallbackNote(candidate.label));
        wrapper.appendChild(host);
        studioDocumentRender.innerHTML = "";
        studioDocumentRender.appendChild(wrapper);
        showStudioDocumentRender();
        notifyFillHighlights();
        highlightSelectedClauseRefs();
        faithfulMappingTelemetry(`redline_faithful_fallback:${candidate.label}`);
      })
      .catch(() => {
        // render() is contracted never to throw; if it somehow does, try the next
        // candidate, and failing all of them the reconstruction floor stands.
        tryCandidate(index + 1);
      });
  };
  tryCandidate(0);
}

// Small, honest in-surface note for the faithful redline fallback: tracked redlines
// could not be composed for this matter, so the true document is shown (clean /
// original) instead -- the reviewer can see the real formatting, and the structured
// redline view + the Clean/Original tabs carry the change detail.
function faithfulRedlineFallbackNote(label) {
  const note = document.createElement("div");
  note.className = "review-faithful-fallback-note";
  note.setAttribute("role", "note");
  const showing = label === "clean" ? "the accepted (clean) document" : "the original document";
  note.textContent = `Tracked redlines aren't available to render on this tab yet, so ${showing} is shown faithfully here. Switch to the structured Redline view for the change-by-change detail.`;
  return note;
}

// Emits an abort/diagnostic reason for the faithful mapping. Console only today
// (no telemetry sink wired); kept centralised so a sink can be added in one place.
function faithfulMappingTelemetry(reason) {
  try {
    // eslint-disable-next-line no-console
    console.warn(`faithful_mapping_aborted: ${reason}`);
  } catch (_loggingError) {
    // never let logging break the fallback
  }
}

// Normalize a text fragment for the mapping checksum: collapse all whitespace
// (so a rendered <br> -> "\n" and a structured "\n" -> " " compare equal) and
// lower-case. Shared by both sides of the guard so the comparison is symmetric.
function faithfulNormalizeText(value) {
  return String(value == null ? "" : value).replace(/\s+/g, " ").trim().toLowerCase();
}

// True when EVERY whitespace-token of `small` appears, in order, as a subsequence
// of `big`'s tokens. This is the PROVEN-SAFE allowance from /tmp/drift/
// final_guard.mjs: a legit inline tracked-INSERT makes the rendered text a
// superset of the structured text (structured ⊑ rendered), and the rare
// rendered-subset edge (rendered ⊑ structured) is also accepted. It is
// deliberately NOT a substring/prefix check: an adversarial test proved a naive
// prefix-checksum SILENTLY mis-attaches redlines on NDA boilerplate, so we require
// an ORDERED TOKEN subsequence over the whole text, not a leading match.
function faithfulIsTokenSubsequence(small, big) {
  const a = faithfulNormalizeText(small).split(" ").filter(Boolean);
  const b = faithfulNormalizeText(big).split(" ").filter(Boolean);
  let j = 0;
  for (const token of b) {
    if (j < a.length && token === a[j]) j += 1;
  }
  return j === a.length && a.length > 0;
}

// THE GUARD (ports /tmp/drift/final_guard.mjs exactly):
//   (1) COUNT exact, tolerance 0: N(rendered) !== N(structured) -> ABORT.
//   (2) Per-pair CHECKSUM with ordered token-subsequence allowance: a pair matches
//       iff norm-equal OR structured ⊑ rendered (token subseq) OR rendered ⊑
//       structured. ABORT on the FIRST non-matching pair.
// `rendered` is an array of strings (already header/footer-excluded by the caller);
// `structured` is the ordered state.reviewParagraphs slice. Returns true to COMMIT,
// false to ABORT (and emits a telemetry reason on abort).
function faithfulMappingGuardPasses(rendered, structured) {
  if (!Array.isArray(rendered) || !Array.isArray(structured)) {
    faithfulMappingTelemetry("guard_bad_input");
    return false;
  }
  if (rendered.length !== structured.length) {
    faithfulMappingTelemetry(`count_mismatch rendered=${rendered.length} structured=${structured.length}`);
    return false;
  }
  for (let i = 0; i < rendered.length; i += 1) {
    const r = rendered[i];
    const s = String(structured[i]?.text || "");
    if (faithfulNormalizeText(r) === faithfulNormalizeText(s)) continue;
    if (faithfulIsTokenSubsequence(s, r)) continue; // legit inline tracked-insert
    if (faithfulIsTokenSubsequence(r, s)) continue; // legit (rare) rendered-subset
    faithfulMappingTelemetry(`checksum_drift index=${i}`);
    return false;
  }
  return true;
}

// The faithful paragraph elements to map, in TREE order, EXCLUDING header/footer
// (docx-preview renders these as <header>/<footer>; their text would never appear
// in the structured review paragraphs). Includes table cell paragraphs (`td p`)
// because querySelectorAll(".docx p") already returns them in document order.
function faithfulMappableParagraphs(container) {
  if (!container || typeof container.querySelectorAll !== "function") return [];
  return Array.from(container.querySelectorAll(".docx p")).filter((el) => {
    // Exclude any paragraph inside a rendered header/footer.
    if (typeof el.closest === "function" && el.closest("header,footer")) return false;
    return true;
  });
}

// Reads a faithful paragraph's text for the GUARD's checksum. Uses the shared
// normalizer so a soft line break (<br>) contributes a "\n" (which the guard's
// whitespace-collapse then treats as a space) -- matching how the structured side's
// "\n" normalizes. Without this, docx-preview's <br> (which yields NO textContent
// char) would glue two words together ("Definitions"+"As") and abort the whole-doc
// mapping over a break that is really just whitespace. The per-paragraph read-back
// assert still catches the cases (tabs) that genuinely cannot round-trip.
function faithfulParagraphText(el) {
  return faithfulEditableTextContent(el);
}

// SHARED faithful-text normalizer: produces the text the way docx_text extraction +
// editableParagraphText (viewer.js) both see it, so capturedRunsFromFaithfulEditable
// and paragraph.text are compared on the SAME footing. The silent trap the
// adversarial pass surfaced: raw textContent keeps NBSP and DROPS <br>/<tab>, while
// the backend emits "\n" for w:br/w:cr and "\t" for w:tab and the FE editor maps
// NBSP->space. We mirror that exactly: <br>/<cr>-rendered breaks -> "\n", rendered
// tab spans -> "\t", NBSP -> space. Used for the re-tile invariant + the read-back
// assert (which then ABORTS to reconstruction on any residual mismatch).
function faithfulEditableTextContent(el) {
  if (!el) return "";
  let text = "";
  const ownerDoc = el.ownerDocument || (typeof document !== "undefined" ? document : null);
  const walk = (node) => {
    if (!node) return;
    // Element fast-paths for the structural characters docx_text emits.
    if (node.nodeType === 1) {
      const tag = String(node.tagName || "").toUpperCase();
      // Skip the contenteditable=false chrome (comment tools / verdict badge / note).
      if (node.classList && (node.classList.contains("paragraph-comment-tools")
        || node.classList.contains("paragraph-verdict-badge")
        || node.classList.contains("faithful-edit-locked-note"))) {
        return;
      }
      if (tag === "BR") { text += "\n"; return; }
      // docx-preview renders a w:tab as a span with a tab class / tab character.
      if (node.classList && (node.classList.contains("docx-tab") || node.classList.contains("tab"))) {
        text += "\t";
        return;
      }
    }
    if (node.nodeType === 3) {
      text += String(node.textContent || "");
      return;
    }
    const children = node.childNodes || [];
    for (let i = 0; i < children.length; i += 1) walk(children[i]);
  };
  walk(el);
  if (ownerDoc) { /* keep ownerDoc reference for symmetry; not otherwise needed */ }
  // Mirror editableParagraphText's NBSP->space + CRLF + collapse of 3+ newlines.
  return text
    .replace(/ /g, " ")
    .replace(/\r\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n");
}

// Detect a faithful paragraph that CANNOT be safely round-tripped and must be
// EDIT-LOCKED (still mapped read-only). Returns a short reason string, or "" when
// the paragraph is safe to edit. The four locked classes (all proven by the
// adversarial round-trip harness against the vendored docx-preview + the backend
// export/extract code):
//   1. tracked_changes  -- <ins>/<del> descendants. docx-preview shows ins+del;
//      the backend model text (docx_text._collect_revision_aware_text) DROPS
//      insertions and RESTORES deletions -- the OPPOSITE resolution -- so offsets
//      drift and formatting lands on the wrong characters.
//   2. table_cell       -- the paragraph is inside a rendered <table>. A
//      whole-paragraph replace inside a cell is structurally risky; the extractor
//      keeps cells in the model (so they ARE mapped), but editing routes to the
//      reconstruction.
//   3. nontext_inline   -- <a> (hyperlink), a rendered field, a drawing/picture, or
//      a footnote/endnote marker. The backend _paragraph_has_nontext_inline_content
//      RAISES on these for replace/delete, so we must lock BEFORE export, not 500.
//   4. block_split      -- the model split one physical <w:p> into >1 paragraph
//      (they share a source_index). One faithful <p> <-> two model ids: an edit
//      can't be attributed.
function faithfulParagraphEditLockReason(el, paragraph, sourceIndexCounts) {
  if (!el || typeof el.querySelector !== "function") return "no_element";
  // 1. Tracked changes.
  if (el.querySelector("ins, del")) return "tracked_changes";
  // 2. Table cell.
  if (typeof el.closest === "function" && el.closest("table")) return "table_cell";
  // 3. Non-text inline content (mirror the backend export guard's risky set).
  if (el.querySelector("a[href], a[data-field], sup a, .docx-field, [data-footnote-ref], [data-endnote-ref], img, svg, object, .docx-drawing")) {
    return "nontext_inline";
  }
  // 4. Block split: this model paragraph shares its source_index with another.
  const si = paragraph && paragraph.source_index;
  if (si !== undefined && si !== null) {
    const count = sourceIndexCounts && typeof sourceIndexCounts.get === "function"
      ? (sourceIndexCounts.get(String(si)) || 0)
      : 0;
    if (count > 1) return "block_split";
  }
  return "";
}

// T5b: walk the rendered .docx paragraphs in TREE order, GUARD them 1:1 against
// state.reviewParagraphs (ordered by source_index), and -- only if the guard
// commits -- stamp the SAME hooks the reconstruction frame uses
// (studio-doc-paragraph + data-paragraph-id + data-clause-ids + comment tools)
// and run the existing DOM-walking binders so clause-click / comments / evidence /
// fill / clause-ref highlights all reattach for free. Then (T5d) make each mapped
// paragraph RICH-editable and bind the text + formatting editors.
//
// Returns true on COMMIT (the caller swaps in the faithful surface), false on
// ABORT (the caller keeps the reconstruction). NEVER mutates the live DOM on abort.
function bindFaithfulDocxInteractions(container, viewMode) {
  const rendered = faithfulMappableParagraphs(container);
  // Order the review model by source_index (its document order). Paragraphs that
  // split a source block share a source_index, so keep a STABLE sort to preserve
  // their original relative order (Array.prototype.sort is stable in modern JS).
  const structured = (Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs.slice() : [])
    .sort((a, b) => {
      const ai = Number(a?.source_index);
      const bi = Number(b?.source_index);
      const an = Number.isFinite(ai) ? ai : Number.MAX_SAFE_INTEGER;
      const bn = Number.isFinite(bi) ? bi : Number.MAX_SAFE_INTEGER;
      return an - bn;
    });

  // (1)+(2) THE GUARD. Abort to reconstruction on any failure.
  if (!faithfulMappingGuardPasses(rendered.map(faithfulParagraphText), structured)) {
    return false;
  }

  // Build the clause-id map exactly as the reconstruction does: a paragraph's
  // data-clause-ids = the ids of clauses whose matched_paragraph_ids include it
  // (merged with any redline clause), suppressed for a document-title paragraph.
  const clauseIdsByParagraphId = faithfulClauseIdsByParagraphId();
  const comments = currentReviewComments();

  // BLOCK-SPLIT detection (per the adversarial round-trip findings): the extractor
  // can split ONE physical <w:p> into >1 model paragraph (they share a source_index).
  // A single faithful <p> then maps to >1 model id, so an edit can't be attributed
  // -- those paragraphs are EDIT-LOCKED (mapped read-only). Count how many model
  // paragraphs share each source_index.
  const sourceIndexCounts = new Map();
  structured.forEach((paragraph) => {
    const si = paragraph?.source_index;
    if (si === undefined || si === null) return;
    const key = String(si);
    sourceIndexCounts.set(key, (sourceIndexCounts.get(key) || 0) + 1);
  });

  const ownerDoc = container.ownerDocument || (typeof document !== "undefined" ? document : null);

  rendered.forEach((el, index) => {
    const paragraph = structured[index];
    const paragraphId = String(paragraph?.id || "");
    // The .docx <p> becomes the FRAME (mirrors renderStudioParagraphFrame's
    // studio-doc-paragraph): data-paragraph-id + clause ids + comment tools live on
    // it. The run content is moved into an INNER editable wrapper so that, exactly
    // like the reconstruction, the editable holds ONLY the rich text -- the comment
    // tools/badge are SIBLINGS, never inside the editable. (If they were inside,
    // syncViewerParagraphEdit's innerText read would fold the comment count/icon
    // text into paragraph.text and corrupt it.)
    el.classList.add("studio-doc-paragraph");
    if (paragraphId) el.setAttribute("data-paragraph-id", paragraphId);
    const clauseIds = clauseIdsByParagraphId.get(paragraphId) || "";
    if (clauseIds) {
      el.setAttribute("data-clause-ids", clauseIds);
    } else {
      el.removeAttribute("data-clause-ids");
    }
    const commentCount = paragraphCommentCount(paragraphId, comments);
    if (commentCount) el.classList.add("has-comments");

    // Move the existing run children into an inner editable wrapper.
    const lockReason = paragraphId
      ? faithfulParagraphEditLockReason(el, paragraph, sourceIndexCounts)
      : "no_paragraph_id";
    const editable = ownerDoc ? ownerDoc.createElement("div") : null;
    if (editable) {
      editable.className = "paragraph-editable faithful-paragraph-editable";
      while (el.firstChild) editable.appendChild(el.firstChild);
      el.appendChild(editable);

      // Comment tools at the TOP of the frame (sibling of the editable), matching
      // renderStudioParagraphFrame. contenteditable=false so they stay out of text.
      if (paragraphId && typeof renderParagraphCommentTools === "function") {
        const tools = renderParagraphCommentTools(paragraphId, commentCount).trim();
        if (tools) el.insertAdjacentHTML("afterbegin", tools);
      }

      // (T5d) RICH-editable -- but ONLY for the paragraph classes that round-trip
      // CLEANLY. The adversarial round-trip pass proved FOUR classes cannot be made
      // safe by read-back normalization (tracked changes resolve OPPOSITE to the
      // model; table cells / non-text inline trip the backend export guard; block
      // splits can't be attributed). For those we MAP read-only (clause-ids /
      // comments / highlights all still work) and route EDITING to the
      // reconstruction view, one toggle away -- never risk a silent corruption the
      // backend's text-only gate would pass. Normal prose (bold/italic/font/color/
      // size/alignment over plain runs) stays fully editable.
      if (paragraphId && !lockReason) {
        editable.setAttribute("data-editable-paragraph-id", paragraphId);
        editable.setAttribute("contenteditable", "true");
        editable.setAttribute("spellcheck", "true");
        editable.setAttribute("role", "textbox");
        editable.setAttribute("aria-multiline", "true");
        editable.setAttribute("data-faithful-editable", "");
      } else {
        editable.setAttribute("contenteditable", "false");
        el.classList.add("faithful-edit-locked");
        if (lockReason) el.setAttribute("data-faithful-lock-reason", lockReason);
      }
    }
  });

  // Now run the SAME binders the reconstruction path uses. They are DOM-walkers
  // scoped to studioDocumentRender / the container, so they reattach for free.
  // Clause-click selection (mirrors the non-Original branch's own binding).
  container.querySelectorAll("[data-clause-ids]").forEach((paragraph) => {
    paragraph.addEventListener("click", (event) => {
      if (event.target.closest("[data-editable-paragraph-id]")
        && event.target.closest("[data-editable-paragraph-id]") !== paragraph) return;
      const clauseId = String(paragraph.dataset.clauseIds || "").split(" ").filter(Boolean)[0];
      if (clauseId) selectReviewClause(clauseId, { jump: false });
    });
  });

  // Comment controls bind on the container itself (works while still detached).
  bindParagraphCommentControls(container);

  // The viewer text-editor + format toolbar + comment-text highlights walk
  // studioDocumentRender to find their nodes; at THIS point the host is still
  // DETACHED (the caller swaps it in only after we return true). Defer those to a
  // microtask so they run against the LIVE surface. Guarded so a stale view never
  // binds.
  bindFaithfulDocxEditorsWhenLive(container);
  return true;
}

// Deferred (post-swap) bind of the studioDocumentRender-scoped binders for the
// faithful surface: the text editor, the format toolbar, and the comment-text
// highlights. They are no-ops until the host is live inside studioDocumentRender.
function bindFaithfulDocxEditorsWhenLive(container) {
  Promise.resolve().then(() => {
    if (!studioDocumentRender || !studioDocumentRender.contains(container)) return;
    if (typeof bindViewerParagraphEditing === "function") bindViewerParagraphEditing();
    if (typeof bindFormatToolbar === "function") bindFormatToolbar();
    if (typeof applyCommentTextHighlights === "function") applyCommentTextHighlights();
  }).catch(() => { /* binders never throw; ignore */ });
}

// Reproduces the reconstruction's per-paragraph clause-id derivation
// (renderReviewDocument + paragraphViewModel.ids) so the faithful map stamps the
// IDENTICAL data-clause-ids -- the anti-mis-attach contract. A document-title
// paragraph carries no clause linkage (it is the doc name, never clause content).
function faithfulClauseIdsByParagraphId() {
  const byParagraph = new Map();
  const clauses = Array.isArray(state.reviewClauses) ? state.reviewClauses : [];
  clauses.forEach((clause) => {
    (clause.matched_paragraph_ids || []).forEach((paragraphId) => {
      const key = String(paragraphId || "");
      if (!key) return;
      if (!byParagraph.has(key)) byParagraph.set(key, []);
      byParagraph.get(key).push(clause.id);
    });
  });
  // Suppress a document-title paragraph's clause linkage, matching the
  // reconstruction (paragraphViewModel: title -> []).
  const result = new Map();
  (Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : []).forEach((paragraph) => {
    const key = String(paragraph?.id || "");
    if (!key) return;
    if (typeof paragraphIsDocumentTitle === "function" && paragraphIsDocumentTitle(paragraph)) {
      return; // no linkage
    }
    const ids = byParagraph.get(key);
    if (ids && ids.length) result.set(key, ids.join(" "));
  });
  return result;
}

// True when a faithful (mapped, editable) surface is currently LIVE in the
// document pane. Used to keep model edits IN PLACE on the faithful DOM rather than
// re-fetching the server DOCX (which would not reflect in-session edits) and to
// re-render a single paragraph's runs from the model after a format edit.
function faithfulSurfaceIsLive() {
  return Boolean(
    studioDocumentRender
    && typeof studioDocumentRender.querySelector === "function"
    && studioDocumentRender.querySelector("[data-faithful-docx] [data-faithful-editable]"),
  );
}

// (T5d) Re-render ONE faithful paragraph's run spans FROM THE MODEL, so a
// formatting edit (the toolbar mutates paragraph.runs by offset) is reflected on
// the faithful DOM -- the model is the single source of truth. We reuse the EXACT
// reconstruction run renderer (renderParagraphRichText) so the run->span mapping is
// identical to the reconstruction editor. The paragraph's comment tools (the
// contenteditable=false toolbar at the top of the frame) are preserved.
//
// Before writing, we assert the model's runs re-tile to the model's text
// (runs.map(r=>r.text).join("")===paragraph.text). renderParagraphRichText already
// falls back to flat text on drift, so a drifted run set degrades to plain text
// here rather than corrupting; either way the editable's textContent stays equal to
// paragraph.text, preserving the text round-trip.
function renderFaithfulParagraphRunsFromModel(paragraphId) {
  if (!faithfulSurfaceIsLive()) return false;
  const id = String(paragraphId || "");
  if (!id) return false;
  const editable = studioDocumentRender.querySelector(
    `[data-faithful-editable][data-editable-paragraph-id="${cssEscape(id)}"]`,
  );
  if (!editable) return false;
  const paragraph = (Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [])
    .find((item) => String(item.id) === id);
  if (!paragraph) return false;
  if (typeof renderParagraphRichText !== "function") return false;

  // The editable holds ONLY the run content (the comment tools/badge are siblings
  // on the frame), so replacing its innerHTML with the freshly rendered runs is a
  // clean swap. renderParagraphRichText falls back to flat text on any run drift,
  // so textContent stays equal to paragraph.text either way.
  editable.innerHTML = renderParagraphRichText(paragraph);
  return true;
}

// === (T5d) Faithful-DOM <-> run-model bridge ================================
//
// The faithful surface shows the SERVER document's styled runs (docx-preview emits
// <strong>/<em>/<u>/<span style="font-...">). The format toolbar, however, edits
// state.reviewParagraphs[id].runs by character offset. If the model's runs are a
// flat/extracted approximation, formatting a selection and then re-rendering from
// the model would DROP the source run formatting the user can see. So BEFORE the
// first format edit on a faithful paragraph we READ the faithful DOM's styled spans
// back into the run array, seeding the model from what is on screen.
//
// CRITICAL SAFETY (per spec): re-tile to the invariant
// runs.map(r=>r.text).join("") === paragraph.text and assert captured text ===
// editable textContent BEFORE assigning. If the re-tile / assert FAILS, ABORT this
// paragraph's faithful editing -- route it to the reconstruction editor with a
// visible notice -- rather than silently corrupt the runs.

// Read the faithful editable's inline runs into the [{text,bold,...}] model shape,
// recursively walking the DOM and accumulating the active inline styles from the
// ancestor chain. Returns { runs, text } where `text` is the concatenated run text.
//
// To keep the invariant runs.join() === paragraph.text HONEST, this uses the SAME
// structural-character handling as faithfulEditableTextContent / docx_text
// extraction: a <br>/<cr> contributes "\n", a rendered tab span contributes "\t",
// NBSP normalizes to a space. The contenteditable=false chrome (comment tools /
// verdict badge / locked note) is skipped. Because both the captured run text and
// the assert text take this SAME path, a residual mismatch is a REAL drift (caught
// by the assert -> abort), never a normalization artefact.
function capturedRunsFromFaithfulEditable(editable) {
  if (!editable) return { runs: [], text: "" };
  const runs = [];
  let text = "";
  const isChrome = (node) => node && node.classList && (
    node.classList.contains("paragraph-comment-tools")
    || node.classList.contains("paragraph-verdict-badge")
    || node.classList.contains("faithful-edit-locked-note")
  );
  const pushChunk = (chunk, styleNode) => {
    if (!chunk) return;
    const normalized = chunk.replace(/ /g, " "); // NBSP -> space (shared rule)
    const styled = styleNode ? inlineStyleFromAncestors(styleNode, editable) : {};
    text += normalized;
    runs.push({ text: normalized, ...styled });
  };
  const walk = (node) => {
    if (!node) return;
    if (node.nodeType === 3) { // text node
      pushChunk(String(node.textContent || ""), node);
      return;
    }
    if (node.nodeType !== 1) return; // comments etc.
    if (isChrome(node)) return;
    const tag = String(node.tagName || "").toUpperCase();
    if (tag === "BR") { pushChunk("\n", node); return; }
    if (node.classList && (node.classList.contains("docx-tab") || node.classList.contains("tab"))) {
      pushChunk("\t", node);
      return;
    }
    const children = node.childNodes || [];
    for (let i = 0; i < children.length; i += 1) walk(children[i]);
  };
  walk(editable);
  return { runs, text };
}

// Walks a text node's ancestor chain (up to, not including, `editable`) and reads
// the inline formatting docx-preview applied: bold (<strong>/<b>/font-weight),
// italic (<em>/<i>/font-style), underline (<u>/text-decoration), strike, the
// font-family / font-size / color / background-color from inline styles. Returns
// only the keys that are set, matching normalizeRun's tidy shape.
function inlineStyleFromAncestors(node, editable) {
  let bold = false;
  let italic = false;
  let underline = false;
  let strike = false;
  let font = "";
  let size = 0;
  let color = "";
  let highlight = "";
  let vertAlign = "";
  let el = node.parentElement;
  while (el && el !== editable) {
    const tag = String(el.tagName || "").toUpperCase();
    if (tag === "STRONG" || tag === "B") bold = true;
    if (tag === "EM" || tag === "I") italic = true;
    if (tag === "U") underline = true;
    if (tag === "S" || tag === "STRIKE" || tag === "DEL") strike = true;
    if (tag === "SUP" && !vertAlign) vertAlign = "superscript";
    if (tag === "SUB" && !vertAlign) vertAlign = "subscript";
    const style = el.style || {};
    const weight = String(style.fontWeight || "").trim().toLowerCase();
    if (weight === "bold" || Number(weight) >= 600) bold = true;
    const fStyle = String(style.fontStyle || "").trim().toLowerCase();
    if (fStyle === "italic" || fStyle === "oblique") italic = true;
    const decoration = String(style.textDecoration || style.textDecorationLine || "").toLowerCase();
    if (decoration.includes("underline")) underline = true;
    if (decoration.includes("line-through")) strike = true;
    if (!font) {
      const family = String(style.fontFamily || "").trim();
      if (family) font = fontNameFromCssFamily(family);
    }
    if (!size) {
      const px = parseFloat(String(style.fontSize || ""));
      if (Number.isFinite(px) && px > 0 && /pt$/i.test(String(style.fontSize || ""))) size = Math.round(px);
    }
    if (!color) {
      const c = cssColorToHex(style.color);
      if (c) color = c;
    }
    if (!highlight) {
      const bg = cssColorToHex(style.backgroundColor);
      if (bg) highlight = bg;
    }
    el = el.parentElement;
  }
  const out = {};
  if (bold) out.bold = true;
  if (italic) out.italic = true;
  if (underline) out.underline = true;
  if (strike) out.strike = true;
  if (font) out.font = font;
  if (size) out.size = size;
  if (color) out.color = color;
  if (highlight) out.highlight = highlight;
  if (vertAlign) out.vertAlign = vertAlign;
  return out;
}

// Reduce a CSS font-family stack (e.g. '"Times New Roman", serif') to the Word
// font NAME the run model stores (the first family, unquoted).
function fontNameFromCssFamily(family) {
  const first = String(family || "").split(",")[0].trim().replace(/^["']|["']$/g, "");
  return first;
}

// Best-effort CSS color -> RRGGBB (no #) for the run model. Handles #rgb / #rrggbb
// and rgb()/rgba(); anything else -> "" (no override). Never throws.
function cssColorToHex(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const hex3 = raw.match(/^#([0-9a-fA-F]{3})$/);
  if (hex3) {
    const h = hex3[1];
    return (h[0] + h[0] + h[1] + h[1] + h[2] + h[2]).toUpperCase();
  }
  const hex6 = raw.match(/^#([0-9a-fA-F]{6})$/);
  if (hex6) return hex6[1].toUpperCase();
  const rgb = raw.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (rgb) {
    const toHex = (n) => Math.max(0, Math.min(255, Number(n))).toString(16).padStart(2, "0");
    return (toHex(rgb[1]) + toHex(rgb[2]) + toHex(rgb[3])).toUpperCase();
  }
  return "";
}

// Seed a faithful paragraph's model runs from the FAITHFUL DOM (read-back), with
// the re-tile invariant + assert. Called once, lazily, before the first format
// edit on a faithful paragraph (the model may have no/flat runs). Returns true if
// the model now carries DOM-faithful runs that re-tile to paragraph.text; false to
// signal the caller MUST abort faithful editing of this paragraph (route it to the
// reconstruction editor) rather than risk corrupting runs.
function seedFaithfulParagraphRunsFromDom(paragraphId) {
  if (!faithfulSurfaceIsLive()) return false;
  const id = String(paragraphId || "");
  const editable = studioDocumentRender.querySelector(
    `[data-faithful-editable][data-editable-paragraph-id="${cssEscape(id)}"]`,
  );
  if (!editable) return false;
  const paragraph = (Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [])
    .find((item) => String(item.id) === id);
  if (!paragraph) return false;
  // Already carries valid runs that tile the text? Nothing to seed.
  const existing = Array.isArray(paragraph.runs) ? paragraph.runs : null;
  if (existing && existing.length
    && existing.map((run) => String(run?.text || "")).join("") === String(paragraph.text || "")) {
    return true;
  }

  const captured = capturedRunsFromFaithfulEditable(editable);
  // ASSERT 1 (BYTE-exact): the captured run text must equal the editable's own text
  // read through the SAME shared normalizer. Both paths emit <br>->"\n", tab->"\t",
  // NBSP->space, so a residual mismatch is a REAL read-back drift (a node the run
  // walker handled differently than the text walker), not a normalization artefact.
  const editableText = editableParagraphTextSafe(editable);
  const modelText = String(paragraph.text || "");
  const capturedJoined = captured.runs.map((run) => String(run?.text || "")).join("");
  // ASSERT 2: the captured text re-tiles to the MODEL text (the export oracle). This
  // is compared on the byte-exact shared normalization too; the trailing-trim that
  // editableParagraphText applies is the only allowed difference, so we trim both.
  const captureMatchesEditable = captured.text === editableText;
  const tilesToModel = faithfulTrimEditableText(capturedJoined) === faithfulTrimEditableText(modelText);
  if (!captureMatchesEditable || !tilesToModel) {
    // ABORT this paragraph's faithful editing -> reconstruction editor + notice.
    // (Catches the tab/break/NBSP + boundary-drift cases the backend's text-only
    // gate would NOT catch -- this FE assert is the only defense against a correct-
    // text / wrong-formatting-boundary read-back.)
    faithfulMappingTelemetry(`readback_retile_failed paragraph=${id}`);
    abortFaithfulParagraphToReconstruction(id);
    return false;
  }
  // The captured run text may differ from the model text only by collapsed
  // whitespace; rebuild the runs onto the EXACT model text so the stored invariant
  // (byte-equal join) holds, distributing formatting by character offset.
  paragraph.runs = retileCapturedRunsOntoText(captured.runs, modelText);
  // Final guard: if the rebuild did not byte-tile, drop runs (degrade to plain
  // text) rather than store a drifted set.
  if (paragraph.runs.map((run) => String(run?.text || "")).join("") !== modelText) {
    delete paragraph.runs;
    faithfulMappingTelemetry(`readback_byte_tile_failed paragraph=${id}`);
    abortFaithfulParagraphToReconstruction(id);
    return false;
  }
  return true;
}

// Re-tile captured runs (whose concatenated text may differ from `text` only by
// whitespace normalization) onto the EXACT `text`, preserving the per-character
// formatting in order. Builds a char->format array from the captured runs (mapping
// only non-whitespace chars 1:1 and letting whitespace inherit its neighbour), then
// emits contiguous runs over `text`. Falls back to a single plain run if lengths
// can't be reconciled.
function retileCapturedRunsOntoText(capturedRuns, text) {
  const target = String(text || "");
  // Build a flat list of {char, fmt} for the captured non-space chars, in order.
  const capturedChars = [];
  (Array.isArray(capturedRuns) ? capturedRuns : []).forEach((run) => {
    const fmt = {};
    ["bold", "italic", "underline", "strike", "font", "size", "color", "highlight", "vertAlign"].forEach((key) => {
      if (run[key]) fmt[key] = run[key];
    });
    String(run.text || "").split("").forEach((ch) => {
      if (!/\s/.test(ch)) capturedChars.push({ ch, fmt });
    });
  });
  const targetNonSpace = target.split("").filter((ch) => !/\s/.test(ch));
  // If the non-space character streams don't line up, we cannot safely attribute
  // formatting -> single plain run (caller's byte-tile guard then accepts it).
  if (capturedChars.length !== targetNonSpace.length) {
    return [{ text: target }];
  }
  // Walk the target, pulling the next captured format for each non-space char and
  // reusing the last format across whitespace, then coalesce equal-format runs.
  const runs = [];
  let capturedIndex = 0;
  let lastFmt = {};
  for (const ch of target) {
    let fmt;
    if (/\s/.test(ch)) {
      fmt = lastFmt;
    } else {
      fmt = capturedChars[capturedIndex] ? capturedChars[capturedIndex].fmt : {};
      lastFmt = fmt;
      capturedIndex += 1;
    }
    const last = runs[runs.length - 1];
    if (last && faithfulRunFmtEqual(last, fmt)) {
      last.text += ch;
    } else {
      runs.push({ text: ch, ...fmt });
    }
  }
  return runs.length ? runs : [{ text: target }];
}

function faithfulRunFmtEqual(run, fmt) {
  const keys = ["bold", "italic", "underline", "strike", "font", "size", "color", "highlight", "vertAlign"];
  return keys.every((key) => String(run[key] || "") === String(fmt[key] || ""));
}

// Safe text of the editable EXCLUDING the comment-tools / verdict badge, read
// through the SHARED normalizer so it is byte-comparable with the captured run
// text (both emit <br>->"\n", tab->"\t", NBSP->space, skip the chrome). This is
// the read-back fidelity assert's reference text.
function editableParagraphTextSafe(editable) {
  return faithfulEditableTextContent(editable);
}

// editableParagraphText (viewer.js) trims the final text; the model paragraph.text
// is therefore trimmed. The captured/joined run text is NOT pre-trimmed, so the
// re-tile comparison trims both sides exactly as the viewer does.
function faithfulTrimEditableText(value) {
  return String(value == null ? "" : value).trim();
}

// ABORT a single faithful paragraph's editing: make it non-editable on the
// faithful surface and surface a visible notice telling the reviewer to use the
// reconstruction view for this paragraph. The OTHER faithful paragraphs keep
// working; only this one is locked. The model is untouched (no corruption).
function abortFaithfulParagraphToReconstruction(paragraphId) {
  const id = String(paragraphId || "");
  if (!faithfulSurfaceIsLive()) return;
  const editable = studioDocumentRender.querySelector(
    `[data-faithful-editable][data-editable-paragraph-id="${cssEscape(id)}"]`,
  );
  if (!editable) return;
  editable.setAttribute("contenteditable", "false");
  editable.removeAttribute("data-faithful-editable");
  // Lock the FRAME (the studio-doc-paragraph), and put the notice on the frame as a
  // sibling of the editable so it is never read into paragraph.text.
  const frame = typeof editable.closest === "function"
    ? (editable.closest(".studio-doc-paragraph") || editable)
    : editable;
  frame.classList.add("faithful-edit-locked");
  frame.setAttribute("data-faithful-lock-reason", "readback_failed");
  if (!frame.querySelector(".faithful-edit-locked-note")) {
    const note = document.createElement("div");
    note.className = "faithful-edit-locked-note";
    note.setAttribute("contenteditable", "false");
    note.setAttribute("role", "note");
    note.textContent = "This paragraph can't be edited on the faithful preview. Switch to the reconstruction view to edit it.";
    frame.appendChild(note);
  }
  try {
    setFileMeta("A paragraph was locked on the faithful preview; edit it in the reconstruction view.");
  } catch (_error) {
    // setFileMeta is best-effort
  }
}

function reviewDocumentRenderState(result) {
  return normalizeReviewDocumentRender(
    reviewDocumentRenderCandidate(result)
      || reviewDocumentRenderCandidate(state.selectedMatter)
      || sourcePdfRenderCandidate(state.selectedMatter),
  );
}

function reviewDocumentRenderCandidate(source) {
  if (!source || typeof source !== "object") return null;
  return source.document_render || source.rendered_document || source.pdf_render || source.source_render || null;
}

function sourcePdfRenderCandidate(matter) {
  if (!matter?.id) return null;
  const filename = String(matter.source_filename || matter.attachment_filename || "").trim();
  if (!/\.pdf$/i.test(filename)) return null;
  return {
    pdf_url: `/api/matters/${encodeURIComponent(matter.id)}/source`,
    source_label: "Original PDF",
    source_fallback: true,
    status: "ready",
  };
}

function requestMatterDocumentRenderPreview() {
  const matterId = state.selectedMatter?.id;
  if (!matterId) return;
  if (hasDocumentRenderPreview(state.reviewDocumentRender)) return;
  const filename = String(state.selectedMatter.source_filename || state.selectedMatter.attachment_filename || "").trim();
  if (!/\.(docx|pdf)$/i.test(filename)) return;
  // A PDF "Original PDF" source arrives as a sourceFallback candidate (see
  // sourcePdfRenderCandidate). The backend rasterizes such a PDF fine via
  // PyMuPDF (document_rendering.py, no soffice), so a PDF source that has a
  // real /source URL must ALWAYS attempt the page-image render -- the same way
  // a .docx source already flows straight through to /render-status. The gate
  // used to drop a sourceFallback PDF unless the matter carried repository
  // markers (source_type / board_column / document_title / review_refresh),
  // which left every non-repository "Original PDF" matter (e.g. Pismo) blank
  // even though the backend could render it. We only need the matter id + a
  // .docx/.pdf source (both already checked above) to drive /render-status, so
  // the repository-marker condition is removed.

  const sequence = reviewDocumentRenderRequestSequence + 1;
  reviewDocumentRenderRequestSequence = sequence;
  state.reviewDocumentRender = normalizeReviewDocumentRender({
    source_label: /\.docx$/i.test(filename) ? "Converted DOCX" : "Rendered PDF",
    status: "loading",
  });
  renderStudioDocumentHighlights();

  fetch(`/api/matters/${encodeURIComponent(matterId)}/render-status`)
    .then(async (response) => {
      const payload = await response.json();
      if (!response.ok) {
        const error = reviewErrorFromPayload(payload, "PDF preview could not load.");
        error.payload = payload;
        throw error;
      }
      return payload;
    })
    .then((payload) => {
      if (sequence !== reviewDocumentRenderRequestSequence || state.selectedMatter?.id !== matterId) return;
      state.reviewDocumentRender = normalizeReviewDocumentRender(
        payload.document_render || payload.rendered_document || payload.pdf_render || null,
      );
      renderStudioDocumentHighlights();
    })
    .catch((error) => {
      if (sequence !== reviewDocumentRenderRequestSequence || state.selectedMatter?.id !== matterId) return;
      state.reviewDocumentRender = normalizeReviewDocumentRender({
        error: error?.message || "PDF preview could not load.",
        source_label: "Rendered PDF",
        status: "error",
      });
      renderStudioDocumentHighlights();
    });
}

function normalizeReviewDocumentRender(candidate) {
  if (!candidate || typeof candidate !== "object") return null;
  const pages = normalizeRenderPages(candidate.pages);
  const pdfUrl = stringValue(candidate.pdf_url || candidate.pdfUrl || candidate.url || candidate.href);
  const rawStatus = stringValue(candidate.status || (pdfUrl ? "ready" : ""));
  const status = normalizedRenderStatus(rawStatus, pdfUrl, pages);
  if (status === "unavailable") return null;
  const pageCount = numericPageCount(
    candidate.page_count
      ?? candidate.pageCount
      ?? (!Array.isArray(candidate.pages) ? candidate.pages : null),
  ) || (pages.length ? pages.length : null);
  const renderState = {
    error: renderDocumentErrorMessage(candidate),
    pageCount,
    pdfUrl,
    sourceLabel: stringValue(candidate.source_label || candidate.label || candidate.kind) || "Rendered PDF",
    status,
  };
  if (pages.length) renderState.pages = pages;
  // The page-image (rasterization) status is a SEPARATE signal from the top-level
  // render status: for a PDF matter the PDF render can succeed (status "ready" +
  // pdf_url) while page-image rasterization fails, in which case the backend sends
  // page_image_status:"failed"/"error" and pages:[]. We MUST read it so the
  // non-Original surfaces do not take the page-image/iframe branch and paint a
  // blank block above the editable text. Read defensively from snake/camel case.
  const pageImageStatus = stringValue(candidate.page_image_status || candidate.pageImageStatus);
  if (pageImageStatus) renderState.pageImageStatus = pageImageStatus.toLowerCase();
  if (candidate.source_fallback || candidate.sourceFallback) renderState.sourceFallback = true;
  // Backend signal (owned by the source->canonical-DOCX lane) that a PDF-source
  // matter now has a canonical "working" DOCX available at /api/matters/<id>/
  // working-docx. Defaults FALSE/absent, which keeps the PDF faithful-render branch
  // in selectFaithfulRenderPlan dormant until the backend ships it. Read defensively
  // from either snake_case (server JSON) or camelCase.
  if (candidate.working_docx_ready === true || candidate.workingDocxReady === true) {
    renderState.workingDocxReady = true;
  }
  const overlay = normalizeDocumentOverlay(candidate.document_overlay || candidate.documentOverlay);
  if (overlay) renderState.documentOverlay = overlay;
  const errorCode = stringValue(candidate.error_code || candidate.errorCode);
  if (errorCode) renderState.errorCode = errorCode;
  return renderState;
}

function normalizeDocumentOverlay(overlay) {
  if (!overlay || typeof overlay !== "object") return null;
  const anchors = Array.isArray(overlay.anchors)
    ? overlay.anchors.map(normalizeDocumentOverlayAnchor).filter(Boolean)
    : [];
  return {
    anchors,
    fallbackMode: stringValue(overlay.fallback_mode || overlay.fallbackMode),
    precision: stringValue(overlay.precision),
    status: stringValue(overlay.status),
    version: positiveInteger(overlay.version) || 1,
  };
}

function normalizeDocumentOverlayAnchor(anchor) {
  if (!anchor || typeof anchor !== "object") return null;
  const pageNumber = positiveInteger(anchor.page_number ?? anchor.pageNumber);
  if (!pageNumber) return null;
  const normalized = {
    boxes: Array.isArray(anchor.boxes) ? anchor.boxes : [],
    clauseId: stringValue(anchor.clause_id || anchor.clauseId),
    confidence: Number.isFinite(Number(anchor.confidence)) ? Number(anchor.confidence) : null,
    paragraphId: stringValue(anchor.paragraph_id || anchor.paragraphId),
    pageNumber,
    targetType: stringValue(anchor.target_type || anchor.targetType),
  };
  const redlineId = stringValue(anchor.redline_id || anchor.redlineId);
  if (redlineId) normalized.redlineId = redlineId;
  return normalized;
}

function normalizeRenderPages(pages) {
  if (!Array.isArray(pages)) return [];
  return pages
    .map((page, index) => normalizeRenderPage(page, index))
    .filter(Boolean);
}

function normalizeRenderPage(page, index) {
  if (!page || typeof page !== "object") return null;
  const imageUrl = stringValue(page.image_url || page.imageUrl || page.url || page.src);
  if (!imageUrl) return null;
  const pageNumber = positiveInteger(page.page_number ?? page.pageNumber ?? page.number) || index + 1;
  const width = positiveInteger(page.width);
  const height = positiveInteger(page.height);
  const dpi = positiveInteger(page.dpi);
  const renderPage = {
    imageUrl,
    pageNumber,
  };
  if (width) renderPage.width = width;
  if (height) renderPage.height = height;
  if (dpi) renderPage.dpi = dpi;
  return renderPage;
}

function positiveInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : null;
}

function hasDocumentRenderPreview(renderState) {
  return Boolean(renderState?.pages?.length || (renderState?.pdfUrl && !renderState?.sourceFallback));
}

function normalizedRenderStatus(status, pdfUrl, pages = []) {
  const normalized = String(status || "").trim().toLowerCase();
  const hasPages = Array.isArray(pages) && pages.length > 0;
  if (["ready", "complete", "completed", "available", "success"].includes(normalized) && (pdfUrl || hasPages)) return "ready";
  if (["failed", "error"].includes(normalized)) return "error";
  if (normalized === "unavailable") return "unavailable";
  if (["queued", "pending", "processing", "running", "loading"].includes(normalized)) return "loading";
  return pdfUrl || hasPages ? "ready" : "unavailable";
}

function renderDocumentErrorMessage(candidate) {
  if (typeof candidate.error === "string") return candidate.error.trim();
  if (candidate.error && typeof candidate.error === "object") {
    return stringValue(candidate.error.message);
  }
  return stringValue(candidate.message || candidate.status_message);
}

function numericPageCount(value) {
  const count = Number(value);
  return Number.isFinite(count) && count > 0 ? Math.floor(count) : null;
}

function stringValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

// True only when a page-image (or iframe) preview surface would genuinely paint
// content for the NON-Original views: the top-level render is ready AND the
// page-image rasterization itself is good AND we actually have page images to show.
//
// WHY: for a PDF matter the PDF render can succeed (status "ready" + pdf_url) while
// page-image rasterization FAILS -- the backend then sends page_image_status:
// "failed"/"error" and pages:[]. Without this guard the non-Original surface took
// the `status==="ready" && pdfUrl` iframe branch and painted a fixed-height
// (~520px) /render-pdf iframe that shows BLANK when the iframe never paints,
// shoving the editable text reconstruction far below the fold. The reconstruction
// is the always-visible FLOOR; the page-image surface is only a tier-2 upgrade, so
// when it cannot genuinely paint we emit nothing and let the reconstruction stand.
function pageImageSurfaceUsable(renderState) {
  if (!renderState) return false;
  if ((renderState.status || "") !== "ready") return false;
  const pages = Array.isArray(renderState.pages) ? renderState.pages : [];
  if (!pages.length) return false;
  // page_image_status, when present, must itself be good. Absent -> trust `pages`
  // (the backend only attaches a manifest+pages when rasterization produced them).
  const pageImageStatus = renderState.pageImageStatus || "";
  if (pageImageStatus && !["ready", "complete", "completed", "available", "success"].includes(pageImageStatus)) {
    return false;
  }
  return true;
}

function renderPdfDocumentSurface(renderState) {
  if (!renderState) return "";
  const pages = Array.isArray(renderState.pages) ? renderState.pages : [];
  const pageLabel = renderState.pageCount
    ? `${renderState.pageCount} ${renderState.pageCount === 1 ? "page" : "pages"}`
    : "";
  const meta = [renderState.sourceLabel, pageLabel].filter(Boolean).join(" · ");

  // ONLY paint the page-image surface when it is genuinely usable. We deliberately
  // do NOT fall back to a fixed-height /render-pdf iframe here: in the non-Original
  // views the editable text reconstruction is appended right after this and is the
  // always-visible floor, so a blank/half-painted iframe above it would just push
  // the real content below the fold. When the page images are not usable we emit
  // nothing and let the reconstruction be the surface.
  if (pageImageSurfaceUsable(renderState)) {
    return `
      <section class="review-pdf-surface review-page-surface ready" data-review-pdf-surface data-review-render-surface data-render-status="ready" aria-label="Rendered document preview">
        <div class="review-pdf-status">
          <strong>${escapeHtml(meta || "Rendered document")}</strong>
          <span>Page image preview</span>
        </div>
        <div class="review-render-pages" data-review-render-pages>
          ${pages.map((page, index) => renderDocumentPageImage(page, index, pages.length, renderState)).join("")}
        </div>
      </section>
      <div class="review-fallback-divider" aria-hidden="true"><span>Editable text review</span></div>
    `;
  }

  // Not usable: no banner, no blank iframe -- the reconstruction below stands alone.
  return "";
}

function renderOriginalDocumentSurface(renderState) {
  const status = renderState?.status || "";
  const pages = Array.isArray(renderState?.pages) ? renderState.pages : [];
  const pdfUrl = renderState?.pdfUrl || "";
  const pageLabel = renderState?.pageCount
    ? `${renderState.pageCount} ${renderState.pageCount === 1 ? "page" : "pages"}`
    : "";
  const meta = [renderState?.sourceLabel, pageLabel].filter(Boolean).join(" · ");

  if (status === "ready" && pages.length) {
    return `
      <section class="review-original-surface review-page-surface ready" data-review-pdf-surface data-review-render-surface data-original-surface data-render-status="ready" aria-label="Original document preview">
        <div class="review-pdf-status">
          <strong>${escapeHtml(meta || "Original document")}</strong>
          <span>Exact document preview</span>
        </div>
        <div class="review-render-pages" data-review-render-pages>
          ${pages.map((page, index) => renderDocumentPageImage(page, index, pages.length, renderState)).join("")}
        </div>
      </section>
    `;
  }

  if (status === "ready" && pdfUrl) {
    return `
      <section class="review-original-surface ready" data-review-pdf-surface data-original-surface data-render-status="ready" aria-label="Original document preview">
        <div class="review-pdf-status">
          <strong>${escapeHtml(meta || "Original document")}</strong>
          <span>Exact document preview</span>
        </div>
        <iframe class="review-pdf-frame review-original-frame" src="${escapeHtml(pdfUrl)}" title="${escapeHtml(renderState?.sourceLabel || "Original document")}"></iframe>
      </section>
    `;
  }

  const sourceFidelity = state.latestReviewResult?.source_fidelity;
  if (sourceFidelityPreviewAvailable(sourceFidelity)) {
    return renderSourceFidelitySurface(sourceFidelity, renderState, status);
  }

  return renderOriginalUnavailableFallback(renderState, status);
}

function sourceFidelityPreviewAvailable(sourceFidelity) {
  return Boolean(
    sourceFidelity
    && typeof sourceFidelity === "object"
    && sourceFidelity.render_model === "source_blocks"
    && Array.isArray(sourceFidelity.blocks)
    && sourceFidelity.blocks.length,
  );
}

function renderSourceFidelitySurface(sourceFidelity, renderState, status) {
  const summary = sourceFidelity.summary && typeof sourceFidelity.summary === "object" ? sourceFidelity.summary : {};
  const capabilities = sourceFidelity.capabilities && typeof sourceFidelity.capabilities === "object" ? sourceFidelity.capabilities : {};
  const sourceType = String(sourceFidelity.source_type || "").trim().toUpperCase();
  const tableCount = Number(summary.table_count) || 0;
  const colorRunCount = Number(summary.color_run_count) || 0;
  const styledTableCellCount = Number(summary.styled_table_cell_count) || 0;
  const previewLabel = sourceFidelityPreviewLabel(sourceFidelity);
  const capabilityLabels = [
    tableCount ? `${tableCount} ${tableCount === 1 ? "table" : "tables"}` : "",
    colorRunCount ? `${colorRunCount} coloured ${colorRunCount === 1 ? "run" : "runs"}` : "",
    styledTableCellCount ? `${styledTableCellCount} styled ${styledTableCellCount === 1 ? "cell" : "cells"}` : "",
    capabilities.inline_runs ? "inline runs" : "",
  ].filter(Boolean);
  const statusNote = sourceFidelityStatusNote(renderState, status, sourceFidelity);
  return `
    <section class="review-original-surface source-fidelity-surface ready" data-review-pdf-surface data-original-surface data-source-fidelity-surface data-render-status="source-fidelity" aria-label="${escapeHtml(previewLabel)}">
      <div class="review-pdf-status source-fidelity-status">
        <strong>${escapeHtml(previewLabel)}</strong>
        <span>${escapeHtml(capabilityLabels.length ? capabilityLabels.join(" · ") : "Source blocks from the original document")}</span>
      </div>
      ${statusNote ? `<p class="source-fidelity-note">${escapeHtml(statusNote)}</p>` : ""}
      <div class="source-fidelity-document" data-source-fidelity-document>
        ${sourceFidelity.blocks.map(renderSourceFidelityBlock).join("")}
      </div>
    </section>
  `;
}

function sourceFidelityPreviewLabel(sourceFidelity) {
  const sourceType = String(sourceFidelity?.source_type || "").trim().toLowerCase();
  if (sourceType === "pdf") return "PDF source analysis preview";
  return sourceType ? `${sourceType.toUpperCase()} source layout preview` : "Source layout preview";
}

function sourceFidelityStatusNote(renderState, status, sourceFidelity) {
  const sourceType = String(sourceFidelity?.source_type || "").trim().toLowerCase();
  if (sourceType === "pdf") {
    const policyMessage = stringValue(sourceFidelity?.pdf_fidelity?.message);
    const profileSummary = sourceFidelityPdfVisualProfileSummary(sourceFidelity?.pdf_fidelity?.visual_profile);
    const message = policyMessage
      || "PDF visual fidelity comes from the Original PDF/page preview. These extracted source blocks are analysis text and may not preserve page layout.";
    return profileSummary ? `${message} ${profileSummary}` : message;
  }
  if (status === "loading") {
    return "Exact page images are still rendering. This source layout preview preserves available tables, runs, and colour data meanwhile.";
  }
  if (status === "error") {
    const detail = stringValue(renderState?.error);
    return detail
      ? `${detail} Showing the source layout preview instead.`
      : "Exact page images could not be rendered. Showing the source layout preview instead.";
  }
  const limitations = Array.isArray(sourceFidelity?.limitations) ? sourceFidelity.limitations : [];
  const limitation = limitations.find((item) => item && typeof item === "object" && item.message);
  if (limitation) return String(limitation.message || "").trim();
  return "This preview uses the source blocks extracted for review. Redline and Clean remain editable text views.";
}

function sourceFidelityPdfVisualProfileSummary(profile) {
  if (!profile || typeof profile !== "object") return "";
  const details = [];
  const colouredText = Number(profile.non_black_text_span_count);
  const drawings = Number(profile.drawing_count);
  const images = Number(profile.image_count);
  if (Number.isFinite(colouredText) && colouredText > 0) {
    details.push(`${colouredText} non-black text ${colouredText === 1 ? "span" : "spans"}`);
  }
  if (Number.isFinite(drawings) && drawings > 0) {
    details.push(`${drawings} drawing or border ${drawings === 1 ? "item" : "items"}`);
  }
  if (Number.isFinite(images) && images > 0) {
    details.push(`${images} image ${images === 1 ? "item" : "items"}`);
  }
  return details.length ? `Detected visual signals: ${details.join(", ")}.` : "";
}

function renderSourceFidelityBlock(block) {
  if (!block || typeof block !== "object") return "";
  if (block.type === "table") return renderSourceFidelityTable(block);
  return renderSourceFidelityParagraphBlock(block);
}

function renderSourceFidelityTable(table) {
  const rows = Array.isArray(table.rows) ? table.rows : [];
  return `
    <table class="source-fidelity-table" data-source-fidelity-table="${escapeHtml(table.table_index || "")}">
      <tbody>
        ${rows.map(renderSourceFidelityTableRow).join("")}
      </tbody>
    </table>
  `;
}

function renderSourceFidelityTableRow(row) {
  const cells = Array.isArray(row?.cells) ? row.cells : [];
  return `
    <tr>
      ${cells.map(renderSourceFidelityTableCell).join("")}
    </tr>
  `;
}

function renderSourceFidelityTableCell(cell) {
  const blocks = Array.isArray(cell?.blocks) ? cell.blocks : [];
  const paragraphIds = Array.isArray(cell?.paragraph_ids) ? cell.paragraph_ids : [];
  const cellStyle = sourceFidelityCellCss(cell);
  const cellStyleAttribute = cellStyle.style ? ` style="${escapeHtml(cellStyle.style)}"` : "";
  const cellStyleData = [
    cellStyle.background ? `data-source-fidelity-cell-background="${escapeHtml(cellStyle.background)}"` : "",
    cellStyle.width ? `data-source-fidelity-cell-width="${escapeHtml(cellStyle.width)}"` : "",
  ].filter(Boolean).join(" ");
  return `
    <td data-source-fidelity-paragraph-ids="${escapeHtml(paragraphIds.join(" "))}"${cellStyleAttribute}${cellStyleData ? ` ${cellStyleData}` : ""}>
      ${blocks.length ? blocks.map(renderSourceFidelityParagraphBlock).join("") : "&nbsp;"}
    </td>
  `;
}

function sourceFidelityCellCss(cell) {
  const style = cell?.style && typeof cell.style === "object" ? cell.style : {};
  const declarations = [];
  const background = sourceFidelityCssColor(style.background_color);
  if (background) declarations.push(`background-color:${background}`);
  const width = sourceFidelityCssWidth(style.width);
  if (width) declarations.push(`width:${width}`);
  return {
    background,
    style: declarations.join(";"),
    width,
  };
}

function sourceFidelityCssColor(value) {
  const color = String(value || "").trim();
  if (/^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i.test(color)) return color;
  if (/^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}(?:\s*,\s*(?:0|1|0?\.\d+))?\s*\)$/i.test(color)) return color;
  return "";
}

function sourceFidelityCssWidth(value) {
  if (value && typeof value === "object") {
    const numeric = Number(value.value);
    if (!Number.isFinite(numeric) || numeric <= 0) return "";
    const type = String(value.type || "").trim().toLowerCase();
    if (type === "dxa") return `${sourceFidelityRoundCssNumber(numeric / 15)}px`;
    if (type === "pct") return `${sourceFidelityRoundCssNumber(Math.min(Math.max(numeric / 50, 1), 100))}%`;
    if (type === "px") return `${sourceFidelityRoundCssNumber(numeric)}px`;
    if (type === "pt") return `${sourceFidelityRoundCssNumber(numeric)}pt`;
    return "";
  }
  const width = String(value || "").trim();
  if (/^\d+(?:\.\d+)?(?:px|pt|em|rem|%)$/i.test(width)) return width;
  return "";
}

function sourceFidelityRoundCssNumber(value) {
  return Number(value.toFixed(2)).toString();
}

// The Structure tab deliberately SUPPRESSES the raw Word style id (e.g. "Heading2",
// "ListParagraph") as meaningless to a reviewer (contract-structure-view.js
// sourceSummary). Mirror that here: only surface the handful of style ids that carry
// a reviewer-meaningful meaning, mapped to plain English; hide every other style id
// (the badge simply does not render) rather than leaking the parser-internal token.
function sourceFidelityStyleLabel(styleName) {
  const name = String(styleName || "").trim();
  if (!name) return "";
  if (/^heading\s*[1-9]$/i.test(name)) return "Heading";
  const normalized = name.toLowerCase().replace(/\s+/g, "");
  if (normalized === "title") return "Title";
  if (normalized === "listparagraph") return "List item";
  return "";
}

function renderSourceFidelityParagraphBlock(block) {
  const paragraphId = String(block?.id || "").trim();
  const text = String(block?.text || "").trim();
  const style = block?.style && typeof block.style === "object" ? block.style : {};
  const styleName = String(block?.style_name || style.style_name || "").trim();
  const styleLabel = sourceFidelityStyleLabel(styleName);
  const classes = ["source-fidelity-paragraph", styleLabel ? "has-style" : ""].filter(Boolean).join(" ");
  const body = sourceFidelityParagraphBody(block);
  return `
    <p class="${classes}" ${paragraphId ? `data-paragraph-id="${escapeHtml(paragraphId)}"` : ""}>
      ${styleLabel ? `<span class="source-fidelity-style">${escapeHtml(styleLabel)}</span>` : ""}
      ${body || escapeHtml(text)}
    </p>
  `;
}

function sourceFidelityParagraphBody(block) {
  if (typeof renderParagraphRichText === "function") return renderParagraphRichText(block);
  const runs = Array.isArray(block?.runs) ? block.runs : [];
  if (!runs.length) return escapeHtml(String(block?.text || ""));
  return runs.map((run) => escapeHtml(String(run?.text || ""))).join("");
}

// Graceful "Original" fallback: when no faithful page-image render exists (DOCX
// with no document server, or a render that is still pending or failed), show a
// friendly explanation and a button back to the structured Redline view — never
// a blank or broken surface.
function renderOriginalUnavailableFallback(renderState, status) {
  const loading = status === "loading";
  const errored = status === "error";
  const title = loading
    ? "Preparing the high-fidelity preview"
    : "High-fidelity preview isn't available here";
  let message;
  if (loading) {
    message = "The document server is rendering the exact page images. This view will update when they are ready.";
  } else if (errored) {
    const detail = stringValue(renderState?.error);
    message = detail
      ? `${detail} Showing the structured view instead.`
      : "The document server could not render this document. Showing the structured view instead.";
  } else {
    message = "The document server isn't running, so the exact page images can't be shown. Showing the structured view instead.";
  }
  return `
    <section class="review-original-surface review-original-empty ${escapeHtml(status || "unavailable")}" data-review-pdf-surface data-original-surface data-render-status="${escapeHtml(status || "unavailable")}" aria-label="Original document preview status">
      <div class="review-original-empty-body">
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(message)}</p>
        <button type="button" class="review-original-fallback-button" data-original-fallback-view-mode="redline">Show structured view</button>
      </div>
    </section>
  `;
}

function renderDocumentPageImage(page, index, totalPages, renderState = null) {
  const pageNumber = page.pageNumber || index + 1;
  const dimensions = page.width && page.height ? `${page.width} x ${page.height}` : "";
  const dpi = page.dpi ? `${page.dpi} DPI` : "";
  const detail = [dimensions, dpi].filter(Boolean).join(" · ");
  const widthAttribute = page.width ? ` width="${escapeHtml(page.width)}"` : "";
  const heightAttribute = page.height ? ` height="${escapeHtml(page.height)}"` : "";
  const aspectStyle = page.width && page.height ? ` style="aspect-ratio: ${escapeHtml(page.width)} / ${escapeHtml(page.height)};"` : "";
  const anchors = pageOverlayAnchors(renderState, pageNumber);
  const clauseIds = uniqueStrings(anchors.map((anchor) => anchor.clauseId)).join(" ");
  const paragraphIds = uniqueStrings(anchors.map((anchor) => anchor.paragraphId)).join(" ");
  const anchorAttributes = [
    clauseIds ? `data-overlay-clause-ids="${escapeHtml(clauseIds)}"` : "",
    paragraphIds ? `data-overlay-paragraph-ids="${escapeHtml(paragraphIds)}"` : "",
  ].filter(Boolean).join(" ");
  const selected = clauseIds.split(" ").includes(state.selectedReviewClauseId);
  return `
    <figure class="${joinClasses("review-render-page", selected ? "has-selected-anchor" : "")}" data-review-render-page="${escapeHtml(pageNumber)}"${anchorAttributes ? ` ${anchorAttributes}` : ""}>
      <div class="review-render-page-image"${aspectStyle}>
        <img
          src="${escapeHtml(page.imageUrl)}"
          alt="${escapeHtml(`Page ${pageNumber} of ${totalPages}`)}"
          loading="${index === 0 ? "eager" : "lazy"}"
          decoding="async"${widthAttribute}${heightAttribute}
        >
      </div>
      <figcaption>
        <span>Page ${escapeHtml(pageNumber)}</span>
        ${selected ? "<span>Selected clause evidence</span>" : detail ? `<span>${escapeHtml(detail)}</span>` : ""}
      </figcaption>
    </figure>
  `;
}

function pageOverlayAnchors(renderState, pageNumber) {
  const anchors = renderState?.documentOverlay?.anchors;
  if (!Array.isArray(anchors)) return [];
  return anchors.filter((anchor) => anchor.pageNumber === pageNumber);
}

function uniqueStrings(values) {
  return Array.from(new Set(values.map((value) => String(value || "").trim()).filter(Boolean)));
}
