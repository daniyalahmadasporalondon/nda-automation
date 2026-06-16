"use strict";

// Frontend unit test for the Overview tab clause roster.
//
// static/js/overview/roster.js is a classic browser script that exposes its
// helpers behind a `typeof module !== "undefined"` CommonJS guard (a no-op in
// the browser, exactly like corpus.js). We require it here and exercise both the
// pure helpers (sort / normalize) and the render path against a tiny hand-rolled
// DOM stub — no jsdom dependency, matching the repo's zero-dep FE harness style.

const assert = require("node:assert/strict");
const path = require("node:path");

const { OverviewRoster, renderOverviewRoster } = require(
  path.join(__dirname, "..", "..", "static", "js", "overview", "roster.js"),
);

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- Minimal DOM stub --------------------------------------------------------
// Just enough surface for render(): innerHTML capture, event listeners, and a
// closest()/contains() pair so the click delegation resolves a row -> id. We
// model the rendered rows as lightweight nodes parsed out of the HTML string.

function makeContainer() {
  const listeners = {};
  const container = {
    innerHTML: "",
    _rows: [],
    set innerHTMLValue(v) {
      this.innerHTML = v;
    },
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    contains(node) {
      return this._rows.includes(node) || node === container;
    },
    dispatch(type, target) {
      (listeners[type] || []).forEach((fn) =>
        fn({ target, key: undefined, preventDefault() {} }),
      );
    },
    dispatchKey(type, target, key) {
      (listeners[type] || []).forEach((fn) =>
        fn({ target, key, preventDefault() {} }),
      );
    },
  };
  return container;
}

// Parse the data-clause-id + class list out of the rendered HTML into row nodes
// the click handler can walk via closest(".ov-row").
function hydrate(container) {
  const html = container.innerHTML;
  const rows = [];
  const re = /<div class="(ov-row[^"]*)"[^>]*data-clause-id="([^"]*)"/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const node = {
      _class: m[1],
      _id: m[2],
      getAttribute(attr) {
        return attr === "data-clause-id" ? this._id : null;
      },
      closest(sel) {
        return sel === ".ov-row" ? this : null;
      },
    };
    rows.push(node);
  }
  container._rows = rows;
  return rows;
}

const SAMPLE = [
  { id: "c1", name: "Confidentiality", verdict: "pass", reviewed: true },
  { id: "c2", name: "Term", verdict: "fail", reviewed: false },
  { id: "c3", name: "Governing Law", verdict: "review", reviewed: false },
  { id: "c4", name: "Non-Solicit", verdict: "pass", reviewed: false },
];

// --- Sorting: problems-first -------------------------------------------------
test("sortClauses orders fail, then review, then pass", () => {
  const order = OverviewRoster.sortClauses(SAMPLE).map((c) => c.id);
  assert.deepEqual(order, ["c2", "c3", "c1", "c4"]);
});

test("sortClauses is stable within a verdict bucket (c1 before c4)", () => {
  const order = OverviewRoster.sortClauses(SAMPLE).map((c) => c.id);
  // c1 and c4 are both pass; c1 came first in the input and must stay first.
  assert.ok(order.indexOf("c1") < order.indexOf("c4"));
});

test("sortClauses does not mutate the caller's array", () => {
  const input = SAMPLE.slice();
  const before = input.map((c) => c.id);
  OverviewRoster.sortClauses(input);
  assert.deepEqual(input.map((c) => c.id), before);
});

test("sortClauses tolerates empty / non-array input", () => {
  assert.deepEqual(OverviewRoster.sortClauses([]), []);
  assert.deepEqual(OverviewRoster.sortClauses(undefined), []);
  assert.deepEqual(OverviewRoster.sortClauses(null), []);
});

// --- Verdict normalization ---------------------------------------------------
test("normalizeVerdict passes through the three known verdicts", () => {
  assert.equal(OverviewRoster.normalizeVerdict("fail"), "fail");
  assert.equal(OverviewRoster.normalizeVerdict("review"), "review");
  assert.equal(OverviewRoster.normalizeVerdict("pass"), "pass");
});

