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
  if (ReviewWorkstationModel.pushReviewEditHistoryEntry(state, entry)) {
    updateReviewUndoButtonState();
  }
}

function currentParagraphText(paragraphId) {
  return ReviewWorkstationModel.currentParagraphText(state, paragraphId);
}

function undoLastViewerEdit() {
  const lastEdit = state.reviewEditHistory?.pop();
  if (!lastEdit) {
    updateReviewUndoButtonState();
    return;
  }

  const restored = ReviewWorkstationModel.restoreReviewEditHistoryEntryState(state, lastEdit);
  if (!restored.restored) {
    updateReviewUndoButtonState();
    return;
  }

  if (restored.sourceTextChanged) {
    setSourceText(state.reviewSourceText);
  }
  markRedlineDraftDirty();
  if (restored.type === "clause_export_decision"
    || restored.type === "redline_template_selection"
    || restored.type === "redline_export_decision") {
    renderStudioResult({ clauses: state.reviewClauses });
    setFileMeta("Undid clause suggestion change");
    studioResultMeta.textContent = "Clause suggestion change undone.";
  } else {
    if (restored.type === "review_comments" && typeof closeParagraphCommentComposers === "function") {
      closeParagraphCommentComposers();
    }
    renderStudioDocumentHighlights();
    if (typeof renderStudioClauseLane === "function") renderStudioClauseLane();
    if (restored.type === "paragraph_text") {
      markSourceEdited("Undid viewer edit", { preserveSourceDocument: true });
      scheduleViewerReviewRefresh("Last viewer edit undone");
    } else {
      setFileMeta(restored.type === "review_comments" ? "Undid comment change" : "Undid formatting change");
    }
  }
  updateExportButtonState();
  updateReviewUndoButtonState();
}

function syncViewerParagraphEdit(editable) {
  const paragraphId = editable.dataset.editableParagraphId;
  const edit = ReviewWorkstationModel.syncViewerParagraphEditState(state, paragraphId, editableParagraphText(editable));
  if (!edit.paragraph) return;

  setSourceText(state.reviewSourceText);
  updateManualRedlinePreview(editable, edit.paragraph);
  markRedlineDraftDirty();
  markSourceEdited("Edited in viewer", { preserveSourceDocument: true });
  scheduleViewerReviewRefresh("Document edited");
  updateExportButtonState();
  if (edit.droppedInlineFormat) {
    setFileMeta("Inline formatting on this paragraph was cleared by the text edit");
  }
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
  const editSelection = snapshotViewerEditSelection();
  ReviewWorkstationModel.applyViewerReviewDetectionState(state, result, reviewedText);
  renderStudioResult({ clauses: state.reviewClauses });
  restoreViewerEditSelection(editSelection);
  updateExportButtonState();
}

function updateManualRedlinePreview(editable, paragraph) {
  const container = editable.closest(".studio-doc-paragraph");
  if (!container) return;
  const manualRedline = manualParagraphRedline(paragraph, manualRedlineBaselineParagraphs());
  const backendRedline = selectedBackendRedline(paragraph.id);
  const hasBackendRedline = ReviewWorkstationModel.effectiveReviewRedlines(state).some((edit) => edit.paragraph_id === paragraph.id);
  syncRenderedManualRedline(container, { paragraph, manualRedline, backendRedline, hasBackendRedline });
}

function selectedBackendRedline(paragraphId) {
  const paragraphRedlines = ReviewWorkstationModel.effectiveReviewRedlines(state).filter((edit) => edit.paragraph_id === paragraphId);
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
  state.reviewSourceText = ReviewWorkstationModel.reviewSourceTextFromParagraphs(state.reviewParagraphs);
  setSourceText(state.reviewSourceText);
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
  const selection = ReviewWorkstationModel.selectReviewClauseState(state, clauseId);
  if (selection.inspectorViewChanged) {
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
  ReviewWorkstationModel.effectiveReviewRedlines(state)
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
  renderResult(reviewResult, matter.extracted_text || reviewResult.extracted_text || "");
  applyMatterRedlineDraft(matter.redline_draft);
  renderReviewRefreshNotice(matter.review_refresh);
  const refreshMessage = matter.review_refresh?.redline_draft_cleared
    ? matter.review_refresh.message || "Saved redline draft cleared after review refresh"
    : matter.review_refresh?.stale
      ? staleReviewMessage(matter.review_refresh)
      : "";
  setFileMeta(
    refreshMessage
      || (matter.redline_draft
        ? `${RepositoryView.sourceTypeLabel(matter.source_type)} matter loaded - draft redline saved`
        : `${RepositoryView.sourceTypeLabel(matter.source_type)} matter loaded`)
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
