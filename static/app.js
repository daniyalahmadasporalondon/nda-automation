const studioDocTitle = document.querySelector("#studioDocTitle");
const studioNdaText = document.querySelector("#studioNdaText");
const studioDocumentRender = document.querySelector("#studioDocumentRender");
const studioFileMeta = document.querySelector("#studioFileMeta");
const studioCounterpartyMeta = document.querySelector("#studioCounterpartyMeta");
const studioCounterpartyField = document.querySelector("#studioCounterpartyField");
const studioCounterpartyName = document.querySelector("#studioCounterpartyName");
const studioCounterpartyConfidence = document.querySelector("#studioCounterpartyConfidence");
const studioCounterpartyUnconfirmed = document.querySelector("#studioCounterpartyUnconfirmed");
const studioCounterpartyConfirmButton = document.querySelector("#studioCounterpartyConfirmButton");
const studioCounterpartyEditButton = document.querySelector("#studioCounterpartyEditButton");
const studioCounterpartyEditForm = document.querySelector("#studioCounterpartyEditForm");
const studioCounterpartyEditInput = document.querySelector("#studioCounterpartyEditInput");
const studioCounterpartyEditCancel = document.querySelector("#studioCounterpartyEditCancel");
const studioCounterpartyStatus = document.querySelector("#studioCounterpartyStatus");
const studioSaveDraftButton = document.querySelector("#studioSaveDraftButton");
const studioDiscardDraftButton = document.querySelector("#studioDiscardDraftButton");
const studioExportButton = document.querySelector("#studioExportButton");
const studioSendButton = document.querySelector("#studioSendButton");
const studioReviewedButton = document.querySelector("#studioReviewedButton");
// Understated, confirm-gated "Mark as executed" — the SECONDARY path for an NDA
// signed OUTSIDE our DocuSign flow (paper / uploaded). DocuSign completion is the
// normal automatic route to executed; this is the manual exception handle.
const studioMarkExecutedButton = document.querySelector("#studioMarkExecutedButton");
// On-demand "Refresh status" — re-syncs the live DocuSign envelope status so a
// matter whose completion webhook was MISSED self-heals to executed. Shown only
// while a sent, non-terminal envelope exists; mirrors the mark-executed gate +
// placement. The normal route is the webhook; this is the manual recovery handle.
const studioRefreshStatusButton = document.querySelector("#studioRefreshStatusButton");
// Approve Review + Send for Signature moved out of the header into the Overview
// footer (static/js/overview/footer.js); the header no longer carries those
// buttons. The footer reads the gate helpers (approveBlockReasons /
// isMatterApproved) and opens the DocuSign composer directly
// (window.openReviewDocuSignComposer), so no header twin button is needed.
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
const studioReviewStaleIndicator = document.querySelector("#studioReviewStaleIndicator");
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
// "overview" is FIRST so it is the default sub-tab (normalizeReviewInspectorView
// falls back to REVIEW_INSPECTOR_VIEWS[0]); it composes the Facts/Roster/Footer
// at-a-glance pane.
const REVIEW_INSPECTOR_VIEWS = ["overview", "clause", "structure"];
const REVIEW_INSPECTOR_TITLES = {
  overview: "Overview",
  clause: "Selected Clause",
  structure: "Contract Structure",
};
let pendingReviewSendMatterId = null;
let authSessionController;
let adminAiController;
let adminModelsController;
let adminHealthController;
let adminIntegrationsController;
let adminDriveController;
let adminDocuSignController;
let adminAccessController;
let adminEntitiesController;
let adminPersonalisationController;
let adminGlobalPersonalisationController;
let docusignSendController;

