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
  const checks = (result.clauses || []).filter((clause) => clause.status === "fail").length;
  overallTitle.textContent = passed ? "Meets requirements" : "Does not meet requirements";
  resultMark.textContent = passed ? "PASS" : "CHECK";
  resultMeta.textContent = passed
    ? "All hard clauses are currently satisfied."
    : `${checks} hard ${checks === 1 ? "clause needs" : "clauses need"} checking.`;
  resultHero.className = `result-hero ${passed ? "pass" : "fail"}`;

  reviewClauses = result.clauses || [];
  selectedReviewClauseId = reviewClauses.find((clause) => clause.status === "fail")?.id || reviewClauses[0]?.id || null;
  renderClauseLane();
  renderStudioResult(result);
  renderReviewClauseList();
  renderReviewDetail();
}

function renderStudioEmpty() {
  if (!studioIssueList) return;
  showStudioSourceEditor();
  studioMatchSummary.textContent = `0/${playbookClauses.length || 6}`;
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
  const passedCount = clauses.filter((clause) => clause.status === "pass").length;
  const failedCount = clauses.filter((clause) => clause.status === "fail").length;
  studioMatchSummary.textContent = `${passedCount}/${clauses.length || playbookClauses.length || 6}`;
  studioOverallTitle.textContent = failedCount
    ? `${failedCount} ${failedCount === 1 ? "clause needs" : "clauses need"} checking`
    : "All hard clauses match";

  studioIssueList.innerHTML = clauses
    .map((clause) => {
      const selected = clause.id === selectedReviewClauseId ? "selected" : "";
      const statusText = clause.status === "fail" ? "Verify" : "Match";
      return `
        <button class="studio-issue-row ${selected}" type="button" data-studio-clause-id="${escapeHtml(clause.id)}">
          <span>${escapeHtml(clause.name)}</span>
          <strong class="${clause.status}">${statusText}</strong>
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
      const statusClass = clause.status === "fail" ? "verify" : clause.status === "pass" ? "match" : "pending";
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
  const statusText = clause.status === "fail" ? "Verify against playbook" : "Matches playbook";
  studioDetailPanel.innerHTML = `
    <p class="eyebrow">playbook language</p>
    <h3>${escapeHtml(clause.name)}</h3>
    <p>${escapeHtml(clause.requirement)}</p>
    <strong class="studio-detail-status">${statusText}</strong>
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
      const status = clause.status === "fail" ? "check" : clause.status;
      const statusText = clause.status === "fail" ? "Check" : clause.status === "pass" ? "Pass" : "Pending";
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
      const statusLabel = clause.status === "fail" ? "CHECK" : clause.status.toUpperCase();
      return `
        <article class="clause-card ${selected}" data-review-clause-id="${escapeHtml(clause.id)}" tabindex="0">
          <header>
            <div>
              <h3>${escapeHtml(clause.name)}</h3>
              <p class="requirement">${escapeHtml(clause.requirement)}</p>
            </div>
            <span class="status ${clause.status}">${statusLabel}</span>
          </header>
          <p class="finding">${escapeHtml(clause.finding)}</p>
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

  const text = studioNdaText.value.trim();
  if (!text || !reviewClauses.length) {
    showStudioSourceEditor();
    return;
  }

  const ranges = reviewClauses
    .flatMap((clause) => findClauseTextRanges(studioNdaText.value, clause)
      .map((range) => ({ clause, range })));
  const paragraphs = getDocumentParagraphs(studioNdaText.value);

  studioDocumentRender.innerHTML = paragraphs
    .map((paragraph, index) => {
      const linked = ranges.filter((item) => rangesOverlap(paragraph, item.range));
      const selected = linked.find((item) => item.clause.id === selectedReviewClauseId);
      const primary = selected || linked.find((item) => item.clause.status === "fail") || linked[0];
      const ids = linked.map((item) => item.clause.id).join(" ");
      const classes = [
        "studio-doc-paragraph",
        linked.length ? "has-clause" : "",
        primary?.clause.status === "fail" ? "verify" : "",
        primary?.clause.status === "pass" ? "match" : "",
        selected ? "selected" : "",
      ]
        .filter(Boolean)
        .join(" ");

      return `<p class="${classes}" data-paragraph-index="${index}" data-clause-ids="${escapeHtml(ids)}">${escapeHtml(paragraph.text)}</p>`;
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

function getDocumentParagraphs(text) {
  const hasBlankLineBreaks = /\n\s*\n/.test(text);
  const separator = hasBlankLineBreaks ? /\n\s*\n/g : /\n+/g;
  const paragraphs = [];
  let cursor = 0;
  let match;

  while ((match = separator.exec(text))) {
    addParagraph(paragraphs, text, cursor, match.index);
    cursor = match.index + match[0].length;
  }

  addParagraph(paragraphs, text, cursor, text.length);
  return paragraphs;
}

function addParagraph(paragraphs, text, start, end) {
  const raw = text.slice(start, end);
  const trimmed = raw.trim();
  if (!trimmed) return;
  const leading = raw.match(/^\s*/)[0].length;
  const trailing = raw.match(/\s*$/)[0].length;
  paragraphs.push({
    end: end - trailing,
    start: start + leading,
    text: trimmed,
  });
}

function rangesOverlap(first, second) {
  return first.start < second.end && second.start < first.end;
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
  if (!sourceInput?.value.trim()) return;

  const range = findClauseTextRange(sourceInput.value, clause);
  if (!range) return;

  if (sourceInput === studioNdaText && studioDocumentRender && !studioDocumentRender.hidden) {
    scrollRenderedClauseToView(clause.id);
    return;
  }

  focusTextRange(sourceInput, range.start, range.end);
}

function getActiveSourceInput() {
  const activeView = document.querySelector("[data-view].active");
  return activeView?.dataset.view === "review" ? ndaText : studioNdaText;
}

function findClauseTextRange(text, clause) {
  return findClauseTextRanges(text, clause)[0] || null;
}

function findClauseTextRanges(text, clause) {
  const paragraphRanges = findClauseParagraphRanges(text, clause);
  if (paragraphRanges.length) return paragraphRanges;

  const searchIndex = createSearchIndex(text);
  const evidenceRanges = uniqueRanges((clause.evidence || [])
    .map((snippet) => findQueryRange(searchIndex, snippet, 12))
    .filter(Boolean)
    .map((range) => expandToParagraph(text, range.start, range.end)));

  if (evidenceRanges.length) return evidenceRanges;

  const fallbackTerms = [
    ...(CLAUSE_JUMP_TERMS[clause.id] || []),
    clause.name,
  ];
  const fallbackRange = fallbackTerms
    .map((term) => findQueryRange(searchIndex, term, 3))
    .find(Boolean);

  return fallbackRange ? [expandToParagraph(text, fallbackRange.start, fallbackRange.end)] : [];
}

function findClauseParagraphRanges(text, clause) {
  const paragraphs = getDocumentParagraphs(text);
  const scored = paragraphs
    .map((paragraph, index) => ({
      index,
      paragraph,
      score: scoreParagraphForClause(paragraph.text, clause),
    }))
    .filter((item) => item.score > 0);

  if (!scored.length) return [];

  if (clause.id === "signatures") {
    const strongHits = scored.filter((item) => item.score >= 6);
    const hits = strongHits.length ? strongHits : scored;
    const startIndex = Math.min(...hits.map((item) => item.index));
    const endIndex = Math.max(...hits.map((item) => item.index));
    return [{
      start: paragraphs[startIndex].start,
      end: paragraphs[endIndex].end,
    }];
  }

  const maxScore = Math.max(...scored.map((item) => item.score));
  const maxMatches = clause.id === "term_and_survival" ? 2 : 1;
  return scored
    .filter((item) => item.score >= maxScore)
    .slice(0, maxMatches)
    .map((item) => ({
      start: item.paragraph.start,
      end: item.paragraph.end,
    }));
}

function scoreParagraphForClause(text, clause) {
  const normalized = normalizeQuery(text);
  const rules = CLAUSE_PARAGRAPH_RULES[clause.id] || [];
  let score = 0;

  rules.forEach((rule) => {
    if (rule.pattern.test(normalized)) score += rule.weight;
  });

  if (clause.id === "confidential_information") {
    const categoryHits = CONFIDENTIAL_INFO_CATEGORIES
      .filter((category) => normalized.includes(category)).length;
    score += Math.min(categoryHits, 6);
  }

  if (clause.id === "term_and_survival" && /\byears?\b/.test(normalized)) {
    score += 4;
  }

  return score;
}

function uniqueRanges(ranges) {
  const seen = new Set();
  return ranges.filter((range) => {
    const key = `${range.start}:${range.end}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

const CLAUSE_JUMP_TERMS = {
  mutuality: ["each party", "both parties", "mutual", "disclosing party", "receiving party"],
  confidential_information: ["confidential information", "any and all information", "business", "financial", "technical"],
  governing_law: ["governing law", "governed by", "laws of", "england and wales", "difc", "delaware", "india"],
  term_and_survival: ["term", "survive", "survival", "period", "years"],
  non_circumvention: ["non-circumvention", "non circumvention", "circumvent", "introduced parties", "exclusive dealing"],
  signatures: ["signatures", "signature", "title:", "date:", "by:"],
};

const CONFIDENTIAL_INFO_CATEGORIES = [
  "business",
  "customer",
  "employee",
  "financial",
  "market",
  "pricing",
  "proprietary",
  "source code",
  "supplier",
  "technical",
  "trade secret",
];

const CLAUSE_PARAGRAPH_RULES = {
  mutuality: [
    { pattern: /\bmutual\b/, weight: 9 },
    { pattern: /\bboth parties\b/, weight: 8 },
    { pattern: /\beach party\b.*\b(disclosing|receiving) party\b/, weight: 8 },
    { pattern: /\bdisclosing party\b.*\breceiving party\b/, weight: 6 },
    { pattern: /\breceiving party\b.*\bdisclosing party\b/, weight: 6 },
    { pattern: /\bone[- ]way\b|\bunilateral\b|\bonly the receiving party\b|\brecipient only\b/, weight: 8 },
  ],
  confidential_information: [
    { pattern: /\bdefinition of confidential information\b/, weight: 11 },
    { pattern: /\bconfidential information\b.{0,40}\b(means|shall mean|includes?|refers to|is defined)\b/, weight: 11 },
    { pattern: /\b(means|shall mean|includes?)\b.{0,40}\bconfidential information\b/, weight: 9 },
    { pattern: /\bany and all information\b/, weight: 8 },
    { pattern: /\bnon[- ]public information\b/, weight: 5 },
    { pattern: /\bindependently developed\b|\bresidual knowledge\b|\bresiduals\b|\breverse engineer(?:ing)?\b/, weight: 6 },
  ],
  governing_law: [
    { pattern: /\bgoverning law\b/, weight: 11 },
    { pattern: /\bgoverned by\b/, weight: 9 },
    { pattern: /\blaws of\b/, weight: 6 },
    { pattern: /\bjurisdiction\b|\bcourts?\b|\barbitration\b/, weight: 3 },
  ],
  term_and_survival: [
    { pattern: /\bterm\b|\bsurviv(?:e|al|es|ing)\b/, weight: 8 },
    { pattern: /\bremain in effect\b|\bperiod of\b|\bterminate\b|\btermination\b/, weight: 5 },
    { pattern: /\bindefinitely\b|\bperpetual confidentiality\b|\bfor so long as the information remains confidential\b/, weight: 9 },
  ],
  non_circumvention: [
    { pattern: /\bnon[- ]circumvention\b/, weight: 11 },
    { pattern: /\bcircumvent(?:ion|s|ed|ing)?\b/, weight: 10 },
    { pattern: /\bintroduced parties\b|\bsubstitute purpose\b|\bexclusive dealing\b/, weight: 8 },
  ],
  signatures: [
    { pattern: /\bsignatures?\b|\bexecution block\b/, weight: 9 },
    { pattern: /\btitle\s*:/, weight: 8 },
    { pattern: /\bdate\s*:/, weight: 8 },
    { pattern: /\bby\s*:/, weight: 7 },
    { pattern: /^for\s+(?!a\b|the\b|any\b|period\b|purpose\b|purposes\b|term\b)[a-z0-9&.,' -]{2,80}/, weight: 5 },
  ],
};

function createSearchIndex(text) {
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

    normalized += char.toLowerCase();
    map.push(index);
    previousWasSpace = false;
  }

  return { normalized, map };
}

function findQueryRange(searchIndex, query, minLength) {
  const normalizedQuery = normalizeQuery(query);
  if (normalizedQuery.length < minLength) return null;

  const candidates = [normalizedQuery, ...queryWindows(normalizedQuery)];
  for (const candidate of candidates) {
    if (candidate.length < minLength) continue;
    const start = searchIndex.normalized.indexOf(candidate);
    if (start === -1) continue;
    const endIndex = Math.min(start + candidate.length - 1, searchIndex.map.length - 1);
    return {
      start: searchIndex.map[start],
      end: searchIndex.map[endIndex] + 1,
    };
  }

  return null;
}

function normalizeQuery(value) {
  return String(value)
    .replace(/^\s*\.\.\./, "")
    .replace(/\.\.\.\s*$/, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

function queryWindows(query) {
  const words = query.split(" ").filter(Boolean);
  if (words.length < 10) return [];

  return [
    words.slice(0, 12).join(" "),
    words.slice(Math.max(words.length - 12, 0)).join(" "),
    words.slice(Math.max(Math.floor(words.length / 2) - 6, 0), Math.floor(words.length / 2) + 6).join(" "),
  ];
}

function expandToParagraph(text, start, end) {
  const doubleStart = text.lastIndexOf("\n\n", start);
  const singleStart = text.lastIndexOf("\n", start);
  let paragraphStart = doubleStart >= 0 ? doubleStart + 2 : singleStart >= 0 ? singleStart + 1 : 0;

  const doubleEnd = text.indexOf("\n\n", end);
  const singleEnd = text.indexOf("\n", end);
  let paragraphEnd = doubleEnd >= 0 ? doubleEnd : singleEnd >= 0 ? singleEnd : text.length;

  if (paragraphEnd - paragraphStart > 1400) {
    paragraphStart = Math.max(0, start - 180);
    paragraphEnd = Math.min(text.length, end + 520);
  }

  return { start: paragraphStart, end: Math.max(paragraphEnd, end) };
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

  const target = Array.from(studioDocumentRender.querySelectorAll("[data-clause-ids]"))
    .find((paragraph) => paragraph.dataset.clauseIds.split(" ").includes(clauseId));
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

  const statusLabel = clause.status === "fail" ? "CHECK" : clause.status.toUpperCase();
  const evidence = clause.evidence?.length
    ? clause.evidence.map((snippet) => `<p class="evidence">${escapeHtml(snippet)}</p>`).join("")
    : '<p class="review-detail-muted">No evidence snippet captured.</p>';

  reviewDetail.innerHTML = `
    <div class="review-detail-header">
      <div>
        <p class="eyebrow">selected clause</p>
        <h2>${escapeHtml(clause.name)}</h2>
      </div>
      <span class="status ${clause.status}">${statusLabel}</span>
    </div>
    <div class="review-detail-block">
      <small>Requirement</small>
      <p>${escapeHtml(clause.requirement)}</p>
    </div>
    <div class="review-detail-block finding-block">
      <small>Finding</small>
      <p>${escapeHtml(clause.finding)}</p>
    </div>
    <div class="review-detail-evidence">
      <small>Evidence</small>
      ${evidence}
    </div>
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
