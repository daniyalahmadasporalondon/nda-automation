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
const studioApproveReviewButton = document.querySelector("#studioApproveReviewButton");
const studioApproveBlockReasons = document.querySelector("#studioApproveBlockReasons");
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
const dashboardSubmitButton = document.querySelector("[data-dashboard-submit]");
const dashboardInboxTableBody = document.querySelector("[data-dashboard-inbox-body]");
const dashboardInboxEmpty = document.querySelector("[data-dashboard-inbox-empty]");
const dashboardInboxCount = document.querySelector("[data-dashboard-inbox-count]");
const manualUploadModal = document.querySelector("#manualUploadModal");
const manualUploadModalClose = document.querySelector("#manualUploadModalClose");
const dashboardHealthItems = document.querySelectorAll("[data-dashboard-health]");
// Clause-panel summary header was removed; tolerate the absent nodes so the
// review flow's textContent/className writes become harmless no-ops.
const studioMatchSummary = document.querySelector("#studioMatchSummary") || {};
const studioOverallTitle = document.querySelector("#studioOverallTitle") || {};
const studioResultMark = document.querySelector("#studioResultMark") || {};
const studioResultMeta = document.querySelector("#studioResultMeta") || {};
const studioDraftMeta = document.querySelector("#studioDraftMeta");
const studioRefreshReviewButton = document.querySelector("#studioRefreshReviewButton");
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

