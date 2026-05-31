const fileInput = document.querySelector("#fileInput");
const studioDocTitle = document.querySelector("#studioDocTitle");
const studioNdaText = document.querySelector("#studioNdaText");
const studioDocumentRender = document.querySelector("#studioDocumentRender");
const studioFileMeta = document.querySelector("#studioFileMeta");
const studioReviewButton = document.querySelector("#studioReviewButton");
const studioExportButton = document.querySelector("#studioExportButton");
const studioClearButton = document.querySelector("#studioClearButton");
const studioClauseLane = document.querySelector("#studioClauseLane");
const studioIssueList = document.querySelector("#studioIssueList");
const studioDetailPanel = document.querySelector("#studioDetailPanel");
const studioMatchSummary = document.querySelector("#studioMatchSummary");
const studioOverallTitle = document.querySelector("#studioOverallTitle");
const studioResultMark = document.querySelector("#studioResultMark");
const studioResultMeta = document.querySelector("#studioResultMeta");
const tabButtons = document.querySelectorAll("[data-tab]");
const views = document.querySelectorAll("[data-view]");
const playbookList = document.querySelector("#playbookList");
const clauseDetail = document.querySelector("#clauseDetail");

const state = {
  playbookClauses: [],
  selectedClauseId: null,
  selectedDocument: null,
  reviewClauses: [],
  reviewOriginalParagraphs: [],
  reviewParagraphs: [],
  reviewRedlines: [],
  reviewDirty: false,
  reviewSourceText: "",
  selectedReviewClauseId: null,
  clauseJumpIndexes: {},
  lastExport: null,
  documentViewMode: VIEW_MODE_REDLINE,
};

setupSourceEditors();
setActiveTab("review");
setupDocumentViewModes();

const emptyState = () => {
  renderStudioEmpty();
};

emptyState();
loadPlaybook();

tabButtons.forEach((button) => {
  button.addEventListener("click", () => {
    setActiveTab(button.dataset.tab);
    requestAnimationFrame(resizeSourceEditors);
  });
});

fileInput.addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const extension = file.name.split(".").pop().toLowerCase();

  if (extension === "docx") {
    state.selectedDocument = file;
    setSourceText("");
    showStudioSourceEditor();
    resizeSourceEditors();
    setSourcePlaceholder("Word document selected");
    setFileMeta(`${file.name} ready for review`);
    setDocumentTitle(file.name);
    resetReviewResults();
    renderStudioEmpty();
    setActiveTab("review");
    return;
  }

  state.selectedDocument = null;
  const fileText = await file.text();
  setSourceText(fileText);
  showStudioSourceEditor();
  resizeSourceEditors();
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  setFileMeta(`${file.name} loaded as text`);
  setDocumentTitle(file.name);
  resetReviewResults();
  renderStudioEmpty();
  setActiveTab("review");
});

function clearReview() {
  setSourceText("");
  showStudioSourceEditor();
  resizeSourceEditors();
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  fileInput.value = "";
  state.selectedDocument = null;
  setFileMeta("No file selected");
  setDocumentTitle(DEFAULT_DOCUMENT_TITLE);
  resetReviewResults();
  emptyState();
}

function resetReviewResults() {
  state.reviewClauses = [];
  state.reviewOriginalParagraphs = [];
  state.reviewParagraphs = [];
  state.reviewRedlines = [];
  state.reviewDirty = false;
  state.reviewSourceText = "";
  state.selectedReviewClauseId = null;
  state.clauseJumpIndexes = {};
  state.lastExport = null;
}

studioClearButton.addEventListener("click", () => {
  clearReview();
});

studioReviewButton.addEventListener("click", async () => {
  await runReview(studioNdaText, studioReviewButton);
});

studioExportButton.addEventListener("click", async () => {
  await exportReviewDocx();
});

