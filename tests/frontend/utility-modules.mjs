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
} from "../../static/js/modules/matter-utils.mjs";
import { createRepositoryApi } from "../../static/js/modules/repository-api.mjs";

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
        review_comparison: { mode: "deterministic_vs_ai_first", status: "completed" },
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
    if (url === "/api/review/compare") return jsonResponse({ review_comparison: { mode: "deterministic_vs_ai_first", source: "text" } });
    if (url === "/api/matters/matter%20one/review-comparison") {
      return jsonResponse({
        matter: { id: "matter one", board_column: "in_review" },
        review_comparison: { mode: "deterministic_vs_ai_first", source: "matter" },
      });
    }
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
  review_comparison: { mode: "deterministic_vs_ai_first", status: "completed" },
  review_refresh: null,
  review_result: { clauses: [] },
});
assert.deepEqual(await repositoryApi.getMatterReview("matter one", { refresh: true }), {
  id: "matter one",
  extracted_text: "Refreshed contract text",
  redline_draft: null,
  review_comparison: null,
  review_refresh: { refreshed: true, stale: false },
  review_result: { clauses: [{ id: "mutuality" }] },
});
assert.deepEqual(await repositoryApi.moveMatterToColumn("matter one", "in_review"), { id: "matter one", board_column: "in_review" });
assert.deepEqual(await repositoryApi.compareTextReview("Contract text"), { mode: "deterministic_vs_ai_first", source: "text" });
assert.deepEqual(await repositoryApi.compareMatterReview("matter one"), {
  matter: { id: "matter one", board_column: "in_review" },
  review_comparison: { mode: "deterministic_vs_ai_first", source: "matter" },
});
assert.deepEqual(await repositoryApi.sendRedline({ matter_id: "matter-1", confirm_send: true }), { sent: true });
assert.deepEqual(await repositoryApi.syncGmail({ limit: 2 }), { result: { imported: [{ id: "matter-2" }] } });
assert.equal(calls[3].url, "/api/matters/matter%20one/review-refresh");
assert.equal(calls[3].options.method, "POST");
assert.equal(calls[calls.length - 1].url, "/api/gmail/import");
assert.equal(calls[calls.length - 1].options.method, "POST");
assert.deepEqual(JSON.parse(calls[calls.length - 1].options.body), { limit: 2 });
assert.equal(calls[4].options.method, "POST");
assert.deepEqual(JSON.parse(calls[4].options.body), { board_column: "in_review" });
assert.deepEqual(JSON.parse(calls[5].options.body), { text: "Contract text" });
assert.equal(calls[6].options.method, "POST");
assert.deepEqual(JSON.parse(calls[7].options.body), { matter_id: "matter-1", confirm_send: true });

function jsonResponse(payload, { ok = true } = {}) {
  return {
    ok,
    json: async () => payload,
  };
}
