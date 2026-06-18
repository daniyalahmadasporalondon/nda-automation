"use strict";

// Regression test for the removal of the per-party Signatures block from the
// Review-workstation OVERVIEW tab.
//
// static/js/overview/overview-tab.js is a classic browser script: it does NOT
// use a CommonJS `module.exports` guard — it only publishes
// `window.createOverviewController`. So we read the source and evaluate it in a
// context that provides a fake `window` + `document`, then drive the controller's
// render() against a tiny hand-rolled DOM stub (no jsdom; matches the repo's
// zero-dep FE harness style).
//
// What this asserts (the no-break contract):
//   1. After render(), the Overview summary body mounts the OTHER sections —
//      facts, the Fill tool, the clause roster, and the footer.
//   2. The signatures block (.ov-block-signatures) is NOT mounted anywhere.
//   3. window.renderOverviewSignatures is NEVER invoked, even when it is present
//      on window (proving the compose step was actually removed, not just made
//      conditional).
//
// Run: node tests/frontend/overview-no-signatures.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SRC = path.join(
  __dirname,
  "..",
  "..",
  "static",
  "js",
  "overview",
  "overview-tab.js",
);

// --- minimal fake DOM -------------------------------------------------------
// Enough surface for createOverviewController.render(): createElement, append,
// className, innerHTML (clears children), querySelector over class selectors,
// and a recursive descendant scan so we can find mounted blocks by class.