async function runReview(sourceInput, button) {
  const text = sourceInput.value.trim();
  if (!text && !state.selectedDocument) {
    emptyState();
    studioOverallTitle.textContent = "Add NDA text";
    studioResultMark.textContent = "-";
    studioResultMeta.textContent = "Paste NDA text or upload a document to run the checklist.";
    studioMatchSummary.textContent = `0/${getClauseTotal()}`;
    return;
  }

  button.disabled = true;
  button.textContent = "Reviewing";

  try {
    const response = state.selectedDocument
      ? await reviewDocument(state.selectedDocument)
      : await fetch("/api/review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Review could not run");
    const reviewedText = payload.extracted_text || text;
    if (payload.extracted_text) {
      setSourceText(payload.extracted_text);
      resizeSourceEditors();
      setSourcePlaceholder(SOURCE_PLACEHOLDER);
      setFileMeta(`${payload.source.filename} reviewed from Word document`);
    }
    renderResult(payload, reviewedText);
  } catch (error) {
    studioOverallTitle.textContent = error.message;
    studioResultMark.textContent = "!";
    studioResultMark.className = "check";
    studioResultMeta.textContent = "Review could not run.";
  } finally {
    button.disabled = false;
    button.textContent = "Review NDA";
  }
}

async function exportReviewDocx() {
  const text = studioNdaText.value.trim() || state.reviewSourceText.trim();
  if (!text) return;

  studioExportButton.disabled = true;
  studioExportButton.textContent = "Exporting";

  try {
    const payload = {
      text,
      reviewed_text: text,
      title: studioDocTitle.textContent || DEFAULT_DOCUMENT_TITLE,
    };
    if (state.selectedDocument) {
      payload.filename = state.selectedDocument.name;
      payload.content_base64 = await fileToBase64(state.selectedDocument);
    }

    const response = await fetch("/api/export-review-docx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || "Export could not run");
    }
    const blob = await response.blob();
    const filename = downloadFilename(response) || "nda-review-report.docx";
    downloadBlob(blob, filename);
    renderExportSuccess(filename, response.headers.get("X-Export-Path"), response.headers.get("X-Export-URL"));
  } catch (error) {
    studioOverallTitle.textContent = error.message;
    studioResultMark.textContent = "!";
    studioResultMark.className = "check";
    studioResultMeta.textContent = "Export could not run.";
  } finally {
    studioExportButton.textContent = "Export DOCX";
    updateExportButtonState();
  }
}

async function reviewDocument(file) {
  const contentBase64 = await fileToBase64(file);
  return fetch("/api/review-document", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filename: file.name,
      content_base64: contentBase64,
    }),
  });
}

async function fileToBase64(file) {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += FILE_BASE64_CHUNK_SIZE) {
    const chunk = bytes.subarray(index, index + FILE_BASE64_CHUNK_SIZE);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_URL_REVOKE_DELAY_MS);
}

function renderExportSuccess(filename, savedPath, savedUrl) {
  state.lastExport = { filename, savedPath, savedUrl };
  studioFileMeta.textContent = "";
  const summary = document.createElement("span");
  summary.className = "export-success";
  summary.textContent = savedUrl ? `Saved export: ${savedUrl}` : `${filename} exported`;
  studioFileMeta.append(summary);
  if (savedUrl) {
    studioFileMeta.append(document.createTextNode(" "));
    const link = document.createElement("a");
    link.className = "download-again";
    link.href = savedUrl;
    link.download = filename;
    link.textContent = "Download again";
    studioFileMeta.append(link);
  } else if (savedPath) {
    studioFileMeta.append(document.createTextNode(` ${savedPath}`));
  }
}

function downloadFilename(response) {
  const contentDisposition = response.headers.get("Content-Disposition") || "";
  const match = contentDisposition.match(/filename="?([^";]+)"?/i);
  return match ? match[1] : "";
}

function setSourceText(text) {
  studioNdaText.value = text;
}

function setSourcePlaceholder(placeholder) {
  studioNdaText.placeholder = placeholder;
}

function setFileMeta(message) {
  studioFileMeta.textContent = message;
}

function setDocumentTitle(title) {
  studioDocTitle.textContent = title;
}

function setupSourceEditors() {
  studioNdaText.addEventListener("input", () => {
    resizeSourceEditor(studioNdaText);
    if (studioNdaText.value.trim()) {
      markSourceEdited("Text edited");
    }
  });
  resizeSourceEditor(studioNdaText);
}

function resizeSourceEditors() {
  resizeSourceEditor(studioNdaText);
}

function resizeSourceEditor(input) {
  if (!input || input.hidden) return;
  input.style.height = "auto";
  input.style.height = `${Math.max(input.scrollHeight, input.clientHeight)}px`;
}

function showStudioSourceEditor() {
  if (!studioDocumentRender) return;
  studioDocumentRender.hidden = true;
  studioDocumentRender.innerHTML = "";
  studioNdaText.hidden = false;
  resizeSourceEditor(studioNdaText);
}

