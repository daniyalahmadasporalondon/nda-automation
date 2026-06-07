let reviewDocumentRenderRequestSequence = 0;

function renderResult(result, reviewedText) {
  pendingReviewSendMatterId = null;
  state.reviewDocumentRender = reviewDocumentRenderState(result);
  state.latestReviewResult = result;
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

function snapshotReviewParagraphs(paragraphs) {
  return (paragraphs || []).map((paragraph) => {
    const snapshot = {
      id: paragraph.id,
      index: paragraph.index,
      text: String(paragraph.text || ""),
    };
    if (paragraph.source_index !== undefined) snapshot.source_index = paragraph.source_index;
    if (paragraph.source_part !== undefined) snapshot.source_part = paragraph.source_part;
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
  const canExportAnnotatedPdf = Boolean(canExport && selectedMatterIsPdf());
  if (studioExportButton) {
    studioExportButton.disabled = !canExport || staleReview;
    studioExportButton.title = staleReview ? "Refresh review before exporting" : "Export DOCX";
  }
  if (studioExportPdfButton) {
    studioExportPdfButton.hidden = !selectedMatterIsPdf();
    studioExportPdfButton.disabled = !canExportAnnotatedPdf || staleReview;
    studioExportPdfButton.title = staleReview ? "Refresh review before exporting" : "Export annotated PDF";
  }
  if (!studioSendButton) {
    updateRedlineDraftControls();
    return;
  }
  const hasSendableMatter = Boolean(state.selectedMatter?.id);
  studioSendButton.hidden = !hasSendableMatter;
  const sendBlockReason = state.selectedMatter?.id ? MatterUtils.gmailSendBlock(state.selectedMatter, state.gmailStatus) : "";
  const canSend = Boolean(canExport && hasSendableMatter && !sendBlockReason && !staleReview);
  // Keep the button clickable once a review has run, even when blocked, so a
  // click can surface *why* sending is blocked (openReviewSendComposer writes the
  // reason to the file-meta line) instead of leaving a silent, dead icon. The
  // .blocked class + aria-disabled mark it not-ready without swallowing the click.
  const interactive = Boolean(canExport && hasSendableMatter && !staleReview);
  studioSendButton.disabled = !interactive;
  studioSendButton.classList.toggle("blocked", interactive && Boolean(sendBlockReason));
  studioSendButton.setAttribute("aria-disabled", String(!interactive));
  if (staleReview) {
    pendingReviewSendMatterId = null;
    setStudioSendButtonLabel("Send Redline", "Refresh review before sending a redline");
  } else if (!canSend) {
    pendingReviewSendMatterId = null;
    const sendLabel = sendBlockReason ? MatterUtils.gmailSendButtonLabel(sendBlockReason) : "Send Redline";
    setStudioSendButtonLabel(sendLabel, sendBlockReason || sendLabel);
  } else {
    pendingReviewSendMatterId = null;
    setStudioSendButtonLabel("Send Redline");
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
  }
  updateApproveReviewControl();
  updateRedlineDraftControls();
}

function selectedMatterIsPdf() {
  return Boolean(state.selectedMatter?.id && String(state.selectedMatter?.source_filename || "").toLowerCase().endsWith(".pdf"));
}

function setStudioSendButtonLabel(label = "Send Redline", title = label) {
  if (!studioSendButton) return;
  const effectiveLabel = label || "Send Redline";
  studioSendButton.setAttribute("aria-label", effectiveLabel);
  studioSendButton.title = title || effectiveLabel;
  studioSendButton.classList.toggle("confirming", effectiveLabel === "Confirm Send");
  studioSendButton.classList.toggle("sending", effectiveLabel === "Sending");
  const textNode = studioSendButton.querySelector(".sr-only");
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
  const counts = state.latestReviewResult?.review_state?.counts;
  const passedCount = reviewStateCount(counts, "pass", clauses.filter((clause) => clauseStatus(clause).passes).length);
  const reviewCount = reviewStateCount(counts, "review", clauses.filter((clause) => clauseStatus(clause).needsReview).length);
  const failedCount = reviewStateCount(counts, "check", clauses.filter((clause) => clauseStatus(clause).fails).length);
  studioMatchSummary.textContent = `${passedCount}/${getClauseTotal(clauses)}`;
  studioResultMark.textContent = failedCount ? "FAIL" : reviewCount ? "REVIEW" : "PASS";
  studioResultMark.className = failedCount ? "check" : reviewCount ? "review" : "pass";
  studioOverallTitle.textContent = failedCount
    ? "Does not meet requirements"
    : reviewCount
      ? "Needs review"
      : "Meets requirements";
  const warning = reviewWarningSummary();
  studioResultMeta.textContent = warning || summaryStatusText(failedCount, reviewCount);
}

function summaryStatusText(failedCount, reviewCount) {
  if (failedCount && reviewCount) {
    return `${failedCount} ${failedCount === 1 ? "clause needs" : "clauses need"} fixing; ${reviewCount} ${reviewCount === 1 ? "needs" : "need"} human review.`;
  }
  if (failedCount) {
    return `${failedCount} hard ${failedCount === 1 ? "clause has" : "clauses have"} failed.`;
  }
  if (reviewCount) {
    return `${reviewCount} ${reviewCount === 1 ? "clause needs" : "clauses need"} human review before send.`;
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
  const label = reviewed ? "Reviewed" : status.issueLabel;
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
  return state.reviewClauses.length > 0;
}

function defaultExportClauseDecisions(clauses, redlines) {
  const clausesWithRedlines = new Set((redlines || []).map((edit) => edit.clause_id).filter(Boolean));
  return Object.fromEntries((clauses || []).map((clause) => [
    clause.id,
    clausesWithRedlines.has(clause.id),
  ]));
}

function defaultRedlineTemplateSelections(redlines) {
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

function upsertReviewComment(comment) {
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
  return state.exportClauseDecisions[clauseId] !== false;
}

function redlineExportIncluded(edit) {
  if (edit && edit.id && Object.prototype.hasOwnProperty.call(state.exportRedlineDecisions, edit.id)) {
    return state.exportRedlineDecisions[edit.id] !== false;
  }
  return clauseExportIncluded(edit.clause_id);
}

function effectiveReviewRedlines() {
  return state.reviewRedlines
    .filter(redlineExportIncluded)
    .map(applyTemplateSelectionToRedline);
}

function applyTemplateSelectionToRedline(edit) {
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
  return state.reviewClauses.find((item) => item.id === state.selectedReviewClauseId);
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
}

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

function renderStudioClauseLane() {
  if (!studioClauseLane) return;

  const sourceClauses = getDisplayClauses();

  if (!sourceClauses.length) {
    studioClauseLane.innerHTML = '<div class="studio-empty">Loading clauses</div>';
    return;
  }

  studioClauseLane.innerHTML = sourceClauses
    .map((clause) => {
      const selected = clause.id === state.selectedReviewClauseId ? "selected" : "";
      const status = clauseStatus(clause);
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

function renderClauseEvidenceList(paragraphs) {
  const list = Array.isArray(paragraphs) ? paragraphs.filter((paragraph) => paragraph && paragraph.text) : [];
  if (!list.length) return "";
  return `
    <div class="evidence-list">
      ${list.map((paragraph, index) => {
        const paragraphNumber = paragraph.index || paragraph.source_index || index + 1;
        return `
          <figure class="evidence-item">
            <figcaption>Paragraph ${escapeHtml(paragraphNumber)}</figcaption>
            <p>${escapeHtml(paragraph.text)}</p>
          </figure>
        `;
      }).join("")}
    </div>
  `;
}

function renderClauseAiEvidenceList(spans) {
  const list = Array.isArray(spans) ? spans.filter(Boolean) : [];
  if (!list.length) return "";
  return `<div class="evidence-list">${list.map(renderAiCitation).join("")}</div>`;
}

function renderClauseAssessmentBlocks({ assessment, evidence = "", note = "", signals = "" }) {
  return `
    <div class="studio-detail-block assessment-block">
      <small>Assessment</small>
      <p>${escapeHtml(assessment)}</p>
      ${signals}
    </div>
    ${(!signals && evidence) ? `
      <div class="studio-detail-block studio-detail-evidence">
        <small>Evidence</small>
        ${evidence}
      </div>
    ` : ""}
    ${note ? `
      <div class="studio-detail-block review-note-block">
        <small>Review note</small>
        <p>${escapeHtml(note)}</p>
      </div>
    ` : ""}
  `;
}

// Builds the heart of the panel around the active AI-first assessment. The
// deterministic result is no longer presented as a competing counterchecker.
function renderClauseExplanation(clause) {
  const analysis = clause && typeof clause.ai_review_analysis === "object" ? clause.ai_review_analysis : null;
  const findingText = clause.reason || clause.finding || "Clause review available.";
  const aiReason = analysis ? String(analysis.ai_reason || analysis.reason || "").trim() : "";
  const detDecision = String(analysis?.deterministic_decision || clause.decision || "").trim().toLowerCase();
  const aiDecision = String(analysis?.ai_decision || "").trim().toLowerCase();
  const isDisagreement = Boolean(analysis) && Boolean(aiReason)
    && (analysis.disagreement === true || (aiDecision && detDecision && aiDecision !== detDecision));

  const allDetParas = Array.isArray(clause.evidence_paragraphs)
    ? clause.evidence_paragraphs.filter((paragraph) => paragraph && paragraph.text)
    : [];
  const aiSpans = Array.isArray(analysis?.cited_spans) ? analysis.cited_spans.filter(Boolean) : [];
  const evidence = aiSpans.length ? renderClauseAiEvidenceList(aiSpans) : renderClauseEvidenceList(allDetParas);
  const signals = renderEvidenceSignalsBlock(clause);

  if (isDisagreement) {
    return renderClauseAssessmentBlocks({
      assessment: aiReason || findingText,
      evidence,
      signals,
      note: "AI assessment and deterministic validation recorded different outcomes. Treat the assessment as the review verdict; the validation result is audit context.",
    });
  }

  // A clause can land on REVIEW because the AI assessment could not be trusted
  // enough to close it automatically.
  const verdict = clauseStatus(clause);
  const aiStatus = analysis ? String(analysis.status || "").trim().toLowerCase() : "";
  const deterministicDecision = String(analysis?.deterministic_decision || "").trim().toLowerCase();
  const technicalEscalation = verdict.tone === "review"
    && deterministicDecision && deterministicDecision !== "review"
    && ["invalid", "low_confidence", "error"].includes(aiStatus);
  if (technicalEscalation) {
    const detail = aiStatus === "low_confidence"
      ? "the AI assessment was not confident enough to confirm it"
      : aiStatus === "invalid"
        ? "the AI assessment cited evidence that could not be verified in the document"
        : "the AI assessment was unavailable";
    return renderClauseAssessmentBlocks({
      assessment: `This clause was escalated for human review because ${detail}.`,
      evidence,
      signals,
      note: findingText,
    });
  }

  const aiAgrees = analysis && String(analysis.status || "").toLowerCase() === "confirmed"
    ? " AI assessment confirmed this finding."
    : "";
  if (verdict.passes) {
    return renderClauseAssessmentBlocks({
      assessment: `${findingText}${aiAgrees}`,
      evidence,
      signals,
    });
  }

  return renderClauseAssessmentBlocks({
    assessment: `${findingText}${aiAgrees}`,
    evidence,
    signals,
  });
}

// First-class Assessment headline (the folded Decision). The clause's decision
// state (PASS/REVIEW/FAIL) and the issue type ARE the finding, so they lead the
// panel as a tone-coded headline rather than sitting in a numbered audit step.
// The reasoning prose follows from renderClauseExplanation directly beneath.
function renderClauseFindingHeadline(clause, status) {
  const decisionLabel = (status.pillLabel || status.issueLabel || "").toUpperCase();
  return `
    <div class="studio-detail-block assessment-headline ${escapeHtml(status.tone)}">
      <div class="assessment-headline-row">
        <small>Assessment</small>
        <span class="assessment-decision-pill ${escapeHtml(status.tone)}">${escapeHtml(decisionLabel)}</span>
      </div>
      <p class="assessment-issue-type">${escapeHtml(status.issueLabel)}</p>
    </div>
  `;
}

function renderStudioDetail() {
  updateReviewInspectorTabs();
  if (state.reviewInspectorView === "structure") {
    reviewStructureController.render();
    return;
  }
  const clause = getSelectedReviewClause();
  if (!clause) {
    studioDetailPanel.innerHTML = "";
    return;
  }
  const status = clauseStatus(clause);
  const findingHeadline = renderClauseFindingHeadline(clause, status);
  const explanation = renderClauseExplanation(clause);
  const rationale = clause.rationale || clause.requirement || "";
  // The "Based on" grounding surface (citation / absence / ungrounded) sits
  // right under the explanation; it self-gates to "" until citation/grounding
  // data is present.
  const citation = renderClauseCitationBlock(clause);
  const playbookPosition = renderClausePlaybookPositionBlock(clause);
  const proposedRedlines = renderProposedRedlinesBlock(clause);
  // Audit/context detail beneath the primary finding, gathered into a single
  // collapsible Reasoning trail (#22). Self-gates to "" when the clause carries
  // no reason codes / evidence signals / audit trace.
  const reasoningTrail = renderReasoningTrailBlock(clause);
  const activeStatus = renderActiveClauseStatusToggle(clause, status);
  const commentBlock = renderClauseCommentBlock(clause);
  studioDetailPanel.innerHTML = `
    <div class="studio-detail-heading active-clause-heading">
      <div>
        <small>Active clause</small>
        <h3>${escapeHtml(clauseDisplayName(clause))}${clauseEngineBadge(clause)}</h3>
      </div>
      ${activeStatus}
    </div>
    <div class="studio-detail-stack">
      ${findingHeadline}
      ${explanation}
      <div class="studio-detail-block rationale-block"><small>Rationale</small><p>${escapeHtml(rationale || "No playbook rationale recorded.")}</p></div>
      ${citation}
      ${playbookPosition}
      ${proposedRedlines}
      ${reasoningTrail}
      ${commentBlock}
    </div>
  `;
  bindExportDecisionControls(studioDetailPanel);
  bindTemplateOptionControls(studioDetailPanel);
  bindReviewAcknowledgementControls(studioDetailPanel);
  bindReviewCommentControls(studioDetailPanel);
  bindReasoningTrailControls(studioDetailPanel);
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

// Single "Based on" grounding surface for a clause, driven by the AI-first
// review path's clause.citation (the first grounded structured-evidence quote)
// and clause.grounding.status. The older crosscheck path's cited_spans are
// already shown in the explanation's Evidence block (renderClauseExplanation),
// so this block deliberately does NOT fall back to them — that would double the
// same quotes. Returns "" when no citation/grounding data is present (a no-op
// until those fields land), so existing reviews are unaffected and there is
// never a second citation surface.
function renderClauseCitationBlock(clause) {
  if (!clause || typeof clause !== "object") return "";
  const grounding = typeof clause.grounding === "object" && clause.grounding ? clause.grounding : null;
  const status = grounding ? String(grounding.status || "").trim().toLowerCase() : "";
  const confidence = renderClauseConfidence(clause, grounding);

  const citation = typeof clause.citation === "object" && clause.citation ? clause.citation : null;
  const citationQuote = citation ? String(citation.quote || "").trim() : "";

  // The matched quote now lives inline in the Assessment evidence, so "Based on"
  // no longer repeats it for a grounded clause — it surfaces only the grounding
  // confidence (when present), plus the absence/ungrounded states below.
  if (citationQuote) {
    if (!confidence) return "";
    return `
      <div class="studio-detail-block clause-citation-block grounded">
        <small>Based on</small>
        ${confidence}
      </div>
    `;
  }

  // Non-quote grounding states only the AI-first path reports.
  if (status === "absence") {
    return `
      <div class="studio-detail-block clause-citation-block absence">
        <small>Based on</small>
        <p>Grounded in the absence of this clause from the document.</p>
        ${confidence}
      </div>
    `;
  }
  if (status === "ungrounded") {
    return `
      <div class="studio-detail-block clause-citation-block ungrounded">
        <small>Based on</small>
        <p>The AI assessment did not ground this finding in any quotable text, so it was escalated for human review.</p>
        ${confidence}
      </div>
    `;
  }

  return "";
}

// 2.2: a grounding/confidence read-out for the "Based on" surface. Prefers an
// explicit grounding.confidence (0–1 or 0–100), then the AI assessment's
// ai_confidence, and degrades to "" when neither is present so existing reviews
// are unaffected. The level bucket drives the styling so reviewers can scan
// high/medium/low at a glance.
function renderClauseConfidence(clause, grounding = null) {
  const ground = grounding || (clause && typeof clause.grounding === "object" ? clause.grounding : null);
  const analysis = clause && typeof clause.ai_review_analysis === "object" ? clause.ai_review_analysis : null;
  const raw = ground && ground.confidence != null
    ? ground.confidence
    : analysis && analysis.ai_confidence != null
      ? analysis.ai_confidence
      : null;
  if (raw == null) return "";
  const numeric = Number(raw);
  if (!Number.isFinite(numeric)) return "";
  const ratio = numeric > 1 ? numeric / 100 : numeric;
  const clamped = Math.max(0, Math.min(1, ratio));
  const percent = Math.round(clamped * 100);
  const level = clamped >= 0.75 ? "high" : clamped >= 0.5 ? "medium" : "low";
  return `
    <div class="clause-confidence ${level}" data-confidence-level="${level}">
      <small>Confidence</small>
      <div class="clause-confidence-meter" role="img" aria-label="Confidence ${percent} percent (${level})">
        <span class="clause-confidence-fill" style="width:${percent}%"></span>
      </div>
      <span class="clause-confidence-value">${percent}%</span>
    </div>
  `;
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
    || (Array.isArray(clause.approved_positions) ? clause.approved_positions : []);
  const approvedPositions = approvedSource
    .map((position) => String(position || "").trim())
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
      || "",
  ).trim();
  if (!wording && !approvedPositions.length && !preferred) return null;
  return { approvedPositions, preferred, wording };
}

function renderClausePlaybookPositionBlock(clause) {
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
  return steps.filter((step) => {
    const name = String(step.name || "").trim().toLowerCase();
    const outcome = String(step.outcome || "").trim().toLowerCase();
    if (name === "decision") return false;
    if (name === "ai assessment normalization" || outcome === "normalized") return false;
    return true;
  });
}

function renderAuditTraceBlock(clause) {
  const steps = auditTraceTrailSteps(clause);
  if (!steps.length) return "";
  return `
    <div class="studio-detail-block audit-trace-block">
      <small>Audit trace</small>
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
  if (!auditTrace) return "";
  const open = reasoningTrailOpenForClause(clause?.id) ? " open" : "";
  return `
    <details class="studio-detail-block reasoning-trail-block" data-reasoning-trail-clause-id="${escapeHtml(clause?.id || "")}"${open}>
      <summary class="reasoning-trail-summary">
        <span>Reasoning trail</span>
        <span class="reasoning-trail-hint">Audit detail</span>
      </summary>
      <div class="reasoning-trail-body">
        ${auditTrace}
      </div>
    </details>
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

function renderProposedRedlinesBlock(clause) {
  const redlines = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id);
  if (!redlines.length) {
    return clauseStatus(clause).requiresAttention
      ? `
        <div class="studio-detail-block proposed-redline-block muted">
          <small>Proposed redline</small>
          <p>No proposed redline was recorded for this clause.</p>
        </div>
      `
      : "";
  }
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

function renderDetailRedlineEdit(edit, clauseRationale = null) {
  const included = redlineExportIncluded(edit);
  const selectedEdit = applyTemplateSelectionToRedline(edit);
  const replacement = renderRedlineReplacement(selectedEdit, "p");
  const original = selectedEdit.action === "insert_after_paragraph"
    ? renderRedlineAnchor(selectedEdit)
    : `<p class="redline-original">${escapeHtml(selectedEdit.original_text || "")}</p>`;
  return `
    <div class="detail-redline-edit ${included ? "included" : "ignored"}">
      <div class="detail-redline-head">
        <span class="redline-label">${escapeHtml(redlineActionLabel(selectedEdit))}</span>
        <span class="detail-export-controls" role="group" aria-label="Redline decision">
          <button class="export-choice ${included ? "active" : ""}" type="button" data-export-redline-id="${escapeHtml(edit.id)}" data-export-decision="include" aria-pressed="${included ? "true" : "false"}">Include</button>
          <button class="export-choice ${!included ? "active" : ""}" type="button" data-export-redline-id="${escapeHtml(edit.id)}" data-export-decision="ignore" aria-pressed="${!included ? "true" : "false"}">Ignore</button>
        </span>
      </div>
      ${original}
      ${replacement}
      ${renderRedlineTemplateOptions(selectedEdit)}
      ${renderRedlineRationaleBlock(selectedEdit, clauseRationale)}
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

  return `
    <div class="redline-options">
      <span class="redline-options-title">Jurisdiction options</span>
      ${options.map((option) => `
        <button class="redline-option ${option.selected ? "selected" : ""}" type="button" data-redline-edit-id="${escapeHtml(edit.id)}" data-redline-option-id="${escapeHtml(option.id || "")}" aria-pressed="${option.selected ? "true" : "false"}">
          <span class="redline-option-dot" aria-hidden="true"></span>
          <span class="redline-option-copy">
            <strong>${escapeHtml(displayRedlineOptionLabel(option))}</strong>
            <span>${escapeHtml(option.text || option.replacement_text || option.insert_text || "")}</span>
          </span>
        </button>
      `).join("")}
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
      const paragraphId = button.dataset.addParagraphCommentId;
      const existing = normalizeReviewComments(state.reviewComments)
        .find((comment) => comment.scope === "paragraph" && comment.paragraph_id === paragraphId);
      openParagraphCommentComposer({
        existingText: existing?.text || "",
        onSave: (text) => setParagraphReviewComment(paragraphId, text),
        paragraphId,
        title: "Paragraph comment",
      });
    });
  });
  container.querySelectorAll("[data-add-selection-comment-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const paragraphId = button.dataset.addSelectionCommentId;
      const selectionInfo = selectedTextInParagraph(paragraphId);
      if (!selectionInfo?.selectedText) {
        setFileMeta("Select text in this paragraph before adding a selected-text comment");
        return;
      }
      const existing = normalizeReviewComments(state.reviewComments)
        .find((comment) => (
          comment.scope === "selection"
          && comment.paragraph_id === paragraphId
          && Number(comment.selection_start) === Number(selectionInfo.startOffset)
          && Number(comment.selection_end) === Number(selectionInfo.endOffset)
        ));
      openParagraphCommentComposer({
        existingText: existing?.text || "",
        onSave: (text) => setSelectedTextReviewComment(paragraphId, selectionInfo, text),
        paragraphId,
        selectedText: selectionInfo.selectedText,
        title: "Selected text comment",
      });
    });
  });
}

function closeParagraphCommentComposers() {
  studioDocumentRender?.querySelectorAll(".paragraph-comment-composer").forEach((composer) => {
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

function openParagraphCommentComposer({
  existingText = "",
  onSave,
  paragraphId,
  selectedText = "",
  title,
}) {
  const paragraph = studioDocumentRender?.querySelector(
    `[data-paragraph-id="${cssEscape(paragraphId)}"]`,
  );
  if (!paragraph || typeof onSave !== "function") return;

  clearSelectionCommentAffordances();
  closeParagraphCommentComposers();
  paragraph.classList.add("has-comment-composer");

  const composer = document.createElement("div");
  composer.className = "paragraph-comment-composer";
  composer.setAttribute("contenteditable", "false");
  composer.addEventListener("click", (event) => event.stopPropagation());

  const label = document.createElement("label");
  const inputId = `paragraph-comment-input-${Date.now()}`;
  label.setAttribute("for", inputId);
  label.textContent = title || "Comment";
  composer.append(label);

  if (selectedText) {
    const excerpt = document.createElement("p");
    excerpt.className = "paragraph-comment-selection";
    excerpt.textContent = selectedText;
    composer.append(excerpt);
  }

  const input = document.createElement("textarea");
  input.id = inputId;
  input.className = "paragraph-comment-input";
  input.rows = 3;
  input.placeholder = "Write a comment for Word export";
  input.value = existingText;
  composer.append(input);

  const actions = document.createElement("div");
  actions.className = "paragraph-comment-actions";

  const saveButton = document.createElement("button");
  saveButton.className = "paragraph-comment-save";
  saveButton.type = "button";
  saveButton.textContent = "Save";

  const cancelButton = document.createElement("button");
  cancelButton.className = "paragraph-comment-cancel";
  cancelButton.type = "button";
  cancelButton.textContent = "Cancel";

  actions.append(saveButton, cancelButton);
  composer.append(actions);
  paragraph.append(composer);

  cancelButton.addEventListener("click", (event) => {
    event.stopPropagation();
    closeParagraphCommentComposers();
  });
  saveButton.addEventListener("click", (event) => {
    event.stopPropagation();
    const text = input.value.trim();
    if (!text) {
      setFileMeta("Write a comment before saving");
      input.focus();
      return;
    }
    onSave(text);
    setFileMeta("Comment saved for Word export");
  });

  requestAnimationFrame(() => {
    input.focus({ preventScroll: true });
    input.setSelectionRange(input.value.length, input.value.length);
  });
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
  bindParagraphCommentControls(studioDocumentRender);

  showStudioDocumentRender();
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

  return renderOriginalUnavailableFallback(renderState, status);
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