class FakeEl {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this._classes = new Set();
    this._textContent = "";
    this._innerHTML = "";
  }
  set className(v) {
    this._classes = new Set(
      String(v || "")
        .split(/\s+/)
        .filter(Boolean),
    );
  }
  get className() {
    return [...this._classes].join(" ");
  }
  hasClass(name) {
    return this._classes.has(name);
  }
  set innerHTML(html) {
    this._innerHTML = String(html);
    this.children = [];
    // Minimal HTML parse: materialize any `<tag class="...">` open tags as child
    // elements so querySelector(".cls") after an innerHTML write resolves, just
    // like a real browser parses the markup string into the child tree. This is
    // exactly what the controller relies on: it writes '<div class="ov-tab">'
    // then does container.querySelector(".ov-tab").
    const re = /<([a-zA-Z][\w-]*)\b([^>]*)>/g;
    let m;
    while ((m = re.exec(this._innerHTML)) !== null) {
      const tag = m[1];
      if (tag === "br" || tag === "img" || tag === "input") continue;
      const child = new FakeEl(tag);
      const clsMatch = /class="([^"]*)"/.exec(m[2]);
      if (clsMatch) child.className = clsMatch[1];
      child.parentNode = this;
      this.children.push(child);
    }
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set textContent(t) {
    this._textContent = String(t);
    this.children = [];
  }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._textContent;
  }
  append(...nodes) {
    for (const n of nodes) {
      if (n == null) continue;
      const node = typeof n === "string" ? Object.assign(new FakeEl("#text"), { _textContent: n }) : n;
      node.parentNode = this;
      this.children.push(node);
    }
  }
  appendChild(node) {
    this.append(node);
    return node;
  }
  _descendants() {
    const out = [];
    const walk = (node) => {
      for (const c of node.children) {
        out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  _matches(sel) {
    const s = sel.trim();
    if (s.startsWith(".")) return this.hasClass(s.slice(1));
    return this.tagName === s.toUpperCase();
  }
  querySelector(selector) {
    for (const part of selector.split(",")) {
      const token = part.trim().split(/\s+/).pop();
      for (const c of this._descendants()) {
        if (c._matches(token)) return c;
      }
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    for (const part of selector.split(",")) {
      const token = part.trim().split(/\s+/).pop();
      for (const c of this._descendants()) {
        if (c._matches(token) && !out.includes(c)) out.push(c);
      }
    }
    return out;
  }
}

function makeDocument(panelEl) {
  return {
    createElement: (tag) => new FakeEl(tag),
    querySelector: (sel) => (sel === "#studioDetailPanel" ? panelEl : null),
  };
}

// --- load the controller into a sandboxed window ----------------------------

function loadController(win) {
  const code = fs.readFileSync(SRC, "utf8");
  const sandbox = { window: win, document: win.document, module: undefined };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: SRC });
  assert.equal(
    typeof win.createOverviewController,
    "function",
    "overview-tab.js must publish window.createOverviewController",
  );
  return win.createOverviewController;
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- build a window with ALL render seams present, signatures included ------
// We deliberately register window.renderOverviewSignatures so that if the
// controller still called composeSignatures, it WOULD fire — and our spy would
// catch it. The other render* seams just mark their target element so we can
// assert the surviving sections mounted.

function buildWindow(panelEl) {
  const win = {};
  win.document = makeDocument(panelEl);
  win.escapeHtml = (v) => String(v == null ? "" : v);

  // Verdict / name / gate helpers used by the data-mapping layer.
  win.clauseStatus = () => ({ passes: true });
  win.clauseDisplayName = (c) => String((c && (c.name || c.id)) || "Clause");
  win.hasReviewResults = () => true;
  win.isMatterApproved = () => false;
  win.approveBlockReasons = () => [];

  const spy = { signatures: 0, facts: 0, roster: 0, footer: 0 };
  win.renderOverviewSignatures = (el) => {
    spy.signatures += 1;
    el.className = el.className + " rendered-signatures";
  };
  win.renderOverviewFacts = (el) => {
    spy.facts += 1;
    el.className = el.className + " rendered-facts";
  };
  win.renderOverviewRoster = (el) => {
    spy.roster += 1;
    el.className = el.className + " rendered-roster";
  };
  win.renderOverviewFooter = (el) => {
    spy.footer += 1;
    el.className = el.className + " rendered-footer";
  };
  win._spy = spy;
  return win;
}

// Fill tool seam: a persistent standalone element + a renderFill callback. The
// controller relocates it into the summary body, proving the Fill section still
// mounts.
function makeFillSection() {
  return new FakeEl("section");
}

const STATE = {
  selectedMatter: {
    id: "m1",
    counterparty: "Acme Robotics Ltd",
    counterparty_needs_confirmation: false,
    ai_review_ran: true,
    status: "in_review",
  },
  reviewClauses: [
    { id: "mutuality", name: "Mutuality", decision: "pass" },
    { id: "term", name: "Term", decision: "review" },
  ],
  selectedReviewClauseId: null,
};

// ===========================================================================
// Case 1 — the surviving sections still mount (facts, Fill, roster, footer).
// ===========================================================================
test("1. Overview render mounts facts, Fill, roster and footer (no break)", () => {
  const panel = new FakeEl("div");
  const win = buildWindow(panel);
  const create = loadController(win);

  const fillSection = makeFillSection();
  let fillRendered = 0;
  const controller = create({
    state: STATE,
    root: panel,
    fillSection,
    renderFill: () => {
      fillRendered += 1;
    },
  });
  controller.render();

  // Facts / roster / footer blocks were each handed to their render seam.
  assert.equal(win._spy.facts, 1, "facts section must render exactly once");
  assert.equal(win._spy.roster, 1, "clause roster must render exactly once");
  assert.equal(win._spy.footer, 1, "approve/send footer must render exactly once");

  // The Fill tool was relocated into the pane and repainted.
  assert.equal(fillRendered, 1, "the Fill tool (renderFill) must be invoked once");
  const fillWrapper = panel.querySelector(".ov-section-fill");
  assert.ok(fillWrapper, "the Aspora-entity Fill section must mount in the Overview pane");

  // The three surviving blocks exist in the rendered tree.
  assert.ok(panel.querySelector(".ov-block-facts"), "facts block must be present");
  assert.ok(panel.querySelector(".ov-block-roster"), "roster block must be present");
  assert.ok(panel.querySelector(".ov-block-footer"), "footer block must be present");
});

// ===========================================================================
// Case 2 — the signatures block is GONE from the Overview output.
// ===========================================================================
test("2. the per-party Signatures block is NOT mounted in the Overview", () => {
  const panel = new FakeEl("div");
  const win = buildWindow(panel);
  const create = loadController(win);

  const controller = create({
    state: STATE,
    root: panel,
    fillSection: makeFillSection(),
    renderFill: () => {},
  });
  controller.render();

  assert.equal(
    panel.querySelector(".ov-block-signatures"),
    null,
    "the .ov-block-signatures element must NOT be mounted in the Overview tab",
  );
});

// ===========================================================================
// Case 3 — renderOverviewSignatures is NEVER invoked (compose step removed).
// ===========================================================================
test("3. window.renderOverviewSignatures is never invoked by the Overview", () => {
  const panel = new FakeEl("div");
  const win = buildWindow(panel);
  const create = loadController(win);

  const controller = create({
    state: STATE,
    root: panel,
    fillSection: makeFillSection(),
    renderFill: () => {},
  });
  controller.render();

  assert.equal(
    win._spy.signatures,
    0,
    "renderOverviewSignatures was called — the signatures compose step is still wired",
  );
});

process.stdout.write(`\n${passed} passed\n`);
