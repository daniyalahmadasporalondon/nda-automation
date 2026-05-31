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

const DEFAULT_DOCUMENT_TITLE = "Untitled NDA";
const SOURCE_PLACEHOLDER = "Paste NDA text here";

const sourceInputs = [studioNdaText];
const fileMetaDisplays = [studioFileMeta];
const documentTitleDisplays = [studioDocTitle];

const state = {
  playbookClauses: [],
  selectedClauseId: null,
  selectedDocument: null,
  reviewClauses: [],
  reviewParagraphs: [],
  reviewRedlines: [],
  reviewDirty: false,
  reviewSourceText: "",
  selectedReviewClauseId: null,
  documentViewMode: "redline",
};

setupSourceEditors();
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
  state.reviewParagraphs = [];
  state.reviewRedlines = [];
  state.reviewDirty = false;
  state.reviewSourceText = "";
  state.selectedReviewClauseId = null;
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
  const text = state.reviewSourceText.trim();
  if (!text) return;
  if (state.reviewDirty) {
    studioOverallTitle.textContent = "Review needed";
    studioResultMark.textContent = "!";
    studioResultMark.className = "check";
    studioResultMeta.textContent = "Run Review NDA again before exporting.";
    updateExportButtonState();
    return;
  }

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
    downloadBlob(blob, downloadFilename(response) || "nda-review-report.docx");
    setFileMeta("Review report exported as Word document");
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
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    const chunk = bytes.subarray(index, index + chunkSize);
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
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function downloadFilename(response) {
  const contentDisposition = response.headers.get("Content-Disposition") || "";
  const match = contentDisposition.match(/filename="?([^";]+)"?/i);
  return match ? match[1] : "";
}

function setSourceText(text) {
  sourceInputs.forEach((input) => {
    input.value = text;
  });
}

function setSourcePlaceholder(placeholder) {
  sourceInputs.forEach((input) => {
    input.placeholder = placeholder;
  });
}

function setFileMeta(message) {
  fileMetaDisplays.forEach((display) => {
    display.textContent = message;
  });
}

function setDocumentTitle(title) {
  documentTitleDisplays.forEach((display) => {
    display.textContent = title;
  });
}

function setupSourceEditors() {
  sourceInputs.forEach((input) => {
    input.addEventListener("input", () => {
      resizeSourceEditor(input);
      if (input.value.trim()) {
        markSourceEdited("Text edited");
      }
    });
    resizeSourceEditor(input);
  });
}

