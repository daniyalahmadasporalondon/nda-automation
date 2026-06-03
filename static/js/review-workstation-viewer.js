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
    });
    editable.addEventListener("paste", pastePlainText);
  });
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

  const paragraph = state.reviewParagraphs.find((item) => item.id === lastEdit.paragraphId);
  if (!paragraph) {
    updateReviewUndoButtonState();
    return;
  }

  paragraph.text = lastEdit.previousText;
  syncReviewSourceFromParagraphs();
  markRedlineDraftDirty();
  markSourceEdited("Undid viewer edit", { preserveSourceDocument: true });
  renderStudioDocumentHighlights();
  scheduleViewerReviewRefresh("Last viewer edit undone");
  updateExportButtonState();
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

function syncViewerParagraphEdit(editable) {
  const paragraphId = editable.dataset.editableParagraphId;
  const paragraph = state.reviewParagraphs.find((item) => item.id === paragraphId);
  if (!paragraph) return;

  paragraph.text = editableParagraphText(editable);
  syncReviewSourceFromParagraphs();
  updateManualRedlinePreview(editable, paragraph);
  markRedlineDraftDirty();
  markSourceEdited("Edited in viewer", { preserveSourceDocument: true });
  scheduleViewerReviewRefresh("Document edited");
  updateExportButtonState();
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
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
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

function applyViewerReviewDetectionResult(result, reviewedText) {
  const previousSelectedClauseId = state.selectedReviewClauseId;
  const previousExportDecisions = { ...state.exportClauseDecisions };
  const previousTemplateSelections = { ...state.redlineTemplateSelections };
  const editSelection = snapshotViewerEditSelection();
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
  const hasBackendRedline = effectiveReviewRedlines().some((edit) => edit.paragraph_id === paragraph.id);
  syncRenderedManualRedline(container, { paragraph, manualRedline, backendRedline, hasBackendRedline });
}

function selectedBackendRedline(paragraphId) {
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
  state.selectedReviewClauseId = clauseId;
  if (state.reviewInspectorView !== "clause") {
    state.reviewInspectorView = "clause";
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

function scrollRenderedClauseToView(clauseId) {
  const container = studioDocumentRender.closest(".studio-page-wrap");
  if (!container) return;

  const targets = renderedClauseTargets(clauseId);
  if (!targets.length) return;

  const nextIndex = state.clauseJumpIndexes[clauseId] || 0;
  const target = targets[nextIndex % targets.length];
  state.clauseJumpIndexes[clauseId] = (nextIndex + 1) % targets.length;
  if (!target) return;

  const targetTop = layoutOffsetTop(target) - layoutOffsetTop(container);
  container.scrollTo({
    behavior: "smooth",
    top: Math.max(0, targetTop - container.clientHeight * RENDERED_SCROLL_CONTEXT_RATIO),
  });

  target.classList.remove("paragraph-pulse");
  void target.offsetWidth;
  target.classList.add("paragraph-pulse");
}

function renderedClauseTargets(clauseId) {
  const clause = state.reviewClauses.find((item) => item.id === clauseId);
  const targetKeys = [];
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

  const seen = new Set();
  return targetKeys
    .filter((target) => {
      const key = `${target.type}:${target.id}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map((target) => {
      if (target.type === "redline") {
        return studioDocumentRender.querySelector(`[data-redline-edit-id="${cssEscape(target.id)}"]`);
      }
      return studioDocumentRender.querySelector(`[data-paragraph-id="${cssEscape(target.id)}"]`);
    })
    .filter(Boolean);
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
  renderResult(reviewResult, matter.extracted_text || reviewResult.extracted_text || "");
  applyMatterRedlineDraft(matter.redline_draft);
  setFileMeta(
    matter.redline_draft
      ? `${RepositoryView.sourceTypeLabel(matter.source_type)} matter loaded - draft redline saved`
      : `${RepositoryView.sourceTypeLabel(matter.source_type)} matter loaded`
  );
  activateTab("review");
  requestAnimationFrame(resizeSourceEditors);
}

function prepareMatterReviewLoad(matter) {
  state.selectedMatter = matter;
  state.selectedDocument = null;
  setSourceText(matter.extracted_text || "");
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  setDocumentTitle(matter.document_title || matter.source_filename || DEFAULT_DOCUMENT_TITLE);
  setCounterpartyMeta(MatterUtils.counterpartyEmail(matter, state.gmailStatus));
  renderStudioEmpty();
  setFileMeta(`${RepositoryView.sourceTypeLabel(matter.source_type)} matter loading review`);
  activateTab("review");
  requestAnimationFrame(resizeSourceEditors);
}

function showMatterReviewLoadError(message) {
  setFileMeta(message || "Matter review details could not load.");
}