function htmlEscape(value) {
  if (typeof window.escapeHtml === "function") return window.escapeHtml(value);
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Inspector view config — declared early (before the controllers and any init-time
// render that calls updateReviewInspectorTabs()) so REVIEW_INSPECTOR_VIEWS is never
// referenced in its temporal dead zone, which would halt the whole script at load.
const REVIEW_INSPECTOR_VIEWS = ["clause", "structure", "fill"];
const REVIEW_INSPECTOR_TITLES = {
  clause: "Selected Clause",
  structure: "Contract Structure",
  fill: "Fill Blanks",
};
let pendingReviewSendMatterId = null;
let authSessionController;
let adminAiController;
let adminHealthController;
let adminIntegrationsController;
let adminDriveController;
let adminPersonalisationController;

const repositoryController = createRepositoryController({
  state,
  gmailDemoMatterList: document.querySelector("#gmailDemoMatterList"),
  repositorySearchInput: document.querySelector("#repositorySearchInput"),
  repositoryMatterPanel: document.querySelector("#repositoryMatterPanel"),
  downloadBlob,
  downloadFilename,
  loadMatterIntoReview,
  prepareMatterReviewLoad,
  redlineDownloadFilename,
  showMatterReviewLoadError,
  reviewErrorFromPayload,
});
// Dashboard smart-search (v1, deterministic). Reads the same state.matters the
// Repository tab loads and reuses repositoryController.openMatter so a result
// click opens the matter exactly as the board does.
const dashboardSearchController = createDashboardSearchController({
  root: document.querySelector("[data-dashboard-search]"),
  input: document.querySelector("#dashboardSearchInput"),
  form: document.querySelector("#dashboardSearchForm"),
  chipList: document.querySelector("#dashboardSearchChips"),
  resultsList: document.querySelector("#dashboardSearchResults"),
  resultsStatus: document.querySelector("#dashboardSearchResultsStatus"),
  interpretedLine: document.querySelector("#dashboardSearchInterpreted"),
  getMatters: () => state.matters,
  ensureMatters: () => Promise.resolve(repositoryController.loadMatters()).then(() => {
    renderDashboardInboxTable();
  }),
  openMatter: (matterId) => {
    // Reuse the repository open-matter flow, then surface the Repository tab so
    // the opened matter's detail panel is visible.
    repositoryController.openMatter(matterId);
    activateTab("repository");
  },
  // Async seam for the per-row "Summarize" affordance: POST to the matter's
  // summary endpoint and hand the controller {ok, payload}. The endpoint is
  // grounded in the matter's real document + review findings; on AI degradation
  // the backend returns a friendly error the controller renders verbatim.
  summarizeMatter: (matterId) =>
    summarizeMatterById(matterId),
  // v2 async seam: translate a natural-language query into a structured filter
  // spec via the AI endpoint. The model NEVER sees matters — only the query — and
  // the spec is validated server-side; the controller validates + applies it to
  // the real state.matters deterministically. On any failure/fallback the
  // controller falls back to the v1 keyword filter, so the box always works.
  assistantQuery: (query) => dashboardAssistantForQuery(query),
  confirmAssistantAction: (action) => confirmDashboardAssistantAction(action),
  searchIntent: (query) => searchIntentForQuery(query),
});
// In-app toast notifications for newly-arrived inbound NDAs. Fed by the matter
// list: the Repository poll feeds it via observe(state.matters), and on every
// other tab a lightweight poll() fetches /api/matters itself. The first feed
// seeds silently so the existing inbox never floods on load.
const notificationsController = createNotificationsController({
  container: document.querySelector("#toastStack"),
  openMatter: (matterId) => {
    repositoryController.openMatter(matterId);
    activateTab("repository");
  },
  openRepository: () => activateTab("repository"),
  fetchMatters: async () => {
    const response = await fetch("/api/matters");
    if (!response.ok) return [];
    const payload = await response.json();
    return Array.isArray(payload.matters) ? payload.matters : [];
  },
});
const manualUploadController = createManualUploadController({
  modalNode: manualUploadModal,
  closeButton: manualUploadModalClose,
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
  routeStageNode: document.querySelector("#manualUploadStageLabel"),
  allowedBoardColumns: ["manual_upload"],
  defaultBoardColumn: "manual_upload",
  boardColumnLabel: RepositoryModel.boardColumnLabel,
  submissionBoardColumn: RepositoryModel.manualUploadSubmissionColumn,
  fileToBase64,
  repositoryController,
  activateTab,
  reviewErrorFromPayload,
});
const sendDocumentController = createSendDocumentController({
  modalNode: document.querySelector("#sendDocumentModal"),
  closeButton: document.querySelector("#sendDocumentModalClose"),
  fileInput: document.querySelector("#sendDocumentFileInput"),
  form: document.querySelector("#sendDocumentForm"),
  selectedFileNode: document.querySelector("#sendDocumentSelectedFile"),
  statusNode: document.querySelector("#sendDocumentStatus"),
  recipientInput: document.querySelector("#sendDocumentRecipientInput"),
  subjectInput: document.querySelector("#sendDocumentSubjectInput"),
  bodyInput: document.querySelector("#sendDocumentBodyInput"),
  submitButton: document.querySelector("#sendDocumentSubmitButton"),
  clearButton: document.querySelector("#sendDocumentClearButton"),
  dropzone: document.querySelector("#sendDocumentDropzone"),
  draftNdaButton: document.querySelector("#sendDocumentDraftNdaButton"),
  fileToBase64,
  repositoryController,
  activateTab,
  reviewErrorFromPayload,
});
const draftIntakeController = createDraftIntakeController({
  form: document.querySelector("#draftIntakeForm"),
  entitySelect: document.querySelector("#draftIntakeEntitySelect"),
  addressField: document.querySelector("#draftIntakeAddressField"),
  addressSelect: document.querySelector("#draftIntakeAddressSelect"),
  bundleNode: document.querySelector("#draftIntakeBundle"),
  counterpartyNameInput: document.querySelector("#draftIntakeCounterpartyName"),
  counterpartyEmailInput: document.querySelector("#draftIntakeCounterpartyEmail"),
  ndaTypeSelect: document.querySelector("#draftIntakeNdaType"),
  termInput: document.querySelector("#draftIntakeTerm"),
  projectPurposeInput: document.querySelector("#draftIntakeProjectPurpose"),
  notesInput: document.querySelector("#draftIntakeNotes"),
  governingLawSelect: document.querySelector("#draftIntakeGoverningLaw"),
  lawStatusNode: document.querySelector("#draftIntakeLawStatus"),
  lawResetButton: document.querySelector("#draftIntakeLawResetButton"),
  statusNode: document.querySelector("#draftIntakeStatus"),
  clearButton: document.querySelector("#draftIntakeClearButton"),
  generateButton: document.querySelector("#draftIntakeGenerateButton"),
  sideEntityNode: document.querySelector("#draftIntakeSideEntity"),
  sideLawNode: document.querySelector("#draftIntakeSideLaw"),
  sideTypeNode: document.querySelector("#draftIntakeSideType"),
  previewNode: document.querySelector("#draftIntakePreview"),
  counterpartyIncorporationInput: document.querySelector("#draftIntakeCounterpartyIncorporation"),
  counterpartyAddressInput: document.querySelector("#draftIntakeCounterpartyAddress"),
  businessDescriptionInput: document.querySelector("#draftIntakeBusinessDescription"),
  downloadButton: document.querySelector("#draftIntakeDownloadButton"),
  sendButton: document.querySelector("#draftIntakeSendButton"),
  onGenerate: generateNdaFromDraft,
  onDownloadGenerated: downloadGeneratedNda,
  onSendGenerated: sendGeneratedNda,
  onEditGenerated: editGeneratedNda,
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
  runtimeSaveButton: document.querySelector("#adminRuntimeSaveButton"),
  aiFacts: document.querySelector("#adminAiFacts"),
  aiOverall: document.querySelector("#adminAiOverall"),
  aiRefreshButton: document.querySelector("#adminAiRefreshButton"),
  reviewErrorFromPayload,
});
adminHealthController = createAdminHealthController({
  state,
  healthCard: document.querySelector("#adminHealthCard"),
  healthFacts: document.querySelector("#adminHealthReviewFacts"),
  healthStatus: document.querySelector("#adminHealthStatus"),
  healthAlerts: document.querySelector("#adminHealthAlerts"),
  healthCaveat: document.querySelector("#adminHealthCaveat"),
  healthRaw: document.querySelector("#adminHealthRaw"),
  healthRefreshButton: document.querySelector("#adminHealthRefreshButton"),
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
  gmailToggle: document.querySelector("#adminGmailEnabledToggle"),
  gmailFrequencyControl: document.querySelector("#adminGmailFrequencyControl"),
  gmailSearchForm: document.querySelector("#adminGmailSearchForm"),
  gmailSearchTermsInput: document.querySelector("#adminGmailSearchTermsInput"),
  gmailSearchSaveButton: document.querySelector("#adminGmailSearchSaveButton"),
  gmailSyncHistory: document.querySelector("#adminGmailSyncHistory"),
  reviewErrorFromPayload,
});
adminDriveController = createAdminDriveController({
  state,
  driveCard: document.querySelector("#adminDriveCard"),
  driveFacts: document.querySelector("#adminDriveFacts"),
  driveOverall: document.querySelector("#adminDriveOverall"),
  driveRefreshButton: document.querySelector("#adminDriveRefreshButton"),
  driveConnectPanel: document.querySelector("#adminDriveConnectPanel"),
  driveEnabledToggle: document.querySelector("#adminDriveEnabledToggle"),
  driveFolderForm: document.querySelector("#adminDriveFolderForm"),
  driveFolderIdInput: document.querySelector("#adminDriveFolderIdInput"),
  driveFolderNameInput: document.querySelector("#adminDriveFolderNameInput"),
  driveFolderSaveButton: document.querySelector("#adminDriveFolderSaveButton"),
  reviewErrorFromPayload,
});
adminPersonalisationController = createAdminPersonalisationController({
  card: document.querySelector("#adminPersonalisationCard"),
  form: document.querySelector("#adminPersonalisationForm"),
  signOffInput: document.querySelector("#adminSignOffInput"),
  signatureInput: document.querySelector("#adminSignatureInput"),
  signatureBlockInput: document.querySelector("#adminSignatureBlockInput"),
  saveButton: document.querySelector("#adminPersonalisationSaveButton"),
  resetButton: document.querySelector("#adminPersonalisationResetButton"),
  overall: document.querySelector("#adminPersonalisationOverall"),
  message: document.querySelector("#adminPersonalisationMessage"),
  persistenceFact: document.querySelector('[data-admin-personalisation="persistence"]'),
  reviewErrorFromPayload,
  onSettingsLoaded: (settings) => {
    state.personalisationSettings = normalizePersonalisationSettings(settings);
  },
});
authSessionController = createAuthSessionController({
  state,
  root: document.querySelector("#sessionStrip"),
  userNode: document.querySelector("[data-session-user]"),
  gmailNode: document.querySelector("[data-session-gmail]"),
  accountToggle: document.querySelector("[data-session-account-toggle]"),
  accountMenu: document.querySelector("[data-session-account-menu]"),
  avatarNode: document.querySelector("[data-session-avatar]"),
  avatarImage: document.querySelector("[data-session-avatar-image]"),
  avatarInitial: document.querySelector("[data-session-avatar-initial]"),
  menuGreeting: document.querySelector("[data-session-menu-greeting]"),
  menuStatus: document.querySelector("[data-session-menu-status]"),
  menuAvatarImage: document.querySelector("[data-session-menu-avatar-image]"),
  menuAvatarInitial: document.querySelector("[data-session-menu-avatar-initial]"),
  greetingNode: document.querySelector("#dashboardHeroTitle"),
  warningNode: document.querySelector("[data-session-warning]"),
  loginLink: document.querySelector("[data-session-login]"),
  logoutButton: document.querySelector("[data-session-logout]"),
  connectButton: document.querySelector("[data-session-gmail-connect]"),
  syncButton: document.querySelector("[data-session-gmail-sync]"),
  disconnectButton: document.querySelector("[data-session-gmail-disconnect]"),
  reviewErrorFromPayload,
  onGmailStatus: (gmailStatus) => {
    state.gmailStatus = gmailStatus;
    renderDashboardEmailHealth(gmailStatus);
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
// Inbound-fill tool: the 3rd inspector tab. It scans the loaded paragraphs for
// blanks, lets the user choose an Aspora entity + address, and fill each blank
// either CLEAN (rewrites the paragraph text + advances the manual-redline
// baseline so no tracked redline is double-emitted) or TRACKED (left for the
// backend to render as a tracked change via the `fills` export payload). Reuses
// the same /api/signing-entities feed (with embedded mirror fallback) the
// generator's entity picker uses.
const reviewFillController = createFillController({
  state,
  root: studioDetailPanel,
  // Re-render the document viewer + source after a CLEAN fill mutates paragraph
  // text/baseline, so the filled text is immediately visible.
  rerenderDocument: () => {
    syncReviewSourceFromParagraphs();
    renderStudioDocumentHighlights();
  },
});
// Interactive PDF markup overlay for the review workstation's Original view.
// It mounts only while the Original page-image surface is shown and a matter is
// loaded; the render funnel (renderStudioDocumentHighlights) drives its
// onOriginalSurfaceRendered / onLeaveOriginal lifecycle.
const pdfMarkupController = createPdfMarkupController({
  state,
  downloadBlob,
  // escapeHtml is resolved lazily inside the controller (via window.escapeHtml)
  // because it is bridged by a deferred module that runs after this load-time
  // construction; passing it here would capture an undefined reference.
  getSurfaceRoot: () => studioDocumentRender?.querySelector("[data-original-surface]") || null,
  matterIsPdf: () => Boolean(state.selectedMatter?.id),
});

// Clicking an AI-referenced paragraph (e.g. "p15") in a clause assessment jumps the
// document to that paragraph and flashes it. Delegated at document level so it fires
// no matter which panel re-rendered the reference (jumpToParagraph lives in the viewer).
document.addEventListener("click", (event) => {
  const el = event.target;
  if (!el || typeof el.closest !== "function") return;
  const ref = el.closest("[data-para-ref]");
  if (ref && typeof jumpToParagraph === "function") {
    jumpToParagraph(ref.dataset.paraRef);
    return;
  }
  const glOption = el.closest("[data-gl-redline-law]");
  if (glOption && typeof applyGoverningLawRedline === "function") {
    applyGoverningLawRedline(glOption.dataset.glRedlinePhrase, glOption.dataset.glRedlineLaw);
  }
}, true);  // capture phase: fires before any handler that stops click propagation

setupSourceEditors();
setupReviewWorkstationActions();
setActiveTab("dashboard");
setupDocumentViewModes();
setupReviewUndoControls();

const emptyState = () => {
  renderStudioEmpty();
};

emptyState();
playbookController.loadPlaybook();
// Refresh any active dashboard-search results once the matter list resolves so
// a search run before data loaded picks up the real matters.
Promise.resolve(repositoryController.loadMatters()).then(() => {
  dashboardSearchController.refresh();
  renderDashboardInboxTable();
  // Silent seed: record the inbox already present at load so only genuinely new
  // inbound NDAs toast during the session.
  notificationsController.observe(state.matters);
});
repositoryController.loadGmailStatus();
authSessionController.load();
adminAiController.load();
loadPersonalisationSettings();
loadDashboardAiHealth();
loadDashboardDriveHealth();
adminIntegrationsController.load();
window.setInterval(() => {
  if (document.querySelector('[data-view="repository"]')?.classList.contains("active")) {
    Promise.resolve(repositoryController.loadMatters()).then(() => {
      renderDashboardInboxTable();
      notificationsController.observe(state.matters);
    });
    repositoryController.loadGmailStatus();
  } else {
    // On any non-Repository tab the board isn't refreshed, so the notifier polls
    // the matter list itself to keep new-inbound toasts flowing app-wide.
    notificationsController.poll();
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

dashboardSubmitButton?.addEventListener("click", () => {
  manualUploadController.openModal();
});


document.querySelector("[data-dashboard-send-document]")?.addEventListener("click", () => {
  sendDocumentController.openModal();
});

document.querySelector("[data-dashboard-inbox]")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-dashboard-inbox-open]");
  if (!button) return;
  const matterId = button.dataset.dashboardInboxOpen;
  if (!matterId) return;
  repositoryController.openMatter(matterId);
  activateTab("repository");
});

document.querySelectorAll("[data-repository-add-column]").forEach((button) => {
  button.addEventListener("click", () => {
    manualUploadController.openModal({ boardColumn: button.dataset.repositoryAddColumn });
  });
});

function reviewErrorFromPayload(payload, fallbackMessage) {
  const error = new Error(payload?.error || fallbackMessage);
  if (Array.isArray(payload?.details)) {
    error.details = payload.details.filter(Boolean).map((item) => String(item));
  }
  if (payload?.review_refresh && typeof payload.review_refresh === "object") {
    error.reviewRefresh = payload.review_refresh;
  }
  if (Array.isArray(payload?.stale_reasons)) {
    error.staleReasons = payload.stale_reasons.filter(Boolean).map((item) => String(item));
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

// onGenerate seam for the draft-intake "Generate NDA" button. POSTs the captured
// entity bundle + intake (buildDraftPayload's shape) to POST /api/generate-nda
// via the bridged generation API, then either triggers the DOCX download (byte
// response) or surfaces the saved artifact (JSON response). Returns a {message,
// tone} the controller renders; a thrown error is shown in the error tone.
//
// The endpoint lives on the generation branch and is not deployed on this base
// until integration, so a 404 is caught as GenerationUnavailableError and shown
// as a neutral "pending" notice — the same graceful degradation the entity
// picker uses for /api/signing-entities — rather than a generation failure.
async function generateNdaFromDraft(payload) {
  const api = window.createGenerationApi();
  const counterpartyEmail = payload?.counterparty?.email || "";
  const subject = payload?.counterparty?.name ? `NDA — ${payload.counterparty.name}` : "NDA";
  try {
    const result = await api.generateNda(payload);
    if (result.kind === "blob") {
      const filename = result.filename || draftNdaDownloadFilename(payload);
      // Don't auto-download — stage the Download/Send actions instead.
      return {
        message: "NDA generated — use Download or Send.",
        tone: "success",
        generated: { blob: result.blob, filename, counterpartyEmail, subject },
      };
    }
    // JSON response (the real contract): the document was generated, a matter +
    // tracked artifact were created, and download_url points at the matter source.
    // We no longer auto-download — the staged Download/Send buttons drive that.
    const generated = {
      downloadUrl: result.download_url || null,
      filename: result.filename || draftNdaDownloadFilename(payload),
      matterId: result.matter_id || null,
      counterpartyEmail,
      subject,
    };
    const savedFor = payload?.counterparty?.name ? ` for ${payload.counterparty.name}` : "";
    const summary = generatedManifestSummary(result.manifest);
    // The engine passes the Playbook deterministically; self_check is advisory, so
    // a rare miss is surfaced as a soft caution rather than blocking the success.
    if (result.self_check && result.self_check.passed === false) {
      return {
        message: `NDA generated and saved${savedFor}${summary}, but the self-check flagged it — review before sending.`,
        tone: "error",
        generated,
      };
    }
    // If untrusted intake text (purpose/notes) was neutralised on the way into the
    // document, surface it: the user should know their free text was sanitised.
    if (Array.isArray(result.manifest?.sanitized_fields) && result.manifest.sanitized_fields.length) {
      return {
        message: `NDA generated and saved${savedFor}${summary}. Note: ${result.manifest.sanitized_fields.join(", ")} was sanitised before drafting.`,
        tone: "error",
        generated,
      };
    }
    return {
      message: `NDA generated and saved${savedFor}${summary}. Use Download or Send.`,
      tone: "success",
      generated,
    };
  } catch (error) {
    if (error instanceof window.GenerationUnavailableError || error?.code === "generation_unavailable") {
      // Endpoint not deployed on this base yet — degrade gracefully.
      return {
        message: `Captured draft for ${payload.counterparty.name} on ${payload.signing_entity.legal_name} paper. Generation is not available on this build yet.`,
        tone: "success",
      };
    }
    throw error;
  }
}

// Download the last generated NDA — from the in-memory blob or the saved matter
// source URL. Wired to the staged "Download" button in the generator.
async function downloadGeneratedNda(generated) {
  if (!generated) return;
  // Prefer the clean edited version when the in-Generator editor has edits.
  const editedBlob = await editedGeneratedBlob();
  if (editedBlob) {
    downloadBlob(editedBlob, generated.filename || "nda.docx");
    return;
  }
  if (generated.blob) {
    downloadBlob(generated.blob, generated.filename || "nda.docx");
  } else if (generated.downloadUrl) {
    downloadUrl(generated.downloadUrl, generated.filename || "nda.docx");
  }
}

// The clean .docx with the in-Generator editor's edits baked in, or null when the
// editor has no edits (the caller then uses the original generated file).
async function editedGeneratedBlob() {
  if (window.generatorEditor && typeof window.generatorEditor.hasEdits === "function"
    && window.generatorEditor.hasEdits()) {
    try {
      return await window.generatorEditor.exportCleanDocx();
    } catch (error) {
      return null;
    }
  }
  return null;
}

// Open the Send Document modal pre-loaded with the generated NDA (+ counterparty
// email / subject) so the user can email it straight from the generator.
async function sendGeneratedNda(generated, { pending = false } = {}) {
  if (!generated) return;
  // Open the modal + prefill recipient/subject IMMEDIATELY and show an
  // "attaching…" state — the popup and the counterparty-email -> Recipient Email
  // link never wait on the document.
  sendDocumentController.openModal();
  if (typeof sendDocumentController.loadFile === "function") {
    sendDocumentController.loadFile(null, {
      recipient: generated.counterpartyEmail,
      subject: generated.subject,
    });
  }
  if (typeof sendDocumentController.showPendingAttachment === "function") {
    // pending = the NDA is still being generated (the slow, AI part); otherwise
    // we're just fetching the already-generated document to attach (fast).
    sendDocumentController.showPendingAttachment(
      pending ? "Generating the NDA… (this can take a moment)" : "Attaching the generated NDA…",
    );
  }
  // pending = the NDA isn't ready yet; a follow-up call attaches it once generated.
  if (pending) return;
  // Fetch the generated document and attach it to the open modal.
  const docxType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  try {
    let file = null;
    // Prefer the clean edited version when the in-Generator editor has edits.
    const editedBlob = await editedGeneratedBlob();
    if (editedBlob) {
      file = new File([editedBlob], generated.filename || "nda.docx", { type: docxType });
    } else if (generated.blob) {
      file = new File([generated.blob], generated.filename || "nda.docx", { type: docxType });
    } else if (generated.downloadUrl) {
      const response = await fetch(generated.downloadUrl, { headers: { Accept: docxType } });
      if (response.ok) {
        const blob = await response.blob();
        file = new File([blob], generated.filename || "nda.docx", { type: blob.type || docxType });
      }
    }
    if (file && typeof sendDocumentController.loadFile === "function") {
      sendDocumentController.loadFile(file);
    } else if (typeof sendDocumentController.showPendingAttachment === "function") {
      sendDocumentController.showPendingAttachment(
        "Couldn't attach the NDA automatically — select the document below.",
      );
    }
  } catch (error) {
    if (typeof sendDocumentController.showPendingAttachment === "function") {
      sendDocumentController.showPendingAttachment(
        "Couldn't attach the NDA automatically — select the document below.",
      );
    }
  }
}

// Open the last generated NDA in the in-Generator document editor, where it can be
// edited with the formatting toolbar right inside the Generator tab (no jump to
// Review). The generated NDA is already a matter with extracted paragraphs, which
// the editor loads from /api/matters/{id}/review. Download/Send then export the
// edited document. The legacy in-memory blob path has no matter, so it no-ops.
async function editGeneratedNda(generated) {
  if (!generated || !generated.matterId || !window.generatorEditor) return;
  await window.generatorEditor.load(generated.matterId);
}

// A short parenthetical summary of what the engine actually filled, from the
// response manifest (governing law + term) — so the success line confirms the
// generated terms at a glance. Empty when the manifest is absent or sparse.
//
// governing_law_value is the EFFECTIVE law written into the doc (server-
// authoritative). When the server marks it overridden, we surface the provenance
// — "England and Wales (overridden from India)" — so the user can see their law
// override actually took effect rather than silently snapping to the entity
// default.
function generatedManifestSummary(manifest) {
  if (!manifest || typeof manifest !== "object") return "";
  const bits = [];
  if (manifest.governing_law_value) {
    let law = String(manifest.governing_law_value);
    if (manifest.governing_law_overridden && manifest.entity_default_governing_law_value) {
      law += ` (overridden from ${manifest.entity_default_governing_law_value})`;
    }
    bits.push(law);
  }
  if (manifest.term_years) {
    const years = Number(manifest.term_years);
    if (Number.isFinite(years) && years > 0) bits.push(`${years}-year term`);
  }
  return bits.length ? ` (${bits.join(", ")})` : "";
}

// Derives a download filename when the response carries none, from the
// counterparty + signing entity so multiple drafts don't all land as "nda.docx".
function draftNdaDownloadFilename(payload) {
  const parts = [payload?.signing_entity?.legal_name, payload?.counterparty?.name, "nda"]
    .filter(Boolean)
    .join("-");
  const safe = Array.from(parts)
    .map((character) => (/[a-z0-9_-]/i.test(character) ? character : "-"))
    .join("")
    .replace(/-+/g, "-")
    .replace(/^[-_]+/g, "")
    .replace(/[-_]+$/g, "");
  return `${safe || "nda"}.docx`;
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
  if (tabName === "dashboard") {
    loadDashboardAiHealth();
    loadDashboardDriveHealth();
    renderDashboardEmailHealth(state.gmailStatus);
    renderDashboardInboxTable();
    // Re-run any active search against the freshest matters when returning to
    // the dashboard (the Repository tab may have loaded/changed the list).
    dashboardSearchController.refresh();
  }
  if (tabName === "generator") {
    // Load the signing-entity registry on first activation and render the
    // current intake state. Idempotent — re-activating preserves in-progress
    // input rather than resetting the form.
    draftIntakeController.activate();
  }
  if (tabName === "review") {
    // The playbook clause list loads asynchronously after bootstrap, so the
    // initial empty-studio render can show a stale "0/0" total. Re-render the
    // empty state when the Review tab is shown (before any review has run) so the
    // clause total reflects the now-loaded playbook.
    if (!state.latestReviewResult && !state.reviewClauses.length) {
      renderStudioEmpty();
    }
    requestAnimationFrame(resizeSourceEditors);
  }
  if (tabName === "repository") {
    Promise.resolve(repositoryController.loadMatters()).then(() => {
      renderDashboardInboxTable();
      notificationsController.observe(state.matters);
    });
    repositoryController.loadGmailStatus();
  }
  if (tabName === "playbook") {
    activateAdminSurface("playbook");
    activateAdminSection("playbook");
  }
  if (tabName === "admin") {
    activateAdminSurface("admin");
    if (!["ai", "health", "email", "personalisation", "drive"].includes(activeAdminSection())) {
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

function renderDashboardInboxTable() {
  if (!dashboardInboxTableBody) return;
  const inboxMatters = Array.isArray(state.matters)
    ? state.matters
      .filter((matter) => RepositoryModel.matterColumn(matter) === "gmail_demo")
      .slice()
      .sort(RepositoryModel.compareMatterRecency)
    : [];
  const mattersByColumn = new Map(RepositoryModel.BOARD_COLUMNS.map((column) => [column.id, 0]));
  if (Array.isArray(state.matters)) {
    state.matters.forEach((matter) => {
      const column = RepositoryModel.matterColumn(matter);
      mattersByColumn.set(column, (mattersByColumn.get(column) || 0) + 1);
    });
  }
  document.querySelectorAll("[data-dashboard-repository-count]").forEach((count) => {
    count.textContent = String(mattersByColumn.get(count.dataset.dashboardRepositoryCount) || 0);
  });
  if (dashboardInboxCount) {
    const noun = inboxMatters.length === 1 ? "document" : "documents";
    dashboardInboxCount.textContent = `${inboxMatters.length} ${noun}`;
  }
  if (dashboardInboxEmpty) dashboardInboxEmpty.hidden = inboxMatters.length > 0;
  dashboardInboxTableBody.innerHTML = inboxMatters.map((matter) => {
    const id = htmlEscape(String(matter?.id || ""));
    const title = htmlEscape(RepositoryModel.matterSubject(matter));
    const counterparty = htmlEscape(matter?.counterparty || matter?.counterparty_name || "Unknown counterparty");
    const sender = htmlEscape(RepositoryModel.matterSender(matter));
    const rawDate = matter?.received_at || matter?.imported_at || matter?.created_at || matter?.updated_at || "";
    const date = rawDate ? RepositoryModel.formatMatterDate(rawDate) : "";
    return (
      `<tr>` +
      `<td>` +
      `<span class="dashboard-inbox-document">` +
      `<span class="dashboard-inbox-document-icon" aria-hidden="true">` +
      `<svg viewBox="0 0 24 24" focusable="false"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/><path d="M9 13h6"/><path d="M9 17h4"/></svg>` +
      `</span>` +
      `<span>${title}</span>` +
      `</span>` +
      `</td>` +
      `<td>${counterparty}</td>` +
      `<td>${sender}</td>` +
      `<td><span class="dashboard-inbox-date">${htmlEscape(date || "—")}</span></td>` +
      `<td><button class="dashboard-inbox-action" type="button" data-dashboard-inbox-open="${id}">Open review</button></td>` +
      `</tr>`
    );
  }).join("");
}

async function loadDashboardAiHealth() {
  if (!dashboardHealthItems.length) return;
  renderDashboardHealth("ai", {
    tone: "checking",
  });
  try {
    const response = await fetch("/api/ai/settings");
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, "AI settings could not load");
    renderDashboardAiHealth(payload);
  } catch (error) {
    renderDashboardHealth("ai", {
      tone: "blocked",
    });
  }
}

async function loadPersonalisationSettings() {
  try {
    const response = await fetch("/api/admin/personalisation-settings");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) return;
    state.personalisationSettings = normalizePersonalisationSettings(
      payload.personalisation || payload.personalization || payload.settings || {},
    );
  } catch (error) {
    state.personalisationSettings = null;
  }
}

function normalizePersonalisationSettings(settings = {}) {
  if (!settings || typeof settings !== "object") return null;
  return {
    sign_off: String(settings.sign_off ?? settings.signOff ?? "").trim(),
    signature: String(settings.signature ?? "").trim(),
    signature_block: String(settings.signature_block ?? settings.signatureBlock ?? "").trim(),
  };
}

// POST for one matter's grounded AI summary. Returns {ok, payload} so the
// dashboard-search controller can render the summary or the friendly error inline;
// it never throws on a non-OK response (degradation is a normal, expected path).
// The endpoint URL comes from the bridged pure helper so the path stays single-
// source with the .mjs the tests exercise.
async function summarizeMatterById(matterId) {
  const lib = window.DashboardSearch || {};
  const url = typeof lib.summaryEndpoint === "function"
    ? lib.summaryEndpoint(matterId)
    : `/api/matters/${encodeURIComponent(String(matterId || ""))}/summary`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (parseError) {
      payload = {};
    }
    return { ok: response.ok, payload };
  } catch (networkError) {
    // A transport failure is just another "unavailable" — let the controller show
    // the friendly message rather than surfacing the raw error.
    return { ok: false, payload: {} };
  }
}

// POST a natural-language query to the v2 search-intent endpoint and return
// {ok, payload}. The backend translates the query into a VALIDATED structured
// filter spec (or a {fallback:true} signal on AI degradation) — it never returns
// matters. The controller applies the spec to the real state.matters itself; on a
// non-OK response / fallback / network failure it falls back to v1 keyword search.
// Never throws (degradation is a normal, expected path).
async function searchIntentForQuery(query) {
  const lib = window.DashboardSearch || {};
  const url = typeof lib.SEARCH_INTENT_ENDPOINT === "string"
    ? lib.SEARCH_INTENT_ENDPOINT
    : "/api/dashboard/search-intent";
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: String(query == null ? "" : query) }),
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (parseError) {
      payload = {};
    }
    return { ok: response.ok, payload };
  } catch (networkError) {
    // A transport failure just means "use the v1 keyword fallback" — never surface
    // the raw error.
    return { ok: false, payload: {} };
  }
}

async function dashboardAssistantForQuery(query) {
  const lib = window.DashboardSearch || {};
  const url = typeof lib.DASHBOARD_ASSISTANT_ENDPOINT === "string"
    ? lib.DASHBOARD_ASSISTANT_ENDPOINT
    : "/api/dashboard/assistant";
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: String(query == null ? "" : query) }),
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (parseError) {
      payload = {};
    }
    return { ok: response.ok, payload, status: response.status };
  } catch (networkError) {
    return { ok: false, payload: {}, status: 0 };
  }
}

async function confirmDashboardAssistantAction(action = {}) {
  const actionName = String(action.action || "").trim();
  if (actionName === "open_generator") {
    const prompt = String(
      action.prompt
      || action.generator?.prefill?.prompt
      || document.querySelector("#dashboardSearchInput")?.value
      || "",
    ).trim();
    const applyDashboardAssistantPrefill = ({ dispatch = true } = {}) => {
      if (!prompt) return;
      const purposeInput = document.querySelector("#draftIntakeProjectPurpose");
      const notesInput = document.querySelector("#draftIntakeNotes");
      if (dispatch) {
        setDraftInputValue(purposeInput, prompt);
        setDraftInputValue(
          notesInput,
          "Started from Dashboard Assistant. Review all details before generating.",
          { onlyIfEmpty: true },
        );
        return;
      }
      if (purposeInput) purposeInput.value = prompt;
      if (notesInput && !String(notesInput.value || "").trim()) {
        notesInput.value = "Started from Dashboard Assistant. Review all details before generating.";
      }
    };
    activateTab("generator");
    applyDashboardAssistantPrefill({ dispatch: false });
    await draftIntakeController.activate();
    applyDashboardAssistantPrefill();
    document.querySelector("#draftIntakeCounterpartyName")?.focus();
    return;
  }
  const targetTab = String(action.target?.tab || "").trim();
  const allowedTabs = new Set(["repository", "playbook", "admin"]);
  if (allowedTabs.has(targetTab)) {
    activateTab(targetTab);
  }
}

function setDraftInputValue(input, value, { onlyIfEmpty = false } = {}) {
  if (!input || (onlyIfEmpty && String(input.value || "").trim())) return;
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function renderDashboardAiHealth(payload = {}) {
  const aiStatus = payload.ai_review || {};
  const runtimeStatus = payload.active_review_engine || {};
  const activeEngine = String(runtimeStatus.active_engine || "ai_first");
  const enabled = aiStatus.enabled === true;
  const keyConfigured = aiStatus.api_key_configured === true;

  if (activeEngine === "deterministic") {
    renderDashboardHealth("ai", {
      tone: "warning",
    });
    return;
  }
  if (!enabled) {
    renderDashboardHealth("ai", {
      tone: "warning",
    });
    return;
  }
  if (!keyConfigured) {
    renderDashboardHealth("ai", {
      tone: "blocked",
    });
    return;
  }
  renderDashboardHealth("ai", {
    tone: "ready",
  });
}

function renderDashboardEmailHealth(gmailStatus = null) {
  if (!dashboardHealthItems.length) return;
  if (!gmailStatus) {
    renderDashboardHealth("email", {
      tone: "checking",
    });
    return;
  }
  const inbound = gmailStatus.inbound || {};
  const outbound = gmailStatus.outbound || {};
  const inboundReady = inbound.ready === true;
  const outboundReady = outbound.ready === true;
  if (inboundReady && outboundReady) {
    renderDashboardHealth("email", {
      tone: "ready",
    });
    return;
  }
  if (inboundReady || outboundReady) {
    renderDashboardHealth("email", {
      tone: "warning",
    });
    return;
  }
  renderDashboardHealth("email", {
    tone: "blocked",
  });
}

async function loadDashboardDriveHealth() {
  if (!dashboardHealthItems.length) return;
  renderDashboardHealth("drive", { tone: "checking" });
  try {
    const response = await fetch("/api/drive/status");
    const payload = await response.json();
    if (!response.ok) throw new Error("Drive status could not load");
    renderDashboardDriveHealth(payload);
  } catch (error) {
    renderDashboardHealth("drive", { tone: "blocked" });
  }
}

function renderDashboardDriveHealth(status = {}) {
  if (!dashboardHealthItems.length) return;
  // Drive is an OPTIONAL export integration: connected -> ready (green); not
  // connected -> warning (amber, "available but not set up") rather than blocked,
  // since an unconfigured optional feature is not an error.
  renderDashboardHealth("drive", { tone: status.connected === true ? "ready" : "warning" });
}

function renderDashboardHealth(kind, { tone }) {
  const item = document.querySelector(`[data-dashboard-health="${kind}"]`);
  if (!item) return;
  const effectiveTone = ["ready", "warning", "blocked", "checking"].includes(tone) ? tone : "checking";
  item.classList.remove("ready", "warning", "blocked", "checking");
  item.classList.add(effectiveTone);
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
  if (sectionName === "health") {
    adminHealthController.load();
  }
  if (sectionName === "drive") {
    adminDriveController.load();
  }
  if (sectionName === "personalisation") {
    adminPersonalisationController.load();
  }
}

function normalizeReviewInspectorView(viewName) {
  return REVIEW_INSPECTOR_VIEWS.includes(viewName) ? viewName : "clause";
}

function setReviewInspectorView(viewName) {
  state.reviewInspectorView = normalizeReviewInspectorView(viewName);
  updateReviewInspectorTabs();
  renderStudioDetail();
}

function updateReviewInspectorTabs() {
  const selectedView = normalizeReviewInspectorView(state.reviewInspectorView);
  reviewInspectorButtons.forEach((button) => {
    const active = button.dataset.reviewInspector === selectedView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  if (studioInspectorTitle) {
    studioInspectorTitle.textContent = REVIEW_INSPECTOR_TITLES[selectedView] || REVIEW_INSPECTOR_TITLES.clause;
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