function resizeSourceEditors() {
  sourceInputs.forEach(resizeSourceEditor);
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
  state.reviewRedlines = result.redline_edits || [];
  state.reviewDirty = false;
  state.reviewSourceText = reviewedText || studioNdaText.value.trim();
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
  studioExportButton.disabled = !state.reviewClauses.length || !state.reviewSourceText.trim() || state.reviewDirty;
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
  const passedCount = clauses.filter(clausePasses).length;
  const failedCount = clauses.filter(clauseNeedsReview).length;
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
      const statusTone = clauseTone(clause);
      const statusText = clauseStatusLabel(clause);
      return `
        <button class="studio-issue-card ${selected} ${statusTone}" type="button" data-studio-clause-id="${escapeHtml(clause.id)}">
          <span class="studio-issue-card-top">
            <span class="studio-issue-title">${escapeHtml(clause.name)}</span>
            <strong class="studio-issue-pill ${statusTone}">${statusText}</strong>
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

function clausePasses(clause) {
  if (!clause) return false;
  if (typeof clause.passes === "boolean") return clause.passes;
  return clause.status === "pass" || clause.status === "match";
}

function clauseNeedsReview(clause) {
  return !clausePasses(clause) && clause.status !== "idle";
}

function clauseTone(clause) {
  if (clause.status === "idle") return "pending";
  return clausePasses(clause) ? "pass" : "check";
}

function clauseDotTone(clause) {
  if (clause.status === "idle") return "pending";
  return clausePasses(clause) ? "match" : "verify";
}

function clauseStatusLabel(clause) {
  if (clause.status === "idle") return "Pending";
  return clausePasses(clause) ? "PASS" : "CHECK";
}

function clauseResultLabel(clause) {
  if (clause.status === "not_present") return "Not present";
  if (clause.status === "match") return "Match";
  if (clause.status === "check") return "Check";
  if (clause.status === "pass") return "Match";
  if (clause.status === "fail") return "Check";
  return "Pending";
}

function clauseIssueLabel(clause) {
  if (clause.status === "idle") return "Pending";
  return clause.issue_label || "Needs review";
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
      const statusClass = clauseDotTone(clause);
      const tag = hasReviewResults() ? "button" : "div";
      const type = hasReviewResults() ? ' type="button"' : "";
      const data = hasReviewResults() ? ` data-studio-lane-id="${escapeHtml(clause.id)}"` : "";
      return `
        <${tag} class="studio-clause-item ${selected}"${type}${data}>
          <span class="studio-clause-dot ${statusClass}"></span>
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
  const whyText = clause.reason || clause.finding || "Clause review available.";
  const excerpt = clause.matched_text
    ? `<div class="studio-detail-block studio-detail-evidence"><small>Exact paragraph</small><p>${escapeHtml(clause.matched_text)}</p></div>`
    : '<div class="studio-detail-block studio-detail-evidence muted"><small>Exact paragraph</small><p>No matching paragraph identified.</p></div>';
  const fixBlock = clauseNeedsReview(clause) && clause.what_to_fix
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
      <span class="status ${clauseTone(clause)}">${escapeHtml(clauseStatusLabel(clause))}</span>
    </div>
    <div class="studio-detail-stack">
      <div class="studio-detail-block requirement-block">
        <small>Requirement</small>
        <p>${escapeHtml(clause.requirement)}</p>
      </div>
      ${excerpt}
      <div class="studio-detail-block issue-block ${escapeHtml(clauseTone(clause))}">
        <small>Issue type</small>
        <p>${escapeHtml(clauseIssueLabel(clause))}</p>
      </div>
      <div class="studio-detail-block finding-block">
        <small>Why</small>
        <p>${escapeHtml(whyText)}</p>
      </div>
      ${fixBlock}
      ${redlineBlock}
      <div class="studio-detail-block">
        <small>Backend result</small>
        <p>${escapeHtml(clauseResultLabel(clause))}</p>
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
  const clausesByParagraphId = new Map();
  state.reviewClauses.forEach((clause) => {
    (clause.matched_paragraph_ids || []).forEach((paragraphId) => {
      if (!clausesByParagraphId.has(paragraphId)) clausesByParagraphId.set(paragraphId, []);
      clausesByParagraphId.get(paragraphId).push(clause);
    });
  });
  const redlinesByParagraphId = new Map();
  state.reviewRedlines.forEach((edit) => {
    if (!redlinesByParagraphId.has(edit.paragraph_id)) redlinesByParagraphId.set(edit.paragraph_id, []);
    redlinesByParagraphId.get(edit.paragraph_id).push(edit);
  });

  const viewMode = state.documentViewMode || "redline";
  studioDocumentRender.classList.toggle("doc-mode-clean", viewMode === "clean");
  studioDocumentRender.classList.toggle("doc-mode-sidebyside", viewMode === "sidebyside");

  studioDocumentRender.innerHTML = state.reviewParagraphs
    .map((paragraph) => {
      const redlines = redlinesByParagraphId.get(paragraph.id) || [];
      const redlineClauses = redlines
        .map((edit) => state.reviewClauses.find((clause) => clause.id === edit.clause_id))
        .filter(Boolean);
      const linked = mergeClauses(clausesByParagraphId.get(paragraph.id) || [], redlineClauses);
      const selected = linked.find((clause) => clause.id === state.selectedReviewClauseId);
      const ids = linked.map((clause) => clause.id).join(" ");

      if (viewMode === "clean") {
        return renderCleanParagraph(paragraph, redlines, { ids, selected: Boolean(selected) });
      }
      if (viewMode === "sidebyside") {
        return renderSideBySideParagraph(paragraph, redlines, { ids, selected: Boolean(selected) });
      }

      const primary = selected || linked.find((clause) => !clausePasses(clause)) || linked[0];
      const selectedRedline = redlines.find((edit) => edit.clause_id === state.selectedReviewClauseId);
      const primaryRedline = selectedRedline || redlines[0];
      const visibleRedlines = redlines.every(isInsertionRedline)
        ? (selectedRedline ? [selectedRedline] : redlines)
        : (primaryRedline ? [primaryRedline] : []);
      const classes = [
        "studio-doc-paragraph",
        linked.length ? "has-clause" : "",
        redlines.length ? "has-redline" : "",
        primaryRedline?.action === "delete_paragraph" ? "redline-delete" : "",
        primaryRedline?.action === "insert_after_paragraph" ? "redline-insert" : "",
        primary && !clausePasses(primary) ? "verify" : "",
        primary && clausePasses(primary) ? "match" : "",
        selected ? "selected" : "",
      ]
        .filter(Boolean)
        .join(" ");
      const paragraphHtml = renderRedlineParagraphBody(paragraph, primaryRedline, visibleRedlines);

      return `<div class="${classes}" data-paragraph-id="${escapeHtml(paragraph.id)}" data-clause-ids="${escapeHtml(ids)}">${paragraphHtml}</div>`;
    })
    .join("");

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
      const mode = button.dataset.viewMode;
      if (state.documentViewMode === mode) return;
      state.documentViewMode = mode;
      buttons.forEach((other) => other.classList.toggle("active", other === button));
      if (state.reviewParagraphs.length && !studioDocumentRender.hidden) {
        renderStudioDocumentHighlights();
      }
    });
  });
}

