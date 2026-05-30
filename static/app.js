const ndaText = document.querySelector("#ndaText");
const reviewButton = document.querySelector("#reviewButton");
const clearButton = document.querySelector("#clearButton");
const fileInput = document.querySelector("#fileInput");
const fileMeta = document.querySelector("#fileMeta");
const docTitle = document.querySelector("#docTitle");
const workspaceDocTitle = document.querySelector("#workspaceDocTitle");
const studioDocTitle = document.querySelector("#studioDocTitle");
const studioNdaText = document.querySelector("#studioNdaText");
const studioDocumentRender = document.querySelector("#studioDocumentRender");
const studioFileMeta = document.querySelector("#studioFileMeta");
const studioReviewButton = document.querySelector("#studioReviewButton");
const studioClearButton = document.querySelector("#studioClearButton");
const studioClauseLane = document.querySelector("#studioClauseLane");
const studioIssueList = document.querySelector("#studioIssueList");
const studioDetailPanel = document.querySelector("#studioDetailPanel");
const studioMatchSummary = document.querySelector("#studioMatchSummary");
const studioOverallTitle = document.querySelector("#studioOverallTitle");
const overallTitle = document.querySelector("#overallTitle");
const resultHero = document.querySelector("#resultHero");
const resultMark = document.querySelector("#resultMark");
const resultMeta = document.querySelector("#resultMeta");
const clauseGrid = document.querySelector("#clauseGrid");
const clauseLane = document.querySelector("#clauseLane");
const reviewDetail = document.querySelector("#reviewDetail");
const tabButtons = document.querySelectorAll("[data-tab]");
const views = document.querySelectorAll("[data-view]");
const interfaceScaleButtons = document.querySelectorAll(".interface-scale [data-interface-scale]");
const playbookList = document.querySelector("#playbookList");
const clauseDetail = document.querySelector("#clauseDetail");

const DEFAULT_INTERFACE_SCALE = "90";
const INTERFACE_SCALE_STORAGE_KEY = "ndaAutomation.interfaceScale";
const INTERFACE_SCALES = new Set(["85", "90", "100"]);

let playbookClauses = [];
let selectedClauseId = null;
let selectedDocument = null;
let reviewClauses = [];
let reviewParagraphs = [];
let selectedReviewClauseId = null;

setupInterfaceScale();
setupSourceEditors();

const emptyState = () => {
  clauseGrid.innerHTML = '<div class="empty">No review yet</div>';
  resultMeta.textContent = "No hard-clause review has run yet.";
  reviewDetail.innerHTML = `
    <div class="review-detail-empty">
      <p class="eyebrow">clause detail</p>
      <h2>No review yet</h2>
    </div>
  `;
  renderClauseLane();
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
    selectedDocument = file;
    ndaText.value = "";
    studioNdaText.value = "";
    showStudioSourceEditor();
    resizeSourceEditors();
    ndaText.placeholder = "Word document selected";
    studioNdaText.placeholder = "Word document selected";
    fileMeta.textContent = `${file.name} ready for review`;
    studioFileMeta.textContent = `${file.name} ready for review`;
    docTitle.textContent = file.name;
    workspaceDocTitle.textContent = file.name;
    studioDocTitle.textContent = file.name;
    setActiveTab("reviewStudio");
    return;
  }

  selectedDocument = null;
  const fileText = await file.text();
  ndaText.value = fileText;
  studioNdaText.value = fileText;
  showStudioSourceEditor();
  resizeSourceEditors();
  ndaText.placeholder = "Paste NDA text here";
  studioNdaText.placeholder = "Paste NDA text here";
  fileMeta.textContent = `${file.name} loaded as text`;
  studioFileMeta.textContent = `${file.name} loaded as text`;
  docTitle.textContent = file.name;
  workspaceDocTitle.textContent = file.name;
  studioDocTitle.textContent = file.name;
  setActiveTab("reviewStudio");
});

