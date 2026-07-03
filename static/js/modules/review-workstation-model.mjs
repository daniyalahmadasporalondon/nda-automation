// Versioned specifier matching global-bridge.mjs's redline-edit-contract.mjs
// token exactly: same token -> same resolved URL -> single module instance, AND
// the browser can cache it immutably instead of revalidating a query-less URL on
// every visit. Keep this token in lockstep with redline-edit-contract.mjs's
// bytes (and with the token in global-bridge.mjs -- the manifest guard fails on
// a conflicting pair).
import {
  isInsertionRedlineEdit,
  redlineReplacementText,
} from "./redline-edit-contract.mjs?v=20260703cachebust1";

export const REVIEW_VIEW_MODES = Object.freeze(["redline", "clean", "sidebyside", "original"]);
export const REVIEW_INSPECTOR_VIEWS = Object.freeze(["clause", "structure", "fill"]);

export function normalizeReviewViewMode(mode, fallback = "redline") {
  const value = String(mode || "");
  return REVIEW_VIEW_MODES.includes(value) ? value : fallback;
}

export function normalizeInspectorView(view, fallback = "clause") {
  const value = String(view || "");
  return REVIEW_INSPECTOR_VIEWS.includes(value) ? value : fallback;
}

export function hasReviewResults(workstation) {
  return Array.isArray(workstation?.reviewClauses) && workstation.reviewClauses.length > 0;
}

export function selectedReviewClause(workstation) {
  const selectedId = String(workstation?.selectedReviewClauseId || "");
  return (workstation?.reviewClauses || []).find((clause) => String(clause.id) === selectedId) || null;
}

export function selectedReviewParagraph(workstation) {
  const selectedClause = selectedReviewClause(workstation);
  const paragraphId = selectedClause?.matched_paragraph_ids?.find(Boolean);
  if (!paragraphId) return null;
  return (workstation?.reviewParagraphs || []).find((paragraph) => String(paragraph.id) === String(paragraphId)) || null;
}

export function nextClauseSelectionState(workstation, clauseId) {
  return {
    reviewInspectorView: "clause",
    selectedReviewClauseId: clauseId || null,
  };
}

