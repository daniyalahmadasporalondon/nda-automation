"use strict";

// Frontend unit test for the counterparty human-confirmation surface:
//   - RepositoryModel.counterpartyNeedsConfirmation: the pure confirmation
//     decision (trust the backend flag; fail open to "needs confirmation" when
//     absent). This still drives the Overview-tab Confirm flow.
//   - RepositoryBoard.renderMatterCard: the board card no longer renders a
//     "Counterparty unconfirmed" badge -- it was removed from the card; the
//     other badges (source + review-status) stay. We assert it never appears,
//     regardless of the flag.
//
// Both modules are classic browser scripts that expose CommonJS exports behind a
// `typeof module !== "undefined"` guard (a no-op in the browser). We require them
// here with the same global stubs the shipped page provides (RepositoryModel +
// MatterUtils load first), so the render path is exercised as it is in production.

const assert = require("node:assert/strict");
const path = require("node:path");

const { RepositoryModel } = require(
  path.join(__dirname, "..", "..", "static", "js", "repository-model.js"),
);

// MatterUtils is referenced by repository-board.js at render time (reviewStale).
// Stub it on the global the way the page wires it before the board script runs.
global.MatterUtils = {
  reviewStale: () => false,
  reviewStaleLabel: () => "",
};
global.RepositoryModel = RepositoryModel;

const { RepositoryBoard } = require(
  path.join(__dirname, "..", "..", "static", "js", "repository-board.js"),
);

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- the pure badge decision -----------------------------------------------
test("counterpartyNeedsConfirmation trusts an explicit backend flag", () => {
  assert.equal(RepositoryModel.counterpartyNeedsConfirmation({ counterparty_needs_confirmation: true }), true);
  assert.equal(RepositoryModel.counterpartyNeedsConfirmation({ counterparty_needs_confirmation: false }), false);
});

test("counterpartyNeedsConfirmation fails open when the flag is absent or malformed", () => {
  assert.equal(RepositoryModel.counterpartyNeedsConfirmation({}), true);
  assert.equal(RepositoryModel.counterpartyNeedsConfirmation({ counterparty_needs_confirmation: undefined }), true);
  assert.equal(RepositoryModel.counterpartyNeedsConfirmation({ counterparty_needs_confirmation: "no" }), true);
  assert.equal(RepositoryModel.counterpartyNeedsConfirmation(null), true);
  assert.equal(RepositoryModel.counterpartyNeedsConfirmation(undefined), true);
});

// --- the board-column derivation (matterColumn) -----------------------------
// An AI review advances an intake matter (Upload / Inbox / Generated) to
// "In Review"; un-reviewed intake matters keep their intake column; reviewed and
// sent matters are never pulled backward (forward-only).
test("matterColumn: un-reviewed intake matters keep their intake column", () => {
  // Manual upload is STORED as in_review and displayed back as Upload.
  assert.equal(
    RepositoryModel.matterColumn({ source_type: "manual_upload", board_column: "in_review" }),
    "manual_upload",
  );
  assert.equal(
    RepositoryModel.matterColumn({ source_type: "manual_upload", board_column: "in_review", ai_review_ran: false }),
    "manual_upload",
  );
  assert.equal(RepositoryModel.matterColumn({ source_type: "gmail_demo", board_column: "gmail_demo" }), "gmail_demo");
  assert.equal(RepositoryModel.matterColumn({ source_type: "generated", board_column: "generated" }), "generated");
});

test("matterColumn: an AI-reviewed intake matter advances to in_review for every source", () => {
  // Upload (stored in_review) must escape "Upload" once reviewed.
  assert.equal(
    RepositoryModel.matterColumn({ source_type: "manual_upload", board_column: "in_review", ai_review_ran: true }),
    "in_review",
  );
  assert.equal(
    RepositoryModel.matterColumn({ source_type: "gmail_demo", board_column: "gmail_demo", ai_review_ran: true }),
    "in_review",
  );
  assert.equal(
    RepositoryModel.matterColumn({ source_type: "generated", board_column: "generated", ai_review_ran: true }),
    "in_review",
  );
});

test("matterColumn: forward-only -- reviewed and sent matters are never pulled back", () => {
  assert.equal(RepositoryModel.matterColumn({ board_column: "reviewed", ai_review_ran: true }), "reviewed");
  assert.equal(RepositoryModel.matterColumn({ board_column: "sent", ai_review_ran: true }), "sent");
  // A non-upload matter already advanced to in_review stays put.
  assert.equal(RepositoryModel.matterColumn({ source_type: "gmail_demo", board_column: "in_review", ai_review_ran: true }), "in_review");
});

// --- the repository card badge ----------------------------------------------
const BASE_MATTER = {
  id: "m1",
  source_type: "gmail_demo",
  subject: "RE: NDA",
  message_snippet: "snippet",
  received_at: "2026-06-15T00:00:00Z",
};

test("repository card never renders the counterparty-unconfirmed badge (it was removed)", () => {
  for (const flag of [true, false, undefined]) {
    const matter = { ...BASE_MATTER };
    if (flag !== undefined) matter.counterparty_needs_confirmation = flag;
    const html = RepositoryBoard.renderMatterCard(matter);
    assert.doesNotMatch(html, /repository-counterparty-badge/);
    assert.doesNotMatch(html, /Counterparty unconfirmed/);
  }
});

test("repository card still renders the other badges (source + review-status)", () => {
  const html = RepositoryBoard.renderMatterCard({ ...BASE_MATTER, counterparty_needs_confirmation: true });
  assert.match(html, /repository-source-badge/);
  assert.match(html, /repository-review-badge/);
});

process.stdout.write(`\n${passed} passed\n`);
