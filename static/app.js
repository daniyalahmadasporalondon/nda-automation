const studioDocTitle = document.querySelector("#studioDocTitle");
const studioNdaText = document.querySelector("#studioNdaText");
const studioDocumentRender = document.querySelector("#studioDocumentRender");
const studioFileMeta = document.querySelector("#studioFileMeta");
const studioCounterpartyMeta = document.querySelector("#studioCounterpartyMeta");
const studioSaveDraftButton = document.querySelector("#studioSaveDraftButton");
const studioDiscardDraftButton = document.querySelector("#studioDiscardDraftButton");
const studioExportButton = document.querySelector("#studioExportButton");
const studioSendButton = document.querySelector("#studioSendButton");
const studioReviewedButton = document.querySelector("#studioReviewedButton");
const studioSendModal = document.querySelector("#studioSendModal");
const studioSendForm = document.querySelector("#studioSendForm");
const studioSendModalClose = document.querySelector("#studioSendModalClose");
const studioSendFrom = document.querySelector("#studioSendFrom");
const studioSendTo = document.querySelector("#studioSendTo");
const studioSendAttachment = document.querySelector("#studioSendAttachment");
const studioSendSubject = document.querySelector("#studioSendSubject");
const studioSendBody = document.querySelector("#studioSendBody");
const studioSendSummary = document.querySelector("#studioSendSummary");
const studioSendStatus = document.querySelector("#studioSendStatus");
const studioSendCancelButton = document.querySelector("#studioSendCancelButton");
const studioSendConfirmButton = document.querySelector("#studioSendConfirmButton");
const studioClearButton = document.querySelector("#studioClearButton");
const studioUndoEditButton = document.querySelector("#studioUndoEditButton");
const studioClauseLane = document.querySelector("#studioClauseLane");
const studioDetailPanel = document.querySelector("#studioDetailPanel");
const studioInspectorTitle = document.querySelector("#studioInspectorTitle");
const reviewInspectorButtons = document.querySelectorAll("[data-review-inspector]");
// Clause-panel summary header was removed; tolerate the absent nodes so the
// review flow's textContent/className writes become harmless no-ops.
const studioMatchSummary = document.querySelector("#studioMatchSummary") || {};
const studioOverallTitle = document.querySelector("#studioOverallTitle") || {};
const studioResultMark = document.querySelector("#studioResultMark") || {};
const studioResultMeta = document.querySelector("#studioResultMeta") || {};
const studioDraftMeta = document.querySelector("#studioDraftMeta");
const tabButtons = document.querySelectorAll("[data-tab]");
const views = document.querySelectorAll("[data-view]");
const adminWorkspaceTabs = new Set(["playbook", "admin", "guide"]);
const adminSectionButtons = document.querySelectorAll("[data-admin-section]");
const adminPanels = document.querySelectorAll("[data-admin-panel]");
const adminWorkspaceView = document.querySelector("#clausesView");
const adminRailEyebrow = document.querySelector("#adminRailEyebrow");
const adminRailTitle = document.querySelector("#adminRailTitle");
const playbookList = document.querySelector("#playbookList");
const clauseDetail = document.querySelector("#clauseDetail");
const REPOSITORY_REFRESH_INTERVAL_MS = 15_000;

const state = AppState.createInitialState({ documentViewMode: VIEW_MODE_REDLINE });
let pendingReviewSendMatterId = null;
let authSessionController;
let adminAiController;
let adminIntegrationsController;