function clearReview() {
  ndaText.value = "";
  studioNdaText.value = "";
  showStudioSourceEditor();
  resizeSourceEditors();
  ndaText.placeholder = "Paste NDA text here";
  studioNdaText.placeholder = "Paste NDA text here";
  fileInput.value = "";
  selectedDocument = null;
  fileMeta.textContent = "No file selected";
  studioFileMeta.textContent = "No file selected";
  docTitle.textContent = "Untitled NDA";
  workspaceDocTitle.textContent = "Untitled NDA";
  studioDocTitle.textContent = "Untitled NDA";
  overallTitle.textContent = "Awaiting review";
  resultMark.textContent = "-";
  resultHero.className = "result-hero";
  reviewClauses = [];
  reviewParagraphs = [];
  selectedReviewClauseId = null;
  emptyState();
}

clearButton.addEventListener("click", () => {
  clearReview();
});

studioClearButton.addEventListener("click", () => {
  clearReview();
});

reviewButton.addEventListener("click", async () => {
  await runReview(ndaText, reviewButton);
});

studioReviewButton.addEventListener("click", async () => {
  await runReview(studioNdaText, studioReviewButton);
});

async function runReview(sourceInput, button) {
  const text = sourceInput.value.trim();
  if (!text && !selectedDocument) {
    overallTitle.textContent = "Add NDA text";
    studioOverallTitle.textContent = "Add NDA text";
    resultMark.textContent = "-";
    resultHero.className = "result-hero fail";
    emptyState();
    return;
  }

  button.disabled = true;
  button.textContent = "Reviewing";

  try {
    const response = selectedDocument
      ? await reviewDocument(selectedDocument)
      : await fetch("/api/review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Review could not run");
    if (payload.extracted_text) {
      ndaText.value = payload.extracted_text;
      studioNdaText.value = payload.extracted_text;
      resizeSourceEditors();
      ndaText.placeholder = "Paste NDA text here";
      studioNdaText.placeholder = "Paste NDA text here";
      fileMeta.textContent = `${payload.source.filename} reviewed from Word document`;
      studioFileMeta.textContent = `${payload.source.filename} reviewed from Word document`;
    }
    renderResult(payload);
  } catch (error) {
    overallTitle.textContent = error.message;
    studioOverallTitle.textContent = error.message;
    resultMark.textContent = "!";
    resultHero.className = "result-hero fail";
  } finally {
    button.disabled = false;
    button.textContent = "Review NDA";
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

function setupInterfaceScale() {
  applyInterfaceScale(getSavedInterfaceScale());

  interfaceScaleButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const scale = normalizeInterfaceScale(button.dataset.interfaceScale);
      applyInterfaceScale(scale);
      saveInterfaceScale(scale);
    });
  });
}

function getSavedInterfaceScale() {
  try {
    return normalizeInterfaceScale(window.localStorage.getItem(INTERFACE_SCALE_STORAGE_KEY));
  } catch {
    return DEFAULT_INTERFACE_SCALE;
  }
}

function saveInterfaceScale(scale) {
  try {
    window.localStorage.setItem(INTERFACE_SCALE_STORAGE_KEY, scale);
  } catch {
    // Local storage can be unavailable in restricted browser modes.
  }
}

function normalizeInterfaceScale(scale) {
  return INTERFACE_SCALES.has(scale) ? scale : DEFAULT_INTERFACE_SCALE;
}

function applyInterfaceScale(scale) {
  const normalizedScale = normalizeInterfaceScale(scale);
  document.body.dataset.interfaceScale = normalizedScale;
  interfaceScaleButtons.forEach((button) => {
    const isActive = button.dataset.interfaceScale === normalizedScale;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", String(isActive));
  });
  requestAnimationFrame(resizeSourceEditors);
}

function setupSourceEditors() {
  [ndaText, studioNdaText].forEach((input) => {
    input.addEventListener("input", () => {
      resizeSourceEditor(input);
    });
    resizeSourceEditor(input);
  });
}

