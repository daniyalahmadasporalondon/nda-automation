"use strict";

// Frontend unit test for the review-failure notification.
//
// THE FEATURE: when a matter's async AI review transitions to
// review_status="failed" (the backend stamps it with a human-readable
// review_error reason), the existing top-right toast/notification system must pop
// an ERROR toast naming the matter + the reason -- so the user is alerted instead
// of left staring at "in review".
//
// This test asserts the contract:
//   1. A failed review the user is watching produces a toast carrying the matter
//      name AND the failure reason, both HTML-escaped.
//   2. It fires EXACTLY ONCE -- a repeated poll that keeps reporting the same
//      failed matter does NOT re-toast.
//   3. A failure ALREADY present on the very first observe() is SEEDED silently
//      (no toast on page load); only a transition DURING the session toasts.
//   4. Detection is not limited to gmail_inbound matters (a generated/manual NDA
//      can fail review too).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..", "..");
const staticDir = path.join(ROOT, "static");

// --- Minimal DOM stub -------------------------------------------------------
// notifications.js is a classic browser script. It only needs document.createElement
// (with innerHTML/className/dataset/setAttribute/querySelector*/addEventListener/
// appendChild/remove), a container node, and window timers. We stub just enough.

function makeElement() {
  const el = {
    className: "",
    innerHTML: "",
    dataset: {},
    _attrs: {},
    children: [],
    _listeners: {},
    setAttribute(name, value) {
      this._attrs[name] = value;
    },
    getAttribute(name) {
      return this._attrs[name];
    },
    classList: {
      _set: new Set(),
      _owner: null,
      add(c) {
        this._set.add(c);
      },
      contains(c) {
        // Classes can arrive two ways in our stub: via classList.add (e.g.
        // "toast--leaving") OR baked into the className string at creation
        // (e.g. "toast toast--info toast--persistent"). Honor BOTH so a persistent
        // toast set through className is still recognised by enforceStackCap.
        if (this._set.has(c)) return true;
        const cn = this._owner && typeof this._owner.className === "string" ? this._owner.className : "";
        return cn.split(/\s+/).includes(c);
      },
    },
    addEventListener(type, fn) {
      (this._listeners[type] || (this._listeners[type] = [])).push(fn);
    },
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    remove() {
      if (this.parentNode) {
        const idx = this.parentNode.children.indexOf(this);
        if (idx >= 0) this.parentNode.children.splice(idx, 1);
      }
    },
    // The toast wiring calls node.querySelector("[data-toast-close]") etc. Our
    // toast HTML is a string in innerHTML; for the test we only need these lookups
    // to return a clickable stub (or null), so resolve by the attribute substring.
    querySelector(sel) {
      if (this.innerHTML && this.innerHTML.includes(sel.replace(/[\[\]]/g, ""))) {
        return { addEventListener() {} };
      }
      return null;
    },
    querySelectorAll(sel) {
      // Used by enforceStackCap over the container. The REAL selector is
      // ".toast:not(.toast--leaving):not(.toast--persistent)" — honor BOTH exclusions
      // so persistent (progress-notice) toasts are immune from the cap exactly as in
      // the browser, otherwise this stub would let the cap evict them.
      const excludePersistent = typeof sel === "string" && sel.includes("toast--persistent");
      return container.children.filter((c) => {
        if (c.classList && c.classList.contains("toast--leaving")) return false;
        if (excludePersistent && c.classList && c.classList.contains("toast--persistent")) return false;
        return true;
      });
    },
  };
  // Back-reference so classList.contains can also read the className string.
  el.classList._owner = el;
  return el;
}

const container = makeElement();