export function defaultExportClauseDecisions(clauses, redlines) {
  const clausesWithRedlines = new Set((redlines || []).map((edit) => edit?.clause_id).filter(Boolean));
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

export function clauseExportIncluded(workstation, clauseId) {
  return workstation?.exportClauseDecisions?.[clauseId] !== false;
}

export function redlineExportIncluded(workstation, edit) {
  if (edit?.id && Object.prototype.hasOwnProperty.call(workstation?.exportRedlineDecisions || {}, edit.id)) {
    return workstation.exportRedlineDecisions[edit.id] !== false;
  }
  return clauseExportIncluded(workstation, edit?.clause_id);
}

export function exportDecisionTransition(decisions, id, included) {
  if (!id) return { ...(decisions || {}) };
  return { ...(decisions || {}), [id]: included === true };
}

export function applyTemplateSelectionToRedline(edit, selections = {}) {
  const selectedOptionId = selections[edit?.id];
  const selectedOption = (edit?.template_options || []).find((option) => option.id === selectedOptionId);
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
  if (isInsertionRedlineEdit(edit)) {
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

export function effectiveReviewRedlines(workstation) {
  return (workstation?.reviewRedlines || [])
    .filter((edit) => redlineExportIncluded(workstation, edit))
    .map((edit) => applyTemplateSelectionToRedline(edit, workstation?.redlineTemplateSelections || {}));
}

export function reviewIsStale(workstation) {
  return Boolean(workstation?.selectedMatter?.review_refresh?.stale);
}

export function canMarkRedlineDraftDirty(workstation) {
  return Boolean(workstation?.selectedMatter?.id && hasReviewResults(workstation));
}

export function redlineDraftTransition(workstation, { dirty = true } = {}) {
  if (!canMarkRedlineDraftDirty(workstation)) return { redlineDraftDirty: Boolean(workstation?.redlineDraftDirty) };
  return { redlineDraftDirty: dirty === true };
}

export function redlineDraftControlState(workstation) {
  const canDraft = canMarkRedlineDraftDirty(workstation);
  return {
    canDraft,
    discardDisabled: !canDraft || !workstation?.redlineDraft,
    metaText: !canDraft
      ? ""
      : workstation?.redlineDraftDirty
        ? "Unsaved redline draft changes"
        : workstation?.redlineDraft
          ? "Draft redline saved"
          : "",
    saveDisabled: !canDraft || !workstation?.redlineDraftDirty,
  };
}

export function gmailSendReadiness({
  blockedLabel = "Send Redline",
  canExport,
  hasSendableMatter,
  sendBlockReason = "",
  staleReview = false,
} = {}) {
  const interactive = Boolean(canExport && hasSendableMatter && !staleReview);
  const canSend = Boolean(interactive && !sendBlockReason);
  return {
    ariaDisabled: String(!interactive),
    canSend,
    interactive,
    label: staleReview
      ? "Send Redline"
      : canSend
        ? "Send Redline"
        : sendBlockReason
          ? blockedLabel
          : "Send Redline",
    title: staleReview
      ? "Refresh review before sending a redline"
      : sendBlockReason || "Send Redline",
  };
}

export function selectedBackendRedline(workstation, paragraphId) {
  const paragraphRedlines = effectiveReviewRedlines(workstation)
    .filter((edit) => String(edit.paragraph_id) === String(paragraphId));
  return paragraphRedlines.find((edit) => edit.clause_id === workstation?.selectedReviewClauseId)
    || paragraphRedlines[0]
    || null;
}

export function manualRedlinePreviewState({
  backendRedline = null,
  manualRedline = null,
  paragraph = null,
  workstation = null,
} = {}) {
  const hasBackendRedline = Boolean(
    paragraph?.id && effectiveReviewRedlines(workstation)
      .some((edit) => String(edit.paragraph_id) === String(paragraph.id)),
  );
  return {
    backendRedline,
    hasBackendRedline,
    manualRedline,
    replacementText: redlineReplacementText(manualRedline),
    visible: Boolean(manualRedline),
  };
}

export function commentComposerState({ composeScope = "", hasThreads = false, selectionInfo = null } = {}) {
  const scope = composeScope || (hasThreads ? "read" : "paragraph");
  return {
    mode: scope === "read" ? "read" : "compose",
    scope,
    selectionText: selectionInfo?.selectedText ? String(selectionInfo.selectedText) : "",
  };
}

export function annotationGeometryState(raw = {}) {
  const page = Math.max(1, Number(raw.page) || 1);
  const rect = raw.rect && typeof raw.rect === "object" ? raw.rect : {};
  const clamp = (value) => Math.max(0, Math.min(1, Number(value) || 0));
  return {
    page,
    rect: {
      h: clamp(rect.h),
      w: clamp(rect.w),
      x: clamp(rect.x),
      y: clamp(rect.y),
    },
    selectedId: raw.selectedId == null ? "" : String(raw.selectedId),
    tool: String(raw.tool || "select"),
  };
}

export const ReviewWorkstationModel = Object.freeze({
  REVIEW_INSPECTOR_VIEWS,
  REVIEW_VIEW_MODES,
  annotationGeometryState,
  applyTemplateSelectionToRedline,
  canMarkRedlineDraftDirty,
  clauseExportIncluded,
  commentComposerState,
  defaultExportClauseDecisions,
  defaultRedlineTemplateSelections,
  effectiveReviewRedlines,
  exportDecisionTransition,
  gmailSendReadiness,
  hasReviewResults,
  manualRedlinePreviewState,
  nextClauseSelectionState,
  normalizeInspectorView,
  normalizeReviewViewMode,
  redlineDraftControlState,
  redlineDraftTransition,
  redlineExportIncluded,
  reviewIsStale,
  selectedBackendRedline,
  selectedReviewClause,
  selectedReviewParagraph,
});
