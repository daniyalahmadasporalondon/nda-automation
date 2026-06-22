"use strict";

// Regression test: the file-local `escapeHtml` helpers in the classic (non-module)
// overview/dashboard scripts must NOT infinitely recurse into themselves.
//
// THE BUG this guards against ("Maximum call stack size exceeded", crashed the
// Playbook tab): static/js/overview/facts.js (and its siblings) is a CLASSIC
// <script>. Its top-level `function escapeHtml(value)` declaration auto-binds to
// `window.escapeHtml`. The helper used to delegate unconditionally:
//
//     return typeof window.escapeHtml === "function" ? window.escapeHtml(value) : <manual>
//
// In the pre-bridge state (before static/js/modules/global-bridge.mjs — a DEFERRED
// module — overwrites window.escapeHtml with the canonical html-utils escaper),
// window.escapeHtml IS this very function. So `window.escapeHtml(value)` called
// ITSELF → infinite recursion → RangeError. The fix adds a `window.escapeHtml !==
// escapeHtml` guard so the helper can only ever delegate to a *different* function.
//
// This test reproduces the exact pre-bridge condition: it evaluates each source
// file in a VM context where the top-level `function escapeHtml` declaration
// becomes the context's global `escapeHtml` (== window.escapeHtml), then calls
// window.escapeHtml(...) and asserts it returns the correctly-escaped string and
// does NOT throw RangeError. With the old (unguarded) code this test throws
// "Maximum call stack size exceeded"; with the fix it passes.
//
// Run: node tests/frontend/escapehtml-no-recursion.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "static", "js");

// The two classic scripts whose top-level (col-0) `function escapeHtml`
// auto-binds to window AND self-delegates to window.escapeHtml — the genuinely
// recursion-capable set. (roster.js / dashboard-search.js declare their escaper
// INSIDE an IIFE so it never attaches to window, and either don't delegate or
// delegate only to the bridged canonical — they cannot self-recurse and are
// intentionally left untouched.)
const CASES = [
  { name: "overview/facts.js", file: path.join(STATIC, "overview", "facts.js"), topLevel: true },
  { name: "overview/signatures.js", file: path.join(STATIC, "overview", "signatures.js"), topLevel: true },
];

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// Correct escaping the helpers must produce. The inline fallbacks use &#39; for
// the apostrophe (html-utils uses &#039; — both are browser-equivalent; we assert
// the apostrophe is escaped to one of those, and the rest byte-exact).
function assertEscapes(fn, label) {
  // Core characters, in one combined string (covers ordering / double-escape bugs).
  assert.equal(
    fn('<a href="x" title=\'y\'> & </a>'),
    "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt; &amp; &lt;/a&gt;",
    `${label}: combined escaping`,
  );
  // Each character individually.
  assert.equal(fn("<"), "&lt;", `${label}: <`);
  assert.equal(fn(">"), "&gt;", `${label}: >`);
  assert.equal(fn("&"), "&amp;", `${label}: &`);
  assert.equal(fn('"'), "&quot;", `${label}: double-quote`);
  assert.equal(fn("'"), "&#39;", `${label}: single-quote`);
  // null / undefined coerce to empty string, never "null"/"undefined", never throw.
  assert.equal(fn(null), "", `${label}: null → ""`);
  assert.equal(fn(undefined), "", `${label}: undefined → ""`);
  // Ampersand is escaped FIRST so it does not double-escape the entity output.
  assert.equal(fn("a&lt;b"), "a&amp;lt;b", `${label}: no double-escape`);
}

// Load a classic script into a fresh VM context. `window` is the context's own
// global object so a top-level `function escapeHtml` declaration becomes
// window.escapeHtml — exactly the browser's pre-bridge state. Returns window.
function loadIntoPreBridgeWindow(file) {
  const code = fs.readFileSync(file, "utf8");
  // The context object IS the global / window. Self-reference window=this so the
  // scripts' `typeof window !== "undefined"` branch is live and a top-level
  // function declaration lands on it.
  const sandbox = { module: { exports: {} }, document: undefined };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: file });
  return sandbox;
}

for (const c of CASES) {
  test(`${c.name}: escapes correctly and does not infinitely recurse (pre-bridge)`, () => {
    const win = loadIntoPreBridgeWindow(c.file);

    if (c.topLevel) {
      // Proof of the exact failure condition: the top-level declaration DID
      // auto-bind onto window (so the old code's window.escapeHtml(value) would
      // have called itself). The guard must make that call safe anyway.
      assert.equal(
        typeof win.escapeHtml,
        "function",
        `${c.name}: top-level escapeHtml must auto-bind to window (reproduces the bug condition)`,
      );
      // Calling window.escapeHtml — which IS the file-local function — must not
      // blow the stack. Under the old code this throws RangeError.
      assertEscapes(win.escapeHtml, `${c.name} (window.escapeHtml self)`);
    }

    // Belt-and-braces: explicitly install window.escapeHtml = the file-local
    // function (the simulated pre-bridge alias) and call THROUGH it. This is the
    // single most direct reproduction of "window.escapeHtml resolves to itself".
    // We dig the local helper out via the module export where possible; for the
    // top-level files the function already equals win.escapeHtml.
    const local = win.escapeHtml;
    if (typeof local === "function") {
      win.escapeHtml = local; // alias window.escapeHtml -> the helper itself
      assert.doesNotThrow(
        () => local("<script>&'\""),
        RangeError,
        `${c.name}: helper must not recurse when window.escapeHtml === itself`,
      );
      assertEscapes(local, `${c.name} (aliased self)`);
    }
  });
}

process.stdout.write(`\nescapehtml-no-recursion: ${passed} passed\n`);
