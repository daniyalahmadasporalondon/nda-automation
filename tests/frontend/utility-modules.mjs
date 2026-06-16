import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { clausePasses, clauseStatus } from "../../static/js/modules/clause-status.mjs";
import { escapeHtml, joinClasses, mergeClauses } from "../../static/js/modules/html-utils.mjs";
import {
  fullReplacementOperations,
  needsInlineSpace,
  renderDiffOperations,
} from "../../static/js/modules/inline-diff.mjs";
import {
  MatterUtils,
  counterpartyEmail,
  gmailSendBlock,
  gmailSendButtonLabel,
  needsHumanReview,
  reviewStale,
  reviewStaleLabel,
  reviewStaleReasons,
} from "../../static/js/modules/matter-utils.mjs";
import { createRepositoryApi } from "../../static/js/modules/repository-api.mjs";
import {
  clausesOf,
  draftDiffersFromActive,
  formatVersionDateTime,
  friendlyVersionLabel,
  hashOf,
  isWorkingDirty,
  normalizePlaybookResponse,
  normalizeValidation,
  rawVersionId,
  shortHash,
  validationSummary,
  versionLabel,
  versionOf,
  versionTimestamp,
} from "../../static/js/modules/playbook-draft.mjs";
import { createPlaybookApi } from "../../static/js/modules/playbook-api.mjs";
import { PlaybookAuthoringModel } from "../../static/js/modules/playbook-authoring-model.mjs";
import {
  dashboardGreeting,
  firstNameFromDisplayName,
  firstNameFromEmail,
  resolveFirstName,
} from "../../static/js/modules/greeting.mjs";
import {
  COUNTERPARTY_UNKNOWN,
  DASHBOARD_ASSISTANT_ENDPOINT,
  DASHBOARD_SEARCH_CHIPS,
  NULL_FILTER_SPEC,
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
  matterGoverningLaw,
  matterHasClause,
  matterSigned,
  matterStatus,
  matterStatusLabel,
  matterTermYears,
  matterTitle,
  runChip,
  summaryEndpoint,
  summaryErrorMessage,
  validateFilterSpec,
} from "../../static/js/modules/dashboard-search.mjs";
import {
  buildSendDocumentPayload,
  isSupportedSendFilename,
  isValidRecipientEmail,
  validateSendDocument,
} from "../../static/js/modules/send-document.mjs";
import {
  DEFAULT_MAX_TERM_YEARS,
  SIGNING_ENTITIES,
  applyEntitySelection,
  buildDraftPayload,
  clearGoverningLawOverride,
  createDraftIntake,
  createInitialIntake,
  defaultAddressFor,
  effectiveGoverningLaw,
  formatAddressLines,
  governingLawOptions,
  hasMultipleAddresses,
  selectAddress,
  selectedAddress,
  setGoverningLawOverride,
  validateDraftIntake,
} from "../../static/js/modules/draft-intake.mjs";
import {
  DEFAULT_GENERATE_TIMEOUT_MS,
  GenerationTimeoutError,
  GenerationUnavailableError,
  createGenerationApi,
} from "../../static/js/modules/generation-api.mjs";
import { GeneratorWorkstationModel } from "../../static/js/modules/generator-workstation-model.mjs";
import { PdfMarkupWorkstation } from "../../static/js/modules/pdf-markup-workstation.mjs";
import { RedlineEditContract } from "../../static/js/modules/redline-edit-contract.mjs";
import { ReviewWorkstationModel } from "../../static/js/modules/review-workstation-model.mjs";

const FIXTURE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "../fixtures");
const inlineDiffVectors = JSON.parse(fs.readFileSync(path.join(FIXTURE_DIR, "inline_diff_vectors.json"), "utf8"));

assert.equal(escapeHtml(`<a data-x="1">Bob's & Co</a>`), "&lt;a data-x=&quot;1&quot;&gt;Bob&#039;s &amp; Co&lt;/a&gt;");
assert.equal(joinClasses("one", "", ["two", null, "three"]), "one two three");
assert.deepEqual(mergeClauses([{ id: "a" }], [{ id: "a" }, { id: "b" }]), [{ id: "a" }, { id: "b" }]);

const reviewStatus = clauseStatus({
  decision: "review",
  status: "match",
  review_state: { state: "review", blocks_send: true, requires_human_review: true },
});
assert.equal(reviewStatus.needsReview, true);
assert.equal(reviewStatus.pillLabel, "NEEDS REVIEW");
assert.equal(reviewStatus.blocksSend, true);

const failStatus = clauseStatus({ decision: "fail", status: "check" });
assert.equal(failStatus.fails, true);
assert.equal(failStatus.requiresRedline, true);
assert.equal(failStatus.pillLabel, "FAIL");
assert.equal(failStatus.resultLabel, "Fail");

assert.equal(clausePasses({ decision: "pass", status: "match" }), true);

// clauseStatus consumes the backend canonical verdict (review_state.state /
// decision) rather than re-deriving a second opinion. A backend "check" state
// is a fail even though the raw `passes` flag is absent.
const canonicalCheck = clauseStatus({ review_state: { state: "check", blocks_send: true } });
assert.equal(canonicalCheck.fails, true);
assert.equal(canonicalCheck.tone, "check");
assert.equal(canonicalCheck.blocksSend, true);

// A "fail" decision maps to the check state (needs a redline), matching
// review_state.py, even with no nested review_state present.
const decisionFail = clauseStatus({ decision: "fail" });
assert.equal(decisionFail.fails, true);
assert.equal(decisionFail.tone, "check");

// A clause that carries only needs_review (no status/decision/review_state) must
// surface as Needs-review, not silently pending -- matching the Python
// normalizers' unknown -> review fail-safe.
const needsReviewOnly = clauseStatus({ needs_review: true });
assert.equal(needsReviewOnly.needsReview, true);
assert.equal(needsReviewOnly.tone, "review");
assert.equal(needsReviewOnly.passes, false);

// A truly signal-less clause stays pre-review Pending (idle), unchanged.
const signalLess = clauseStatus({});
assert.equal(signalLess.tone, "pending");
assert.equal(signalLess.needsReview, false);
assert.equal(signalLess.fails, false);

for (const pair of inlineDiffVectors.flatMap((vector) => vector.spacing_pairs || [])) {
  assert.equal(needsInlineSpace(pair.previous_token, pair.token), pair.needs_space, `${pair.previous_token} + ${pair.token}`);
}
for (const vector of inlineDiffVectors.filter((item) => item.rendered_html)) {
  assert.equal(renderDiffOperations(vector.operations), vector.rendered_html, vector.name);
}
assert.deepEqual(fullReplacementOperations("Old", "New"), [
  { type: "delete", token: "Old" },
  { type: "insert", token: "New" },
]);

const normalizedManualEdit = RedlineEditContract.normalizeRedlineEdit({
  action: "replace_paragraph",
  clause_id: "manual_viewer_edit",
  id: "manual-p1",
  inline_diff_operations: [
    { type: "equal", token: "Old" },
    { type: "unknown", token: "dropped" },
    { type: "insert", token: "New" },
  ],
  paragraph_id: "p1",
  replacement_text: "New text",
  whole_paragraph: false,
});
assert.equal(RedlineEditContract.isManualRedlineEdit(normalizedManualEdit), true);
assert.equal(RedlineEditContract.redlineInlinePreviewMode(normalizedManualEdit), "operations");
assert.equal(RedlineEditContract.redlineOperationPreviewMode(normalizedManualEdit), "operations");
assert.deepEqual(normalizedManualEdit.inline_diff_operations, [
  { type: "equal", token: "Old" },
  { type: "insert", token: "New" },
]);
const normalizedClauseEdit = RedlineEditContract.normalizeRedlineEdit({
  action: "replace_paragraph",
  clause_id: "governing_law",
  paragraph_id: "p2",
  replacement_text: "English law applies.",
});
assert.equal(RedlineEditContract.redlineInlinePreviewMode(normalizedClauseEdit), "whole_paragraph");
assert.equal(RedlineEditContract.redlineInlinePreviewMode({
  action: "replace_paragraph",
  clause_id: "manual_viewer_edit",
  paragraph_id: "p3",
  replacement_text: "Manual",
}), "character_diff");
assert.equal(RedlineEditContract.redlineOperationPreviewMode({
  action: "replace_paragraph",
  clause_id: "manual_viewer_edit",
  paragraph_id: "p3",
  replacement_text: "Manual",
}), "word_diff");
assert.equal(RedlineEditContract.isKnownRedlineAction("unknown_action"), false);
assert.equal(RedlineEditContract.isManualRedlineAction("insert_after_paragraph"), false);
assert.equal(RedlineEditContract.normalizeRedlineEdit({ action: "unknown_action", paragraph_id: "p1" }), null);
assert.equal(RedlineEditContract.normalizeRedlineEdit({ action: "replace_paragraph" }), null);
assert.equal(RedlineEditContract.normalizeRedlineEdit({
  action: "insert_after_paragraph",
  clause_id: "manual_viewer_edit",
  paragraph_id: "p4",
  replacement_text: "Manual insert should be rejected.",
}), null);
assert.equal(RedlineEditContract.normalizeRedlineEdit({
  action: "insert_after_paragraph",
  is_manual: true,
  paragraph_id: "p4",
  replacement_text: "Manual insert should be rejected.",
}), null);
assert.deepEqual(RedlineEditContract.normalizeRedlineEdit({
  action: "delete_paragraph",
  is_manual: true,
  original_text: "Delete this.",
  paragraph_id: "p5",
  replacement_text: "Delete this.",
}), {
  action: "delete_paragraph",
  action_label: "Remove paragraph",
  clause_id: "manual_viewer_edit",
  id: "",
  is_manual: true,
  original_text: "Delete this.",
  paragraph_id: "p5",
  replacement_text: "Delete this.",
  status: "proposed",
});
assert.equal(RedlineEditContract.redlineReplacementText({
  action: "delete_paragraph",
  replacement_text: "Ignored for preview/export.",
}), "");
assert.equal(RedlineEditContract.redlineInlinePreviewMode({
  action: "replace_paragraph",
  clause_id: "server_clause",
  paragraph_id: "p6",
  replacement_text: "Server edit.",
  whole_paragraph: false,
}), "whole_paragraph");
assert.equal(RedlineEditContract.redlineActionLabel({ action: "insert_after_paragraph" }), "Insert after paragraph");
assert.equal(RedlineEditContract.redlineInsertedText({ insert_text: "Inserted", replacement_text: "Fallback" }), "Inserted");
assert.deepEqual(RedlineEditContract.normalizeRedlineEdits([
  { action: "replace_paragraph", clause_id: "clause", paragraph_id: "p1", replacement_text: "Server" },
  { action: "insert_after_paragraph", clause_id: "manual_viewer_edit", paragraph_id: "p1", replacement_text: "Unsafe" },
  { action: "format_paragraph", is_manual: true, paragraph_id: "p1", replacement_text: "Formatted", format_ops: [{ op: "align", value: "center" }] },
]).map((edit) => edit.action), ["replace_paragraph", "format_paragraph"]);