const repositoryController = createRepositoryController({
  state,
  gmailDemoMatterList: document.querySelector("#gmailDemoMatterList"),
  repositorySearchInput: document.querySelector("#repositorySearchInput"),
  repositoryMatterPanel: document.querySelector("#repositoryMatterPanel"),
  downloadBlob,
  downloadFilename,
  downloadUrl,
  loadMatterIntoReview,
  prepareMatterReviewLoad,
  redlineDownloadFilename,
  showMatterReviewLoadError,
  reviewErrorFromPayload,
});
// Corpus tab (read-only filing-cabinet view). Fetches GET /api/corpus and paints
// the Counterparty -> Contract -> artifact tree. "Open matter" reuses the
// Repository open-matter flow then surfaces the Repository tab, mirroring the
// dashboard-search openMatter seam below.
const corpusController = createCorpusController({
  panel: document.querySelector("#corpusView"),
  listNode: document.querySelector("#corpusGroups"),
  emptyNode: document.querySelector("#corpusEmpty"),
  noResultsNode: document.querySelector("#corpusNoResults"),
  statusNode: document.querySelector("#corpusDriveStatus"),
  summaryNode: document.querySelector("#corpusSummary"),
  refreshButton: document.querySelector("#corpusRefreshButton"),
  searchForm: document.querySelector("#corpusSearchForm"),
  searchInput: document.querySelector("#corpusSearchInput"),
  tokenField: document.querySelector("#corpusTokenField"),
  searchClear: document.querySelector("#corpusSearchClear"),
  facetRail: document.querySelector("#corpusFacetRail"),
  groupToggle: document.querySelector("#corpusGroupToggle"),
  executedToggle: document.querySelector("#corpusExecutedToggle"),
  openMatter: (matterId) => {
    repositoryController.openMatter(matterId);
    activateTab("repository");
  },
});
const corpusNoResultsReset = document.querySelector("#corpusNoResultsReset");
if (corpusNoResultsReset) {
  corpusNoResultsReset.addEventListener("click", () => corpusController.resetFilters());
}
// The dashboard search bar searches the FULL CORPUS (app-state + Drive-reconciled,
// ~95 matters), not just the ~20 app-state matters. ensureSearchCorpus fetches
// GET /api/corpus (reusing the shipped corpus fetch + its per-owner TTL cache) and
// flattens groups[].matters[] into the flat, matcher-shaped list the search controller
// filters client-side. On a failure (e.g. Drive disconnected) the corpus payload still
// carries the app-state matters, so search degrades gracefully and never crashes.
state.corpusSearchMatters = [];
function dashboardSearchLib() {
  return window.DashboardSearch || {};
}
function ensureSearchCorpus() {
  const fetchCorpus = typeof CorpusView !== "undefined" && CorpusView && CorpusView.fetchCorpus;
  if (typeof fetchCorpus !== "function") {
    return Promise.resolve(state.corpusSearchMatters);
  }
  return fetchCorpus()
    .then((payload) => {
      const flatten = dashboardSearchLib().flattenCorpusPayload;
      state.corpusSearchMatters = typeof flatten === "function" ? flatten(payload) : [];
      return state.corpusSearchMatters;
    })
    .catch(() => {
      // Keep the last good corpus list; the controller renders against it (possibly
      // empty) rather than throwing. Search must always be graceful.
      return state.corpusSearchMatters;
    });
}
// Load the Playbook-derived enum allowlists (governing-law option ids + clause ids)
// from the server and feed them into the smart-search validators, so the FE re-
// validation uses the SAME Playbook source the backend does and never silently drops
// a legitimately-approved 6th law / new clause. Best-effort: on any failure the
// validators keep their safe seed sets, so search still works (it just can't honor a
// brand-new approved value until the fetch succeeds). Runs once at bootstrap.
function loadDashboardSearchConfig() {
  const lib = dashboardSearchLib();
  const apply = lib.setFilterSpecAllowlists;
  const endpoint = lib.SEARCH_CONFIG_ENDPOINT || "/api/dashboard/search-config";
  if (typeof apply !== "function") return Promise.resolve();
  return fetch(endpoint, { headers: { Accept: "application/json" } })
    .then((response) => (response.ok ? response.json() : null))
    .then((config) => {
      if (config && typeof config === "object") apply(config);
    })
    .catch(() => {
      // Keep the seed allowlists; search must always be graceful.
    });
}
// Open a search result respecting CORPUS PROVENANCE: an app/both matter opens in-app
// via the repository flow; a Drive-only matter (no app-state) links out to its Drive
// folder in a new tab (no in-app deep link, no Summarize). Falls back to the in-app
// flow for a bare matter id (e.g. the legacy app-state path).
function openCorpusSearchResult(matterId) {
  const match = Array.isArray(state.corpusSearchMatters)
    ? state.corpusSearchMatters.find((candidate) => String(candidate?.id) === String(matterId))
    : null;
  if (match && match.in_app !== true && match.open_in_drive_url) {
    window.open(match.open_in_drive_url, "_blank", "noopener");
    return;
  }
  repositoryController.openMatter(matterId);
  activateTab("repository");
}
// Dashboard smart-search. Searches the full corpus (see ensureSearchCorpus) and
// reuses the provenance-aware open above so a result click opens an app matter in-app
// and a Drive-only matter out to Drive.
const dashboardSearchController = createDashboardSearchController({
  root: document.querySelector("[data-dashboard-search]"),
  input: document.querySelector("#dashboardSearchInput"),
  form: document.querySelector("#dashboardSearchForm"),
  chipList: document.querySelector("#dashboardSearchChips"),
  resultsList: document.querySelector("#dashboardSearchResults"),
  resultsStatus: document.querySelector("#dashboardSearchResultsStatus"),
  interpretedLine: document.querySelector("#dashboardSearchInterpreted"),
  getMatters: () => state.corpusSearchMatters,
  ensureMatters: () => ensureSearchCorpus(),
  openMatter: (matterId) => openCorpusSearchResult(matterId),
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
    // 45s cap so a stalled-but-open connection can't hang this promise forever —
    // the 15s poll's in-flight guard holds until it settles. The abort rejects,
    // the poll's catch swallows it, and the next tick retries. Browsers without
    // AbortSignal.timeout keep the old unbounded fetch (the poll watchdog still
    // bounds the guard).
    const signal = (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function")
      ? AbortSignal.timeout(45_000)
      : undefined;
    const response = await fetch("/api/matters", { signal });
    if (!response.ok) return [];
    const payload = await response.json();
    return Array.isArray(payload.matters) ? payload.matters : [];
  },
});
// Wire global session-expiry handling: a 401 from any request surfaces a clean
// "session expired — sign in again" toast (via the existing notification system)
// instead of a cryptic JSON parse error. Kept to a single line so this file stays
// merge-friendly with other in-flight branches.
globalThis.AuthExpired?.register?.({ notify: notificationsController.notify });
// Bridge the shared success-toast onto window so modules that aren't handed the
// controller directly (e.g. playbook-view.js's nested Entities & Courts save) can
// flash a transient green success toast through the ONE notification center,
// rather than instantiating a second toaster. Guarded at every call site.
window.notifySuccess = (title, subtitle) => notificationsController.notifySuccess(title, subtitle);
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
  termDecrementButton: document.querySelector("#draftIntakeTermDecrement"),
  termIncrementButton: document.querySelector("#draftIntakeTermIncrement"),
  termHintNode: document.querySelector("#draftIntakeTermHint"),
  termUnitNode: document.querySelector("#draftIntakeTermUnit"),
  projectPurposeInput: document.querySelector("#draftIntakeProjectPurpose"),
  // LAW + COURT LOCKED TO ENTITY: read-only display nodes (a <p>), not an editable
  // picker. The law and court are derived from the picked signing entity.
  governingLawNode: document.querySelector("#draftIntakeGoverningLaw"),
  forumNode: document.querySelector("#draftIntakeForum"),
  lawStatusNode: document.querySelector("#draftIntakeLawStatus"),
  statusNode: document.querySelector("#draftIntakeStatus"),
  clearButton: document.querySelector("#draftIntakeClearButton"),
  newNdaButton: document.querySelector("#draftIntakeNewNdaButton"),
  generateButton: document.querySelector("#draftIntakeGenerateButton"),
  sideEntityNode: document.querySelector("#draftIntakeSideEntity"),
  sideLawNode: document.querySelector("#draftIntakeSideLaw"),
  sideTypeNode: document.querySelector("#draftIntakeSideType"),
  previewNode: document.querySelector("#draftIntakePreview"),
  // First-run onboarding panel + its dismiss control (top of the Generator view).
  onboardingNode: document.querySelector("[data-generator-onboarding]"),
  onboardingDismissButton: document.querySelector("[data-generator-onboarding-dismiss]"),
  counterpartyIncorporationInput: document.querySelector("#draftIntakeCounterpartyIncorporation"),
  counterpartyAddressInput: document.querySelector("#draftIntakeCounterpartyAddress"),
  businessDescriptionInput: document.querySelector("#draftIntakeBusinessDescription"),
  downloadButton: document.querySelector("#draftIntakeDownloadButton"),
  sendButton: document.querySelector("#draftIntakeSendButton"),
  onGenerate: generateNdaFromDraft,
  onDownloadGenerated: downloadGeneratedNda,
  onSendGenerated: sendGeneratedNda,
  onEditGenerated: editGeneratedNda,
  // Fire the transient green SUCCESS toast on a finished generation (this replaced
  // the persistent inline green status text). Reuses the one in-app notification
  // toaster — the same machinery as the inbound-NDA / review-failed toasts.
  notifyGenerated: (message) => notificationsController.notifySuccess(message),
  // Reveal/hide the "Send for Signature" CTA in step with the staged generation:
  // a saved generated matter -> show + prime the composer's matter; null -> hide.
  onStagedActionsChanged: setGeneratorSignatureMatter,
});
adminAiController = createAdminAiController({
  state,
  aiCard: document.querySelector("#adminAiCard"),
  aiKeyForm: document.querySelector("#adminAiKeyForm"),
  aiApiKeyInput: document.querySelector("#adminAiApiKeyInput"),
  aiClearKeyButton: document.querySelector("#adminAiClearKeyButton"),
  aiEnabledToggle: document.querySelector("#adminAiEnabledToggle"),
  aiFacts: document.querySelector("#adminAiFacts"),
  aiOverall: document.querySelector("#adminAiOverall"),
  aiRefreshButton: document.querySelector("#adminAiRefreshButton"),
  reviewErrorFromPayload,
});
adminModelsController = createAdminModelsController({
  card: document.querySelector("#adminModelsCard"),
  overall: document.querySelector("#adminModelsOverall"),
  refreshButton: document.querySelector("#adminModelsRefreshButton"),
  rowsList: document.querySelector("#adminModelsList"),
  saveButton: document.querySelector("#adminModelsSaveButton"),
  message: document.querySelector("#adminModelsMessage"),
  warningNote: document.querySelector("#adminModelsWarning"),
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
  costTotal: document.querySelector("#adminAiCostTotal"),
  costTokens: document.querySelector("#adminAiCostTokens"),
  costFeatures: document.querySelector("#adminAiCostFeatures"),
  costCaveat: document.querySelector("#adminAiCostCaveat"),
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
  gmailImportLimitForm: document.querySelector("#adminGmailImportLimitForm"),
  gmailImportLimitInput: document.querySelector("#adminGmailImportLimitInput"),
  gmailImportLimitSaveButton: document.querySelector("#adminGmailImportLimitSaveButton"),
  gmailSyncWindowForm: document.querySelector("#adminGmailSyncWindowForm"),
  gmailSyncWindowInput: document.querySelector("#adminGmailSyncWindowInput"),
  gmailSyncWindowSaveButton: document.querySelector("#adminGmailSyncWindowSaveButton"),
  gmailIntakeForm: document.querySelector("#adminGmailIntakeForm"),
  gmailIntakePanels: document.querySelector("#adminGmailIntakePanels"),
  gmailIntakeRuleInput: document.querySelector("#adminGmailIntakeRuleInput"),
  gmailIntakeSaveButton: document.querySelector("#adminGmailIntakeSaveButton"),
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
  drivePauseToggle: document.querySelector("#adminDrivePauseToggle"),
  driveFolderForm: document.querySelector("#adminDriveFolderForm"),
  driveFolderIdInput: document.querySelector("#adminDriveFolderIdInput"),
  driveFolderSaveButton: document.querySelector("#adminDriveFolderSaveButton"),
  // Name-first display layer + Edit-ID toggle (all optional).
  driveFolderDisplay: document.querySelector("#adminDriveFolderDisplay"),
  driveFolderDisplayName: document.querySelector("#adminDriveFolderDisplayName"),
  driveFolderDisplayId: document.querySelector("#adminDriveFolderDisplayId"),
  driveFolderIdRow: document.querySelector("#adminDriveFolderIdRow"),
  driveFolderEditIdButton: document.querySelector("#adminDriveFolderEditIdButton"),
  driveBrowseButton: document.querySelector("#adminDriveBrowseButton"),
  driveBrowseButtonAlt: document.querySelector("#adminDriveBrowseButtonAlt"),
  drivePickerBackdrop: document.querySelector("#adminDriverPickerBackdrop"),
  drivePickerClose: document.querySelector("#adminDrivePickerClose"),
  drivePickerCancel: document.querySelector("#adminDrivePickerCancel"),
  drivePickerSelect: document.querySelector("#adminDrivePickerSelect"),
  drivePickerList: document.querySelector("#adminDrivePickerList"),
  drivePickerBreadcrumb: document.querySelector("#adminDrivePickerBreadcrumb"),
  drivePickerBack: document.querySelector("#adminDrivePickerBack"),
  drivePickerStatus: document.querySelector("#adminDrivePickerStatus"),
  drivePickerSelection: document.querySelector("#adminDrivePickerSelection"),
  // "+ New folder" controls inside the picker (all optional).
  drivePickerNewToggle: document.querySelector("#adminDrivePickerNewToggle"),
  drivePickerNewRow: document.querySelector("#adminDrivePickerNewRow"),
  drivePickerNewInput: document.querySelector("#adminDrivePickerNewInput"),
  drivePickerNewCreate: document.querySelector("#adminDrivePickerNewCreate"),
  drivePickerNewCancel: document.querySelector("#adminDrivePickerNewCancel"),
  drivePickerNewError: document.querySelector("#adminDrivePickerNewError"),
  reviewErrorFromPayload,
});
adminDocuSignController = createAdminDocuSignController({
  state,
  docusignCard: document.querySelector("#adminDocuSignCard"),
  docusignFacts: document.querySelector("#adminDocuSignFacts"),
  docusignOverall: document.querySelector("#adminDocuSignOverall"),
  docusignRefreshButton: document.querySelector("#adminDocuSignRefreshButton"),
  docusignConnectPanel: document.querySelector("#adminDocuSignConnectPanel"),
  docusignConnectToggle: document.querySelector("#adminDocuSignConnectToggle"),
  reviewErrorFromPayload,
});
adminAccessController = createAdminAccessController({
  card: document.querySelector("#adminAccessCard"),
  overall: document.querySelector("#adminAccessOverall"),
  refreshButton: document.querySelector("#adminAccessRefreshButton"),
  addForm: document.querySelector("#adminAccessAddForm"),
  emailInput: document.querySelector("#adminAccessEmailInput"),
  addButton: document.querySelector("#adminAccessAddButton"),
  message: document.querySelector("#adminAccessMessage"),
  envRootsList: document.querySelector("#adminAccessEnvRoots"),
  persistedList: document.querySelector("#adminAccessPersisted"),
  reviewErrorFromPayload,
});
// The signing-entity registry now lives INSIDE the Playbook editor as its
// "Entities" section (Clauses | Entities switcher), not in the Admin area. The
// controller logic / data layer / save+validation+forum-reconcile contract is
// unchanged — only the host elements moved.
adminEntitiesController = createAdminEntitiesController({
  panel: document.querySelector('[data-playbook-panel="entities"]'),
  list: document.querySelector("#playbookEntitiesList"),
  message: document.querySelector("#playbookEntitiesMessage"),
  refreshButton: document.querySelector("#playbookEntitiesRefreshButton"),
  addButton: document.querySelector("#playbookEntitiesAddButton"),
  saveButton: document.querySelector("#playbookEntitiesSaveButton"),
  cardTemplate: document.querySelector("#adminEntityCardTemplate"),
  addressTemplate: document.querySelector("#adminEntityAddressTemplate"),
  // Fire the transient green SUCCESS toast on a finished registry save (this
  // replaced the lingering inline green "Registry saved." text). Reuses the one
  // in-app notification toaster — same machinery as the generate/inbound toasts.
  notifySuccess: (title, subtitle) => notificationsController.notifySuccess(title, subtitle),
});
// SELF-SERVE: every authenticated user (admin or not) edits their OWN
// signature here, through /api/me/personalisation-settings (no `endpoint` =
// the controller's per-user default). This replaces the old admin-only wiring
// that 403'd for non-admins with a dead "Administrator access is required".
adminPersonalisationController = createAdminPersonalisationController({
  card: document.querySelector("#adminPersonalisationCard"),
  form: document.querySelector("#adminPersonalisationForm"),
  signOffInput: document.querySelector("#adminSignOffInput"),
  signatureInput: document.querySelector("#adminSignatureInput"),
  signatureBlockInput: document.querySelector("#adminSignatureBlockInput"),
  shadowNote: document.querySelector("#adminPersonalisationShadowNote"),
  saveButton: document.querySelector("#adminPersonalisationSaveButton"),
  resetButton: document.querySelector("#adminPersonalisationResetButton"),
  overall: document.querySelector("#adminPersonalisationOverall"),
  message: document.querySelector("#adminPersonalisationMessage"),
  persistenceFact: document.querySelector('[data-admin-personalisation="persistence"]'),
  reviewErrorFromPayload,
  onSettingsLoaded: (settings) => {
    // The caller's resolved signature is what the outbound-email defaults use.
    state.personalisationSettings = normalizePersonalisationSettings(settings);
  },
});
// ADMIN-ONLY: the deployment/global default that users inherit when they have
// no personal override. Wired to /api/admin/personalisation-settings and
// self-hides (onUnavailable) for non-admins — never a dead-end.
adminGlobalPersonalisationController = createAdminPersonalisationController({
  endpoint: "/api/admin/personalisation-settings",
  adminOnly: true,
  card: document.querySelector("#adminGlobalPersonalisationCard"),
  form: document.querySelector("#adminGlobalPersonalisationForm"),
  signOffInput: document.querySelector("#adminGlobalSignOffInput"),
  signatureInput: document.querySelector("#adminGlobalSignatureInput"),
  signatureBlockInput: document.querySelector("#adminGlobalSignatureBlockInput"),
  shadowNote: document.querySelector("#adminGlobalPersonalisationShadowNote"),
  saveButton: document.querySelector("#adminGlobalPersonalisationSaveButton"),
  resetButton: document.querySelector("#adminGlobalPersonalisationResetButton"),
  overall: document.querySelector("#adminGlobalPersonalisationOverall"),
  message: document.querySelector("#adminGlobalPersonalisationMessage"),
  persistenceFact: document.querySelector('[data-admin-global-personalisation="persistence"]'),
  reviewErrorFromPayload,
  onUnavailable: () => {
    const section = document.querySelector("#adminGlobalPersonalisationSection");
    if (section) section.hidden = true;
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
  signOutModal: document.querySelector("#signOutModal"),
  signOutModalClose: document.querySelector("#signOutModalClose"),
  signOutModalStatus: document.querySelector("#signOutModalStatus"),
  signOutThisDeviceButton: document.querySelector("#signOutThisDeviceButton"),
  signOutAllDevicesButton: document.querySelector("#signOutAllDevicesButton"),
  signOutCancelButton: document.querySelector("#signOutCancelButton"),
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
// Overview tab: the FIRST inspector sub-tab. Composes the Facts / clause Roster /
// Footer at-a-glance pane into #studioDetailPanel and wires each component's
// callbacks to the existing review-workstation flows (clause select+jump,
// counterparty confirm/override, Approve, Send-for-signature, reviewed sign-off,
// AI refresh). The three component renderers (renderOverviewFacts / Roster /
// Footer) are bridged onto window by their own files, folded in by the integrator.
// The Fill (Aspora-entity) tool now lives INSIDE the merged Overview pane rather
// than in its own inspector tab. It paints into a persistent standalone <section>
// (created once here, never re-created) that the Overview controller relocates
// into the bottom of the merged pane on every render. Keeping a single stable
// element alive across re-renders preserves the Fill controller's bound handlers
// and per-candidate working state — its render() only touches this element's own
// innerHTML/querySelector, so it does not need to be document-attached to build.
const reviewFillSection = document.createElement("section");
reviewFillSection.className = "ov-section ov-section-fill";
const reviewOverviewController = createOverviewController({
  state,
  root: studioDetailPanel,
  // The merged pane folds the existing Fill/Aspora tool in below the Overview
  // summary. The Overview controller appends this persistent section, then asks
  // the Fill controller (untouched) to paint into it.
  fillSection: reviewFillSection,
  renderFill: () => {
    if (typeof reviewFillController !== "undefined"
      && reviewFillController
      && typeof reviewFillController.render === "function") {
      reviewFillController.render();
    }
  },
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
  // Render into the persistent merged-pane section (see reviewFillSection above)
  // instead of the whole inspector panel, so the Overview summary above it
  // survives. review-fill.js is UNCHANGED — it still only uses this root's own
  // innerHTML/querySelector. Export wiring (currentReviewFills) + the document
  // highlight hook are unaffected by where this element is mounted.
  root: reviewFillSection,
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
  notify: notificationsController.notify,
  // escapeHtml is resolved lazily inside the controller (via window.escapeHtml)
  // because it is bridged by a deferred module that runs after this load-time
  // construction; passing it here would capture an undefined reference.
  getSurfaceRoot: () => studioDocumentRender?.querySelector("[data-original-surface]") || null,
  // D8: PDF markup tools mount over the Original PDF page-images and their
  // marked-up download is a source-PDF-only endpoint (400s for DOCX). Gating on
  // `.id` alone offered the tools on EVERY matter — including DOCX ones — so this
  // now actually tests that the selected matter's source is a PDF.
  matterIsPdf: () => selectedMatterIsPdfSource(),
});

// True only when the selected matter's SOURCE document is a real PDF (not DOCX or
// other). Mirrors review-workstation-rendering's matterIsPdfSource() filename
// sniff so PDF-only affordances (markup tools, annotated-PDF recovery) agree on
// what "a PDF matter" is, with an inline fallback so the gate still holds if that
// global isn't present (e.g. an isolated test harness).
function selectedMatterIsPdfSource() {
  const matter = state.selectedMatter;
  if (!matter?.id) return false;
  if (typeof matterIsPdfSource === "function") return Boolean(matterIsPdfSource(matter));
  const filename = String(matter.source_filename || matter.attachment_filename || "").trim();
  return /\.pdf$/i.test(filename);
}

// "Send for signature" — the DocuSign e-signature action on a reviewed/approved
// matter. The chooser is a modal; the always-visible signature-status badge lives
// in the studio matter-actions group. There is NO header trigger button anymore —
// the Overview footer's "Send for signature" opens this composer directly via
// window.openReviewDocuSignComposer (exposed below). `triggerButton` is therefore
// null; syncTriggerButton still refreshes the header badge from matter state.
// The Aspora signatory name defaults to the personalisation signature ("Aspora
// Legal" fallback) and its email to the outbound Gmail account — both editable in
// the chooser.
docusignSendController = createDocuSignSendController({
  modalNode: document.querySelector("#docusignSendModal"),
  closeButton: document.querySelector("#docusignSendModalClose"),
  cancelButton: document.querySelector("#docusignSendCancelButton"),
  form: document.querySelector("#docusignSendForm"),
  signerRows: document.querySelector("#docusignSignerRows"),
  signingOrderControl: document.querySelector("#docusignSigningOrder"),
  statusNode: document.querySelector("#docusignSendStatus"),
  badgeNode: document.querySelector("#docusignSignatureBadge"),
  // The always-visible status badge in the studio matter-actions group, driven
  // alongside the in-modal badge so a reloaded matter shows its signature state
  // without opening the composer.
  headerBadgeNode: document.querySelector("#studioSignatureBadge"),
  envelopeNode: document.querySelector("#docusignEnvelopeId"),
  downloadSignedLink: document.querySelector("#docusignDownloadSignedLink"),
  submitButton: document.querySelector("#docusignSendSubmitButton"),
  // No header trigger button: the Overview footer drives the send.
  triggerButton: null,
  getMatter: () => state.selectedMatter || null,
  // Pre-review gate: nothing to send before the AI review runs. The footer applies
  // the same gate (sendDisabled pre-review); this guards the direct-open path too.
  // aiReviewRan() reads the ai_review_ran flag with a clause-presence fallback for
  // old payloads, matching the Approve gate.
  isTriggerEnabled: (matter) => (typeof aiReviewRan === "function" ? aiReviewRan(matter) : true),
  getAsporaSignatory: () => ({
    name: String(state.personalisationSettings?.signature || "").trim() || "Aspora Legal",
    email: String(state.gmailStatus?.outbound?.email || "").trim(),
  }),
  reviewErrorFromPayload,
  downloadUrl,
  onMatterUpdated: (matter) => {
    if (!matter?.id) return;
    state.selectedMatter = matter;
    // Reflect the new signature state on the header badge immediately.
    if (typeof syncDocuSignTriggerButton === "function") syncDocuSignTriggerButton();
  },
});

// The Overview footer's "Send for signature" opens the Review DocuSign composer
// through this global (it no longer has a header button to click). The footer
// already gates Send pre-review (sendDisabled), so a click here means the review
// has run and there is something to send.
if (typeof window !== "undefined") {
  window.openReviewDocuSignComposer = () => docusignSendController?.openComposer?.();
}

// Thin global hook the review-workstation render funnel calls whenever the
// selected matter/review state changes (see updateExportButtonState), so the
// "Send for signature" trigger + signature badge stay in sync without the
// rendering module importing the controller directly.
function syncDocuSignTriggerButton() {
  docusignSendController?.syncTriggerButton?.();
}

// The Generator's own "Send for Signature" composer. A SECOND instance of the
// same DocuSign send controller (model helpers + status states identical to the
// Review workstation's), bound to the generator-scoped modal nodes so the two
// never double-bind. Its matter is the last generated NDA — a matter-like view
// built from the generation result (id + counterparty + recipient) so
// DocuSignModel.defaultSigners resolves the counterparty (FIRST party) + the
// Aspora signatory exactly as the Review send does. Like a generated NDA, both
// parties sign: it is our paper sent out for execution.
let generatorSignatureMatter = null;
const generatorDocusignSendController = createDocuSignSendController({
  modalNode: document.querySelector("#generatorDocusignSendModal"),
  closeButton: document.querySelector("#generatorDocusignSendModalClose"),
  cancelButton: document.querySelector("#generatorDocusignSendCancelButton"),
  form: document.querySelector("#generatorDocusignSendForm"),
  signerRows: document.querySelector("#generatorDocusignSignerRows"),
  signingOrderControl: document.querySelector("#generatorDocusignSigningOrder"),
  statusNode: document.querySelector("#generatorDocusignSendStatus"),
  badgeNode: document.querySelector("#generatorDocusignSignatureBadge"),
  // The always-visible badge in the Generator action row, driven alongside the
  // in-modal badge so the generated NDA shows its envelope state inline.
  headerBadgeNode: document.querySelector("#draftIntakeSignatureBadge"),
  envelopeNode: document.querySelector("#generatorDocusignEnvelopeId"),
  downloadSignedLink: document.querySelector("#generatorDocusignDownloadSignedLink"),
  submitButton: document.querySelector("#generatorDocusignSendSubmitButton"),
  triggerButton: document.querySelector("#draftIntakeSendForSignatureButton"),
  getMatter: () => generatorSignatureMatter,
  getAsporaSignatory: () => ({
    name: String(state.personalisationSettings?.signature || "").trim() || "Aspora Legal",
    email: String(state.gmailStatus?.outbound?.email || "").trim(),
  }),
  reviewErrorFromPayload,
  downloadUrl,
  onMatterUpdated: (matter) => {
    if (!matter?.id) return;
    // Keep the in-session generator matter in sync with the envelope state the
    // controller merged in (after send / on each status poll).
    generatorSignatureMatter = { ...generatorSignatureMatter, ...matter };
    syncGeneratorDocuSignTrigger();
  },
});

// Keep the Generator's "Send for Signature" CTA in sync with the last generated
// matter. Unlike the Review workstation's trigger (which the shared controller
// HIDES until a matter exists), the generator CTA is ALWAYS VISIBLE in the
// action row alongside Generate / Download / Send — it is DISABLED with a hint
// until a sendable generated matter exists, then ENABLES so a click opens the
// composer.
//
// We deliberately do NOT call the controller's syncTriggerButton here: that
// helper drives the Review trigger by writing triggerButton.textContent, which
// would wipe this button's SVG icon + <span> structure. Instead we drive the
// inline badge directly via renderSignatureState (the same badge the controller
// uses) and set the label inside our own <span>, leaving the icon intact.
function syncGeneratorDocuSignTrigger() {
  const matter = generatorSignatureMatter;
  // Drive the inline signature badge from the matter's envelope state (idle ->
  // hidden, sent/signed -> the tone-coloured pill), exactly as the Review path.
  generatorDocusignSendController?.renderSignatureState?.(matter || null);

  const triggerButton = document.querySelector("#draftIntakeSendForSignatureButton");
  if (!triggerButton) return;
  const label = triggerButton.querySelector("span");
  const sendable = Boolean(matter?.id);
  // Always in the row; enabled only once a sendable generated matter exists.
  triggerButton.hidden = false;
  triggerButton.disabled = !sendable;
  if (!sendable) {
    if (label) label.textContent = "Send for Signature";
    triggerButton.title = "Generate the NDA first";
    return;
  }
  // Sendable: reflect the envelope state on the label/hint without losing the
  // icon. An already-sent matter reads "Signature status" / "View signature".
  const model = (typeof window !== "undefined" && window.DocuSignModel) || null;
  const view = model?.matterSignatureView ? model.matterSignatureView(matter) : null;
  if (view?.sent) {
    if (label) label.textContent = view.completed ? "View Signature" : "Signature Status";
    triggerButton.title = view.label;
  } else {
    if (label) label.textContent = "Send for Signature";
    triggerButton.title = "Send this NDA for e-signature via DocuSign";
  }
}

// Record the just-generated NDA as the Generator send composer's matter, via the
// shared DocuSignModel.generatorSignatureMatter helper (single source of the
// matter-like shape defaultSigners reads: id + counterparty + recipient_email).
// Null when the generation has no saved matter id (the legacy in-memory blob
// path), so the CTA stays DISABLED — that NDA can't be sent for signature.
function setGeneratorSignatureMatter(generated) {
  const model = (typeof window !== "undefined" && window.DocuSignModel) || null;
  generatorSignatureMatter = model && typeof model.generatorSignatureMatter === "function"
    ? model.generatorSignatureMatter(generated)
    : (generated && generated.matterId
      ? {
        id: generated.matterId,
        counterparty: String(generated.counterpartyName || "").trim(),
        counterparty_name: String(generated.counterpartyName || "").trim(),
        recipient_email: String(generated.counterpartyEmail || "").trim(),
        document_title: String(generated.counterpartyName || "").trim(),
      }
      : null);
  syncGeneratorDocuSignTrigger();
}

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
setupCounterpartyConfirmation();
setActiveTab("dashboard");
setupDocumentViewModes();
setupReviewUndoControls();
if (typeof setupReviewFindReplace === "function") setupReviewFindReplace();

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
// Feed the Playbook-derived search allowlists into the smart-search validators so a
// newly-approved law/clause isn't dropped on FE re-validation, then refresh any
// active search so it re-applies with the live allowlists.
loadDashboardSearchConfig().then(() => {
  dashboardSearchController.refresh();
});
repositoryController.loadGmailStatus();
authSessionController.load();
adminAiController.load();
loadPersonalisationSettings();
loadDashboardAiHealth();
loadDashboardDriveHealth();
loadDashboardDocuSignHealth();
adminIntegrationsController.load();
// IN-FLIGHT POLL GUARD (large stores): on a multi-thousand-matter account one
// /api/matters response can take longer than the 15s poll interval. Without this
// guard each tick started ANOTHER full-list download; the overlapping multi-MB
// transfers saturated the browser's per-host connection pool (starving every
// other /api request) while the stale-response run-token discarded almost every
// body -- all cost, no render. One tick's fetch must fully settle before the next
// tick may start a new one; ticks that would overlap are simply skipped.
let matterPollInFlight = false;
let matterPollStartedAt = 0;
// WATCHDOG: the poll fetches carry a 45s AbortSignal.timeout, so they normally
// settle (and release the guard) well before this. If a fetch ever hangs WITHOUT
// aborting (no AbortSignal.timeout support, a black-holed request the browser
// never fails), force the guard open after 60s so polling resumes instead of
// dying until a page reload. A late stale response is defused by loadMatters'
// run-token, so force-releasing is safe.
const MATTER_POLL_WATCHDOG_MS = 60_000;
window.setInterval(() => {
  if (matterPollInFlight) {
    if (Date.now() - matterPollStartedAt < MATTER_POLL_WATCHDOG_MS) return;
    matterPollInFlight = false;
  }
  matterPollInFlight = true;
  matterPollStartedAt = Date.now();
  const releasePoll = () => { matterPollInFlight = false; };
  if (document.querySelector('[data-view="repository"]')?.classList.contains("active")) {
    Promise.resolve(repositoryController.loadMatters()).then(() => {
      renderDashboardInboxTable();
      notificationsController.observe(state.matters);
    }).catch(() => {}).finally(releasePoll);
    repositoryController.loadGmailStatus();
  } else {
    // On any non-Repository tab the board isn't refreshed, so the notifier polls
    // the matter list itself to keep new-inbound toasts flowing app-wide.
    Promise.resolve(notificationsController.poll()).catch(() => {}).finally(releasePoll);
  }
}, REPOSITORY_REFRESH_INTERVAL_MS);

// REFRESH-ON-VISIBLE: mobile browsers/webviews throttle or suspend background
// tabs, so the 15s poll can be minutes stale when the user switches back. One
// immediate matters+notifications refresh on return restores freshness; the
// same in-flight guard keeps it from stacking onto a poll already running.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || matterPollInFlight) return;
  matterPollInFlight = true;
  matterPollStartedAt = Date.now();
  Promise.resolve(repositoryController.loadMatters()).then(() => {
    renderDashboardInboxTable();
    notificationsController.observe(state.matters);
  }).catch(() => {}).finally(() => { matterPollInFlight = false; });
});

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

// Playbook surface switcher: the Playbook editor is the single home for the
// signing-entity registry (Registry > Signing Entities) and the clause rules
// (Clauses). The OLD top "Clauses | Entities" segmented toggle is gone — the LEFT
// SIDEBAR nav drives the swap now. The sidebar itself is persistent; only the
// right-hand main panel swaps between the clause editor ([data-playbook-panel=
// "clauses"]) and the entities registry ([data-playbook-panel="entities"]).
// Entities lazy-loads its registry on first open (mirrors the admin sections).
const playbookShell = document.querySelector(".playbook-shell[data-playbook-surface]");
const playbookMainPanels = document.querySelectorAll("[data-playbook-panel]");
const playbookEntitiesNavEntry = document.getElementById("playbookEntitiesNavEntry");
let entitiesLoadedOnce = false;
function activatePlaybookSection(sectionName) {
  const section = sectionName === "entities" ? "entities" : "clauses";
  // Track the active surface on the shell so CSS can style the nav active-state.
  if (playbookShell) playbookShell.dataset.playbookSurface = section;
  // The Registry nav entry carries the active highlight when entities is showing.
  if (playbookEntitiesNavEntry) {
    const entitiesActive = section === "entities";
    playbookEntitiesNavEntry.classList.toggle("active", entitiesActive);
    playbookEntitiesNavEntry.classList.toggle("selected", entitiesActive);
    playbookEntitiesNavEntry.setAttribute("aria-pressed", entitiesActive ? "true" : "false");
  }
  // Swap the right-hand main panels (the persistent sidebar never hides).
  playbookMainPanels.forEach((node) => {
    node.hidden = node.dataset.playbookPanel !== section;
  });
  if (section === "entities") {
    // Load on first activation, then serve the in-memory working copy.
    if (!entitiesLoadedOnce) {
      entitiesLoadedOnce = true;
      adminEntitiesController.load();
    }
  }
}
// The Registry nav entry swaps to the entities surface.
playbookEntitiesNavEntry?.addEventListener("click", () => activatePlaybookSection("entities"));
// Selecting a clause (or "+ Add Clause") swaps back to the clause editor. The
// clause rows are re-rendered by playbook-view.js, so delegate on the static list
// container rather than binding each row. The clause-selection handler inside
// playbook-view.js still drives WHICH clause is shown; this only flips the surface.
const playbookListNav = document.getElementById("playbookList");
playbookListNav?.addEventListener("click", () => activatePlaybookSection("clauses"));

// Onboarding empty-states (e.g. the fresh-user repository panel) route the user
// to the right tab via a [data-onboarding-goto] attribute. One delegated handler
// covers any such CTA, no matter which panel renders it.
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-onboarding-goto]");
  if (!trigger) return;
  const tab = trigger.dataset.onboardingGoto;
  if (!tab) return;
  event.preventDefault();
  activateTab(tab);
});

