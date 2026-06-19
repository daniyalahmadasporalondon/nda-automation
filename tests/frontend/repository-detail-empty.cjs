"use strict";

// Regression: a fresh/empty user must never get a THROWN render that blanks the
// matter inspector panel.
//
// RepositoryDetail.renderDetailPanel is called as renderDetailPanel(matter) where
// `matter` is whatever api.getMatter() returned -- i.e. `payload.matter`. For a
// fresh user opening a matter whose fetch came back empty ({} / {error:...} / no
// body), that value is `undefined`. The render used to immediately do
// `const reviewResult = matter.review_result || {}`, which throws
// `Cannot read properties of undefined (reading 'review_result')`, aborts the
// render, and leaves the inspector panel blank/stale. openMatter() only
// console.warns the throw, so the user sees an empty panel with no explanation.
//
// renderDetailPanel must instead FAIL SAFE on a missing/non-object matter: keep
// the panel hidden and return, exactly as it does for a missing panel node. This
// test drives the REAL shipped repository-detail.js through the same vm-context
// loader inspector-signatures.cjs uses, and asserts the no-throw + hidden-panel
// contract. It is RED on the pre-fix code (the deref throws) and GREEN with the
// guard.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticDir = path.join(__dirname, "..", "..", "static");

// Load the real RepositoryDetail IIFE in a vm context with the light global stubs
// the render path references. Mirrors inspector-signatures.cjs::loadRepositoryDetail.
function loadRepositoryDetail() {
  const sandbox = {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.console = console;
  sandbox.module = undefined; // suppress any CommonJS guard

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

  vm.createContext(sandbox);
  let code = fs.readFileSync(path.join(staticDir, "js", "repository-detail.js"), "utf8");
  code += "\n;globalThis.RepositoryDetail = RepositoryDetail;";
  vm.runInContext(code, sandbox, { filename: "repository-detail.js" });
  return sandbox.RepositoryDetail;
}

// A tiny fake panel node modelling only what renderDetailPanel touches before it
// would have thrown: the `hidden` flag, `innerHTML`, and setAttribute.
function makePanel() {
  return {
    hidden: false,
    innerHTML: "<stale>previous matter</stale>",
    _attrs: {},
    setAttribute(name, value) { this._attrs[name] = value; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

const RepositoryDetail = loadRepositoryDetail();

// The empty-fetch shapes a fresh user's api.getMatter() can resolve to.
const EMPTY_MATTER_VALUES = [
  ["undefined (getMatter returned a body with no `matter`)", undefined],
  ["null", null],
];

EMPTY_MATTER_VALUES.forEach(([label, value]) => {
  test(`renderDetailPanel does NOT throw on an empty matter -> ${label}`, () => {
    const repositoryMatterPanel = makePanel();
    assert.doesNotThrow(() => {
      RepositoryDetail.renderDetailPanel({
        handlers: {},
        matter: value,
        pendingSendMatterId: null,
        repositoryMatterPanel,
        repositoryWorkspace: null,
        state: { gmailStatus: {} },
      });
    }, "a fresh user opening a matter with an empty fetch must not crash the inspector render");
  });

  test(`renderDetailPanel keeps the panel hidden (fail-safe) on an empty matter -> ${label}`, () => {
    const repositoryMatterPanel = makePanel();
    RepositoryDetail.renderDetailPanel({
      handlers: {},
      matter: value,
      pendingSendMatterId: null,
      repositoryMatterPanel,
      repositoryWorkspace: null,
      state: { gmailStatus: {} },
    });
    assert.equal(repositoryMatterPanel.hidden, true, "an empty matter must leave the inspector panel hidden, not half-rendered/visible");
  });
});

// Guardrail: a real matter object still renders (the guard must not swallow the
// normal path). We only assert the panel becomes visible and gets content -- the
// rich rendering is covered elsewhere (inspector-signatures.cjs et al.).
test("renderDetailPanel still renders a real matter (guard does not over-fire)", () => {
  const repositoryMatterPanel = makePanel();
  RepositoryDetail.renderDetailPanel({
    handlers: {},
    matter: { id: "matter_1", review_result: {}, ai_review_ran: false },
    pendingSendMatterId: null,
    repositoryMatterPanel,
    repositoryWorkspace: null,
    state: { gmailStatus: {} },
  });
  assert.equal(repositoryMatterPanel.hidden, false, "a real matter must show the inspector panel");
  assert.ok(
    typeof repositoryMatterPanel.innerHTML === "string" && repositoryMatterPanel.innerHTML.length > 0,
    "a real matter must populate the inspector panel",
  );
});

process.stdout.write(`\nrepository-detail-empty: ${passed} passed\n`);
