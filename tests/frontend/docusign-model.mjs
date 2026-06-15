// Frontend unit test for the DocuSign view-model (static/js/modules/docusign-model.mjs).
//
// This is the single source of the status-driven decisions the shipped browser
// path runs (the admin-docusign + docusign-send controllers call it via the
// global-bridge), so exercising it here covers the connect/disconnect status
// rendering and the send -> awaiting -> signed -> download lifecycle without a
// browser.
//
// Run: node tests/frontend/docusign-model.mjs

import assert from "node:assert/strict";

import {
  DocuSignModel,
  buildSendForSignaturePayload,
  connectionView,
  defaultSigners,
  matterEnvelopeId,
  matterSignatureStatus,
  matterSignatureView,
  normalizeSignatureStatus,
  signatureView,
  validateSigners,
} from "../../static/js/modules/docusign-model.mjs";

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- connection view (admin panel: connected vs not) -----------------------
test("connectionView: not connected -> blocked tone + Connect intent", () => {
  const view = connectionView({ connected: false });
  assert.equal(view.connected, false);
  assert.equal(view.tone, "blocked");
  assert.equal(view.statusLabel, "Not connected");
  assert.equal(view.actionLabel, "Connect DocuSign");
  assert.equal(view.account, "No account connected");
});

test("connectionView: connected -> ready tone + Disconnect intent + account label", () => {
  const view = connectionView({
    connected: true,
    account: { name: "Aspora Inc", email: "legal@aspora.com" },
  });
  assert.equal(view.connected, true);
  assert.equal(view.tone, "ready");
  assert.equal(view.statusLabel, "Connected");
  assert.equal(view.actionLabel, "Disconnect DocuSign");
  assert.equal(view.account, "Aspora Inc (legal@aspora.com)");
});

test("connectionView: connected with a string account label", () => {
  const view = connectionView({ connected: true, account: "legal@aspora.com" });
  assert.equal(view.account, "legal@aspora.com");
});

test("connectionView: no demo concept leaks into the view", () => {
  // Scope decision: there is no demo/stub mode. Even if a stray `demo` flag
  // arrives it must not produce a demo label or notice.
  const view = connectionView({ connected: true, demo: true, account: "x@y.com" });
  assert.equal(view.statusLabel, "Connected");
  assert.equal("demo" in view, false);
  assert.equal("demoNotice" in view, false);
});

// --- signature lifecycle (sent -> awaiting -> signed -> download) ----------
test("normalizeSignatureStatus trims/cases known states, drops unknown", () => {
  assert.equal(normalizeSignatureStatus(" Sent "), "sent");
  assert.equal(normalizeSignatureStatus("COMPLETED"), "completed");
  assert.equal(normalizeSignatureStatus("bogus"), "");
  assert.equal(normalizeSignatureStatus(null), "");
  assert.equal(normalizeSignatureStatus(undefined), "");
});

test("signatureView: no status -> idle, no badge, no download", () => {
  const view = signatureView(null);
  assert.equal(view.status, "");
  assert.equal(view.tone, "idle");
  assert.equal(view.badge, "");
  assert.equal(view.sent, false);
  assert.equal(view.completed, false);
  assert.equal(view.canDownloadSigned, false);
});

test("signatureView: sent -> awaiting signature, pending, not downloadable", () => {
  const view = signatureView("sent");
  assert.equal(view.tone, "pending");
  assert.equal(view.label, "Awaiting signature");
  assert.equal(view.sent, true);
  assert.equal(view.completed, false);
  assert.equal(view.canDownloadSigned, false);
  assert.equal(view.terminal, false);
});

test("signatureView: delivered is still awaiting (pending, no download)", () => {
  const view = signatureView({ status: "delivered" });
  assert.equal(view.tone, "pending");
  assert.equal(view.label, "Awaiting signature");
  assert.equal(view.canDownloadSigned, false);
});

test("signatureView: completed -> signed, ready, downloadable, terminal", () => {
  const view = signatureView({ status: "completed" });
  assert.equal(view.tone, "ready");
  assert.equal(view.label, "Signed");
  assert.equal(view.badge, "Signed");
  assert.equal(view.completed, true);
  assert.equal(view.canDownloadSigned, true);
  assert.equal(view.terminal, true);
});

test("signatureView: declined/voided -> blocked + terminal, never downloadable", () => {
  for (const status of ["declined", "voided"]) {
    const view = signatureView(status);
    assert.equal(view.tone, "blocked", status);
    assert.equal(view.canDownloadSigned, false, status);
    assert.equal(view.terminal, true, status);
  }
});

// --- F1 regression: read the CANONICAL nested matter.docusign field ---------
// The backend persists + exposes the envelope state nested under
// matter.docusign = {envelope_id, status, ...} (the durable, server-exposed
// source that survives a reload / a freshly-fetched matter). The flat
// matter.signature_* fields only exist in the in-session merge after a live
// send/poll. Reading flat-only reset a reloaded matter to "not sent" — the bug.
test("matterSignatureStatus: reads nested matter.docusign.status (server-exposed) first", () => {
  assert.equal(matterSignatureStatus({ docusign: { status: "sent" } }), "sent");
  assert.equal(matterSignatureStatus({ docusign: { status: "completed" } }), "completed");
});

test("matterSignatureStatus: falls back to the flat in-session field when nested is absent", () => {
  assert.equal(matterSignatureStatus({ signature_status: "sent" }), "sent");
  assert.equal(matterSignatureStatus({}), "");
  assert.equal(matterSignatureStatus(null), "");
});

