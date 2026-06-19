"use strict";

// Frontend unit test for the first-run ONBOARDING empty-states.
//
// A fresh user with nothing connected and no matters previously saw blank,
// ambiguous "looks broken" screens (six identical "No documents" columns on the
// repository board; a bare "No NDAs on file yet." line in the corpus). These
// tests assert the welcoming "get started" onboarding renders when the board /
// corpus is empty, and does NOT render once the user has data.
//
// Both modules are classic browser scripts (IIFEs assigned to globals) that
// also expose CommonJS exports behind a `typeof module` guard (a no-op in the
// page). We require them here, providing a minimal fake DOM the way the shipped
// page would, and assert on the rendered markup.

const assert = require("node:assert/strict");
const path = require("node:path");

// --- Minimal fake DOM -------------------------------------------------------
// Enough of the element surface the onboarding renderers touch: innerHTML,
// hidden, dataset, classList, addEventListener, and querySelector(All) over a
// small registry keyed by selector. Event wiring is exercised but events are
// not dispatched (the renderers only register listeners).

class FakeElement {
  constructor() {
    this.innerHTML = "";
    this.hidden = false;
    this.textContent = "";
    this.dataset = {};
    this._children = [];
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  addEventListener() {}
  // After innerHTML is set, the renderers querySelectorAll the action buttons to
  // wire clicks. Return any registered children so the wiring path is exercised.
  querySelectorAll() {
    return this._children;
  }
  querySelector() {
    return null;
  }
}

function makeDocument(registry) {
  return {
    querySelector(selector) {
      return registry[selector] || null;
    },
    querySelectorAll() {
      return [];
    },
  };
}

global.window = { escapeHtml: undefined };

const { RepositoryBoard } = require(path.join(
  __dirname,
  "..",
  "..",
  "static",
  "js",
  "repository-board.js"
));
const { CorpusView, CorpusRender } = require(path.join(
  __dirname,
  "..",
  "..",
  "static",
  "js",
  "corpus.js"
));

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

const pending = [];
function asyncTest(name, fn) {
  pending.push(
    Promise.resolve()
      .then(fn)
      .then(() => {
        passed += 1;
        process.stdout.write(`  ok ${name}\n`);
      })
  );
}

// ---------------------------------------------------------------------------
// Repository board onboarding
// ---------------------------------------------------------------------------

function boardNode() {
  const node = new FakeElement();
  global.document = makeDocument({ "[data-repository-onboarding]": node });
  return node;
}

test("board onboarding RENDERS when empty + not searching + Gmail not connected", () => {
  const node = boardNode();
  RepositoryBoard.renderOnboarding({
    hasMatters: false,
    isSearching: false,
    state: { gmailStatus: { inbound: { connected: false } } },
  });
  assert.equal(node.hidden, false, "onboarding panel must be visible");
  assert.match(node.innerHTML, /Get started/);
  assert.match(node.innerHTML, /Generate an NDA/);
  assert.match(node.innerHTML, /data-onboarding-tab="generator"/);
  // Gmail not connected -> the connect prompt is present.
  assert.match(node.innerHTML, /Connect Gmail to import inbound NDAs/);
  assert.match(node.innerHTML, /data-onboarding-tab="admin"/);
});

test("board onboarding HIDES once the user has matters", () => {
  const node = boardNode();
  RepositoryBoard.renderOnboarding({
    hasMatters: true,
    isSearching: false,
    state: { gmailStatus: { inbound: { connected: false } } },
  });
  assert.equal(node.hidden, true, "onboarding must hide when matters exist");
  assert.equal(node.innerHTML, "", "onboarding markup must be cleared when hidden");
});

test("board onboarding HIDES while the user is searching an empty result", () => {
  const node = boardNode();
  RepositoryBoard.renderOnboarding({
    hasMatters: false,
    isSearching: true,
    state: { gmailStatus: null },
  });
  assert.equal(node.hidden, true, "onboarding must not hijack an empty search result");
});

test("board onboarding DROPS the Gmail prompt once Gmail is connected + ready", () => {
  const node = boardNode();
  RepositoryBoard.renderOnboarding({
    hasMatters: false,
    isSearching: false,
    state: { gmailStatus: { inbound: { connected: true, ready: true, enabled: true } } },
  });
  assert.equal(node.hidden, false);
  // Still invites generation, but no Gmail connect/setup row.
  assert.match(node.innerHTML, /Generate an NDA/);
  assert.doesNotMatch(node.innerHTML, /Connect Gmail/);
  assert.doesNotMatch(node.innerHTML, /Finish Gmail setup/);
});

test("board onboarding shows a FINISH-SETUP prompt when Gmail is connected but not ready", () => {
  const node = boardNode();
  RepositoryBoard.renderOnboarding({
    hasMatters: false,
    isSearching: false,
    state: { gmailStatus: { inbound: { connected: true, ready: false, error: "token expired" } } },
  });
  assert.match(node.innerHTML, /Finish Gmail setup/);
  // The error string is interpolated AND escaped (no raw angle brackets leak).
  assert.match(node.innerHTML, /token expired/);
});

test("board onboarding ESCAPES interpolated Gmail error text", () => {
  const node = boardNode();
  RepositoryBoard.renderOnboarding({
    hasMatters: false,
    isSearching: false,
    state: { gmailStatus: { inbound: { connected: true, ready: false, error: "<img src=x onerror=alert(1)>" } } },
  });
  assert.doesNotMatch(node.innerHTML, /<img src=x/, "raw HTML must not reach the DOM");
  assert.match(node.innerHTML, /&lt;img src=x/, "the value must be HTML-escaped");
});

test("board onboarding is a no-op when the container is absent (no throw)", () => {
  global.document = makeDocument({});
  assert.doesNotThrow(() =>
    RepositoryBoard.renderOnboarding({ hasMatters: false, isSearching: false, state: {} })
  );
});

// ---------------------------------------------------------------------------
// Corpus onboarding
// ---------------------------------------------------------------------------
//
// The corpus controller renders the empty-state into emptyNode when the payload
// has no groups. We drive the real controller with stub nodes and feed it an
// empty payload, then assert the onboarding markup landed in the empty node.

function stubNode() {
  const node = new FakeElement();
  return node;
}

function corpusControllerWithNodes() {
  const nodes = {
    panel: stubNode(),
    listNode: stubNode(),
    emptyNode: stubNode(),
    noResultsNode: stubNode(),
    statusNode: stubNode(),
    summaryNode: stubNode(),
  };
  global.document = makeDocument({});
  const controller = CorpusView.createController({
    panel: nodes.panel,
    listNode: nodes.listNode,
    emptyNode: nodes.emptyNode,
    noResultsNode: nodes.noResultsNode,
    statusNode: nodes.statusNode,
    summaryNode: nodes.summaryNode,
    refreshButton: null,
    searchForm: null,
    searchInput: null,
    tokenField: null,
    searchClear: null,
    facetRail: null,
    groupToggle: null,
    executedToggle: null,
    openMatter() {},
  });
  return { controller, nodes };
}

// Drive the real controller through its public load() path with a stubbed fetch
// returning the given payload — exactly how the page fetches /api/corpus.
function loadCorpus(payload) {
  const { controller, nodes } = corpusControllerWithNodes();
  global.fetch = () =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
  return controller.load().then(() => nodes);
}

asyncTest("corpus onboarding RENDERS when there are no groups (empty corpus)", () =>
  loadCorpus({ groups: [], drive: { connected: false } }).then((nodes) => {
    assert.equal(nodes.emptyNode.hidden, false, "empty node must be visible");
    assert.match(nodes.emptyNode.innerHTML, /Your corpus is empty/);
    assert.match(nodes.emptyNode.innerHTML, /Generate an NDA/);
    assert.match(nodes.emptyNode.innerHTML, /data-corpus-onboarding-tab="generator"/);
    assert.match(nodes.emptyNode.innerHTML, /Connect Gmail to import inbound NDAs/);
    // Drive not connected -> the Drive connect row is present.
    assert.match(nodes.emptyNode.innerHTML, /Connect your Google Drive/);
  })
);

asyncTest("corpus onboarding DROPS the Drive row once Drive is connected", () =>
  loadCorpus({ groups: [], drive: { connected: true } }).then((nodes) => {
    assert.match(nodes.emptyNode.innerHTML, /Your corpus is empty/);
    assert.doesNotMatch(nodes.emptyNode.innerHTML, /Connect your Google Drive/);
  })
);

asyncTest("corpus onboarding does NOT render once the corpus has groups (data present)", () =>
  loadCorpus({
    groups: [{ key: "acme", matters: [{ id: "m1", facets: { signed: true } }] }],
    drive: { connected: true },
  }).then((nodes) => {
    assert.equal(nodes.emptyNode.hidden, true, "empty/onboarding must hide when groups exist");
    assert.doesNotMatch(nodes.emptyNode.innerHTML || "", /Your corpus is empty/);
  })
);

test("CorpusRender.escape escapes hostile markup (onboarding copy is escaped)", () => {
  assert.equal(CorpusRender.escape("<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;");
});

Promise.all(pending).then(() => {
  process.stdout.write(`\nonboarding: ${passed} passed\n`);
});
