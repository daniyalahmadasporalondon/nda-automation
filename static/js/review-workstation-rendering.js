function renderResult(result, reviewedText) {
  pendingReviewSendMatterId = null;
  state.latestReviewResult = result;
  state.reviewClauses = result.clauses || [];
  state.reviewParagraphs = result.paragraphs || [];
  state.reviewOriginalParagraphs = snapshotReviewParagraphs(state.reviewParagraphs);
  state.reviewExportOriginalParagraphs = snapshotReviewParagraphs(state.reviewParagraphs);
  state.reviewRedlines = result.redline_edits || [];
  state.reviewComments = [];
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  state.redlineDraft = null;
  state.redlineDraftDirty = false;
  resetReviewEditHistory();
  state.reviewSourceText = reviewedText || studioNdaText.value.trim();
  state.clauseJumpIndexes = {};
  state.selectedReviewClauseId =
    state.reviewClauses.find((clause) => clauseStatus(clause).requiresAttention)?.id || state.reviewClauses[0]?.id || null;
  renderStudioResult(result);
  updateExportButtonState();
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
  showStudioSourceEditor();
  studioMatchSummary.textContent = `0/${getClauseTotal()}`;
  studioResultMark.textContent = "-";
  studioResultMark.className = "";
  studioOverallTitle.textContent = "Awaiting review";
  studioResultMeta.textContent = "No hard-clause review has run yet.";
  resetReviewEditHistory();
  if (state.reviewInspectorView === "structure") {
    reviewStructureController.render();
  } else {
    studioDetailPanel.innerHTML = `
      <p>No review yet.</p>
    `;
  }
  updateReviewInspectorTabs();
  updateExportButtonState();
  renderStudioClauseLane();
}

