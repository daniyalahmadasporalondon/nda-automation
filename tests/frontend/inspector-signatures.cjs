"use strict";

// Frontend unit test for the matter-inspector SIGNATURES section.
//
// static/js/repository-detail.js is a classic browser script (an IIFE assigning
// `const RepositoryDetail = ...`). It runs in the page AFTER overview/signatures.js
// has loaded, so at render time it prefers the bridged window.signatureParties;
// when that helper is absent it falls back to a behaviour-identical local replica
// (inspectorSignatureParties). We exercise BOTH paths here in a vm sandbox, and we
// require the REAL overview/signatures.js signatureParties to prove the inspector
// and the Overview block can never disagree on the per-party model.
//
// Covers the product cases the task names: not-sent -> both Pending; executed ->
// both Signed; 1/2 envelope -> one Signed one Pending (and WHICH party); declined
// party -> Declined. Plus a no-regression check that the inspector still renders
// the other sections (metadata / gmail routing / timeline).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..", "..");
const staticDir = path.join(ROOT, "static");

// The tested Overview helper, loaded straight via its CommonJS guard.
const { signatureParties } = require(
  path.join(staticDir, "js", "overview", "signatures.js"),
);

// --- sandbox ----------------------------------------------------------------
// repository-detail.js references globals (escapeHtml, RepositoryModel,
// MatterUtils, RepositorySend, clauseStatus, ...) only INSIDE functions, so the
// IIFE evaluates fine with light stubs. We capture the RepositoryDetail binding
// via a trailing eval in the same context (vm const-binding semantics).
function loadRepositoryDetail(withBridgedHelper) {
  const sandbox = {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.console = console;
  sandbox.module = undefined; // suppress any CommonJS guard

  // Minimal globals the render path touches. We only render the SIGNATURES path
  // and the surrounding sections, so stub the helpers those need.
  sandbox.escapeHtml = (v) => String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  sandbox.clauseStatus = () => ({ requiresAttention: false, needsReview: false });
  sandbox.RepositoryModel = {
    matterSubject: () => "Subject",
    sourceTypeLabel: () => "Gmail",
    statusLabel: () => "Status",
    triageLabel: () => "Route",
    formatMatterDateTime: (v) => v || "-",
    reviewCountSummary: () => "0 checks",
    playbookMatchLabel: () => "match",
    boardColumnLabel: () => "Column",
    matterSender: () => "sender@acme.com",
  };
  sandbox.MatterUtils = {
    recipientEmail: () => "cp@acme.com",
    gmailSendBlock: () => "",
    gmailSendButtonLabel: () => "Send Redline",
    reviewStale: () => false,
    reviewStaleLabel: () => "",
    reviewActionable: () => false,
    reviewNeverRan: () => false,
  };
  sandbox.RepositorySend = { renderSendComposer: () => "" };

  if (withBridgedHelper) {
    sandbox.signatureParties = signatureParties; // bridge the real helper
  }

  vm.createContext(sandbox);
  let code = fs.readFileSync(path.join(staticDir, "js", "repository-detail.js"), "utf8");
  code += "\n;globalThis.RepositoryDetail = RepositoryDetail;";
  vm.runInContext(code, sandbox, { filename: "repository-detail.js" });
  return { RepositoryDetail: sandbox.RepositoryDetail, sandbox };
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// Convenience: render the SIGNATURES section HTML for a matter, on the chosen
// helper path (bridged real helper vs local replica fallback).
function sigHtml(matter, withBridgedHelper) {
  const { RepositoryDetail } = loadRepositoryDetail(withBridgedHelper);
  return RepositoryDetail.renderInspectorSignatures(matter);
}

// --- fixtures ---------------------------------------------------------------

const NOT_SENT = { docusign: {} };
const EXECUTED_OFF_PLATFORM = { executed: true };
const EXECUTED_STATUS = { status: "fully_signed" };
const ONE_SIGNED = {
  docusign: {
    envelope_id: "env-1",
    signers: [
      { role: "aspora", signature_status: "signed", signed_at: "2026-06-17T10:00:00+00:00" },
      { role: "counterparty", signature_status: "awaiting" },
    ],
  },
};
const BOTH_SIGNED = {
  docusign: {
    envelope_id: "env-1",
    signers: [
      { role: "aspora", signature_status: "signed" },
      { role: "counterparty", signature_status: "signed" },
    ],
  },
};
const DECLINED = {
  docusign: {
    envelope_id: "env-1",
    signers: [
      { role: "aspora", signature_status: "signed" },
      { role: "counterparty", signature_status: "declined" },
    ],
  },
};

// Both helper paths must produce identical text; run every case on both.
[true, false].forEach((bridged) => {
  const tag = bridged ? "[bridged window.signatureParties]" : "[local replica fallback]";

  test(`${tag} not-sent matter -> Aspora Pending + Counterparty Pending`, () => {
    const html = sigHtml(NOT_SENT, bridged);
    assert.match(html, /Aspora/);
    assert.match(html, /Counterparty/);
    // Exactly two "Pending", no "Signed".
    assert.equal((html.match(/Pending/g) || []).length, 2);
    assert.equal((html.match(/Signed/g) || []).length, 0);
  });

  test(`${tag} executed-off-platform (executed:true) -> both Signed`, () => {
    const html = sigHtml(EXECUTED_OFF_PLATFORM, bridged);
    assert.equal((html.match(/Signed/g) || []).length, 2);
    assert.equal((html.match(/Pending/g) || []).length, 0);
  });

  test(`${tag} executed via status fully_signed -> both Signed`, () => {
    const html = sigHtml(EXECUTED_STATUS, bridged);
    assert.equal((html.match(/Signed/g) || []).length, 2);
  });

  test(`${tag} 1/2 envelope -> one Signed one Pending`, () => {
    const html = sigHtml(ONE_SIGNED, bridged);
    assert.equal((html.match(/Signed/g) || []).length, 1);
    assert.equal((html.match(/Pending/g) || []).length, 1);
    // Aspora is the signed one (its dt/dd precede the counterparty's).
    const asporaIdx = html.indexOf("Aspora");
    const cpIdx = html.indexOf("Counterparty");
    const signedIdx = html.indexOf("Signed");
    assert.ok(asporaIdx < cpIdx, "Aspora row before Counterparty row");
    assert.ok(signedIdx > asporaIdx && signedIdx < cpIdx, "Signed belongs to the Aspora row");
  });

  test(`${tag} declined counterparty -> Declined`, () => {
    const html = sigHtml(DECLINED, bridged);
    assert.match(html, /Declined/);
    assert.equal((html.match(/Signed/g) || []).length, 1);
  });
});

// --- parity with the Overview block ----------------------------------------
// The inspector's local replica must map status the same way the real Overview
// helper does, so the two surfaces never disagree.
test("local replica status maps match the real Overview signatureParties", () => {
  const { RepositoryDetail } = loadRepositoryDetail(false);
  const collapse = (s) => (s === "signed" ? "Signed" : s === "declined" ? "Declined" : "Pending");
  [NOT_SENT, EXECUTED_OFF_PLATFORM, EXECUTED_STATUS, ONE_SIGNED, BOTH_SIGNED, DECLINED].forEach((m) => {
    const real = signatureParties(m).map((p) => `${p.label}:${collapse(p.status)}`);
    const html = RepositoryDetail.renderInspectorSignatures(m);
    real.forEach((pair) => {
      const [label, text] = pair.split(":");
      // The inspector renders label as a <dt> and the collapsed status as its <dd>.
      const re = new RegExp(`<dt>${label}</dt>\\s*<dd>${text}</dd>`);
      assert.match(html.replace(/\s+/g, " "), re);
    });
  });
});

// --- no-regression: other inspector sections still render -------------------
test("renderDetailPanel still renders all sections PLUS Signatures", () => {
  const { RepositoryDetail } = loadRepositoryDetail(true);
  let captured = "";
  const panel = {
    set innerHTML(v) { captured = v; },
    get innerHTML() { return captured; },
    hidden: true,
    setAttribute() {},
    querySelector() { return { addEventListener() {} }; },
  };
  RepositoryDetail.renderDetailPanel({
    handlers: {},
    matter: ONE_SIGNED,
    pendingSendMatterId: null,
    repositoryMatterPanel: panel,
    repositoryWorkspace: { classList: { remove() {}, add() {} } },
    state: { gmailStatus: {}, personalisationSettings: {} },
  });
  assert.match(captured, /Metadata Details/);
  assert.match(captured, /Gmail Routing/);
  assert.match(captured, /Review Checks/);
  assert.match(captured, /NDA Timeline/);
  assert.match(captured, /Signatures/);
  // The new section sits before the timeline in the side column.
  assert.ok(captured.indexOf("Signatures") < captured.indexOf("NDA Timeline"));
});

process.stdout.write(`\n${passed} passed\n`);
