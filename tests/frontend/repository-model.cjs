"use strict";

// Frontend unit test for the pure RepositoryModel label helpers — specifically
// the humanization of source-type and triage enums, so no raw internal token
// (send_document / gmail_demo / ...) leaks into a board badge / inspector kicker.
//
// repository-model.js is a classic browser script that exposes CommonJS exports
// behind a `typeof module !== "undefined"` guard. We require it directly; the
// label helpers under test touch neither MatterUtils nor the DOM.

const assert = require("node:assert/strict");
const path = require("node:path");

const { RepositoryModel } = require(path.join(__dirname, "..", "..", "static", "js", "repository-model.js"));

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- sourceTypeLabel: curated enum + humanised fallback ---------------------
test("sourceTypeLabel maps the known source enums to human phrases", () => {
  assert.equal(RepositoryModel.sourceTypeLabel("send_document"), "Sent NDA");
  assert.equal(RepositoryModel.sourceTypeLabel("gmail_demo"), "Inbox");
  assert.equal(RepositoryModel.sourceTypeLabel("gmail_inbound"), "Mail");
  assert.equal(RepositoryModel.sourceTypeLabel("manual_upload"), "Manual Upload");
  assert.equal(RepositoryModel.sourceTypeLabel("generated"), "Generated");
});

test("sourceTypeLabel never leaks the internal demo channel name", () => {
  // "gmail_demo" must read as "Inbox", not the internal *demo* channel name.
  assert.notEqual(RepositoryModel.sourceTypeLabel("gmail_demo"), "Gmail Demo");
});

test("sourceTypeLabel humanises an unmapped token instead of echoing it raw", () => {
  // NON-VACUITY: a send_document matter renders "Sent NDA" (the curated map),
  // and a brand-new unmapped token is Title-cased rather than shown raw.
  assert.equal(RepositoryModel.sourceTypeLabel("brand_new_channel"), "Brand New Channel");
  assert.notEqual(RepositoryModel.sourceTypeLabel("brand_new_channel"), "brand_new_channel");
  // Empty / nullish -> the generic noun, never blank or "Source".
  assert.equal(RepositoryModel.sourceTypeLabel(""), "Document");
  assert.equal(RepositoryModel.sourceTypeLabel(undefined), "Document");
});

test("no raw source token renders for any curated enum", () => {
  ["send_document", "gmail_demo", "gmail_inbound", "manual_upload", "generated"].forEach((token) => {
    const label = RepositoryModel.sourceTypeLabel(token);
    assert.notEqual(label, token, `raw source token leaked: ${token}`);
  });
});

// --- triageLabel: full phrases incl. sent -----------------------------------
test("triageLabel expands the terse codes to full phrases", () => {
  assert.equal(RepositoryModel.triageLabel("ready_to_sign"), "Ready to sign");
  assert.equal(RepositoryModel.triageLabel("needs_redline"), "Needs redline");
  assert.equal(RepositoryModel.triageLabel("legal_review"), "Legal review");
  assert.equal(RepositoryModel.triageLabel("intake_error"), "Intake error");
  assert.equal(RepositoryModel.triageLabel("sent"), "Sent");
});

test("triageLabel falls back to a friendly default for an unknown status", () => {
  assert.equal(RepositoryModel.triageLabel("whatever"), "Needs review");
  assert.equal(RepositoryModel.triageLabel(undefined), "Needs review");
});

process.stdout.write(`\nrepository-model: ${passed} passed\n`);
