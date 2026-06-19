"use strict";

// Frontend unit test for the fresh-user ONBOARDING empty-state on the contract
// repository board (RepositoryBoard.renderBoardOnboarding, also driven via
// renderBoard).
//
// A brand-new user with nothing connected and no matters used to see six
// "No documents" columns and a blank panel, which reads as broken. The board now
// renders a welcoming "Get started" panel into [data-repository-onboarding] when
// the board is genuinely empty -- and hides it the moment any matter exists, a
// search is active, or an error is being surfaced.
//
// repository-board.js is a classic browser script exposing a CommonJS export
// behind a `typeof module` guard. We require it with the same global stubs the
// page wires before it (RepositoryModel + MatterUtils), and drive the real render
// path against a tiny fake document so the shipped wiring is exercised as in prod.

const assert = require("node:assert/strict");
const path = require("node:path");

const { RepositoryModel } = require(
  path.join(__dirname, "..", "..", "static", "js", "repository-model.js"),
);

global.MatterUtils = {
  reviewStale: () => false,
  reviewStaleLabel: () => "",
  reviewInProgress: () => false,
};
global.RepositoryModel = RepositoryModel;

const { RepositoryBoard } = require(
  path.join(__dirname, "..", "..", "static", "js", "repository-board.js"),
);

// --- a tiny fake document --------------------------------------------------
// Models exactly the surface renderBoard touches: the per-column count spans,
// the per-column lists, the sync-status node, and the onboarding container. The
// onboarding node records its `hidden` flag and `innerHTML` so we can assert on
// what the board rendered into it.

function makeNode() {
  return {
    hidden: undefined,
    innerHTML: "",
    textContent: "",
    classList: { _set: new Set(), add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
}

function installDocument({ withBoard = true } = {}) {
  const onboarding = makeNode();
  const syncStatus = makeNode();
  const counts = {};
  const lists = {};
  const columnIds = ["generated", "manual_upload", "gmail_demo", "in_review", "reviewed", "sent"];
  global.document = {
    onboarding,
    querySelectorAll(selector) {
      if (selector === "[data-repository-count]") {
        return columnIds.map((id) => ({
          dataset: { repositoryCount: id },
          set textContent(value) { counts[id] = value; },
        }));
      }
      if (selector === "[data-repository-list]") {
        if (!withBoard) return [];
        return columnIds.map((id) => {
          const node = makeNode();
          node.dataset = { repositoryList: id };
          lists[id] = node;
          return node;
        });
      }
      return [];
    },
    querySelector(selector) {
      if (selector === "[data-repository-onboarding]") return onboarding;
      if (selector === "[data-repository-sync-status]") return syncStatus;
      return null;
    },
  };
  return { onboarding, counts, lists };
}

function renderWith(state, opts = {}) {
  const dom = installDocument();
  RepositoryBoard.renderBoard({
    gmailDemoMatterList: true,
    handlers: {},
    state,
    searchQuery: opts.searchQuery || "",
    errorMessage: opts.errorMessage || "",
  });
  return dom;
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- direct renderBoardOnboarding assertions --------------------------------

test("a fresh user (no matters, Gmail not connected) sees the onboarding panel", () => {
  const dom = installDocument();
  RepositoryBoard.renderBoardOnboarding({ state: { matters: [], gmailStatus: {} } });
  assert.equal(dom.onboarding.hidden, false, "panel is visible");
  assert.match(dom.onboarding.innerHTML, /repository-onboarding-card/);
  assert.match(dom.onboarding.innerHTML, /Generate your first NDA/i);
  // Gmail is NOT connected -> the Connect-Gmail step shows its CTA.
  assert.match(dom.onboarding.innerHTML, /Connect Gmail/i);
  assert.match(dom.onboarding.innerHTML, /data-onboarding-goto="generator"/);
  assert.match(dom.onboarding.innerHTML, /data-onboarding-goto="admin"/);
});

test("once Gmail is connected the panel marks that step done (no Connect CTA)", () => {
  const dom = installDocument();
  RepositoryBoard.renderBoardOnboarding({
    state: { matters: [], gmailStatus: { inbound: { ready: true } } },
  });
  assert.equal(dom.onboarding.hidden, false, "still shown for an empty board");
  assert.match(dom.onboarding.innerHTML, /Gmail is connected/i);
  assert.match(dom.onboarding.innerHTML, /is-done/);
  // No "Connect Gmail" action button remains once it's wired up.
  assert.doesNotMatch(dom.onboarding.innerHTML, /data-onboarding-goto="admin"/);
  // The Generator CTA is always offered.
  assert.match(dom.onboarding.innerHTML, /data-onboarding-goto="generator"/);
});

// --- the negative cases: the panel must NOT appear --------------------------

test("the onboarding panel is HIDDEN once at least one matter exists", () => {
  const dom = installDocument();
  RepositoryBoard.renderBoardOnboarding({
    state: { matters: [{ id: "m1", board_column: "generated" }], gmailStatus: {} },
  });
  assert.equal(dom.onboarding.hidden, true, "hidden when there is data");
  assert.equal(dom.onboarding.innerHTML, "", "no markup rendered");
});

test("the onboarding panel is HIDDEN while a search is active (filtered-to-nothing is a different state)", () => {
  const dom = installDocument();
  RepositoryBoard.renderBoardOnboarding({ state: { matters: [], gmailStatus: {} }, searchActive: true });
  assert.equal(dom.onboarding.hidden, true);
});

test("the onboarding panel is HIDDEN when an error message is being surfaced", () => {
  const dom = installDocument();
  RepositoryBoard.renderBoardOnboarding({
    state: { matters: [], gmailStatus: {} },
    errorMessage: "Could not load matters",
  });
  assert.equal(dom.onboarding.hidden, true);
});

// --- wired through the full renderBoard path --------------------------------

test("renderBoard shows the onboarding panel for an empty board", () => {
  const dom = renderWith({ matters: [], gmailStatus: {} });
  assert.equal(dom.onboarding.hidden, false);
  assert.match(dom.onboarding.innerHTML, /repository-onboarding-card/);
});

test("renderBoard hides the onboarding panel when matters are present", () => {
  const dom = renderWith({
    matters: [{ id: "m1", source_type: "generated", board_column: "generated" }],
    gmailStatus: {},
  });
  assert.equal(dom.onboarding.hidden, true);
});

test("renderBoard hides the onboarding panel when the search box is in use", () => {
  const dom = renderWith({ matters: [], gmailStatus: {} }, { searchQuery: "acme" });
  assert.equal(dom.onboarding.hidden, true);
});

// --- escaping: nothing user-controlled is interpolated, but prove the panel
//     contains no unescaped script even when state is hostile-shaped ---------

test("onboarding markup carries no raw <script> regardless of state shape", () => {
  const dom = installDocument();
  RepositoryBoard.renderBoardOnboarding({
    state: { matters: [], gmailStatus: { inbound: { ready: "<script>alert(1)</script>" } } },
  });
  assert.doesNotMatch(dom.onboarding.innerHTML, /<script>alert/);
});

process.stdout.write(`\nrepository-onboarding.cjs: ${passed} passed\n`);

delete global.document;
delete global.MatterUtils;
delete global.RepositoryModel;
