const fileInput = document.querySelector("#fileInput");
const studioDocTitle = document.querySelector("#studioDocTitle");
const studioNdaText = document.querySelector("#studioNdaText");
const studioDocumentRender = document.querySelector("#studioDocumentRender");
const studioFileMeta = document.querySelector("#studioFileMeta");
const studioReviewButton = document.querySelector("#studioReviewButton");
const studioExportButton = document.querySelector("#studioExportButton");
const studioClearButton = document.querySelector("#studioClearButton");
const studioClauseLane = document.querySelector("#studioClauseLane");
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
  selectedMatter: null,
  matters: [],
  activeTab: "review",
  reviewClauses: [],
  reviewOriginalParagraphs: [],
  reviewParagraphs: [],
  reviewRedlines: [],
  latestReviewResult: null,
  reviewDirty: false,
  reviewSourceText: "",
  selectedReviewClauseId: null,
  clauseJumpIndexes: {},
  exportClauseDecisions: {},
  redlineTemplateSelections: {},
  lastExport: null,
  documentViewMode: VIEW_MODE_REDLINE,
};

const repositoryController = createRepositoryController({
  state,
  fileInput,
  repositoryFileInput: document.querySelector("#repositoryFileInput"),
  gmailDemoMatterList: document.querySelector("#gmailDemoMatterList"),
  repositoryMatterPanel: document.querySelector("#repositoryMatterPanel"),
  repositoryImportStatus: document.querySelector("#repositoryImportStatus"),
  downloadBlob,
  downloadFilename,
  fileToBase64,
  loadMatterIntoReview,
  redlineDownloadFilename,
  reviewErrorFromPayload,
});

setupSourceEditors();
setActiveTab("review");
setupDocumentViewModes();

const emptyState = () => {
  renderStudioEmpty();
};

emptyState();
loadPlaybook();
repositoryController.loadMatters();

tabButtons.forEach((button) => {
  button.addEventListener("click", () => {
    setActiveTab(button.dataset.tab);
    requestAnimationFrame(resizeSourceEditors);
  });
});

fileInput.addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (isWordDocument(file)) {
    setActiveTab("repository");
    await repositoryController.importMatter(file);
    fileInput.value = "";
    return;
  }
  loadFileIntoReview(file);
});

async function loadFileIntoReview(file) {
  const extension = file.name.split(".").pop().toLowerCase();

  if (extension === "docx") {
    state.selectedDocument = file;
    state.selectedMatter = null;
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
  state.selectedMatter = null;
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
}

function isWordDocument(file) {
  return file.name.toLowerCase().endsWith(".docx");
}

function clearReview() {
  setSourceText("");
  showStudioSourceEditor();
  resizeSourceEditors();
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  fileInput.value = "";
  state.selectedDocument = null;
  state.selectedMatter = null;
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
  state.exportClauseDecisions = {};
  state.redlineTemplateSelections = {};
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
  const rerunningLoadedMatter = Boolean(state.selectedMatter?.id && !state.selectedDocument);
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
    if (!response.ok) throw reviewErrorFromPayload(payload, "Review could not run");
    const reviewedText = payload.extracted_text || text;
    if (rerunningLoadedMatter) {
      state.selectedMatter = null;
      setFileMeta("Repository text reviewed as a fresh draft");
    }
    if (payload.extracted_text) {
      setSourceText(payload.extracted_text);
      resizeSourceEditors();
      setSourcePlaceholder(SOURCE_PLACEHOLDER);
      setFileMeta(`${payload.source.filename} reviewed from Word document`);
    }
    renderResult(payload, reviewedText);
  } catch (error) {
    renderOperationError(error, "Review could not run.");
  } finally {
    button.disabled = false;
    button.textContent = "Review NDA";
  }
}

async function exportReviewDocx() {
  const text = studioNdaText.value.trim() || state.reviewSourceText.trim();
  if (!text) return;

  studioExportButton.disabled = true;
  studioExportButton.textContent = "Choosing file";

  try {
    const saveHandle = await chooseExportSaveHandle(suggestedExportFilename());
    if (saveHandle === null) {
      studioFileMeta.textContent = "Export cancelled";
      return;
    }

    studioExportButton.textContent = "Exporting";
    const payload = {
      text,
      reviewed_text: text,
      title: studioDocTitle.textContent || DEFAULT_DOCUMENT_TITLE,
      export_redline_edits: effectiveReviewRedlines(),
      manual_redline_edits: manualExportRedlines(),
    };
    if (state.selectedMatter?.id) {
      payload.matter_id = state.selectedMatter.id;
    } else if (state.selectedDocument) {
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
      throw reviewErrorFromPayload(payload, "Export could not run");
    }
    const filename = downloadFilename(response) || "nda-review-report.docx";
    const savedPath = response.headers.get("X-Export-Path");
    const savedUrl = response.headers.get("X-Export-URL");
    const exportVerified = response.headers.get("X-Export-Verified");
    if (saveHandle) {
      const blob = await response.blob();
      await writeBlobToSaveHandle(saveHandle, blob);
      renderExportSuccess(filename, savedPath, savedUrl, exportVerified, "saved");
    } else if (savedUrl) {
      renderExportSuccess(filename, savedPath, savedUrl, exportVerified);
      downloadUrl(savedUrl, filename);
    } else {
      const blob = await response.blob();
      downloadBlob(blob, filename);
      renderExportSuccess(filename, savedPath, savedUrl, exportVerified, "downloading");
    }
  } catch (error) {
    renderOperationError(error, "Export could not run.");
  } finally {
    studioExportButton.textContent = "Export DOCX";
    updateExportButtonState();
  }
}

