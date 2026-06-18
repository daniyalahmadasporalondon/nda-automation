"use strict";

// Frontend unit test for the failure-notification toasts (notifications.js).
//
// Asserts the toast de-dup contract the controller adds for the failure feed:
//   1. A NEW active failure event -> exactly ONE toast (via notify()).
//   2. The SAME active event on the next poll -> NO repeat toast.
//   3. A resolved/dismissed event never toasts.
//   4. The first observation seeds the seen-set SILENTLY (no flood on load).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..", "..");
const staticDir = path.join(ROOT, "static");

function loadClassicScript(relPath, sandbox, captureGlobal) {
  let code = fs.readFileSync(path.join(staticDir, relPath), "utf8");
  if (captureGlobal) code += `\n;globalThis.${captureGlobal} = ${captureGlobal};`;
  vm.runInContext(code, sandbox, { filename: relPath });
}

// A minimal fake DOM: just enough for notify()/mountToast() to build + append a
// toast node so we can COUNT the toasts that actually surface (the real DOM is
// absent in this headless harness). querySelector returns a no-op element so the
// close/open button wiring inside mountToast is harmless.
function makeFakeDom() {
  const container = { children: [] };
  const noopEl = { addEventListener() {} };
  container.appendChild = (node) => container.children.push(node);
  container.querySelectorAll = () => [];
  function createElement() {
    return {
      className: "",
      dataset: {},
      _html: "",
      set innerHTML(value) { this._html = value; },
      get innerHTML() { return this._html; },
      setAttribute() {},
      addEventListener() {},
      querySelector() { return noopEl; },
      remove() {},
      classList: { add() {} },
    };
  }
  return { container, document: { createElement } };
}

function makeSandbox() {
  const fake = makeFakeDom();
  const sandbox = {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.console = console;
  sandbox.setTimeout = () => 0; // don't schedule auto-dismiss in tests
  sandbox.clearTimeout = () => {};
  sandbox.setInterval = () => 0; // never auto-fire the 60s timer in tests
  sandbox.document = fake.document;
  sandbox.module = undefined;
  vm.createContext(sandbox);
  sandbox.__container = fake.container;
  return sandbox;
}

// Build a controller wired to the fake container; `notified` counts surfaced
// toasts by tracking nodes appended to the container.
function makeController(sandbox) {
  loadClassicScript("js/notifications.js", sandbox, "createNotificationsController");
  const container = sandbox.__container;
  const controller = sandbox.createNotificationsController({
    container,
    openMatter: () => {},
    openRepository: () => {},
    fetchMatters: async () => [],
  });
  const notified = container.children; // each appended node is one surfaced toast
  return { controller, notified };
}

function run() {
  let passed = 0;

  // 1 + 2 + 4: seed silently, new active -> one toast, repeat -> none.
  {
    const sandbox = makeSandbox();
    const { controller, notified } = makeController(sandbox);

    // First observation SEEDS silently (golden rule): a pre-existing active event
    // must NOT toast on load.
    controller.observeFailures([
      { id: "e1", status: "active", title: "Pre-existing", detail: "old", severity: "error" },
    ]);
    assert.equal(notified.length, 0, "first observation must seed silently (no flood on load)");

    // A genuinely NEW active event during the session -> exactly one toast.
    controller.observeFailures([
      { id: "e1", status: "active", title: "Pre-existing", detail: "old", severity: "error" },
      { id: "e2", status: "active", title: "Drive archive failed", detail: "boom", severity: "error" },
    ]);
    assert.equal(notified.length, 1, "a new active event must toast exactly once");
    assert.match(notified[0].innerHTML, /Drive archive failed/, "toast carries the event title");
    assert.match(notified[0].innerHTML, /boom/, "toast carries the event detail as subtitle");

    // Same event on the next poll -> NO repeat toast.
    controller.observeFailures([
      { id: "e1", status: "active", title: "Pre-existing", detail: "old", severity: "error" },
      { id: "e2", status: "active", title: "Drive archive failed", detail: "boom", severity: "error" },
    ]);
    assert.equal(notified.length, 1, "the same active event must not re-toast on a later poll");

    passed += 1;
    console.log("ok 1 - failure feed: seed silent, new active -> one toast, repeat -> none");
  }

  // 3: resolved / dismissed events never toast.
  {
    const sandbox = makeSandbox();
    const { controller, notified } = makeController(sandbox);
    controller.observeFailures([]); // seed empty
    controller.observeFailures([
      { id: "r1", status: "resolved", title: "Resolved one", detail: "x", severity: "error" },
      { id: "d1", status: "dismissed", title: "Dismissed one", detail: "y", severity: "warning" },
    ]);
    assert.equal(notified.length, 0, "resolved/dismissed events must never toast");
    passed += 1;
    console.log("ok 2 - failure feed: resolved/dismissed events never toast");
  }

  // pollFailures wiring: an injected fetchNotifications feeds observeFailures.
  {
    const sandbox = makeSandbox();
    loadClassicScript("js/notifications.js", sandbox, "createNotificationsController");
    const container = sandbox.__container;
    const notified = container.children;
    let batch = [];
    const controller = sandbox.createNotificationsController({
      container,
      fetchMatters: async () => [],
      fetchNotifications: async () => batch,
    });

    return (async () => {
      await controller.pollFailures(); // seed empty
      batch = [{ id: "n1", status: "active", title: "New failure", detail: "d", severity: "error" }];
      await controller.pollFailures(); // new -> one toast
      await controller.pollFailures(); // same -> no repeat
      assert.equal(notified.length, 1, "pollFailures toasts a new active event exactly once");
      passed += 1;
      console.log("ok 3 - pollFailures: injected fetch feeds observeFailures, de-dups");
      console.log(`\n# ${passed} test group(s) passed`);
    })();
  }
}

Promise.resolve()
  .then(run)
  .then(
    () => process.exit(0),
    (error) => {
      console.error("\nFAIL:", error && error.stack ? error.stack : error);
      process.exit(1);
    },
  );
