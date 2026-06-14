const DEFAULT_PANEL = "policy";
const ALLOWED_PANELS = ["policy", "redline", "decision", "audit"];

function clonePanelState(panelState) {
  return panelState && typeof panelState === "object" ? { ...panelState } : {};
}

function panelIsAllowed(panel) {
  return ALLOWED_PANELS.includes(String(panel || ""));
}

function resolveActivePanel({ clauseId, panelState, mutualityPanel = "" } = {}) {
  const panels = clonePanelState(panelState);
  const savedPanel = panels[clauseId] || (clauseId === "mutuality" ? mutualityPanel : "");
  return panelIsAllowed(savedPanel) ? savedPanel : DEFAULT_PANEL;
}

function setClausePanel({ panelState, clauseId, panel, mutualityPanel = "" } = {}) {
  const panels = clonePanelState(panelState);
  const nextPanel = panelIsAllowed(panel) ? String(panel) : DEFAULT_PANEL;
  if (clauseId) panels[clauseId] = nextPanel;
  return {
    panelState: panels,
    mutualityPanel: clauseId === "mutuality" ? nextPanel : mutualityPanel,
    activePanel: nextPanel,
  };
}

function draftStatus({ hasUnsavedChanges = false, draftAhead = false } = {}) {
  if (hasUnsavedChanges) {
    return {
      state: "editing",
      note: "Unsaved changes - Save Draft to keep them.",
      showDirtyDot: true,
    };
  }
  if (draftAhead) {
    return {
      state: "ahead",
      note: "Saved draft is ahead of the active version - Publish to make it live.",
      showDirtyDot: false,
    };
  }
  return {
    state: "in-sync",
    note: "Matches the active published version.",
    showDirtyDot: false,
  };
}

function validationView(validation) {
  if (!validation) {
    return { state: "idle", hidden: true, errors: [], warnings: [], title: "" };
  }
  const errors = Array.isArray(validation.errors) ? validation.errors : [];
  // Layer-2 semantic-lint warnings are ADVISORY: they ride alongside the result in
  // BOTH the valid and invalid states and never change `state` or block publish.
  const warnings = Array.isArray(validation.warnings) ? validation.warnings : [];
  if (validation.valid) {
    return { state: "valid", hidden: false, errors: [], warnings, title: "Draft passed validation." };
  }
  return {
    state: "invalid",
    hidden: false,
    errors,
    warnings,
    title: `Resolve ${errors.length === 1 ? "this issue" : "these issues"} before publishing:`,
  };
}

function shouldInvalidateValidation({ validation, hasUnsavedChanges = false } = {}) {
  return Boolean(validation && hasUnsavedChanges);
}

function canPublishDraft({
  hasUnsavedChanges = false,
  hasTemplateValidationErrors = false,
  validation = null,
  draftAhead = false,
  runtimeReady = true,
} = {}) {
  if (!runtimeReady) return false;
  if (hasUnsavedChanges || hasTemplateValidationErrors) return false;
  if (validation && !validation.valid) return false;
  return Boolean(draftAhead);
}

function actionAvailability({
  clauseHasDraft = false,
  hasUnsavedChanges = false,
  hasTemplateValidationErrors = false,
  canPublish = false,
} = {}) {
  return {
    discardDisabled: !clauseHasDraft,
    saveDisabled: !hasUnsavedChanges || hasTemplateValidationErrors,
    publishDisabled: !canPublish,
  };
}

const PlaybookAuthoringModel = {
  ALLOWED_PANELS,
  DEFAULT_PANEL,
  actionAvailability,
  canPublishDraft,
  draftStatus,
  panelIsAllowed,
  resolveActivePanel,
  setClausePanel,
  shouldInvalidateValidation,
  validationView,
};

export {
  ALLOWED_PANELS,
  DEFAULT_PANEL,
  PlaybookAuthoringModel,
  actionAvailability,
  canPublishDraft,
  draftStatus,
  panelIsAllowed,
  resolveActivePanel,
  setClausePanel,
  shouldInvalidateValidation,
  validationView,
};
