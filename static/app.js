const ndaText = document.querySelector("#ndaText");
const reviewButton = document.querySelector("#reviewButton");
const clearButton = document.querySelector("#clearButton");
const fileInput = document.querySelector("#fileInput");
const fileMeta = document.querySelector("#fileMeta");
const docTitle = document.querySelector("#docTitle");
const workspaceDocTitle = document.querySelector("#workspaceDocTitle");
const studioDocTitle = document.querySelector("#studioDocTitle");
const studioNdaText = document.querySelector("#studioNdaText");
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
const playbookList = document.querySelector("#playbookList");
const clauseDetail = document.querySelector("#clauseDetail");

let playbookClauses = [];
let selectedClauseId = null;
let selectedDocument = null;
let reviewClauses = [];
let selectedReviewClauseId = null;

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
      selectedReviewClauseId = row.dataset.studioClauseId;
      renderStudioResult({ clauses: reviewClauses });
      renderClauseLane();
      renderReviewClauseList();
      renderReviewDetail();
    });
  });

  renderStudioClauseLane();
  renderStudioDetail();
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
      selectedReviewClauseId = row.dataset.studioLaneId;
      renderStudioResult({ clauses: reviewClauses });
      renderClauseLane();
      renderReviewClauseList();
      renderReviewDetail();
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
      selectedReviewClauseId = item.dataset.laneClauseId;
      renderClauseLane();
      renderReviewClauseList();
      renderReviewDetail();
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
      selectedReviewClauseId = card.dataset.reviewClauseId;
      renderClauseLane();
      renderStudioResult({ clauses: reviewClauses });
      renderReviewClauseList();
      renderReviewDetail();
    });
    card.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selectedReviewClauseId = card.dataset.reviewClauseId;
      renderClauseLane();
      renderStudioResult({ clauses: reviewClauses });
      renderReviewClauseList();
      renderReviewDetail();
    });
  });
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
