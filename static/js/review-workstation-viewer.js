const REVIEW_EDIT_HISTORY_LIMIT = 50;
const VIEWER_REVIEW_REFRESH_DELAY_MS = 650;

let viewerReviewRefreshTimer = null;
let viewerReviewRefreshSequence = 0;

function setupDocumentViewModes() {
  const buttons = document.querySelectorAll(".studio-view-switch [data-view-mode]");
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      setDocumentViewMode(button.dataset.viewMode, { render: true });
    });
  });
  updateDocumentViewModeButtons();
}

function setupReviewUndoControls() {
  studioUndoEditButton?.addEventListener("click", undoLastViewerEdit);
  updateReviewUndoButtonState();
}

// Apply a programmatic Find & Replace text change to one review paragraph. Reuses the
// shared formatting-preserving re-tile (findReplace._retileRunsForReplace) so inline
// run formatting around the replaced span survives, records an Undo entry, and routes
// the edit through the SAME source-sync + dirty + render hooks a typed edit uses so it
// rides the existing manual-redline export with no new serializer op.
function applyReviewFindReplace(paragraph, newText, oldText) {
  if (!paragraph) return;
  const before = oldText !== undefined ? String(oldText) : String(paragraph.text || "");
  if (before === newText) return;
  // Capture the inline runs BEFORE the retile so Undo can restore the original
  // bold/italic/per-run-font tiling. A plain paragraph_text undo only restores
  // text and would leave the runs tiling the post-replace text -> formatting lost.
  // Mirrors the hadRuns/previousRuns shape pushParagraphFormatHistory uses; runs
  // are deep-copied so a later mutation can't corrupt the captured undo state.
  const hadRuns = Object.prototype.hasOwnProperty.call(paragraph, "runs") && Array.isArray(paragraph.runs);
  pushReviewEditHistoryEntry({
    paragraphId: paragraph.id,
    previousText: before,
    type: "paragraph_text",
    hadRuns,
    previousRuns: hadRuns ? paragraph.runs.map((run) => ({ ...run })) : undefined,
  });
  if (window.findReplace && typeof window.findReplace._retileRunsForReplace === "function") {
    const retiled = window.findReplace._retileRunsForReplace(paragraph.runs, before, newText);
    if (retiled) paragraph.runs = retiled;
    // When nothing was worth preserving the runs go inert via the join==text guards
    // (same as a typed edit), so the paragraph renders as clean replaced text.
  }
  paragraph.text = newText;
  paragraph.clauseRedlineWholeParagraph = false; // text changed -> word-level diff
}

// Re-sync + re-render once after a batch of Find & Replace edits, mirroring the
// post-edit bookkeeping syncViewerParagraphEdit does (source rebuild, dirty marker,
// re-render, staleness flag, export button) but only ONCE for the whole batch.
function afterReviewFindReplaceBatch() {
  syncReviewSourceFromParagraphs();
  markRedlineDraftDirty();
  markSourceEdited("Find & Replace", { preserveSourceDocument: true });
  renderStudioDocumentHighlights();
  scheduleViewerReviewRefresh("Document edited");
  markReviewMayBeStaleFromEdit();
  updateExportButtonState();
}

function setupReviewFindReplace() {
  if (!window.findReplace || typeof window.findReplace.register !== "function") return;
  window.findReplace.register("review", {
    paragraphs: () => state.reviewParagraphs || [],
    getRenderEl: () => studioDocumentRender,
    getPanelHost: () => studioDocumentRender?.closest(".studio-page") || studioDocumentRender,
    applyReplacement: applyReviewFindReplace,
    afterBatch: afterReviewFindReplaceBatch,
  });
  // Toolbar trigger (additive button; co-exists with the other editor toolbar work).
  const button = document.getElementById("studioFindReplaceButton");
  if (button) button.addEventListener("click", () => window.findReplace.open("review"));
}

function updateReviewUndoButtonState() {
  if (!studioUndoEditButton) return;
  studioUndoEditButton.disabled = !(state.reviewEditHistory || []).length;
}

function resetReviewEditHistory() {
  state.reviewEditHistory = [];
  updateReviewUndoButtonState();
}

function cancelViewerReviewRefresh() {
  if (viewerReviewRefreshTimer !== null) {
    window.clearTimeout(viewerReviewRefreshTimer);
    viewerReviewRefreshTimer = null;
  }
  viewerReviewRefreshSequence += 1;
}

function setDocumentViewMode(mode, { render = false } = {}) {
  if (!DOCUMENT_VIEW_MODES.includes(mode)) return;
  if (state.documentViewMode === mode && render === false) {
    updateDocumentViewModeButtons();
    return;
  }
  state.documentViewMode = mode;
  updateDocumentViewModeButtons();
  if (render && state.reviewParagraphs.length && !studioDocumentRender.hidden) {
    renderStudioDocumentHighlights();
  }
}

