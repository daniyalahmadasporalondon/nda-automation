// Single-source bridge: expose the shipped utility modules as the globals the
// classic <script> chain expects.
//
// Historically each of these helpers existed twice — a classic static/js/X.js
// that declared globals for the browser, and a static/js/modules/X.mjs that the
// frontend tests import. The two could drift silently: a fix in one was never
// caught by tests driving the other. This bridge deletes that divergence — the
// .mjs files are now the ONLY source, and the browser runs exactly the modules
// the tests exercise.
//
// Module scripts are deferred, so this runs after the classic scripts but before
// any user interaction. Every global assigned here is only *called* at runtime
// (inside render functions and event handlers), never at classic-script load
// time, so the slightly-later availability is safe. (The one load-time consumer,
// createSendDocumentController, stays a classic script for exactly this reason.)
// Versioned specifier so a returning browser re-fetches clause-status.mjs when
// its bytes change (its clauseDisplayName now humanizes a name-less clause id).
// Bump this token in lockstep with the clause-status.mjs bytes.
import { clauseStatus, clausePasses, clauseDisplayName, clauseIsDynamic } from "./clause-status.mjs?v=20260621humanize2";
// Versioned specifier so a returning browser re-fetches humanize.mjs when its
// bytes change (a bare relative import resolves to a query-less URL the browser
// caches independently of this file's ?v=). global-bridge.mjs is the SOLE
// importer of this module (every FE consumer reads window.humanizeId /
// window.friendlyModelName via this bridge, never re-importing), so versioning
// the specifier here cannot create a duplicate module instance. Keep this token
// in lockstep with the humanize.mjs bytes.
import {
  humanizeId,
  friendlyModelName,
  humanizeClauseId,
  humanizeAuditAction,
  humanizeSettingKey,
  humanizeCounterKey,
} from "./humanize.mjs?v=20260621humanize2";
// Versioned specifiers on EVERY module import below: a bare relative import
// resolves to a query-less URL that the server serves with no-cache+ETag, so
// each warm visit paid a revalidation round-trip per module (~10 extra RTTs
// before first render on high-latency mobile). With the token the server serves
// them immutable, so a warm visit is fully cache-served. global-bridge.mjs is
// the sole runtime importer of each (inline-diff.mjs / review-workstation-model
// .mjs re-import html-utils.mjs / redline-edit-contract.mjs with the SAME token,
// resolving to the same URL), so no duplicate module instances are created.
// Keep each token in lockstep with its module's bytes.
import { escapeHtml, joinClasses, mergeClauses } from "./html-utils.mjs?v=20260703cachebust1";
import {
  fullReplacementOperations,
  renderDiffOperations,
  renderInlineToken,
  needsInlineSpace,
// Versioned specifier so a returning browser re-fetches inline-diff.mjs when its
// bytes change (renderDiffOperations now defensively filters null/typeless ops).
// A bare relative import resolves to a query-less URL the browser caches
// independently of this file's ?v=, so the global-bridge token bump alone would
// not refresh it. global-bridge.mjs is the SOLE importer of this module (every
// FE consumer reads window.renderDiffOperations / window.renderInlineToken via
// this bridge, never re-importing), so versioning here cannot create a duplicate
// module instance. Keep this token in lockstep with the inline-diff.mjs bytes.
} from "./inline-diff.mjs?v=20260703cachebust1";
import { MatterUtils } from "./matter-utils.mjs?v=reviewrecovery1";
import {
  isSupportedSendFilename,
  isValidRecipientEmail,
  fileStem,
} from "./send-document.mjs?v=20260703cachebust1";
import { createDraftIntake } from "./draft-intake.mjs?v=20260702termunit1";
import { GeneratorWorkstationModel } from "./generator-workstation-model.mjs?v=20260703cachebust1";
import { createGenerationApi, GenerationUnavailableError, GenerationTimeoutError } from "./generation-api.mjs?v=20260703cachebust1";
import { PdfMarkupWorkstation } from "./pdf-markup-workstation.mjs?v=20260703cachebust1";
import { RedlineEditContract } from "./redline-edit-contract.mjs?v=20260703cachebust1";
import { ReviewWorkstationModel } from "./review-workstation-model.mjs?v=20260703cachebust1";
// Versioned import so a returning browser re-fetches docusign-model.mjs when its
// bytes change (a bare relative import resolves to a query-less URL the browser
// caches independently of this file's ?v=, so a token bump on global-bridge alone
// would not refresh it). global-bridge.mjs is the SOLE importer of this module
// (all other FE consumers read window.DocuSignModel via the global bridge, never
// re-importing it), so versioning the specifier here cannot create a duplicate
// module instance. Keep this token in lockstep with the docusign-model.mjs bytes.
import { DocuSignModel } from "./docusign-model.mjs?v=20260619signorder2";
import {
  DASHBOARD_SEARCH_CHIPS,
  NULL_FILTER_SPEC,
  SEARCH_CONFIG_ENDPOINT,
  SEARCH_INTENT_ENDPOINT,
  SUMMARY_LABEL,
  SUMMARY_UNAVAILABLE_MESSAGE,
  adaptCorpusMatter,
  applyFilterSpec,
  buildArtifactLineage,
  chipById,
  filterMattersByStatus,
  filterMattersByText,
  filterSpecIsEmpty,
  flattenCorpusPayload,
  formatSummaryResult,
  groupMattersByCounterparty,
  matterCounterparty,
  matterStatusLabel,
  matterTitle,
  runChip,
  setFilterSpecAllowlists,
  summaryEndpoint,
  summaryErrorMessage,
  validateFilterSpec,
} from "./dashboard-search.mjs?v=20260703cachebust1";

