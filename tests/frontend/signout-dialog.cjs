"use strict";

// Frontend unit test for the sign-out CHOICE dialog.
//
// Contract under test (static/js/auth-session.js + the #signOutModal markup):
//   1. Clicking the profile-dropdown "Sign out" button no longer calls
//      /api/auth/logout immediately -- it OPENS the confirmation dialog.
//   2. "This device only"  -> POST /api/auth/logout   then reload on success.
//   3. "All devices"       -> POST /api/auth/logout-all then reload on success.
//   4. "Cancel" / close / Escape close the dialog with NO network call.
//   5. Opening the dialog moves focus into it (the primary choice button).
//
// Runs the SHIPPED controller verbatim against a jsdom DOM that carries the
// real #signOutModal markup, so the wiring is exercised end to end.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");
const staticDir = path.join(ROOT, "static");

// Minimal DOM: the profile-dropdown sign-out button + the sign-out modal, using
// the exact ids/data-attributes the controller and app.js wiring rely on.
const HTML = `<!doctype html><html><body>
  <section id="sessionStrip">
    <button data-session-account-toggle aria-expanded="false"></button>
    <div data-session-account-menu role="menu" hidden>
      <button data-session-logout type="button">Sign out</button>
    </div>
  </section>
  <div class="send-modal" id="signOutModal" role="dialog" aria-modal="true"
       aria-labelledby="signOutModalTitle" hidden>
    <div class="confirm-modal-dialog">
      <button id="signOutModalClose" type="button"></button>
      <p id="signOutModalBody">How do you want to sign out?</p>
      <button id="signOutThisDeviceButton" type="button"><span>This device only</span></button>
      <button id="signOutAllDevicesButton" type="button"><span>All devices</span></button>
      <p id="signOutModalStatus"></p>
      <button id="signOutCancelButton" type="button">Cancel</button>
    </div>
  </div>
</body></html>`;

function reviewErrorFromPayload(payload, fallbackMessage) {
  return new Error((payload && payload.error) || fallbackMessage);
}

function loadController(sandbox) {
  const code = fs.readFileSync(path.join(staticDir, "js", "auth-session.js"), "utf8")
    + "\n;globalThis.createAuthSessionController = createAuthSessionController;";
  vm.runInContext(code, sandbox, { filename: "js/auth-session.js" });
}

