import { clausePasses, clauseStatus } from "./modules/clause-status.mjs";

export const REDLINE_DELETE_PARAGRAPH_ACTION = "delete_paragraph";
export const REDLINE_INSERT_AFTER_PARAGRAPH_ACTION = "insert_after_paragraph";

export function snapshotReviewParagraphs(paragraphs) {
  return (paragraphs || []).map((paragraph) => {
    const snapshot = {
      id: paragraph.id,
      index: paragraph.index,
      text: String(paragraph.text || ""),
    };
    if (paragraph.source_index !== undefined) snapshot.source_index = paragraph.source_index;
    if (paragraph.source_part !== undefined) snapshot.source_part = paragraph.source_part;
    if (paragraph.alignment !== undefined) snapshot.alignment = paragraph.alignment;
    if (paragraph.font !== undefined) snapshot.font = paragraph.font;
    if (paragraph.fontSize !== undefined) snapshot.fontSize = paragraph.fontSize;
    if (Array.isArray(paragraph.runs)) snapshot.runs = paragraph.runs.map((run) => ({ ...run }));
    return snapshot;
  });
}

export function paragraphsAlignWithBaseline(paragraphs, baseline) {
  if (!Array.isArray(paragraphs) || !Array.isArray(baseline) || !baseline.length) return false;
  if (paragraphs.length !== baseline.length) return false;
  return paragraphs.every((paragraph, index) => String(paragraph.id || "") === String(baseline[index]?.id || ""));
}

export function defaultExportClauseDecisions(clauses, redlines) {
  const clausesWithRedlines = new Set((redlines || []).map((edit) => edit.clause_id).filter(Boolean));
  return Object.fromEntries((clauses || []).map((clause) => [
    clause.id,
    clausesWithRedlines.has(clause.id),
  ]));
}

export function defaultRedlineTemplateSelections(redlines) {
  const selections = {};
  (redlines || []).forEach((edit) => {
    const selected = (edit.template_options || []).find((option) => option.selected) || (edit.template_options || [])[0];
    if (selected?.id) selections[edit.id] = selected.id;
  });
  return selections;
}

export function initialSelectedReviewClauseId(clauses) {
  return (clauses || []).find((clause) => clauseStatus(clause).requiresAttention)?.id
    || (clauses || [])[0]?.id
    || null;
}

export function refreshedSelectedReviewClauseId(clauses, previousClauseId) {
  const list = clauses || [];
  return list.some((clause) => clause.id === previousClauseId)
    ? previousClauseId
    : list.find((clause) => !clausePasses(clause))?.id || list[0]?.id || null;
}

export function loadReviewResultState(state, result = {}, { reviewedText = "", documentRender = null } = {}) {
  state.reviewDocumentRender = documentRender;
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
  state.reviewSourceText = reviewedText;
  state.clauseJumpIndexes = {};
  state.selectedReviewClauseId = initialSelectedReviewClauseId(state.reviewClauses);
  return state;
}

export function applyViewerReviewDetectionState(state, result = {}, reviewedText = "") {
  const previousSelectedClauseId = state.selectedReviewClauseId;
  const previousExportDecisions = { ...(state.exportClauseDecisions || {}) };
  const previousTemplateSelections = { ...(state.redlineTemplateSelections || {}) };

  state.latestReviewResult = result;
  state.reviewClauses = result.clauses || [];
  state.reviewParagraphs = result.paragraphs || [];
  state.reviewOriginalParagraphs = snapshotReviewParagraphs(state.reviewParagraphs);
  if (!paragraphsAlignWithBaseline(state.reviewParagraphs, state.reviewExportOriginalParagraphs)) {
    state.reviewExportOriginalParagraphs = snapshotReviewParagraphs(state.reviewParagraphs);
  }
  state.reviewRedlines = result.redline_edits || [];
  state.reviewSourceText = reviewedText;
  state.clauseJumpIndexes = {};
  state.selectedReviewClauseId = refreshedSelectedReviewClauseId(state.reviewClauses, previousSelectedClauseId);
  reconcileExportDecisions(state, previousExportDecisions);
  reconcileTemplateSelections(state, previousTemplateSelections);
  return state;
}

