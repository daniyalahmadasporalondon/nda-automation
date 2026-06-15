"use strict";

// Frontend unit test for the counterparty human-confirmation surface:
//   - RepositoryModel.counterpartyNeedsConfirmation: the pure badge decision
//     (trust the backend flag; fail open to "needs confirmation" when absent).
//   - RepositoryBoard.renderMatterCard: the repository card renders the
//     "Counterparty unconfirmed" badge exactly when the flag is set.
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

// --- the repository card badge ----------------------------------------------
const BASE_MATTER = {
  id: "m1",
  source_type: "gmail_demo",
  subject: "RE: NDA",
  message_snippet: "snippet",
  received_at: "2026-06-15T00:00:00Z",
};

test("repository card shows the unconfirmed badge when confirmation is needed", () => {
  const html = RepositoryBoard.renderMatterCard({ ...BASE_MATTER, counterparty_needs_confirmation: true });
  assert.match(html, /repository-counterparty-badge/);
  assert.match(html, /Counterparty unconfirmed/);
});

test("repository card omits the badge once the counterparty is confirmed", () => {
  const html = RepositoryBoard.renderMatterCard({ ...BASE_MATTER, counterparty_needs_confirmation: false });
  assert.doesNotMatch(html, /repository-counterparty-badge/);
  assert.doesNotMatch(html, /Counterparty unconfirmed/);
});

test("repository card shows the badge for a matter with no flag at all (fail open)", () => {
  const html = RepositoryBoard.renderMatterCard({ ...BASE_MATTER });
  assert.match(html, /repository-counterparty-badge/);
});

process.stdout.write(`\n${passed} passed\n`);