function updateDocumentViewModeButtons() {
  document.querySelectorAll(".studio-view-switch [data-view-mode]").forEach((button) => {
    const active = button.dataset.viewMode === state.documentViewMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function bindViewerParagraphEditing() {
  studioDocumentRender.querySelectorAll("[data-editable-paragraph-id]").forEach((editable) => {
    editable.addEventListener("focus", () => {
      editable.dataset.editStartText = editableParagraphText(editable);
      delete editable.dataset.editHistoryRecorded;
      editable.closest(".studio-doc-paragraph")?.classList.add("is-editing");
    });
    editable.addEventListener("blur", () => {
      delete editable.dataset.editStartText;
      delete editable.dataset.editHistoryRecorded;
      editable.closest(".studio-doc-paragraph")?.classList.remove("is-editing");
    });
    editable.addEventListener("input", () => {
      recordViewerEditHistoryEntry(editable);
      syncViewerParagraphEdit(editable);
      if (typeof refreshGoverningLawConcurrence === "function") refreshGoverningLawConcurrence();
    });
    editable.addEventListener("paste", pastePlainText);
  });

  // Click-to-edit: a manual-redline paragraph shows only the diff (its plain
  // editor is collapsed via CSS). Clicking the diff reveals + focuses the editor
  // in place; the diff returns on blur -- so no editor box opens below it.
  studioDocumentRender.querySelectorAll("[data-redline-preview]").forEach((preview) => {
    preview.addEventListener("mousedown", (event) => {
      const container = preview.closest(".studio-doc-paragraph");
      const editable = container?.querySelector(".paragraph-editable");
      if (!editable) return;
      event.preventDefault();
      container.classList.add("is-editing");
      editable.focus({ preventScroll: true });
      placeViewerCaretAtEnd(editable);
    });
  });
}

function placeViewerCaretAtEnd(editable) {
  try {
    const range = document.createRange();
    range.selectNodeContents(editable);
    range.collapse(false);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  } catch (error) {
    /* caret placement is best-effort */
  }
}

function recordViewerEditHistoryEntry(editable) {
  if (editable.dataset.editHistoryRecorded === "true") return;
  const paragraphId = editable.dataset.editableParagraphId;
  const beforeText = editable.dataset.editStartText ?? currentParagraphText(paragraphId);
  const afterText = editableParagraphText(editable);
  if (beforeText === afterText) return;

  pushReviewEditHistoryEntry({
    paragraphId,
    previousText: beforeText,
    type: "paragraph_text",
  });
  editable.dataset.editHistoryRecorded = "true";
}

function pushReviewEditHistoryEntry(entry) {
  if (!entry || typeof entry !== "object") return;
  state.reviewEditHistory = [
    ...(state.reviewEditHistory || []),
    entry,
  ].slice(-REVIEW_EDIT_HISTORY_LIMIT);
  updateReviewUndoButtonState();
}

function currentParagraphText(paragraphId) {
  const paragraph = state.reviewParagraphs.find((item) => item.id === paragraphId);
  return String(paragraph?.text || "");
}

function undoLastViewerEdit() {
  const lastEdit = state.reviewEditHistory?.pop();
  if (!lastEdit) {
    updateReviewUndoButtonState();
    return;
  }

  if (lastEdit.type === "clause_export_decision") {
    restoreClauseExportDecision(lastEdit);
    return;
  }

  if (lastEdit.type === "redline_template_selection") {
    restoreRedlineTemplateSelection(lastEdit);
    return;
  }

  if (lastEdit.type === "redline_export_decision") {
    restoreRedlineExportDecision(lastEdit);
    return;
  }

  if (lastEdit.type === "review_comments") {
    restoreReviewCommentsSnapshot(lastEdit);
    return;
  }

  if (lastEdit.type === "paragraph_format") {
    restoreParagraphFormat(lastEdit);
    return;
  }

  const paragraph = state.reviewParagraphs.find((item) => item.id === lastEdit.paragraphId);
  if (!paragraph) {
    updateReviewUndoButtonState();
    return;
  }

  paragraph.text = lastEdit.previousText;
  // A Find & Replace entry also captured the pre-retile runs (hadRuns); restore
  // them so inline bold/italic/per-run font survives the undo. A normal typed edit
  // carries no captured runs and has always relied on the retile-from-text guards,
  // so leave its runs untouched. Deep-copy to keep the undo state immutable; after
  // this the runs.join("") === text invariant holds again.
  if (lastEdit.hadRuns) {
    paragraph.runs = Array.isArray(lastEdit.previousRuns)
      ? lastEdit.previousRuns.map((run) => ({ ...run }))
      : [];
  }
  syncReviewSourceFromParagraphs();
  markRedlineDraftDirty();
  markSourceEdited("Undid viewer edit", { preserveSourceDocument: true });
  renderStudioDocumentHighlights();
  scheduleViewerReviewRefresh("Last viewer edit undone");
  updateExportButtonState();
  updateReviewUndoButtonState();
}

function restoreReviewCommentsSnapshot(historyEntry) {
  state.reviewComments = Array.isArray(historyEntry.snapshot)
    ? historyEntry.snapshot.map((comment) => ({ ...comment }))
    : [];
  if (typeof closeParagraphCommentComposers === "function") closeParagraphCommentComposers();
  markRedlineDraftDirty();
  renderStudioDocumentHighlights();
  if (typeof renderStudioClauseLane === "function") renderStudioClauseLane();
  updateExportButtonState();
  setFileMeta("Undid comment change");
  updateReviewUndoButtonState();
}

function restoreParagraphFormat(historyEntry) {
  const paragraph = state.reviewParagraphs.find(
    (item) => String(item.id) === String(historyEntry.paragraphId),
  );
  if (!paragraph) {
    updateReviewUndoButtonState();
    return;
  }
  // Restore the exact prior formatting; delete the property when it didn't exist
  // before, so the derived format_paragraph redline recomputes (or disappears).
  if (historyEntry.hadAlignment) {
    paragraph.alignment = historyEntry.previousAlignment;
  } else {
    delete paragraph.alignment;
  }
  if (historyEntry.hadFont) {
    paragraph.font = historyEntry.previousFont;
  } else {
    delete paragraph.font;
  }
  if (historyEntry.hadFontSize) {
    paragraph.fontSize = historyEntry.previousFontSize;
  } else if (Object.prototype.hasOwnProperty.call(historyEntry, "hadFontSize")) {
    delete paragraph.fontSize;
  }
  // Inline (bold/italic/per-selection font) edits paragraph.runs; restore the
  // prior run list (or delete it). Guard on the field existing so older entries
  // without run state never clobber runs.
  if (historyEntry.hadRuns) {
    paragraph.runs = Array.isArray(historyEntry.previousRuns)
      ? historyEntry.previousRuns.map((run) => ({ ...run }))
      : [];
  } else if (Object.prototype.hasOwnProperty.call(historyEntry, "hadRuns")) {
    delete paragraph.runs;
  }
  markRedlineDraftDirty();
  renderStudioDocumentHighlights();
  if (typeof renderStudioClauseLane === "function") renderStudioClauseLane();
  updateExportButtonState();
  setFileMeta("Undid formatting change");
  updateReviewUndoButtonState();
}

function restoreClauseExportDecision(historyEntry) {
  if (!historyEntry.clauseId) {
    updateReviewUndoButtonState();
    return;
  }
  if (historyEntry.hadPrevious) {
    state.exportClauseDecisions[historyEntry.clauseId] = Boolean(historyEntry.previousIncluded);
  } else {
    delete state.exportClauseDecisions[historyEntry.clauseId];
  }
  state.selectedReviewClauseId = historyEntry.clauseId;
  markRedlineDraftDirty();
  renderStudioResult({ clauses: state.reviewClauses });
  setFileMeta("Undid clause suggestion change");
  studioResultMeta.textContent = "Clause suggestion change undone.";
  updateExportButtonState();
  updateReviewUndoButtonState();
}

function restoreRedlineTemplateSelection(historyEntry) {
  if (!historyEntry.editId) {
    updateReviewUndoButtonState();
    return;
  }
  if (historyEntry.hadPrevious) {
    state.redlineTemplateSelections[historyEntry.editId] = historyEntry.previousOptionId;
  } else {
    delete state.redlineTemplateSelections[historyEntry.editId];
  }
  const edit = state.reviewRedlines.find((item) => item.id === historyEntry.editId);
  if (edit?.clause_id) state.selectedReviewClauseId = edit.clause_id;
  markRedlineDraftDirty();
  renderStudioResult({ clauses: state.reviewClauses });
  setFileMeta("Undid clause suggestion change");
  studioResultMeta.textContent = "Clause suggestion change undone.";
  updateExportButtonState();
  updateReviewUndoButtonState();
}

function restoreRedlineExportDecision(historyEntry) {
  if (!historyEntry.editId) {
    updateReviewUndoButtonState();
    return;
  }
  if (historyEntry.hadPrevious) {
    state.exportRedlineDecisions[historyEntry.editId] = Boolean(historyEntry.previousIncluded);
  } else {
    delete state.exportRedlineDecisions[historyEntry.editId];
  }
  const edit = state.reviewRedlines.find((item) => item.id === historyEntry.editId);
  if (edit?.clause_id) state.selectedReviewClauseId = edit.clause_id;
  markRedlineDraftDirty();
  renderStudioResult({ clauses: state.reviewClauses });
  setFileMeta("Undid clause suggestion change");
  studioResultMeta.textContent = "Clause suggestion change undone.";
  updateExportButtonState();
  updateReviewUndoButtonState();
}

function syncViewerParagraphEdit(editable) {
  const paragraphId = editable.dataset.editableParagraphId;
  const paragraph = state.reviewParagraphs.find((item) => item.id === paragraphId);
  if (!paragraph) return;

  // A free-form text edit invalidates any inline (bold/italic/per-selection-font)
  // runs on this paragraph (their offsets tiled the OLD text). The runs are kept
  // but go inert via the render/derive join==text guards, so the formatting drops
  // out of the redline + export. Warn the user ONCE, on the first edit that breaks
  // a previously-valid formatted run set, so the loss is explicit, not silent.
  // (A text-undo restores the old text, re-tiles the runs, and the formatting
  // reappears — so we deliberately do NOT delete the runs.)
  const oldText = String(paragraph.text || "");
  const newText = editableParagraphText(editable);
  const runs = Array.isArray(paragraph.runs) ? paragraph.runs : null;
  const runsWereValid = runs && runs.map((run) => String(run?.text || "")).join("") === oldText;
  const runsFormatted = runs && runs.some((run) => run && (run.bold || run.italic || String(run.font || "").trim()));
  const droppedInlineFormat = newText !== oldText && runsWereValid && runsFormatted;

  paragraph.text = newText;
  paragraph.clauseRedlineWholeParagraph = false;  // free-form edit -> word-level diff
  syncReviewSourceFromParagraphs();
  updateManualRedlinePreview(editable, paragraph);
  markRedlineDraftDirty();
  markSourceEdited("Edited in viewer", { preserveSourceDocument: true });
  // Cheap, offline clause DETECTION may run automatically (deterministic, no model).
  scheduleViewerReviewRefresh("Document edited");
  // The expensive per-clause AI reassess must NOT auto-fire on every keystroke.
  // AI review is gated behind the explicit "Refresh with AI" action. A viewer edit
  // marks the review as possibly stale instead, so the indicator + button surface.
  markReviewMayBeStaleFromEdit();
  updateExportButtonState();
  if (droppedInlineFormat) {
    setFileMeta("Inline formatting on this paragraph was cleared by the text edit");
  }
}

// A viewer edit can invalidate the stored AI review, but we no longer auto-run
// the model. Flag the loaded matter as GENUINELY drifted so the "Reviewed (may be
// out of date)" indicator + "Refresh with AI" button appear; the operator triggers
// the (expensive) AI reassess explicitly. Only meaningful for a saved matter.
//
// The dedicated `review_edited_since_load` marker is what drives the freshness
// indicator's state (c). It is set ONLY here (an actual in-session document edit)
// and is naturally absent after every (re)open, because loadMatterIntoReview
// replaces state.selectedMatter with a fresh server matter object that never
// carries this FE-only marker. This is the discriminator that keeps a plain reopen
// of an unedited reviewed matter in the confident state (b) "Reviewed": the broad
// review_may_be_stale open flag (set on EVERY open merely because the open path
// does not re-run AI) must NOT, on its own, read as drift.
function markReviewMayBeStaleFromEdit() {
  if (!state.selectedMatter?.id) return;
  if (state.selectedMatter.review_edited_since_load) return;
  // Keep the legacy broad flag set for any other consumer, but the freshness
  // indicator keys off the dedicated edit marker, not this broad flag.
  state.selectedMatter.review_may_be_stale = true;
  state.selectedMatter.review_edited_since_load = true;
  if (typeof renderReviewRefreshNotice === "function") {
    renderReviewRefreshNotice(state.selectedMatter.review_refresh);
  }
}

// Schedule a per-clause re-assessment for every clause that cites the edited
// paragraph.  Only fires when there is a saved matter to reassess against.
// NOTE: this is the expensive AI path and is now invoked ONLY from explicit
// actions, never automatically on edit (see markReviewMayBeStaleFromEdit).
function scheduleClauseReassessForParagraph(paragraphId) {
  if (!paragraphId || !state.selectedMatter?.id || typeof scheduleClauseReassess !== "function") return;
  const editedParagraphs = state.reviewParagraphs.map((p) => ({
    id: p.id,
    index: p.index,
    source_index: p.source_index,
    text: p.text,
  }));
  const affectedClauseIds = new Set();
  state.reviewClauses.forEach((clause) => {
    const ids = Array.isArray(clause.matched_paragraph_ids) ? clause.matched_paragraph_ids : [];
    if (ids.some((id) => String(id) === String(paragraphId))) {
      affectedClauseIds.add(clause.id);
    }
  });
  // Also check redline edits which carry a paragraph_id.
  state.reviewRedlines.forEach((edit) => {
    if (String(edit.paragraph_id || "") === String(paragraphId) && edit.clause_id) {
      affectedClauseIds.add(edit.clause_id);
    }
  });
  affectedClauseIds.forEach((clauseId) => {
    scheduleClauseReassess(clauseId, editedParagraphs);
  });
}

function scheduleViewerReviewRefresh(message) {
  if (!state.reviewParagraphs.length) return;
  if (viewerReviewRefreshTimer !== null) {
    window.clearTimeout(viewerReviewRefreshTimer);
  }
  const sequence = viewerReviewRefreshSequence + 1;
  viewerReviewRefreshSequence = sequence;
  studioResultMeta.textContent = `${message}. Rechecking clause detection.`;
  viewerReviewRefreshTimer = window.setTimeout(() => {
    viewerReviewRefreshTimer = null;
    refreshViewerReviewDetection(sequence);
  }, VIEWER_REVIEW_REFRESH_DELAY_MS);
}

async function refreshViewerReviewDetection(sequence) {
  const text = state.reviewSourceText.trim();
  if (!text) return;
  try {
    // Live, per-edit clause DETECTION must stay cheap and AI-free. Pass
    // offline:true so the backend runs only deterministic detection (no model).
    // The expensive AI review is reserved for the explicit "Refresh with AI".
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, offline: true }),
    });
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, "Review could not run");
    if (sequence !== viewerReviewRefreshSequence) return;
    if (state.reviewSourceText.trim() !== text) {
      scheduleViewerReviewRefresh("Document edited");
      return;
    }
    applyViewerReviewDetectionResult(payload, text);
  } catch (error) {
    if (sequence !== viewerReviewRefreshSequence) return;
    studioResultMeta.textContent = `Document edited. Clause detection could not refresh: ${error.message || "Review could not run."}`;
  }
}

