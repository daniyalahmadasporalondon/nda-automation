"use strict";

// Frontend unit test for the self-serve "My signature" personalisation form
// (admin-personalisation.js).
//
// The controller is a browser IIFE that also exposes a CommonJS export behind a
// `typeof module` guard. We require it here and drive the REAL controller
// against a minimal fake DOM + a stubbed fetch, exactly as it runs in the page.
//
// The behavioural contract under test (the bug this fixes: a non-admin used to
// hit the admin endpoint, get 403, and see a dead "Administrator access is
// required" Save button):
//   * a NON-admin's form loads via GET /api/me/personalisation-settings (NOT the
//     admin endpoint) and populates from the caller's resolved `personalisation`;
//   * Save POSTs to /api/me/personalisation-settings and persists the caller's
//     OWN signature -- no 403 dead-end;
//   * the admin-only GLOBAL-default controller (adminOnly + /api/admin/...) does
//     NOT show an "Administrator access is required" message to a non-admin: on a
//     403 it self-hides via onUnavailable instead.

const assert = require("node:assert/strict");
const path = require("node:path");

// --- Minimal fake DOM -------------------------------------------------------
class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  toggle(name, force) {
    const on = force === undefined ? !this._set.has(name) : Boolean(force);
    if (on) this._set.add(name);
    else this._set.delete(name);
    return on;
  }
  contains(name) {
    return this._set.has(name);
  }
}

class FakeElement {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.attributes = {};
    this.classList = new FakeClassList();
    this.dataset = {};
    this.value = "";
    this.placeholder = "";
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.innerHTML = "";
    this._listeners = {};
  }
  addEventListener(type, handler) {
    (this._listeners[type] || (this._listeners[type] = [])).push(handler);
  }
  async dispatchEvent(event) {
    const handlers = this._listeners[event.type] || [];
    for (const handler of handlers) {
      // eslint-disable-next-line no-await-in-loop
      await handler.call(this, event);
    }
    return true;
  }
  async submit() {
    await this.dispatchEvent({ type: "submit", preventDefault() {} });
  }
}

// --- Stub fetch -------------------------------------------------------------
function installFetch(responder) {
  const calls = [];
  global.fetch = async (url, options = {}) => {
    let body;
    try {
      body = options.body ? JSON.parse(options.body) : undefined;
    } catch {
      body = options.body;
    }
    calls.push({ url, method: options.method || "GET", body });
    const result = responder(url, options.method || "GET", body) || {};
    const ok = result.ok !== false;
    return {
      ok,
      status: result.status || (ok ? 200 : 400),
      statusText: result.statusText || (ok ? "OK" : "Bad Request"),
      async json() {
        return result.payload || {};
      },
    };
  };
  return calls;
}