function updateExportButtonState() {
  const canExport = state.reviewClauses.length && (studioNdaText.value.trim() || state.reviewSourceText.trim());
  if (studioExportButton) {
    studioExportButton.disabled = !canExport;
  }
  updateReviewButtonState();
  if (!studioSendButton) {
    updateRedlineDraftControls();
    return;
  }
  const hasSendableMatter = Boolean(state.selectedMatter?.id);
  studioSendButton.hidden = !hasSendableMatter;
  const sendBlockReason = state.selectedMatter?.id ? MatterUtils.gmailSendBlock(state.selectedMatter, state.gmailStatus) : "";
  const canSend = Boolean(canExport && hasSendableMatter && !sendBlockReason);
  studioSendButton.disabled = !canSend;
  if (!canSend) {
    pendingReviewSendMatterId = null;
    const sendLabel = sendBlockReason ? MatterUtils.gmailSendButtonLabel(sendBlockReason) : "Send Redline";
    setStudioSendButtonLabel(sendLabel, sendBlockReason || sendLabel);
  } else {
    pendingReviewSendMatterId = null;
    setStudioSendButtonLabel("Send Redline");
  }
  updateRedlineDraftControls();
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

function updateReviewButtonState() {
  if (!studioReviewButton) return;
  const hasReviewableSource = Boolean((studioNdaText.value || "").trim() || state.selectedDocument);
  const hasReviewedDocument = Boolean(
    state.latestReviewResult
      || state.reviewClauses.length
      || state.reviewParagraphs.length
  );
  studioReviewButton.hidden = !hasReviewableSource || hasReviewedDocument;
}

function renderStudioResult(result) {
  const clauses = result.clauses || [];
  renderStudioSummary(clauses);
  renderStudioClauseLane();
  renderStudioDetail();
  renderStudioDocumentHighlights();
}

function renderStudioSummary(clauses) {
  const passedCount = clauses.filter((clause) => clauseStatus(clause).passes).length;
  const reviewCount = clauses.filter((clause) => clauseStatus(clause).needsReview).length;
  const failedCount = clauses.filter((clause) => clauseStatus(clause).fails).length;
  studioMatchSummary.textContent = `${passedCount}/${getClauseTotal(clauses)}`;
  studioResultMark.textContent = failedCount ? "CHECK" : reviewCount ? "REVIEW" : "PASS";
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
    return `${failedCount} hard ${failedCount === 1 ? "clause needs" : "clauses need"} checking.`;
  }
  if (reviewCount) {
    return `${reviewCount} ${reviewCount === 1 ? "clause needs" : "clauses need"} human review before send.`;
  }
  return "All hard clauses are currently satisfied.";
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

function renderClauseExportControls(clause, canDecide, included) {
  if (!canDecide) return "";
  return `
    <span class="studio-export-controls" role="group" aria-label="${escapeHtml(clause.name)} export decision">
      <button class="export-choice ${included ? "active" : ""}" type="button" data-export-clause-id="${escapeHtml(clause.id)}" data-export-decision="include" aria-pressed="${included ? "true" : "false"}">Include</button>
      <button class="export-choice ${!included ? "active" : ""}" type="button" data-export-clause-id="${escapeHtml(clause.id)}" data-export-decision="ignore" aria-pressed="${!included ? "true" : "false"}">Ignore</button>
    </span>
  `;
}

function renderClauseCommentState(clause) {
  if (!hasReviewResults() || !clauseReviewComment(clause.id)) return "";
  return '<span class="studio-comment-state">Comment</span>';
}

function renderClauseCommentBlock(clause) {
  if (!hasReviewResults()) return "";
  const comment = clauseReviewComment(clause.id);
  return `
    <div class="studio-detail-block comment-block">
      <small>Word comment</small>
      <textarea class="review-comment-input" data-review-comment-clause-id="${escapeHtml(clause.id)}" rows="4" placeholder="Leave a comment for Word export">${escapeHtml(comment?.text || "")}</textarea>
    </div>
  `;
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
  applyDraftTemplateSelections(state.redlineDraft.template_selections);
  applyDraftManualRedlines(state.redlineDraft.manual_redline_edits);
  applyDraftReviewComments(state.redlineDraft.review_comments);
  renderStudioResult({ clauses: state.reviewClauses });
  resetReviewEditHistory();
  updateRedlineDraftControls();
}

function resetCurrentRedlineDraftToDefaults() {
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
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

function renderStudioClauseLane() {
  if (!studioClauseLane) return;

  const sourceClauses = getDisplayClauses();

  if (!sourceClauses.length) {
    studioClauseLane.innerHTML = '<div class="studio-empty">Loading clauses</div>';
    return;
  }

  studioClauseLane.innerHTML = sourceClauses
    .map((clause, index) => {
      const selected = clause.id === state.selectedReviewClauseId ? "selected" : "";
      const status = clauseStatus(clause);
      const redlineCount = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id).length;
      const canDecide = hasReviewResults() && redlineCount > 0;
      const included = clauseExportIncluded(clause.id);
      const exportState = renderClauseExportState(clause, canDecide, included);
      const exportControls = renderClauseExportControls(clause, canDecide, included);
      const commentState = renderClauseCommentState(clause);
      const finding = hasReviewResults()
        ? `<span class="studio-clause-finding">${escapeHtml(clause.reason || clause.finding || "Clause review available.")}</span>`
        : "";
      const pill = hasReviewResults()
        ? `<strong class="studio-issue-pill ${status.tone}">${status.pillLabel}</strong>`
        : "";
      const selectable = hasReviewResults()
        ? `
          <button class="studio-clause-select" type="button" data-studio-lane-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}">
            <span class="studio-clause-dot ${status.dotTone}"></span>
            <strong class="studio-clause-number">${index + 1}</strong>
            <span class="studio-clause-title">${escapeHtml(clause.name)}</span>
            ${pill}
            ${finding}
            ${exportState}
            ${commentState}
          </button>
        `
        : `
          <div class="studio-clause-select">
            <span class="studio-clause-dot ${status.dotTone}"></span>
            <strong class="studio-clause-number">${index + 1}</strong>
            <span class="studio-clause-title">${escapeHtml(clause.name)}</span>
          </div>
        `;
      return `
        <article class="studio-clause-item ${selected} ${status.tone}">
          ${selectable}
          ${exportControls}
        </article>
      `;
    })
    .join("");

  bindClauseSelection(studioClauseLane, "[data-studio-lane-id]", "studioLaneId");
  bindExportDecisionControls(studioClauseLane);
}

function renderStudioDetail() {
  updateReviewInspectorTabs();
  if (state.reviewInspectorView === "structure") {
    reviewStructureController.render();
    return;
  }
  const clause = getSelectedReviewClause();
  if (!clause) {
    studioDetailPanel.innerHTML = "<p>No review yet.</p>";
    return;
  }
  const status = clauseStatus(clause);
  const whyText = clause.reason || clause.finding || "Clause review available.";
  const excerpt = renderEvidenceBlock(clause);
  const evidenceSignalsBlock = renderEvidenceSignalsBlock(clause);
  const auditTraceBlock = renderAuditTraceBlock(clause);
  const fixBlock = status.requiresAttention && clause.what_to_fix
    ? `<div class="studio-detail-block fix-block"><small>${status.needsReview ? "What to verify" : "What to fix"}</small><p>${escapeHtml(clause.what_to_fix)}</p></div>`
    : "";
  const rationaleBlock = clause.rationale
    ? `<div class="studio-detail-block rationale-block"><small>Playbook rationale</small><p>${escapeHtml(clause.rationale)}</p></div>`
    : "";
  const evidenceGuidanceBlock = clause.evidence_guidance
    ? `<div class="studio-detail-block evidence-guidance-block"><small>Evidence guidance</small><p>${escapeHtml(clause.evidence_guidance)}</p></div>`
    : "";
  const redlineEdits = getSelectedRedlineEdits();
  const selectedClauseRedlineCount = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id).length;
  const exportDecisionBlock = selectedClauseRedlineCount
    ? `
      <div class="studio-detail-block export-decision-block">
        <small>Export decision</small>
        <div class="detail-export-controls" role="group" aria-label="${escapeHtml(clause.name)} export decision">
          <button class="export-choice ${clauseExportIncluded(clause.id) ? "active" : ""}" type="button" data-export-clause-id="${escapeHtml(clause.id)}" data-export-decision="include" aria-pressed="${clauseExportIncluded(clause.id) ? "true" : "false"}">Include redline</button>
          <button class="export-choice ${!clauseExportIncluded(clause.id) ? "active" : ""}" type="button" data-export-clause-id="${escapeHtml(clause.id)}" data-export-decision="ignore" aria-pressed="${!clauseExportIncluded(clause.id) ? "true" : "false"}">Ignore</button>
        </div>
      </div>
    `
    : "";
  const redlineBlock = redlineEdits.length
    ? `
      <div class="studio-detail-block redline-block">
        <small>Proposed redline</small>
        ${redlineEdits.map(renderDetailRedlineEdit).join("")}
      </div>
    `
    : "";
  const acceptableLanguage = clause.acceptable_language
    ? `<div class="studio-detail-block"><small>Acceptable language</small><p>${escapeHtml(clause.acceptable_language)}</p></div>`
    : "";
  const commentBlock = renderClauseCommentBlock(clause);
  studioDetailPanel.innerHTML = `
    <div class="studio-detail-heading">
      <h3>${escapeHtml(clause.name)}</h3>
      <span class="status ${status.tone}">${escapeHtml(status.pillLabel)}</span>
    </div>
    <div class="studio-detail-stack">
      <div class="studio-detail-block requirement-block">
        <small>Requirement</small>
        <p>${escapeHtml(clause.requirement)}</p>
      </div>
      ${excerpt}
      ${evidenceSignalsBlock}
      ${auditTraceBlock}
      <div class="studio-detail-block issue-block ${escapeHtml(status.tone)}">
        <small>Issue type</small>
        <p>${escapeHtml(status.issueLabel)}</p>
      </div>
      <div class="studio-detail-block finding-block">
        <small>Why</small>
        <p>${escapeHtml(whyText)}</p>
      </div>
      ${rationaleBlock}
      ${evidenceGuidanceBlock}
      ${fixBlock}
      ${commentBlock}
      ${exportDecisionBlock}
      ${redlineBlock}
      <div class="studio-detail-block">
        <small>Backend result</small>
        <p>${escapeHtml(status.resultLabel)}</p>
      </div>
      ${acceptableLanguage}
    </div>
  `;
  bindExportDecisionControls(studioDetailPanel);
  bindTemplateOptionControls(studioDetailPanel);
  bindReviewCommentControls(studioDetailPanel);
}

