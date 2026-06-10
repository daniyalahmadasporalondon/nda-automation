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
import { clauseStatus, clausePasses, clauseDisplayName, clauseIsDynamic } from "./clause-status.mjs";
import { escapeHtml, joinClasses, mergeClauses } from "./html-utils.mjs";
import {
  fullReplacementOperations,
  renderDiffOperations,
  renderInlineToken,
  needsInlineSpace,
} from "./inline-diff.mjs";
import { MatterUtils } from "./matter-utils.mjs";
import {
  isSupportedSendFilename,
  isValidRecipientEmail,
  fileStem,
} from "./send-document.mjs";
import { createDraftIntake } from "./draft-intake.mjs";
import { createGenerationApi, GenerationUnavailableError } from "./generation-api.mjs";
import * as ReviewWorkstationModel from "../review-workstation-model.mjs";
import {
  DASHBOARD_SEARCH_CHIPS,
  NULL_FILTER_SPEC,
  SEARCH_INTENT_ENDPOINT,
  SUMMARY_LABEL,
  SUMMARY_UNAVAILABLE_MESSAGE,
  applyFilterSpec,
  buildArtifactLineage,
  chipById,
  filterMattersByStatus,
  filterMattersByText,
  filterSpecIsEmpty,
  formatSummaryResult,
  groupMattersByCounterparty,
  matterCounterparty,
  matterStatusLabel,
  matterTitle,
  runChip,
  summaryEndpoint,
  summaryErrorMessage,
  validateFilterSpec,
} from "./dashboard-search.mjs";

Object.assign(window, {
  clauseStatus,
  clausePasses,
  clauseDisplayName,
  clauseIsDynamic,
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
  ReviewWorkstationModel,
  effectiveReviewRedlines: () => ReviewWorkstationModel.effectiveReviewRedlines(state),
  // Dashboard smart-search (v1, deterministic). The DOM controller is a classic
  // script built at app.js load time; it reads these pure filters lazily at
  // runtime (inside handlers), so the deferred-module availability is safe.
  DashboardSearch: {
    DASHBOARD_SEARCH_CHIPS,
    NULL_FILTER_SPEC,
    SEARCH_INTENT_ENDPOINT,
    SUMMARY_LABEL,
    SUMMARY_UNAVAILABLE_MESSAGE,
    applyFilterSpec,
    buildArtifactLineage,
    chipById,
    filterMattersByStatus,
    filterMattersByText,
    filterSpecIsEmpty,
    formatSummaryResult,
    groupMattersByCounterparty,
    matterCounterparty,
    matterStatusLabel,
    matterTitle,
    runChip,
    summaryEndpoint,
    summaryErrorMessage,
    validateFilterSpec,
  },
});