async function flush(turns = 20) {
  for (let i = 0; i < turns; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

const { createAdminPersonalisationController, AdminPersonalisationView } = require(
  path.join(__dirname, "..", "..", "static", "js", "admin-personalisation.js")
);

function mount(extra = {}) {
  const elements = {
    card: new FakeElement("article"),
    form: new FakeElement("form"),
    signOffInput: new FakeElement("input"),
    signatureInput: new FakeElement("input"),
    signatureBlockInput: new FakeElement("textarea"),
    shadowNote: new FakeElement("p"),
    saveButton: new FakeElement("button"),
    resetButton: new FakeElement("button"),
    overall: new FakeElement("span"),
    message: new FakeElement("p"),
    persistenceFact: new FakeElement("dd"),
  };
  const reviewErrorFromPayload = (payload, fallback) =>
    new Error((payload && payload.error) || fallback);
  const controller = createAdminPersonalisationController({
    ...elements,
    reviewErrorFromPayload,
    ...extra,
  });
  return { controller, ...elements };
}

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

(async () => {
  await test("SELF_ENDPOINT is the per-user route, not the admin route", () => {
    assert.equal(AdminPersonalisationView.SELF_ENDPOINT, "/api/me/personalisation-settings");
  });

  await test("non-admin form loads from GET /api/me/... (never the admin endpoint)", async () => {
    const ui = mount(); // no `endpoint` => self-serve default
    const calls = installFetch((url) => {
      if (url === "/api/me/personalisation-settings") {
        return {
          payload: {
            personalisation: { sign_off: "Cheers,", signature: "Dana Doe", signature_block: "Cheers,\nDana Doe" },
            is_custom: true,
            global_default: { sign_off: "Best,", signature: "Aspora Legal", signature_block: "Best,\nAspora Legal" },
          },
        };
      }
      // Any hit to the admin endpoint is a contract violation for the self form.
      return { ok: false, status: 403, payload: { error: "Administrator access is required." } };
    });

    await ui.controller.load();
    await flush();

    assert.ok(
      calls.some((c) => c.url === "/api/me/personalisation-settings" && c.method === "GET"),
      "must GET the per-user endpoint",
    );
    assert.ok(
      !calls.some((c) => c.url === "/api/admin/personalisation-settings"),
      "self-serve form must NOT touch the admin endpoint",
    );
    assert.equal(ui.signOffInput.value, "Cheers,");
    assert.equal(ui.signatureInput.value, "Dana Doe");
    assert.equal(ui.signatureBlockInput.value, "Cheers,\nDana Doe");
    assert.equal(ui.overall.textContent, "Ready");
    // Non-admin is not admin-blocked: the always-editable Signature Block field
    // is enabled. (Sign-Off/Signature are shadowed here only because this
    // fixture has a non-empty Signature Block — see the shadow tests below.)
    assert.ok(!ui.signatureBlockInput.disabled, "fields editable for a non-admin");
    assert.doesNotMatch(ui.message.textContent, /Administrator access is required/i);
  });

  await test("Save POSTs the caller's OWN signature to /api/me/... and persists", async () => {
    const ui = mount();
    const calls = installFetch((url, method) => {
      if (url === "/api/me/personalisation-settings" && method === "GET") {
        return {
          payload: {
            personalisation: { sign_off: "Best,", signature: "Aspora Legal", signature_block: "Best,\nAspora Legal" },
            is_custom: false,
            global_default: { sign_off: "Best,", signature: "Aspora Legal", signature_block: "Best,\nAspora Legal" },
          },
        };
      }
      if (url === "/api/me/personalisation-settings" && method === "POST") {
        return {
          payload: {
            personalisation: { sign_off: "Warmly,", signature: "Sam Signer", signature_block: "Warmly,\nSam Signer" },
            is_custom: true,
          },
        };
      }
      return { ok: false, status: 403, payload: { error: "Administrator access is required." } };
    });

    await ui.controller.load();
    await flush();

    // The user types a new signature, then saves.
    ui.signOffInput.value = "Warmly,";
    ui.signatureInput.value = "Sam Signer";
    ui.signatureBlockInput.value = "Warmly,\nSam Signer";
    await ui.signOffInput.dispatchEvent({ type: "input" });
    await ui.form.submit();
    await flush();

    const post = calls.find((c) => c.method === "POST");
    assert.ok(post, "a POST must be issued");
    assert.equal(post.url, "/api/me/personalisation-settings", "POST goes to the per-user endpoint");
    assert.deepEqual(post.body, {
      sign_off: "Warmly,",
      signature: "Sam Signer",
      signature_block: "Warmly,\nSam Signer",
    });
    assert.equal(ui.overall.textContent, "Saved");
    // Persisted: the form now reflects the saved values (reload would re-read them).
    assert.equal(ui.signatureInput.value, "Sam Signer");
    assert.ok(!calls.some((c) => c.url === "/api/admin/personalisation-settings"));
  });

  await test("admin-only global panel self-hides on 403 (no 'Administrator access is required' dead-end)", async () => {
    let hidden = false;
    const ui = mount({
      endpoint: "/api/admin/personalisation-settings",
      adminOnly: true,
      onUnavailable: () => {
        hidden = true;
      },
    });
    installFetch((url) => {
      if (url === "/api/admin/personalisation-settings") {
        return { ok: false, status: 403, payload: { error: "Administrator access is required." } };
      }
      return {};
    });

    await ui.controller.load();
    await flush();

    assert.equal(hidden, true, "the admin-only panel hands off to onUnavailable to hide itself");
    // It must NOT have rendered the admin-only nag into the visible message.
    assert.doesNotMatch(ui.message.textContent, /Administrator access is required/i);
    assert.notEqual(ui.overall.textContent, "Unavailable");
  });

  await test("non-empty Signature Block shadows Sign-Off + Signature on load and shows the note", async () => {
    const ui = mount();
    installFetch((url) => {
      if (url === "/api/me/personalisation-settings") {
        return {
          payload: {
            personalisation: { sign_off: "Cheers,", signature: "Dana Doe", signature_block: "Cheers,\nDana Doe" },
            is_custom: true,
          },
        };
      }
      return { ok: false, status: 403, payload: {} };
    });

    await ui.controller.load();
    await flush();

    // Signature Block has content => the other two are shadowed and the note shows.
    assert.equal(ui.signatureBlockInput.disabled, false, "Signature Block itself stays editable");
    assert.equal(ui.signOffInput.disabled, true, "Sign-Off shadowed while Signature Block is set");
    assert.equal(ui.signatureInput.disabled, true, "Signature shadowed while Signature Block is set");
    assert.equal(ui.shadowNote.hidden, false, "the inline note is visible");
    // Values are preserved (never wiped) even though the fields are disabled.
    assert.equal(ui.signOffInput.value, "Cheers,");
    assert.equal(ui.signatureInput.value, "Dana Doe");
  });

  await test("clearing Signature Block live re-enables Sign-Off + Signature and hides the note", async () => {
    const ui = mount();
    installFetch((url) => {
      if (url === "/api/me/personalisation-settings") {
        return {
          payload: {
            personalisation: { sign_off: "Cheers,", signature: "Dana Doe", signature_block: "Cheers,\nDana Doe" },
            is_custom: true,
          },
        };
      }
      return { ok: false, status: 403, payload: {} };
    });

    await ui.controller.load();
    await flush();
    assert.equal(ui.signOffInput.disabled, true, "starts shadowed");
    assert.equal(ui.shadowNote.hidden, false);

    // User empties the Signature Block -> live re-enable.
    ui.signatureBlockInput.value = "";
    await ui.signatureBlockInput.dispatchEvent({ type: "input" });
    assert.equal(ui.signOffInput.disabled, false, "Sign-Off re-enabled");
    assert.equal(ui.signatureInput.disabled, false, "Signature re-enabled");
    assert.equal(ui.shadowNote.hidden, true, "note hidden");

    // Re-typing content shadows them again (change event path).
    ui.signatureBlockInput.value = "Regards,\nDana";
    await ui.signatureBlockInput.dispatchEvent({ type: "change" });
    assert.equal(ui.signOffInput.disabled, true, "shadowed again on new content");
    assert.equal(ui.shadowNote.hidden, false);
  });

  await test("empty Signature Block leaves Sign-Off + Signature editable (no shadow, no note)", async () => {
    const ui = mount();
    installFetch((url) => {
      if (url === "/api/me/personalisation-settings") {
        return {
          payload: {
            personalisation: { sign_off: "Cheers,", signature: "Dana Doe", signature_block: "" },
            is_custom: true,
          },
        };
      }
      return { ok: false, status: 403, payload: {} };
    });

    await ui.controller.load();
    await flush();

    assert.equal(ui.signOffInput.disabled, false, "editable when Signature Block is empty");
    assert.equal(ui.signatureInput.disabled, false, "editable when Signature Block is empty");
    assert.equal(ui.shadowNote.hidden, true, "no note when Signature Block is empty");
  });

  process.stdout.write(`\npersonalisation-self-serve.cjs: ${passed} passed\n`);
})().catch((error) => {
  process.stderr.write(`\npersonalisation-self-serve.cjs FAILED: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