// Structure/clause-mapping keys the live detection refresh is allowed to overlay
// onto an existing (possibly user-edited) paragraph. These are the detector's
// OUTPUT — heading/numbering/clause-anchoring metadata — and carry NO run-bearing
// content. The run-bearing model (text/runs/alignment/font/fontSize) is owned by
// the editor and is NEVER taken from the detection result (that was the data-loss
// bug: a detection refresh after an edit clobbered the edited runs/text/format).
const VIEWER_DETECTION_STRUCTURE_KEYS = [
  "clause_id",
  "clause_ids",
  "heading_level",
  "indent_left",
  "numbering",
  "outline_level",
  "page_number",
  "role",
  "section_role",
  "source_kind",
  "structure",
  "structure_label",
  "structure_number",
  "style_id",
  "style_name",
  "table",
];

// MERGE the live detection result onto the EXISTING run-bearing paragraphs by
// stable identity, instead of replacing the model wholesale. We key on the unique
// review id/index first (source_index is non-unique provenance and must not be the
// primary key — split blocks share it). For each existing paragraph we overlay only
// the detector's structure/clause tags (VIEWER_DETECTION_STRUCTURE_KEYS), preserving
// the paragraph's edited text/runs/alignment/font/fontSize. Paragraphs the detector
// added or dropped fall back to the detection copy so clause anchoring stays whole.
function mergeViewerDetectionParagraphs(existingParagraphs, detectedParagraphs) {
  const existing = Array.isArray(existingParagraphs) ? existingParagraphs : [];
  const detected = Array.isArray(detectedParagraphs) ? detectedParagraphs : [];
  if (!detected.length) return existing.map((paragraph) => ({ ...paragraph }));
  if (!existing.length) return detected.map((paragraph) => ({ ...paragraph }));
  const existingById = new Map();
  existing.forEach((paragraph) => {
    if (paragraph.id !== undefined && paragraph.id !== null) {
      existingById.set(String(paragraph.id), paragraph);
    }
  });
  const existingByIndex = new Map();
  existing.forEach((paragraph) => {
    if (paragraph.index !== undefined && paragraph.index !== null) {
      existingByIndex.set(String(paragraph.index), paragraph);
    }
  });
  return detected.map((detectedParagraph) => {
    const key = detectedParagraph.id !== undefined && detectedParagraph.id !== null
      ? String(detectedParagraph.id)
      : null;
    const indexKey = detectedParagraph.index !== undefined && detectedParagraph.index !== null
      ? String(detectedParagraph.index)
      : null;
    const match = (key !== null && existingById.get(key))
      || (indexKey !== null && existingByIndex.get(indexKey))
      || null;
    // No existing run-bearing paragraph to preserve (detector added it): take the
    // detection copy verbatim so clause anchoring still has its paragraph.
    if (!match) return { ...detectedParagraph };
    // Start from the EXISTING paragraph (its edited text/runs/format wins) and
    // overlay only the detector's structure/clause tags.
    const merged = { ...match };
    VIEWER_DETECTION_STRUCTURE_KEYS.forEach((tagKey) => {
      if (Object.prototype.hasOwnProperty.call(detectedParagraph, tagKey)) {
        merged[tagKey] = detectedParagraph[tagKey];
      } else if (Object.prototype.hasOwnProperty.call(merged, tagKey)) {
        // The detector no longer reports this tag for this paragraph — drop the
        // stale tag so clause/structure mapping reflects the fresh detection.
        delete merged[tagKey];
      }
    });
    return merged;
  });
}