export function reconcileExportDecisions(state, previousExportDecisions) {
  const clauseIds = new Set((state.reviewClauses || []).map((clause) => clause.id));
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  Object.entries(previousExportDecisions || {}).forEach(([clauseId, included]) => {
    if (clauseIds.has(clauseId)) state.exportClauseDecisions[clauseId] = Boolean(included);
  });
  return state.exportClauseDecisions;
}

export function reconcileTemplateSelections(state, previousTemplateSelections) {
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  (state.reviewRedlines || []).forEach((edit) => {
    const previousSelection = previousTemplateSelections?.[edit.id];
    if (previousSelection && (edit.template_options || []).some((option) => option.id === previousSelection)) {
      state.redlineTemplateSelections[edit.id] = previousSelection;
    }
  });
  return state.redlineTemplateSelections;
}

export function applyRedlineDraftState(state, draft, { baselineParagraphs = [] } = {}) {
  state.redlineDraft = draft && typeof draft === "object" ? draft : null;
  state.redlineDraftDirty = false;
  if (!state.redlineDraft) return { sourceTextChanged: false };

  applyDraftClauseDecisions(state, state.redlineDraft.clause_decisions);
  applyDraftRedlineDecisions(state, state.redlineDraft.redline_decisions);
  applyDraftTemplateSelections(state, state.redlineDraft.template_selections);
  applyDraftReviewedClauseIds(state, state.redlineDraft.reviewed_clause_ids);
  const sourceTextChanged = applyDraftManualRedlines(state, state.redlineDraft.manual_redline_edits, { baselineParagraphs });
  applyDraftReviewComments(state, state.redlineDraft.review_comments);
  return { sourceTextChanged };
}

export function resetRedlineDraftState(state, { baselineParagraphs = [] } = {}) {
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  state.exportRedlineDecisions = {};
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  state.reviewedClauseIds = {};
  state.reviewComments = [];
  state.reviewParagraphs = (state.reviewParagraphs || []).map((paragraph) => {
    const original = baselineParagraphs.find((item) => item.id === paragraph.id);
    return original ? { ...paragraph, text: original.text } : paragraph;
  });
  state.reviewSourceText = reviewSourceTextFromParagraphs(state.reviewParagraphs);
  state.redlineDraft = null;
  state.redlineDraftDirty = false;
  return state;
}

export function applyDraftClauseDecisions(state, decisions) {
  if (!decisions || typeof decisions !== "object") return state.exportClauseDecisions;
  Object.entries(decisions).forEach(([clauseId, included]) => {
    if ((state.reviewClauses || []).some((clause) => clause.id === clauseId)) {
      state.exportClauseDecisions[clauseId] = Boolean(included);
    }
  });
  return state.exportClauseDecisions;
}

export function applyDraftRedlineDecisions(state, decisions) {
  if (!decisions || typeof decisions !== "object") return state.exportRedlineDecisions;
  const validRedlineIds = new Set((state.reviewRedlines || []).map((edit) => edit.id));
  Object.entries(decisions).forEach(([redlineId, included]) => {
    if (validRedlineIds.has(redlineId)) {
      state.exportRedlineDecisions[redlineId] = Boolean(included);
    }
  });
  return state.exportRedlineDecisions;
}

export function applyDraftReviewedClauseIds(state, reviewedIds) {
  state.reviewedClauseIds = {};
  if (!reviewedIds || typeof reviewedIds !== "object") return state.reviewedClauseIds;
  Object.entries(reviewedIds).forEach(([clauseId, reviewed]) => {
    if ((state.reviewClauses || []).some((clause) => clause.id === clauseId)) {
      state.reviewedClauseIds[clauseId] = reviewed === true;
    }
  });
  return state.reviewedClauseIds;
}

export function applyDraftTemplateSelections(state, selections) {
  if (!selections || typeof selections !== "object") return state.redlineTemplateSelections;
  const validRedlineIds = new Set((state.reviewRedlines || []).map((edit) => edit.id));
  Object.entries(selections).forEach(([editId, optionId]) => {
    if (validRedlineIds.has(editId) && optionId) {
      state.redlineTemplateSelections[editId] = String(optionId);
    }
  });
  return state.redlineTemplateSelections;
}