function reviewErrorFromPayload(payload, fallbackMessage) {
  const error = new Error(payload?.error || fallbackMessage);
  if (Array.isArray(payload?.details)) {
    error.details = payload.details.filter(Boolean).map((item) => String(item));
  }
  return error;
}

function renderOperationError(error, fallbackMeta) {
  studioOverallTitle.textContent = error.message || fallbackMeta;
  studioResultMark.textContent = "!";
  studioResultMark.className = "check";
  const details = Array.isArray(error.details) && error.details.length
    ? ` ${error.details.slice(0, 3).join(" ")}`
    : "";
  studioResultMeta.textContent = `${fallbackMeta}${details}`;
}

async function chooseExportSaveHandle(suggestedName, options = {}) {
  if (!shouldUseSaveFilePicker(options)) return undefined;
  try {
    return await window.showSaveFilePicker({
      suggestedName,
      types: DOCX_FILE_PICKER_TYPES,
    });
  } catch (error) {
    if (error?.name === "AbortError") return null;
    console.warn("Save picker unavailable; falling back to browser download.", error);
    return undefined;
  }
}

function shouldUseSaveFilePicker({ allowAutomation = false } = {}) {
  return (
    typeof window.showSaveFilePicker === "function"
    && window.isSecureContext
    && (!navigator.webdriver || allowAutomation)
  );
}

async function writeBlobToSaveHandle(fileHandle, blob) {
  const writable = await fileHandle.createWritable();
  try {
    await writable.write(blob);
  } finally {
    await writable.close();
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
  downloadUrl(url, filename);
  window.setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_URL_REVOKE_DELAY_MS);
}

