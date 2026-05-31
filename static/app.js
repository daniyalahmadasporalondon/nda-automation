const studioDocTitle = document.querySelector("#studioDocTitle");
const studioNdaText = document.querySelector("#studioNdaText");
const studioDocumentRender = document.querySelector("#studioDocumentRender");
const studioFileMeta = document.querySelector("#studioFileMeta");
const studioReviewButton = document.querySelector("#studioReviewButton");
const studioSaveDraftButton = document.querySelector("#studioSaveDraftButton");
const studioDiscardDraftButton = document.querySelector("#studioDiscardDraftButton");
const studioExportButton = document.querySelector("#studioExportButton");
const studioSendButton = document.querySelector("#studioSendButton");
const studioClearButton = document.querySelector("#studioClearButton");
const studioClauseLane = document.querySelector("#studioClauseLane");
const studioDetailPanel = document.querySelector("#studioDetailPanel");
const studioMatchSummary = document.querySelector("#studioMatchSummary");
const studioOverallTitle = document.querySelector("#studioOverallTitle");
const studioResultMark = document.querySelector("#studioResultMark");
const studioResultMeta = document.querySelector("#studioResultMeta");
const studioDraftMeta = document.querySelector("#studioDraftMeta");
const tabButtons = document.querySelectorAll("[data-tab]");
const views = document.querySelectorAll("[data-view]");
const adminSectionButtons = document.querySelectorAll("[data-admin-section]");
const adminPanels = document.querySelectorAll("[data-admin-panel]");
const playbookList = document.querySelector("#playbookList");
const clauseDetail = document.querySelector("#clauseDetail");

const state = {
  playbookClauses: [],
  selectedClauseId: null,
  selectedDocument: null,
  selectedMatter: null,
  matters: [],
  reviewClauses: [],
  reviewOriginalParagraphs: [],
  reviewParagraphs: [],
  reviewRedlines: [],
  latestReviewResult: null,
  reviewSourceText: "",
  selectedReviewClauseId: null,
  clauseJumpIndexes: {},
  exportClauseDecisions: {},
  redlineTemplateSelections: {},
  redlineDraft: null,
  redlineDraftDirty: false,
  documentViewMode: VIEW_MODE_REDLINE,
  gmailStatus: null,
};
let pendingReviewSendMatterId = null;
let adminIntegrationsController;

const repositoryController = createRepositoryController({
  state,
  repositoryDemoResetButton: document.querySelector("#repositoryDemoResetButton"),
  gmailDemoMatterList: document.querySelector("#gmailDemoMatterList"),
  repositoryMatterPanel: document.querySelector("#repositoryMatterPanel"),
  repositoryImportStatus: document.querySelector("#repositoryImportStatus"),
  downloadBlob,
  downloadFilename,
  loadMatterIntoReview,
  redlineDownloadFilename,
  reviewErrorFromPayload,
});
adminIntegrationsController = createAdminIntegrationsController({
  state,
  gmailCard: document.querySelector("#adminGmailCard"),
  gmailFacts: document.querySelector("#adminGmailFacts"),
  gmailOverall: document.querySelector("#adminGmailOverall"),
  gmailRecentSend: document.querySelector("#adminGmailRecentSend"),
  gmailRefreshButton: document.querySelector("#adminGmailRefreshButton"),
  gmailInboundToggle: document.querySelector("#adminGmailInboundToggle"),
  gmailOutboundToggle: document.querySelector("#adminGmailOutboundToggle"),
  gmailFrequencyControl: document.querySelector("#adminGmailFrequencyControl"),
  reviewErrorFromPayload,
});
const playbookController = createPlaybookController({
  state,
  playbookList,
  clauseDetail,
  renderStudioEmpty,
});

setupSourceEditors();
setupReviewWorkstationActions();
setActiveTab("review");
setupDocumentViewModes();

const emptyState = () => {
  renderStudioEmpty();
};

emptyState();
playbookController.loadPlaybook();
repositoryController.loadMatters();
repositoryController.loadGmailStatus();
adminIntegrationsController.load();

tabButtons.forEach((button) => {
  button.addEventListener("click", () => {
    activateTab(button.dataset.tab);
  });
  button.addEventListener("keydown", (event) => {
    const nextTab = tabForKeyboardEvent(event, button);
    if (!nextTab) return;
    event.preventDefault();
    activateTab(nextTab.dataset.tab);
    nextTab.focus();
  });
});

adminSectionButtons.forEach((button) => {
  button.addEventListener("click", () => activateAdminSection(button.dataset.adminSection));
});

function reviewErrorFromPayload(payload, fallbackMessage) {
  const error = new Error(payload?.error || fallbackMessage);
  if (Array.isArray(payload?.details)) {
    error.details = payload.details.filter(Boolean).map((item) => String(item));
  }
  return error;
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

function activateTab(tabName) {
  setActiveTab(tabName);
  if (tabName === "review") {
    requestAnimationFrame(resizeSourceEditors);
  }
  if (tabName === "clauses" && activeAdminSection() === "integrations") {
    adminIntegrationsController.load();
  }
}

function activateAdminSection(sectionName) {
  adminSectionButtons.forEach((button) => {
    const active = button.dataset.adminSection === sectionName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  adminPanels.forEach((panel) => {
    const active = panel.dataset.adminPanel === sectionName;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  if (sectionName === "integrations") {
    adminIntegrationsController.load();
  }
}

function activeAdminSection() {
  return document.querySelector("[data-admin-section].active")?.dataset.adminSection || "playbook";
}

function tabForKeyboardEvent(event, currentButton) {
  const buttons = Array.from(tabButtons);
  const currentIndex = buttons.indexOf(currentButton);
  if (currentIndex < 0) return null;
  if (event.key === "Home") return buttons[0];
  if (event.key === "End") return buttons[buttons.length - 1];
  if (event.key === "ArrowRight" || event.key === "ArrowDown") {
    return buttons[(currentIndex + 1) % buttons.length];
  }
  if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
    return buttons[(currentIndex - 1 + buttons.length) % buttons.length];
  }
  return null;
}