function renderEvidenceSignalsBlock(clause) {
  const records = Array.isArray(clause?.structured_evidence)
    ? clause.structured_evidence.filter((record) => record && record.paragraph_id)
    : [];
  if (!records.length) return "";
  return `
    <div class="studio-detail-block evidence-signals-block">
      <small>Evidence signals</small>
      <div class="evidence-signal-list">
        ${records.slice(0, 5).map((record) => {
          const terms = Array.isArray(record.matched_terms)
            ? record.matched_terms.filter(Boolean).slice(0, 5)
            : [];
          const paragraphLabel = record.paragraph_index || record.source_index || record.paragraph_id;
          const signal = record.signal_type || record.decision || "evidence";
          const bucket = record.rule_bucket || record.issue_type || "none";
          const matchedText = record.matched_text || record.text || "";
          return `
            <article class="evidence-signal-item">
              <header>
                <strong>${escapeHtml(signal)}</strong>
                <span>Paragraph ${escapeHtml(paragraphLabel)} · ${escapeHtml(bucket)}</span>
              </header>
              <p>${escapeHtml(matchedText)}</p>
              ${terms.length ? `<div class="evidence-signal-terms">${terms.map((term) => `<span>${escapeHtml(term)}</span>`).join("")}</div>` : ""}
            </article>
          `;
        }).join("")}
      </div>
    </div>
  `;
}