// The Admin setup checklist (admin-onboarding.js) routes to a SECTION within the
// already-open Admin console, not a top-level tab, so it carries its own goto
// attribute handled here. One delegated listener covers every checklist CTA.
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-admin-onboarding-goto]");
  if (!trigger) return;
  const section = trigger.dataset.adminOnboardingGoto;
  if (!section) return;
  event.preventDefault();
  activateAdminSurface("admin");
  activateAdminSection(section);
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
  // #31: carry the DocuSign "not connected" hint (and its connect link) through to
  // the caller's catch so a 409 needs_connect can render a GUIDING message + link
  // instead of a bare red error. Previously these fields were dropped here.
  if (payload?.needs_connect) {
    error.needsConnect = true;
    if (payload.connect_url) error.connectUrl = String(payload.connect_url);
  }
  // D4: carry the reviewed-PDF export RECOVERY pointer through to the caller's
  // catch. When a PDF-source redline can't be produced faithfully, the backend
  // (redline_export_service.PdfSourceRedlineUnavailableError) returns a 503 with a
  // `recovery.endpoint` template pointing at the source-PDF annotation export.
  // Previously this was dropped here, so the reviewer dead-ended on a red error
  // with no way to fetch the marked-up source PDF.
  if (payload?.recovery && typeof payload.recovery === "object" && payload.recovery.endpoint) {
    error.recovery = {
      endpoint: String(payload.recovery.endpoint),
      message: payload.recovery.message ? String(payload.recovery.message) : "",
      path: payload.recovery.path ? String(payload.recovery.path) : "",
    };
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

// D6: shared, ok-checked download for SERVER URLs. A bare `downloadUrl` (an
// <a download> click) saves WHATEVER the server returns under the requested
// filename — including a 4xx/5xx JSON error body — so a failed export lands on
// disk as a broken ".pdf"/".docx". This fetches the URL first, verifies
// response.ok AND that the body is a real file (not a JSON/HTML error page), and
// only then triggers the browser download from bytes in hand. On any failure it
// throws an Error carrying the server's message (via reviewErrorFromPayload) so
// the caller can surface it instead of saving garbage. blob:/data: URLs already
// hold in-hand bytes (no server round-trip, no error body) and bypass the check.
// Callers that today use `downloadUrl(serverUrl, ...)` can adopt this to get the
// same guard for free.
async function downloadUrlGuarded(url, filename) {
  if (typeof url === "string" && /^(blob:|data:)/i.test(url)) {
    downloadUrl(url, filename);
    return;
  }
  const response = await fetch(url, { headers: { Accept: "application/octet-stream" } });
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_parseError) {
      payload = null;
    }
    throw reviewErrorFromPayload(payload, `Download failed (${response.status}).`);
  }
  const contentType = String(response.headers.get("Content-Type") || "").toLowerCase();
  if (contentType.includes("application/json") || contentType.includes("text/html")) {
    // 200 OK but the body is a structured error / HTML page, not the file. Do NOT
    // save it under the requested name — read the message and surface it instead.
    let payload = null;
    try {
      payload = await response.json();
    } catch (_parseError) {
      payload = null;
    }
    throw reviewErrorFromPayload(payload, "The server did not return a downloadable file.");
  }
  const blob = await response.blob();
  // Prefer the server's Content-Disposition filename when it provides one.
  const serverFilename = downloadFilename(response);
  downloadBlob(blob, serverFilename || filename);
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
  const counterpartyName = payload?.counterparty?.name || "";
  const subject = payload?.counterparty?.name ? `NDA — ${payload.counterparty.name}` : "NDA";
  // Snapshot the generated matters that already exist BEFORE we fire the request.
  // Generation is synchronous and (with the AI clause adapter active) makes live
  // model calls, so a slow/cold host or proxy can make the POST time out AFTER the
  // backend already finished and SAVED the matter — the "spinner stuck, but the
  // NDA is in the Repository" failure. If that happens we self-heal by finding the
  // matter that appeared since this snapshot, so we never spin forever.
  const knownGeneratedIds = await generatedMatterIdSnapshot();
  try {
    const result = await api.generateNda(payload);
    if (result.kind === "blob") {
      const filename = result.filename || draftNdaDownloadFilename(payload);
      // Don't auto-download — stage the Download/Send actions instead.
      return {
        message: "NDA generated — use Download or Send.",
        toast: generatedToastSummary(null, counterpartyName),
        tone: "success",
        generated: { blob: result.blob, filename, counterpartyEmail, counterpartyName, subject },
      };
    }
    // JSON response (the real contract): the document was generated, a matter +
    // tracked artifact were created, and download_url points at the matter source.
    // We no longer auto-download — the staged Download/Send buttons drive that.
    const documentDownloads = result.document_downloads || null;
    const generatedDocx = window.DocumentDownloadMenu?.option(documentDownloads, "source", "docx");
    const generated = {
      documentDownloads,
      downloadUrl: result.download_url || null,
      filename: result.filename || generatedDocx?.filename || draftNdaDownloadFilename(payload),
      matterId: result.matter_id || null,
      pdfDownloadUrl: result.pdf_download_url || null,
      counterpartyEmail,
      // Prefer the server's manifest company name (the exact name written into the
      // document); fall back to the intake name. Carried so the Send-for-Signature
      // composer can label the counterparty signer without a second matter fetch.
      counterpartyName: String(result.manifest?.counterparty_name || counterpartyName || "").trim(),
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
      toast: generatedToastSummary(result.manifest, generated.counterpartyName || counterpartyName),
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
    // The POST timed out (or the connection dropped mid-flight). The backend may
    // still have generated and saved the NDA, so before reporting failure, poll
    // the repository for a generated matter that appeared since we started. If we
    // find it, the spinner clears into the normal generated-result state — the
    // known "backend finished but the request hung" case self-heals.
    if (error instanceof window.GenerationTimeoutError || error?.code === "generation_timeout") {
      const recovered = await recoverGeneratedMatter(knownGeneratedIds, {
        counterpartyEmail,
        counterpartyName,
        subject,
      });
      if (recovered) {
        const savedFor = counterpartyName ? ` for ${counterpartyName}` : "";
        return {
          message: `NDA generated and saved${savedFor} (the request was slow to respond, so it was recovered from the Repository). Use Download or Send.`,
          toast: generatedToastSummary(null, counterpartyName),
          tone: "success",
          generated: recovered,
        };
      }
      // No matter surfaced in the recovery window — surface a clear, non-spinning
      // error with retry guidance rather than leaving "Generating…" up forever.
      return {
        message:
          "Generation is taking longer than expected and the request timed out. The NDA may still appear in the Repository shortly — check there, or try Generate again.",
        tone: "error",
      };
    }
    throw error;
  }
}