const repositoryController = createRepositoryController({
  state,
  gmailDemoMatterList: document.querySelector("#gmailDemoMatterList"),
  repositoryMatterPanel: document.querySelector("#repositoryMatterPanel"),
  downloadBlob,
  downloadFilename,
  loadMatterIntoReview,
  prepareMatterReviewLoad,
  redlineDownloadFilename,
  showMatterReviewLoadError,
  reviewErrorFromPayload,
});
createManualUploadController({
  fileInput: document.querySelector("#manualUploadFileInput"),
  form: document.querySelector("#manualUploadForm"),
  selectedFileNode: document.querySelector("#manualUploadSelectedFile"),
  statusNode: document.querySelector("#manualUploadStatus"),
  subjectInput: document.querySelector("#manualUploadSubjectInput"),
  senderInput: document.querySelector("#manualUploadSenderInput"),
  noteInput: document.querySelector("#manualUploadNoteInput"),
  submitButton: document.querySelector("#manualUploadSubmitButton"),
  clearButton: document.querySelector("#manualUploadClearButton"),
  dropzone: document.querySelector("#manualUploadDropzone"),
  fileToBase64,
  repositoryController,
  activateTab,
  reviewErrorFromPayload,
});
adminAiController = createAdminAiController({
  state,
  aiCard: document.querySelector("#adminAiCard"),
  aiKeyForm: document.querySelector("#adminAiKeyForm"),
  aiApiKeyInput: document.querySelector("#adminAiApiKeyInput"),
  aiClearKeyButton: document.querySelector("#adminAiClearKeyButton"),
  aiEnabledToggle: document.querySelector("#adminAiEnabledToggle"),
  runtimeForm: document.querySelector("#adminRuntimeForm"),
  activeReviewEngineSelect: document.querySelector("#adminActiveReviewEngineSelect"),
  aiFirstFallbackSelect: document.querySelector("#adminAiFirstFallbackSelect"),
  runtimeSaveButton: document.querySelector("#adminRuntimeSaveButton"),
  aiFacts: document.querySelector("#adminAiFacts"),
  aiOverall: document.querySelector("#adminAiOverall"),
  aiRefreshButton: document.querySelector("#adminAiRefreshButton"),
  reviewErrorFromPayload,
});
adminIntegrationsController = createAdminIntegrationsController({
  state,
  gmailCard: document.querySelector("#adminGmailCard"),
  gmailFacts: document.querySelector("#adminGmailFacts"),
  gmailOverall: document.querySelector("#adminGmailOverall"),
  gmailRecentSend: document.querySelector("#adminGmailRecentSend"),
  gmailRefreshButton: document.querySelector("#adminGmailRefreshButton"),
  gmailSetupPanel: document.querySelector("#adminGmailSetupPanel"),
  gmailInboundToggle: document.querySelector("#adminGmailInboundToggle"),
  gmailOutboundToggle: document.querySelector("#adminGmailOutboundToggle"),
  gmailFrequencyControl: document.querySelector("#adminGmailFrequencyControl"),
  gmailSearchForm: document.querySelector("#adminGmailSearchForm"),
  gmailSearchTermsInput: document.querySelector("#adminGmailSearchTermsInput"),
  gmailSearchSaveButton: document.querySelector("#adminGmailSearchSaveButton"),
  gmailSyncHistory: document.querySelector("#adminGmailSyncHistory"),
  reviewErrorFromPayload,
});
authSessionController = createAuthSessionController({
  state,
  root: document.querySelector("#sessionStrip"),
  userNode: document.querySelector("[data-session-user]"),
  gmailNode: document.querySelector("[data-session-gmail]"),
  warningNode: document.querySelector("[data-session-warning]"),
  loginLink: document.querySelector("[data-session-login]"),
  logoutButton: document.querySelector("[data-session-logout]"),
  connectButton: document.querySelector("[data-session-gmail-connect]"),
  syncButton: document.querySelector("[data-session-gmail-sync]"),
  disconnectButton: document.querySelector("[data-session-gmail-disconnect]"),
  reviewErrorFromPayload,
  onGmailStatus: (gmailStatus) => {
    state.gmailStatus = gmailStatus;
    repositoryController.renderBoard();
    adminIntegrationsController.renderGmailStatus(gmailStatus);
  },
  onSyncComplete: () => {
    repositoryController.loadMatters();
    adminIntegrationsController.load();
  },
});
const playbookController = createPlaybookController({
  state,
  playbookList,
  clauseDetail,
  renderStudioEmpty,
});
const reviewStructureController = createContractStructureController({
  state,
  root: studioDetailPanel,
});

