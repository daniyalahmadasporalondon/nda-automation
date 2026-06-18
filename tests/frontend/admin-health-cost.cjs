"use strict";

// Frontend unit test for the AI-spend (USD) cost panel in admin-health.js.
//
// admin-health.js is a classic browser IIFE that also exposes a CommonJS export
// behind a `typeof module` guard (a no-op in the page). We require it here and
// drive the real controller's payload renderer against a minimal fake DOM, so the
// shipped cost-panel wiring (USD formatting, per-feature rows, token secondary,
// honest cumulative-since-restart caveat) is exercised exactly as in the browser.
//
// Coverage (the panel's behavioural contract):
//   * the headline total renders the server `total_usd` as a "$" figure;
//   * each feature row renders feature name + "$" amount + token count;
//   * the panel reflects server feature ORDER (server sorts by spend);
//   * a sub-dollar total keeps sub-cent precision (not rounded to $0.00);
//   * the caveat carries the honest "since last restart / not today" note;
//   * the empty case shows a "No AI spend recorded" line.

const assert = require("node:assert/strict");
const path = require("node:path");

// --- Minimal fake DOM -------------------------------------------------------

class FakeClassList {
  toggle() {}
  add() {}
  remove() {}
  contains() {
    return false;
  }
}

class FakeElement {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.className = "";
    this.dataset = {};
    this.classList = new FakeClassList();
    this.children = [];
    this.textContent = "";
    this._listeners = {};
  }
  addEventListener(type, handler) {
    (this._listeners[type] || (this._listeners[type] = [])).push(handler);
  }
  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
  append(...nodes) {
    for (const node of nodes) this.appendChild(node);
  }
  replaceChildren(...nodes) {
    this.children = [];
    for (const node of nodes) this.appendChild(node);
  }
  querySelector() {
    return null;
  }
}

const fakeDocument = {
  createElement(tag) {
    return new FakeElement(tag);
  },
};

global.document = fakeDocument;
// The controller reads window.AuthExpired only inside load(); render() does not.
global.window = global.window || {};

const { createAdminHealthController } = require(
  path.join(__dirname, "..", "..", "static", "js", "admin-health.js")
);

// --- Harness ---------------------------------------------------------------

function mountController() {
  const refs = {
    state: {},
    healthCard: new FakeElement("article"),
    healthFacts: new FakeElement("dl"),
    healthStatus: new FakeElement("span"),
    healthAlerts: new FakeElement("ul"),
    healthCaveat: new FakeElement("p"),
    healthRaw: new FakeElement("pre"),
    healthRefreshButton: new FakeElement("button"),
    costTotal: new FakeElement("span"),
    costTokens: new FakeElement("span"),
    costFeatures: new FakeElement("div"),
    costCaveat: new FakeElement("p"),
    reviewErrorFromPayload: () => "",
  };
  const controller = createAdminHealthController(refs);
  return { controller, refs };
}

// --- Tests -----------------------------------------------------------------

function testRendersUsdTotalAndPerFeatureBreakdown() {
  const { controller, refs } = mountController();
  controller.render({
    telemetry: { counters: {}, started_at: "2026-06-19T00:00:00Z", uptime_seconds: 10 },
    health: {},
    ai_cost: {
      currency: "USD",
      total_usd: 0.15,
      total_tokens: 210,
      features: [
        { feature: "review", cost_usd: 0.12, cost_micro_units: 120000, total_tokens: 150 },
        { feature: "generation", cost_usd: 0.03, cost_micro_units: 30000, total_tokens: 60 },
      ],
      note: "Spend is cumulative since process start (since last restart). ... not a per-day \"today\" number.",
    },
  });

  assert.equal(refs.costTotal.textContent, "$0.15", "headline total in USD");
  assert.equal(refs.costTokens.textContent, "210 tokens", "total token secondary");

  const rows = refs.costFeatures.children;
  assert.equal(rows.length, 2, "one row per feature");
  // Row order mirrors the server-supplied (spend-sorted) order.
  assert.equal(rows[0].dataset.feature, "review");
  assert.equal(rows[1].dataset.feature, "generation");

  // Each row: humanised name span, "$" amount span, token span.
  const [name0, amount0, tokens0] = rows[0].children;
  assert.equal(name0.textContent, "Review");
  assert.equal(amount0.textContent, "$0.12");
  assert.equal(tokens0.textContent, "150 tokens");

  // Honest cumulative-since-restart caveat (no fabricated "today").
  assert.match(refs.costCaveat.textContent, /restart/i);
  assert.match(refs.costCaveat.textContent, /today/i);
}

function testSubDollarTotalKeepsSubCentPrecision() {
  const { controller, refs } = mountController();
  controller.render({
    telemetry: { counters: {} },
    health: {},
    ai_cost: {
      currency: "USD",
      total_usd: 0.0009,
      total_tokens: 10,
      features: [{ feature: "triage", cost_usd: 0.0009, cost_micro_units: 900, total_tokens: 10 }],
      note: "since last restart; not windowed; not today",
    },
  });
  // Must NOT round a real sub-cent spend down to $0.00.
  assert.equal(refs.costTotal.textContent, "$0.0009");
  assert.equal(refs.costFeatures.children[0].children[1].textContent, "$0.0009");
}

function testEmptySpendShowsPlaceholder() {
  const { controller, refs } = mountController();
  controller.render({
    telemetry: { counters: {} },
    health: {},
    ai_cost: { currency: "USD", total_usd: 0, total_tokens: 0, features: [], note: "n/a" },
  });
  assert.equal(refs.costTotal.textContent, "$0.00");
  assert.equal(refs.costFeatures.children.length, 1, "single placeholder node");
  assert.match(refs.costFeatures.children[0].textContent, /No AI spend recorded/i);
}

function testMissingCostBlockDoesNotThrow() {
  const { controller, refs } = mountController();
  // A payload with no ai_cost must render an empty/zeroed panel, never throw.
  controller.render({ telemetry: { counters: {} }, health: {} });
  assert.equal(refs.costTotal.textContent, "$0.00");
  assert.equal(refs.costTokens.textContent, "0 tokens");
}

testRendersUsdTotalAndPerFeatureBreakdown();
testSubDollarTotalKeepsSubCentPrecision();
testEmptySpendShowsPlaceholder();
testMissingCostBlockDoesNotThrow();

console.log("admin-health-cost.cjs: all assertions passed");