export function applyDraftManualRedlines(state, manualRedlines, { baselineParagraphs = [] } = {}) {
  if (!Array.isArray(manualRedlines) || !manualRedlines.length) return false;
  const redlineByParagraph = new Map();
  manualRedlines.forEach((redline) => {
    if (redline?.paragraph_id) redlineByParagraph.set(String(redline.paragraph_id), redline);
  });
  state.reviewParagraphs = (state.reviewParagraphs || []).map((paragraph) => {
    const redline = redlineByParagraph.get(String(paragraph.id));
    if (!redline) return paragraph;
    const replacement = redline.action === REDLINE_DELETE_PARAGRAPH_ACTION ? "" : String(redline.replacement_text || "");
    return { ...paragraph, text: replacement };
  });
  state.reviewSourceText = reviewSourceTextFromParagraphs(state.reviewParagraphs);
  return true;
}

export function applyDraftReviewComments(state, reviewComments) {
  state.reviewComments = normalizeReviewComments(reviewComments);
  return state.reviewComments;
}

export function reviewSourceTextFromParagraphs(paragraphs) {
  return (paragraphs || [])
    .map((paragraph) => String(paragraph.text || "").trim())
    .filter(Boolean)
    .join("\n\n");
}

export function selectReviewClauseState(state, clauseId, { inspectorView = "clause" } = {}) {
  const previousInspectorView = state.reviewInspectorView;
  state.selectedReviewClauseId = clauseId;
  if (inspectorView && state.reviewInspectorView !== inspectorView) {
    state.reviewInspectorView = inspectorView;
  }
  return {
    inspectorViewChanged: previousInspectorView !== state.reviewInspectorView,
    previousInspectorView,
    selectedClauseId: state.selectedReviewClauseId,
  };
}

export function reviewedClauseMap(state) {
  if (!state.reviewedClauseIds || typeof state.reviewedClauseIds !== "object") {
    state.reviewedClauseIds = {};
  }
  return state.reviewedClauseIds;
}

export function reviewClauseIds(state) {
  return (state.reviewClauses || [])
    .filter((clause) => clauseStatus(clause).needsReview)
    .map((clause) => clause.id)
    .filter(Boolean);
}

export function clauseReviewAcknowledged(state, clauseId) {
  const reviewedMap = reviewedClauseMap(state);
  if (Object.prototype.hasOwnProperty.call(reviewedMap, clauseId)) {
    return reviewedMap[clauseId] === true;
  }
  return Boolean(state.selectedMatter?.human_reviewed);
}

export function humanReviewAcknowledged(state) {
  const ids = reviewClauseIds(state);
  return ids.length > 0 && ids.every((clauseId) => clauseReviewAcknowledged(state, clauseId));
}

export function toggleReviewAcknowledgement(state, { clauseId = "" } = {}) {
  const targetClauseId = String(clauseId || "");
  const targetClauseIds = targetClauseId ? [targetClauseId] : reviewClauseIds(state);
  if (!targetClauseIds.length) return null;

  const previousReviewedClauseIds = { ...reviewedClauseMap(state) };
  const previousMatter = state.selectedMatter ? { ...state.selectedMatter } : null;
  const previousMatterReviewed = Boolean(previousMatter?.human_reviewed);

  if (state.selectedMatter?.human_reviewed) {
    reviewClauseIds(state).forEach((id) => {
      if (!Object.prototype.hasOwnProperty.call(reviewedClauseMap(state), id)) {
        reviewedClauseMap(state)[id] = true;
      }
    });
  }

  const nextReviewed = targetClauseIds.some((id) => !clauseReviewAcknowledged(state, id));
  targetClauseIds.forEach((id) => {
    if ((state.reviewClauses || []).some((clause) => clause.id === id)) {
      reviewedClauseMap(state)[id] = nextReviewed;
    }
  });
  const allReviewed = humanReviewAcknowledged(state);
  const matterId = state.selectedMatter?.id;
  const shouldPersistMatterReviewed = Boolean(matterId && allReviewed !== previousMatterReviewed);

  if (state.selectedMatter && shouldPersistMatterReviewed) {
    state.selectedMatter = { ...state.selectedMatter, human_reviewed: allReviewed };
    if (allReviewed) delete state.selectedMatter.send_block_reason;
  }

  return {
    allReviewed,
    matterId,
    nextReviewed,
    previousMatter,
    previousMatterReviewed,
    previousReviewedClauseIds,
    shouldPersistMatterReviewed,
    targetClauseIds,
  };
}