function makeSandbox(dom, fetchImpl, reloadSpy) {
  const win = dom.window;
  // jsdom forbids redefining window.location (and proxying its non-configurable
  // reload), so hand the controller a plain substitute location object with a
  // reload() we can observe. It mirrors the few fields the controller reads.
  const locationStub = {
    href: win.location.href,
    origin: win.location.origin,
    pathname: win.location.pathname,
    search: win.location.search,
    hash: win.location.hash,
    reload: reloadSpy || (() => {}),
  };
  const windowProxy = new Proxy(win, {
    get(target, prop) {
      if (prop === "location") return locationStub;
      const value = Reflect.get(target, prop);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const sandbox = {
    window: windowProxy,
    document: win.document,
    HTMLElement: win.HTMLElement,
    setTimeout: (fn) => { fn(); return 0; }, // run the deferred focus synchronously
    clearTimeout: () => {},
    console,
    URL: win.URL,
    // The controller reads RepositoryApi + fetch off the global scope.
    RepositoryApi: { create: () => ({ loadGmailStatus: async () => ({}) }) },
    fetch: fetchImpl,
    module: undefined,
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  return sandbox;
}

function q(dom, sel) {
  return dom.window.document.querySelector(sel);
}

function buildController(dom, fetchImpl, reloadSpy) {
  const sandbox = makeSandbox(dom, fetchImpl, reloadSpy);
  loadController(sandbox);
  const d = dom.window.document;
  sandbox.createAuthSessionController({
    state: {},
    root: d.querySelector("#sessionStrip"),
    accountToggle: d.querySelector("[data-session-account-toggle]"),
    accountMenu: d.querySelector("[data-session-account-menu]"),
    logoutButton: d.querySelector("[data-session-logout]"),
    signOutModal: d.querySelector("#signOutModal"),
    signOutModalClose: d.querySelector("#signOutModalClose"),
    signOutModalStatus: d.querySelector("#signOutModalStatus"),
    signOutThisDeviceButton: d.querySelector("#signOutThisDeviceButton"),
    signOutAllDevicesButton: d.querySelector("#signOutAllDevicesButton"),
    signOutCancelButton: d.querySelector("#signOutCancelButton"),
    reviewErrorFromPayload,
  });
  return sandbox;
}

async function run() {
  let passed = 0;

  // 1. Clicking "Sign out" opens the dialog and does NOT hit the network.
  {
    const dom = new JSDOM(HTML, { url: "https://app.test/" });
    const calls = [];
    buildController(dom, async (url) => { calls.push(url); return { ok: true, json: async () => ({}) }; });

    q(dom, "[data-session-logout]").click();
    await new Promise((r) => setImmediate(r)); // let the deferred focus run

    assert.equal(q(dom, "#signOutModal").hidden, false, "dialog should open on Sign out click");
    assert.deepEqual(calls, [], "no logout request should fire just from opening the dialog");
    assert.equal(
      dom.window.document.activeElement,
      q(dom, "#signOutThisDeviceButton"),
      "focus should move to the primary choice",
    );
    console.log("ok 1 - Sign out click opens dialog, no network call, focus trapped in");
    passed += 1;
  }

  // 2. "This device only" -> POST /api/auth/logout, then reload.
  {
    const dom = new JSDOM(HTML, { url: "https://app.test/" });
    const calls = [];
    let reloaded = false;
    buildController(dom, async (url, opts) => {
      calls.push([url, opts && opts.method]);
      return { ok: true, json: async () => ({ authenticated: false }) };
    }, () => { reloaded = true; });

    q(dom, "[data-session-logout]").click();
    q(dom, "#signOutThisDeviceButton").click();
    await new Promise((r) => setImmediate(r));

    assert.deepEqual(calls, [["/api/auth/logout", "POST"]], "should POST to /api/auth/logout");
    assert.equal(reloaded, true, "should reload on success (same as original logout)");
    console.log("ok 2 - This device only -> POST /api/auth/logout + reload");
    passed += 1;
  }

  // 3. "All devices" -> POST /api/auth/logout-all, then reload.
  {
    const dom = new JSDOM(HTML, { url: "https://app.test/" });
    const calls = [];
    let reloaded = false;
    buildController(dom, async (url, opts) => {
      calls.push([url, opts && opts.method]);
      return { ok: true, json: async () => ({ authenticated: false }) };
    }, () => { reloaded = true; });

    q(dom, "[data-session-logout]").click();
    q(dom, "#signOutAllDevicesButton").click();
    await new Promise((r) => setImmediate(r));

    assert.deepEqual(calls, [["/api/auth/logout-all", "POST"]], "should POST to /api/auth/logout-all");
    assert.equal(reloaded, true, "should reload on success");
    console.log("ok 3 - All devices -> POST /api/auth/logout-all + reload");
    passed += 1;
  }

  // 4. Cancel closes the dialog with no network call.
  {
    const dom = new JSDOM(HTML, { url: "https://app.test/" });
    const calls = [];
    buildController(dom, async (url) => { calls.push(url); return { ok: true, json: async () => ({}) }; });

    q(dom, "[data-session-logout]").click();
    assert.equal(q(dom, "#signOutModal").hidden, false, "dialog open before cancel");
    q(dom, "#signOutCancelButton").click();

    assert.equal(q(dom, "#signOutModal").hidden, true, "Cancel should close the dialog");
    assert.deepEqual(calls, [], "Cancel must not call any logout endpoint");
    console.log("ok 4 - Cancel closes dialog, no network call");
    passed += 1;
  }

  // 5. Escape closes the dialog (no network call).
  {
    const dom = new JSDOM(HTML, { url: "https://app.test/" });
    const calls = [];
    buildController(dom, async (url) => { calls.push(url); return { ok: true, json: async () => ({}) }; });

    q(dom, "[data-session-logout]").click();
    const esc = new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true });
    dom.window.document.dispatchEvent(esc);

    assert.equal(q(dom, "#signOutModal").hidden, true, "Escape should close the dialog");
    assert.deepEqual(calls, [], "Escape must not call any logout endpoint");
    console.log("ok 5 - Escape closes dialog, no network call");
    passed += 1;
  }

  console.log(`\n# ${passed} test group(s) passed`);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
