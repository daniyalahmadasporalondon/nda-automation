// Behaviour tests for the Review workstation's new "Overview" tab.
//
// STATUS: the Overview feature is being built in parallel and is NOT integrated
// at this SHA. These tests therefore FAIL today (by design) and go green once the
// integrator assembles the whole. They are written to FAIL CLEARLY — never to
// crash on import. The Overview module is resolved through a guarded dynamic
// import over a set of plausible module paths + export names; if it (or a needed
// render function) is missing, every test records a single readable assertion
// failure naming the absent symbol, rather than an ERR_MODULE_NOT_FOUND that
// would abort the whole file before a single case is reported.
//
// The three component interfaces under test (provided by the feature owner):
//   renderOverviewRoster(el, {clauses, currentClauseId}, {onClauseClick})
//   renderOverviewFooter(el, {reviewedCount, totalCount, anyFail}, {onApprove, onSend})
//   renderOverviewFacts(el, {counterparty, facts}, {onConfirm, onEntityFill})
//
// Cases (the Overview tab's TARGET behaviour):
//   1. Roster renders clauses sorted problems-first (fail, then review, then pass).
//   2. Clicking a roster row selects that clause AND switches to the Clause tab
//      (the onClauseClick seam fires with the clause id; NO AI re-review request).
//   3. Progress line reads "{reviewed} of {total} clauses reviewed".
//   4. Approve is disabled until reviewed === total; enabled + invokes onApprove
//      when every clause is reviewed.
//   5. Empty state (no AI review yet): the tab shows "No review yet" + a
//      "Refresh with AI" button, and rendering the tab does NOT auto-fire AI.
//   6. The Overview tab is FIRST in the inspector tab order (before Clause /
//      Structure) and is named "Overview" — asserted against the real index.html.
//
// Run: node tests/frontend/overview-tab.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "../..");

// --- guarded module resolution ---------------------------------------------
// The integrator may land the Overview render functions under any of these
// module paths, exported either as bare named exports or behind a namespace
// object (mirroring how DocuSignModel / ReviewWorkstationModel are bundled). We
// try them all. Resolution never throws here; a miss is surfaced per-test.

const CANDIDATE_MODULES = [
  "static/js/modules/overview-tab.mjs",
  "static/js/modules/overview.mjs",
  "static/js/modules/overview-model.mjs",
  "static/js/modules/review-overview.mjs",
];
const NAMESPACE_EXPORTS = ["OverviewTab", "Overview", "OverviewModel", "ReviewOverview"];
const REQUIRED_FNS = ["renderOverviewRoster", "renderOverviewFooter", "renderOverviewFacts"];

let overviewApi = null;
let resolveError = "";

async function resolveOverview() {
  if (overviewApi || resolveError) return;
  for (const rel of CANDIDATE_MODULES) {
    const abs = path.join(ROOT, rel);
    if (!fs.existsSync(abs)) continue;
    let mod;
    try {
      mod = await import(abs);
    } catch (err) {
      resolveError = `Overview module ${rel} failed to import: ${err && err.message}`;
      return;
    }
    // Bare named exports take priority; otherwise look inside a namespace export.
    const flat = {};
    for (const fn of REQUIRED_FNS) {
      if (typeof mod[fn] === "function") flat[fn] = mod[fn];
    }
    if (REQUIRED_FNS.every((fn) => flat[fn])) {
      overviewApi = flat;
      return;
    }
    for (const ns of NAMESPACE_EXPORTS) {
      const space = mod[ns];
      if (space && REQUIRED_FNS.every((fn) => typeof space[fn] === "function")) {
        overviewApi = {
          renderOverviewRoster: space.renderOverviewRoster,
          renderOverviewFooter: space.renderOverviewFooter,
          renderOverviewFacts: space.renderOverviewFacts,
        };
        return;
      }
    }
    const present = REQUIRED_FNS.filter(
      (fn) => typeof mod[fn] === "function" || NAMESPACE_EXPORTS.some((ns) => mod[ns] && typeof mod[ns][fn] === "function"),
    );
    resolveError =
      `Overview module ${rel} is present but does not export ${REQUIRED_FNS.join(", ")}` +
      ` (found: ${present.length ? present.join(", ") : "none"}).`;
    return;
  }
  resolveError =
    "Overview module not found. Expected one of: " +
    CANDIDATE_MODULES.join(", ") +
    " exporting renderOverviewRoster / renderOverviewFooter / renderOverviewFacts" +
    " (as named exports or via an OverviewTab namespace).";
}