test("matterSignatureStatus: nested wins over a stale flat field", () => {
  // A freshly-fetched matter carries the canonical nested status; any stale flat
  // value left in the in-session object must not override it.
  assert.equal(matterSignatureStatus({ docusign: { status: "completed" }, signature_status: "sent" }), "completed");
});

test("matterEnvelopeId: nested-first, flat fallback", () => {
  assert.equal(matterEnvelopeId({ docusign: { envelope_id: "env-123" } }), "env-123");
  assert.equal(matterEnvelopeId({ signature_envelope_id: "env-flat" }), "env-flat");
  assert.equal(matterEnvelopeId({ docusign: { envelope_id: "env-nested" }, signature_envelope_id: "env-flat" }), "env-nested");
  assert.equal(matterEnvelopeId({}), "");
});

test("matterSignatureView: a freshly-fetched 'sent' matter shows the awaiting badge (no reset)", () => {
  // The exact reload scenario from the bug report: matter.docusign.status="sent",
  // no flat fields. Must show the awaiting badge + read as already-sent.
  const view = matterSignatureView({ id: "m1", docusign: { envelope_id: "env-1", status: "sent" } });
  assert.equal(view.sent, true);
  assert.equal(view.tone, "pending");
  assert.equal(view.label, "Awaiting signature");
  assert.equal(view.badge, "Sent for signature");
  assert.equal(view.canDownloadSigned, false);
});

test("matterSignatureView: a freshly-fetched 'completed' matter shows the signed badge + download", () => {
  const view = matterSignatureView({ id: "m1", docusign: { envelope_id: "env-1", status: "completed" } });
  assert.equal(view.completed, true);
  assert.equal(view.tone, "ready");
  assert.equal(view.badge, "Signed");
  assert.equal(view.canDownloadSigned, true);
});

test("matterSignatureView: a never-sent matter (no docusign, no flat) stays idle / no badge", () => {
  const view = matterSignatureView({ id: "m1" });
  assert.equal(view.sent, false);
  assert.equal(view.badge, "");
  assert.equal(view.canDownloadSigned, false);
});

test("DocuSignModel namespace re-exports the nested accessors", () => {
  assert.equal(typeof DocuSignModel.matterSignatureStatus, "function");
  assert.equal(typeof DocuSignModel.matterEnvelopeId, "function");
  assert.equal(DocuSignModel.matterSignatureView({ docusign: { status: "completed" } }).canDownloadSigned, true);
});

// --- prefilled signer rows + signing order ---------------------------------
test("defaultSigners: counterparty (signer 1) prefilled from matter, Aspora (signer 2) from options", () => {
  const signers = defaultSigners(
    { recipient_email: "cp@acme.com", counterparty_name: "Acme Corp" },
    { asporaSignatory: { name: "Jane Aspora", email: "jane@aspora.com" } },
  );
  assert.equal(signers.length, 2);
  assert.deepEqual(signers[0], { role: "counterparty", name: "Acme Corp", email: "cp@acme.com", order: 1 });
  assert.deepEqual(signers[1], { role: "aspora", name: "Jane Aspora", email: "jane@aspora.com", order: 2 });
});

test("defaultSigners: extracts a bare email from a 'Name <addr>' recipient", () => {
  const signers = defaultSigners({ sender: "Bob <bob@acme.com>" }, {});
  assert.equal(signers[0].email, "bob@acme.com");
});

test("defaultSigners: falls back to safe defaults when the matter is empty", () => {
  const signers = defaultSigners({}, {});
  assert.equal(signers[0].name, "Counterparty");
  assert.equal(signers[0].email, "");
  assert.equal(signers[1].name, "Aspora signatory");
});

// --- validation + POST payload ---------------------------------------------
test("validateSigners: rejects an empty list, a missing name, and an invalid email", () => {
  assert.equal(validateSigners([]).ok, false);
  assert.equal(validateSigners([{ name: "", email: "a@b.com" }]).ok, false);
  assert.equal(validateSigners([{ name: "Acme", email: "not-an-email" }]).ok, false);
});

test("validateSigners: accepts and normalises a valid row set", () => {
  const result = validateSigners([
    { role: "counterparty", name: "  Acme  ", email: " cp@acme.com " },
    { role: "aspora", name: "Jane", email: "Jane <jane@aspora.com>" },
  ]);
  assert.equal(result.ok, true);
  assert.equal(result.signers[0].name, "Acme");
  assert.equal(result.signers[0].email, "cp@acme.com");
  assert.equal(result.signers[1].email, "jane@aspora.com");
  assert.equal(result.signers[0].order, 1);
  assert.equal(result.signers[1].order, 2);
});

test("buildSendForSignaturePayload: matches the REST contract body shape", () => {
  const payload = buildSendForSignaturePayload(
    [
      { role: "counterparty", name: "Acme", email: "cp@acme.com" },
      { role: "aspora", name: "Jane", email: "jane@aspora.com" },
    ],
    "sequential",
  );
  assert.deepEqual(payload, {
    signers: [
      { name: "Acme", email: "cp@acme.com", role: "counterparty" },
      { name: "Jane", email: "jane@aspora.com", role: "aspora" },
    ],
    signing_order: "sequential",
  });
});

test("buildSendForSignaturePayload: coerces an unknown signing_order to sequential", () => {
  assert.equal(buildSendForSignaturePayload([], "weird").signing_order, "sequential");
  assert.equal(buildSendForSignaturePayload([], "parallel").signing_order, "parallel");
});

test("DocuSignModel namespace re-exports the same functions", () => {
  assert.equal(typeof DocuSignModel.signatureView, "function");
  assert.equal(typeof DocuSignModel.connectionView, "function");
  assert.equal(DocuSignModel.signatureView("completed").canDownloadSigned, true);
});

process.stdout.write(`\n${passed} passed\n`);