const workstation = {
  exportClauseDecisions: { governing_law: true },
  exportRedlineDecisions: { "redline-2": false },
  redlineDraft: { saved_at: "2026-06-10T10:00:00Z" },
  redlineDraftDirty: true,
  redlineTemplateSelections: { "redline-1": "option-2" },
  reviewClauses: [
    { id: "governing_law", matched_paragraph_ids: ["p1"] },
    { id: "term", matched_paragraph_ids: ["p2"] },
  ],
  reviewParagraphs: [
    { id: "p1", text: "Old law." },
    { id: "p2", text: "Term." },
  ],
  reviewRedlines: [
    {
      action: "replace_paragraph",
      clause_id: "governing_law",
      id: "redline-1",
      paragraph_id: "p1",
      replacement_text: "New law.",
      template_options: [
        { id: "option-1", replacement_text: "First" },
        { id: "option-2", inline_diff_operations: [{ type: "insert", token: "Second" }], replacement_text: "Second" },
      ],
    },
    {
      action: "delete_paragraph",
      clause_id: "term",
      id: "redline-2",
      paragraph_id: "p2",
      replacement_text: "",
    },
  ],
  selectedMatter: { id: "matter-1", review_refresh: { stale: true } },
  selectedReviewClauseId: "governing_law",
};
assert.equal(ReviewWorkstationModel.hasReviewResults(workstation), true);
assert.equal(ReviewWorkstationModel.selectedReviewClause(workstation).id, "governing_law");
assert.equal(ReviewWorkstationModel.selectedReviewParagraph(workstation).id, "p1");
assert.deepEqual(ReviewWorkstationModel.defaultExportClauseDecisions(workstation.reviewClauses, workstation.reviewRedlines), {
  governing_law: true,
  term: true,
});
assert.equal(ReviewWorkstationModel.effectiveReviewRedlines(workstation).length, 1);
assert.equal(ReviewWorkstationModel.effectiveReviewRedlines(workstation)[0].replacement_text, "Second");
const exportStabilityWorkstation = {
  exportClauseDecisions: { server_clause: true },
  exportRedlineDecisions: { "server-redline": true, "manual-redline": false },
  redlineTemplateSelections: { "server-redline": "server-option" },
  reviewRedlines: [
    {
      action: "replace_paragraph",
      clause_id: "server_clause",
      id: "server-redline",
      paragraph_id: "p1",
      replacement_text: "Default server replacement.",
      template_options: [
        { id: "default-option", replacement_text: "Default server replacement." },
        { id: "server-option", inline_diff_operations: [{ type: "insert", token: "Selected" }], replacement_text: "Selected server replacement." },
      ],
    },
    {
      action: "replace_paragraph",
      clause_id: "manual_viewer_edit",
      id: "manual-redline",
      paragraph_id: "p2",
      replacement_text: "Manual replacement.",
      whole_paragraph: false,
    },
  ],
};
assert.deepEqual(ReviewWorkstationModel.effectiveReviewRedlines(exportStabilityWorkstation), [{
  action: "replace_paragraph",
  clause_id: "server_clause",
  id: "server-redline",
  inline_diff_operations: [{ type: "insert", token: "Selected" }],
  paragraph_id: "p1",
  replacement_text: "Selected server replacement.",
  template_options: [
    { id: "default-option", replacement_text: "Default server replacement.", selected: false },
    { id: "server-option", inline_diff_operations: [{ type: "insert", token: "Selected" }], replacement_text: "Selected server replacement.", selected: true },
  ],
}]);
assert.equal(ReviewWorkstationModel.redlineExportIncluded({
  exportClauseDecisions: { server_clause: false },
  exportRedlineDecisions: { "server-redline": true },
}, { clause_id: "server_clause", id: "server-redline" }), true);
assert.equal(ReviewWorkstationModel.redlineExportIncluded({
  exportClauseDecisions: { server_clause: true },
  exportRedlineDecisions: { "server-redline": false },
}, { clause_id: "server_clause", id: "server-redline" }), false);
assert.deepEqual(ReviewWorkstationModel.exportDecisionTransition({ existing: true }, "", false), { existing: true });
assert.deepEqual(ReviewWorkstationModel.exportDecisionTransition({ existing: true }, "server-redline", false), {
  existing: true,
  "server-redline": false,
});
assert.equal(ReviewWorkstationModel.reviewIsStale(workstation), true);
assert.deepEqual(ReviewWorkstationModel.redlineDraftControlState(workstation), {
  canDraft: true,
  discardDisabled: false,
  metaText: "Unsaved redline draft changes",
  saveDisabled: false,
});
assert.deepEqual(ReviewWorkstationModel.nextClauseSelectionState(workstation, "term"), {
  reviewInspectorView: "clause",
  selectedReviewClauseId: "term",
});
assert.equal(ReviewWorkstationModel.selectedBackendRedline(workstation, "p1").id, "redline-1");
assert.equal(ReviewWorkstationModel.gmailSendReadiness({
  blockedLabel: "Needs Review",
  canExport: true,
  hasSendableMatter: true,
  sendBlockReason: "Matter needs human review before a redline can be sent.",
}).label, "Needs Review");
assert.deepEqual(ReviewWorkstationModel.gmailSendReadiness({
  canExport: true,
  hasSendableMatter: true,
}), {
  ariaDisabled: "false",
  canSend: true,
  interactive: true,
  label: "Send Redline",
  title: "Send Redline",
});
assert.deepEqual(ReviewWorkstationModel.gmailSendReadiness({
  canExport: true,
  hasSendableMatter: true,
  staleReview: true,
}), {
  ariaDisabled: "true",
  canSend: false,
  interactive: false,
  label: "Send Redline",
  title: "Refresh review before sending a redline",
});
assert.deepEqual(ReviewWorkstationModel.gmailSendReadiness({
  canExport: false,
  hasSendableMatter: true,
}), {
  ariaDisabled: "true",
  canSend: false,
  interactive: false,
  label: "Send Redline",
  title: "Send Redline",
});
assert.equal(ReviewWorkstationModel.commentComposerState({ hasThreads: false }).scope, "paragraph");
assert.deepEqual(ReviewWorkstationModel.annotationGeometryState({
  page: 0,
  rect: { x: -1, y: 0.25, w: 2, h: 0.1 },
  selectedId: 42,
  tool: "highlight",
}), {
  page: 1,
  rect: { h: 0.1, w: 1, x: 0, y: 0.25 },
  selectedId: "42",
  tool: "highlight",
});
assert.deepEqual(PdfMarkupWorkstation.normalizeRect({ x: -1, y: 0.25, w: 2, h: 0.1 }, "highlight"), {
  h: 0.1,
  w: 1,
  x: 0,
  y: 0.25,
});
assert.deepEqual(PdfMarkupWorkstation.normalizeRect({ x: 0.4, y: 0.5, w: 0.8, h: 0.9 }, "comment"), {
  h: 0,
  w: 0,
  x: 0.4,
  y: 0.5,
});
assert.deepEqual(
  PdfMarkupWorkstation.pointFromClientRect({ clientX: 150, clientY: 240 }, { left: 100, top: 200, width: 200, height: 100 }),
  { x: 0.25, y: 0.4 },
);
assert.deepEqual(PdfMarkupWorkstation.rectFromPoints({ x: 0.8, y: 0.2 }, { x: 0.3, y: 0.6 }), {
  h: 0.39999999999999997,
  w: 0.5,
  x: 0.3,
  y: 0.2,
});
assert.equal(PdfMarkupWorkstation.dragHasDrawableArea({ w: 0.02, h: 0.02 }), true);
assert.equal(PdfMarkupWorkstation.dragHasDrawableArea({ w: 0.02, h: 0.005 }), false);
assert.deepEqual(PdfMarkupWorkstation.overlayStyle({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 }, { width: 200, height: 100 }), {
  height: "40px",
  left: "20px",
  top: "20px",
  width: "60px",
});
let markupState = PdfMarkupWorkstation.createInitialState();
markupState = PdfMarkupWorkstation.setActiveTool(markupState, "highlight");
assert.equal(markupState.activeTool, "highlight");
assert.equal(PdfMarkupWorkstation.toolIsDrawing(markupState.activeTool), true);
markupState = PdfMarkupWorkstation.startLoad(markupState, "matter-1");
assert.equal(markupState.loadSequence, 1);
markupState = PdfMarkupWorkstation.completeLoad(markupState, "matter-1", 1, [
  { id: "ann-1", page: "2", type: "comment", rect: { x: 0.2, y: 0.3, w: 0.7, h: 0.8 }, text: "Note" },
  { id: "bad", page: 1, type: "scribble", rect: {} },
]);
assert.deepEqual(markupState.annotations, [{
  id: "ann-1",
  page: 2,
  rect: { h: 0, w: 0, x: 0.2, y: 0.3 },
  text: "Note",
  type: "comment",
}]);
markupState = PdfMarkupWorkstation.appendAnnotation(markupState, {
  id: "ann-2",
  page: 1,
  rect: { h: 0.2, w: 0.4, x: 0.1, y: 0.1 },
  type: "highlight",
});
assert.equal(markupState.annotations.length, 2);
markupState = PdfMarkupWorkstation.togglePopover(markupState, "ann-1");
assert.equal(markupState.openPopoverId, "ann-1");
markupState = PdfMarkupWorkstation.removeAnnotation(markupState, "ann-1");
assert.equal(markupState.openPopoverId, null);
assert.deepEqual(markupState.annotations.map((annotation) => annotation.id), ["ann-2"]);
assert.deepEqual(PdfMarkupWorkstation.annotationPayload({
  page: "1",
  rect: { x: -1, y: 0.5, w: 2, h: 0 },
  text: "Comment",
  type: "comment",
}), {
  page: 1,
  rect: { h: 0, w: 0, x: 0, y: 0.5 },
  text: "Comment",
  type: "comment",
});
assert.equal(PdfMarkupWorkstation.annotationPayload({ page: 1, rect: {}, type: "scribble" }), null);
assert.equal(
  PdfMarkupWorkstation.markedUpFilename({ matterId: "matter-1", selectedMatter: { source_filename: "Counterparty NDA.final.pdf" } }),
  "Counterparty-NDA-final-marked-up.pdf",
);

const matter = {
  can_send_redline: true,
  recipient_email: "sender@example.com",
  review_result: { overall_status: "needs_review", requirements_needs_review: 1 },
};
assert.equal(needsHumanReview(matter), true);
assert.equal(MatterUtils.recipientEmail(matter), "sender@example.com");
assert.equal(gmailSendBlock(matter), "Matter needs human review before a redline can be sent.");
assert.equal(gmailSendButtonLabel("Matter needs human review before a redline can be sent."), "Needs Review");
assert.equal(counterpartyEmail({
  gmail_account: "me@example.com",
  sender: "Me <me@example.com>",
  reply_to: "Counterparty <counterparty@example.com>",
}), "counterparty@example.com");

// reviewStale: reads the list-level flag and the opened-review review_refresh.
assert.equal(reviewStale({}), false);
assert.equal(reviewStale({ review_stale: true }), true);
assert.equal(reviewStale({ review_refresh: { stale: true } }), true);
assert.equal(reviewStale({ review_refresh: { stale: false } }), false);
assert.deepEqual(reviewStaleReasons({ review_refresh: { stale_reasons: ["playbook_changed"] } }), ["playbook_changed"]);
assert.deepEqual(reviewStaleReasons({ review_stale_reasons: ["review_engine_version_changed"] }), ["review_engine_version_changed"]);
assert.deepEqual(reviewStaleReasons({}), []);
// reviewStaleLabel: prefers explicit message, else maps reasons, else generic.
assert.equal(reviewStaleLabel({}), "");
assert.equal(
  reviewStaleLabel({ review_refresh: { stale: true, stale_message: "Custom stale copy." } }),
  "Custom stale copy.",
);
assert.equal(
  reviewStaleLabel({ review_stale: true, review_stale_reasons: ["playbook_changed"] }),
  "Active Playbook changed since this review. Refresh before exporting or sending.",
);
assert.equal(
  reviewStaleLabel({ review_refresh: { stale: true, stale_reasons: ["review_engine_version_changed"] } }),
  "Review engine changed since this review. Refresh before exporting or sending.",
);
assert.equal(
  reviewStaleLabel({ review_stale: true }),
  "Review is out of date. Refresh against the active Playbook.",
);
assert.equal(MatterUtils.reviewStale({ review_stale: true }), true);

const calls = [];
const repositoryApi = createRepositoryApi({
  fetchImpl: async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/api/gmail/status") return jsonResponse({ gmail: { inbound: { ready: true } } });
    if (url === "/api/matters") return jsonResponse({ matters: [{ id: "matter-1" }] });
    if (url === "/api/matters/matter%20one/review") {
      return jsonResponse({
        extracted_text: "Contract text",
        matter: { id: "matter one" },
        review_result: { clauses: [] },
      });
    }
    if (url === "/api/matters/matter%20one/review-refresh") {
      return jsonResponse({
        extracted_text: "Refreshed contract text",
        matter: { id: "matter one" },
        review_refresh: { refreshed: true, stale: false },
        review_result: { clauses: [{ id: "mutuality" }] },
      });
    }
    if (url === "/api/matters/matter%20one/stage") return jsonResponse({ matter: { id: "matter one", board_column: "in_review" } });
    if (url === "/api/gmail/send-redline") return jsonResponse({ sent: true });
    if (url === "/api/gmail/import") return jsonResponse({ result: { imported: [{ id: "matter-2" }] } });
    return jsonResponse({ error: "not found" }, { ok: false });
  },
  reviewErrorFromPayload: (payload, fallback) => new Error(payload.error || fallback),
});
assert.deepEqual(await repositoryApi.loadGmailStatus(), { inbound: { ready: true } });
assert.deepEqual(await repositoryApi.listMatters(), [{ id: "matter-1" }]);
assert.deepEqual(await repositoryApi.getMatterReview("matter one"), {
  id: "matter one",
  extracted_text: "Contract text",
  redline_draft: null,
  review_refresh: null,
  review_result: { clauses: [] },
});
assert.deepEqual(await repositoryApi.getMatterReview("matter one", { refresh: true }), {
  id: "matter one",
  extracted_text: "Refreshed contract text",
  redline_draft: null,
  review_refresh: { refreshed: true, stale: false },
  review_result: { clauses: [{ id: "mutuality" }] },
});
assert.deepEqual(await repositoryApi.moveMatterToColumn("matter one", "in_review"), { id: "matter one", board_column: "in_review" });
assert.deepEqual(await repositoryApi.sendRedline({ matter_id: "matter-1", confirm_send: true }), { sent: true });
assert.deepEqual(await repositoryApi.syncGmail({ limit: 2 }), { result: { imported: [{ id: "matter-2" }] } });
assert.equal(calls[3].url, "/api/matters/matter%20one/review-refresh");
assert.equal(calls[3].options.method, "POST");
assert.equal(calls[calls.length - 1].url, "/api/gmail/import");
assert.equal(calls[calls.length - 1].options.method, "POST");
assert.deepEqual(JSON.parse(calls[calls.length - 1].options.body), { limit: 2 });
assert.equal(calls[4].options.method, "POST");
assert.deepEqual(JSON.parse(calls[4].options.body), { board_column: "in_review" });
assert.deepEqual(JSON.parse(calls[5].options.body), { matter_id: "matter-1", confirm_send: true });

// --- Playbook draft/publish state helpers ---

// shortHash truncates long hashes, strips algorithm prefixes, tolerates missing.
assert.equal(shortHash("a1b2c3d4e5f6"), "a1b2c3d4");
assert.equal(shortHash("abc123"), "abc123");
assert.equal(shortHash("sha256:e2e59c8ed770abc123"), "e2e59c8e");
assert.equal(shortHash(null), "");
assert.equal(shortHash(undefined), "");

// versionOf / hashOf read the backend's nested metadata, with flat fallback.
assert.equal(versionOf({ metadata: { active_version_id: "pbv_9" } }), "pbv_9");
assert.equal(versionOf({ metadata: { draft_id: "drf_3" } }), "drf_3");
assert.equal(hashOf({ metadata: { active_hash: "abc12345def" } }), "abc12345def");
assert.equal(hashOf({ metadata: { draft_hash: "draft999aa" } }), "draft999aa");
assert.equal(versionOf({ version: 4 }), 4);
assert.equal(hashOf({ hash: "flat1234" }), "flat1234");

