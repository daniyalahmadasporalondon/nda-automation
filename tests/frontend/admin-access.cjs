"use strict";

// Frontend unit test for the Admin Access panel (admin-access.js).
//
// admin-access.js is a classic browser script (an IIFE assigned to a global)
// with a CommonJS export behind a `typeof module` guard. We require it here and
// drive the real controller through a minimal fake DOM + a stubbed fetch +
// stubbed window.escapeHtml / window.AuthExpired, so the shipped wiring (probe
// GET, add POST, delegated Remove DELETE, re-render from the full list, HTML
// escaping) is exercised exactly as it runs in the browser.
//
// Coverage (the panel's behavioural contract):
//   * load() probes GET /api/admin/admins and renders env roots (immutable, no
//     remove button) + persisted admins (each with a Remove button);
//   * a 403 on the probe renders the calm "admin only" read-only state (no error,
//     controls disabled);
//   * the add form POSTs {email} to /api/admin/admins/add and re-renders from the
//     returned full list;
//   * a delegated Remove DELETEs {email} to /api/admin/admins and re-renders;
//   * a 409 (lockout / immutable) on remove surfaces the server error inline and
//     reloads the authoritative list;
//   * interpolated emails are HTML-escaped (no injection).

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
    this.disabled = false;
    this.textContent = "";
    this.innerHTML = "";
    this._listeners = {};
  }
  addEventListener(type, handler) {
    (this._listeners[type] || (this._listeners[type] = [])).push(handler);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  focus() {}
  async dispatchEvent(event) {
    const handlers = this._listeners[event.type] || [];
    for (const handler of handlers) {
      // eslint-disable-next-line no-await-in-loop
      await handler.call(this, event);
    }
    return true;
  }
  async click() {
    await this.dispatchEvent({ type: "click", preventDefault() {} });
  }
  async submit() {
    await this.dispatchEvent({ type: "submit", preventDefault() {} });
  }
}

// A button stand-in used as event.target for a delegated Remove click. It
// satisfies the controller's `event.target.closest("[data-admin-remove]")` and
// getAttribute lookups.
function removeClickTarget(email) {
  return {
    closest(selector) {
      if (selector === "[data-admin-remove]") return this;
      return null;
    },
    getAttribute(name) {
      return name === "data-admin-remove" ? email : null;
    },
  };
}

// --- Stubs ------------------------------------------------------------------
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

global.window = {
  escapeHtml,
  AuthExpired: {
    async parseOkJson(response, fallback, reviewErrorFromPayload) {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw reviewErrorFromPayload(payload, `${fallback} (HTTP ${response.status})`);
      }
      return payload;
    },
  },
};

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

const { createAdminAccessController } = require(
  path.join(__dirname, "..", "..", "static", "js", "admin-access.js")
);