// Pull the API or FAIL the current test with the recorded reason. Calling this
// at the top of each behaviour test is what turns "feature absent" into a clean,
// named assertion failure instead of an import crash.
function requireOverview() {
  if (!overviewApi) {
    assert.fail(resolveError || "Overview module unavailable.");
  }
  return overviewApi;
}

// --- minimal fake DOM -------------------------------------------------------
// Enough of the element surface a render-into-el function plausibly touches:
// innerHTML, textContent, classList, dataset, attributes, child tree,
// createElement, querySelector/All over class + [data-*] selectors, and
// click/dispatchEvent so a roster-row click drives the wired handler.

class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  add(...names) {
    names.forEach((n) => this._set.add(n));
  }
  remove(...names) {
    names.forEach((n) => this._set.delete(n));
  }
  toggle(name, force) {
    const on = force === undefined ? !this._set.has(name) : Boolean(force);
    if (on) this._set.add(name);
    else this._set.delete(name);
    return on;
  }
  contains(name) {
    return this._set.has(name);
  }
  get value() {
    return [...this._set].join(" ");
  }
}

class FakeElement {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.attributes = {};
    this.classList = new FakeClassList();
    this.dataset = {};
    this.children = [];
    this.parentNode = null;
    this.value = "";
    this.disabled = false;
    this.title = "";
    this._textContent = "";
    this._innerHTML = "";
    this.isConnected = true;
    this._listeners = {};
  }
  set className(v) {
    this.classList = new FakeClassList();
    String(v || "")
      .split(/\s+/)
      .filter(Boolean)
      .forEach((c) => this.classList.add(c));
  }
  get className() {
    return this.classList.value;
  }
  // innerHTML write clears the live child tree (a render function that paints via
  // innerHTML="..." replaces its content). textContent collapses the subtree.
  set innerHTML(html) {
    this._innerHTML = String(html);
    this.children = [];
  }
  get innerHTML() {
    if (this._innerHTML) return this._innerHTML;
    return this.children.map((c) => c.outerText).join("");
  }
  set textContent(text) {
    this._textContent = String(text);
    this.children = [];
    this._innerHTML = "";
  }
  get textContent() {
    if (this.children.length) {
      return this.children.map((c) => c.textContent).join("");
    }
    return this._textContent;
  }
  get outerText() {
    return this.textContent;
  }
  addEventListener(type, handler) {
    (this._listeners[type] || (this._listeners[type] = [])).push(handler);
  }
  removeEventListener(type, handler) {
    const list = this._listeners[type] || [];
    this._listeners[type] = list.filter((fn) => fn !== handler);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "disabled") this.disabled = value !== false && value != null;
    if (name.startsWith("data-")) {
      const key = name
        .slice(5)
        .replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  getAttribute(name) {
    if (name === "disabled") return this.disabled ? "" : null;
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  hasAttribute(name) {
    if (name === "disabled") return this.disabled;
    return Object.prototype.hasOwnProperty.call(this.attributes, name);
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "disabled") this.disabled = false;
  }
  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
  append(...nodes) {
    nodes.forEach((n) => this.appendChild(typeof n === "string" ? textNode(n) : n));
  }
  collectDescendants() {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  _matches(selector) {
    const sel = selector.trim();
    // class selector: .foo
    if (sel.startsWith(".")) return this.classList.contains(sel.slice(1));
    // attribute selector: [data-x] or [data-x="v"]
    const attr = /^\[([a-z0-9-]+)(?:="(.*)")?\]$/i.exec(sel);
    if (attr) {
      const have = this.getAttribute(attr[1]);
      if (attr[2] === undefined) return have !== null;
      return have === attr[2];
    }
    // tag selector
    return this.tagName === sel.toUpperCase();
  }
  querySelector(selector) {
    // support a comma-separated OR list and a simple "a b" descendant (we only
    // need the rightmost token across the subtree for these tests).
    for (const part of selector.split(",")) {
      const token = part.trim().split(/\s+/).pop();
      for (const child of this.collectDescendants()) {
        if (child._matches(token)) return child;
      }
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    for (const part of selector.split(",")) {
      const token = part.trim().split(/\s+/).pop();
      for (const child of this.collectDescendants()) {
        if (child._matches(token) && !out.includes(child)) out.push(child);
      }
    }
    return out;
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (node._matches && node._matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  dispatchEvent(event) {
    const ev = { target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...event };
    const handlers = this._listeners[ev.type] || [];
    for (const handler of handlers) handler.call(this, ev);
    return true;
  }
  click() {
    this.dispatchEvent({ type: "click" });
  }
}

function textNode(text) {
  const n = new FakeElement("#text");
  n.textContent = String(text);
  return n;
}

function makeDocument() {
  return {
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (t) => textNode(t),
    createDocumentFragment: () => new FakeElement("#fragment"),
  };
}

// Install a document global so a render function can createElement freely. Pure
// string/innerHTML renderers ignore it; node-building renderers use it.
globalThis.document = makeDocument();

// --- fetch recorder (proves no AI re-review fires) --------------------------
// Every render path is exercised with fetch stubbed + recorded. Tests 2 and 5
// assert NO request that looks like an AI review/refresh left the page when the
// Overview tab merely renders or a roster row is selected.

function installFetch() {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), method: (options.method || "GET").toUpperCase() });
    return {
      ok: true,
      status: 200,
      async json() {
        return {};
      },
      async text() {
        return "";
      },
    };
  };
  return calls;
}