// versionLabel combines version + short hash from metadata, tolerant of gaps.
// Numeric versions get a "v" prefix; string ids (e.g. "pbv_8") show verbatim.
assert.equal(versionLabel({ metadata: { active_version_id: 4, active_hash: "a1b2c3d4e5f6" } }), "v4 · a1b2c3d4");
assert.equal(versionLabel({ metadata: { draft_id: 7 } }), "v7");
assert.equal(versionLabel({ metadata: { active_version_id: "pbv_8", active_hash: "draft888aa" } }), "pbv_8 · draft888");
assert.equal(versionLabel({ metadata: { active_version_id: "12", active_hash: "abc" } }), "v12 · abc");
assert.equal(versionLabel({ metadata: { draft_hash: "deadbeefcafe" } }), "deadbeef");
assert.equal(versionLabel({ metadata: {} }), "");
assert.equal(versionLabel(null), "");

// --- Human-readable version labels (task #17) ---
// versionTimestamp prefers the backend ISO field, falls back to the id timestamp.
const publishedIso = "2026-06-04T23:09:58.581923+00:00";
const activeBlockWithDate = { metadata: { active_version_id: "pbv_20260604T230958581923Z_e2e59c8ed770", active_hash: "sha256:e2e59c8ed770aa", published_at: publishedIso } };
assert.equal(versionTimestamp(activeBlockWithDate).toISOString(), new Date(publishedIso).toISOString());
// Falls back to the timestamp embedded in a pbv_ id when no ISO field is present.
const idOnlyBlock = { metadata: { active_version_id: "pbv_20260604T230958581923Z_e2e59c8ed770" } };
assert.equal(versionTimestamp(idOnlyBlock).toISOString(), "2026-06-04T23:09:58.581Z");
// No timestamp anywhere → null.
assert.equal(versionTimestamp({ metadata: { active_version_id: "pbv_legacy" } }), null);
assert.equal(versionTimestamp(null), null);

// formatVersionDateTime produces a friendly absolute date; "" for bad input.
// Compare against the same locale call so the test is timezone-independent.
const expectedFriendly = new Date(publishedIso).toLocaleString(undefined, {
  year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
});
assert.equal(formatVersionDateTime(publishedIso), expectedFriendly);
assert.equal(formatVersionDateTime("not a date"), "");
assert.equal(formatVersionDateTime(null), "");

// friendlyVersionLabel: "Published <date>" / "Draft saved <date>".
assert.equal(friendlyVersionLabel(activeBlockWithDate, "active"), `Published ${expectedFriendly}`);
const draftBlockWithDate = { metadata: { draft_id: "pbd_x", draft_updated_at: publishedIso } };
assert.equal(friendlyVersionLabel(draftBlockWithDate, "draft"), `Draft saved ${expectedFriendly}`);
// No timestamp but a semver → "Version <semver>" / "Draft (v<semver>)".
assert.equal(friendlyVersionLabel({ metadata: { playbook_version: "0.1.0" } }, "active"), "Version 0.1.0");
assert.equal(friendlyVersionLabel({ metadata: { playbook_version: "0.1.0" } }, "draft"), "Draft (v0.1.0)");
// Empty block → friendly empty-state copy, never the raw id.
assert.equal(friendlyVersionLabel({ metadata: {} }, "active"), "Not yet published");
assert.equal(friendlyVersionLabel({ metadata: {} }, "draft"), "No saved draft yet");
// The raw id is available for tooltips but not the headline.
assert.equal(rawVersionId(activeBlockWithDate), "pbv_20260604T230958581923Z_e2e59c8ed770");
assert.equal(rawVersionId({ metadata: {} }), "");

// normalizePlaybookResponse: {active, draft, history} with nested metadata.
const normNew = normalizePlaybookResponse({
  active: { playbook: { clauses: [{ id: "a" }] }, metadata: { active_version_id: "pbv_3", active_hash: "active11" } },
  draft: {
    playbook: { clauses: [{ id: "a" }, { id: "b" }] },
    metadata: { draft_id: "drf_4", draft_hash: "draft222" },
    has_unpublished_changes: true,
  },
  history: [{ id: "h1" }],
});
assert.deepEqual(clausesOf(normNew.active), [{ id: "a" }]);
assert.deepEqual(clausesOf(normNew.draft), [{ id: "a" }, { id: "b" }]);
assert.equal(versionOf(normNew.active), "pbv_3");
assert.equal(normNew.draft.has_unpublished_changes, true);
assert.deepEqual(normNew.history, [{ id: "h1" }]);

// normalizePlaybookResponse: draft null → active becomes the draft baseline.
const normNoDraft = normalizePlaybookResponse({
  active: { playbook: { clauses: [{ id: "a" }] }, metadata: { active_version_id: "pbv_3", active_hash: "active11" } },
  draft: null,
  history: [],
});
assert.deepEqual(clausesOf(normNoDraft.draft), [{ id: "a" }]);
assert.equal(hashOf(normNoDraft.draft), "active11");
assert.equal(draftDiffersFromActive(normNoDraft.draft, normNoDraft.active), false);

// normalizePlaybookResponse: legacy {playbook, history} → active==draft baseline.
const normLegacy = normalizePlaybookResponse({ playbook: { clauses: [{ id: "x" }] }, history: [] });
assert.deepEqual(clausesOf(normLegacy.active), [{ id: "x" }]);
assert.deepEqual(clausesOf(normLegacy.draft), [{ id: "x" }]);

// normalizePlaybookResponse: empty/garbage payload degrades to empty blocks.
const normEmpty = normalizePlaybookResponse(null);
assert.deepEqual(clausesOf(normEmpty.active), []);
assert.deepEqual(clausesOf(normEmpty.draft), []);
assert.deepEqual(normEmpty.history, []);

// isWorkingDirty: working clauses vs saved draft clauses.
const draftBlock = { playbook: { clauses: [{ id: "a", name: "Alpha" }] } };
assert.equal(isWorkingDirty([{ id: "a", name: "Alpha" }], draftBlock), false);
assert.equal(isWorkingDirty([{ id: "a", name: "Alpha edited" }], draftBlock), true);

// draftDiffersFromActive: explicit flag wins, else metadata hash, else clauses.
assert.equal(draftDiffersFromActive({ has_unpublished_changes: true }, {}), true);
assert.equal(draftDiffersFromActive({ has_unpublished_changes: false }, {}), false);
assert.equal(
  draftDiffersFromActive({ metadata: { draft_hash: "aaa" } }, { metadata: { active_hash: "bbb" } }),
  true,
);
assert.equal(
  draftDiffersFromActive({ metadata: { draft_hash: "same" } }, { metadata: { active_hash: "same" } }),
  false,
);
assert.equal(
  draftDiffersFromActive(
    { playbook: { clauses: [{ id: "a" }, { id: "b" }] } },
    { playbook: { clauses: [{ id: "a" }] } },
  ),
  true,
);

// normalizeValidation: backend {location, clause, field, message, severity}.
const valOk = normalizeValidation({ valid: true, errors: [] });
assert.equal(valOk.valid, true);
assert.deepEqual(valOk.errors, []);
const valErr = normalizeValidation({
  valid: false,
  errors: [
    { location: "mutuality.name", clause: "mutuality", field: "name", message: "Name is required", severity: "error" },
    "Free-form problem",
  ],
});
assert.equal(valErr.valid, false);
assert.deepEqual(valErr.errors[0], { message: "Name is required", clause_id: "mutuality", field: "name", code: "error" });
assert.deepEqual(valErr.errors[1], { message: "Free-form problem" });
// Also accepts clause_id/code aliases.
assert.deepEqual(
  normalizeValidation({ errors: [{ clause_id: "term", field: "max_term_years", code: "required", message: "Bad" }] }).errors[0],
  { message: "Bad", clause_id: "term", field: "max_term_years", code: "required" },
);
// Errors present but valid flag missing → treated as invalid.
assert.equal(normalizeValidation({ errors: [{ message: "x" }] }).valid, false);
// No errors and no flag → valid.
assert.equal(normalizeValidation({}).valid, true);
// Bare array of errors.
assert.equal(normalizeValidation(["broken"]).valid, false);
// Layer-2 advisory warnings are normalized into a separate list and NEVER affect
// `valid` (they carry check_id + confidence from the semantic lint).
const valWithWarnings = normalizeValidation({
  valid: true,
  errors: [],
  warnings: [
    { location: "term_and_survival", clause: "term_and_survival", field: null, message: "Prose mandates a 3-year cap that no rule enforces.", severity: "warning", check_id: "prose_mandate_unenforced", confidence: 0.82 },
  ],
});
assert.equal(valWithWarnings.valid, true);
assert.deepEqual(valWithWarnings.warnings, [
  { message: "Prose mandates a 3-year cap that no rule enforces.", clause_id: "term_and_survival", code: "warning", check_id: "prose_mandate_unenforced", confidence: 0.82 },
]);
// Warnings present alongside blocking errors: errors still drive `valid: false`.
const valErrAndWarn = normalizeValidation({
  valid: false,
  errors: [{ clause: "mutuality", field: "name", message: "Name is required", severity: "error" }],
  warnings: [{ clause: "mutuality", message: "Redline contradicts the requirement.", severity: "warning", confidence: 0.7 }],
});
assert.equal(valErrAndWarn.valid, false);
assert.equal(valErrAndWarn.errors.length, 1);
assert.equal(valErrAndWarn.warnings.length, 1);
// Missing/empty warnings key → empty list.
assert.deepEqual(normalizeValidation({ valid: true, errors: [] }).warnings, []);

// validationSummary: pluralization + valid case.
assert.equal(validationSummary({ valid: true, errors: [] }), "Draft is valid.");
assert.equal(validationSummary({ valid: false, errors: [{ message: "a" }] }), "1 validation issue found.");
assert.equal(validationSummary({ valid: false, errors: [{ message: "a" }, { message: "b" }] }), "2 validation issues found.");

// --- Playbook browser authoring model ---
assert.equal(PlaybookAuthoringModel.resolveActivePanel({
  clauseId: "mutuality",
  mutualityPanel: "redline",
}), "redline");
assert.equal(PlaybookAuthoringModel.resolveActivePanel({
  clauseId: "mutuality",
  mutualityPanel: "unknown",
}), "policy");
assert.equal(PlaybookAuthoringModel.resolveActivePanel({
  clauseId: "governing_law",
  panelState: { governing_law: "audit" },
  mutualityPanel: "redline",
}), "audit");
assert.deepEqual(PlaybookAuthoringModel.setClausePanel({
  clauseId: "mutuality",
  panel: "decision",
  panelState: { governing_law: "audit" },
  mutualityPanel: "policy",
}), {
  activePanel: "decision",
  mutualityPanel: "decision",
  panelState: { governing_law: "audit", mutuality: "decision" },
});
assert.deepEqual(PlaybookAuthoringModel.setClausePanel({
  clauseId: "mutuality",
  panel: "unsupported",
  panelState: {},
  mutualityPanel: "redline",
}), {
  activePanel: "policy",
  mutualityPanel: "policy",
  panelState: { mutuality: "policy" },
});
assert.deepEqual(PlaybookAuthoringModel.draftStatus({ hasUnsavedChanges: true, draftAhead: true }), {
  note: "Unsaved changes - Save Draft to keep them.",
  showDirtyDot: true,
  state: "editing",
});
assert.deepEqual(PlaybookAuthoringModel.draftStatus({ draftAhead: true }), {
  note: "Saved draft is ahead of the active version - Publish to make it live.",
  showDirtyDot: false,
  state: "ahead",
});
assert.deepEqual(PlaybookAuthoringModel.draftStatus(), {
  note: "Matches the active published version.",
  showDirtyDot: false,
  state: "in-sync",
});
assert.equal(PlaybookAuthoringModel.canPublishDraft({ draftAhead: true }), true);
assert.equal(PlaybookAuthoringModel.canPublishDraft({ draftAhead: true, hasUnsavedChanges: true }), false);
assert.equal(PlaybookAuthoringModel.canPublishDraft({ draftAhead: true, hasTemplateValidationErrors: true }), false);
assert.equal(PlaybookAuthoringModel.canPublishDraft({ draftAhead: true, validation: { valid: false } }), false);
assert.equal(PlaybookAuthoringModel.canPublishDraft({ draftAhead: true, runtimeReady: false }), false);
assert.equal(PlaybookAuthoringModel.shouldInvalidateValidation({
  validation: { valid: true },
  hasUnsavedChanges: true,
}), true);
assert.deepEqual(PlaybookAuthoringModel.validationView(null), {
  errors: [],
  warnings: [],
  hidden: true,
  state: "idle",
  title: "",
});
assert.deepEqual(PlaybookAuthoringModel.validationView({ valid: true, errors: [] }), {
  errors: [],
  warnings: [],
  hidden: false,
  state: "valid",
  title: "Draft passed validation.",
});
assert.deepEqual(PlaybookAuthoringModel.validationView({
  valid: false,
  errors: [{ message: "Name is required" }],
}), {
  errors: [{ message: "Name is required" }],
  warnings: [],
  hidden: false,
  state: "invalid",
  title: "Resolve this issue before publishing:",
});
// Layer-2 advisory warnings ride through in BOTH states without changing `state`:
// a valid draft can still carry advisory warnings.
assert.deepEqual(PlaybookAuthoringModel.validationView({
  valid: true,
  errors: [],
  warnings: [{ message: "Prose mandates a cap no rule enforces.", clause_id: "term_and_survival", confidence: 0.8 }],
}), {
  errors: [],
  warnings: [{ message: "Prose mandates a cap no rule enforces.", clause_id: "term_and_survival", confidence: 0.8 }],
  hidden: false,
  state: "valid",
  title: "Draft passed validation.",
});
assert.deepEqual(PlaybookAuthoringModel.validationView({
  valid: false,
  errors: [{ message: "Name is required" }],
  warnings: [{ message: "Redline contradicts the requirement.", clause_id: "mutuality" }],
}), {
  errors: [{ message: "Name is required" }],
  warnings: [{ message: "Redline contradicts the requirement.", clause_id: "mutuality" }],
  hidden: false,
  state: "invalid",
  title: "Resolve this issue before publishing:",
});
assert.deepEqual(PlaybookAuthoringModel.actionAvailability({
  clauseHasDraft: true,
  hasUnsavedChanges: true,
  canPublish: false,
}), {
  discardDisabled: false,
  publishDisabled: true,
  saveDisabled: false,
});
assert.deepEqual(PlaybookAuthoringModel.actionAvailability({
  clauseHasDraft: false,
  hasUnsavedChanges: true,
  hasTemplateValidationErrors: true,
  canPublish: true,
}), {
  discardDisabled: true,
  publishDisabled: false,
  saveDisabled: true,
});