function paragraphRedlinePlan(paragraph, redlines) {
  const replace = redlines.find((edit) => edit.action === "replace_paragraph");
  const remove = redlines.find((edit) => edit.action === "delete_paragraph");
  const inserts = redlines.filter((edit) => edit.action === "insert_after_paragraph");
  const cleanText = remove
    ? ""
    : replace
      ? String(replace.replacement_text || "")
      : String(paragraph.text || "");
  return { replace, remove, inserts, cleanText };
}

function renderCleanParagraph(paragraph, redlines, context) {
  const plan = paragraphRedlinePlan(paragraph, redlines);
  let html = "";
  if (!plan.remove) {
    const classes = ["studio-doc-paragraph", "doc-clean-paragraph", context.selected ? "selected" : ""]
      .filter(Boolean)
      .join(" ");
    html += `<div class="${classes}" data-paragraph-id="${escapeHtml(paragraph.id)}" data-clause-ids="${escapeHtml(context.ids)}">${escapeHtml(plan.cleanText)}</div>`;
  }
  plan.inserts.forEach((edit) => {
    const inserted = escapeHtml(String(edit.insert_text || edit.replacement_text || ""));
    html += `<div class="studio-doc-paragraph doc-clean-paragraph">${inserted}</div>`;
  });
  return html;
}

function renderSideBySideParagraph(paragraph, redlines, context) {
  const plan = paragraphRedlinePlan(paragraph, redlines);
  const original = escapeHtml(String(paragraph.text || ""));
  const latest = plan.remove
    ? `<span class="sxs-removed">${original}</span>`
    : escapeHtml(plan.cleanText) || `<span class="sxs-empty">—</span>`;
  const classes = ["studio-doc-paragraph", "doc-sxs-paragraph", context.selected ? "selected" : ""]
    .filter(Boolean)
    .join(" ");
  let html = `<div class="${classes}" data-paragraph-id="${escapeHtml(paragraph.id)}" data-clause-ids="${escapeHtml(context.ids)}"><div class="clause-sxs"><div class="clause-sxs-col"><span class="clause-sxs-tag">Original</span><div>${original}</div></div><div class="clause-sxs-col latest"><span class="clause-sxs-tag">Latest</span><div>${latest}</div></div></div></div>`;
  plan.inserts.forEach((edit) => {
    const inserted = escapeHtml(String(edit.insert_text || edit.replacement_text || ""));
    html += `<div class="studio-doc-paragraph doc-sxs-paragraph"><div class="clause-sxs"><div class="clause-sxs-col"><span class="clause-sxs-tag">Original</span><div class="sxs-empty">—</div></div><div class="clause-sxs-col latest"><span class="clause-sxs-tag">Latest</span><div class="sxs-inserted">${inserted}</div></div></div></div>`;
  });
  return html;
}

function renderRedlineParagraphBody(paragraph, primaryRedline, visibleRedlines) {
  if (primaryRedline?.action === "replace_paragraph") {
    return `<div class="paragraph-diff" contenteditable="false">${renderWordDiff(paragraph.text, primaryRedline.replacement_text || "")}</div>`;
  }
  if (primaryRedline?.action === "delete_paragraph") {
    return `<div class="paragraph-diff paragraph-diff-removed" contenteditable="false"><span class="diff-del">${escapeHtml(String(paragraph.text || ""))}</span></div><div class="paragraph-redline-note" contenteditable="false"><span class="redline-label">${escapeHtml(redlineActionLabel(primaryRedline))}</span></div>`;
  }
  const redlineHtml = visibleRedlines.length ? renderParagraphRedlines(paragraph, visibleRedlines) : "";
  return `
        <div
          class="paragraph-editable"
          contenteditable="plaintext-only"
          spellcheck="true"
          role="textbox"
          aria-multiline="true"
          data-editable-paragraph-id="${escapeHtml(paragraph.id)}"
          aria-label="Edit paragraph ${escapeHtml(paragraph.index || "")}"
        >${escapeHtml(String(paragraph.text || ""))}</div>
        ${redlineHtml}
      `;
}

function tokenizeForDiff(text) {
  return String(text || "").match(/\s+|\S+/g) || [];
}