const AI_REVIEW_RE = /review|refresh|reassess|re-?review|\/ai|assess/i;
function aiCalls(calls) {
  return calls.filter((c) => AI_REVIEW_RE.test(c.url));
}

// --- runner -----------------------------------------------------------------

let passed = 0;
let failed = 0;
const failures = [];

async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    process.stdout.write(`  ok ${name}\n`);
  } catch (err) {
    failed += 1;
    failures.push({ name, message: err && err.message ? err.message : String(err) });
    process.stdout.write(`  FAIL ${name}\n    ${err && err.message ? err.message : err}\n`);
  }
}

// Resolve the Overview module once, up front. This never throws — a miss leaves
// overviewApi null + resolveError set, which requireOverview() turns into a clear
// per-test failure. (Case 6 needs no module; it asserts against index.html.)
await resolveOverview();

// --- fixtures ---------------------------------------------------------------
// Clause shape mirrors the live review payload: the canonical verdict rides on
// review_state.state ("check" = fail, "review", "pass"); clauseStatus() reads it.

function clause(id, name, state) {
  return {
    id,
    name,
    decision: state === "check" ? "fail" : state,
    passes: state === "pass",
    status: state,
    review_state: { state },
  };
}

// Deliberately out of problems-first order so the sort is observable.
const MIXED_CLAUSES = [
  clause("mutuality", "Mutuality", "pass"),
  clause("term", "Term", "review"),
  clause("non_compete", "Non-compete", "check"),
  clause("governing_law", "Governing law", "pass"),
  clause("confidentiality", "Confidentiality", "review"),
];