// The ids of every generated matter currently in the repository. Used as the
// "before" baseline for the timeout self-heal so we can tell which matter the
// hung request created. Best-effort: a failed/empty fetch returns an empty set,
// which simply means the recovery treats any generated matter as a candidate.
async function generatedMatterIdSnapshot() {
  try {
    const matters = await generatorRepositoryApi().listMatters();
    return new Set(
      (Array.isArray(matters) ? matters : [])
        .filter(isGeneratedMatter)
        .map((matter) => String(matter.id)),
    );
  } catch (error) {
    return new Set();
  }
}

// True for a matter produced by the Generator (POST /api/generate-nda persists it
// with source_type/board_column "generated"). Either marker qualifies so a future
// board rename of one doesn't silently break recovery.
function isGeneratedMatter(matter) {
  if (!matter || matter.id === undefined || matter.id === null) return false;
  return matter.source_type === "generated" || matter.board_column === "generated";
}

// Polls the repository for a generated matter that wasn't present in `knownIds`,
// for a short window, and maps it into the `generated` handle the Download/Send/
// Edit actions consume. Returns null if none appears within the budget. The
// matters list is sorted newest-first by the backend, so the first unseen
// generated matter is the one this generation just created.
async function recoverGeneratedMatter(knownIds, { counterpartyEmail, counterpartyName, subject }) {
  const baseline = knownIds instanceof Set ? knownIds : new Set();
  const ATTEMPTS = 6;
  const INTERVAL_MS = 2000;
  for (let attempt = 0; attempt < ATTEMPTS; attempt += 1) {
    let matters;
    try {
      matters = await generatorRepositoryApi().listMatters();
    } catch (error) {
      matters = null;
    }
    const fresh = (Array.isArray(matters) ? matters : []).find(
      (matter) => isGeneratedMatter(matter) && !baseline.has(String(matter.id)),
    );
    if (fresh) {
      return generatedHandleFromMatter(fresh, { counterpartyEmail, counterpartyName, subject });
    }
    if (attempt < ATTEMPTS - 1) {
      await new Promise((resolve) => setTimeout(resolve, INTERVAL_MS));
    }
  }
  return null;
}