// --- Playbook draft/publish API wrapper (real endpoint contract) ---
const playbookCalls = [];
const blockWith = (idKey, idVal, hashKey, hashVal) => ({ playbook: {}, metadata: { [idKey]: idVal, [hashKey]: hashVal } });
const playbookApi = createPlaybookApi({
  fetchImpl: async (url, options = {}) => {
    playbookCalls.push({ url, options });
    if (url === "/api/playbook/draft" && (!options.method || options.method === "GET")) {
      return jsonResponse({ active: blockWith("active_version_id", "pbv_1", "active_hash", "act11111"), draft: null, history: [] });
    }
    if (url === "/api/playbook/draft") return jsonResponse({ draft: blockWith("draft_id", "drf_3", "draft_hash", "drf33333") });
    if (url === "/api/playbook/validate-draft") return jsonResponse({ valid: true, errors: [] });
    if (url === "/api/playbook/publish") return jsonResponse({ active: blockWith("active_version_id", "pbv_3", "active_hash", "drf33333"), draft: null });
    if (url === "/api/playbook/discard-draft") return jsonResponse({ active: blockWith("active_version_id", "pbv_1", "active_hash", "act11111"), draft: null });
    if (url === "/api/playbook/restore") return jsonResponse({ active: blockWith("active_version_id", "pbv_4", "active_hash", "rst44444"), draft: null });
    return jsonResponse({ error: "not found" }, { ok: false });
  },
});
const samplePlaybook = { clauses: [{ id: "a", name: "Alpha" }] };
const activeMeta = { active_version_id: "pbv_1", active_hash: "act11111" };
await playbookApi.loadPlaybook();
await playbookApi.saveDraft(samplePlaybook, { activeMeta });
await playbookApi.validateDraft(samplePlaybook);
await playbookApi.publishPlaybook(samplePlaybook, { activeMeta });
await playbookApi.discardDraft({ draftId: "drf_3" });
await playbookApi.restoreVersion("hist-1", "admin");
// loadPlaybook GETs the draft endpoint.
assert.equal(playbookCalls[0].url, "/api/playbook/draft");
assert.ok(!playbookCalls[0].options.method || playbookCalls[0].options.method === "GET");
// saveDraft POSTs the playbook + optimistic-concurrency hints.
assert.equal(playbookCalls[1].url, "/api/playbook/draft");
assert.equal(playbookCalls[1].options.method, "POST");
assert.deepEqual(JSON.parse(playbookCalls[1].options.body), {
  playbook: samplePlaybook,
  expected_active_version_id: "pbv_1",
  expected_active_hash: "act11111",
});
// validate POSTs to /validate-draft.
assert.equal(playbookCalls[2].url, "/api/playbook/validate-draft");
assert.equal(playbookCalls[2].options.method, "POST");
assert.deepEqual(JSON.parse(playbookCalls[2].options.body), { playbook: samplePlaybook });
// publish POSTs playbook + actor + concurrency hints.
assert.equal(playbookCalls[3].url, "/api/playbook/publish");
assert.deepEqual(JSON.parse(playbookCalls[3].options.body), {
  playbook: samplePlaybook,
  actor: "admin",
  expected_active_version_id: "pbv_1",
  expected_active_hash: "act11111",
});
// discard POSTs the draft id.
assert.equal(playbookCalls[4].url, "/api/playbook/discard-draft");
assert.deepEqual(JSON.parse(playbookCalls[4].options.body), { draft_id: "drf_3" });
// restore POSTs history_id + actor.
assert.equal(playbookCalls[5].url, "/api/playbook/restore");
assert.deepEqual(JSON.parse(playbookCalls[5].options.body), { history_id: "hist-1", actor: "admin" });
// Failed request surfaces the backend error message.
await assert.rejects(
  createPlaybookApi({ fetchImpl: async () => jsonResponse({ error: "boom" }, { ok: false }) }).saveDraft({}),
  /boom/,
);

// --- Send Document module ---
assert.equal(isSupportedSendFilename("Engagement Letter.docx"), true);
assert.equal(isSupportedSendFilename("Engagement Letter.DOCX"), true);
assert.equal(isSupportedSendFilename("contract.pdf"), false);
assert.equal(isSupportedSendFilename(""), false);

assert.equal(isValidRecipientEmail("counterparty@example.com"), true);
assert.equal(isValidRecipientEmail("  counterparty@example.com  "), true);
assert.equal(isValidRecipientEmail("not-an-email"), false);
assert.equal(isValidRecipientEmail(""), false);

assert.deepEqual(
  validateSendDocument({ filename: "Doc.docx", hasFile: true, recipient: "to@example.com" }),
  { ok: true, error: "" },
);
assert.equal(validateSendDocument({ filename: "Doc.docx", hasFile: false, recipient: "to@example.com" }).ok, false);
assert.equal(validateSendDocument({ filename: "Doc.pdf", hasFile: true, recipient: "to@example.com" }).ok, false);
assert.equal(validateSendDocument({ filename: "Doc.docx", hasFile: true, recipient: "bad" }).ok, false);

assert.deepEqual(
  buildSendDocumentPayload({
    filename: "Engagement Letter.docx",
    contentBase64: "QUJD",
    recipient: "  to@example.com  ",
    subject: "  Custom subject  ",
    body: "  Please review.  ",
  }),
  {
    filename: "Engagement Letter.docx",
    content_base64: "QUJD",
    to: "to@example.com",
    subject: "Custom subject",
    body: "Please review.",
  },
);
// Empty subject falls back to the file stem; empty body is omitted.
assert.deepEqual(
  buildSendDocumentPayload({ filename: "Engagement Letter.docx", contentBase64: "QUJD", recipient: "to@example.com" }),
  {
    filename: "Engagement Letter.docx",
    content_base64: "QUJD",
    to: "to@example.com",
    subject: "Engagement Letter",
  },
);

// --- Dashboard greeting name resolution ---
// firstNameFromEmail derives a title-cased first name from the local-part.
assert.equal(firstNameFromEmail("daniyal.ahmad@aspora.com"), "Daniyal");
assert.equal(firstNameFromEmail("john_smith@x.io"), "John");
assert.equal(firstNameFromEmail("jane-doe+newsletter@x.io"), "Jane");
assert.equal(firstNameFromEmail("o'brien@x.io"), "O'Brien");
assert.equal(firstNameFromEmail("jdoe@x.io"), "Jdoe");
assert.equal(firstNameFromEmail("12345@x.io"), "");
assert.equal(firstNameFromEmail("not-an-email"), "");
assert.equal(firstNameFromEmail(""), "");

// firstNameFromDisplayName ignores names that just echo the email/id.
assert.equal(firstNameFromDisplayName("Daniyal Ahmad"), "Daniyal");
assert.equal(firstNameFromDisplayName("daniyal.ahmad@aspora.com"), "");
assert.equal(firstNameFromDisplayName("user-123", { id: "user-123" }), "");
assert.equal(firstNameFromDisplayName("me@x.io", { email: "me@x.io" }), "");
assert.equal(firstNameFromDisplayName(""), "");

// resolveFirstName priority: real display name > user email > gmail email.
assert.equal(resolveFirstName({ user: { name: "Alex Park", email: "alex@x.io" } }), "Alex");
assert.equal(resolveFirstName({ user: { name: "u@x.io", email: "u@x.io" }, gmailStatus: { inbound: { email: "priya.nair@x.io" } } }), "Priya");
assert.equal(resolveFirstName({ gmailStatus: { outbound: { email: "daniyal.ahmad@aspora.com" } } }), "Daniyal");
assert.equal(resolveFirstName({}), "");

// dashboardGreeting: "Welcome back, <Name>" or a placeholder-free fallback (never "Counsel").
assert.equal(dashboardGreeting({ gmailStatus: { inbound: { email: "daniyal.ahmad@aspora.com" } } }), "Welcome back, Daniyal");
assert.equal(dashboardGreeting({ user: { name: "Sam Lee" } }), "Welcome back, Sam");
assert.equal(dashboardGreeting({}), "Welcome back");
assert.equal(dashboardGreeting({ user: null, gmailStatus: null }), "Welcome back");
assert.ok(!dashboardGreeting({}).includes("Counsel"));

function jsonResponse(payload, { ok = true } = {}) {
  return {
    ok,
    json: async () => payload,
  };
}

// --- Outbound-draft intake: entity picker bundle-prefill + law override ---
//
// The registry mirrors nda_automation/entity_registry.py field-for-field: the
// same entity ids, the {playbook_option_id,label} governing-law bundle, and the
// {id,label,lines,country,default} address shape. These tests pin that contract
// so the embedded copy can never drift from entity-model's source of truth.

// Our signing entities, each a coupled bundle. Mirrors the seven bundles in
// nda_automation/entity_registry.py (the embedded copy was expanded to match the
// registry's full roster).
assert.equal(SIGNING_ENTITIES.length, 7);
assert.deepEqual(
  SIGNING_ENTITIES.map((entity) => entity.id),
  [
    "aspora_technology",
    "vance_money",
    "real_transfer",
    "vance_techlabs",
    "nesse_technologies",
    "vance_technologies",
    "aspora_financial_services",
  ],
);
for (const entity of SIGNING_ENTITIES) {
  assert.ok(entity.id && entity.legal_name, "entity has id + legal name");
  assert.ok(
    entity.governing_law?.playbook_option_id && entity.governing_law?.label,
    "entity law carries playbook_option_id + label",
  );
  assert.ok(Array.isArray(entity.addresses) && entity.addresses.length >= 1, "entity has >=1 address");
  // Exactly one default address per entity (matches the Python validator).
  assert.equal(entity.addresses.filter((address) => address.default).length, 1);
  assert.ok(defaultAddressFor(entity), "entity resolves a default address");
}

// Exactly one entity (Real Transfer) carries two addresses; its default is the
// London corporate office, the alternate is the Belfast registered office.
const multiAddressEntities = SIGNING_ENTITIES.filter(hasMultipleAddresses);
assert.equal(multiAddressEntities.length, 1);
assert.equal(multiAddressEntities[0].id, "real_transfer");
assert.equal(multiAddressEntities[0].addresses.length, 2);
assert.equal(defaultAddressFor(multiAddressEntities[0]).id, "corporate");

// governingLawOptions is the distinct set of laws across the entities — every
// option id is a playbook governing_law approved_option id, and the override
// dropdown can never offer a law that no entity defines.
const lawOptions = governingLawOptions();
const lawIds = lawOptions.map((law) => law.id);
assert.deepEqual(new Set(lawIds), new Set(["india", "delaware", "england_and_wales", "difc", "ontario_canada"]));
assert.deepEqual(new Set(lawIds).size, lawIds.length, "law options are de-duplicated");
for (const entity of SIGNING_ENTITIES) {
  assert.ok(lawIds.includes(entity.governing_law.playbook_option_id), "every entity law is offered");
}

// Picking an entity pre-fills the coupled bundle: address defaults, law couples.
const indiaPick = applyEntitySelection(createInitialIntake(), "aspora_technology");
assert.equal(indiaPick.entityId, "aspora_technology");
assert.equal(indiaPick.addressId, "registered");
assert.equal(indiaPick.governingLawId, "india");
assert.equal(indiaPick.governingLawOverridden, false);
assert.equal(effectiveGoverningLaw(indiaPick).label, "India");

// Re-picking a DIFFERENT entity moves the whole bundle together — you cannot end
// up with the US (Vance Money) entity still bound to India.
const usPick = applyEntitySelection(indiaPick, "vance_money");
assert.equal(usPick.entityId, "vance_money");
assert.equal(usPick.governingLawId, "delaware");
assert.equal(effectiveGoverningLaw(usPick).label, "Delaware");

// The escape hatch: override the governing law independently of the entity.
const overridden = setGoverningLawOverride(usPick, "difc");
assert.equal(overridden.governingLawId, "difc");
assert.equal(overridden.governingLawOverridden, true);
assert.equal(effectiveGoverningLaw(overridden).label, "DIFC");
// The entity itself is untouched by a law override.
assert.equal(overridden.entityId, "vance_money");

// Once overridden, re-picking an entity preserves the user's chosen law (the
// whole point of an independent override) but still moves the address bundle.
const repickAfterOverride = applyEntitySelection(overridden, "real_transfer");
assert.equal(repickAfterOverride.entityId, "real_transfer");
assert.equal(repickAfterOverride.addressId, "corporate");
assert.equal(repickAfterOverride.governingLawId, "difc", "override survives an entity re-pick");
assert.equal(repickAfterOverride.governingLawOverridden, true);

// Clearing the override re-couples the law to the current entity's law.
const recoupled = clearGoverningLawOverride(repickAfterOverride);
assert.equal(recoupled.governingLawOverridden, false);
assert.equal(recoupled.governingLawId, "england_and_wales");
assert.equal(effectiveGoverningLaw(recoupled).label, "England and Wales");