// Read the rendered row order out of the roster element, tolerant of however the
// feature marks rows (a [data-clause-id] hook is the natural contract; fall back
// to scanning text for clause display names).
function rosterClauseOrder(el) {
  const rows = el.querySelectorAll("[data-clause-id]");
  if (rows.length) return rows.map((r) => r.getAttribute("data-clause-id") || r.dataset.clauseId);
  // Fallback: derive order from where each clause name first appears in the text.
  const text = el.textContent || el.innerHTML;
  return [...MIXED_CLAUSES]
    .map((c) => ({ id: c.id, at: text.indexOf(c.name) }))
    .filter((x) => x.at >= 0)
    .sort((a, b) => a.at - b.at)
    .map((x) => x.id);
}

// ===========================================================================
// Case 1 — roster is sorted problems-first (fail, then review, then pass).
// ===========================================================================
await test("1. roster renders clauses sorted problems-first (fail > review > pass)", async () => {
  const { renderOverviewRoster } = requireOverview();
  const el = new FakeElement("div");
  installFetch();
  renderOverviewRoster(el, { clauses: MIXED_CLAUSES, currentClauseId: null }, { onClauseClick() {} });

  const order = rosterClauseOrder(el);
  assert.ok(order.length >= MIXED_CLAUSES.length, `roster did not render all clauses (saw ${order.length})`);

  const rank = (id) => {
    const c = MIXED_CLAUSES.find((x) => x.id === id);
    const s = c && c.review_state.state;
    return s === "check" ? 0 : s === "review" ? 1 : 2; // fail < review < pass
  };
  const ranks = order.map(rank);
  for (let i = 1; i < ranks.length; i += 1) {
    assert.ok(
      ranks[i] >= ranks[i - 1],
      `roster not problems-first: '${order[i - 1]}'(rank ${ranks[i - 1]}) precedes '${order[i]}'(rank ${ranks[i]})`,
    );
  }
  // And concretely: the failing clause leads, a passing clause trails.
  assert.equal(order[0], "non_compete", "the failing clause must lead the roster");
  assert.ok(["mutuality", "governing_law"].includes(order[order.length - 1]), "a passing clause must trail the roster");
});

// ===========================================================================
// Case 2 — clicking a roster row selects the clause AND switches to Clause tab,
//          with NO AI re-review fired.
// ===========================================================================
await test("2. clicking a roster row invokes onClauseClick(clauseId) and fires no AI re-review", async () => {
  const { renderOverviewRoster } = requireOverview();
  const el = new FakeElement("div");
  const calls = installFetch();

  const clicked = [];
  renderOverviewRoster(
    el,
    { clauses: MIXED_CLAUSES, currentClauseId: null },
    { onClauseClick: (id) => clicked.push(id) },
  );

  // Find the row for a specific clause and click it. Prefer the [data-clause-id]
  // contract; otherwise click the first interactive descendant.
  const targetId = "term";
  let row =
    el.querySelector(`[data-clause-id="${targetId}"]`) ||
    el.querySelectorAll("[data-clause-id]").find((r) => (r.getAttribute("data-clause-id") || r.dataset.clauseId) === targetId);
  assert.ok(row, "no clickable roster row carrying a data-clause-id was rendered");
  row.click();

  assert.ok(clicked.length >= 1, "clicking a roster row did not invoke the onClauseClick seam");
  assert.equal(
    clicked[clicked.length - 1],
    targetId,
    "onClauseClick must receive the clicked clause id (so the host selects it AND switches to the Clause tab)",
  );
  // The seam is selection-only; the tab must NOT kick off a fresh AI review.
  assert.equal(
    aiCalls(calls).length,
    0,
    `selecting a clause fired an AI re-review request: ${aiCalls(calls).map((c) => c.url).join(", ")}`,
  );
});