function makeSandbox() {
  const sandbox = {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.console = console;
  // Timers: make setTimeout a no-op-capable stub so auto-dismiss never fires
  // during the synchronous test, and clearTimeout a no-op.
  sandbox.setTimeout = () => 0;
  sandbox.clearTimeout = () => {};
  sandbox.module = undefined; // suppress any CommonJS guard
  sandbox.document = {
    createElement: () => makeElement(),
  };
  vm.createContext(sandbox);
  return sandbox;
}

function loadNotifications(sandbox) {
  let code = fs.readFileSync(path.join(staticDir, "js", "notifications.js"), "utf8");
  code += "\n;globalThis.createNotificationsController = createNotificationsController;";
  vm.runInContext(code, sandbox, { filename: "js/notifications.js" });
  return sandbox.createNotificationsController;
}

// The toasts currently mounted in the container (error toasts only).
function errorToasts() {
  return container.children.filter(
    (c) => typeof c.className === "string" && c.className.includes("toast--error"),
  );
}

// The persistent INFO toasts currently mounted (the "Reviewing with AI…" progress notice).
function persistentToasts() {
  return container.children.filter(
    (c) => typeof c.className === "string" && c.className.includes("toast--persistent"),
  );
}

function run() {
  let passed = 0;

  // ---------------------------------------------------------------------------
  // 1 + 2: a failed review that ARRIVES mid-session toasts once with the matter
  //         name + escaped reason, and a repeated poll does NOT re-toast.
  // ---------------------------------------------------------------------------
  {
    container.children = [];
    const sandbox = makeSandbox();
    const create = loadNotifications(sandbox);
    const openedIds = [];
    const controller = create({
      container,
      openMatter: (id) => openedIds.push(id),
      openRepository: () => {},
      fetchMatters: undefined,
    });

    // First observe SEEDS silently: matter is still in_progress, no failures yet.
    controller.observe([
      { id: "m1", source_type: "gmail_inbound", counterparty: "Acme Corp", review_status: "in_progress" },
    ]);
    assert.equal(errorToasts().length, 0, "no error toast while review is in_progress");

    // The review transitions to failed with a reason -> one error toast.
    const failed = {
      id: "m1",
      source_type: "gmail_inbound",
      counterparty: "Acme & <Co>",
      review_error: 'Scanned PDF: no extractable text found "page 1".',
      review_status: "failed",
    };
    controller.observe([failed]);
    let toasts = errorToasts();
    assert.equal(toasts.length, 1, "a newly-failed review produces exactly one error toast");

    const html = toasts[0].innerHTML;
    assert.match(html, /Review failed/, "toast title says the review failed");
    // The matter name is HTML-escaped (Acme & <Co> -> Acme &amp; &lt;Co&gt;).
    assert.match(html, /Acme &amp; &lt;Co&gt;/, "matter name is escaped into the toast");
    assert.ok(!html.includes("<Co>"), "raw unescaped matter name must NOT appear");
    // The reason is escaped too (the double-quote -> &quot;).
    assert.match(html, /Scanned PDF: no extractable text found &quot;page 1&quot;\./,
      "the failure reason is escaped into the toast");
    assert.equal(toasts[0].getAttribute("role"), "alert", "failure toast is an assertive alert");

    // Repeated poll with the SAME failed matter -> still exactly one toast.
    controller.observe([failed]);
    controller.observe([failed]);
    assert.equal(errorToasts().length, 1, "a repeated poll must NOT re-fire the failure toast");
    passed += 1;
    console.log("ok 1 - failed review -> one escaped error toast, no re-fire on repeated polls");
  }

  // ---------------------------------------------------------------------------
  // 3: a failure ALREADY present on the very first observe() is seeded silently.
  // ---------------------------------------------------------------------------
  {
    container.children = [];
    const sandbox = makeSandbox();
    const create = loadNotifications(sandbox);
    const controller = create({
      container,
      openMatter: () => {},
      openRepository: () => {},
      fetchMatters: undefined,
    });

    controller.observe([
      { id: "old", source_type: "gmail_inbound", counterparty: "Stale Co", review_status: "failed", review_error: "old failure" },
    ]);
    assert.equal(errorToasts().length, 0, "a pre-existing failure on first load must NOT toast (seeded)");

    // A genuinely NEW failure during the session still toasts.
    controller.observe([
      { id: "old", source_type: "gmail_inbound", counterparty: "Stale Co", review_status: "failed", review_error: "old failure" },
      { id: "new", source_type: "gmail_inbound", counterparty: "Fresh Co", review_status: "failed", review_error: "fresh failure" },
    ]);
    const toasts = errorToasts();
    assert.equal(toasts.length, 1, "only the new failure toasts; the seeded one stays silent");
    assert.match(toasts[0].innerHTML, /Fresh Co/, "the new failure names the right matter");
    passed += 1;
    console.log("ok 2 - pre-existing failures seeded silently on load; new ones toast");
  }

  // ---------------------------------------------------------------------------
  // 4: a NON-gmail_inbound matter (generated/manual) that fails review still toasts.
  // ---------------------------------------------------------------------------
  {
    container.children = [];
    const sandbox = makeSandbox();
    const create = loadNotifications(sandbox);
    const controller = create({
      container,
      openMatter: () => {},
      openRepository: () => {},
      fetchMatters: undefined,
    });

    controller.observe([]); // seed empty
    controller.observe([
      { id: "g1", source_type: "generated", counterparty: "Generated Partner", review_status: "failed", review_error: "AI reviewer unavailable" },
    ]);
    const toasts = errorToasts();
    assert.equal(toasts.length, 1, "a generated matter's failed review still toasts");
    assert.match(toasts[0].innerHTML, /AI reviewer unavailable/, "the reason surfaces for non-inbound matters");
    passed += 1;
    console.log("ok 3 - non-inbound (generated) failed review toasts with reason");
  }

  // ---------------------------------------------------------------------------
  // 5: a "stalled" review (the read-time staleness override -- a pure timeout, NOT
  //    a durable failure) must NEVER fire a red failure toast. This is the core
  //    false-failure regression: a slow/interrupted review is not an error.
  // ---------------------------------------------------------------------------
  {
    container.children = [];
    const sandbox = makeSandbox();
    const create = loadNotifications(sandbox);
    const controller = create({
      container,
      openMatter: () => {},
      openRepository: () => {},
      fetchMatters: undefined,
    });

    controller.observe([]); // seed empty
    // The matter ages past the TTL -> the backend reports review_status="stalled"
    // (distinct from "failed"). A repeated poll must still never toast.
    const stalled = {
      id: "slow1",
      source_type: "gmail_inbound",
      counterparty: "Patient Co",
      review_status: "stalled",
      review_error: "The review is taking longer than expected or was interrupted. You can keep waiting or retry.",
    };
    controller.observe([stalled]);
    controller.observe([stalled]);
    assert.equal(errorToasts().length, 0, "a stalled (timeout) review must NOT fire a failure toast");

    // And a genuine failure on the SAME matter afterwards STILL toasts (the channel
    // is intact -- stalled does not poison the failed-seen set).
    controller.observe([
      { ...stalled, review_status: "failed", review_error: "Real error: AI reviewer unavailable." },
    ]);
    const toasts = errorToasts();
    assert.equal(toasts.length, 1, "a real failure after a stall still toasts exactly once");
    assert.match(toasts[0].innerHTML, /Real error/, "the genuine failure reason surfaces");
    passed += 1;
    console.log("ok 4 - stalled (timeout) review never toasts; a real failure after it still does");
  }

  // ---------------------------------------------------------------------------
  // 6: the PERSISTENT progress notification mechanism (notifyInProgress /
  //    dismissInProgress) used by the review workstation to move the verbose
  //    "Reviewing with AI…" sentence out of the inline toolbar.
  //    Contract:
  //      a. notifyInProgress(id, …) raises ONE persistent INFO toast (no auto-dismiss,
  //         immune to the stack cap).
  //      b. re-calling with the SAME id UPDATES in place — never a second toast.
  //      c. a flood of arrival toasts does NOT evict the persistent toast (cap-immune).
  //      d. dismissInProgress(id) clears it (marks it leaving), and is a safe no-op
  //         when nothing is live.
  //      e. the red failure toast (keyed on review_status="failed") is unaffected.
  // ---------------------------------------------------------------------------
  {
    container.children = [];
    const sandbox = makeSandbox();
    const create = loadNotifications(sandbox);
    const controller = create({
      container,
      openMatter: () => {},
      openRepository: () => {},
      fetchMatters: undefined,
    });

    // a. raise -> exactly one persistent INFO toast, role=status (calm), not an alert.
    controller.notifyInProgress("review-in-progress", "Reviewing with AI…", "This can take a couple of minutes.");
    let persist = persistentToasts();
    assert.equal(persist.length, 1, "notifyInProgress raised exactly one persistent toast");
    assert.ok(persist[0].className.includes("toast--info"), "the progress toast is the calm INFO variant");
    assert.equal(persist[0].getAttribute("role"), "status", "the progress toast is a polite status, not an alert");
    assert.match(persist[0].innerHTML, /Reviewing with AI/, "the progress toast carries the title");
    assert.equal(persist[0].dataset.toastPersistentId, "review-in-progress", "keyed by the supplied id");

    // b. re-raise SAME id -> still one toast (update in place, no duplicate).
    controller.notifyInProgress("review-in-progress", "Reviewing with AI…", "This document is taking a little longer than usual.");
    assert.equal(persistentToasts().length, 1, "re-raising the same id must UPDATE in place, not stack a duplicate");

    // c. a burst of inbound arrivals (which would normally cap the stack) must NOT
    //    evict the persistent progress toast.
    controller.observe([]); // seed empty so subsequent arrivals toast
    controller.observe([
      { id: "a1", source_type: "gmail_inbound", counterparty: "Co 1", created_at: "2026-06-22T10:00:01Z" },
      { id: "a2", source_type: "gmail_inbound", counterparty: "Co 2", created_at: "2026-06-22T10:00:02Z" },
    ]);
    controller.observe([
      { id: "a3", source_type: "gmail_inbound", counterparty: "Co 3", created_at: "2026-06-22T10:00:03Z" },
      { id: "a4", source_type: "gmail_inbound", counterparty: "Co 4", created_at: "2026-06-22T10:00:04Z" },
      { id: "a5", source_type: "gmail_inbound", counterparty: "Co 5", created_at: "2026-06-22T10:00:05Z" },
    ]);
    assert.equal(persistentToasts().length, 1, "a flood of arrival toasts evicted the persistent progress toast (cap must skip it)");

    // e. the red failure toast still fires and is independent of the progress notice.
    controller.observe([
      { id: "fX", source_type: "gmail_inbound", counterparty: "Bad Co", review_status: "failed", review_error: "AI reviewer unavailable" },
    ]);
    assert.equal(errorToasts().length, 1, "the red failure toast must still fire alongside the progress notice");
    assert.equal(persistentToasts().length, 1, "the failure toast must not disturb the persistent progress notice");

    // d. dismiss -> the persistent toast is torn down (marked leaving); double-dismiss
    //    and dismiss-of-unknown are safe no-ops.
    controller.dismissInProgress("review-in-progress");
    const live = persistentToasts().filter((c) => !c.className.includes("toast--leaving") && c.dataset.leaving !== "true");
    assert.equal(live.length, 0, "dismissInProgress did not tear down the persistent progress toast");
    controller.dismissInProgress("review-in-progress"); // no-op
    controller.dismissInProgress("never-raised"); // no-op
    passed += 1;
    console.log("ok 5 - persistent progress notice: one toast, updates in place, cap-immune, dismissable, failure toast intact");
  }

  console.log(`\n# ${passed} test group(s) passed`);
}

try {
  run();
  process.exit(0);
} catch (error) {
  console.error("\nFAIL:", error && error.stack ? error.stack : error);
  process.exit(1);
}