// Builds the same `generated` handle generateNdaFromDraft returns on the happy
// path, but from a public matter object (the /api/matters shape) instead of the
// generate response. The public matter carries document_downloads + id, which is
// everything Download/Send/Edit need; the counterparty name/email/subject come
// from the intake we already have. Mirrors derive_counterparty's fallback so the
// label is never blank.
function generatedHandleFromMatter(matter, { counterpartyEmail, counterpartyName, subject }) {
  const documentDownloads = matter.document_downloads || null;
  const generatedDocx = window.DocumentDownloadMenu?.option(documentDownloads, "source", "docx");
  const sourcePdf = window.DocumentDownloadMenu?.option(documentDownloads, "source", "pdf");
  const matterId = matter.id ? String(matter.id) : null;
  return {
    documentDownloads,
    downloadUrl: matterId ? `/api/matters/${encodeURIComponent(matterId)}/source` : null,
    filename: generatedDocx?.filename || draftNdaDownloadFilename({ counterparty: { name: counterpartyName } }),
    matterId,
    pdfDownloadUrl: sourcePdf?.download_url || null,
    counterpartyEmail,
    counterpartyName: String(matter.counterparty || counterpartyName || "").trim(),
    subject,
  };
}

// A repository API instance for the generator's timeout self-heal. The global
// RepositoryApi factory is the same one the repository controller uses; we build
// our own thin handle so the recovery doesn't reach into the controller's state.
let generatorRepositoryApiInstance = null;
function generatorRepositoryApi() {
  if (!generatorRepositoryApiInstance) {
    generatorRepositoryApiInstance = RepositoryApi.create({ reviewErrorFromPayload });
  }
  return generatorRepositoryApiInstance;
}