// ===========================================================================
// Case 3 — progress line reads "{reviewed} of {total} clauses reviewed".
// ===========================================================================
await test("3. footer progress line reads '{reviewed} of {total} clauses reviewed'", async () => {
  const { renderOverviewFooter } = requireOverview();
  const el = new FakeElement("div");
  installFetch();
  renderOverviewFooter(
    el,
    { reviewedCount: 3, totalCount: 7, anyFail: true },
    { onApprove() {}, onSend() {} },
  );
  const text = (el.textContent || "") + " " + (el.innerHTML || "");
  assert.match(
    text,
    /3\s+of\s+7\s+clauses\s+reviewed/i,
    `progress line missing or malformed; rendered text was: ${JSON.stringify(text.trim())}`,
  );
});

// ===========================================================================
// Case 4 — Approve disabled until reviewed === total; enabled + calls onApprove
//          when all reviewed.
// ===========================================================================
function approveButton(el) {
  return (
    el.querySelector("[data-overview-approve]") ||
    el.querySelector("[data-approve]") ||
    el.querySelectorAll("button").find((b) => /approve/i.test(b.textContent || "")) ||
    null
  );
}

await test("4a. Approve is DISABLED while reviewed < total", async () => {
  const { renderOverviewFooter } = requireOverview();
  const el = new FakeElement("div");
  installFetch();
  renderOverviewFooter(
    el,
    { reviewedCount: 4, totalCount: 7, anyFail: false },
    { onApprove() {}, onSend() {} },
  );
  const btn = approveButton(el);
  assert.ok(btn, "no Approve control was rendered in the footer");
  assert.ok(
    btn.disabled === true || btn.hasAttribute("disabled"),
    "Approve must be disabled until every clause is reviewed",
  );
});

await test("4b. Approve is ENABLED and invokes onApprove once reviewed === total", async () => {
  const { renderOverviewFooter } = requireOverview();
  const el = new FakeElement("div");
  installFetch();
  let approvedCalls = 0;
  renderOverviewFooter(
    el,
    { reviewedCount: 7, totalCount: 7, anyFail: false },
    { onApprove: () => { approvedCalls += 1; }, onSend() {} },
  );
  const btn = approveButton(el);
  assert.ok(btn, "no Approve control was rendered in the footer");
  assert.ok(
    btn.disabled === false && !btn.hasAttribute("disabled"),
    "Approve must be enabled once reviewed === total",
  );
  btn.click();
  assert.equal(approvedCalls, 1, "clicking an enabled Approve must invoke the onApprove action exactly once");
});

// ===========================================================================
// Case 5 — empty state: no review yet -> "No review yet" + "Refresh with AI",
//          and rendering the empty tab does NOT auto-fire AI.
// ===========================================================================
function refreshButton(el) {
  return (
    el.querySelector("[data-overview-refresh]") ||
    el.querySelector("[data-refresh-ai]") ||
    el.querySelectorAll("button").find((b) => /refresh with ai/i.test(b.textContent || "")) ||
    null
  );
}

await test("5. empty state shows 'No review yet' + 'Refresh with AI' and does NOT auto-fire AI", async () => {
  const { renderOverviewRoster, renderOverviewFooter } = requireOverview();
  const calls = installFetch();

  // No AI review has run: an empty clause set is the "not reviewed yet" signal.
  const rosterEl = new FakeElement("div");
  renderOverviewRoster(rosterEl, { clauses: [], currentClauseId: null }, { onClauseClick() {} });

  const footerEl = new FakeElement("div");
  renderOverviewFooter(
    footerEl,
    { reviewedCount: 0, totalCount: 0, anyFail: false },
    { onApprove() {}, onSend() {} },
  );

  const combined =
    (rosterEl.textContent || "") + " " + (rosterEl.innerHTML || "") +
    " " + (footerEl.textContent || "") + " " + (footerEl.innerHTML || "");

  assert.match(combined, /no review yet/i, "empty state must show a 'No review yet' message");

  const refresh = refreshButton(rosterEl) || refreshButton(footerEl);
  assert.ok(refresh, "empty state must offer a 'Refresh with AI' button");

  // The crucial guard: merely opening/rendering the empty tab must NOT auto-run
  // AI (that was the cost-storm class of bug). AI runs only when the user clicks.
  assert.equal(
    aiCalls(calls).length,
    0,
    `rendering the empty Overview tab auto-fired an AI request: ${aiCalls(calls).map((c) => c.url).join(", ")}`,
  );
});