// The two-address entity: default address is the London corporate office, and
// the user can switch to the Belfast registered office.
const rtPick = applyEntitySelection(createInitialIntake(), "real_transfer");
assert.equal(rtPick.addressId, "corporate");
assert.equal(selectedAddress(rtPick).label, "Corporate office");
const rtRegistered = selectAddress(rtPick, "registered");
assert.equal(rtRegistered.addressId, "registered");
assert.equal(selectedAddress(rtRegistered).label, "Registered office");
assert.ok(formatAddressLines(selectedAddress(rtRegistered)).includes("Belfast"));

// selectAddress ignores an address id that does not belong to the picked entity
// (e.g. a "corporate" id on a single-address entity).
const indiaCorporateAttempt = selectAddress(indiaPick, "corporate");
assert.equal(indiaCorporateAttempt.addressId, "registered", "foreign address id is ignored");

// --- Validation ---
assert.equal(validateDraftIntake(createInitialIntake()).ok, false);
assert.match(validateDraftIntake(createInitialIntake()).error, /counterparty name/i);
assert.equal(
  validateDraftIntake({ ...createInitialIntake(), counterpartyName: "Acme Co" }).ok,
  false,
  "entity is required",
);
assert.match(
  validateDraftIntake({ ...createInitialIntake(), counterpartyName: "Acme Co" }).error,
  /signing entity/i,
);
const validIntake = applyEntitySelection(
  { ...createInitialIntake(), counterpartyName: "Acme Co" },
  "aspora_technology",
);
assert.equal(validateDraftIntake(validIntake).ok, true);
// A malformed email blocks; a blank email is allowed.
assert.equal(validateDraftIntake({ ...validIntake, counterpartyEmail: "not-an-email" }).ok, false);
assert.equal(validateDraftIntake({ ...validIntake, counterpartyEmail: "" }).ok, true);
assert.equal(validateDraftIntake({ ...validIntake, counterpartyEmail: "deals@acme.com" }).ok, true);

// --- Payload: the signing-entity bundle travels as one coupled unit, with the
// playbook_option_id join key preserved for downstream generation. ---
const payload = buildDraftPayload({
  ...validIntake,
  counterpartyEmail: "deals@acme.com",
  projectPurpose: "Series B diligence",
  term: "2 years",
});
assert.equal(payload.counterparty.name, "Acme Co");
assert.equal(payload.counterparty.email, "deals@acme.com");
assert.equal(payload.nda_type, "mutual");
assert.equal(payload.signing_entity.id, "aspora_technology");
assert.equal(payload.signing_entity.legal_name, "Aspora Technology Services Private Limited");
assert.equal(payload.signing_entity.governing_law.playbook_option_id, "india");
assert.equal(payload.signing_entity.governing_law.label, "India");
assert.equal(payload.signing_entity.address.id, "registered");
assert.equal(payload.signing_entity.governing_law_overridden, false);
// The signing_entity.address bundle carries its id (the picked address handle).
assert.equal(payload.signing_entity.address.id, "registered");
// First-party recital + identity fields default to "" when not supplied (the
// preview shows placeholders; the payload sends empty strings, not undefined).
assert.equal(payload.business_description, "");
assert.equal(payload.counterparty_jurisdiction, "");
assert.equal(payload.counterparty_registered_office, "");
// The Special Notes field was removed (it had no defined purpose and only leaked
// into the recital): the payload no longer carries a `notes` key, and the recital
// now comes from the real business_description field.
assert.equal("notes" in payload, false, "notes is no longer part of the payload");
assert.equal("notes" in createInitialIntake(), false, "notes is no longer part of the intake state");
// A blank email serializes to null, not "".
assert.equal(buildDraftPayload(validIntake).counterparty.email, null);

// The first-party recital/identity fields the preview renders now ride the
// payload under their backend-contract key names (business_description,
// counterparty_jurisdiction, counterparty_registered_office). These were
// previously dropped, so a generated NDA silently lost the recital business line
// and the counterparty's incorporation/registered office.
const fullPayload = buildDraftPayload({
  ...validIntake,
  businessDescription: "  cross-border payments  ",
  counterpartyIncorporation: "Delaware, USA",
  counterpartyAddress: "1 Market St, San Francisco, CA",
});
assert.equal(fullPayload.business_description, "cross-border payments", "trimmed business_description");
assert.equal(fullPayload.counterparty_jurisdiction, "Delaware, USA");
assert.equal(fullPayload.counterparty_registered_office, "1 Market St, San Francisco, CA");

// An overridden law is flagged in the payload so generation/review can see the
// coupling was deliberately broken.
const overriddenPayload = buildDraftPayload(setGoverningLawOverride(validIntake, "delaware"));
assert.equal(overriddenPayload.signing_entity.governing_law.playbook_option_id, "delaware");
assert.equal(overriddenPayload.signing_entity.governing_law_overridden, true);

// --- Governing-law clause is LAW-ONLY (preview clause 13). Generation writes the
// governing law and names no forum/courts (it omits the courts sentence to match
// how review reads the clause), so the intake module's bound API exposes no
// forum-resolution helper — there is nothing to keep in sync with the registry,
// and the preview cannot drift into showing a court the executed NDA would not
// contain. (A removed *named* export can't be import-tested under ESM without a
// parse error, so we assert against the bound factory surface instead.) ---
const forumlessApi = createDraftIntake();
assert.equal(typeof forumlessApi.forumForOptionId, "undefined", "the bound intake API exposes no forum helper");
assert.equal(typeof forumlessApi.effectiveForum, "undefined", "the bound intake API exposes no forum helper");

// The playbook term cap the preview clamps to has a sane fallback default.
assert.equal(DEFAULT_MAX_TERM_YEARS, 5);

// --- Factory binds a custom registry (the seam for an entity-model
// /api/signing-entities feed): every helper reads through the injected entities,
// and reads them through the SAME field names as the Python registry. ---
const customRegistry = [
  {
    id: "only_one",
    short_name: "Solo",
    legal_name: "Solo Company Ltd",
    governing_law: { playbook_option_id: "scotland", label: "Scotland" },
    addresses: [
      { id: "hq", label: "HQ", lines: ["Edinburgh"], country: "United Kingdom", default: true },
    ],
  },
];
const intakeApi = createDraftIntake({ entities: customRegistry });
assert.deepEqual(
  intakeApi.governingLawOptions().map((law) => law.id),
  ["scotland"],
);
// The dropdown label is the FULL legal name (what travels into the generated NDA).
assert.equal(intakeApi.entityLabel(customRegistry[0]), "Solo Company Ltd");
// When an entity carries no legal_name, the label falls back to the short_name.
assert.equal(intakeApi.entityLabel({ id: "x", short_name: "Shorty" }), "Shorty");
const customPick = intakeApi.applyEntitySelection(intakeApi.createInitialIntake(), "only_one");
assert.equal(customPick.governingLawId, "scotland");
assert.equal(intakeApi.validateDraftIntake({ ...customPick, counterpartyName: "X" }).ok, true);
assert.equal(
  intakeApi.buildDraftPayload({ ...customPick, counterpartyName: "X" }).signing_entity.id,
  "only_one",
);
// An entity id that belongs to the default registry but not this one does not
// resolve through the injected registry.
assert.equal(
  intakeApi.applyEntitySelection(intakeApi.createInitialIntake(), "aspora_technology").entityId,
  null,
);

// --- Governing-law options sourced from the playbook (single source of truth) ---
//
// When the controller injects playbook governing-law options (the
// /api/signing-entities feed's `governing_law_options`, sourced from the playbook's
// governing_law approved_options), the override dropdown is playbook-driven rather
// than derived from the embedded entity mirror. The injected order/labels win.
const playbookLawOptions = [
  { id: "england_and_wales", label: "England and Wales" },
  { id: "india", label: "India" },
  { id: "delaware", label: "Delaware" },
  { id: "difc", label: "DIFC" },
  { id: "ontario_canada", label: "Ontario, Canada" },
];
const playbookDrivenApi = createDraftIntake({ lawOptions: playbookLawOptions });
assert.deepEqual(
  playbookDrivenApi.governingLawOptions().map((law) => law.id),
  ["england_and_wales", "india", "delaware", "difc", "ontario_canada"],
  "the dropdown is sourced from the injected playbook options, in playbook order",
);
// effectiveGoverningLaw resolves its label through the injected playbook options.
const playbookPick = playbookDrivenApi.applyEntitySelection(
  playbookDrivenApi.createInitialIntake(),
  "vance_money",
);
assert.equal(playbookDrivenApi.effectiveGoverningLaw(playbookPick).label, "Delaware");
// Falling back: with no injected options, the dropdown derives laws from the
// (embedded or injected) entity mirror exactly as before.
assert.deepEqual(
  new Set(governingLawOptions(SIGNING_ENTITIES, null).map((law) => law.id)),
  new Set(["india", "delaware", "england_and_wales", "difc", "ontario_canada"]),
);
// An empty injected list also falls back to the entity-derived options.
assert.deepEqual(
  new Set(governingLawOptions(SIGNING_ENTITIES, []).map((law) => law.id)),
  new Set(["india", "delaware", "england_and_wales", "difc", "ontario_canada"]),
);

// --- NDA generation API wrapper (the "Generate NDA" un-stub) ---
//
// createGenerationApi POSTs buildDraftPayload's shape to /api/generate-nda and
// normalises the response so the controller can either download the DOCX bytes
// or surface the saved artifact. A 404 means the endpoint is not deployed on the
// running base yet (generation lives on another branch until integration) and is
// reported as GenerationUnavailableError so the form degrades gracefully — the
// same fallback the entity picker uses for /api/signing-entities.

// Builds a response that mirrors the parts of fetch's Response the wrapper reads:
// status, ok, a Content-Type/Content-Disposition header bag, and json()/blob().
function generationResponse({ status = 200, headers = {}, json = null, blob = null } = {}) {
  const headerBag = new Map(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value]));
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (name) => headerBag.get(String(name).toLowerCase()) ?? null },
    json: async () => {
      if (json === null) throw new Error("not json");
      return json;
    },
    blob: async () => blob,
  };
}

// The payload the entity picker produces — sent to the endpoint verbatim.
const generationPayload = buildDraftPayload(validIntake);

// --- Generator workstation model ------------------------------------------
const generatedParagraphs = [
  {
    id: "p1",
    index: 1,
    runs: [{ text: "Generated", bold: true }],
    source_index: 1,
    text: "Generated",
  },
];
const generatedState = GeneratorWorkstationModel.generatedGeneratorState({
  matterId: "matter-generated",
  paragraphs: generatedParagraphs,
});
assert.equal(generatedState.generatorMode, "generated");
assert.equal(generatedState.generatorMatterId, "matter-generated");
assert.deepEqual(generatedState.generatorOriginalParagraphs, generatedState.generatorParagraphs);
generatedParagraphs[0].runs[0].text = "mutated";
assert.equal(generatedState.generatorParagraphs[0].runs[0].text, "Generated");
assert.equal(
  GeneratorWorkstationModel.activeGeneratorParagraph({
    ...generatedState,
    generatorActiveParagraphId: "p1",
  }).id,
  "p1",
);
assert.deepEqual(GeneratorWorkstationModel.generatorEditSnapshot({
  ...generatedState,
  generatorActiveParagraphId: "p1",
  generatorDraftTouched: true,
  generatorParagraphs: [{
    alignment: "center",
    font: "Arial",
    fontSize: 12,
    id: "p1",
    runs: [{ text: "Edited", bold: true }],
    source_index: 1,
    source_part: "body",
    text: "Edited",
  }],
}), {
  dirty: true,
  matterId: "matter-generated",
  mode: "generated",
  paragraphs: [{
    alignment: "center",
    font: "Arial",
    fontSize: 12,
    id: "p1",
    runs: [{ text: "Edited", bold: true }],
    source_index: 1,
    source_part: "body",
    text: "Edited",
  }],
});
assert.equal(GeneratorWorkstationModel.generatorExportReady(generatedState, [{ id: "edit-1" }]), true);
assert.equal(GeneratorWorkstationModel.generatorExportReady({ ...generatedState, generatorMatterId: null }, [{ id: "edit-1" }]), false);
assert.deepEqual(GeneratorWorkstationModel.draftGeneratorState([{ id: "draft-1", text: "Draft" }]), {
  generatorActiveParagraphId: null,
  generatorHistory: [],
  generatorMatterId: null,
  generatorMode: "draft",
  generatorParagraphs: [{ id: "draft-1", text: "Draft" }],
});
assert.deepEqual(GeneratorWorkstationModel.clearGeneratorState(), {
  generatorActiveParagraphId: null,
  generatorDraftTouched: false,
  generatorHistory: [],
  generatorMatterId: null,
  generatorMode: "draft",
  generatorParagraphs: [],
});
assert.deepEqual(GeneratorWorkstationModel.pushGeneratorHistory(
  [{ id: "old", text: "Old" }],
  { alignment: "right", id: "p1", runs: [{ text: "Before", italic: true }], text: "Before" },
  1,
), [{
  alignment: "right",
  id: "p1",
  runs: [{ text: "Before", italic: true }],
  text: "Before",
}]);
assert.deepEqual(GeneratorWorkstationModel.generatorTouchedState({ generatorMode: "draft" }), { generatorDraftTouched: true });
assert.deepEqual(GeneratorWorkstationModel.generatorTouchedState({ generatorMode: "generated" }), {});