function renderAuditTraceBlock(clause) {
  const trace = clause?.audit_trace && typeof clause.audit_trace === "object" ? clause.audit_trace : null;
  const steps = Array.isArray(trace?.steps) ? trace.steps.filter((step) => step && step.name) : [];
  if (!trace || !steps.length) return "";
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

function renderDetailRedlineEdit(edit) {
  const replacement = renderRedlineReplacement(edit, "p");
  const original = edit.action === "insert_after_paragraph"
    ? renderRedlineAnchor(edit)
    : `<p class="redline-original">${escapeHtml(edit.original_text || "")}</p>`;
  return `
    <div class="detail-redline-edit">
      <span class="redline-label">${escapeHtml(redlineActionLabel(edit))}</span>
      ${original}
      ${replacement}
      ${renderRedlineTemplateOptions(edit)}
    </div>
  `;
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
          <strong>${escapeHtml(option.label || "Option")}${option.selected ? " - Default" : ""}</strong>
          <span>${escapeHtml(option.text || option.replacement_text || option.insert_text || "")}</span>
        </button>
      `).join("")}
    </div>
  `;
}

function bindTemplateOptionControls(container) {
  container.querySelectorAll("[data-redline-edit-id][data-redline-option-id]").forEach((button) => {
    button.addEventListener("click", () => {
      setRedlineTemplateSelection(button.dataset.redlineEditId, button.dataset.redlineOptionId);
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
    showStudioSourceEditor();
    return;
  }

  if (!state.reviewParagraphs.length) {
    showStudioSourceEditor();
    return;
  }
  const viewMode = state.documentViewMode || VIEW_MODE_REDLINE;
  studioDocumentRender.innerHTML = renderReviewDocument({
    clauses: state.reviewClauses,
    comments: currentReviewComments(),
    originalParagraphs: manualRedlineBaselineParagraphs(),
    paragraphs: state.reviewParagraphs,
    redlines: effectiveReviewRedlines(),
    selectedClauseId: state.selectedReviewClauseId,
    viewMode,
  });

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