setupSourceEditors();
setupReviewWorkstationActions();
setActiveTab("review");
setupDocumentViewModes();
setupReviewUndoControls();

const emptyState = () => {
  renderStudioEmpty();
};

emptyState();
playbookController.loadPlaybook();
repositoryController.loadMatters();
repositoryController.loadGmailStatus();
authSessionController.load();
adminAiController.load();
adminIntegrationsController.load();
window.setInterval(() => {
  if (document.querySelector('[data-view="repository"]')?.classList.contains("active")) {
    repositoryController.loadMatters();
    repositoryController.loadGmailStatus();
  }
}, REPOSITORY_REFRESH_INTERVAL_MS);

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
  button.addEventListener("click", () => {
    if (button.dataset.adminSurface) activateAdminSurface(button.dataset.adminSurface);
    activateAdminSection(button.dataset.adminSection);
  });
});

reviewInspectorButtons.forEach((button) => {
  button.addEventListener("click", () => setReviewInspectorView(button.dataset.reviewInspector));
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
    const active = view.dataset.view === tabName
      || (view.dataset.view === "admin-workspace" && adminWorkspaceTabs.has(tabName));
    view.classList.toggle("active", active);
    view.hidden = !active;
  });
}

function activateTab(tabName) {
  setActiveTab(tabName);
  if (tabName === "review") {
    requestAnimationFrame(resizeSourceEditors);
  }
  if (tabName === "repository") {
    repositoryController.loadMatters();
    repositoryController.loadGmailStatus();
  }
  if (tabName === "playbook") {
    activateAdminSurface("playbook");
    activateAdminSection("playbook");
  }
  if (tabName === "admin") {
    activateAdminSurface("admin");
    if (!["ai", "email"].includes(activeAdminSection())) {
      activateAdminSection("ai");
    } else {
      activateAdminSection(activeAdminSection());
    }
  }
  if (tabName === "guide") {
    activateAdminSurface("guide");
    if (!["document", "checkers", "ai_guide"].includes(activeAdminSection())) {
      activateAdminSection("document");
    } else {
      activateAdminSection(activeAdminSection());
    }
  }
}

function activateAdminSurface(surfaceName) {
  const surface = adminWorkspaceTabs.has(surfaceName) ? surfaceName : "playbook";
  if (adminWorkspaceView) {
    adminWorkspaceView.dataset.adminSurface = surface;
    const tab = document.querySelector(`[data-tab="${surface}"]`);
    if (tab?.id) adminWorkspaceView.setAttribute("aria-labelledby", tab.id);
  }
  if (adminRailEyebrow && adminRailTitle) {
    const labels = {
      admin: ["admin", "Operations"],
      guide: ["guide", "Methodology"],
      playbook: ["playbook", "Legal policy"],
    };
    const [eyebrow, title] = labels[surface] || labels.playbook;
    adminRailEyebrow.textContent = eyebrow;
    adminRailTitle.textContent = title;
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
  if (sectionName === "email") {
    adminIntegrationsController.load();
  }
  if (sectionName === "ai") {
    adminAiController.load();
  }
}

function setReviewInspectorView(viewName) {
  state.reviewInspectorView = viewName === "structure" ? "structure" : "clause";
  updateReviewInspectorTabs();
  renderStudioDetail();
}

function updateReviewInspectorTabs() {
  const selectedView = state.reviewInspectorView === "structure" ? "structure" : "clause";
  reviewInspectorButtons.forEach((button) => {
    const active = button.dataset.reviewInspector === selectedView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  if (studioInspectorTitle) {
    studioInspectorTitle.textContent = selectedView === "structure" ? "Contract Structure" : "Selected Clause";
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