// DOCX-bytes response: returns kind "blob" with the bytes + the header filename.
const docxCalls = [];
const docxBlob = { size: 1024, type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" };
const docxApi = createGenerationApi({
  fetchImpl: async (url, options) => {
    docxCalls.push({ url, options });
    return generationResponse({
      headers: {
        "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "Content-Disposition": 'attachment; filename="aspora-nda.docx"',
      },
      blob: docxBlob,
    });
  },
});
const docxResult = await docxApi.generateNda(generationPayload);
// POSTs to the generation endpoint with the buildDraftPayload body verbatim.
assert.equal(docxCalls.length, 1);
assert.equal(docxCalls[0].url, "/api/generate-nda");
assert.equal(docxCalls[0].options.method, "POST");
assert.equal(docxCalls[0].options.headers["Content-Type"], "application/json");
assert.deepEqual(JSON.parse(docxCalls[0].options.body), generationPayload);
// Byte response normalises to a downloadable blob + the Content-Disposition name.
assert.equal(docxResult.kind, "blob");
assert.equal(docxResult.blob, docxBlob);
assert.equal(docxResult.filename, "aspora-nda.docx");

// JSON response (the real 201 contract from routes/generation.py): matter_id,
// artifact_id, status, a matter-source download_url, an advisory self_check, and
// the manifest. The wrapper passes the whole body through so the controller can
// download via download_url and flag a self_check miss.
const jsonApi = createGenerationApi({
  fetchImpl: async () =>
    generationResponse({
      status: 201,
      headers: { "Content-Type": "application/json" },
      json: {
        matter_id: "mat_1",
        artifact_id: "art_1",
        status: "generated",
        download_url: "/api/matters/mat_1/source",
        self_check: { passed: true, overall_status: "pass", native_failures: [], dynamic_failures: [] },
        manifest: { entity_id: "aspora_technology" },
      },
    }),
});
const jsonResult = await jsonApi.generateNda(generationPayload);
assert.equal(jsonResult.kind, "json");
assert.equal(jsonResult.matter_id, "mat_1");
assert.equal(jsonResult.artifact_id, "art_1");
assert.equal(jsonResult.status, "generated");
assert.equal(jsonResult.download_url, "/api/matters/mat_1/source");
assert.equal(jsonResult.self_check.passed, true);
assert.deepEqual(jsonResult.manifest, { entity_id: "aspora_technology" });

// 404: endpoint not deployed on this base → GenerationUnavailableError (degrade).
const missingApi = createGenerationApi({
  fetchImpl: async () => generationResponse({ status: 404, json: { error: "not found" } }),
});
await assert.rejects(missingApi.generateNda(generationPayload), (error) => {
  assert.ok(error instanceof GenerationUnavailableError);
  assert.equal(error.code, "generation_unavailable");
  return true;
});

// Other failure (e.g. 422 validation): surfaces the backend message.
const badApi = createGenerationApi({
  fetchImpl: async () =>
    generationResponse({ status: 422, headers: { "Content-Type": "application/json" }, json: { error: "Counterparty name is required" } }),
});
await assert.rejects(badApi.generateNda(generationPayload), /Counterparty name is required/);

// A failure whose body is not JSON falls back to the default message rather than
// throwing the JSON-parse error.
const opaqueApi = createGenerationApi({
  fetchImpl: async () => generationResponse({ status: 500 }),
});
await assert.rejects(opaqueApi.generateNda(generationPayload), /Could not generate the NDA/);

// The endpoint URL is overridable (parity with the other API wrappers' seams).
const customUrlCalls = [];
await createGenerationApi({
  url: "/api/v2/generate-nda",
  fetchImpl: async (url) => {
    customUrlCalls.push(url);
    return generationResponse({ headers: { "Content-Type": "application/json" }, json: {} });
  },
}).generateNda(generationPayload);
assert.equal(customUrlCalls[0], "/api/v2/generate-nda");

// Client timeout: generation is synchronous and the AI clause adapter adds live
// model calls, so a hung POST must NOT pin the caller forever. A fetchImpl that
// never resolves (until aborted) must reject with GenerationTimeoutError once the
// short timeout fires — this is what lets the controller self-heal instead of
// spinning. The fetchImpl honours the AbortSignal the wrapper passes so the
// pending promise rejects promptly when the timer aborts it.
const timeoutApi = createGenerationApi({
  timeoutMs: 25,
  fetchImpl: (url, options) =>
    new Promise((resolve, reject) => {
      const signal = options?.signal;
      if (signal) {
        signal.addEventListener("abort", () => {
          const abortError = new Error("aborted");
          abortError.name = "AbortError";
          reject(abortError);
        });
      }
      // Never resolves on its own -> only the abort path settles it.
    }),
});
await assert.rejects(timeoutApi.generateNda(generationPayload), (error) => {
  assert.ok(error instanceof GenerationTimeoutError);
  assert.equal(error.code, "generation_timeout");
  return true;
});

// A network drop mid-flight (an AbortError NOT triggered by our timer, e.g. the
// connection died) also maps to the recoverable timeout signal, because it has
// the same "backend may have finished" ambiguity.
const dropApi = createGenerationApi({
  fetchImpl: async () => {
    const abortError = new Error("network dropped");
    abortError.name = "AbortError";
    throw abortError;
  },
});
await assert.rejects(dropApi.generateNda(generationPayload), (error) => {
  assert.ok(error instanceof GenerationTimeoutError);
  return true;
});

// A non-abort network error (e.g. DNS failure) is NOT swallowed as a timeout — it
// propagates so a genuine connection failure still surfaces as an error.
const netErrApi = createGenerationApi({
  fetchImpl: async () => {
    throw new TypeError("Failed to fetch");
  },
});
await assert.rejects(netErrApi.generateNda(generationPayload), (error) => {
  assert.ok(!(error instanceof GenerationTimeoutError));
  assert.ok(error instanceof TypeError);
  return true;
});

// timeoutMs <= 0 disables the client ceiling (the old unbounded behaviour, kept
// for callers/tests that drive a controlled fetchImpl): no AbortSignal is passed
// and a slow-but-eventually-resolving fetch still succeeds.
const noTimeoutCalls = [];
const noTimeoutApi = createGenerationApi({
  timeoutMs: 0,
  fetchImpl: async (url, options) => {
    noTimeoutCalls.push(options);
    return generationResponse({ headers: { "Content-Type": "application/json" }, json: { ok: true } });
  },
});
const noTimeoutResult = await noTimeoutApi.generateNda(generationPayload);
assert.equal(noTimeoutResult.kind, "json");
assert.equal(noTimeoutResult.ok, true);
assert.equal(noTimeoutCalls[0].signal, undefined);

// The default ceiling is exported and sane (well above a normal AI generation but
// bounded so a hung request can't spin forever).
assert.equal(typeof DEFAULT_GENERATE_TIMEOUT_MS, "number");
assert.ok(DEFAULT_GENERATE_TIMEOUT_MS >= 10000 && DEFAULT_GENERATE_TIMEOUT_MS <= 120000);

// A successful generate within the timeout still passes the AbortSignal AND clears
// the timer (no dangling handle): a normal JSON response resolves cleanly.
const withinApi = createGenerationApi({
  timeoutMs: 5000,
  fetchImpl: async (url, options) => {
    assert.ok(options.signal, "an AbortSignal is passed when the timeout is active");
    return generationResponse({ status: 201, headers: { "Content-Type": "application/json" }, json: { matter_id: "m9" } });
  },
});
const withinResult = await withinApi.generateNda(generationPayload);
assert.equal(withinResult.matter_id, "m9");

// --- Dashboard smart-search (v1, deterministic) -----------------------------
const dashboardMatters = [
  {
    id: "m_pending",
    subject: "Acme Mutual NDA",
    sender: "legal@acme.example",
    workflow_state: { status: "awaiting_approval", label: "Awaiting approval" },
  },
  {
    id: "m_sent",
    subject: "Globex One-Way NDA",
    sender: "deals@globex.example",
    workflow_state: { status: "sent_awaiting_counterparty", label: "Awaiting signature" },
  },
  {
    id: "m_reviewing",
    subject: "Initech Confidentiality Agreement",
    sender: "ip@initech.example",
    workflow_state: { status: "ai_reviewing", label: "AI reviewing" },
  },
];

// The two solid v1 status chips plus the v3 counterparty-grouping chip.
assert.equal(DASHBOARD_SEARCH_CHIPS.length, 3);
assert.deepEqual(
  DASHBOARD_SEARCH_CHIPS.map((chip) => chip.id),
  ["pending_approval", "awaiting_signature", "by_counterparty"],
);
assert.equal(chipById("pending_approval").status, "awaiting_approval");
assert.equal(chipById("awaiting_signature").status, "sent_awaiting_counterparty");
assert.equal(chipById("nope"), null);
// The v1 chips are exact status filters; the v3 chip is a grouping view, not a status.
assert.equal(chipById("pending_approval").kind, "status");
assert.equal(chipById("by_counterparty").kind, "group");
assert.equal(chipById("by_counterparty").status, undefined);
// runChip is for status chips only — the group chip returns [] (it has its own path).
assert.deepEqual(runChip(dashboardMatters, chipById("by_counterparty")), []);

// Status filter is an EXACT match on workflow_state.status.
assert.deepEqual(
  filterMattersByStatus(dashboardMatters, "awaiting_approval").map((m) => m.id),
  ["m_pending"],
);
assert.deepEqual(
  filterMattersByStatus(dashboardMatters, "sent_awaiting_counterparty").map((m) => m.id),
  ["m_sent"],
);
assert.deepEqual(filterMattersByStatus(dashboardMatters, "fully_signed"), []);
assert.deepEqual(filterMattersByStatus(dashboardMatters, ""), []);

// runChip drives the chip's backing status filter over the real matters.
assert.deepEqual(
  runChip(dashboardMatters, chipById("pending_approval")).map((m) => m.id),
  ["m_pending"],
);
assert.deepEqual(
  runChip(dashboardMatters, chipById("awaiting_signature")).map((m) => m.id),
  ["m_sent"],
);

// Free-text keyword filter matches subject/sender/status; empty query -> [].
assert.deepEqual(filterMattersByText(dashboardMatters, "acme").map((m) => m.id), ["m_pending"]);
assert.deepEqual(filterMattersByText(dashboardMatters, "GLOBEX").map((m) => m.id), ["m_sent"]); // case-insensitive
assert.deepEqual(filterMattersByText(dashboardMatters, "globex@example"), []); // no match -> empty, never fabricated
assert.deepEqual(filterMattersByText(dashboardMatters, "nda").map((m) => m.id), ["m_pending", "m_sent"]);
assert.deepEqual(filterMattersByText(dashboardMatters, "deals@globex.example").map((m) => m.id), ["m_sent"]); // sender
assert.deepEqual(filterMattersByText(dashboardMatters, "reviewing").map((m) => m.id), ["m_reviewing"]); // status token
// Multiple terms are ANDed.
assert.deepEqual(filterMattersByText(dashboardMatters, "globex nda").map((m) => m.id), ["m_sent"]);
assert.deepEqual(filterMattersByText(dashboardMatters, "acme globex"), []);
assert.deepEqual(filterMattersByText(dashboardMatters, "   "), []);
assert.deepEqual(filterMattersByText(dashboardMatters, ""), []);
// Non-array input is tolerated.
assert.deepEqual(filterMattersByText(null, "acme"), []);
assert.deepEqual(filterMattersByStatus(undefined, "awaiting_approval"), []);

// Status + title helpers used by the result rows.
assert.equal(matterStatus(dashboardMatters[0]), "awaiting_approval");
assert.equal(matterStatus({ status: "sending" }), "sending"); // flat fallback
assert.equal(matterStatus({}), "");
assert.equal(matterStatusLabel(dashboardMatters[0]), "Awaiting approval"); // prefers backend label
assert.equal(matterStatusLabel({ workflow_state: { status: "send_failed" } }), "Send Failed"); // title-cased fallback
assert.equal(matterTitle(dashboardMatters[1]), "Globex One-Way NDA");
assert.equal(matterTitle({}), "Untitled NDA");

// --- "Summarize a document" (v1.1) pure helpers ---------------------------- //
// The endpoint encodes the matter id so an odd id can't break out of the path.
assert.equal(summaryEndpoint("matter_abc123"), "/api/matters/matter_abc123/summary");
assert.equal(summaryEndpoint("a/b c"), "/api/matters/a%2Fb%20c/summary");
assert.equal(summaryEndpoint(""), "/api/matters//summary");
assert.equal(DASHBOARD_ASSISTANT_ENDPOINT, "/api/dashboard/assistant");

// A successful payload is normalized to the rendered fields, always carrying the
// "AI summary" label (the golden rule: a generated summary is never mistaken for
// verified fact). We never fabricate text — a blank/absent summary yields null.
assert.equal(SUMMARY_LABEL, "AI summary");
const formatted = formatSummaryResult({
  summary: "  Mutual NDA with Acme. Needs human review.  ",
  model: "anthropic/claude-opus-4.8",
  generated_at: "2026-06-07T10:00:00Z",
});
assert.equal(formatted.label, "AI summary");
assert.equal(formatted.summary, "Mutual NDA with Acme. Needs human review."); // trimmed
assert.equal(formatted.model, "anthropic/claude-opus-4.8");
assert.equal(formatted.generatedAt, "2026-06-07T10:00:00Z");
assert.equal(formatSummaryResult({ summary: "   " }), null); // blank -> null, never fabricated
assert.equal(formatSummaryResult({}), null);
assert.equal(formatSummaryResult(null), null);

// The error message prefers the backend's friendly copy, else the constant. It
// never surfaces a raw stack/HTTP detail.
assert.equal(SUMMARY_UNAVAILABLE_MESSAGE, "Summary unavailable right now.");
assert.equal(summaryErrorMessage({ error: "Summary unavailable right now." }), "Summary unavailable right now.");
assert.equal(summaryErrorMessage({}), "Summary unavailable right now.");
assert.equal(summaryErrorMessage(null), "Summary unavailable right now.");
assert.equal(summaryErrorMessage({ error: "  Custom backend message  " }), "Custom backend message");

// --- Dashboard smart-search v2: validateFilterSpec (client-side schema gate) ---
// Mirrors the backend validator (defense in depth): out-of-enum values are dropped,
// ints are clamped, bools are coerced, unknown keys ignored.
const validSpec = validateFilterSpec({
  status: "awaiting_approval",
  phase: "review",
  needs_attention: true,
  human_gate: false,
  has_issues: true,
  text: "Acme",
  min_age_days: 5,
  sort: "oldest",
  bogus: "x", // unknown key dropped
});
assert.deepEqual(validSpec, {
  status: "awaiting_approval",
  phase: "review",
  needs_attention: true,
  human_gate: false,
  has_issues: true,
  has_clause: null,
  signed: null,
  governing_law: null,
  term_years: null,
  text: "Acme",
  min_age_days: 5,
  sort: "oldest",
});
// Out-of-enum status/phase/sort are dropped to null.
assert.equal(validateFilterSpec({ status: "made_up" }).status, null);
assert.equal(validateFilterSpec({ phase: "shipping" }).phase, null);
assert.equal(validateFilterSpec({ sort: "sideways" }).sort, null);
// Status match is case-insensitive + trimmed.
assert.equal(validateFilterSpec({ status: "  Awaiting_Approval " }).status, "awaiting_approval");
// Non-bool flags are dropped, never coerced (a truthy string must NOT become true).
assert.equal(validateFilterSpec({ needs_attention: "yes" }).needs_attention, null);
assert.equal(validateFilterSpec({ human_gate: 1 }).human_gate, null);
// min_age_days clamps + rejects: 0/negative disable, over-ceiling clamps, bool != int.
assert.equal(validateFilterSpec({ min_age_days: 99999 }).min_age_days, 365);
assert.equal(validateFilterSpec({ min_age_days: 7 }).min_age_days, 7);
assert.equal(validateFilterSpec({ min_age_days: 0 }).min_age_days, null);
assert.equal(validateFilterSpec({ min_age_days: -3 }).min_age_days, null);
assert.equal(validateFilterSpec({ min_age_days: true }).min_age_days, null);
assert.equal(validateFilterSpec({ min_age_days: "9" }).min_age_days, 9);
assert.equal(validateFilterSpec({ min_age_days: "lots" }).min_age_days, null);
// text is capped + blank collapses to null.
assert.equal(validateFilterSpec({ text: "x".repeat(1000) }).text.length, 200);
assert.equal(validateFilterSpec({ text: "   " }).text, null);
// A non-object collapses to the all-null spec, and empty-detection works.
assert.deepEqual(validateFilterSpec("not a dict"), { ...NULL_FILTER_SPEC });
assert.equal(filterSpecIsEmpty(validateFilterSpec(null)), true);
assert.equal(filterSpecIsEmpty(validateFilterSpec({ status: "approved" })), false);

// --- Corpus dimensions (demo): has_clause, signed, governing_law mirror the backend ---
// The null spec carries the three new keys, and the validator gates each one.
assert.equal("has_clause" in NULL_FILTER_SPEC, true);
assert.equal("signed" in NULL_FILTER_SPEC, true);
assert.equal("governing_law" in NULL_FILTER_SPEC, true);
// has_clause: in-allowlist (incl. the demo dynamic clauses) passes; junk drops.
assert.equal(validateFilterSpec({ has_clause: "non_solicitation" }).has_clause, "non_solicitation");
assert.equal(validateFilterSpec({ has_clause: "non_compete" }).has_clause, "non_compete");
assert.equal(validateFilterSpec({ has_clause: "governing_law" }).has_clause, "governing_law");
assert.equal(validateFilterSpec({ has_clause: "not_a_clause" }).has_clause, null);
// signed: strict bool like the other flags.
assert.equal(validateFilterSpec({ signed: true }).signed, true);
assert.equal(validateFilterSpec({ signed: false }).signed, false);
assert.equal(validateFilterSpec({ signed: "yes" }).signed, null);
// governing_law: case-insensitive against the approved-option allowlist; junk drops.
assert.equal(validateFilterSpec({ governing_law: "DIFC" }).governing_law, "difc");
assert.equal(validateFilterSpec({ governing_law: "england_and_wales" }).governing_law, "england_and_wales");
assert.equal(validateFilterSpec({ governing_law: "narnia" }).governing_law, null);
// term_years: clamps + rejects like min_age_days; bool != int, float truncates, junk drops.
assert.equal("term_years" in NULL_FILTER_SPEC, true);
assert.equal(validateFilterSpec({ term_years: 5 }).term_years, 5);
assert.equal(validateFilterSpec({ term_years: 5.0 }).term_years, 5);
assert.equal(validateFilterSpec({ term_years: "5" }).term_years, 5);
assert.equal(validateFilterSpec({ term_years: 0 }).term_years, null);
assert.equal(validateFilterSpec({ term_years: -3 }).term_years, null);
assert.equal(validateFilterSpec({ term_years: true }).term_years, null);
assert.equal(validateFilterSpec({ term_years: 9999 }).term_years, 100);
assert.equal(validateFilterSpec({ term_years: "lots" }).term_years, null);

// matterHasClause: the corpus facet (facets.has_clauses) is the primary source...
const corpusClauseMatter = { facets: { has_clauses: ["confidential_information", "non_solicitation"] } };
assert.equal(matterHasClause(corpusClauseMatter, "non_solicitation"), true);
assert.equal(matterHasClause(corpusClauseMatter, "confidential_information"), true);
assert.equal(matterHasClause(corpusClauseMatter, "non_compete"), false);
// ...and an app-state matter still resolves via review_state.clause_ids buckets.
const appClauseMatter = {
  review_state: { clause_ids: { pass: ["confidential_information"], review: ["non_solicitation"], check: ["governing_law"] } },
};
assert.equal(matterHasClause(appClauseMatter, "non_solicitation"), true); // review bucket
assert.equal(matterHasClause(appClauseMatter, "confidential_information"), true); // pass bucket
assert.equal(matterHasClause(appClauseMatter, "governing_law"), true); // check bucket
assert.equal(matterHasClause(appClauseMatter, "non_compete"), false); // absent
assert.equal(matterHasClause({}, "non_solicitation"), false); // neither shape -> false
// matterSigned reads the corpus facet first (true/false/null)...
assert.equal(matterSigned({ facets: { signed: true } }), true);
assert.equal(matterSigned({ facets: { signed: false } }), false);
assert.equal(matterSigned({ facets: { signed: null } }), null); // explicit unknown
// ...and falls back to the workflow status for an app-state matter (no facets).
assert.equal(matterSigned({ workflow_state: { status: "fully_signed" } }), true);
assert.equal(matterSigned({ workflow_state: { status: "sent_awaiting_counterparty" } }), false);
assert.equal(matterSigned({ workflow_state: { status: "ai_reviewing" } }), null);
// matterGoverningLaw reads the corpus facet (or the legacy top-level field).
assert.equal(matterGoverningLaw({ facets: { governing_law: "difc" } }), "difc");
assert.equal(matterGoverningLaw({ governing_law: "delaware" }), "delaware");
assert.equal(matterGoverningLaw({}), "");
// matterTermYears reads the corpus facet; a positive number is the term, else null
// (unknown). A term_years filter matches only a matter whose term we detected.
assert.equal(matterTermYears({ facets: { term_years: 5 } }), 5);
assert.equal(matterTermYears({ facets: { term_years: 0.5 } }), 0.5);
assert.equal(matterTermYears({ facets: { term_years: null } }), null); // explicit unknown
assert.equal(matterTermYears({ facets: { term_years: 0 } }), null); // 0 is unknown, not a match
assert.equal(matterTermYears({ facets: {} }), null);
assert.equal(matterTermYears({}), null);
// applyFilterSpec term_years null-safety: a known-5yr matter matches term_years:5; a
// 3yr matter and an UNKNOWN-term matter are both excluded (never a false positive).
const fiveYearMatter = { id: "t5", facets: { term_years: 5 } };
const threeYearMatter = { id: "t3", facets: { term_years: 3 } };
const unknownTermMatter = { id: "tnull", facets: { term_years: null } };
assert.deepEqual(
  applyFilterSpec([fiveYearMatter, threeYearMatter, unknownTermMatter], { term_years: 5 }).map((m) => m.id),
  ["t5"],
);
assert.deepEqual(applyFilterSpec([unknownTermMatter], { term_years: 5 }).map((m) => m.id), []);

// --- Corpus adapter: flatten groups[].matters[] + map facets + open-link provenance ---
const corpusPayload = {
  groups: [
    {
      counterparty: "Acme",
      matters: [
        {
          matter_id: "m_app",
          title: "Acme DIFC NDA",
          counterparty: "Acme",
          created_at: "2026-01-01T00:00:00Z",
          source: "both",
          in_app: true,
          open_matter_url: "/?tab=corpus&matter=m_app",
          open_in_drive_url: "https://drive/folder/app",
          facets: { governing_law: "difc", signed: true, has_clauses: ["governing_law"], phase: "executed", status: "fully_signed", facets_available: true },
        },
      ],
    },
    {
      counterparty: "Old Co",
      matters: [
        {
          matter_id: "m_drive",
          title: "Legacy Drive NDA",
          counterparty: "Old Co",
          created_at: "2025-06-01T00:00:00Z",
          source: "drive",
          in_app: false,
          open_matter_url: "",
          open_in_drive_url: "https://drive/folder/legacy",
          facets: { governing_law: "", signed: null, has_clauses: [], phase: "", status: "", facets_available: false },
        },
      ],
    },
  ],
};
const flatCorpus = flattenCorpusPayload(corpusPayload);
assert.equal(flatCorpus.length, 2);
assert.deepEqual(flatCorpus.map((m) => m.id), ["m_app", "m_drive"]);
// The adapter maps matter_id -> id, title -> subject, and passes facets through.
const adaptedApp = flatCorpus[0];
assert.equal(adaptedApp.id, "m_app");
assert.equal(adaptedApp.subject, "Acme DIFC NDA");
assert.equal(adaptedApp.facets.governing_law, "difc");
assert.equal(adaptedApp.in_app, true);
// Open-link provenance: app/both matter has an in-app link; the Drive-only matter
// has no in-app deep link but keeps its Drive folder url.
const adaptedDrive = flatCorpus[1];
assert.equal(adaptedDrive.in_app, false);
assert.equal(adaptedDrive.open_matter_url, "");
assert.equal(adaptedDrive.open_in_drive_url, "https://drive/folder/legacy");
// A facet filter over the flattened corpus matches the app matter and NEVER the
// legacy Drive matter (facets_available=false -> unknown facets never positive-match).
assert.deepEqual(applyFilterSpec(flatCorpus, { governing_law: "difc" }).map((m) => m.id), ["m_app"]);
assert.deepEqual(applyFilterSpec(flatCorpus, { signed: true }).map((m) => m.id), ["m_app"]);
assert.deepEqual(applyFilterSpec(flatCorpus, { signed: false }).map((m) => m.id), []);
// adaptCorpusMatter tolerates junk.
assert.equal(adaptCorpusMatter(null), null);
assert.deepEqual(flattenCorpusPayload({}), []);
assert.deepEqual(flattenCorpusPayload(null), []);

// --- Dashboard smart-search v2: applyFilterSpec (deterministic AND over matters) ---
const NOW = Date.parse("2026-06-08T00:00:00Z");
const daysAgo = (n) => new Date(NOW - n * 86400000).toISOString();
const specMatters = [
  {
    id: "m_old_pending",
    subject: "Acme Mutual NDA",
    sender: "legal@acme.example",
    created_at: daysAgo(30),
    requirements_failed: 2,
    requirements_needs_review: 0,
    workflow_state: { status: "awaiting_approval", phase: "approval", needs_attention: false, human_gate: true },
  },
  {
    id: "m_fresh_review",
    subject: "Globex One-Way NDA",
    sender: "deals@globex.example",
    created_at: daysAgo(1),
    requirements_failed: 0,
    requirements_needs_review: 0,
    workflow_state: { status: "ai_reviewing", phase: "review", needs_attention: false, human_gate: false },
  },
  {
    id: "m_stuck_attention",
    subject: "Initech Confidentiality Agreement",
    sender: "ip@initech.example",
    created_at: daysAgo(10),
    requirements_failed: 0,
    requirements_needs_review: 1,
    workflow_state: { status: "review_failed", phase: "review", needs_attention: true, human_gate: false },
  },
];
const ids = (list) => list.map((m) => m.id);

// Empty spec -> [] (the controller shows the idle hint, mirroring filterMattersByText).
assert.deepEqual(applyFilterSpec(specMatters, NULL_FILTER_SPEC, NOW), []);
assert.deepEqual(applyFilterSpec(specMatters, {}, NOW), []);
// Each dimension on its own.
assert.deepEqual(ids(applyFilterSpec(specMatters, { status: "awaiting_approval" }, NOW)), ["m_old_pending"]);
assert.deepEqual(ids(applyFilterSpec(specMatters, { phase: "review" }, NOW)), ["m_fresh_review", "m_stuck_attention"]);
assert.deepEqual(ids(applyFilterSpec(specMatters, { needs_attention: true }, NOW)), ["m_stuck_attention"]);
assert.deepEqual(ids(applyFilterSpec(specMatters, { needs_attention: false }, NOW)), ["m_old_pending", "m_fresh_review"]);
assert.deepEqual(ids(applyFilterSpec(specMatters, { human_gate: true }, NOW)), ["m_old_pending"]);
assert.deepEqual(ids(applyFilterSpec(specMatters, { has_issues: true }, NOW)), ["m_old_pending", "m_stuck_attention"]);
assert.deepEqual(ids(applyFilterSpec(specMatters, { has_issues: false }, NOW)), ["m_fresh_review"]);
assert.deepEqual(ids(applyFilterSpec(specMatters, { text: "globex" }, NOW)), ["m_fresh_review"]);
// min_age_days: only matters older than N days (the 30d and 10d ones for N=7).
assert.deepEqual(ids(applyFilterSpec(specMatters, { min_age_days: 7 }, NOW)), ["m_old_pending", "m_stuck_attention"]);
// AND-combination: review phase AND has_issues AND older than 7 days -> only m_stuck.
assert.deepEqual(
  ids(applyFilterSpec(specMatters, { phase: "review", has_issues: true, min_age_days: 7 }, NOW)),
  ["m_stuck_attention"],
);
// sort: oldest-first / newest-first by created_at.
assert.deepEqual(ids(applyFilterSpec(specMatters, { phase: "review", sort: "oldest" }, NOW)), ["m_stuck_attention", "m_fresh_review"]);
assert.deepEqual(ids(applyFilterSpec(specMatters, { phase: "review", sort: "newest" }, NOW)), ["m_fresh_review", "m_stuck_attention"]);
// Bad spec is re-validated inside applyFilterSpec: an out-of-enum status drops to
// null, so a spec that is otherwise empty applies nothing (never a fabricated set).
assert.deepEqual(applyFilterSpec(specMatters, { status: "made_up_status" }, NOW), []);
// A matter with no usable timestamp is excluded by a min_age_days filter (never
// silently included).
assert.deepEqual(applyFilterSpec([{ id: "undated", workflow_state: {} }], { min_age_days: 1 }, NOW), []);
// Non-array input is tolerated.
assert.deepEqual(applyFilterSpec(null, { status: "approved" }, NOW), []);

// --- applyFilterSpec with the corpus dimensions (has_clause / signed / governing_law) ---
// These corpus matters carry the facets block the corpus payload surfaces, exercising
// the facets-aware matchers over the SAME applyFilterSpec the FE search uses.
const corpusFacetMatters = [
  {
    id: "difc_signed",
    subject: "Acme DIFC NDA",
    facets: { governing_law: "difc", signed: true, has_clauses: ["governing_law", "confidential_information"], phase: "executed", status: "fully_signed", facets_available: true },
  },
  {
    id: "difc_sent_unsigned",
    subject: "Globex DIFC NDA",
    facets: { governing_law: "difc", signed: false, has_clauses: ["governing_law", "non_solicitation"], phase: "sent", status: "sent_awaiting_counterparty", facets_available: true },
  },
  {
    id: "india_review_nonsolicit",
    subject: "Initech India NDA",
    facets: { governing_law: "india", signed: null, has_clauses: ["non_solicitation", "non_compete"], phase: "review", status: "ai_reviewing", facets_available: true },
  },
];
const cids = (list) => list.map((m) => m.id);
// governing_law dimension.
assert.deepEqual(cids(applyFilterSpec(corpusFacetMatters, { governing_law: "difc" }, NOW)), ["difc_signed", "difc_sent_unsigned"]);
assert.deepEqual(cids(applyFilterSpec(corpusFacetMatters, { governing_law: "india" }, NOW)), ["india_review_nonsolicit"]);
// signed dimension (derived from the facet; pre-send matters excluded either polarity).
assert.deepEqual(cids(applyFilterSpec(corpusFacetMatters, { signed: true }, NOW)), ["difc_signed"]);
assert.deepEqual(cids(applyFilterSpec(corpusFacetMatters, { signed: false }, NOW)), ["difc_sent_unsigned"]);
// has_clause dimension (membership in the flattened facets.has_clauses).
assert.deepEqual(cids(applyFilterSpec(corpusFacetMatters, { has_clause: "non_solicitation" }, NOW)), ["difc_sent_unsigned", "india_review_nonsolicit"]);
assert.deepEqual(cids(applyFilterSpec(corpusFacetMatters, { has_clause: "non_compete" }, NOW)), ["india_review_nonsolicit"]);
// The headline compound: DIFC AND sent phase AND unsigned -> just the one matter.
assert.deepEqual(
  cids(applyFilterSpec(corpusFacetMatters, { governing_law: "difc", phase: "sent", signed: false }, NOW)),
  ["difc_sent_unsigned"],
);

// --- Corpus FE/backend parity: workflow-state facets through adaptCorpusMatter ---
// A corpus payload (GET /api/corpus shape) carries the workflow-state failure/gate
// axes + requirement counts on the facets block. adaptCorpusMatter must reconstruct
// workflow_state.{needs_attention,human_gate} + the top-level requirements_* counts
// so the human_gate / needs_attention / has_issues filters positively match the SAME
// set the Python twin (corpus_matter facets, derived from workflow_state) matches.
// Regression guard for the prior divergence where the FE returned 0 for these facets.
const corpusWorkflowMatters = [
  {
    matter_id: "cw_human_gate",
    title: "Acme NDA awaiting human review",
    facets: { phase: "approval", status: "awaiting_approval", needs_attention: false, human_gate: true, requirements_failed: 0, requirements_needs_review: 0, facets_available: true },
  },
  {
    matter_id: "cw_stuck",
    title: "Globex NDA stuck",
    facets: { phase: "review", status: "review_failed", needs_attention: true, human_gate: false, requirements_failed: 0, requirements_needs_review: 1, facets_available: true },
  },
  {
    matter_id: "cw_clean",
    title: "Initech NDA clean",
    facets: { phase: "review", status: "ai_reviewing", needs_attention: false, human_gate: false, requirements_failed: 0, requirements_needs_review: 0, facets_available: true },
  },
  {
    // A legacy/degraded matter: facets_available=false, all signals at their
    // defaults, so it never positively matches any of these facets (graceful
    // degradation — exactly as the Python twin drops it from facet-filtered counts).
    matter_id: "cw_legacy",
    title: "Legacy Drive doc",
    facets: { phase: "", status: "", needs_attention: false, human_gate: false, requirements_failed: 0, requirements_needs_review: 0, facets_available: false },
  },
];
const adaptedCorpus = corpusWorkflowMatters.map((m) => adaptCorpusMatter(m));
const acids = (list) => list.map((m) => m.id);
// adaptCorpusMatter reconstructs the workflow_state the matchers read.
assert.equal(adaptedCorpus[0].workflow_state.human_gate, true);
assert.equal(adaptedCorpus[1].workflow_state.needs_attention, true);
assert.equal(adaptedCorpus[1].requirements_needs_review, 1);
// human_gate filter now positively matches the corpus matter (was 0 before the fix).
assert.deepEqual(acids(applyFilterSpec(adaptedCorpus, { human_gate: true }, NOW)), ["cw_human_gate"]);
// needs_attention + has_issues mirror the backend matcher over the same source.
assert.deepEqual(acids(applyFilterSpec(adaptedCorpus, { needs_attention: true }, NOW)), ["cw_stuck"]);
assert.deepEqual(acids(applyFilterSpec(adaptedCorpus, { has_issues: true }, NOW)), ["cw_stuck"]);
// The legacy/degraded matter never positively matches any of these facets.
assert.equal(acids(applyFilterSpec(adaptedCorpus, { human_gate: true }, NOW)).includes("cw_legacy"), false);
assert.equal(acids(applyFilterSpec(adaptedCorpus, { needs_attention: true }, NOW)).includes("cw_legacy"), false);

// --- Dashboard smart-search v3: groupMattersByCounterparty (grouping view) ---
// Each matter carries a derived `counterparty` (from public_matter). Grouping is
// exact on that best-available name; group order is by first appearance, and the
// "Unknown Counterparty" bucket always sorts LAST.
const cpMatters = [
  { id: "m1", counterparty: "Acme Robotics Ltd", subject: "Acme NDA" },
  { id: "m2", counterparty: "Globex Ltd", subject: "Globex NDA" },
  { id: "m3", counterparty: "Acme Robotics Ltd", subject: "Acme NDA round 2" },
  { id: "m4", subject: "stray inbound" }, // no counterparty -> unknown bucket
  { id: "m5", counterparty: "  ", subject: "blank cp" }, // blank trims -> unknown bucket
  { id: "m6", counterparty: "Globex Ltd", subject: "Globex addendum" },
];
const cpGroups = groupMattersByCounterparty(cpMatters);
// Named groups in first-appearance order, then the unknown bucket last.
assert.deepEqual(
  cpGroups.map((g) => g.counterparty),
  ["Acme Robotics Ltd", "Globex Ltd", COUNTERPARTY_UNKNOWN],
);
// Matters keep input order within each group; grouping is exact on the name.
assert.deepEqual(cpGroups[0].matters.map((m) => m.id), ["m1", "m3"]);
assert.deepEqual(cpGroups[1].matters.map((m) => m.id), ["m2", "m6"]);
assert.deepEqual(cpGroups[2].matters.map((m) => m.id), ["m4", "m5"]); // blank + missing
assert.equal(COUNTERPARTY_UNKNOWN, "Unknown Counterparty");
// All matters with a name -> no unknown bucket at all.
const namedOnly = groupMattersByCounterparty([
  { id: "a", counterparty: "Initech" },
  { id: "b", counterparty: "Initech" },
]);
assert.deepEqual(namedOnly.map((g) => g.counterparty), ["Initech"]);
assert.deepEqual(namedOnly[0].matters.map((m) => m.id), ["a", "b"]);
// Empty / non-array input -> no groups, never fabricated.
assert.deepEqual(groupMattersByCounterparty([]), []);
assert.deepEqual(groupMattersByCounterparty(null), []);
// Only-unknown input still yields exactly the unknown bucket.
const unknownOnly = groupMattersByCounterparty([{ id: "x" }, { id: "y", counterparty: "" }]);
assert.deepEqual(unknownOnly.map((g) => g.counterparty), [COUNTERPARTY_UNKNOWN]);
assert.deepEqual(unknownOnly[0].matters.map((m) => m.id), ["x", "y"]);

// --- Dashboard smart-search v3: buildArtifactLineage (document lineage chain) ---
// A matter's artifacts ordered by lineage (root -> derived), each labelled with
// role/version/actor/date, the current artifact marked. Built ONLY from the
// matter's own artifacts — no fabrication.
const lineageMatter = {
  id: "m_lineage",
  current_artifact_id: "a_reviewed",
  artifacts: [
    // Deliberately NOT in lineage order in the source list, to prove the builder
    // reorders by based_on_artifact_id rather than trusting input order.
    { id: "a_redline", role: "redline", version: 1, actor: "ai", based_on_artifact_id: "a_original", created_at: "2026-06-02T10:00:00+00:00", is_current: false },
    { id: "a_original", role: "original", version: 1, actor: "counterparty", based_on_artifact_id: "", created_at: "2026-06-01T09:00:00+00:00", is_current: false },
    { id: "a_reviewed", role: "reviewed", version: 1, actor: "human", based_on_artifact_id: "a_redline", created_at: "2026-06-03T11:00:00+00:00", is_current: true },
  ],
};
const lineage = buildArtifactLineage(lineageMatter);
// Ordered root -> derived: original -> redline -> reviewed (follows based_on chain).
assert.deepEqual(lineage.map((n) => n.id), ["a_original", "a_redline", "a_reviewed"]);
assert.deepEqual(lineage.map((n) => n.role), ["original", "redline", "reviewed"]);
// Labels are friendly; actor mapped; current flag set from current_artifact_id.
assert.equal(lineage[0].roleLabel, "Original");
assert.equal(lineage[1].actorLabel, "AI agent");
assert.equal(lineage[2].roleLabel, "Reviewed");
assert.equal(lineage[2].actorLabel, "Legal reviewer");
assert.deepEqual(lineage.map((n) => n.isCurrent), [false, false, true]);
assert.equal(lineage[2].version, 1);
assert.equal(lineage[0].date, "2026-06-01T09:00:00+00:00");
// is_current is honoured even when current_artifact_id is absent on the matter.
const flaggedOnly = buildArtifactLineage({
  id: "m_flag",
  artifacts: [
    { id: "a1", role: "original", version: 1, based_on_artifact_id: "" },
    { id: "a2", role: "redline", version: 1, based_on_artifact_id: "a1", is_current: true },
  ],
});
assert.deepEqual(flaggedOnly.map((n) => n.isCurrent), [false, true]);
// Two roots at the same level order by version then date then registration index.
const twoRoots = buildArtifactLineage({
  id: "m_roots",
  artifacts: [
    { id: "gen_v2", role: "generated", version: 2, based_on_artifact_id: "", created_at: "2026-06-05T00:00:00Z" },
    { id: "gen_v1", role: "generated", version: 1, based_on_artifact_id: "", created_at: "2026-06-04T00:00:00Z" },
  ],
});
assert.deepEqual(twoRoots.map((n) => n.id), ["gen_v1", "gen_v2"]); // v1 before v2
// A based_on pointing at a non-existent artifact is treated as a root (never dropped).
const dangling = buildArtifactLineage({
  id: "m_dangle",
  artifacts: [
    { id: "only", role: "counter", version: 1, based_on_artifact_id: "missing_parent" },
    { id: "root", role: "original", version: 1, based_on_artifact_id: "" },
  ],
});
assert.deepEqual(dangling.map((n) => n.id).sort(), ["only", "root"]);
assert.equal(dangling.length, 2); // both surfaced, nothing lost
// Empty / single-artifact matters: the builder returns the 0/1-length list as-is so
// the controller shows the friendly "No earlier versions yet." for fewer than two.
assert.deepEqual(buildArtifactLineage({ id: "m_empty", artifacts: [] }), []);
assert.deepEqual(buildArtifactLineage({ id: "m_none" }), []);
const single = buildArtifactLineage({
  id: "m_single",
  current_artifact_id: "solo",
  artifacts: [{ id: "solo", role: "original", version: 1, actor: "counterparty", is_current: true }],
});
assert.equal(single.length, 1);
assert.equal(single[0].id, "solo");
assert.equal(single[0].isCurrent, true);
// The internal ordering index is not leaked on the returned nodes.
assert.ok(!("_index" in single[0]));
assert.ok(!("_index" in lineage[0]));
