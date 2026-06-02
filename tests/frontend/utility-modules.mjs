import assert from "node:assert/strict";

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

assert.equal(clausePasses({ decision: "pass", status: "match" }), true);

assert.equal(needsInlineSpace("$", "100"), false);
assert.equal(needsInlineSpace("Agreement", "applies"), true);
assert.equal(needsInlineSpace("Agreement", "."), false);
assert.equal(
  renderDiffOperations([
    { type: "same", token: "$" },
    { type: "same", token: "100" },
    { type: "insert", token: " cap" },
  ]),
  "$100<span class=\"inline-ins\"> cap</span>",
);
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