function resizeSourceEditors() {
  [ndaText, studioNdaText].forEach(resizeSourceEditor);
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

function renderResult(result) {
  const passed = result.overall_status === "meets_requirements";
  const checks = (result.clauses || []).filter((clause) => !clausePasses(clause)).length;
  overallTitle.textContent = passed ? "Meets requirements" : "Does not meet requirements";
  resultMark.textContent = passed ? "PASS" : "CHECK";
  resultMeta.textContent = passed
    ? "All hard clauses are currently satisfied."
    : `${checks} hard ${checks === 1 ? "clause needs" : "clauses need"} checking.`;
  resultHero.className = `result-hero ${passed ? "pass" : "fail"}`;

  reviewClauses = result.clauses || [];
  reviewParagraphs = result.paragraphs || [];
  selectedReviewClauseId = reviewClauses.find((clause) => !clausePasses(clause))?.id || reviewClauses[0]?.id || null;
  renderClauseLane();
  renderStudioResult(result);
  renderReviewClauseList();
  renderReviewDetail();
}

function renderStudioEmpty() {
  if (!studioIssueList) return;
  showStudioSourceEditor();
  studioMatchSummary.textContent = `0/${getClauseTotal()}`;
  studioOverallTitle.textContent = "Awaiting review";
  studioIssueList.innerHTML = '<div class="studio-empty">No review yet</div>';
  studioDetailPanel.innerHTML = `
    <p class="eyebrow">playbook language</p>
    <p>No review yet.</p>
  `;
  renderStudioClauseLane();
}

function renderStudioResult(result) {
  if (!studioIssueList) return;
  const clauses = result.clauses || [];
  const passedCount = clauses.filter(clausePasses).length;
  const failedCount = clauses.filter(clauseNeedsReview).length;
  studioMatchSummary.textContent = `${passedCount}/${getClauseTotal(clauses)}`;
  studioOverallTitle.textContent = failedCount
    ? `${failedCount} ${failedCount === 1 ? "clause needs" : "clauses need"} checking`
    : "All hard clauses match";

  studioIssueList.innerHTML = clauses
    .map((clause) => {
      const selected = clause.id === selectedReviewClauseId ? "selected" : "";
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

  studioIssueList.querySelectorAll("[data-studio-clause-id]").forEach((row) => {
    row.addEventListener("click", () => {
      selectReviewClause(row.dataset.studioClauseId, { jump: true });
    });
  });

  renderStudioClauseLane();
  renderStudioDetail();
  renderStudioDocumentHighlights();
}

function getClauseTotal(clauses = []) {
  return clauses.length || playbookClauses.length || 0;
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

function renderStudioClauseLane() {
  if (!studioClauseLane) return;

  const sourceClauses = reviewClauses.length
    ? reviewClauses
    : playbookClauses.map((clause) => ({ ...clause, status: "idle" }));

  if (!sourceClauses.length) {
    studioClauseLane.innerHTML = '<div class="studio-empty">Loading clauses</div>';
    return;
  }

  studioClauseLane.innerHTML = sourceClauses
    .map((clause, index) => {
      const selected = clause.id === selectedReviewClauseId ? "selected" : "";
      const statusClass = clauseDotTone(clause);
      const tag = reviewClauses.length ? "button" : "div";
      const type = reviewClauses.length ? ' type="button"' : "";
      const data = reviewClauses.length ? ` data-studio-lane-id="${escapeHtml(clause.id)}"` : "";
      return `
        <${tag} class="studio-clause-item ${selected}"${type}${data}>
          <span class="studio-clause-dot ${statusClass}"></span>
          <strong>${index + 1}</strong>
          <span>${escapeHtml(clause.name)}</span>
        </${tag}>
      `;
    })
    .join("");

  studioClauseLane.querySelectorAll("[data-studio-lane-id]").forEach((row) => {
    row.addEventListener("click", () => {
      selectReviewClause(row.dataset.studioLaneId, { jump: true });
    });
  });
}

function renderStudioDetail() {
  const clause = reviewClauses.find((item) => item.id === selectedReviewClauseId);
  if (!clause) return;
  const excerpt = clause.matched_text
    ? `<div class="studio-detail-block"><small>Exact paragraph</small><p>${escapeHtml(clause.matched_text)}</p></div>`
    : '<div class="studio-detail-block muted"><small>Exact paragraph</small><p>No matching paragraph identified.</p></div>';
  const acceptableLanguage = clause.acceptable_language
    ? `<div class="studio-detail-block"><small>Acceptable language</small><p>${escapeHtml(clause.acceptable_language)}</p></div>`
    : "";
  studioDetailPanel.innerHTML = `
    <p class="eyebrow">clause detail</p>
    <div class="studio-detail-heading">
      <h3>${escapeHtml(clause.name)}</h3>
      <span class="status ${clauseTone(clause)}">${escapeHtml(clauseStatusLabel(clause))}</span>
    </div>
    <div class="studio-detail-block">
      <small>Requirement</small>
      <p>${escapeHtml(clause.requirement)}</p>
    </div>
    <div class="studio-detail-block finding-block">
      <small>Why</small>
      <p>${escapeHtml(clause.reason || clause.finding)}</p>
    </div>
    ${excerpt}
    ${acceptableLanguage}
  `;
}

function renderClauseLane() {
  if (!clauseLane) return;

  const sourceClauses = reviewClauses.length
    ? reviewClauses
    : playbookClauses.map((clause) => ({ ...clause, status: "idle" }));

  if (!sourceClauses.length) {
    clauseLane.innerHTML = '<div class="lane-empty">Loading clauses</div>';
    return;
  }

  clauseLane.innerHTML = sourceClauses
    .map((clause, index) => {
      const selected = clause.id === selectedReviewClauseId ? "selected" : "";
      const status = clauseTone(clause);
      const statusText = clauseStatusLabel(clause);
      const tag = reviewClauses.length ? "button" : "div";
      const type = reviewClauses.length ? ' type="button"' : "";
      const data = reviewClauses.length ? ` data-lane-clause-id="${escapeHtml(clause.id)}"` : "";
      return `
        <${tag} class="lane-item ${selected} ${status}"${type}${data}>
          <span class="lane-dot"></span>
          <span class="lane-code">CL-${String(index + 1).padStart(2, "0")}</span>
          <span class="lane-name">${escapeHtml(clause.name)}</span>
          <span class="lane-status">${statusText}</span>
        </${tag}>
      `;
    })
    .join("");

  clauseLane.querySelectorAll("[data-lane-clause-id]").forEach((item) => {
    item.addEventListener("click", () => {
      selectReviewClause(item.dataset.laneClauseId, { jump: true });
    });
  });
}

function renderReviewClauseList() {
  clauseGrid.innerHTML = reviewClauses
    .map((clause) => {
      const selected = clause.id === selectedReviewClauseId ? "selected" : "";
      const statusLabel = clauseStatusLabel(clause);
      const statusTone = clauseTone(clause);
      return `
        <article class="clause-card ${selected}" data-review-clause-id="${escapeHtml(clause.id)}" tabindex="0">
          <header>
            <div>
              <h3>${escapeHtml(clause.name)}</h3>
              <p class="requirement">${escapeHtml(clause.requirement)}</p>
            </div>
            <span class="status ${statusTone}">${statusLabel}</span>
          </header>
          <p class="finding">${escapeHtml(clause.reason || clause.finding)}</p>
        </article>
      `;
    })
    .join("");

  clauseGrid.querySelectorAll("[data-review-clause-id]").forEach((card) => {
    card.addEventListener("click", () => {
      selectReviewClause(card.dataset.reviewClauseId, { jump: true });
    });
    card.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selectReviewClause(card.dataset.reviewClauseId, { jump: true });
    });
  });
}

function renderStudioDocumentHighlights() {
  if (!studioDocumentRender) return;

  if (!reviewClauses.length) {
    showStudioSourceEditor();
    return;
  }

  if (!reviewParagraphs.length) {
    showStudioSourceEditor();
    return;
  }
  const clausesByParagraphId = new Map();
  reviewClauses.forEach((clause) => {
    (clause.matched_paragraph_ids || []).forEach((paragraphId) => {
      if (!clausesByParagraphId.has(paragraphId)) clausesByParagraphId.set(paragraphId, []);
      clausesByParagraphId.get(paragraphId).push(clause);
    });
  });

  studioDocumentRender.innerHTML = reviewParagraphs
    .map((paragraph) => {
      const linked = clausesByParagraphId.get(paragraph.id) || [];
      const selected = linked.find((clause) => clause.id === selectedReviewClauseId);
      const primary = selected || linked.find((clause) => !clausePasses(clause)) || linked[0];
      const ids = linked.map((clause) => clause.id).join(" ");
      const classes = [
        "studio-doc-paragraph",
        linked.length ? "has-clause" : "",
        primary && !clausePasses(primary) ? "verify" : "",
        primary && clausePasses(primary) ? "match" : "",
        selected ? "selected" : "",
      ]
        .filter(Boolean)
        .join(" ");

      return `<p class="${classes}" data-paragraph-id="${escapeHtml(paragraph.id)}" data-clause-ids="${escapeHtml(ids)}">${escapeHtml(paragraph.text)}</p>`;
    })
    .join("");

  studioDocumentRender.querySelectorAll("[data-clause-ids]").forEach((paragraph) => {
    paragraph.addEventListener("click", () => {
      const clauseId = paragraph.dataset.clauseIds.split(" ").filter(Boolean)[0];
      if (clauseId) selectReviewClause(clauseId, { jump: false });
    });
  });

  showStudioDocumentRender();
}

function selectReviewClause(clauseId, options = {}) {
  selectedReviewClauseId = clauseId;
  renderClauseLane();
  renderStudioResult({ clauses: reviewClauses });
  renderReviewClauseList();
  renderReviewDetail();

  if (options.jump) {
    const clause = reviewClauses.find((item) => item.id === clauseId);
    requestAnimationFrame(() => jumpToClauseSource(clause));
  }
}

function jumpToClauseSource(clause) {
  if (!clause) return;

  const sourceInput = getActiveSourceInput();

  if (sourceInput === studioNdaText && studioDocumentRender && !studioDocumentRender.hidden) {
    scrollRenderedClauseToView(clause.id);
    return;
  }

  if (!sourceInput?.value.trim() || !clause.matched_text) return;
  const range = findExactTextRange(sourceInput.value, clause.matched_text);
  if (!range) return;
  focusTextRange(sourceInput, range.start, range.end);
}

function getActiveSourceInput() {
  const activeView = document.querySelector("[data-view].active");
  return activeView?.dataset.view === "review" ? ndaText : studioNdaText;
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

  const container = input.closest(".studio-page-wrap, .document-canvas");
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

  const clause = reviewClauses.find((item) => item.id === clauseId);
  const paragraphIds = clause?.matched_paragraph_ids || [];
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
  const page = input.closest(".studio-page, .document-page");
  if (!page) return;
  page.classList.remove("source-jump");
  void page.offsetWidth;
  page.classList.add("source-jump");
}

function renderReviewDetail() {
  const clause = reviewClauses.find((item) => item.id === selectedReviewClauseId);
  if (!clause) {
    emptyState();
    return;
  }

  const statusLabel = clauseStatusLabel(clause);
  const statusTone = clauseTone(clause);
  const exactExcerpt = clause.matched_text
    ? `<p class="evidence">${escapeHtml(clause.matched_text)}</p>`
    : '<p class="review-detail-muted">No matching paragraph identified.</p>';
  const acceptableLanguage = clause.acceptable_language
    ? `<div class="review-detail-block"><small>Acceptable language</small><p>${escapeHtml(clause.acceptable_language)}</p></div>`
    : "";

  reviewDetail.innerHTML = `
    <div class="review-detail-header">
      <div>
        <p class="eyebrow">selected clause</p>
        <h2>${escapeHtml(clause.name)}</h2>
      </div>
      <span class="status ${statusTone}">${statusLabel}</span>
    </div>
    <div class="review-detail-block">
      <small>Requirement</small>
      <p>${escapeHtml(clause.requirement)}</p>
    </div>
    <div class="review-detail-block finding-block">
      <small>Why</small>
      <p>${escapeHtml(clause.reason || clause.finding)}</p>
    </div>
    <div class="review-detail-evidence">
      <small>Exact paragraph</small>
      ${exactExcerpt}
    </div>
    <div class="review-detail-block">
      <small>Backend result</small>
      <p>${escapeHtml(clauseResultLabel(clause))}</p>
    </div>
    ${acceptableLanguage}
  `;
}

async function loadPlaybook() {
  playbookList.innerHTML = '<div class="playbook-loading">Loading clauses</div>';
  clauseDetail.innerHTML = '<div class="detail-empty">Loading playbook</div>';

  try {
    const response = await fetch("/playbook");
    const playbook = await response.json();
    if (!response.ok) throw new Error(playbook.error || "Playbook could not load");

    playbookClauses = playbook.clauses || [];
    selectedClauseId = playbookClauses[0]?.id || null;
    renderClauseLane();
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
  playbookList.innerHTML = playbookClauses
    .map((clause, index) => {
      const selected = clause.id === selectedClauseId ? "selected" : "";
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
      selectedClauseId = row.dataset.clauseId;
      renderPlaybookList();
      renderClauseDetail();
    });
  });
}

function renderClauseDetail() {
  const clause = playbookClauses.find((item) => item.id === selectedClauseId);
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