function applyViewerReviewDetectionResult(result, reviewedText) {
  const previousSelectedClauseId = state.selectedReviewClauseId;
  const previousExportDecisions = { ...state.exportClauseDecisions };
  const previousTemplateSelections = { ...state.redlineTemplateSelections };
  const editSelection = snapshotViewerEditSelection();
  state.latestReviewResult = result;
  state.reviewClauses = result.clauses || [];
  // MERGE detection tags onto the existing edited paragraphs (bug 1+4): never
  // replace the run-bearing model with `result.paragraphs` — that discarded the
  // user's edited text/runs/format on every live detection refresh AND made a
  // single edit strip formatting from the whole export.
  state.reviewParagraphs = mergeViewerDetectionParagraphs(state.reviewParagraphs, result.paragraphs || []);
  state.reviewOriginalParagraphs = snapshotReviewParagraphs(state.reviewParagraphs);
  if (!paragraphsAlignWithBaseline(state.reviewParagraphs, state.reviewExportOriginalParagraphs)) {
    state.reviewExportOriginalParagraphs = snapshotReviewParagraphs(state.reviewParagraphs);
  }
  state.reviewRedlines = result.redline_edits || [];
  state.reviewSourceText = reviewedText;
  state.clauseJumpIndexes = {};
  state.selectedReviewClauseId = state.reviewClauses.some((clause) => clause.id === previousSelectedClauseId)
    ? previousSelectedClauseId
    : state.reviewClauses.find((clause) => !clausePasses(clause))?.id || state.reviewClauses[0]?.id || null;
  reconcileExportDecisions(previousExportDecisions);
  reconcileTemplateSelections(previousTemplateSelections);
  renderStudioResult({ clauses: state.reviewClauses });
  restoreViewerEditSelection(editSelection);
  updateExportButtonState();
}

