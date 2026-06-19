"use strict";

// Behavior test for the TIER-1 reviewer notifications (in-app toast + persistent
// unread-count badge) that ride the existing board poll.
//
// static/js/notifications.js is a classic browser script publishing
// `createNotificationsController` on `window` (and a CommonJS guard). We vm-load it
// into a sandboxed window with a tiny hand-rolled DOM (the repo's zero-dep FE
// harness style; no jsdom), then drive two successive poll payloads through
// controller.observeTransitions() and assert the contract:
//
//   1. A transition INTO "needs review" pops ONE toast AND bumps the unread count.
//   2. A FYI transition (-> clean/ready) does NOT toast and does NOT bump.
//   3. Multiple attention transitions in one poll COLLAPSE into a SINGLE toast.
//   4. "Still needs review" across polls (token unchanged) is NOT a re-notify.
//   5. markSeen (open / bell) clears the unread count; persists to localStorage.
//
// Run: node tests/frontend/reviewer-notifications.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SRC = path.join(__dirname, "..", "..", "static", "js", "notifications.js");

// --- minimal fake DOM -------------------------------------------------------
// Just enough for the toast machinery: createElement, appendChild, classList,
// dataset, innerHTML (parses class tokens so querySelectorAll(".toast") resolves),
// querySelector/All, addEventListener, setAttribute, remove.

class FakeEl {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = {};
    this.hidden = false;
    this.title = "";
    this._textContent = "";
    this._innerHTML = "";
    this._classes = new Set();
    this._listeners = {};
    this.classList = {
      add: (c) => this._classes.add(c),
      remove: (c) => this._classes.delete(c),
      contains: (c) => this._classes.has(c),
      toggle: (c, on) => (on ? this._classes.add(c) : this._classes.delete(c)),
    };
  }
  set className(v) {
    this._classes = new Set(String(v || "").split(/\s+/).filter(Boolean));
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
    // Match an open tag and (optionally) the text up to its matching same-tag
    // close, so a child element captures the inline text the toast writes between
    // its tags (e.g. <span class="toast-title">3 matters need review</span>).
    const re = /<([a-zA-Z][\w-]*)\b([^>]*?)(\/?)>(?:([^<]*)<\/\1>)?/g;
    let m;
    while ((m = re.exec(this._innerHTML)) !== null) {
      const tag = m[1];
      const child = new FakeEl(tag);
      const clsMatch = /class="([^"]*)"/.exec(m[2]);
      if (clsMatch) child.className = clsMatch[1];
      const dataMatch = m[2].match(/data-([\w-]+)(?:="([^"]*)")?/g) || [];
      dataMatch.forEach((d) => {
        const dm = /data-([\w-]+)(?:="([^"]*)")?/.exec(d);
        if (dm) child.dataset[camel(dm[1])] = dm[2] == null ? "" : dm[2];
      });
      const innerText = m[4];
      if (innerText != null) child._textContent = innerText.trim();
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
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith("data-")) this.dataset[camel(name.slice(5))] = String(value);
  }
  getAttribute(name) {
    return this.attributes[name];
  }
  appendChild(node) {
    node.parentNode = this;
    this.children.push(node);
    return node;
  }
  append(...nodes) {
    nodes.forEach((n) => this.appendChild(n));
  }
  remove() {
    if (this.parentNode) {
      this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
      this.parentNode = null;
    }
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  dispatch(type, event = {}) {
    (this._listeners[type] || []).forEach((fn) => fn(event));
  }
  _descendants() {
    const out = [];
    const walk = (node) => node.children.forEach((c) => { out.push(c); walk(c); });
    walk(this);
    return out;
  }
  _matches(sel) {
    const s = sel.trim();
    if (s.startsWith("[") && s.endsWith("]")) {
      const attr = s.slice(1, -1);
      return camel(attr) in this.dataset || attr in this.attributes;
    }
    if (s.startsWith(".")) {
      // support compound ".a.b"
      return s.slice(1).split(".").every((c) => this.hasClass(c));
    }
    return this.tagName === s.toUpperCase();
  }
  querySelector(selector) {
    for (const part of selector.split(",")) {
      const token = part.trim().split(/\s+/).pop();
      for (const c of this._descendants()) if (c._matches(token)) return c;
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    for (const part of selector.split(",")) {
      const token = part.trim().split(/\s+/).pop();
      for (const c of this._descendants()) if (c._matches(token) && !out.includes(c)) out.push(c);
    }
    return out;
  }
}

function camel(name) {
  return String(name).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

// In-memory localStorage stub.
function makeLocalStorage() {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    _store: store,
  };
}

function makeWindow() {
  const win = {};
  win.document = {
    createElement: (tag) => new FakeEl(tag),
  };
  // Timers: capture but never auto-fire, so toasts persist for assertions.
  win.setTimeout = () => 0;
  win.clearTimeout = () => {};
  win.localStorage = makeLocalStorage();
  win.MatterUtils = null; // exercise the fallback field-reading path
  return win;
}

function loadFactory(win) {
  const code = fs.readFileSync(SRC, "utf8");
  const sandbox = {
    window: win,
    document: win.document,
    localStorage: win.localStorage,
    setTimeout: win.setTimeout,
    clearTimeout: win.clearTimeout,
    module: { exports: {} },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: SRC });
  assert.equal(
    typeof sandbox.createNotificationsController,
    "function",
    "notifications.js must publish createNotificationsController",
  );
  return sandbox.createNotificationsController;
}

