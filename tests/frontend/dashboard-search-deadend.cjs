"use strict";

// Frontend behavioural test for the dashboard smart-search controller
// (static/js/dashboard-search.js), covering FIX 1: the "Moorwand" dead-end.
//
// Bug: when the assistant classifies a query it can't structure (most commonly a
// BARE COUNTERPARTY NAME like "Moorwand") it returns an `unsupported` intent. The
// controller used to render a terminal "Unsupported request" help card AND early-
// return, so the deterministic keyword fallback never ran — the matter list never
// filtered. Fix: an `unsupported` intent now falls back to the v1 keyword filter
// (filterMattersByText / renderKeywordResults), so a bare name (and ANY unclassified
// query) filters the documents below instead of dead-ending.
//
// Zero-dep, hand-rolled DOM matching the repo's FE harness style. The REAL pure
// filters (.mjs) are wired onto window.DashboardSearch so the keyword fallback runs
// for real, not against a stub.

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

// Load the REAL pure filters and expose them the way the deferred bridge does, so the
// controller's lib() (window.DashboardSearch) runs the production keyword filter.
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

function installGlobals(lib) {
  global.window = {
    DashboardSearch: lib,
    escapeHtml: (value) => String(value == null ? "" : value),
    addEventListener() {},
    location: { search: "" },
  };
  global.document = { addEventListener() {} };
  // The controller reads window.escapeHtml; mirror it onto the global the helper checks.
  global.escapeHtml = global.window.escapeHtml;
}

const MATTERS = [
  { id: "m_moorwand", subject: "Moorwand Ltd NDA", counterparty: "Moorwand", workflow_state: { status: "awaiting_approval" } },
  { id: "m_globex", subject: "Globex deal", counterparty: "Globex", workflow_state: { status: "sent_awaiting_counterparty" } },
];

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

  // The assistant seam: classify EVERY query as `unsupported` (the exact bug case for
  // a bare counterparty name the assistant can't structure into a command/filter).
  const assistantCalls = [];
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
    assistantQuery: (query) => {
      assistantCalls.push(query);
      return Promise.resolve({ ok: true, payload: { intent: "unsupported", query } });
    },
  });

  const submit = () => form.dispatch("submit", { preventDefault() {} });
  const flush = async () => {
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setImmediate(resolve));
  };

  // --- Case 1: a bare counterparty name FILTERS the list, not the help card ----
  input.value = "Moorwand";
  submit(); // a free-text submit runs runTextSearch()
  // The controller awaits the assistant + a matter load; give microtasks time to flush.
  await flush();

  assert.deepEqual(assistantCalls, ["Moorwand"], "the assistant was consulted for the bare name");
  // The keyword fallback rendered REAL matter rows, NOT the unsupported card.
  assert.ok(
    !/Unsupported request/i.test(resultsList.innerHTML),
    "an unsupported intent must NOT render the terminal help card"
  );
  assert.ok(
    resultsList.innerHTML.includes("m_moorwand"),
    "the bare name 'Moorwand' filters the matter list to the matching matter"
  );
  assert.ok(
    !resultsList.innerHTML.includes("m_globex"),
    "non-matching matters are excluded by the keyword filter"
  );
  assert.equal(resultsList.hidden, false, "the results list is shown");

  // --- Case 2: any unclassified query that doesn't match -> honest empty msg ----
  input.value = "zzz-no-such-counterparty";
  submit();
  await flush();
  assert.ok(
    !/Unsupported request/i.test(resultsList.innerHTML),
    "an unmatched unclassified query still uses keyword fallback, not the help card"
  );
  assert.equal(resultsList.hidden, true, "no rows render for an unmatched query");
  assert.match(
    resultsStatus.textContent,
    /No documents match your search/i,
    "the honest keyword empty message is shown, not a dead-end help card"
  );

  console.log("dashboard-search-deadend.cjs: all assertions passed");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