function reconcileExportDecisions(previousExportDecisions) {
  const clauseIds = new Set(state.reviewClauses.map((clause) => clause.id));
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  Object.entries(previousExportDecisions || {}).forEach(([clauseId, included]) => {
    if (clauseIds.has(clauseId)) state.exportClauseDecisions[clauseId] = Boolean(included);
  });
}

function reconcileTemplateSelections(previousTemplateSelections) {
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  state.reviewRedlines.forEach((edit) => {
    const previousSelection = previousTemplateSelections?.[edit.id];
    if (previousSelection && (edit.template_options || []).some((option) => option.id === previousSelection)) {
      state.redlineTemplateSelections[edit.id] = previousSelection;
    }
  });
}

function updateManualRedlinePreview(editable, paragraph) {
  const container = editable.closest(".studio-doc-paragraph");
  if (!container) return;
  const manualRedline = manualParagraphRedline(paragraph, manualRedlineBaselineParagraphs());
  const backendRedline = selectedBackendRedline(paragraph.id);
  const previewState = reviewWorkstationModel()?.manualRedlinePreviewState({
    backendRedline,
    manualRedline,
    paragraph,
    workstation: state,
  });
  syncRenderedManualRedline(container, {
    paragraph,
    manualRedline,
    backendRedline,
    hasBackendRedline: previewState?.hasBackendRedline
      ?? effectiveReviewRedlines().some((edit) => edit.paragraph_id === paragraph.id),
  });
}

function selectedBackendRedline(paragraphId) {
  if (reviewWorkstationModel()) return reviewWorkstationModel().selectedBackendRedline(state, paragraphId);
  const paragraphRedlines = effectiveReviewRedlines().filter((edit) => edit.paragraph_id === paragraphId);
  return paragraphRedlines.find((edit) => edit.clause_id === state.selectedReviewClauseId) || paragraphRedlines[0] || null;
}