function makeController() {
  const win = makeWindow();
  const create = loadFactory(win);
  const container = new FakeEl("div");
  const bell = new FakeEl("button");
  const bellCount = new FakeEl("span");
  bellCount.dataset.unreadCount = "0";
  bell.appendChild(bellCount);
  const opened = [];
  let repositoryOpened = 0;
  const controller = create({
    container,
    bellNode: bell,
    openMatter: (id) => opened.push(String(id)),
    openRepository: () => { repositoryOpened += 1; },
  });
  return { win, controller, container, bell, opened, getRepoOpened: () => repositoryOpened };
}

function attentionToasts(container) {
  return container.querySelectorAll(".toast--attention");
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

const needsReview = (id, extra = {}) => ({ id, requirements_needs_review: 1, ...extra });
const clean = (id, extra = {}) => ({ id, requirements_needs_review: 0, overall_status: "ready", ...extra });

// ===========================================================================
// 1 + 4 — attention transition toasts + bumps once; "still needs review" is quiet.
// ===========================================================================
test("attention transition toasts once and bumps; staying flagged does not re-notify", () => {
  const { controller, container } = makeController();

  // Poll 1: SEED silently (matter is clean). No toast, count 0.
  controller.observeTransitions([clean("m1")]);
  assert.equal(attentionToasts(container).length, 0, "seed poll must not toast");
  assert.equal(controller.unreadCount(), 0, "seed poll must not bump");

  // Poll 2: m1 flips clean -> needs review. ONE toast, count 1.
  controller.observeTransitions([needsReview("m1")]);
  assert.equal(attentionToasts(container).length, 1, "transition into needs-review must toast once");
  assert.equal(controller.unreadCount(), 1, "transition must bump the unread count to 1");

  // Poll 3: m1 STILL needs review (token unchanged). No new toast, count still 1.
  controller.observeTransitions([needsReview("m1")]);
  assert.equal(attentionToasts(container).length, 1, "'still needs review' must NOT re-toast");
  assert.equal(controller.unreadCount(), 1, "'still needs review' must NOT bump again");
});

// ===========================================================================
// 2 — FYI transition (-> clean/ready) is silent.
// ===========================================================================
test("FYI transition (-> clean/ready) does not toast or bump", () => {
  const { controller, container } = makeController();
  // Seed with a flagged matter, then clear it.
  controller.observeTransitions([needsReview("m1")]); // seed (silent)
  assert.equal(controller.unreadCount(), 0, "seed must be silent even for a flagged matter");

  controller.observeTransitions([clean("m1")]); // needs_review -> clean = FYI
  assert.equal(attentionToasts(container).length, 0, "FYI transition must not toast");
  assert.equal(controller.unreadCount(), 0, "FYI transition must not bump");
});

// ===========================================================================
// 3 — multiple attention transitions in one poll COLLAPSE into one toast.
// ===========================================================================
test("multiple attention transitions collapse into a single toast", () => {
  const { controller, container } = makeController();
  controller.observeTransitions([clean("m1"), clean("m2"), clean("m3")]); // seed

  // All three flip to needs-review in the SAME poll.
  controller.observeTransitions([needsReview("m1"), needsReview("m2"), needsReview("m3")]);
  const toasts = attentionToasts(container);
  assert.equal(toasts.length, 1, "three transitions in one poll must collapse to ONE toast");
  const title = toasts[0].querySelector(".toast-title");
  assert.ok(/3 matters need review/.test(title.textContent), `collapsed toast should read '3 matters need review', got: ${title.textContent}`);
  assert.equal(controller.unreadCount(), 3, "all three must each bump the count -> 3");
});

// ===========================================================================
// 5 — markSeen clears unread (per-matter and all), and persists.
// ===========================================================================
test("markSeen clears the unread count and persists to localStorage", () => {
  const { win, controller, bell } = makeController();
  controller.observeTransitions([clean("m1"), clean("m2")]); // seed
  controller.observeTransitions([needsReview("m1"), needsReview("m2")]);
  assert.equal(controller.unreadCount(), 2, "two flagged -> count 2");

  // Open m1: clears just m1.
  controller.markSeen("m1");
  assert.equal(controller.unreadCount(), 1, "marking m1 seen leaves count 1");

  // Clicking the bell clears all.
  bell.dispatch("click");
  assert.equal(controller.unreadCount(), 0, "bell click clears the whole count");

  // Persisted empty to localStorage.
  const stored = JSON.parse(win.localStorage.getItem("ndaReviewerUnread") || "[]");
  assert.deepEqual(stored, [], "cleared unread must persist as [] in localStorage");
});

// ===========================================================================
// 6 — review_failed and send_failed are also ATTENTION transitions.
// ===========================================================================
test("review_failed and send_failed transitions both toast and bump", () => {
  const { controller, container } = makeController();
  controller.observeTransitions([clean("rf"), clean("sf")]); // seed
  controller.observeTransitions([
    { id: "rf", review_status: "failed" },
    { id: "sf", send_status: "failed" },
  ]);
  assert.equal(attentionToasts(container).length, 1, "two failures in one poll collapse to one toast");
  assert.equal(controller.unreadCount(), 2, "both failures bump the count");
});

// ===========================================================================
// 7 — unread survives a page reload via localStorage seed.
// ===========================================================================
test("unread count survives a reload via localStorage", () => {
  const win = makeWindow();
  win.localStorage.setItem("ndaReviewerUnread", JSON.stringify(["m9"]));
  const create = loadFactory(win);
  const controller = create({
    container: new FakeEl("div"),
    bellNode: new FakeEl("button"),
    openMatter: () => {},
    openRepository: () => {},
  });
  assert.equal(controller.unreadCount(), 1, "stored unread id must seed the count on construction");
});

process.stdout.write(`\n${passed} passed\n`);