function mount() {
  const elements = {
    card: new FakeElement("article"),
    overall: new FakeElement("span"),
    refreshButton: new FakeElement("button"),
    addForm: new FakeElement("form"),
    emailInput: new FakeElement("input"),
    addButton: new FakeElement("button"),
    message: new FakeElement("p"),
    envRootsList: new FakeElement("ul"),
    persistedList: new FakeElement("ul"),
  };
  const reviewErrorFromPayload = (payload, fallback) =>
    new Error((payload && payload.error) || fallback);
  const controller = createAdminAccessController({ ...elements, reviewErrorFromPayload });
  return { controller, ...elements };
}

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// A known, parseable ISO-8601 UTC added_at for the date-render assertion. The
// panel formats in LOCAL time, so we derive the expected "D Mon YYYY, HH:MM"
// string from the same Date here -> the assertion holds in any timezone.
const KNOWN_ADDED_AT = "2026-06-19T10:21:33+00:00";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function expectedAddedAt(iso) {
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${hh}:${mm}`;
}

const LIST = {
  env_root_admins: ["google:root", "boot@example.com"],
  persisted_admins: [
    { email: "alice@example.com", added_at: KNOWN_ADDED_AT, added_by: "admin@example.com" },
  ],
};

(async () => {
  await test("load() renders env roots (no remove) and persisted (with remove)", async () => {
    const ui = mount();
    installFetch((url) => {
      if (url === "/api/admin/admins") return { payload: LIST };
      return {};
    });
    await ui.controller.load();
    await flush();

    assert.match(ui.envRootsList.innerHTML, /google:root/);
    assert.match(ui.envRootsList.innerHTML, /Bootstrap/);
    assert.doesNotMatch(ui.envRootsList.innerHTML, /data-admin-remove/, "env roots have no Remove button");
    assert.match(ui.persistedList.innerHTML, /alice@example\.com/);
    assert.match(ui.persistedList.innerHTML, /data-admin-remove="alice@example\.com"/);
    // The row carries the actor AND a human-readable added-at derived from the
    // ISO added_at (rendered in local time; see expectedAddedAt).
    assert.match(ui.persistedList.innerHTML, /Added by admin@example\.com/);
    const expectedDate = expectedAddedAt(KNOWN_ADDED_AT);
    assert.ok(
      ui.persistedList.innerHTML.includes(expectedDate),
      `expected the row to include the formatted added-at "${expectedDate}"`,
    );
    assert.doesNotMatch(ui.persistedList.innerHTML, /Invalid Date/, "no Invalid Date ever rendered");
    assert.equal(ui.overall.textContent, "3 admins", "2 env roots + 1 persisted");
    assert.equal(ui.emailInput.disabled, false);
  });

  await test("enriched env roots render a friendly label + raw id subtitle + (you)", async () => {
    const ui = mount();
    installFetch((url) => {
      if (url === "/api/admin/admins") {
        return {
          payload: {
            // The backend now sends enriched objects. A bare google:<sub> with no
            // known email shows the friendly "Google account ···<last6>" label;
            // an email-shaped root shows its email; the caller's own root is
            // tagged (you).
            env_root_admins: [
              { id: "google:101508195488490085718", kind: "google", email: "", display: "Google account ···490085718", is_self: false },
              { id: "boot@example.com", kind: "email", email: "boot@example.com", display: "boot@example.com", is_self: false },
              { id: "google:777", kind: "google", email: "me@example.com", display: "me@example.com", name: "Mia", is_self: true },
            ],
            persisted_admins: [],
          },
        };
      }
      return {};
    });
    await ui.controller.load();
    await flush();

    const html = ui.envRootsList.innerHTML;
    // 1) The opaque google:<sub> root shows the FRIENDLY label as the primary
    //    text, with the full raw id demoted to the subtitle (still present so a
    //    human can read it on hover / in the mono line).
    assert.match(html, /Google account ···490085718/, "friendly label is the primary text");
    assert.match(html, /admin-access-id[^>]*>google:101508195488490085718/, "raw id demoted to a subtitle");
    assert.match(html, /title="google:101508195488490085718"/, "full id available on hover");
    // 2) An email root shows its email (no redundant id subtitle since id===email).
    assert.match(html, /boot@example\.com/);
    // 3) The caller's own root is tagged (you) and shows the email.
    assert.match(html, /me@example\.com/);
    assert.match(html, /admin-access-you[^>]*>\(you\)/, "the caller's own root is tagged (you)");
    // Still immutable: env roots never carry a Remove button, and the Bootstrap
    // badge stays.
    assert.doesNotMatch(html, /data-admin-remove/, "env roots have no Remove button");
    assert.match(html, /Bootstrap/);
    // 3 env roots + 0 persisted.
    assert.equal(ui.overall.textContent, "3 admins");
  });

  await test("a legacy STRING env root still renders a friendly label (back-compat)", async () => {
    const ui = mount();
    installFetch((url) => {
      if (url === "/api/admin/admins") {
        // An older cached payload may still send plain strings.
        return { payload: { env_root_admins: ["google:101508195488490085718"], persisted_admins: [] } };
      }
      return {};
    });
    await ui.controller.load();
    await flush();
    const html = ui.envRootsList.innerHTML;
    // The string path derives the same friendly form (last 6 of the subject);
    // the raw id is still shown.
    assert.match(html, /Google account ···085718/, "string id is humanized too");
    assert.match(html, /google:101508195488490085718/, "raw id still present");
    assert.doesNotMatch(html, /data-admin-remove/);
  });

  await test("a missing/blank added_at renders 'Added by ...' with NO date (no Invalid Date)", async () => {
    const ui = mount();
    installFetch((url) => {
      if (url === "/api/admin/admins") {
        return {
          payload: {
            env_root_admins: [],
            persisted_admins: [
              { email: "blank@example.com", added_at: "", added_by: "admin@example.com" },
              { email: "missing@example.com", added_by: "admin@example.com" },
              { email: "bad@example.com", added_at: "not-a-date", added_by: "admin@example.com" },
            ],
          },
        };
      }
      return {};
    });
    await ui.controller.load();
    await flush();

    const html = ui.persistedList.innerHTML;
    assert.match(html, /Added by admin@example\.com/, "actor still shown without a date");
    assert.doesNotMatch(html, /Invalid Date/, "never render Invalid Date for blank/missing/unparseable added_at");
    // No "·" separator (raw or escaped) is emitted when there is no date.
    assert.doesNotMatch(html, /&middot;/, "no separator without a date");
    assert.doesNotMatch(html, /·/, "no raw middot without a date");
  });

  await test("a 403 probe renders the calm admin-only state (disabled, no error)", async () => {
    const ui = mount();
    installFetch((url) => {
      if (url === "/api/admin/admins") return { ok: false, status: 403, payload: { error: "Administrator access is required." } };
      return {};
    });
    await ui.controller.load();
    await flush();

    assert.equal(ui.overall.textContent, "Admin only");
    assert.equal(ui.emailInput.disabled, true, "controls disabled for a non-admin");
    assert.equal(ui.addButton.disabled, true);
    assert.match(ui.message.textContent, /managed by an administrator/i);
    assert.doesNotMatch(ui.message.textContent, /required/i, "no raw 403 error text leaks");
  });

  await test("add form POSTs {email} and re-renders from the returned list", async () => {
    const ui = mount();
    ui.emailInput.value = "Bob@Example.com";
    const after = {
      env_root_admins: LIST.env_root_admins,
      persisted_admins: [
        ...LIST.persisted_admins,
        { email: "bob@example.com", added_at: "t2", added_by: "admin@example.com" },
      ],
    };
    const calls = installFetch((url) => {
      if (url === "/api/admin/admins/add") return { payload: after };
      return {};
    });
    await ui.addForm.submit();
    await flush();

    const addCall = calls.find((c) => c.url === "/api/admin/admins/add");
    assert.ok(addCall, "expected a POST to /api/admin/admins/add");
    assert.equal(addCall.method, "POST");
    assert.deepEqual(addCall.body, { email: "Bob@Example.com" }, "posts the typed email (server normalizes)");
    assert.match(ui.persistedList.innerHTML, /bob@example\.com/, "re-rendered from the returned list");
    assert.equal(ui.emailInput.value, "", "input cleared on success");
  });

  await test("blank email is rejected client-side (no POST)", async () => {
    const ui = mount();
    ui.emailInput.value = "   ";
    const calls = installFetch(() => ({}));
    await ui.addForm.submit();
    await flush();
    assert.ok(!calls.some((c) => c.url === "/api/admin/admins/add"), "a blank email must not be posted");
  });

  await test("delegated Remove DELETEs {email} and re-renders", async () => {
    const ui = mount();
    // Seed the rendered list first.
    installFetch((url) => (url === "/api/admin/admins" ? { payload: LIST } : {}));
    await ui.controller.load();
    await flush();

    const afterRemove = { env_root_admins: LIST.env_root_admins, persisted_admins: [] };
    const calls = installFetch((url) => {
      if (url === "/api/admin/admins") return { payload: afterRemove };
      return {};
    });
    // Fire a delegated click whose target resolves to the Remove button.
    await ui.persistedList.dispatchEvent({ type: "click", target: removeClickTarget("alice@example.com") });
    await flush();

    const delCall = calls.find((c) => c.url === "/api/admin/admins" && c.method === "DELETE");
    assert.ok(delCall, "expected a DELETE to /api/admin/admins");
    assert.deepEqual(delCall.body, { email: "alice@example.com" });
    assert.doesNotMatch(ui.persistedList.innerHTML, /alice@example\.com/, "row removed after re-render");
  });

  await test("a 409 on remove surfaces the server error and reloads the list", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/admin/admins" ? { payload: LIST } : {}));
    await ui.controller.load();
    await flush();

    let deleteCalls = 0;
    const calls = installFetch((url, method) => {
      if (url === "/api/admin/admins" && method === "DELETE") {
        deleteCalls += 1;
        return { ok: false, status: 409, payload: { error: "Cannot remove the last administrator. Add another admin first." } };
      }
      if (url === "/api/admin/admins") return { payload: LIST }; // the reload
      return {};
    });
    await ui.persistedList.dispatchEvent({ type: "click", target: removeClickTarget("alice@example.com") });
    await flush();

    assert.equal(deleteCalls, 1);
    assert.match(ui.message.textContent, /last administrator/i, "server 409 surfaced inline");
    // It reloaded the authoritative list (the GET fired after the failed DELETE).
    assert.ok(calls.some((c) => c.url === "/api/admin/admins" && c.method === "GET"), "reloaded the list after a failed remove");
    assert.match(ui.persistedList.innerHTML, /alice@example\.com/, "the un-removed row is still shown");
  });

  await test("interpolated emails are HTML-escaped (no injection)", async () => {
    const ui = mount();
    const evil = '"><img src=x onerror=alert(1)>@evil.com';
    installFetch((url) => {
      if (url === "/api/admin/admins") {
        return {
          payload: {
            env_root_admins: [],
            persisted_admins: [{ email: evil, added_at: "t", added_by: evil }],
          },
        };
      }
      return {};
    });
    await ui.controller.load();
    await flush();

    assert.doesNotMatch(ui.persistedList.innerHTML, /<img/, "raw tag must not appear unescaped");
    assert.match(ui.persistedList.innerHTML, /&lt;img/, "tag is escaped");
    assert.match(ui.persistedList.innerHTML, /&quot;/, "quotes escaped");
  });

  process.stdout.write(`\nadmin-access.cjs: ${passed} passed\n`);
})().catch((error) => {
  process.stderr.write(`\nadmin-access.cjs FAILED: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