function downloadUrl(url, filename) {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function renderExportSuccess(filename, savedPath, savedUrl, verification, fallbackVerb = "exported") {
  state.lastExport = { filename, savedPath, savedUrl, verification };
  studioFileMeta.textContent = "";
  const summary = document.createElement("span");
  summary.className = "export-success";
  const verificationText = verification ? " · Word package verified · Track Changes enabled" : "";
  summary.textContent = `${savedUrl ? `Saved export: ${savedUrl}` : `${filename} ${fallbackVerb}`}${verificationText}`;
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

function suggestedExportFilename() {
  if (state.selectedMatter?.source_filename) return redlineDownloadFilename(state.selectedMatter.source_filename);
  if (state.selectedDocument?.name) return redlineDownloadFilename(state.selectedDocument.name);
  return "nda-review-report.docx";
}

function manualExportRedlines() {
  const originalById = new Map(state.reviewOriginalParagraphs.map((paragraph) => [paragraph.id, paragraph]));
  return state.reviewParagraphs
    .map((paragraph) => {
      const original = originalById.get(paragraph.id);
      if (!original) return null;
      const originalText = String(original.text || "").trim();
      const replacementText = String(paragraph.text || "").trim();
      if (originalText === replacementText) return null;
      const isDelete = !replacementText;
      return {
        id: `manual-${paragraph.id}`,
        clause_id: "manual_viewer_edit",
        status: "proposed",
        action: isDelete ? REDLINE_DELETE_PARAGRAPH : REDLINE_REPLACE_PARAGRAPH,
        action_label: isDelete ? "Remove paragraph" : "Replace paragraph",
        paragraph_id: paragraph.id,
        paragraph_index: paragraph.index,
        source_index: paragraph.source_index || paragraph.index,
        original_text: originalText,
        replacement_text: replacementText,
      };
    })
    .filter(Boolean);
}

function redlineDownloadFilename(filename) {
  const basename = filename.split(/[\\/]/).pop() || "";
  const stem = basename.replace(/\.[^.]*$/, "");
  const safeName = Array.from(stem)
    .map((character) => (/[a-z0-9_-]/i.test(character) ? character : "-"))
    .join("")
    .replace(/^[-_]+/g, "")
    .replace(/[-_]+$/g, "");
  return `${safeName || "nda"}-redlined.docx`;
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
  state.latestReviewResult = result;
  state.reviewClauses = result.clauses || [];
  state.reviewParagraphs = result.paragraphs || [];
  state.reviewOriginalParagraphs = state.reviewParagraphs.map((paragraph) => ({
    id: paragraph.id,
    text: String(paragraph.text || ""),
  }));
  state.reviewRedlines = result.redline_edits || [];
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  state.reviewDirty = false;
  state.reviewSourceText = reviewedText || studioNdaText.value.trim();
  state.clauseJumpIndexes = {};
  state.selectedReviewClauseId =
    state.reviewClauses.find((clause) => !clausePasses(clause))?.id || state.reviewClauses[0]?.id || null;
  renderStudioResult(result);
  updateExportButtonState();
}

function renderStudioEmpty() {
  state.latestReviewResult = null;
  showStudioSourceEditor();
  studioClauseLane?.classList.add("awaiting-review");
  studioMatchSummary.textContent = `0/${getClauseTotal()}`;
  studioResultMark.textContent = "-";
  studioResultMark.className = "";
  studioOverallTitle.textContent = "Awaiting review";
  studioResultMeta.textContent = "No hard-clause review has run yet.";
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
  const clauses = result.clauses || [];
  renderStudioSummary(clauses);
  renderStudioClauseLane();
  renderStudioDetail();
  renderStudioDocumentHighlights();
}

function renderStudioSummary(clauses) {
  studioClauseLane?.classList.remove("awaiting-review");
  const passedCount = clauses.filter((clause) => clauseStatus(clause).passes).length;
  const failedCount = clauses.filter((clause) => clauseStatus(clause).needsReview).length;
  studioMatchSummary.textContent = `${passedCount}/${getClauseTotal(clauses)}`;
  studioResultMark.textContent = failedCount ? "CHECK" : "PASS";
  studioResultMark.className = failedCount ? "check" : "pass";
  studioOverallTitle.textContent = failedCount ? "Does not meet requirements" : "Meets requirements";
  const warning = reviewWarningSummary();
  studioResultMeta.textContent = warning || (failedCount
    ? `${failedCount} hard ${failedCount === 1 ? "clause needs" : "clauses need"} checking.`
    : "All hard clauses are currently satisfied.");
}

function reviewWarningSummary() {
  const trust = state.latestReviewResult?.evidence_trust;
  if (trust?.status !== "flagged") return "";
  const firstError = Array.isArray(trust.errors) && trust.errors.length ? ` ${trust.errors[0]}` : "";
  return `Evidence provenance warning.${firstError}`;
}

function renderClauseExportState(clause, canDecide, included) {
  if (!canDecide) return "";
  return `<span class="studio-export-state ${included ? "included" : "ignored"}">${included ? "Included in export" : "Ignored in export"}</span>`;
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
  state.exportClauseDecisions[clauseId] = included;
  renderStudioResult({ clauses: state.reviewClauses });
  updateExportButtonState();
}

function setRedlineTemplateSelection(editId, optionId) {
  state.redlineTemplateSelections[editId] = optionId;
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
      const finding = hasReviewResults()
        ? `<span class="studio-clause-finding">${escapeHtml(clause.reason || clause.finding || "Clause review available.")}</span>`
        : "";
      const pill = hasReviewResults()
        ? `<strong class="studio-issue-pill ${status.tone}">${status.pillLabel}</strong>`
        : "";
      const selectable = hasReviewResults()
        ? `
          <button class="studio-clause-select" type="button" data-studio-lane-id="${escapeHtml(clause.id)}" data-studio-clause-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}">
            <span class="studio-clause-dot ${status.dotTone}"></span>
            <strong class="studio-clause-number">${index + 1}</strong>
            <span class="studio-clause-title">${escapeHtml(clause.name)}</span>
            ${pill}
            ${finding}
            ${exportState}
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
        <article class="studio-clause-item ${selected} ${status.tone} ${canDecide ? "decidable" : ""}" data-lane-card-id="${escapeHtml(clause.id)}">
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
  markSourceEdited("Edited in viewer", { preserveSourceDocument: true });
  studioResultMeta.textContent = "Document edited. Run Review NDA again to refresh the checklist.";
  updateExportButtonState();
}

function updateManualRedlinePreview(editable, paragraph) {
  const container = editable.closest(".studio-doc-paragraph");
  if (!container) return;
  const manualRedline = manualParagraphRedline(paragraph, state.reviewOriginalParagraphs);
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

function syncReviewSourceFromParagraphs() {
  const text = state.reviewParagraphs
    .map((paragraph) => String(paragraph.text || "").trim())
    .filter(Boolean)
    .join("\n\n");
  state.reviewSourceText = text;
  setSourceText(text);
}

function markSourceEdited(message, { preserveSourceDocument = false } = {}) {
  if (state.reviewClauses.length || state.reviewSourceText.trim()) {
    state.reviewDirty = true;
  }
  if (state.selectedDocument && !preserveSourceDocument) {
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

function loadMatterIntoReview(matter) {
  const reviewResult = matter.review_result || {};
  state.selectedMatter = matter;
  state.selectedDocument = null;
  fileInput.value = "";
  setSourceText(matter.extracted_text || reviewResult.extracted_text || "");
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  setDocumentTitle(matter.document_title || matter.source_filename || DEFAULT_DOCUMENT_TITLE);
  setFileMeta(`${RepositoryView.sourceTypeLabel(matter.source_type)} matter loaded`);
  renderResult(reviewResult, matter.extracted_text || reviewResult.extracted_text || "");
  setActiveTab("review");
  requestAnimationFrame(resizeSourceEditors);
}

function setActiveTab(tabName) {
  state.activeTab = tabName;
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