function renderWordDiff(oldText, newText) {
  const a = tokenizeForDiff(oldText);
  const b = tokenizeForDiff(newText);
  const m = a.length;
  const n = b.length;
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i -= 1) {
    for (let j = n - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const del = (token) => (token.trim() ? `<span class="diff-del">${escapeHtml(token)}</span>` : escapeHtml(token));
  const ins = (token) => (token.trim() ? `<span class="diff-ins">${escapeHtml(token)}</span>` : escapeHtml(token));
  let i = 0;
  let j = 0;
  let html = "";
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      html += escapeHtml(a[i]);
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      html += del(a[i]);
      i += 1;
    } else {
      html += ins(b[j]);
      j += 1;
    }
  }
  while (i < m) {
    html += del(a[i]);
    i += 1;
  }
  while (j < n) {
    html += ins(b[j]);
    j += 1;
  }
  return html;
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
  markSourceEdited("Edited in viewer");
  studioResultMeta.textContent = "Document edited. Run Review NDA again to refresh the checklist.";
  updateExportButtonState();
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
  document.execCommand("insertText", false, text);
}

function mergeClauses(primaryClauses, secondaryClauses) {
  const merged = [...primaryClauses];
  secondaryClauses.forEach((clause) => {
    if (!merged.find((item) => item.id === clause.id)) merged.push(clause);
  });
  return merged;
}

function renderParagraphRedlines(paragraph, edits) {
  if (edits.every(isInsertionRedline)) {
    return edits.map(renderParagraphInsertion).join("");
  }
  return renderParagraphRedline(paragraph, edits[0]);
}

function renderParagraphRedline(paragraph, edit) {
  if (isInsertionRedline(edit)) {
    return renderParagraphInsertion(edit);
  }

  return `
    <div class="paragraph-redline-note" contenteditable="false">
      <span class="redline-label">${escapeHtml(redlineActionLabel(edit))}</span>
      ${renderRedlineReplacement(edit, "span")}
    </div>
  `;
}

function renderParagraphInsertion(edit) {
  return `
    <div class="paragraph-insertion" contenteditable="false">
      <span class="redline-label">${escapeHtml(redlineActionLabel(edit))}</span>
      <span class="redline-insertion">${escapeHtml(edit.insert_text || edit.replacement_text || "")}</span>
    </div>
  `;
}

function redlineActionLabel(edit) {
  if (edit.action === "delete_paragraph") return edit.action_label || "Remove paragraph";
  if (edit.action === "insert_after_paragraph") return edit.action_label || "Insert after paragraph";
  if (edit.action === "replace_paragraph") return edit.action_label || "Replace paragraph";
  return edit.action_label || "Proposed edit";
}

function renderRedlineReplacement(edit, tagName) {
  if (edit.action === "delete_paragraph") {
    return `<${tagName} class="redline-removal">Remove this paragraph.</${tagName}>`;
  }
  if (edit.action === "insert_after_paragraph") {
    return `<${tagName} class="redline-insertion">${escapeHtml(edit.insert_text || edit.replacement_text || "")}</${tagName}>`;
  }
  return `<${tagName} class="redline-replacement">${escapeHtml(edit.replacement_text || "")}</${tagName}>`;
}

function isInsertionRedline(edit) {
  return edit?.action === "insert_after_paragraph";
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
  const fontSize = parseFloat(style.fontSize) || 16;
  const lineHeight = parseFloat(style.lineHeight) || fontSize * 1.7;
  const paddingX = (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
  const availableWidth = Math.max(input.clientWidth - paddingX, 80);
  const charsPerLine = Math.max(24, Math.floor(availableWidth / (fontSize * 0.55)));
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
    top: Math.max(0, targetTop - container.clientHeight * 0.32),
  });
}

function scrollRenderedClauseToView(clauseId) {
  const container = studioDocumentRender.closest(".studio-page-wrap");
  if (!container) return;

  const clause = state.reviewClauses.find((item) => item.id === clauseId);
  const redlineParagraphIds = state.reviewRedlines
    .filter((edit) => edit.clause_id === clauseId)
    .map((edit) => edit.paragraph_id);
  const paragraphIds = [...(clause?.matched_paragraph_ids || []), ...redlineParagraphIds];
  const target = Array.from(studioDocumentRender.querySelectorAll("[data-paragraph-id]"))
    .find((paragraph) => paragraphIds.includes(paragraph.dataset.paragraphId));
  if (!target) return;

  const targetTop = layoutOffsetTop(target) - layoutOffsetTop(container);
  container.scrollTo({
    behavior: "smooth",
    top: Math.max(0, targetTop - container.clientHeight * 0.24),
  });

  target.classList.remove("paragraph-pulse");
  void target.offsetWidth;
  target.classList.add("paragraph-pulse");
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
    button.classList.toggle("active", button.dataset.tab === tabName);
  });
  views.forEach((view) => {
    view.classList.toggle("active", view.dataset.view === tabName);
  });
}

function renderPlaybookList() {
  playbookList.innerHTML = state.playbookClauses
    .map((clause, index) => {
      const selected = clause.id === state.selectedClauseId ? "selected" : "";
      const position = String(index + 1).padStart(2, "0");
      return `
        <button class="playbook-row ${selected}" type="button" data-clause-id="${escapeHtml(clause.id)}">
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

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