// Download the last generated NDA — from the in-memory blob or the saved matter
// source URL. Wired to the staged "Download" button in the generator.
async function downloadGeneratedNda(generated, { sourceButton } = {}) {
  if (!generated) return;
  const downloadMenu = window.DocumentDownloadMenu;
  if (sourceButton && downloadMenu) {
    const sourcePdf = downloadMenu.option(generated.documentDownloads, "source", "pdf")
      || (generated.pdfDownloadUrl ? {
        available: true,
        content_type: "application/pdf",
        download_url: generated.pdfDownloadUrl,
        filename: String(generated.filename || "nda.docx").replace(/\.docx$/i, ".pdf"),
        format: "pdf",
      } : null);
    downloadMenu.open(sourceButton, {
      label: "Download generated NDA",
      sections: [{
        label: "Generated document",
        choices: [
          {
            available: true,
            filename: generated.filename || "nda.docx",
            format: "docx",
            label: "DOCX",
            onSelect: () => downloadGeneratedDocx(generated),
          },
          downloadMenu.contractChoice(sourcePdf, {
            label: "PDF",
            onSelect: (choice) => downloadGeneratedPdf(choice),
            unavailableReason: generated.matterId
              ? "PDF is not available for this generated NDA yet."
              : "PDF is available after the NDA is saved.",
          }),
        ],
      }],
    });
    return;
  }
  await downloadGeneratedDocx(generated);
}

// D6: the generated-NDA PDF is a server URL, so it goes through the ok-checked
// guard too — a 4xx/5xx JSON error body must surface as a notice, not be saved as
// a broken "generated-nda.pdf".
async function downloadGeneratedPdf(choice) {
  if (!choice?.url) return;
  try {
    await downloadUrlGuarded(choice.url, choice.filename || "generated-nda.pdf");
  } catch (error) {
    notificationsController.notify("Download failed", error?.message || "The PDF could not be downloaded.");
  }
}

async function downloadGeneratedDocx(generated) {
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

// The concise SUCCESS-toast line for a finished generation: "NDA generated for
// <Counterparty> — <law>, <term>". The law + term come from the same manifest
// generatedManifestSummary reads (server-authoritative effective values); the
// counterparty falls back to the intake name. Used for the transient green toast
// that replaced the persistent inline green status text in the Generator.
function generatedToastSummary(manifest, counterpartyName) {
  const name = String(counterpartyName || manifest?.counterparty_name || "").trim();
  const head = name ? `NDA generated for ${name}` : "NDA generated";
  const bits = [];
  if (manifest && manifest.governing_law_value) {
    let law = String(manifest.governing_law_value);
    if (manifest.governing_law_overridden && manifest.entity_default_governing_law_value) {
      law += ` (overridden from ${manifest.entity_default_governing_law_value})`;
    }
    bits.push(law);
  }
  if (manifest && manifest.term_years) {
    const years = Number(manifest.term_years);
    if (Number.isFinite(years) && years > 0) bits.push(`${years}-year term`);
  }
  return bits.length ? `${head} — ${bits.join(", ")}` : head;
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
    loadDashboardDocuSignHealth();
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
  if (tabName === "corpus") {
    // Lazy-load on activation; the controller serves a warm cache when the Drive
    // pass is fresh, so re-activating the tab is cheap (mirrors repository's
    // load-on-activate).
    corpusController.load();
  }
  if (tabName === "playbook") {
    activateAdminSurface("playbook");
    activateAdminSection("playbook");
    // Default to the Clauses surface each time the Playbook is opened; the user
    // can switch to Entities via the in-editor section switcher.
    activatePlaybookSection("clauses");
  }
  if (tabName === "admin") {
    activateAdminSurface("admin");
    if (!["ai", "health", "email", "personalisation", "drive"].includes(activeAdminSection())) {
      activateAdminSection("ai");
    } else {
      activateAdminSection(activeAdminSection());
    }
    // Refresh the first-run setup checklist. Re-rendering on every activation lets
    // a step's ✓ light up once its connection status has been detected (e.g. the
    // admin opened the Drive section, so state.driveStatus is now populated).
    if (typeof AdminOnboarding !== "undefined") AdminOnboarding.render(state);
  }
  if (tabName === "guide") {
    // The entire Guide tab IS the embedded User Guide. Its own sidebar (the
    // 15 walkthrough tabs + Glossary) is the navigation, so there is a single
    // guide section — "user_guide" — that renders the full-bleed handbook.
    // The old Document / Checkers / AI Review explainers were folded into the
    // guide's own tabs and no longer have admin-nav entries.
    activateAdminSurface("guide");
    activateAdminSection("user_guide");
    initGuideToggle();
  }
}

// --- User⇄Developer guide toggle (open to all users) -----------------------
// The Guide tab embeds one of two self-contained static HTML guides via an
// iframe. The segmented toggle in #adminUserGuidePanel swaps which one is
// shown (iframe src + open-in-new-tab href + header title) and persists the
// choice in localStorage. Both guides are public static files — no auth gate.
var GUIDE_SRC = {
  user: "/static/user-guide.html?v=20260708guide4",
  developer: "/static/developer-guide.html?v=20260708guide4",
};
var GUIDE_MODE_STORAGE_KEY = "ndaGuideMode";

function applyGuideMode(mode) {
  var resolved = mode === "developer" ? "developer" : "user";
  var frame = document.getElementById("guideFrame");
  var link = document.getElementById("guideOpenLink");
  var title = document.getElementById("guideTitle");
  var src = GUIDE_SRC[resolved];
  if (frame && frame.getAttribute("src") !== src) frame.setAttribute("src", src);
  if (link) link.setAttribute("href", src);
  if (title) title.textContent = resolved === "developer" ? "Developer Guide" : "User Guide";
  var buttons = document.querySelectorAll(".guide-mode-btn[data-guide-mode]");
  Array.prototype.forEach.call(buttons, function (btn) {
    btn.classList.toggle("active", btn.dataset.guideMode === resolved);
  });
  try {
    window.localStorage.setItem(GUIDE_MODE_STORAGE_KEY, resolved);
  } catch (err) {
    /* localStorage may be unavailable (private mode); mode just won't persist */
  }
}

function initGuideToggle() {
  var buttons = document.querySelectorAll(".guide-mode-btn[data-guide-mode]");
  if (!buttons.length) return;
  var stored = "user";
  try {
    if (window.localStorage.getItem(GUIDE_MODE_STORAGE_KEY) === "developer") {
      stored = "developer";
    }
  } catch (err) {
    /* default to user when storage is unavailable */
  }
  if (!initGuideToggle._bound) {
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.addEventListener("click", function () {
        applyGuideMode(btn.dataset.guideMode);
      });
    });
    initGuideToggle._bound = true;
  }
  applyGuideMode(stored);
}

