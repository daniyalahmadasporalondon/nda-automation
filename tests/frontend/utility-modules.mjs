import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { clausePasses, clauseStatus } from "../../static/js/modules/clause-status.mjs";
import { formatBytes, formatMatterDate, formatMatterDateTime } from "../../static/js/modules/formatting.mjs";
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
import {
  dashboardGreeting,
  firstNameFromDisplayName,
  firstNameFromEmail,
  resolveFirstName,
} from "../../static/js/modules/greeting.mjs";
import {
  buildSendDocumentPayload,
  isSupportedSendFilename,
  isValidRecipientEmail,
  validateSendDocument,
} from "../../static/js/modules/send-document.mjs";

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
assert.equal(reviewStatus.pillLabel, "REVIEW");
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

assert.equal(formatBytes(0), "0 B");
assert.equal(formatBytes(1536), "1.5 KB");
assert.equal(formatBytes(2 * 1024 * 1024), "2.0 MB");
assert.equal(formatMatterDate("not a date"), "");
assert.equal(formatMatterDateTime("not a date"), "");

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

// validationSummary: pluralization + valid case.
assert.equal(validationSummary({ valid: true, errors: [] }), "Draft is valid.");
assert.equal(validationSummary({ valid: false, errors: [{ message: "a" }] }), "1 validation issue found.");
assert.equal(validationSummary({ valid: false, errors: [{ message: "a" }, { message: "b" }] }), "2 validation issues found.");

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