// ===========================================================================
// Case 6 — Overview is FIRST in the inspector tab order and named "Overview".
//          Asserted against the real shipped index.html (the integrated whole).
// ===========================================================================
await test("6. Overview is the FIRST inspector tab (before Clause/Structure) and is named 'Overview'", async () => {
  const html = fs.readFileSync(path.join(ROOT, "static", "index.html"), "utf8");

  // Isolate the inspector tablist block, then read its buttons in document order.
  const blockMatch = /<div class="studio-inspector-tabs"[\s\S]*?<\/div>/i.exec(html);
  assert.ok(blockMatch, "could not locate the .studio-inspector-tabs block in index.html");
  const block = blockMatch[0];

  const buttons = [...block.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/gi)].map((m) => ({
    attrs: m[0],
    label: m[1].replace(/<[^>]*>/g, "").trim(),
  }));
  assert.ok(buttons.length >= 1, "no inspector tab buttons found");

  // data-review-inspector key order
  const keys = buttons.map((b) => {
    const k = /data-review-inspector="([^"]+)"/i.exec(b.attrs);
    return k ? k[1] : "";
  });

  assert.equal(keys[0], "overview", `first inspector tab must be 'overview', got '${keys[0]}' (order: ${keys.join(" > ")})`);
  assert.equal(
    buttons[0].label.toLowerCase(),
    "overview",
    `first inspector tab must be labelled 'Overview', got '${buttons[0].label}'`,
  );
  // Overview precedes both Clause and Structure.
  const clauseIdx = keys.indexOf("clause");
  const structureIdx = keys.indexOf("structure");
  assert.ok(clauseIdx === -1 || clauseIdx > 0, "Overview must precede the Clause tab");
  assert.ok(structureIdx === -1 || structureIdx > 0, "Overview must precede the Structure tab");
});

// ===========================================================================
// Bonus — renderOverviewFacts surface exists and renders the counterparty +
//          a confirm/entity-fill affordance (the third documented interface).
//          Asserted lightly so it fails clear if the symbol is absent.
// ===========================================================================
await test("7. renderOverviewFacts renders the counterparty + confirm/entity-fill affordance", async () => {
  const { renderOverviewFacts } = requireOverview();
  const el = new FakeElement("div");
  installFetch();
  let confirmed = 0;
  let filled = 0;
  renderOverviewFacts(
    el,
    { counterparty: "Acme Robotics Ltd", facts: { governing_law: "Delaware", term_years: 3 } },
    { onConfirm: () => { confirmed += 1; }, onEntityFill: () => { filled += 1; } },
  );
  const text = (el.textContent || "") + " " + (el.innerHTML || "");
  assert.match(text, /Acme Robotics Ltd/, "facts panel must show the counterparty name");

  // At least one affordance must be wired to its handler.
  const action =
    el.querySelector("[data-overview-confirm]") ||
    el.querySelector("[data-overview-entity-fill]") ||
    el.querySelectorAll("button").find((b) => /(confirm|fill)/i.test(b.textContent || "")) ||
    null;
  assert.ok(action, "facts panel must offer a confirm or entity-fill affordance");
  action.click();
  assert.ok(confirmed + filled >= 1, "the facts affordance must invoke its onConfirm/onEntityFill handler");
});

// --- summary ----------------------------------------------------------------
process.stdout.write(`\n${passed} passed, ${failed} failed\n`);
if (failed > 0) {
  process.stdout.write(`\nThese cases fail until the Overview feature is integrated (expected at this SHA):\n`);
  for (const f of failures) process.stdout.write(`  - ${f.name}: ${f.message}\n`);
  process.exitCode = 1;
}
