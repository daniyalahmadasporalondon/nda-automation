"use strict";

// Frontend unit test for the Gmail polling controls in admin-integrations.js.
//
// admin-integrations.js is a classic browser script (an IIFE assigned to a
// global) that also exposes a CommonJS export behind a `typeof module` guard --
// a no-op in the page. We require it here and drive the real controller through
// a minimal fake DOM + a stubbed fetch, so the shipped wiring (event listeners,
// renderers, request bodies) is exercised exactly as it runs in the browser.
//
// Coverage (the feature's behavioural contract):
//   * toggling the switch OFF posts {sync_enabled:false} -- it PAUSES polling
//     and never calls /api/gmail/disconnect;
//   * with polling paused the admin copy reads "Polling off" and the connection
//     stays put (no disconnect request);
//   * resuming posts {sync_enabled:true};
//   * the import-limit input saves (posts {import_limit}) and re-renders the
//     copy line from the refreshed status;
//   * the import limit is clamped to the backend cap (40) before it is posted.

const assert = require("node:assert/strict");
const path = require("node:path");

// --- Minimal fake DOM -------------------------------------------------------
// Just enough of the element surface the controller actually touches: events,
// attributes, classList, value/disabled/title/textContent, and querySelector
// over registered [data-admin-gmail] children.

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
    this.children = [];
    this.value = "";
    this.disabled = false;
    this.title = "";
    this.textContent = "";
    this.innerHTML = "";
    this.isConnected = true;
    this._listeners = {};
  }
  addEventListener(type, handler) {
    (this._listeners[type] || (this._listeners[type] = [])).push(handler);
  }
  removeEventListener(type, handler) {
    const list = this._listeners[type] || [];
    this._listeners[type] = list.filter((fn) => fn !== handler);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
  // Selector support is intentionally tiny: only the [data-admin-gmail="key"]
  // form the controller uses for its copy spans.
  querySelector(selector) {
    const match = /^\[data-admin-gmail="(.+)"\]$/.exec(selector);
    if (match) return this._findByAdminGmail(match[1]) || null;
    return null;
  }
  querySelectorAll() {
    return [];
  }
  _findByAdminGmail(key) {
    for (const child of this.collectDescendants()) {
      if (child.dataset && child.dataset.adminGmail === key) return child;
    }
    return null;
  }
  collectDescendants() {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  closest() {
    return null;
  }
  async dispatchEvent(event) {
    const handlers = this._listeners[event.type] || [];
    for (const handler of handlers) {
      // Support async listeners (updateGmailToggle etc. are async).
      // eslint-disable-next-line no-await-in-loop
      await handler.call(this, event);
    }
    return true;
  }
  // Convenience: fire a click and await any async listener chain.
  async click() {
    await this.dispatchEvent({ type: "click", preventDefault() {} });
  }
  async submit() {
    await this.dispatchEvent({ type: "submit", preventDefault() {} });
  }
}

function copySpan(key) {
  const node = new FakeElement("span");
  node.dataset.adminGmail = key;
  return node;
}

// --- Test scaffolding -------------------------------------------------------

const { createAdminIntegrationsController } = require(
  path.join(__dirname, "..", "..", "static", "js", "admin-integrations.js")
);

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// The controller's event handlers are fire-and-forget (they kick off an async
// save without returning the promise, exactly as in the page). flush() yields
// to the macrotask queue enough times to drain the whole POST -> load() ->
// render cascade (which spans several await boundaries), so assertions observe
// the settled UI rather than a mid-flight state.
async function flush(turns = 20) {
  for (let i = 0; i < turns; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

// Stubs the global fetch with a scripted response per URL and records every
// call (url + parsed JSON body).
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
    const result = responder(url, body) || {};
    const ok = result.ok !== false;
    return {
      ok,
      status: result.status || (ok ? 200 : 400),
      statusText: result.statusText || (ok ? "OK" : "Bad Request"),
      async json() {
        if (result.nonJson) throw new SyntaxError("Unexpected token < in JSON");
        return result.payload || {};
      },
    };
  };
  return calls;
}

// Builds a controller wired to a fresh fake DOM. Returns the controller plus
// the elements the tests need to inspect or drive.
function mountController(initialStatus) {
  const gmailCard = new FakeElement("article");
  const enabledCopy = copySpan("enabled-copy");
  const importLimitCopy = copySpan("import-limit-copy");
  const syncWindowCopy = copySpan("sync-window-copy");
  const searchTermsCopy = copySpan("search-terms-copy");
  const inboundConfigured = copySpan("inbound-configured");
  const outboundConfigured = copySpan("outbound-configured");
  const inboundEmail = copySpan("inbound-email");
  gmailCard.appendChild(enabledCopy);
  gmailCard.appendChild(importLimitCopy);
  gmailCard.appendChild(syncWindowCopy);
  gmailCard.appendChild(searchTermsCopy);
  gmailCard.appendChild(inboundConfigured);
  gmailCard.appendChild(outboundConfigured);
  gmailCard.appendChild(inboundEmail);

  const gmailToggle = new FakeElement("button");
  const gmailImportLimitInput = new FakeElement("input");
  const gmailImportLimitSaveButton = new FakeElement("button");
  const gmailImportLimitForm = new FakeElement("form");
  // The save button submits the form in the page; model that link so a button
  // click drives the same submit handler.
  gmailImportLimitSaveButton.addEventListener("click", async () => {
    await gmailImportLimitForm.submit();
  });
  const gmailSyncWindowInput = new FakeElement("input");
  const gmailSyncWindowSaveButton = new FakeElement("button");
  const gmailSyncWindowForm = new FakeElement("form");
  gmailSyncWindowSaveButton.addEventListener("click", async () => {
    await gmailSyncWindowForm.submit();
  });
  const gmailSearchTermsInput = new FakeElement("textarea");
  const gmailSearchSaveButton = new FakeElement("button");
  const gmailSearchForm = new FakeElement("form");
  gmailSearchSaveButton.addEventListener("click", async () => {
    await gmailSearchForm.submit();
  });
  const gmailOverall = new FakeElement("span");

  const state = { gmailStatus: {} };
  const reviewErrorFromPayload = (payload, fallback) =>
    new Error((payload && payload.error) || fallback);

  const controller = createAdminIntegrationsController({
    state,
    gmailCard,
    gmailFacts: gmailCard,
    gmailOverall,
    gmailToggle,
    gmailImportLimitForm,
    gmailImportLimitInput,
    gmailImportLimitSaveButton,
    gmailSyncWindowForm,
    gmailSyncWindowInput,
    gmailSyncWindowSaveButton,
    gmailSearchForm,
    gmailSearchTermsInput,
    gmailSearchSaveButton,
    reviewErrorFromPayload,
  });

  if (initialStatus) controller.renderGmailStatus(initialStatus);

  return {
    controller,
    state,
    gmailCard,
    gmailToggle,
    gmailImportLimitInput,
    gmailImportLimitSaveButton,
    gmailImportLimitForm,
    gmailSyncWindowInput,
    gmailSyncWindowSaveButton,
    gmailSyncWindowForm,
    gmailSearchTermsInput,
    gmailSearchSaveButton,
    gmailSearchForm,
    enabledCopy,
    importLimitCopy,
    syncWindowCopy,
    searchTermsCopy,
    inboundConfigured,
    outboundConfigured,
    inboundEmail,
  };
}

const ENV_CONNECTED = {
  // Env / shared-token mode, polling currently ON, both roles ready.
  user_scoped: false,
  inbound: { ready: true, enabled: true },
  outbound: { ready: true, enabled: true },
  settings: { sync_enabled: true, import_limit: 25 },
};

(async () => {
  await test("toggle OFF posts {sync_enabled:false} and never disconnects", async () => {
    const ui = mountController(ENV_CONNECTED);
    const calls = installFetch((url) => {
      if (url === "/api/gmail/status") {
        return {
          payload: {
            gmail: {
              ...ENV_CONNECTED,
              settings: { sync_enabled: false, import_limit: 25 },
            },
          },
        };
      }
      if (url === "/api/matters") return { payload: { matters: [] } };
      if (url === "/api/gmail/settings") {
        return {
          payload: {
            gmail: {
              ...ENV_CONNECTED,
              settings: { sync_enabled: false, import_limit: 25 },
            },
          },
        };
      }
      return {};
    });

    await ui.gmailToggle.click();
    await flush();

    const settingsCall = calls.find((c) => c.url === "/api/gmail/settings");
    assert.ok(settingsCall, "expected a POST to /api/gmail/settings");
    assert.equal(settingsCall.method, "POST");
    assert.deepEqual(settingsCall.body, { sync_enabled: false }, "must pause via sync_enabled:false");
    // The disconnect endpoint must NOT be touched by the toggle.
    assert.ok(
      !calls.some((c) => c.url === "/api/gmail/disconnect"),
      "toggle must not call /api/gmail/disconnect"
    );
  });

  await test('admin shows "Polling off" while Gmail stays connected', async () => {
    const ui = mountController(ENV_CONNECTED);
    installFetch((url) => {
      if (url === "/api/gmail/status") {
        return {
          payload: {
            gmail: {
              ...ENV_CONNECTED,
              settings: { sync_enabled: false, import_limit: 25 },
            },
          },
        };
      }
      if (url === "/api/matters") return { payload: { matters: [] } };
      if (url === "/api/gmail/settings") {
        return {
          payload: {
            gmail: {
              ...ENV_CONNECTED,
              settings: { sync_enabled: false, import_limit: 25 },
            },
          },
        };
      }
      return {};
    });

    await ui.gmailToggle.click();
    await flush();

    assert.equal(ui.enabledCopy.textContent, "Polling off", "copy reflects paused polling");
    assert.equal(ui.gmailToggle.getAttribute("aria-checked"), "false", "switch is off");
    assert.equal(ui.gmailToggle.getAttribute("aria-label"), "Resume Gmail polling");
    // Connection is intact: status still reports both roles ready.
    assert.equal(ui.state.gmailStatus.inbound.ready, true);
    assert.equal(ui.state.gmailStatus.outbound.ready, true);
  });

  await test('resuming posts {sync_enabled:true} and reads "Polling on"', async () => {
    const PAUSED = {
      ...ENV_CONNECTED,
      settings: { sync_enabled: false, import_limit: 25 },
    };
    const ui = mountController(PAUSED);
    assert.equal(ui.enabledCopy.textContent, "Polling off", "starts paused");

    const calls = installFetch((url) => {
      if (url === "/api/gmail/status") {
        return { payload: { gmail: { ...ENV_CONNECTED } } };
      }
      if (url === "/api/matters") return { payload: { matters: [] } };
      if (url === "/api/gmail/settings") return { payload: { gmail: { ...ENV_CONNECTED } } };
      return {};
    });

    await ui.gmailToggle.click();
    await flush();

    const settingsCall = calls.find((c) => c.url === "/api/gmail/settings");
    assert.deepEqual(settingsCall.body, { sync_enabled: true }, "resume via sync_enabled:true");
    assert.equal(ui.enabledCopy.textContent, "Polling on");
    assert.equal(ui.gmailToggle.getAttribute("aria-label"), "Pause Gmail polling");
  });

  await test("import-limit input saves {import_limit} and re-renders the copy", async () => {
    const ui = mountController(ENV_CONNECTED);
    assert.equal(ui.importLimitCopy.textContent, "25 messages per scheduled poll.");
    assert.equal(ui.gmailImportLimitInput.value, "25", "input seeded from status");

    ui.gmailImportLimitInput.value = "30";
    const refreshed = {
      ...ENV_CONNECTED,
      settings: { sync_enabled: true, import_limit: 30 },
    };
    const calls = installFetch((url) => {
      if (url === "/api/gmail/status") return { payload: { gmail: refreshed } };
      if (url === "/api/matters") return { payload: { matters: [] } };
      if (url === "/api/gmail/settings") return { payload: { gmail: refreshed } };
      return {};
    });

    await ui.gmailImportLimitSaveButton.click();
    await flush();

    const settingsCall = calls.find((c) => c.url === "/api/gmail/settings");
    assert.ok(settingsCall, "expected a POST to /api/gmail/settings");
    assert.deepEqual(settingsCall.body, { import_limit: 30 }, "posts the typed limit");
    assert.equal(ui.importLimitCopy.textContent, "30 messages per scheduled poll.", "copy re-rendered");
    assert.equal(ui.gmailImportLimitInput.value, "30", "input re-rendered from refreshed status");
  });

  await test("import limit above the cap is clamped to 40 before posting", async () => {
    const ui = mountController(ENV_CONNECTED);
    ui.gmailImportLimitInput.value = "100"; // beyond the backend clamp (40)
    const refreshed = {
      ...ENV_CONNECTED,
      settings: { sync_enabled: true, import_limit: 40 },
    };
    const calls = installFetch((url) => {
      if (url === "/api/gmail/status") return { payload: { gmail: refreshed } };
      if (url === "/api/matters") return { payload: { matters: [] } };
      if (url === "/api/gmail/settings") return { payload: { gmail: refreshed } };
      return {};
    });

    await ui.gmailImportLimitSaveButton.click();
    await flush();

    const settingsCall = calls.find((c) => c.url === "/api/gmail/settings");
    assert.deepEqual(settingsCall.body, { import_limit: 40 }, "clamped to the backend cap");
  });

  await test("blank import limit is rejected client-side (no POST)", async () => {
    const ui = mountController(ENV_CONNECTED);
    ui.gmailImportLimitInput.value = "   ";
    const calls = installFetch(() => ({}));

    await ui.gmailImportLimitSaveButton.click();
    await flush();

    assert.ok(
      !calls.some((c) => c.url === "/api/gmail/settings"),
      "a blank limit must not be posted"
    );
  });

  await test("a non-ok save surfaces the HTTP status (defensive parse)", async () => {
    // The new handlers check response.ok BEFORE parsing, so a 500 with a
    // non-JSON proxy body must not throw a raw SyntaxError -- the overall
    // banner shows the real status instead.
    const ui = mountController(ENV_CONNECTED);
    ui.gmailImportLimitInput.value = "30";
    installFetch((url) => {
      if (url === "/api/gmail/settings") {
        return { ok: false, status: 500, statusText: "Internal Server Error", nonJson: true };
      }
      if (url === "/api/gmail/status") return { payload: { gmail: { ...ENV_CONNECTED } } };
      if (url === "/api/matters") return { payload: { matters: [] } };
      return {};
    });

    await ui.gmailImportLimitSaveButton.click();
    await flush();

    // The error copy beside the input must mention the real status, not a
    // generic JSON parse failure.
    assert.match(ui.importLimitCopy.textContent, /HTTP 500/, "shows the real HTTP status");
    assert.doesNotMatch(ui.importLimitCopy.textContent, /JSON|token/i, "no raw parse error leaks");
  });

  await test("a capped import limit surfaces the server warning inline", async () => {
    // Honesty (Bug 1): the FE clamps before posting, but if the server still
    // returns a warning (e.g. a future cap change), it must be shown inline so
    // the admin understands the effective value, not left thinking it took raw.
    const ui = mountController(ENV_CONNECTED);
    ui.gmailImportLimitInput.value = "40";
    const refreshed = {
      ...ENV_CONNECTED,
      settings: { sync_enabled: true, import_limit: 40 },
    };
    installFetch((url) => {
      if (url === "/api/gmail/status") return { payload: { gmail: refreshed } };
      if (url === "/api/matters") return { payload: { matters: [] } };
      if (url === "/api/gmail/settings") {
        return {
          payload: {
            gmail: refreshed,
            warning: "Import limit capped at 40 (max safe per-poll value).",
          },
        };
      }
      return {};
    });

    await ui.gmailImportLimitSaveButton.click();
    await flush();

    assert.equal(
      ui.importLimitCopy.textContent,
      "Import limit capped at 40 (max safe per-poll value).",
      "the cap warning is shown inline next to the input"
    );
  });

  await test("empty search terms are rejected client-side (no silent default)", async () => {
    // Honesty (Bug 2): clearing the field must NOT post (which would let the
    // server default it back); the admin sees a clear inline message instead.
    const ui = mountController(ENV_CONNECTED);
    ui.gmailSearchTermsInput.value = "   \n  ";
    const calls = installFetch(() => ({}));

    await ui.gmailSearchSaveButton.click();
    await flush();

    assert.ok(
      !calls.some((c) => c.url === "/api/gmail/settings"),
      "an empty terms list must not be posted"
    );
    assert.match(
      ui.searchTermsCopy.textContent,
      /can't be empty/,
      "shows the honest empty-field message inline"
    );
  });

  await test("a 400 from the server on search terms surfaces inline", async () => {
    // If the field is non-empty client-side but the server still rejects it, the
    // 400 message must land inline so the admin knows the save did not take.
    const ui = mountController(ENV_CONNECTED);
    ui.gmailSearchTermsInput.value = "alpha";
    installFetch((url) => {
      if (url === "/api/gmail/settings") {
        return {
          ok: false,
          status: 400,
          payload: { error: "Add at least one Gmail search term — it can't be empty." },
        };
      }
      if (url === "/api/gmail/status") return { payload: { gmail: { ...ENV_CONNECTED } } };
      if (url === "/api/matters") return { payload: { matters: [] } };
      return {};
    });

    await ui.gmailSearchSaveButton.click();
    await flush();

    assert.match(
      ui.searchTermsCopy.textContent,
      /can't be empty/,
      "the server 400 is surfaced inline next to the field"
    );
  });

  await test("a 403 on the settings POST disables the toggle (admin-only latch)", async () => {
    // /api/gmail/settings is admin-only but the panel renders for every
    // signed-in user (the status GET is open). Once a settings write comes
    // back 403 the pause/resume switch must go read-only instead of offering a
    // control that can only fail again.
    const ui = mountController(ENV_CONNECTED);
    installFetch((url) => {
      if (url === "/api/gmail/settings") {
        return { ok: false, status: 403, payload: { error: "Administrator access is required." } };
      }
      if (url === "/api/gmail/status") return { payload: { gmail: { ...ENV_CONNECTED } } };
      if (url === "/api/matters") return { payload: { matters: [] } };
      return {};
    });

    await ui.gmailToggle.click();
    await flush();

    assert.equal(ui.state.gmailSettingsForbidden, true, "the admin-only latch is set");
    assert.equal(ui.gmailToggle.disabled, true, "switch is disabled after the 403");
    assert.equal(
      ui.gmailToggle.getAttribute("aria-label"),
      "Gmail polling is managed by an administrator",
      "the switch says who can manage polling"
    );

    // The latch survives a later re-render (e.g. a refresh of the status).
    ui.controller.renderGmailStatus({ ...ENV_CONNECTED });
    assert.equal(ui.gmailToggle.disabled, true, "still disabled after a re-render");
  });

  await test("a non-JSON 401 on load surfaces the status, not a parse error", async () => {
    // The historical "string did not match the expected pattern" class: load()
    // used to parse response.json() BEFORE checking response.ok, so a 401 with
    // an HTML/blank body threw a raw SyntaxError. Now the ok-first guard
    // surfaces the real HTTP status.
    const ui = mountController(ENV_CONNECTED);
    installFetch((url) => {
      if (url === "/api/gmail/status") {
        return { ok: false, status: 401, statusText: "Unauthorized", nonJson: true };
      }
      if (url === "/api/matters") return { payload: { matters: [] } };
      return {};
    });

    await ui.controller.load();
    await flush();

    assert.match(ui.inboundEmail.textContent, /HTTP 401/, "shows the real HTTP status");
    assert.doesNotMatch(ui.inboundEmail.textContent, /JSON|token/i, "no raw parse error leaks");
  });

  await test("master sync OFF greys the sub-roles and labels them inactive", async () => {
    // Honesty (Fix 3): when the master gate is off the scheduler skips Gmail, so
    // the inbound/outbound rows are inert -- the screen must say so (non-blocking
    // grey + label), not show them as plain "Yes".
    const ui = mountController({
      user_scoped: false,
      inbound: { ready: true, enabled: true, configured: true },
      outbound: { ready: true, enabled: true, configured: true },
      settings: { sync_enabled: false, import_limit: 25 },
    });

    assert.ok(
      ui.gmailCard.classList.contains("gmail-sync-off"),
      "the card carries the master-off class so CSS can grey the rows"
    );
    assert.match(
      ui.inboundConfigured.textContent,
      /Gmail sync is off, inactive/,
      "inbound row is labelled inactive"
    );
    assert.match(
      ui.outboundConfigured.textContent,
      /Gmail sync is off, inactive/,
      "outbound row is labelled inactive"
    );
  });

  await test("master sync ON does not mark the sub-roles inactive", async () => {
    const ui = mountController({
      user_scoped: false,
      inbound: { ready: true, enabled: true, configured: true },
      outbound: { ready: true, enabled: true, configured: true },
      settings: { sync_enabled: true, import_limit: 25 },
    });

    assert.ok(
      !ui.gmailCard.classList.contains("gmail-sync-off"),
      "no master-off class while polling is on"
    );
    assert.doesNotMatch(
      ui.inboundConfigured.textContent,
      /inactive/,
      "inbound row reads its normal configured status"
    );
    assert.equal(ui.inboundConfigured.textContent, "Yes");
  });

  // --- Pure helper assertions (clamp + parse correctness) -------------------
  const { AdminIntegrationsView: V } = require(
    path.join(__dirname, "..", "..", "static", "js", "admin-integrations.js")
  );

  await test("parseImportLimit clamps and validates", async () => {
    assert.equal(V.parseImportLimit("25"), 25);
    assert.equal(V.parseImportLimit("40"), 40);
    assert.equal(V.parseImportLimit("100"), 40, "clamps to the cap");
    assert.equal(V.parseImportLimit("0"), null);
    assert.equal(V.parseImportLimit("-3"), null);
    assert.equal(V.parseImportLimit("12.5"), null);
    assert.equal(V.parseImportLimit(""), null);
    assert.equal(V.parseImportLimit("abc"), null);
    assert.equal(V.MAX_IMPORT_LIMIT, 40, "UI cap matches the backend clamp");
  });

  await test("importLimitFromStatus clamps and defaults", async () => {
    assert.equal(V.importLimitFromStatus({ settings: { import_limit: 30 } }), 30);
    assert.equal(V.importLimitFromStatus({ settings: { import_limit: 999 } }), 40);
    assert.equal(V.importLimitFromStatus({ settings: {} }), 20);
    assert.equal(V.importLimitFromStatus({}), 20);
    assert.equal(V.importLimitFromStatus(null), 20);
  });

  // --- Sync window (days) controls -----------------------------------------

  await test("the sync-window input saves and re-renders from refreshed status", async () => {
    const ui = mountController(ENV_CONNECTED);
    ui.gmailSyncWindowInput.value = "30";
    const refreshed = {
      ...ENV_CONNECTED,
      inbound_window_days: 30,
      inbound_window_days_default: 90,
      settings: { sync_enabled: true, inbound_window_days: 30 },
    };
    const calls = installFetch((url) => {
      if (url === "/api/gmail/status") return { payload: { gmail: refreshed } };
      if (url === "/api/matters") return { payload: { matters: [] } };
      if (url === "/api/gmail/settings") return { payload: { gmail: refreshed } };
      return {};
    });

    await ui.gmailSyncWindowSaveButton.click();
    await flush();

    const save = calls.find((c) => c.url === "/api/gmail/settings");
    assert.ok(save, "a settings POST is sent");
    assert.deepEqual(save.body, { inbound_window_days: 30 }, "posts the typed window");
    assert.equal(ui.gmailSyncWindowInput.value, "30", "input reflects the saved window");
    assert.match(
      ui.syncWindowCopy.textContent,
      /Syncs emails from the last 30 days\./,
      "copy reflects the refreshed window"
    );
  });

  await test("an out-of-band sync window is rejected client-side (no POST)", async () => {
    const ui = mountController(ENV_CONNECTED);
    ui.gmailSyncWindowInput.value = "9999";
    const calls = installFetch(() => ({}));

    await ui.gmailSyncWindowSaveButton.click();
    await flush();

    assert.ok(
      !calls.some((c) => c.url === "/api/gmail/settings"),
      "an over-cap window must not be posted"
    );
    assert.match(
      ui.syncWindowCopy.textContent,
      /between 1 and 365/,
      "shows the inline band message"
    );
  });

  await test("a 400 from the server on the sync window surfaces inline", async () => {
    const ui = mountController(ENV_CONNECTED);
    // 30 is in-band client-side, so the POST happens and the server 400 must land
    // inline (proves the error path surfaces a server rejection too).
    ui.gmailSyncWindowInput.value = "30";
    installFetch((url) => {
      if (url === "/api/gmail/settings") {
        return {
          ok: false,
          status: 400,
          payload: { error: "Gmail sync window must be between 1 and 365 days." },
        };
      }
      if (url === "/api/gmail/status") return { payload: { gmail: { ...ENV_CONNECTED } } };
      if (url === "/api/matters") return { payload: { matters: [] } };
      return {};
    });

    await ui.gmailSyncWindowSaveButton.click();
    await flush();

    assert.match(
      ui.syncWindowCopy.textContent,
      /between 1 and 365 days/,
      "the server 400 is surfaced inline next to the field"
    );
  });

  await test("parseSyncWindow validates the band", async () => {
    assert.equal(V.parseSyncWindow("30"), 30);
    assert.equal(V.parseSyncWindow("1"), 1);
    assert.equal(V.parseSyncWindow("365"), 365);
    assert.equal(V.parseSyncWindow("3650"), 3650, "the widened ceiling is accepted");
    assert.equal(V.parseSyncWindow("0"), null);
    assert.equal(V.parseSyncWindow("-5"), null);
    assert.equal(V.parseSyncWindow("3651"), null, "rejects one past the ceiling");
    assert.equal(V.parseSyncWindow("99999"), null, "rejects far over-cap (no silent clamp)");
    assert.equal(V.parseSyncWindow("12.5"), null);
    assert.equal(V.parseSyncWindow(""), null);
    assert.equal(V.parseSyncWindow("abc"), null);
    assert.equal(V.MAX_SYNC_WINDOW, 3650, "UI cap matches the backend band");
    assert.equal(V.MIN_SYNC_WINDOW, 1, "UI floor matches the backend band");
  });

  await test("syncWindowFromStatus clamps and defaults", async () => {
    assert.equal(V.syncWindowFromStatus({ inbound_window_days: 30 }), 30);
    assert.equal(V.syncWindowFromStatus({ settings: { inbound_window_days: 45 } }), 45);
    assert.equal(V.syncWindowFromStatus({ inbound_window_days: 3650 }), 3650);
    assert.equal(V.syncWindowFromStatus({ inbound_window_days: 99999 }), 3650, "clamps to the cap");
    assert.equal(V.syncWindowFromStatus({ settings: {} }), 90, "defaults to 90");
    assert.equal(V.syncWindowFromStatus({}), 90);
    assert.equal(V.syncWindowFromStatus(null), 90);
  });

  process.stdout.write(`\nadmin-integrations.cjs: ${passed} passed\n`);
})().catch((error) => {
  process.stderr.write(`\nadmin-integrations.cjs FAILED: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