Object.assign(window, {
  clauseStatus,
  clausePasses,
  clauseDisplayName,
  clauseIsDynamic,
  // Shared humanizers: keep raw snake_case ids and raw AI model ids off the
  // screens legal users read. Called only inside render functions at runtime.
  humanizeId,
  friendlyModelName,
  // Admin-panel humanizers: clause-id list, settings-audit action/setting keys,
  // and telemetry counter keys all leaked raw to the admin screens. These keep
  // the DISPLAY strings human; the underlying ids/keys stay untouched.
  humanizeClauseId,
  humanizeAuditAction,
  humanizeSettingKey,
  humanizeCounterKey,
  escapeHtml,
  joinClasses,
  mergeClauses,
  fullReplacementOperations,
  renderDiffOperations,
  renderInlineToken,
  needsInlineSpace,
  MatterUtils,
  // The send-document controller (a classic script, since it is constructed at
  // app.js load time) shares these validation helpers so its form rules are the
  // same single source the tests exercise — not a re-implemented copy.
  isSupportedSendFilename,
  isValidRecipientEmail,
  fileStem,
  // The draft-intake controller (also a classic script built at app.js load
  // time) constructs its helper surface lazily via this factory, so it runs the
  // exact entity-picker logic the tests exercise.
  createDraftIntake,
  // The generation API wrapper backs the draft-intake controller's onGenerate
  // seam (wired in app.js). Constructed lazily inside the handler, never at
  // load time, so the deferred-module availability is safe.
  createGenerationApi,
  GenerationUnavailableError,
  GenerationTimeoutError,
  GeneratorWorkstationModel,
  PdfMarkupWorkstation,
  RedlineEditContract,
  ReviewWorkstationModel,
  // DocuSign view-model. The admin-docusign + docusign-send controllers are
  // classic scripts built at app.js load time; they call this model only at
  // runtime (inside render/handler functions), so deferred availability is safe.
  DocuSignModel,
  // Dashboard smart-search (v1, deterministic). The DOM controller is a classic
  // script built at app.js load time; it reads these pure filters lazily at
  // runtime (inside handlers), so the deferred-module availability is safe.
  DashboardSearch: {
    DASHBOARD_SEARCH_CHIPS,
    NULL_FILTER_SPEC,
    SEARCH_CONFIG_ENDPOINT,
    SEARCH_INTENT_ENDPOINT,
    SUMMARY_LABEL,
    SUMMARY_UNAVAILABLE_MESSAGE,
    adaptCorpusMatter,
    applyFilterSpec,
    buildArtifactLineage,
    chipById,
    filterMattersByStatus,
    filterMattersByText,
    filterSpecIsEmpty,
    flattenCorpusPayload,
    formatSummaryResult,
    groupMattersByCounterparty,
    matterCounterparty,
    matterStatusLabel,
    matterTitle,
    runChip,
    setFilterSpecAllowlists,
    summaryEndpoint,
    summaryErrorMessage,
    validateFilterSpec,
  },
});