function showStudioDocumentRender() {
  if (!studioDocumentRender) return;
  studioNdaText.hidden = true;
  studioDocumentRender.hidden = false;
}

function renderResult(result, reviewedText) {
  state.reviewClauses = result.clauses || [];
  state.reviewParagraphs = result.paragraphs || [];
  state.reviewOriginalParagraphs = state.reviewParagraphs.map((paragraph) => ({
    id: paragraph.id,
    text: String(paragraph.text || ""),
  }));
  state.reviewRedlines = result.redline_edits || [];
  state.reviewDirty = false;
  state.reviewSourceText = reviewedText || studioNdaText.value.trim();
  state.clauseJumpIndexes = {};
  state.selectedReviewClauseId =
    state.reviewClauses.find((clause) => !clausePasses(clause))?.id || state.reviewClauses[0]?.id || null;
  renderStudioResult(result);
  updateExportButtonState();
}

function renderStudioEmpty() {
  if (!studioIssueList) return;
  showStudioSourceEditor();
  studioMatchSummary.textContent = `0/${getClauseTotal()}`;
  studioResultMark.textContent = "-";
  studioResultMark.className = "";
  studioOverallTitle.textContent = "Awaiting review";
  studioResultMeta.textContent = "No hard-clause review has run yet.";
  studioIssueList.innerHTML = '<div class="studio-empty">No review yet</div>';
  studioDetailPanel.innerHTML = `
    <p class="eyebrow">selected clause</p>
    <p>No review yet.</p>
  `;
  updateExportButtonState();
  renderStudioClauseLane();
}

function updateExportButtonState() {
  if (!studioExportButton) return;
  studioExportButton.disabled = !state.reviewClauses.length || !(studioNdaText.value.trim() || state.reviewSourceText.trim());
}

function renderStudioResult(result) {
  if (!studioIssueList) return;
  const clauses = result.clauses || [];
  renderStudioSummary(clauses);
  renderStudioIssueList(clauses);
  renderStudioClauseLane();
  renderStudioDetail();
  renderStudioDocumentHighlights();
}

function renderStudioSummary(clauses) {
  const passedCount = clauses.filter((clause) => clauseStatus(clause).passes).length;
  const failedCount = clauses.filter((clause) => clauseStatus(clause).needsReview).length;
  studioMatchSummary.textContent = `${passedCount}/${getClauseTotal(clauses)}`;
  studioResultMark.textContent = failedCount ? "CHECK" : "PASS";
  studioResultMark.className = failedCount ? "check" : "pass";
  studioOverallTitle.textContent = failedCount ? "Does not meet requirements" : "Meets requirements";
  studioResultMeta.textContent = failedCount
    ? `${failedCount} hard ${failedCount === 1 ? "clause needs" : "clauses need"} checking.`
    : "All hard clauses are currently satisfied.";
}

function renderStudioIssueList(clauses) {
  studioIssueList.innerHTML = clauses
    .map((clause) => {
      const selected = clause.id === state.selectedReviewClauseId ? "selected" : "";
      const status = clauseStatus(clause);
      return `
        <button class="studio-issue-card ${selected} ${status.tone}" type="button" data-studio-clause-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}">
          <span class="studio-issue-card-top">
            <span class="studio-issue-title">${escapeHtml(clause.name)}</span>
            <strong class="studio-issue-pill ${status.tone}">${status.pillLabel}</strong>
          </span>
          <span class="studio-issue-finding">${escapeHtml(clause.reason || clause.finding || "Clause review available.")}</span>
        </button>
      `;
    })
    .join("");

  bindClauseSelection(studioIssueList, "[data-studio-clause-id]", "studioClauseId");
}

function getClauseTotal(clauses = []) {
  return clauses.length || state.playbookClauses.length || 0;
}

function hasReviewResults() {
  return state.reviewClauses.length > 0;
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
  return state.reviewRedlines.filter((edit) => edit.clause_id === state.selectedReviewClauseId);
}

function bindClauseSelection(container, selector, datasetKey) {
  container.querySelectorAll(selector).forEach((item) => {
    item.addEventListener("click", () => {
      selectReviewClause(item.dataset[datasetKey], { jump: true });
    });
  });
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
      const tag = hasReviewResults() ? "button" : "div";
      const type = hasReviewResults() ? ' type="button"' : "";
      const data = hasReviewResults()
        ? ` data-studio-lane-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}"`
        : "";
      return `
        <${tag} class="studio-clause-item ${selected}"${type}${data}>
          <span class="studio-clause-dot ${status.dotTone}"></span>
          <strong>${index + 1}</strong>
          <span>${escapeHtml(clause.name)}</span>
        </${tag}>
      `;
    })
    .join("");

  bindClauseSelection(studioClauseLane, "[data-studio-lane-id]", "studioLaneId");
}