// LARGE-STORE BOUND: the dashboard intake table used to render EVERY Inbox matter;
// a Gmail-import-storm account (thousands of inbound matters) froze the dashboard
// on boot. The visible table caps at this many rows -- the count label and the
// per-column stat chips still aggregate over the FULL list (cheap data-only pass),
// and a truncation row points at the Repository board for the rest.
const DASHBOARD_INBOX_MAX_ROWS = 30;

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
  const visibleInboxMatters = inboxMatters.slice(0, DASHBOARD_INBOX_MAX_ROWS);
  const truncatedCount = inboxMatters.length - visibleInboxMatters.length;
  const truncationRow = truncatedCount > 0
    ? (
      `<tr class="dashboard-inbox-more-row" data-dashboard-inbox-truncated="${truncatedCount}">` +
      `<td colspan="5">Showing ${visibleInboxMatters.length} of ${inboxMatters.length} Inbox NDAs — open the Repository to browse the rest.</td>` +
      `</tr>`
    )
    : "";
  dashboardInboxTableBody.innerHTML = visibleInboxMatters.map((matter) => {
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
  }).join("") + truncationRow;
}

async function loadDashboardAiHealth() {
  if (!dashboardHealthItems.length) return;
  renderDashboardHealth("ai", {
    tone: "checking",
  });
  try {
    // Read the NON-admin AI-availability endpoint so the "is AI usable?" badge is
    // correct for every authenticated user. The full /api/ai/settings read is
    // admin-only (it carries provider/model/key-source config); pointing this badge
    // at it made non-admins get a 403 and see AI as permanently "blocked" even
    // though USING the AI review is open to them.
    const response = await fetch("/api/ai/availability");
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, "AI availability could not load");
    renderDashboardAiHealth(payload);
  } catch (error) {
    renderDashboardHealth("ai", {
      tone: "blocked",
    });
  }
}

async function loadPersonalisationSettings() {
  // The caller's OWN resolved signature — works for every authenticated user
  // (admin or not), unlike the admin-only endpoint which 403'd for non-admins
  // and left state.personalisationSettings null. The /api/me/ GET returns the
  // user's override or the inherited default as `personalisation`.
  try {
    const response = await fetch("/api/me/personalisation-settings");
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
  const params = action.params && typeof action.params === "object" ? action.params : {};
  const matter = action.matter && typeof action.matter === "object" ? action.matter : {};
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
      if (dispatch) {
        setDraftInputValue(purposeInput, prompt);
        return;
      }
      if (purposeInput) purposeInput.value = prompt;
    };
    activateTab("generator");
    applyDashboardAssistantPrefill({ dispatch: false });
    await draftIntakeController.activate();
    applyDashboardAssistantPrefill();
    document.querySelector("#draftIntakeCounterpartyName")?.focus();
    return { statusText: "Generator opened. Review all intake fields before generating." };
  }
  if (actionName === "gmail_import" || actionName === "sync_gmail") {
    const limit = Number.isFinite(Number(params.limit)) ? Math.max(1, Math.min(100, Number(params.limit))) : 25;
    const payload = await postAssistantActionJson(
      "/api/gmail/import",
      { limit },
      "Gmail sync could not run",
    );
    await Promise.resolve(repositoryController.loadMatters()).then(() => {
      renderDashboardInboxTable();
      notificationsController.observe(state.matters);
    });
    adminIntegrationsController.load();
    authSessionController.load();
    const imported = Number(payload?.result?.imported_count ?? payload?.result?.created_count ?? 0);
    return { statusText: `Gmail sync complete. Imported ${imported} ${imported === 1 ? "NDA" : "NDAs"}.` };
  }
  if (actionName === "refresh_review" || actionName === "run_review") {
    const matterId = assistantActionMatterId(params, matter);
    if (!matterId) throw new Error("NDA not found.");
    // The AI review now runs ASYNCHRONOUSLY: POST /review-refresh returns 202 in
    // milliseconds and a worker does the heavy review. So there is NO long
    // synchronous wait to bound anymore — drop the 180s timeout. We open the matter
    // into the Review tab and hand off to the in-flight poll controller, which
    // tracks review_status to completion (or surfaces failure + Retry).
    const payload = await postAssistantActionJson(
      `/api/matters/${encodeURIComponent(matterId)}/review-refresh`,
      null,
      "Review could not refresh",
    );

    // AI off: nothing was scheduled; report honestly without claiming a refresh.
    if (payload?.ai_review_unavailable) {
      return {
        statusText:
          payload.ai_review_unavailable_message
          || "Review can't be completed — no AI reviewer available.",
      };
    }

    // Load the matter into the Review tab so the in-flight spinner/result render
    // there. The server's 202 payload carries review_status (+ the matter) but not
    // the heavy review_result, so reflect the scheduled state.
    const refreshedMatter = matterReviewPayloadToMatter(payload);
    loadMatterIntoReview(refreshedMatter);
    await repositoryController.loadMatters();
    renderDashboardInboxTable();
    activateTab("review");

    const status = String(payload?.review_status || "");
    const title = refreshedMatter?.matter?.document_title || refreshedMatter?.document_title || matter.title || "NDA";
    if (status === "in_progress") {
      // Background review scheduled (or already pending): start polling so the tab
      // updates when it finishes.
      if (typeof startReviewPoll === "function") {
        enterReviewInFlightUi?.();
        startReviewPoll(matterId);
      }
      return { statusText: `Review started for ${title}. It will update when it finishes.` };
    }
    const refresh = payload?.review_refresh || {};
    if (refresh.stale) return { statusText: "Review refreshed, but it is still marked stale." };
    return { statusText: `Review is current for ${title}.` };
  }
  if (actionName === "approve_matter") {
    const matterId = assistantActionMatterId(params, matter);
    if (!matterId) throw new Error("NDA not found.");
    const payload = await postAssistantActionJson(
      `/api/matters/${encodeURIComponent(matterId)}/approve`,
      null,
      "Review could not be approved",
    );
    if (payload.matter && typeof payload.matter === "object") {
      state.selectedMatter = state.selectedMatter?.id === matterId
        ? { ...state.selectedMatter, ...payload.matter }
        : state.selectedMatter;
    }
    await repositoryController.loadMatters();
    renderDashboardInboxTable();
    activateTab("repository");
    return { statusText: `Approved ${matter.title || "NDA"}.` };
  }
  if (actionName === "send_redline") {
    const matterId = assistantActionMatterId(params, matter);
    if (!matterId) throw new Error("NDA not found.");
    const selectedMatter = await assistantMatterFromState(matterId);
    const recipient = MatterUtils.recipientEmail(selectedMatter);
    const sendBlockReason = MatterUtils.gmailSendBlock(selectedMatter, state.gmailStatus);
    if (sendBlockReason) throw new Error(sendBlockReason);
    if (!recipient) throw new Error("NDA does not have a valid reply recipient email address.");
    const sendPayload = {
      matter_id: matterId,
      confirm_send: true,
      confirm_recipient: recipient,
      to: recipient,
      subject: RepositorySend.defaultOutboundSubject(selectedMatter),
      body: RepositorySend.defaultOutboundBody(selectedMatter, state.personalisationSettings),
    };
    const payload = await postAssistantActionJson(
      "/api/gmail/send-redline",
      sendPayload,
      "Redline email could not send",
    );
    if (payload.matter?.id) {
      state.matters = state.matters.map((existing) => (
        existing.id === payload.matter.id ? payload.matter : existing
      ));
      if (state.selectedMatter?.id === payload.matter.id) {
        state.selectedMatter = { ...state.selectedMatter, ...payload.matter };
      }
    }
    await repositoryController.loadMatters();
    renderDashboardInboxTable();
    activateTab("repository");
    return { statusText: `Sent redline to ${recipient}.` };
  }
  const targetTab = String(action.target?.tab || "").trim();
  const allowedTabs = new Set(["repository", "playbook", "admin"]);
  if (allowedTabs.has(targetTab)) {
    activateTab(targetTab);
    return { statusText: `${targetTab[0].toUpperCase()}${targetTab.slice(1)} opened.` };
  }
  return { statusText: "No supported assistant action was available." };
}

async function postAssistantActionJson(url, body, fallbackMessage, { timeoutMs = 0 } = {}) {
  const options = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  };
  if (body && typeof body === "object") {
    options.body = JSON.stringify(body);
  }
  // Optional generous bound so a hung server (e.g. a long synchronous AI review)
  // cannot leave the assistant awaiting forever. 0 disables it (default).
  const canAbort = typeof AbortController === "function" && timeoutMs > 0;
  const controller = canAbort ? new AbortController() : null;
  const timer = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : null;
  if (controller) options.signal = controller.signal;
  let response;
  try {
    response = await fetch(url, options);
  } catch (fetchError) {
    if (fetchError?.name === "AbortError") {
      throw new Error(`${fallbackMessage} — the server did not respond in time. Please try again.`);
    }
    throw fetchError;
  } finally {
    if (timer !== null) window.clearTimeout(timer);
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (parseError) {
    payload = {};
  }
  if (!response.ok) throw reviewErrorFromPayload(payload, fallbackMessage);
  return payload;
}

function assistantActionMatterId(params = {}, matter = {}) {
  return String(params.matter_id || matter.id || "").trim();
}

async function assistantMatterFromState(matterId) {
  let matter = Array.isArray(state.matters)
    ? state.matters.find((candidate) => String(candidate?.id) === String(matterId))
    : null;
  if (matter) return matter;
  await repositoryController.loadMatters();
  matter = Array.isArray(state.matters)
    ? state.matters.find((candidate) => String(candidate?.id) === String(matterId))
    : null;
  if (!matter) throw new Error("NDA not found.");
  return matter;
}