export function applyReviewedMatterResponse(state, matter, { allReviewed = false } = {}) {
  if (!matter?.id) return state.selectedMatter;
  const merged = { ...(state.selectedMatter || {}), ...matter };
  if (allReviewed && !matter.send_block_reason) delete merged.send_block_reason;
  state.selectedMatter = merged;
  return state.selectedMatter;
}

export function normalizeReviewComments(reviewComments) {
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

export function reviewCommentsSnapshot(state) {
  return normalizeReviewComments(state.reviewComments).map((comment) => ({ ...comment }));
}

export function currentReviewComments(state) {
  return normalizeReviewComments(state.reviewComments)
    .map((comment) => (comment.scope === "clause" || (comment.clause_id && !comment.paragraph_id)
      ? { ...comment, ...reviewCommentTargetForClause(state, comment.clause_id) }
      : { ...comment, ...reviewCommentTargetForParagraph(state, comment.paragraph_id) }))
    .filter((comment) => String(comment.text || "").trim() && (comment.paragraph_id || comment.clause_id));
}

export function clauseReviewComment(state, clauseId) {
  return normalizeReviewComments(state.reviewComments).find((comment) => comment.clause_id === clauseId) || null;
}

export function setClauseReviewComment(state, clauseId, text, { now = () => new Date().toISOString() } = {}) {
  const clause = (state.reviewClauses || []).find((item) => item.id === clauseId);
  if (!clause) return null;
  const existing = clauseReviewComment(state, clauseId);
  const trimmedText = String(text || "").trim();
  state.reviewComments = normalizeReviewComments(state.reviewComments)
    .filter((comment) => comment.clause_id !== clauseId);
  if (trimmedText) {
    state.reviewComments.push({
      ...(existing || {}),
      ...reviewCommentTargetForClause(state, clauseId),
      author: existing?.author || "Reviewer",
      clause_id: clauseId,
      clause_name: clause.name || clauseId,
      created_at: existing?.created_at || resolveNow(now),
      id: existing?.id || `comment-${clauseId}`,
      scope: "clause",
      text: trimmedText,
    });
  }
  return clauseReviewComment(state, clauseId);
}

export function reviewCommentTargetForClause(state, clauseId) {
  const clause = (state.reviewClauses || []).find((item) => item.id === clauseId);
  const targetParagraphId = firstClauseParagraphId(state, clauseId, clause);
  const paragraph = (state.reviewParagraphs || []).find((item) => item.id === targetParagraphId);
  const target = {};
  if (targetParagraphId) target.paragraph_id = targetParagraphId;
  if (paragraph?.index !== undefined) target.paragraph_index = paragraph.index;
  if (paragraph?.source_index !== undefined) target.source_index = paragraph.source_index;
  return target;
}

export function reviewCommentTargetForParagraph(state, paragraphId) {
  const paragraph = (state.reviewParagraphs || []).find((item) => item.id === paragraphId);
  const target = {};
  if (paragraph?.id) target.paragraph_id = paragraph.id;
  if (paragraph?.index !== undefined) target.paragraph_index = paragraph.index;
  if (paragraph?.source_index !== undefined) target.source_index = paragraph.source_index;
  return target;
}

export function firstClauseParagraphId(state, clauseId, clause) {
  const matched = Array.isArray(clause?.matched_paragraph_ids)
    ? clause.matched_paragraph_ids.find(Boolean)
    : "";
  if (matched) return String(matched);
  const redline = (state.reviewRedlines || []).find((edit) => edit.clause_id === clauseId && edit.paragraph_id);
  return redline?.paragraph_id ? String(redline.paragraph_id) : "";
}

export function buildParagraphReviewComment(state, paragraphId, text, { now = () => new Date().toISOString() } = {}) {
  const paragraph = (state.reviewParagraphs || []).find((item) => item.id === paragraphId);
  if (!paragraph) return null;
  return {
    ...reviewCommentTargetForParagraph(state, paragraphId),
    author: "Reviewer",
    created_at: resolveNow(now),
    id: `comment-paragraph-${paragraphId}`,
    scope: "paragraph",
    text,
  };
}

export function buildSelectedTextReviewComment(state, paragraphId, selectionInfo, text, { now = () => new Date().toISOString() } = {}) {
  const paragraph = (state.reviewParagraphs || []).find((item) => item.id === paragraphId);
  if (!paragraph || !selectionInfo?.selectedText) return null;
  return {
    ...reviewCommentTargetForParagraph(state, paragraphId),
    author: "Reviewer",
    created_at: resolveNow(now),
    id: `comment-selection-${paragraphId}-${selectionInfo.startOffset}-${selectionInfo.endOffset}`,
    scope: "selection",
    selected_text: selectionInfo.selectedText,
    selection_end: selectionInfo.endOffset,
    selection_start: selectionInfo.startOffset,
    text,
  };
}

export function upsertReviewComment(state, comment) {
  const trimmedText = String(comment?.text || "").trim();
  state.reviewComments = normalizeReviewComments(state.reviewComments).filter((item) => item.id !== comment?.id);
  if (trimmedText) {
    state.reviewComments.push({
      ...comment,
      text: trimmedText,
    });
  }
  return state.reviewComments;
}

export function paragraphCommentThreads(state, paragraphId) {
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

export function nextCommentReplyId(state, rootId) {
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

export function buildCommentReply(state, rootId, text, { now = () => new Date().toISOString() } = {}) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return null;
  const root = normalizeReviewComments(state.reviewComments).find((comment) => comment.id === rootId);
  if (!root) return null;
  return {
    ...reviewCommentTargetForParagraph(state, root.paragraph_id),
    author: "Reviewer",
    created_at: resolveNow(now),
    id: nextCommentReplyId(state, rootId),
    parent_id: rootId,
    scope: "reply",
    text: trimmed,
  };
}

export function updateReviewCommentText(state, commentId, text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return null;
  const existing = normalizeReviewComments(state.reviewComments).find((comment) => comment.id === commentId);
  if (!existing) return null;
  upsertReviewComment(state, { ...existing, text: trimmed });
  return normalizeReviewComments(state.reviewComments).find((comment) => comment.id === commentId) || null;
}

export function removeReviewCommentThread(state, commentId) {
  const all = normalizeReviewComments(state.reviewComments);
  const target = all.find((comment) => comment.id === commentId);
  if (!target) return [];
  const removeIds = new Set([commentId]);
  if (!target.parent_id) {
    all.forEach((comment) => {
      if (comment.parent_id === commentId) removeIds.add(comment.id);
    });
  }
  state.reviewComments = all.filter((comment) => !removeIds.has(comment.id));
  return state.reviewComments;
}

export function toggleReviewCommentResolved(state, rootId) {
  const existing = normalizeReviewComments(state.reviewComments).find((comment) => comment.id === rootId);
  if (!existing) return null;
  upsertReviewComment(state, { ...existing, resolved: !existing.resolved });
  return normalizeReviewComments(state.reviewComments).find((comment) => comment.id === rootId) || null;
}

export function clauseExportIncluded(state, clauseId) {
  return state.exportClauseDecisions?.[clauseId] !== false;
}

export function redlineExportIncluded(state, edit) {
  if (edit?.id && Object.prototype.hasOwnProperty.call(state.exportRedlineDecisions || {}, edit.id)) {
    return state.exportRedlineDecisions[edit.id] !== false;
  }
  return clauseExportIncluded(state, edit?.clause_id);
}

export function effectiveReviewRedlines(state) {
  return (state.reviewRedlines || [])
    .filter((edit) => redlineExportIncluded(state, edit))
    .map((edit) => applyTemplateSelectionToRedline(state, edit));
}

export function applyTemplateSelectionToRedline(state, edit) {
  const selectedOptionId = state.redlineTemplateSelections?.[edit.id];
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
  if (edit.action === REDLINE_INSERT_AFTER_PARAGRAPH_ACTION) {
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

export function setClauseExportDecision(state, clauseId, included) {
  const hadPrevious = Object.prototype.hasOwnProperty.call(state.exportClauseDecisions || {}, clauseId);
  const previousIncluded = state.exportClauseDecisions?.[clauseId];
  const currentIncluded = clauseExportIncluded(state, clauseId);
  state.exportClauseDecisions[clauseId] = included;
  state.selectedReviewClauseId = clauseId;
  return {
    changed: currentIncluded !== included,
    clauseId,
    hadPrevious,
    previousIncluded,
    type: "clause_export_decision",
  };
}

export function setRedlineExportDecision(state, redlineId, included) {
  const edit = (state.reviewRedlines || []).find((item) => item.id === redlineId);
  const hadPrevious = Object.prototype.hasOwnProperty.call(state.exportRedlineDecisions || {}, redlineId);
  const previousIncluded = state.exportRedlineDecisions?.[redlineId];
  const currentIncluded = edit ? redlineExportIncluded(state, edit) : previousIncluded !== false;
  state.exportRedlineDecisions[redlineId] = included;
  if (edit?.clause_id) state.selectedReviewClauseId = edit.clause_id;
  return {
    changed: currentIncluded !== included,
    edit,
    editId: redlineId,
    hadPrevious,
    previousIncluded,
    type: "redline_export_decision",
  };
}

export function setRedlineTemplateSelection(state, editId, optionId) {
  const hadPrevious = Object.prototype.hasOwnProperty.call(state.redlineTemplateSelections || {}, editId);
  const previousOptionId = state.redlineTemplateSelections?.[editId];
  if (previousOptionId === optionId) {
    return { changed: false, editId, hadPrevious, previousOptionId, type: "redline_template_selection" };
  }
  state.redlineTemplateSelections[editId] = optionId;
  return {
    changed: true,
    editId,
    hadPrevious,
    previousOptionId,
    type: "redline_template_selection",
  };
}

export function selectedRedlineTemplateOptionId(state, edit) {
  return state.redlineTemplateSelections?.[edit.id]
    || (edit.template_options || []).find((option) => option.selected)?.id
    || "";
}

export function buildRedlineDraftPayload(state, { manualRedlineEdits = [], reviewComments = currentReviewComments(state) } = {}) {
  return {
    clause_decisions: { ...(state.exportClauseDecisions || {}) },
    redline_decisions: { ...(state.exportRedlineDecisions || {}) },
    template_selections: { ...(state.redlineTemplateSelections || {}) },
    reviewed_clause_ids: { ...reviewedClauseMap(state) },
    export_redline_edits: effectiveReviewRedlines(state),
    manual_redline_edits: manualRedlineEdits,
    review_comments: reviewComments,
  };
}

export function buildReviewExportPayload(state, {
  contentBase64 = "",
  document = null,
  fills = [],
  manualRedlineEdits = [],
  matter = null,
  reviewComments = currentReviewComments(state),
  text = "",
  title = "",
} = {}) {
  const payload = {
    text,
    reviewed_text: text,
    title,
    export_redline_edits: effectiveReviewRedlines(state),
    manual_redline_edits: manualRedlineEdits,
    review_comments: reviewComments,
    fills,
  };
  if (matter?.id) {
    payload.matter_id = matter.id;
  } else if (document) {
    payload.filename = document.name;
    payload.content_base64 = contentBase64;
  }
  return payload;
}

export function buildReviewSendPayload(state, {
  body = "",
  fills = [],
  manualRedlineEdits = [],
  recipient = "",
  reviewComments = currentReviewComments(state),
  subject = "",
  text = "",
} = {}) {
  return {
    matter_id: state.selectedMatter?.id,
    confirm_send: true,
    confirm_recipient: recipient,
    text,
    reviewed_text: text,
    export_redline_edits: effectiveReviewRedlines(state),
    manual_redline_edits: manualRedlineEdits,
    review_comments: reviewComments,
    fills,
    to: recipient,
    subject: String(subject || "").trim(),
    body: String(body || "").trim(),
  };
}

function resolveNow(now) {
  return typeof now === "function" ? now() : now || new Date().toISOString();
}