function renderStudioDetail() {
  const clause = getSelectedReviewClause();
  if (!clause) return;
  const status = clauseStatus(clause);
  const whyText = clause.reason || clause.finding || "Clause review available.";
  const excerpt = clause.matched_text
    ? `<div class="studio-detail-block studio-detail-evidence"><small>Exact paragraph</small><p>${escapeHtml(clause.matched_text)}</p></div>`
    : '<div class="studio-detail-block studio-detail-evidence muted"><small>Exact paragraph</small><p>No matching paragraph identified.</p></div>';
  const fixBlock = status.needsReview && clause.what_to_fix
    ? `<div class="studio-detail-block fix-block"><small>What to fix</small><p>${escapeHtml(clause.what_to_fix)}</p></div>`
    : "";
  const redlineEdits = getSelectedRedlineEdits();
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
  studioDetailPanel.innerHTML = `
    <p class="eyebrow">selected clause</p>
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
      <div class="studio-detail-block issue-block ${escapeHtml(status.tone)}">
        <small>Issue type</small>
        <p>${escapeHtml(status.issueLabel)}</p>
      </div>
      <div class="studio-detail-block finding-block">
        <small>Why</small>
        <p>${escapeHtml(whyText)}</p>
      </div>
      ${fixBlock}
      ${redlineBlock}
      <div class="studio-detail-block">
        <small>Backend result</small>
        <p>${escapeHtml(status.resultLabel)}</p>
      </div>
      ${acceptableLanguage}
    </div>
  `;
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
        <div class="redline-option ${option.selected ? "selected" : ""}">
          <strong>${escapeHtml(option.label || "Option")}${option.selected ? " - Default" : ""}</strong>
          <span>${escapeHtml(option.text || option.replacement_text || option.insert_text || "")}</span>
        </div>
      `).join("")}
    </div>
  `;
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
    originalParagraphs: state.reviewOriginalParagraphs,
    paragraphs: state.reviewParagraphs,
    redlines: state.reviewRedlines,
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

  showStudioDocumentRender();
}

function setupDocumentViewModes() {
  const buttons = document.querySelectorAll(".studio-view-switch [data-view-mode]");
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      setDocumentViewMode(button.dataset.viewMode, { render: true });
    });
  });
  updateDocumentViewModeButtons();
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
      editable.closest(".studio-doc-paragraph")?.classList.add("is-editing");
    });
    editable.addEventListener("blur", () => {
      editable.closest(".studio-doc-paragraph")?.classList.remove("is-editing");
    });
    editable.addEventListener("input", () => {
      syncViewerParagraphEdit(editable);
    });
    editable.addEventListener("paste", pastePlainText);
  });
}

function syncViewerParagraphEdit(editable) {
  const paragraphId = editable.dataset.editableParagraphId;
  const paragraph = state.reviewParagraphs.find((item) => item.id === paragraphId);
  if (!paragraph) return;

  paragraph.text = editableParagraphText(editable);
  syncReviewSourceFromParagraphs();
  updateManualRedlinePreview(editable, paragraph);
  markSourceEdited("Edited in viewer");
  studioResultMeta.textContent = "Document edited. Run Review NDA again to refresh the checklist.";
  updateExportButtonState();
}

function updateManualRedlinePreview(editable, paragraph) {
  const container = editable.closest(".studio-doc-paragraph");
  if (!container) return;
  const manualRedline = manualParagraphRedline(paragraph, state.reviewOriginalParagraphs);
  const backendRedline = selectedBackendRedline(paragraph.id);
  const hasBackendRedline = state.reviewRedlines.some((edit) => edit.paragraph_id === paragraph.id);
  syncRenderedManualRedline(container, { paragraph, manualRedline, backendRedline, hasBackendRedline });
}

function selectedBackendRedline(paragraphId) {
  const paragraphRedlines = state.reviewRedlines.filter((edit) => edit.paragraph_id === paragraphId);
  return paragraphRedlines.find((edit) => edit.clause_id === state.selectedReviewClauseId) || paragraphRedlines[0] || null;
}

