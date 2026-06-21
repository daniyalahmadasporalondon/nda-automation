"use strict";

// Frontend behavioural test for the dashboard smart-search controller
// (static/js/dashboard-search.js) — HUMANIZATION pass.
//
// Guards that the assistant SURFACES a user sees never leak internal
// implementation detail:
//   * NO "Route: POST /api/..." line on an action card or in the confirm dialog
//     (the human-summary line already explains the action in plain English).
//   * NO bare REST path ("/api/...") anywhere in the rendered card / dialog text.
//   * NO raw snake_case enum tokens (gmail_inbound, system_question, sends_email,
//     review_finding, ...) — they are mapped to curated labels or omitted.
//
// The test PROVES the leak on base: against the pre-fix dashboard-search.js the
// action card and the confirm dialog both print "Route: POST /api/gmail/send-redline".
//
// Zero-dep, hand-rolled DOM in the repo's FE harness style. The REAL pure filters
// (.mjs) are wired onto window.DashboardSearch so the controller runs for real.

const assert = require("node:assert/strict");
const path = require("node:path");

// --- minimal DOM / window stubs ---------------------------------------------

function makeNode(extra = {}) {
  const listeners = {};
  return {
    listeners,
    hidden: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    value: "",
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(...cs) { cs.forEach((c) => this._set.delete(c)); },
      toggle(c, on) { if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    dispatch(type, event = {}) { (listeners[type] || []).forEach((fn) => fn(event)); },
    ...extra,
  };
}

async function loadDashboardSearchLib() {
  const mod = await import(
    path.join(__dirname, "..", "..", "static", "js", "modules", "dashboard-search.mjs")
  );
  return {
    filterMattersByText: mod.filterMattersByText,
    filterSpecIsEmpty: mod.filterSpecIsEmpty,
    validateFilterSpec: mod.validateFilterSpec,
    applyFilterSpec: mod.applyFilterSpec,
    chipById: mod.chipById,
    runChip: mod.runChip,
    setFilterSpecAllowlists: mod.setFilterSpecAllowlists,
  };
}

let confirmLines = "";
function installGlobals(lib) {
  global.window = {
    DashboardSearch: lib,
    escapeHtml: (value) => String(value == null ? "" : value),
    addEventListener() {},
    location: { search: "" },
    // Capture the confirm() body and decline, so no real action fires.
    confirm: (text) => { confirmLines = String(text || ""); return false; },
  };
  global.document = { addEventListener() {} };
  global.escapeHtml = global.window.escapeHtml;
}

const MATTERS = [
  { id: "m_acme", subject: "Acme NDA", counterparty: "Acme" },
];

// Strip HTML tags so we test the VISIBLE text a user reads, not machine-only
// attributes (e.g. data-dashboard-assistant-action="send_redline" — a legit DOM
// hook the user never sees). For plain dialog text this is a no-op.
function visibleText(html) {
  return String(html || "").replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

// Patterns a regular user must never SEE in assistant card / dialog text.
const FORBIDDEN_TEXT = [
  { name: "REST path", re: /\/api\// },
  { name: "Route: line", re: /Route:/ },
  // Raw snake_case token (word fragments joined by underscore). The curated labels
  // are space-separated Title Case, so any underscore-joined token in visible text
  // is an un-humanized leak.
  { name: "raw snake_case token", re: /[a-z]+_[a-z]+/ },
];

function assertClean(label, html) {
  // /api/ and Route: must not appear ANYWHERE — not even in an attribute.
  assert.ok(!/\/api\//.test(html), `${label}: must not contain a /api/ path anywhere — got: ${JSON.stringify(html)}`);
  assert.ok(!/Route:/.test(html), `${label}: must not contain a 'Route:' line anywhere — got: ${JSON.stringify(html)}`);
  const text = visibleText(html);
  FORBIDDEN_TEXT.forEach(({ name, re }) => {
    assert.ok(
      !re.test(text),
      `${label}: visible text must not contain ${name} — got: ${JSON.stringify(text)}`
    );
  });
}

async function run() {
  const lib = await loadDashboardSearchLib();
  installGlobals(lib);

  const { createDashboardSearchController } = require(
    path.join(__dirname, "..", "..", "static", "js", "dashboard-search.js")
  );

  const root = makeNode();
  const input = makeNode();
  const form = makeNode();
  const resultsList = makeNode();
  const resultsStatus = makeNode();
  const interpretedLine = makeNode();

  // The exact leak case: a send-redline action carrying an internal route +
  // snake_case side-effect token.
  const ACTION_PAYLOAD = {
    intent: "action_request",
    action: "send_redline",
    message: "Confirm before anything changes.",
    human_summary: "Send the reviewed redline back to the counterparty by email.",
    requires_confirmation: true,
    route: { method: "post", url: "/api/gmail/send-redline" },
    matter: { title: "Acme NDA", resolved_recipient: "legal@acme.test" },
    side_effects: ["sends_email"],
  };

  // A repository answer carrying capability domains, search hits, and a citation
  // with a raw workflow_phase — all of which used to passthrough as snake_case.
  const ANSWER_PAYLOAD = {
    intent: "repository_question",
    answer: {
      text: "Here is what I found.",
      domains: ["gmail", "review", "playbook"],
      capabilities: [
        { domain: "gmail", description: "Inspect Gmail connection." },
        { domain: "review", description: "Explain review findings." },
      ],
      hits: [
        { type: "review_finding", title: "Confidentiality term", snippet: "5 years" },
        { source: "gmail_inbound", title: "Acme intro" },
      ],
    },
    citations: [
      { title: "Acme NDA", workflow_phase: "gmail_inbound" },
    ],
  };

  let mode = "action";
  const controller = createDashboardSearchController({
    root,
    input,
    form,
    resultsList,
    resultsStatus,
    interpretedLine,
    getMatters: () => MATTERS,
    ensureMatters: () => Promise.resolve(MATTERS),
    openMatter() {},
    confirmAssistantAction: () => Promise.resolve({ ok: true }),
    assistantQuery: () => Promise.resolve({
      ok: true,
      payload: mode === "action" ? ACTION_PAYLOAD : ANSWER_PAYLOAD,
    }),
  });

  const submit = () => form.dispatch("submit", { preventDefault() {} });
  const flush = async () => {
    for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
  };

  // --- Case 1: action card text is clean --------------------------------------
  mode = "action";
  input.value = "send the redline to acme";
  submit();
  await flush();

  const cardHtml = resultsList.innerHTML;
  assert.ok(/Will happen:/.test(cardHtml), "the action card shows the plain-English 'Will happen:' line");
  assert.ok(!/Route:/.test(cardHtml), "the action card shows NO 'Route:' line");
  assert.ok(!/\/api\//.test(cardHtml), "the action card shows NO raw /api/ path");
  assertClean("action card", cardHtml);

  // --- Case 2: confirm dialog text is clean -----------------------------------
  // Drive the real resultsList click handler so runAssistantAction ->
  // confirmAssistantActionDialog builds the dialog body for the SAME action.
  const actionButton = { dataset: { dashboardAssistantAction: "send_redline" }, disabled: false, isConnected: true };
  resultsList.dispatch("click", {
    target: {
      closest: (selector) => (selector === "[data-dashboard-assistant-action]" ? actionButton : null),
    },
  });
  await flush();
  assert.ok(confirmLines.length > 0, "the confirm dialog was shown for the action");
  assert.ok(/Acme NDA/.test(confirmLines), "the confirm dialog still names the NDA in plain English");
  assert.ok(!/Route:/.test(confirmLines), "the confirm dialog shows NO 'Route:' line");
  assert.ok(!/\/api\//.test(confirmLines), "the confirm dialog shows NO raw /api/ path");
  assert.ok(!/sends_email/.test(confirmLines), "the confirm dialog does NOT print the raw side-effect token");
  assertClean("confirm dialog", confirmLines);

  // --- Case 3: a gmail_inbound workflow_phase / hit renders as a friendly label
  mode = "answer";
  input.value = "what did we find for acme";
  submit();
  await flush();
  const answerHtml = resultsList.innerHTML;
  assert.ok(/Inbox/.test(answerHtml), "a gmail_inbound workflow_phase / hit renders the friendly label 'Inbox'");
  assert.ok(!/gmail_inbound/.test(answerHtml), "the raw 'gmail_inbound' token never reaches the user");
  assert.ok(!/review_finding/.test(answerHtml), "the raw 'review_finding' hit kind is humanized");
  assertClean("answer card", answerHtml);

  console.log("dashboard-humanize.cjs: all assertions passed");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