function setDraftInputValue(input, value, { onlyIfEmpty = false } = {}) {
  if (!input || (onlyIfEmpty && String(input.value || "").trim())) return;
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function renderDashboardAiHealth(payload = {}) {
  // Flat shape from the non-admin /api/ai/availability endpoint: { ai_enabled,
  // ai_configured, active_engine }. No provider/model/key detail is exposed.
  const activeEngine = String(payload.active_engine || "ai_first");
  const enabled = payload.ai_enabled === true;
  const keyConfigured = payload.ai_configured === true;

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
      detail: "Checking Gmail",
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
      detail: "Gmail ready",
    });
    return;
  }
  if (inboundReady || outboundReady) {
    renderDashboardHealth("email", {
      tone: "warning",
      detail: dashboardGmailHealthDetail(gmailStatus, inbound, outbound),
    });
    return;
  }
  renderDashboardHealth("email", {
    tone: "blocked",
    detail: dashboardGmailHealthDetail(gmailStatus, inbound, outbound),
  });
}

async function loadDashboardDriveHealth() {
  if (!dashboardHealthItems.length) return;
  renderDashboardHealth("drive", { tone: "checking", detail: "Checking Drive" });
  try {
    const response = await fetch("/api/drive/status");
    const payload = await response.json();
    if (!response.ok) throw new Error("Drive status could not load");
    renderDashboardDriveHealth(payload);
  } catch (error) {
    renderDashboardHealth("drive", { tone: "blocked", detail: "Drive status unavailable" });
  }
}

function renderDashboardDriveHealth(status = {}) {
  if (!dashboardHealthItems.length) return;
  // Drive is an OPTIONAL export integration: connected -> ready (green); not
  // connected -> warning (amber, "available but not set up") rather than blocked,
  // since an unconfigured optional feature is not an error.
  renderDashboardHealth("drive", {
    tone: status.connected === true ? "ready" : driveDashboardTone(status),
    detail: dashboardDriveHealthDetail(status),
  });
}

async function loadDashboardDocuSignHealth() {
  if (!dashboardHealthItems.length) return;
  renderDashboardHealth("docusign", { tone: "checking", detail: "Checking DocuSign" });
  try {
    const response = await fetch("/api/docusign/status");
    const payload = await response.json();
    if (!response.ok) throw new Error("DocuSign status could not load");
    renderDashboardDocuSignHealth(payload);
  } catch (error) {
    renderDashboardHealth("docusign", { tone: "blocked", detail: "DocuSign status unavailable" });
  }
}

function renderDashboardDocuSignHealth(status = {}) {
  if (!dashboardHealthItems.length) return;
  // DocuSign is an OPTIONAL e-signature integration, mirroring Drive: connected
  // -> ready (green); not connected -> warning (amber, "available but not set
  // up") rather than blocked, since an unconfigured optional feature is not an
  // error. The light is driven by the `connected` boolean from
  // GET /api/docusign/status — the same status the admin DocuSign panel reads.
  renderDashboardHealth("docusign", {
    tone: status.connected === true ? "ready" : "warning",
    detail: status.connected === true ? "DocuSign connected" : "DocuSign not connected",
  });
}

function renderDashboardHealth(kind, { tone, detail }) {
  const item = document.querySelector(`[data-dashboard-health="${kind}"]`);
  if (!item) return;
  const effectiveTone = ["ready", "warning", "blocked", "checking"].includes(tone) ? tone : "checking";
  const name = item.querySelector(".dashboard-health-name")?.textContent?.trim() || kind;
  const detailText = detail || defaultDashboardHealthDetail(kind, effectiveTone);
  item.classList.remove("ready", "warning", "blocked", "checking");
  item.classList.add(effectiveTone);
  item.setAttribute("title", detailText);
  item.setAttribute("aria-label", `${name}: ${detailText}`);
  let detailNode = item.querySelector("[data-dashboard-health-detail]");
  if (!detailNode) {
    detailNode = document.createElement("span");
    detailNode.className = "dashboard-health-detail";
    detailNode.dataset.dashboardHealthDetail = "";
    item.appendChild(detailNode);
  }
  // While the probe is in flight (tone === "checking") show a subtle shimmer
  // placeholder instead of the bare "Checking" word — a quiet "we're working"
  // signal. The accessible name still carries the textual status (set above), and
  // the moment a real tone arrives the placeholder is replaced by the status text.
  // The shimmer animation is gated behind prefers-reduced-motion in CSS.
  if (effectiveTone === "checking") {
    detailNode.textContent = "";
    const placeholder = document.createElement("span");
    placeholder.className = "skeleton-block health-skeleton";
    placeholder.setAttribute("aria-hidden", "true");
    detailNode.appendChild(placeholder);
  } else {
    detailNode.textContent = detailText;
  }
}

function defaultDashboardHealthDetail(kind, tone) {
  if (tone === "checking") return "Checking";
  if (kind === "ai") return tone === "ready" ? "AI review ready" : "AI review needs setup";
  if (kind === "email") return tone === "ready" ? "Gmail ready" : "Gmail needs setup";
  if (kind === "drive") return tone === "ready" ? "Drive connected" : "Drive needs setup";
  if (kind === "docusign") return tone === "ready" ? "DocuSign connected" : "DocuSign needs setup";
  return tone;
}

function dashboardGmailHealthDetail(status, inbound, outbound) {
  const setup = status?.setup || {};
  if (
    status?.google_oauth_configured === false
    || status?.oauth_configured === false
    || setup.state === "missing_oauth_config"
    || setup.google_oauth_configured === false
  ) {
    return "Google OAuth not configured";
  }
  if (
    (status?.user_scoped === true && status?.signed_in === false)
    || setup.state === "sign_in_required"
  ) {
    return "Sign in with Google";
  }
  const missingScopes = dashboardMissingScopes(status, inbound, outbound);
  if (missingScopes.length) return "Gmail scope needed";
  const roleDetails = [
    dashboardGmailRoleDetail("Inbound", inbound),
    dashboardGmailRoleDetail("Outbound", outbound),
  ].filter(Boolean);
  return roleDetails.length ? roleDetails.join("; ") : "Gmail needs setup";
}

function dashboardGmailRoleDetail(label, account = {}) {
  if (account.ready === true) return "";
  if (account.enabled === false) return `${label} disabled`;
  if (account.recovery?.state === "missing_token") return `${label} token missing`;
  if (account.recovery?.state === "missing_scope") return `${label} scope needed`;
  if (account.recovery?.state === "sign_in_required") return "Sign in with Google";
  if (account.recovery?.state === "missing_oauth_config") return "Google OAuth not configured";
  const token = account.token || {};
  if (token.source === "missing" || token.configured === false) return `${label} token missing`;
  if (account.connect_url) return `${label} needs connection`;
  if (account.error) return `${label} needs setup`;
  return `${label} needs setup`;
}

function driveDashboardTone(status = {}) {
  if (status.connected === true) return "ready";
  const setup = status.setup || {};
  if (
    status.google_oauth_configured === false
    || status.oauth_configured === false
    || setup.state === "missing_oauth_config"
    || setup.google_oauth_configured === false
  ) return "blocked";
  if (status.error) return "blocked";
  return "warning";
}

function dashboardDriveHealthDetail(status = {}) {
  const setup = status.setup || {};
  const recovery = status.recovery || {};
  if (status.connected === true) return "Drive connected";
  if (
    status.google_oauth_configured === false
    || status.oauth_configured === false
    || setup.state === "missing_oauth_config"
    || recovery.state === "missing_oauth_config"
  ) {
    return "Google OAuth not configured";
  }
  if (
    (status.user_scoped === true && status.signed_in === false)
    || setup.state === "sign_in_required"
    || recovery.state === "sign_in_required"
  ) return "Sign in with Google";
  if (recovery.state === "missing_token") return "Drive token missing";
  if (dashboardMissingScopes(status, recovery).length || recovery.state === "missing_scope") return "Drive scope needed";
  if (status.needs_connect === true || status.connect_url) return "Drive access needed";
  if (status.enabled === false) return "Drive uploads disabled";
  if (status.token?.source === "missing" || status.token?.configured === false) return "Drive token missing";
  return "Drive not connected";
}

function dashboardMissingScopes(...sources) {
  const scopes = [];
  sources.forEach((source) => {
    if (Array.isArray(source?.missing_scopes)) scopes.push(...source.missing_scopes);
    if (Array.isArray(source?.token?.missing_scopes)) scopes.push(...source.token.missing_scopes);
    if (Array.isArray(source?.scope_status?.missing)) scopes.push(...source.scope_status.missing);
    if (Array.isArray(source?.token?.scope_status?.missing)) scopes.push(...source.token.scope_status.missing);
    if (source?.scope_status === "missing" || source?.token?.scope_status === "missing") scopes.push("required scope");
    if (source?.scope_status?.ok === false || source?.token?.scope_status?.ok === false) scopes.push("required scope");
  });
  return scopes.filter(Boolean);
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
  if (sectionName === "models") {
    adminModelsController.load();
  }
  if (sectionName === "health") {
    adminHealthController.load();
    // Deployment status is admin-only; load it on demand here (not on app boot)
    // so a non-admin authenticated user never triggers the admin-only 403 on
    // normal load. This also (re)renders the session-strip deployment warning.
    authSessionController.refreshDeploymentStatus();
  }
  if (sectionName === "drive") {
    adminDriveController.load();
  }
  if (sectionName === "docusign") {
    adminDocuSignController.load();
  }
  if (sectionName === "access") {
    adminAccessController.load();
  }
  // The "entities" admin section was removed: the signing-entity registry now
  // lives in the Playbook editor's Entities surface (activatePlaybookSection).
  if (sectionName === "personalisation") {
    adminPersonalisationController.load();
    // The admin global-default panel loads too; it self-hides for non-admins.
    adminGlobalPersonalisationController.load();
  }
}

function normalizeReviewInspectorView(viewName) {
  // Default to the first configured view ("overview") so a fresh/unknown state
  // lands on the at-a-glance Overview pane rather than the Clause detail.
  return REVIEW_INSPECTOR_VIEWS.includes(viewName) ? viewName : REVIEW_INSPECTOR_VIEWS[0];
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
