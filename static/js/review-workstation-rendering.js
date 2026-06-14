let reviewDocumentRenderRequestSequence = 0;

function reviewWorkstationModel() {
  return window.ReviewWorkstationModel || null;
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
  state.reviewRedlines = result.redline_edits || [];
  state.reviewComments = [];
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  state.exportRedlineDecisions = {};
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  state.redlineDraft = null;
  state.redlineDraftDirty = false;
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
  studioResultMeta.textContent = "No hard-clause review has run yet.";
  resetReviewEditHistory();
  if (state.reviewInspectorView === "structure") {
    reviewStructureController.render();
  } else if (state.reviewInspectorView === "fill") {
    reviewFillController.render();
  } else {
    studioDetailPanel.innerHTML = "";
  }
  updateReviewInspectorTabs();
  updateExportButtonState();
  renderStudioClauseLane();
}

function updateExportButtonState() {
  const canExport = state.reviewClauses.length && (studioNdaText.value.trim() || state.reviewSourceText.trim());
  const staleReview = Boolean(state.selectedMatter?.review_refresh?.stale);
  if (studioExportButton) {
    studioExportButton.disabled = !canExport || staleReview;
    studioExportButton.title = staleReview ? "Refresh review before downloading" : "Download";
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
    if (reviewBlocked) updateReviewedButtonScope();
  }
  updateApproveReviewControl();
  updateRedlineDraftControls();
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

function renderStudioResult(result) {
  const clauses = result.clauses || [];
  renderStudioSummary(clauses);
  renderStudioClauseLane();
  renderStudioDetail();
  renderStudioDocumentHighlights();
}

function renderStudioSummary(clauses) {
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

// Generic human-readable timestamp formatter (used by the approved-review
// title). Falls back to the raw value when it isn't a parseable date.
function formatReviewTimestamp(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  try {
    return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch (error) {
    return date.toISOString();
  }
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
  // Live re-assessment: when the reviewer picks a different template option,
  // re-run the single-clause check against the PROPOSED text so the verdict
  // reflects the selected wording, not the stale source text.
  const edit = state.reviewRedlines.find((item) => item.id === editId);
  if (edit?.clause_id && state.selectedMatter?.id) {
    if (typeof scheduleClauseReassess === "function") {
      scheduleClauseReassess(edit.clause_id, _buildEditedParagraphsForTemplateOption(edit, optionId));
    }
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
// clause reads as a FAIL in real time (recomputed on every render, on entity
// change, and on a document edit). This is a live UI signal layered on top of the
// backend verdict — it does not re-run the backend review.

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
const DOCUMENT_GOVERNING_LAWS = [
  ["India", /\b(?:india|indian)\b/i],
  ["Delaware", /\bdelaware\b/i],
  ["England and Wales", /\b(?:england and wales|english\s+law|laws?\s+of\s+england)\b/i],
  ["DIFC", /\b(?:difc|dubai international financial cent)/i],
  ["Ontario, Canada", /\b(?:ontario|ontarian|canadian|canada)\b/i],
];

function detectDocumentGoverningLaw() {
  const paragraphs = Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [];
  for (const paragraph of paragraphs) {
    const text = String((paragraph && paragraph.text) || "");
    // Operative governing-law language only — never an "incorporated under the
    // laws of X" recital, which names a party's jurisdiction, not the contract's.
    if (!/governing\s+law|governed\s+by|construed\s+in\s+accordance/i.test(text)) continue;
    for (const [label, pattern] of DOCUMENT_GOVERNING_LAWS) {
      if (pattern.test(text)) return label;
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

// clauseStatus, overridden to a fail for the Governing Law clause when the
// document's law does not concur with the picked entity's law. Used wherever the
// clause verdict is shown so the conflict reads as a fail (dot, headline, status).
function clauseDisplayStatus(clause) {
  const status = clauseStatus(clause);
  if (clause && clause.id === "governing_law" && governingLawConflict()) {
    return {
      ...status,
      tone: "check",
      dotTone: "verify",
      fails: true,
      needsReview: false,
      passes: false,
      requiresAttention: true,
      blocksSend: true,
      issueLabel: "Fail",
      pillLabel: "FAIL",
    };
  }
  return status;
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
  if (state.reviewInspectorView === "structure") {
    reviewStructureController.render();
    return;
  }
  if (state.reviewInspectorView === "fill") {
    reviewFillController.render();
    return;
  }
  const clause = getSelectedReviewClause();
  if (!clause) {
    studioDetailPanel.innerHTML = "";
    return;
  }
  const status = clauseDisplayStatus(clause);
  const verdictHeader = renderClauseVerdictHeader(clause, status);
  const assessment = renderClauseAssessmentSection(clause);
  const documentEvidence = renderClauseDocumentEvidenceBlock(clause);
  const playbookPosition = renderClausePlaybookPositionBlock(clause);
  const proposedChange = renderProposedChangeBlock(clause, status);
  const proposedRedlines = renderProposedRedlinesBlock(clause);
  const actions = renderClauseActionsBlock(clause, status);
  const reasoningTrail = renderReasoningTrailBlock(clause);
  // Governing-law concurrence banner + unified entity-aware picker (Issue 1).
  const glConflict = clause.id === "governing_law" ? governingLawConflict() : null;
  const concurrenceBanner = glConflict
    ? `<div class="studio-detail-block gl-concurrence-fail">
        <small>Governing law conflict</small>
        <p>The document's governing law (<strong>${escapeHtml(glConflict.docLaw)}</strong>) does not concur with the selected entity <strong>${escapeHtml(glConflict.entityName)}</strong>, which is governed by <strong>${escapeHtml(glConflict.entityLaw)}</strong>.</p>
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
  // option display in that case — the concurrence detection, banner, and FAIL-pill
  // (clauseDisplayStatus override) are untouched. When there is NO backend govlaw
  // redline_edit to host the options, keep this picker so the redline-to-
  // recommended-law capability is preserved.
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
// excluded as pure contract plumbing that means nothing to a reviewer.
function auditTraceTrailSteps(clause) {
  const trace = clause?.audit_trace && typeof clause.audit_trace === "object" ? clause.audit_trace : null;
  const steps = Array.isArray(trace?.steps) ? trace.steps.filter((step) => step && step.name) : [];
  return steps;
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

function renderGroundingAuditBlock(clause) {
  const grounding = clause?.grounding && typeof clause.grounding === "object" ? clause.grounding : {};
  const evidenceCount = Array.isArray(clause?.structured_evidence) ? clause.structured_evidence.length : 0;
  const status = String(grounding.status || "").trim() || (evidenceCount ? "grounded" : "not recorded");
  const paragraphIds = Array.isArray(clause?.matched_paragraph_ids) ? clause.matched_paragraph_ids : [];
  return `
    <div class="grounding-audit-block">
      <span class="detail-field-label">Grounding</span>
      <p>Status: ${escapeHtml(status.replace(/_/g, " "))}. Evidence records: ${escapeHtml(evidenceCount)}.${paragraphIds.length ? ` Paragraphs: ${paragraphIds.map((id) => escapeHtml(id)).join(", ")}.` : ""}</p>
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
      return action ? action.replace(/_/g, " ") : "Review change";
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
      return String(status || "").replace(/_/g, " ");
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
  return `
    <div class="studio-detail-block clause-actions-block" data-card-section="actions">
      <small>Actions</small>
      ${redlines.length ? `
        <p class="action-muted">Use the Include/Ignore controls on the proposed edit above to choose what is exported.</p>
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
  let applied = false;
  spans.forEach((span) => {
    const start = Number(span?.start);
    const end = Number(span?.end);
    if (Number.isFinite(start) && Number.isFinite(end) && Number.isFinite(paragraphStart)) {
      applied = highlightClauseTextRange(editable, start - paragraphStart, end - paragraphStart, clauseId, toneClass) || applied;
    }
  });
  if (applied) return true;
  const quote = String(item?.quote || "").trim();
  if (quote) {
    const fullText = editable.textContent || "";
    const index = fullText.toLowerCase().indexOf(quote.toLowerCase());
    if (index >= 0) {
      return highlightClauseTextRange(editable, index, index + quote.length, clauseId, toneClass);
    }
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
    return;
  }
  // Any non-Original render means we have left the Original view: drop the
  // markup toolbar/overlays so they never bleed into the other modes.
  notifyPdfMarkupLeaveOriginal();

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
  if (state.reviewDocumentRender?.sourceFallback && !isRepositoryMatterForRenderPreview(state.selectedMatter)) return;

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
  if (candidate.source_fallback || candidate.sourceFallback) renderState.sourceFallback = true;
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

function isRepositoryMatterForRenderPreview(matter) {
  return Boolean(matter?.source_type || matter?.board_column || matter?.document_title || matter?.review_refresh);
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

function renderPdfDocumentSurface(renderState) {
  if (!renderState) return "";
  const status = renderState.status || "loading";
  const pages = Array.isArray(renderState.pages) ? renderState.pages : [];
  const pageLabel = renderState.pageCount
    ? `${renderState.pageCount} ${renderState.pageCount === 1 ? "page" : "pages"}`
    : "";
  const meta = [renderState.sourceLabel, pageLabel].filter(Boolean).join(" · ");

  if (status === "ready" && pages.length) {
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

  if (status === "ready" && renderState.pdfUrl) {
    return `
      <section class="review-pdf-surface ready" data-review-pdf-surface data-render-status="ready" aria-label="Rendered document preview">
        <div class="review-pdf-status">
          <strong>${escapeHtml(meta || "Rendered PDF")}</strong>
          <span>High-resolution preview</span>
        </div>
        <iframe class="review-pdf-frame" src="${escapeHtml(renderState.pdfUrl)}" title="${escapeHtml(renderState.sourceLabel || "Rendered document")}"></iframe>
      </section>
      <div class="review-fallback-divider" aria-hidden="true"><span>Editable text review</span></div>
    `;
  }

  const message = status === "error"
    ? renderState.error || "Rendered PDF is unavailable. Showing editable text review."
    : "Preparing high-resolution document preview. Showing editable text review.";
  return `
    <section class="review-pdf-surface ${escapeHtml(status)}" data-review-pdf-surface data-render-status="${escapeHtml(status)}" aria-label="Rendered document preview status">
      <div class="review-pdf-status">
        <strong>${escapeHtml(status === "error" ? "PDF preview unavailable" : "PDF preview loading")}</strong>
        <span>${escapeHtml(message)}</span>
      </div>
    </section>
  `;
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

function renderSourceFidelityParagraphBlock(block) {
  const paragraphId = String(block?.id || "").trim();
  const text = String(block?.text || "").trim();
  const style = block?.style && typeof block.style === "object" ? block.style : {};
  const styleName = String(block?.style_name || style.style_name || "").trim();
  const classes = ["source-fidelity-paragraph", styleName ? "has-style" : ""].filter(Boolean).join(" ");
  const body = sourceFidelityParagraphBody(block);
  return `
    <p class="${classes}" ${paragraphId ? `data-paragraph-id="${escapeHtml(paragraphId)}"` : ""}>
      ${styleName ? `<span class="source-fidelity-style">${escapeHtml(styleName)}</span>` : ""}
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