function editableParagraphText(editable) {
  return editable.innerText
    .replace(/\u00a0/g, " ")
    .replace(/\r\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function syncReviewSourceFromParagraphs() {
  const text = state.reviewParagraphs
    .map((paragraph) => String(paragraph.text || "").trim())
    .filter(Boolean)
    .join("\n\n");
  state.reviewSourceText = text;
  setSourceText(text);
}

function markSourceEdited(message) {
  if (state.reviewClauses.length || state.reviewSourceText.trim()) {
    state.reviewDirty = true;
  }
  if (state.selectedDocument) {
    state.selectedDocument = null;
    fileInput.value = "";
  }
  if (message) {
    setFileMeta(message);
  }
}

function pastePlainText(event) {
  const text = event.clipboardData?.getData("text/plain");
  if (!text) return;
  event.preventDefault();
  insertPlainTextAtSelection(text);
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
  state.reviewRedlines
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

async function loadPlaybook() {
  playbookList.innerHTML = '<div class="playbook-loading">Loading clauses</div>';
  clauseDetail.innerHTML = '<div class="detail-empty">Loading playbook</div>';

  try {
    const response = await fetch("/playbook");
    const playbook = await response.json();
    if (!response.ok) throw new Error(playbook.error || "Playbook could not load");

    state.playbookClauses = playbook.clauses || [];
    state.selectedClauseId = state.playbookClauses[0]?.id || null;
    renderStudioEmpty();
    renderPlaybookList();
    renderClauseDetail();
  } catch (error) {
    playbookList.innerHTML = `<div class="playbook-loading">${escapeHtml(error.message)}</div>`;
    clauseDetail.innerHTML = '<div class="detail-empty">Playbook unavailable</div>';
  }
}

function setActiveTab(tabName) {
  tabButtons.forEach((button) => {
    const active = button.dataset.tab === tabName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  views.forEach((view) => {
    const active = view.dataset.view === tabName;
    view.classList.toggle("active", active);
    view.hidden = !active;
  });
}

function renderPlaybookList() {
  playbookList.innerHTML = state.playbookClauses
    .map((clause, index) => {
      const selected = clause.id === state.selectedClauseId ? "selected active" : "";
      const position = String(index + 1).padStart(2, "0");
      return `
        <button class="playbook-row ${selected}" type="button" data-clause-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}">
          <span class="clause-number">${position}</span>
          <span>
            <strong>${escapeHtml(clause.name)}</strong>
            <small>${escapeHtml(clause.type)}</small>
          </span>
        </button>
      `;
    })
    .join("");

  playbookList.querySelectorAll("[data-clause-id]").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedClauseId = row.dataset.clauseId;
      renderPlaybookList();
      renderClauseDetail();
    });
  });
}

function renderClauseDetail() {
  const clause = state.playbookClauses.find((item) => item.id === state.selectedClauseId);
  if (!clause) {
    clauseDetail.innerHTML = '<div class="detail-empty">No clause selected</div>';
    return;
  }

  const lawChips = (clause.approved_laws || [])
    .map((law) => `<span>${escapeHtml(law)}</span>`)
    .join("");
  const maxTermYears = clause.max_term_years || clause.term_years;
  const termYears = maxTermYears
    ? `<div class="fact-box"><small>Term cap</small><strong>Up to ${escapeHtml(maxTermYears)} years</strong></div>`
    : "";
  const approvedLaws = lawChips
    ? `<div class="law-strip">${lawChips}</div>`
    : "";

  clauseDetail.innerHTML = `
    <div class="detail-header">
      <div>
        <p class="eyebrow">clause ${escapeHtml(clause.id)}</p>
        <h2>${escapeHtml(clause.name)}</h2>
      </div>
      <span class="policy-chip ${escapeHtml(clause.type)}">${escapeHtml(clause.type)}</span>
    </div>

    <div class="requirement-panel">
      <small>Requirement</small>
      <p>${escapeHtml(clause.requirement)}</p>
    </div>

    <div class="detail-grid">
      <div class="fact-box">
        <small>Checker outcome</small>
        <strong>${clause.type === "prohibited" ? "Must be absent" : "Must be present"}</strong>
      </div>
      <div class="fact-box">
        <small>Source</small>
        <strong>playbook.json</strong>
      </div>
      ${termYears}
    </div>

    ${approvedLaws}
  `;
}