test("normalizeVerdict treats unknown/missing as review (never silently pass)", () => {
  assert.equal(OverviewRoster.normalizeVerdict(""), "review");
  assert.equal(OverviewRoster.normalizeVerdict(undefined), "review");
  assert.equal(OverviewRoster.normalizeVerdict("PASSED"), "review");
  assert.equal(OverviewRoster.normalizeVerdict("PASS"), "pass"); // case-insensitive
});

// --- Row markup --------------------------------------------------------------
test("rowHtml emits the verdict pill modifier class", () => {
  assert.ok(OverviewRoster.rowHtml({ id: "x", name: "X", verdict: "fail" }).includes("ov-pill--fail"));
  assert.ok(OverviewRoster.rowHtml({ id: "x", name: "X", verdict: "review" }).includes("ov-pill--review"));
  assert.ok(OverviewRoster.rowHtml({ id: "x", name: "X", verdict: "pass" }).includes("ov-pill--pass"));
});

test("rowHtml renders ov-check only when reviewed is true", () => {
  assert.ok(OverviewRoster.rowHtml({ id: "x", name: "X", verdict: "pass", reviewed: true }).includes("ov-check"));
  assert.ok(!OverviewRoster.rowHtml({ id: "x", name: "X", verdict: "pass", reviewed: false }).includes("ov-check"));
  assert.ok(!OverviewRoster.rowHtml({ id: "x", name: "X", verdict: "pass" }).includes("ov-check"));
});

test("rowHtml adds ov-row--current only for the current row", () => {
  assert.ok(OverviewRoster.rowHtml({ id: "x", name: "X" }, { current: true }).includes("ov-row--current"));
  assert.ok(!OverviewRoster.rowHtml({ id: "x", name: "X" }, { current: false }).includes("ov-row--current"));
});

test("rowHtml escapes a malicious clause name", () => {
  const html = OverviewRoster.rowHtml({ id: "x", name: '<img src=x onerror=alert(1)>', verdict: "pass" });
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img"));
});

// --- render(): wrapper, ordering, current marker, click ----------------------
test("render wraps rows in .ov-roster and orders problems-first", () => {
  const container = makeContainer();
  renderOverviewRoster(container, { clauses: SAMPLE, currentClauseId: "c1" }, {});
  assert.ok(container.innerHTML.startsWith('<div class="ov-roster">'));
  const ids = [...container.innerHTML.matchAll(/data-clause-id="([^"]*)"/g)].map((m) => m[1]);
  assert.deepEqual(ids, ["c2", "c3", "c1", "c4"]);
});

test("render marks the current clause row", () => {
  const container = makeContainer();
  renderOverviewRoster(container, { clauses: SAMPLE, currentClauseId: "c3" }, {});
  // The c3 row should carry ov-row--current; assert via the hydrated nodes.
  const rows = hydrate(container);
  const currentRows = rows.filter((r) => r._class.includes("ov-row--current"));
  assert.equal(currentRows.length, 1);
  assert.equal(currentRows[0]._id, "c3");
});

test("render fires onClauseClick with the clicked clause id", () => {
  const container = makeContainer();
  const clicked = [];
  renderOverviewRoster(
    container,
    { clauses: SAMPLE, currentClauseId: null },
    { onClauseClick: (id) => clicked.push(id) },
  );
  const rows = hydrate(container);
  const target = rows.find((r) => r._id === "c2");
  container.dispatch("click", target);
  assert.deepEqual(clicked, ["c2"]);
});

test("render fires onClauseClick on Enter / Space keydown", () => {
  const container = makeContainer();
  const clicked = [];
  renderOverviewRoster(
    container,
    { clauses: SAMPLE, currentClauseId: null },
    { onClauseClick: (id) => clicked.push(id) },
  );
  const rows = hydrate(container);
  const target = rows.find((r) => r._id === "c3");
  container.dispatchKey("keydown", target, "Enter");
  container.dispatchKey("keydown", target, " ");
  assert.deepEqual(clicked, ["c3", "c3"]);
});

test("render is a safe no-op without a container", () => {
  assert.doesNotThrow(() => renderOverviewRoster(null, { clauses: SAMPLE }, {}));
});

test("render tolerates a missing onClauseClick handler", () => {
  const container = makeContainer();
  assert.doesNotThrow(() => renderOverviewRoster(container, { clauses: SAMPLE }, {}));
});

process.stdout.write(`\n${passed} passed\n`);