function editableParagraphText(editable) {
  return editable.innerText
    .replace(/\u00a0/g, " ")
    .replace(/\r\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function snapshotViewerEditSelection() {
  const editable = document.activeElement?.closest?.("[data-editable-paragraph-id]");
  if (!editable || !studioDocumentRender?.contains(editable)) return null;

  const snapshot = {
    editHistoryRecorded: editable.dataset.editHistoryRecorded || "",
    editStartText: editable.dataset.editStartText || "",
    endOffset: editableParagraphText(editable).length,
    paragraphId: editable.dataset.editableParagraphId,
    startOffset: editableParagraphText(editable).length,
  };
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return snapshot;

  const range = selection.getRangeAt(0);
  if (!editable.contains(range.startContainer) || !editable.contains(range.endContainer)) return snapshot;

  snapshot.startOffset = editableSelectionTextOffset(editable, range.startContainer, range.startOffset);
  snapshot.endOffset = editableSelectionTextOffset(editable, range.endContainer, range.endOffset);
  return snapshot;
}

function restoreViewerEditSelection(snapshot) {
  if (!snapshot?.paragraphId) return;
  const editable = studioDocumentRender?.querySelector(
    `[data-editable-paragraph-id="${cssEscape(snapshot.paragraphId)}"]`,
  );
  if (!editable) return;

  // A re-render rebuilds the paragraph without .is-editing; for a manual-redline
  // paragraph that collapses the editor (CSS), so re-add the class before focus
  // (a display:none element can't take focus).
  editable.closest(".studio-doc-paragraph")?.classList.add("is-editing");
  try {
    editable.focus({ preventScroll: true });
  } catch {
    editable.focus();
  }

  if (snapshot.editStartText) {
    editable.dataset.editStartText = snapshot.editStartText;
  }
  if (snapshot.editHistoryRecorded) {
    editable.dataset.editHistoryRecorded = snapshot.editHistoryRecorded;
  }

  const textLength = editableParagraphText(editable).length;
  const startOffset = clampTextOffset(snapshot.startOffset, textLength);
  const endOffset = clampTextOffset(snapshot.endOffset, textLength);
  const range = document.createRange();
  const startPosition = editableTextPositionForOffset(editable, startOffset);
  const endPosition = editableTextPositionForOffset(editable, endOffset);
  range.setStart(startPosition.node, startPosition.offset);
  range.setEnd(endPosition.node, endPosition.offset);

  const selection = window.getSelection();
  if (!selection) return;
  selection.removeAllRanges();
  selection.addRange(range);
}

function editableSelectionTextOffset(editable, node, offset) {
  const range = document.createRange();
  range.selectNodeContents(editable);
  try {
    range.setEnd(node, offset);
  } catch {
    return editableParagraphText(editable).length;
  }
  return range.toString().length;
}

function editableTextPositionForOffset(editable, offset) {
  const walker = document.createTreeWalker(editable, NodeFilter.SHOW_TEXT);
  let current;
  let remaining = offset;
  let lastTextNode = null;

  while ((current = walker.nextNode())) {
    lastTextNode = current;
    const length = current.textContent.length;
    if (remaining <= length) return { node: current, offset: remaining };
    remaining -= length;
  }

  if (lastTextNode) {
    return { node: lastTextNode, offset: lastTextNode.textContent.length };
  }
  return { node: editable, offset: 0 };
}

function clampTextOffset(offset, textLength) {
  const numericOffset = Number.isFinite(Number(offset)) ? Number(offset) : textLength;
  return Math.max(0, Math.min(numericOffset, textLength));
}

function syncReviewSourceFromParagraphs() {
  const text = state.reviewParagraphs
    .map((paragraph) => String(paragraph.text || "").trim())
    .filter(Boolean)
    .join("\n\n");
  state.reviewSourceText = text;
  // FIX 1 guard: if the user has unreconciled keystrokes pending in the source
  // textarea (state.sourceTextDirty, set by the input handler before its debounce
  // commits), do NOT overwrite the .value from the model -- that is exactly the
  // silent data-loss this fix closes. The model text is still tracked above; the
  // visible textarea keeps the in-flight edit until the reconcile lands. This
  // mirrors the redlineDraftDirty "don't clobber unsaved edits" pattern.
  if (state.sourceTextDirty) return;
  setSourceText(text);
}

function markSourceEdited(message, { preserveSourceDocument = false } = {}) {
  if (state.selectedDocument && !preserveSourceDocument) {
    state.selectedDocument = null;
  }
  if (message) {
    setFileMeta(message);
  }
}

function pastePlainText(event) {
  const text = event.clipboardData?.getData("text/plain");
  if (!text) return;
  event.preventDefault();
  const editable = event.target?.closest?.("[data-editable-paragraph-id]");
  insertPlainTextAtSelection(text);
  if (editable) {
    const inputEvent = typeof InputEvent === "function"
      ? new InputEvent("input", { bubbles: true, data: text, inputType: "insertFromPaste" })
      : new Event("input", { bubbles: true });
    editable.dispatchEvent(inputEvent);
  }
}

function insertPlainTextAtSelection(text) {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return;

  const range = selection.getRangeAt(0);
  range.deleteContents();

  const textNode = document.createTextNode(text);
  range.insertNode(textNode);
  range.setStartAfter(textNode);
  range.setEndAfter(textNode);
  range.collapse(false);

  selection.removeAllRanges();
  selection.addRange(range);
}

function selectReviewClause(clauseId, options = {}) {
  const transition = reviewWorkstationModel()?.nextClauseSelectionState(state, clauseId) || {
    reviewInspectorView: "clause",
    selectedReviewClauseId: clauseId,
  };
  state.selectedReviewClauseId = transition.selectedReviewClauseId;
  if (state.reviewInspectorView !== transition.reviewInspectorView) {
    state.reviewInspectorView = transition.reviewInspectorView;
    updateReviewInspectorTabs();
  }
  renderStudioResult({ clauses: state.reviewClauses });

  if (options.jump) {
    const clause = state.reviewClauses.find((item) => item.id === clauseId);
    requestAnimationFrame(() => jumpToClauseSource(clause));
  }
}

function jumpToClauseSource(clause) {
  if (!clause) return;

  if (studioDocumentRender && !studioDocumentRender.hidden) {
    scrollRenderedClauseToView(clause.id);
    return;
  }

  if (!studioNdaText.value.trim() || !clause.matched_text) return;
  const range = findExactTextRange(studioNdaText.value, clause.matched_text);
  if (!range) return;
  focusTextRange(studioNdaText, range.start, range.end);
}

function findExactTextRange(text, query) {
  const exactStart = text.indexOf(query);
  if (exactStart !== -1) {
    return {
      start: exactStart,
      end: exactStart + query.length,
    };
  }

  const searchIndex = createExactSearchIndex(text);
  const normalizedQuery = normalizeExactSearch(query);
  if (!normalizedQuery) return null;
  const start = searchIndex.normalized.indexOf(normalizedQuery);
  if (start === -1) return null;
  const endIndex = Math.min(start + normalizedQuery.length - 1, searchIndex.map.length - 1);
  return {
    start: searchIndex.map[start],
    end: searchIndex.map[endIndex] + 1,
  };
}

function createExactSearchIndex(text) {
  let normalized = "";
  const map = [];
  let previousWasSpace = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (/\s/.test(char)) {
      if (normalized && !previousWasSpace) {
        normalized += " ";
        map.push(index);
      }
      previousWasSpace = true;
      continue;
    }
    normalized += char;
    map.push(index);
    previousWasSpace = false;
  }

  return { normalized: normalized.trim(), map };
}

function normalizeExactSearch(value) {
  return String(value).replace(/\s+/g, " ").trim();
}

function focusTextRange(input, start, end) {
  const safeStart = Math.max(0, Math.min(start, input.value.length));
  const safeEnd = Math.max(safeStart, Math.min(end, input.value.length));

  try {
    input.focus({ preventScroll: true });
  } catch {
    input.focus();
  }

  input.setSelectionRange(safeStart, safeEnd);
  resizeSourceEditor(input);
  scrollTextareaToIndex(input, safeStart);
  pulseSourcePage(input);
}

function scrollTextareaToIndex(input, index) {
  const style = window.getComputedStyle(input);
  const fontSize = parseFloat(style.fontSize) || DEFAULT_FONT_SIZE_PX;
  const lineHeight = parseFloat(style.lineHeight) || fontSize * LINE_HEIGHT_FALLBACK_MULTIPLIER;
  const paddingX = (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
  const availableWidth = Math.max(input.clientWidth - paddingX, SOURCE_SCROLL_MIN_WIDTH_PX);
  const charsPerLine = Math.max(
    SOURCE_SCROLL_MIN_CHARS_PER_LINE,
    Math.floor(availableWidth / (fontSize * SOURCE_SCROLL_AVG_CHAR_WIDTH_EM)),
  );
  const visualLineCount = input.value
    .slice(0, index)
    .split("\n")
    .reduce((count, line) => count + Math.max(1, Math.ceil(line.length / charsPerLine)), 0);

  input.scrollTop = 0;

  const container = input.closest(".studio-page-wrap");
  if (!container) return;

  const targetTop = layoutOffsetTop(input) - layoutOffsetTop(container) + visualLineCount * lineHeight;
  container.scrollTo({
    behavior: "smooth",
    top: Math.max(0, targetTop - container.clientHeight * SOURCE_SCROLL_CONTEXT_RATIO),
  });
}

function scrollRenderedClauseToView(clauseId, options = {}) {
  const container = studioDocumentRender.closest(".studio-page-wrap");
  if (!container) return;

  const targets = renderedClauseTargets(clauseId);
  if (!targets.length) {
    // FIX 3 (P2): no anchor target. Previously this was a SILENT no-op -- a dead
    // clause click. This happens on image-rendered matters whose only render
    // anchors are whole page figures (no per-paragraph data-paragraph-id), e.g. an
    // un-converted pre-Approach-C PDF in the Original page-image view. The
    // structured/redline view DOES render data-paragraph-id anchors, so fall back:
    // if we are in the Original page-image view and have a structured paragraph
    // model to render, switch to Redline and re-jump once. Otherwise surface a
    // brief inline notice instead of failing silently.
    const inOriginalView = (state.documentViewMode || VIEW_MODE_REDLINE) === VIEW_MODE_ORIGINAL;
    const haveStructuredModel = Array.isArray(state.reviewParagraphs) && state.reviewParagraphs.length > 0;
    if (!options.fromFallback && inOriginalView && haveStructuredModel
      && typeof setDocumentViewMode === "function") {
      setDocumentViewMode(VIEW_MODE_REDLINE, { render: true });
      // Re-jump after the redline surface (with its paragraph anchors) paints.
      requestAnimationFrame(() => scrollRenderedClauseToView(clauseId, { fromFallback: true }));
      return;
    }
    try {
      console.warn(`scrollRenderedClauseToView: no anchor for clause ${clauseId}; jump unavailable in this view`);
    } catch (_loggingError) {
      /* never let a logging failure swallow the fallback */
    }
    if (studioResultMeta) {
      studioResultMeta.textContent = "Jump unavailable in page view — switch to Redline or Clean to locate this clause.";
    }
    return;
  }

  const nextIndex = state.clauseJumpIndexes[clauseId] || 0;
  const target = targets[nextIndex % targets.length];
  state.clauseJumpIndexes[clauseId] = (nextIndex + 1) % targets.length;
  if (!target) return;

  // FIX 3 (highlights-but-doesn't-scroll): the target IS found and gets the
  // selected/pulse class, but the document pane never scrolled to it -- a converted
  // PDF matter has 100+ data-paragraph-id paragraphs and clicking a clause chip
  // selected the right one (e.g. governing_law->p68) while .studio-page-wrap stayed
  // at scrollTop 0. The cause was the manual offset math:
  // layoutOffsetTop(target) - layoutOffsetTop(container) walks offsetParent chains,
  // and when the target's offsetParent is NOT the .studio-page-wrap scroller (a
  // positioned ancestor sits between them) the computed top is wrong and scrollTo
  // no-ops. scrollIntoView resolves the actual scrollable ancestor itself and needs
  // no offset arithmetic -- the same approach jumpToParagraph already uses
  // reliably. We keep scrollContainerToElement as a guarded explicit-scroll
  // fallback for environments without a working scrollIntoView.
  scrollContainerToElement(container, target);

  target.classList.remove("paragraph-pulse");
  void target.offsetWidth;
  target.classList.add("paragraph-pulse");
}

// Bring `element` into view within its scroll container. Prefers the native
// scrollIntoView (resolves the real scrollable ancestor; no offset math), and
// falls back to an explicit scrollTop set computed from bounding rects (NOT
// offsetParent chains) when scrollIntoView is unavailable.
function scrollContainerToElement(container, element) {
  if (!element) return;
  if (typeof element.scrollIntoView === "function") {
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (!container) return;
  const containerRect = container.getBoundingClientRect();
  const elementRect = element.getBoundingClientRect();
  const delta = elementRect.top - containerRect.top;
  const targetTop = container.scrollTop + delta - container.clientHeight * RENDERED_SCROLL_CONTEXT_RATIO;
  container.scrollTo({ behavior: "smooth", top: Math.max(0, targetTop) });
}

// Scroll the document to a specific paragraph id and flash it. Powers the
// clickable paragraph references in a clause assessment.
function jumpToParagraph(paragraphId) {
  const id = String(paragraphId || "");
  if (!id || !studioDocumentRender) return;
  const target = studioDocumentRender.querySelector(`[data-paragraph-id="${id}"]`)
    || studioDocumentRender.querySelector(`[data-editable-paragraph-id="${id}"]`);
  if (!target) return;
  // scrollIntoView locates the scrollable ancestor itself, so this works regardless
  // of which wrapper is the scroller and needs no manual offset math.
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.remove("paragraph-pulse");
  void target.offsetWidth;
  target.classList.add("paragraph-pulse");
}

function renderedClauseTargets(clauseId) {
  const clause = state.reviewClauses.find((item) => item.id === clauseId);
  const targetKeys = [];
  renderedOverlayPageTargets(clauseId).forEach((target) => targetKeys.push(target));
  (clause?.matched_paragraph_ids || []).forEach((paragraphId) => {
    targetKeys.push({ type: "paragraph", id: paragraphId });
  });
  effectiveReviewRedlines()
    .filter((edit) => edit.clause_id === clauseId)
    .forEach((edit) => {
      if (edit.action === REDLINE_INSERT_AFTER_PARAGRAPH && edit.id) {
        targetKeys.push({ type: "redline", id: edit.id });
      } else if (edit.paragraph_id) {
        targetKeys.push({ type: "paragraph", id: edit.paragraph_id });
      }
    });

  // Fallback for clauses with no grounded/redline anchor (typically "needs review"):
  // use the paragraphs the AI named in its assessment so the navigator still jumps.
  if (!targetKeys.length && typeof referencedParagraphIds === "function") {
    const text = `${clause?.finding || ""} ${clause?.reason || ""} ${clause?.rationale || ""}`;
    referencedParagraphIds(text).forEach((id) => targetKeys.push({ type: "paragraph", id }));
  }

  const seen = new Set();
  return targetKeys
    .filter((target) => {
      const key = `${target.type}:${target.id}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map((target) => {
      if (target.type === "overlay-page") {
        return studioDocumentRender.querySelector(`[data-review-render-page="${cssEscape(target.id)}"]`);
      }
      if (target.type === "redline") {
        return studioDocumentRender.querySelector(`[data-redline-edit-id="${cssEscape(target.id)}"]`);
      }
      return studioDocumentRender.querySelector(`[data-paragraph-id="${cssEscape(target.id)}"]`);
    })
    .filter(Boolean);
}

function renderedOverlayPageTargets(clauseId) {
  const anchors = state.reviewDocumentRender?.documentOverlay?.anchors;
  if (!Array.isArray(anchors)) return [];
  const pageNumbers = anchors
    .filter((anchor) => anchor?.clauseId === clauseId && anchor.pageNumber)
    .map((anchor) => String(anchor.pageNumber));
  return Array.from(new Set(pageNumbers)).map((id) => ({ type: "overlay-page", id }));
}

function layoutOffsetTop(element) {
  let offset = 0;
  let current = element;

  while (current) {
    offset += current.offsetTop || 0;
    current = current.offsetParent;
  }

  return offset;
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(String(value));
  return String(value).replace(/["\\]/g, "\\$&");
}

function pulseSourcePage(input) {
  const page = input.closest(".studio-page");
  if (!page) return;
  page.classList.remove("source-jump");
  void page.offsetWidth;
  page.classList.add("source-jump");
}

function loadMatterIntoReview(matter) {
  const reviewResult = matter.review_result || {};
  state.selectedMatter = matter;
  state.selectedDocument = null;
  setSourceText(matter.extracted_text || reviewResult.extracted_text || "");
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  setDocumentTitle(matter.document_title || matter.source_filename || DEFAULT_DOCUMENT_TITLE);
  setCounterpartyMeta(MatterUtils.counterpartyEmail(matter, state.gmailStatus));
  renderCounterpartyConfirmation(matter);
  // Pass the saved redline draft into renderResult so the INITIAL view-mode
  // decision (defaultDocumentViewModeForReviewResult) can see it: a matter with
  // saved redline work opens on Redline even when its PDF source would otherwise
  // prefer the Original surface. The draft signal must ride along here because
  // the mode is computed inside renderResult BEFORE applyMatterRedlineDraft runs.
  renderResult(reviewResult, matter.extracted_text || reviewResult.extracted_text || "", {
    redlineDraft: matter.redline_draft,
  });
  applyMatterRedlineDraft(matter.redline_draft);
  renderReviewRefreshNotice(matter.review_refresh);
  // The freshness file-meta line follows the SAME (a)/(b)/(c) contract as the
  // header indicator (renderReviewRefreshNotice). On a plain OPEN there is no
  // genuine-drift signal: review_edited_since_load is unset (an in-session edit sets
  // it AFTER load) and the broad review_may_be_stale flag (set on EVERY open merely
  // because the open path does not re-run AI) is DELIBERATELY not treated as drift.
  // So the only load-time freshness warning is the narrow server gate
  // (review_refresh.stale) — otherwise the line stays quiet and the normal
  // "matter loaded" copy speaks, keeping a plain reopen in the confident (b) state.
  const refreshMessage = matter.review_refresh?.redline_draft_cleared
    ? matter.review_refresh.message || "Saved redline draft cleared after review refresh"
    : matter.review_refresh?.stale
      ? staleReviewMessage(matter.review_refresh)
      : "";
  setFileMeta(
    refreshMessage
      || (matter.redline_draft
        ? `${RepositoryView.sourceTypeLabel(matter.source_type)} NDA loaded - draft redline saved`
        : `${RepositoryView.sourceTypeLabel(matter.source_type)} NDA loaded`)
  );
  activateTab("review");
  // RESUME the background-review poll when a matter is opened (or its tab is
  // re-entered, or the page reloaded) while its review is STILL in flight. Without
  // this, a plain open of an in-progress matter renders a disabled "Reviewing…"
  // header but starts NO poll, so the matter strands forever with no terminal
  // transition — half the "review never finishes" bug. We re-enter the in-flight UI
  // (spinner + skeletons) and (re)start the single-in-flight poll, which then drives
  // the matter to its terminal completed/failed/interrupted state exactly as the
  // in-session refresh path does.
  //
  // Fires for `in_progress` (a live worker) AND `stalled` (a live-but-slow review):
  // both are still running server-side, so resuming the poll lets them resolve. It
  // does NOT fire for `interrupted` (the worker died — terminal/recoverable, handled
  // by the calm Retry render) or any other terminal status. typeof-guarded so an
  // isolated load order / test harness without the actions module is a no-op.
  maybeResumeReviewPollOnLoad(matter);
  requestAnimationFrame(resizeSourceEditors);
}

// Resume the background-review poll for a matter loaded while its review is in
// flight (review_status in_progress/stalled). Shared by every load funnel
// (openMatterInReview, tab re-entry, reload) since they all route through
// loadMatterIntoReview. Idempotent: startReviewPoll is single-in-flight and no-ops
// when a poll for this matter is already running.
function maybeResumeReviewPollOnLoad(matter) {
  const status = String(matter?.review_status || "");
  const live = status === "in_progress" || status === "stalled";
  if (!live) return;
  if (typeof startReviewPoll !== "function") return;
  if (typeof enterReviewInFlightUi === "function") enterReviewInFlightUi();
  startReviewPoll(matter.id);
}

function prepareMatterReviewLoad(matter) {
  state.selectedMatter = matter;
  state.selectedDocument = null;
  setSourceText(matter.extracted_text || "");
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  setDocumentTitle(matter.document_title || matter.source_filename || DEFAULT_DOCUMENT_TITLE);
  setCounterpartyMeta(MatterUtils.counterpartyEmail(matter, state.gmailStatus));
  renderCounterpartyConfirmation(matter);
  renderStudioEmpty();
  setFileMeta(`${RepositoryView.sourceTypeLabel(matter.source_type)} NDA loading review`);
  activateTab("review");
  requestAnimationFrame(resizeSourceEditors);
}

function showMatterReviewLoadError(message) {
  setFileMeta(message || "NDA review details could not load.");
}
