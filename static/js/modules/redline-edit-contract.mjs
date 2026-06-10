export const REDLINE_ACTION_REPLACE_PARAGRAPH = "replace_paragraph";
export const REDLINE_ACTION_DELETE_PARAGRAPH = "delete_paragraph";
export const REDLINE_ACTION_INSERT_AFTER_PARAGRAPH = "insert_after_paragraph";
export const REDLINE_ACTION_FORMAT_PARAGRAPH = "format_paragraph";
export const MANUAL_VIEWER_EDIT_CLAUSE_ID = "manual_viewer_edit";

export const REDLINE_ACTIONS = Object.freeze([
  REDLINE_ACTION_REPLACE_PARAGRAPH,
  REDLINE_ACTION_DELETE_PARAGRAPH,
  REDLINE_ACTION_INSERT_AFTER_PARAGRAPH,
  REDLINE_ACTION_FORMAT_PARAGRAPH,
]);

export const MANUAL_REDLINE_ACTIONS = Object.freeze([
  REDLINE_ACTION_REPLACE_PARAGRAPH,
  REDLINE_ACTION_DELETE_PARAGRAPH,
  REDLINE_ACTION_FORMAT_PARAGRAPH,
]);

export function isKnownRedlineAction(action) {
  return REDLINE_ACTIONS.includes(String(action || ""));
}

export function isManualRedlineAction(action) {
  return MANUAL_REDLINE_ACTIONS.includes(String(action || ""));
}

export function isManualRedlineEdit(edit) {
  return Boolean(edit?.is_manual || edit?.clause_id === MANUAL_VIEWER_EDIT_CLAUSE_ID);
}

export function isInsertionRedlineEdit(edit) {
  return edit?.action === REDLINE_ACTION_INSERT_AFTER_PARAGRAPH;
}

export function redlineActionLabel(edit) {
  if (edit?.action_label) return String(edit.action_label);
  if (edit?.action === REDLINE_ACTION_DELETE_PARAGRAPH) return "Remove paragraph";
  if (edit?.action === REDLINE_ACTION_INSERT_AFTER_PARAGRAPH) return "Insert after paragraph";
  if (edit?.action === REDLINE_ACTION_REPLACE_PARAGRAPH) return "Replace paragraph";
  if (edit?.action === REDLINE_ACTION_FORMAT_PARAGRAPH) return "Format paragraph";
  return "Proposed edit";
}

export function redlineInsertedText(edit) {
  return String(edit?.insert_text || edit?.replacement_text || "");
}

export function redlineReplacementText(edit) {
  if (edit?.action === REDLINE_ACTION_DELETE_PARAGRAPH) return "";
  if (edit?.action === REDLINE_ACTION_INSERT_AFTER_PARAGRAPH) return redlineInsertedText(edit);
  return String(edit?.replacement_text || "");
}

export function hasInlineDiffOperations(edit) {
  return Array.isArray(edit?.inline_diff_operations) && edit.inline_diff_operations.length > 0;
}

export function redlineInlinePreviewMode(edit) {
  if (hasInlineDiffOperations(edit)) return "operations";
  if (edit?.whole_paragraph) return "whole_paragraph";
  if (edit?.action === REDLINE_ACTION_REPLACE_PARAGRAPH && isManualRedlineEdit(edit)) return "character_diff";
  return "whole_paragraph";
}

export function redlineOperationPreviewMode(edit) {
  if (hasInlineDiffOperations(edit)) return "operations";
  if (edit?.whole_paragraph) return "whole_paragraph";
  if (edit?.action === REDLINE_ACTION_REPLACE_PARAGRAPH && isManualRedlineEdit(edit)) return "word_diff";
  return "whole_paragraph";
}

export function normalizeRedlineEdit(raw) {
  if (!raw || typeof raw !== "object") return null;
  const action = String(raw.action || "");
  if (!isKnownRedlineAction(action)) return null;
  const paragraphId = String(raw.paragraph_id || "").trim();
  if (!paragraphId) return null;
  const manualEdit = Boolean(raw.is_manual || raw.clause_id === MANUAL_VIEWER_EDIT_CLAUSE_ID);
  if (manualEdit && !isManualRedlineAction(action)) return null;

  const edit = {
    ...raw,
    action,
    action_label: redlineActionLabel(raw),
    clause_id: String(raw.clause_id || (raw.is_manual ? MANUAL_VIEWER_EDIT_CLAUSE_ID : "")).trim(),
    id: String(raw.id || ""),
    original_text: String(raw.original_text ?? ""),
    paragraph_id: paragraphId,
    replacement_text: String(raw.replacement_text ?? ""),
    status: String(raw.status || "proposed"),
  };
  if (raw.insert_text != null) edit.insert_text = String(raw.insert_text);
  if (raw.paragraph_index != null) edit.paragraph_index = Number(raw.paragraph_index);
  if (raw.source_index != null) edit.source_index = Number(raw.source_index);
  if (raw.source_part != null) edit.source_part = String(raw.source_part);
  if (raw.whole_paragraph != null) edit.whole_paragraph = Boolean(raw.whole_paragraph);
  if (raw.is_manual != null) edit.is_manual = Boolean(raw.is_manual);
  if (Array.isArray(raw.inline_diff_operations)) {
    edit.inline_diff_operations = raw.inline_diff_operations
      .filter((operation) => operation && ["equal", "delete", "insert"].includes(operation.type))
      .map((operation) => ({ type: operation.type, token: String(operation.token ?? "") }));
  }
  if (Array.isArray(raw.replacement_runs)) {
    edit.replacement_runs = raw.replacement_runs.map((run) => ({ ...run }));
  }
  if (Array.isArray(raw.format_ops)) {
    edit.format_ops = raw.format_ops.map((operation) => ({ ...operation }));
  }
  return edit;
}

export function normalizeRedlineEdits(rawEdits) {
  if (!Array.isArray(rawEdits)) return [];
  return rawEdits.map(normalizeRedlineEdit).filter(Boolean);
}

export const RedlineEditContract = Object.freeze({
  MANUAL_REDLINE_ACTIONS,
  MANUAL_VIEWER_EDIT_CLAUSE_ID,
  REDLINE_ACTION_DELETE_PARAGRAPH,
  REDLINE_ACTION_FORMAT_PARAGRAPH,
  REDLINE_ACTION_INSERT_AFTER_PARAGRAPH,
  REDLINE_ACTION_REPLACE_PARAGRAPH,
  REDLINE_ACTIONS,
  hasInlineDiffOperations,
  isInsertionRedlineEdit,
  isKnownRedlineAction,
  isManualRedlineAction,
  isManualRedlineEdit,
  normalizeRedlineEdit,
  normalizeRedlineEdits,
  redlineActionLabel,
  redlineInlinePreviewMode,
  redlineInsertedText,
  redlineOperationPreviewMode,
  redlineReplacementText,
});
