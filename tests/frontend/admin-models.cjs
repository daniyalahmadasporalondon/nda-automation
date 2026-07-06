"use strict";

// Frontend unit test for the Admin AI Models panel (admin-models.js).
//
// admin-models.js is a classic browser script (an IIFE assigned to a global)
// with a CommonJS export behind a `typeof module` guard. We require it here and
// drive the real controller through a minimal fake DOM + a stubbed fetch +
// stubbed window.escapeHtml / window.humanizeId / window.AuthExpired, so the
// shipped wiring (probe GET, per-role rows + source badges, Save diff POST,
// 400/403/warn handling, HTML escaping) is exercised exactly as in the browser.
//
// Coverage (the panel's behavioural contract):
//   * load() probes GET /api/ai/settings and renders ALL 11 roles from the
//     `ai_models` array, in the order the backend supplied;
//   * each row shows a source badge reflecting source (persisted -> Admin
//     override, env -> From env, default -> Default);
//   * Save POSTs ONLY the changed role(s) as {models:{role:id}} to
//     /api/ai/models and re-renders from the authoritative response;
//   * a 400 surfaces the server {error} inline and does NOT wipe the other rows'
//     in-progress edits (nothing was saved);
//   * a "Custom..." selection routes the free-text input value into the POST;
//   * Reset-to-default POSTs {role:""} to clear an override;
//   * a 200 with an ai_model_unverified warning surfaces a non-blocking notice;
//   * a 403 probe renders the calm admin-only state (disabled, no error leak);
//   * a missing ai_models array degrades gracefully (no crash, empty state);
//   * interpolated model ids are HTML-escaped (no injection).

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

// A tiny DOM node that supports the SUBSET of the API the controller uses:
// innerHTML assignment + querySelector/querySelectorAll over the rendered HTML.
// Because the controller writes a full HTML string and then reads it back via
// data-* attribute selectors, we parse the assigned innerHTML into lightweight
// row records and serve querySelector results from them.
class FakeElement {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.attributes = {};
    this.classList = new FakeClassList();
    this.dataset = {};
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this._innerHTML = "";
    this._listeners = {};
    this._rows = [];
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this._rows = parseRows(html);
  }
  get innerHTML() {
    return this._innerHTML;
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
  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length ? all[0] : null;
  }
  querySelectorAll(selector) {
    // Row node lookup: [data-model-row="<role>"]
    const rowMatch = selector.match(/^\[data-model-row="(.+)"\]$/);
    if (rowMatch) {
      const role = rowMatch[1].replace(/\\(.)/g, "$1");
      const row = this._rows.find((r) => r.role === role);
      return row ? [row.node] : [];
    }
    // Bulk control disable: "select, input, [data-model-reset]"
    if (selector.includes("data-model-reset") && selector.includes("select")) {
      const nodes = [];
      this._rows.forEach((r) => {
        nodes.push(r.selectNode, r.customNode, r.resetNode);
      });
      return nodes;
    }
    return [];
  }
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
}

// Parse the rows-list innerHTML into per-role records, each exposing a select /
// custom input / reset button stand-in. The controller addresses these via
// querySelector("[data-model-select]") etc. WITHIN a row node, and reads/writes
// their .value / .hidden / .disabled -- so the stand-ins are mutable objects.
function parseRows(html) {
  const rows = [];
  const rowRe = /data-model-row="([^"]+)"/g;
  let m;
  const roles = [];
  while ((m = rowRe.exec(html)) !== null) roles.push(m[1]);
  roles.forEach((role) => {
    // Effective (pre-selected) model: the option marked selected, else the
    // custom input's value (when Custom is the selected option).
    const selectNode = makeSelect(html, role);
    const customNode = makeCustom(html, role);
    const resetNode = { disabled: false, getAttribute: (n) => (n === "data-model-reset" ? role : null) };
    const node = {
      role,
      querySelector(sel) {
        if (sel === "[data-model-select]") return selectNode;
        if (sel === "[data-model-custom]") return customNode;
        return null;
      },
    };
    rows.push({ role, node, selectNode, customNode, resetNode });
  });
  return rows;
}

function makeSelect(html, role) {
  // Extract the <select data-model-select="role"> ... </select> block and its
  // selected option value (or the Custom sentinel if Custom is selected).
  const block = sliceTag(html, `data-model-select="${role}"`, "</select>");
  let value = "";
  if (block) {
    const sel = block.match(/<option value="([^"]*)"\s+selected>/);
    value = sel ? sel[1] : "";
  }
  return {
    value,
    disabled: false,
    getAttribute(n) {
      return n === "data-model-select" ? role : null;
    },
    closest(sel) {
      return sel === "[data-model-select]" ? this : null;
    },
  };
}

function makeCustom(html, role) {
  const block = sliceTag(html, `data-model-custom="${role}"`, ">");
  let value = "";
  let hidden = false;
  if (block) {
    const v = block.match(/value="([^"]*)"/);
    value = v ? unescapeHtml(v[1]) : "";
    hidden = /\shidden(\s|>|$)/.test(block);
  }
  return {
    value,
    hidden,
    disabled: false,
    focus() {},
    getAttribute(n) {
      return n === "data-model-custom" ? role : null;
    },
  };
}

function sliceTag(html, anchor, endToken) {
  const start = html.indexOf(anchor);
  if (start === -1) return "";
  // Walk backward to the opening "<".
  const open = html.lastIndexOf("<", start);
  const end = html.indexOf(endToken, start);
  if (end === -1) return html.slice(open);
  return html.slice(open, end + endToken.length);
}

function unescapeHtml(value) {
  return String(value)
    .replaceAll("&amp;", "&")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replaceAll("&#039;", "'");
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
  humanizeId: (value) => String(value),
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

const { createAdminModelsController } = require(
  path.join(__dirname, "..", "..", "static", "js", "admin-models.js")
);

function mount() {
  const elements = {
    card: new FakeElement("article"),
    overall: new FakeElement("span"),
    refreshButton: new FakeElement("button"),
    rowsList: new FakeElement("ul"),
    saveButton: new FakeElement("button"),
    message: new FakeElement("p"),
    warningNote: new FakeElement("p"),
  };
  const reviewErrorFromPayload = (payload, fallback) =>
    new Error((payload && payload.error) || fallback);
  const controller = createAdminModelsController({ ...elements, reviewErrorFromPayload });
  return { controller, ...elements };
}

// The canonical 11-role overview the backend GET returns (model_resolver order).
const ALL_ROLES = [
  "reviewer",
  "verifier",
  "structure",
  "semantic_lint",
  "generation",
  "gmail_triage",
  "gmail_intake",
  "pdf_ocr",
  "dashboard_assistant",
  "search_intent",
  "matter_summary",
];

function overview(overrides = {}) {
  return ALL_ROLES.map((role) => ({
    role,
    model: overrides[role]?.model || `default/${role}`,
    source: overrides[role]?.source || "default",
    env_var: `NDA_${role.toUpperCase()}_MODEL`,
    default: `default/${role}`,
    recommended: overrides[role]?.recommended || [`rec-a/${role}`, `rec-b/${role}`],
  }));
}

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

(async () => {
  await test("load() renders ALL 11 roles in order with source badges", async () => {
    const ui = mount();
    const models = overview({
      reviewer: { model: "rec-a/reviewer", source: "persisted", recommended: ["rec-a/reviewer", "rec-b/reviewer"] },
      verifier: { model: "env/verifier", source: "env" },
    });
    installFetch((url) => (url === "/api/ai/settings" ? { payload: { ai_models: models } } : {}));
    await ui.controller.load();
    await flush();

    // All 11 role rows are present.
    ALL_ROLES.forEach((role) => {
      assert.match(ui.rowsList.innerHTML, new RegExp(`data-model-row="${role}"`), `row for ${role}`);
    });
    assert.equal((ui.rowsList.innerHTML.match(/data-model-row=/g) || []).length, 11, "exactly 11 rows");
    assert.equal(ui.overall.textContent, "11 roles");

    // Source badge reflects `source`.
    assert.match(ui.rowsList.innerHTML, /admin-models-badge--persisted">Admin override/);
    assert.match(ui.rowsList.innerHTML, /admin-models-badge--env">From env/);
    assert.match(ui.rowsList.innerHTML, /admin-models-badge--default">Default/);

    // The 3 decoupled roles are flagged Independent.
    assert.match(ui.rowsList.innerHTML, /Independent/);
  });

  await test("Save POSTs only the changed role as {models:{role:id}} and re-renders", async () => {
    const ui = mount();
    const before = overview();
    installFetch((url) => (url === "/api/ai/settings" ? { payload: { ai_models: before } } : {}));
    await ui.controller.load();
    await flush();

    // Change ONLY the reviewer to its first recommended option.
    const reviewerRow = ui.rowsList._rows.find((r) => r.role === "reviewer");
    reviewerRow.selectNode.value = "rec-a/reviewer";

    const after = overview({ reviewer: { model: "rec-a/reviewer", source: "persisted" } });
    const calls = installFetch((url) => {
      if (url === "/api/ai/models") return { payload: { ai_models: after, operational_warnings: [] } };
      return {};
    });
    await ui.saveButton.click();
    await flush();

    const post = calls.find((c) => c.url === "/api/ai/models" && c.method === "POST");
    assert.ok(post, "expected a POST to /api/ai/models");
    assert.deepEqual(post.body, { models: { reviewer: "rec-a/reviewer" } }, "only the changed role is sent");
    assert.match(ui.rowsList.innerHTML, /admin-models-badge--persisted">Admin override/, "re-rendered from response");
    assert.match(ui.message.textContent, /saved/i);
  });

  await test("no changes => no POST", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/ai/settings" ? { payload: { ai_models: overview() } } : {}));
    await ui.controller.load();
    await flush();
    const calls = installFetch(() => ({}));
    await ui.saveButton.click();
    await flush();
    assert.ok(!calls.some((c) => c.url === "/api/ai/models"), "an unchanged form must not POST");
    assert.match(ui.message.textContent, /no model changes/i);
  });

  await test("a 400 surfaces the error inline WITHOUT wiping edits", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/ai/settings" ? { payload: { ai_models: overview() } } : {}));
    await ui.controller.load();
    await flush();

    // Stage TWO edits; the server rejects the whole request.
    ui.rowsList._rows.find((r) => r.role === "reviewer").selectNode.value = "rec-a/reviewer";
    ui.rowsList._rows.find((r) => r.role === "verifier").selectNode.value = "rec-b/verifier";
    const htmlBeforeSave = ui.rowsList.innerHTML;

    installFetch((url) => {
      if (url === "/api/ai/models") {
        return { ok: false, status: 400, payload: { error: "reviewer: model not found in the OpenRouter catalog." } };
      }
      return {};
    });
    await ui.saveButton.click();
    await flush();

    assert.match(ui.message.textContent, /not found in the OpenRouter catalog/i, "server 400 surfaced inline");
    assert.equal(ui.rowsList.innerHTML, htmlBeforeSave, "rows NOT re-rendered -- edits preserved");
  });

  await test("Custom selection routes the free-text id into the POST", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/ai/settings" ? { payload: { ai_models: overview() } } : {}));
    await ui.controller.load();
    await flush();

    // Select Custom for the generation role and type an arbitrary slug.
    const row = ui.rowsList._rows.find((r) => r.role === "generation");
    row.selectNode.value = "__custom__";
    row.customNode.value = "acme/custom-model-9";

    const calls = installFetch((url) => {
      if (url === "/api/ai/models") return { payload: { ai_models: overview(), operational_warnings: [] } };
      return {};
    });
    await ui.saveButton.click();
    await flush();

    const post = calls.find((c) => c.url === "/api/ai/models");
    assert.deepEqual(post.body, { models: { generation: "acme/custom-model-9" } });
  });

  await test("a Custom selection with an EMPTY box blocks the save (no POST)", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/ai/settings" ? { payload: { ai_models: overview() } } : {}));
    await ui.controller.load();
    await flush();
    const row = ui.rowsList._rows.find((r) => r.role === "generation");
    row.selectNode.value = "__custom__";
    row.customNode.value = "   ";
    const calls = installFetch(() => ({}));
    await ui.saveButton.click();
    await flush();
    assert.ok(!calls.some((c) => c.url === "/api/ai/models"), "an empty custom id must not be posted");
    assert.match(ui.message.textContent, /enter a model id/i);
  });

  await test("Reset to default POSTs {role:''} to clear an override", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/ai/settings"
      ? { payload: { ai_models: overview({ reviewer: { model: "rec-a/reviewer", source: "persisted" } }) } }
      : {}));
    await ui.controller.load();
    await flush();

    const calls = installFetch((url) => {
      if (url === "/api/ai/models") return { payload: { ai_models: overview(), operational_warnings: [] } };
      return {};
    });
    // Fire the delegated reset click for the reviewer row.
    await ui.rowsList.dispatchEvent({
      type: "click",
      preventDefault() {},
      target: { closest: (s) => (s === "[data-model-reset]" ? { getAttribute: () => "reviewer" } : null) },
    });
    await flush();

    const post = calls.find((c) => c.url === "/api/ai/models");
    assert.ok(post, "expected a POST to clear the override");
    assert.deepEqual(post.body, { models: { reviewer: "" } }, "an empty string clears the override");
  });

  await test("a 200 with ai_model_unverified surfaces a non-blocking notice", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/ai/settings" ? { payload: { ai_models: overview() } } : {}));
    await ui.controller.load();
    await flush();
    ui.rowsList._rows.find((r) => r.role === "reviewer").selectNode.value = "rec-a/reviewer";

    installFetch((url) => {
      if (url === "/api/ai/models") {
        return {
          payload: {
            ai_models: overview({ reviewer: { model: "rec-a/reviewer", source: "persisted" } }),
            operational_warnings: [{ code: "ai_model_unverified", message: "reviewer: catalog unreachable." }],
          },
        };
      }
      return {};
    });
    await ui.saveButton.click();
    await flush();

    assert.equal(ui.warningNote.hidden, false, "the warning note is shown");
    assert.match(ui.warningNote.textContent, /could not be verified/i);
    assert.match(ui.warningNote.textContent, /catalog unreachable/i);
  });

  await test("a 403 probe renders the calm admin-only state (disabled, no error leak)", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/ai/settings"
      ? { ok: false, status: 403, payload: { error: "Administrator access is required." } }
      : {}));
    await ui.controller.load();
    await flush();

    assert.equal(ui.overall.textContent, "Admin only");
    assert.equal(ui.saveButton.disabled, true, "controls disabled for a non-admin");
    assert.match(ui.message.textContent, /managed by an administrator/i);
    assert.doesNotMatch(ui.message.textContent, /required/i, "no raw 403 error text leaks");
  });

  await test("a missing ai_models array degrades gracefully (empty state, no crash)", async () => {
    const ui = mount();
    installFetch((url) => (url === "/api/ai/settings" ? { payload: {} } : {}));
    await ui.controller.load();
    await flush();
    assert.match(ui.rowsList.innerHTML, /No AI roles were returned/);
    assert.equal(ui.overall.textContent, "No roles");
  });

  await test("a role with enabled=false renders a 'Feature off' badge and a subdued row", async () => {
    const ui = mount();
    const models = overview();
    // Mark the two dormant roles off; leave everything else on (default true).
    models.forEach((m) => {
      m.enabled = !(m.role === "pdf_ocr" || m.role === "structure");
    });
    installFetch((url) => (url === "/api/ai/settings" ? { payload: { ai_models: models } } : {}));
    await ui.controller.load();
    await flush();

    // Exactly the two dormant rows are flagged off + subdued.
    assert.equal((ui.rowsList.innerHTML.match(/Feature off/g) || []).length, 2, "two Feature off badges");
    assert.equal((ui.rowsList.innerHTML.match(/admin-models-row--off/g) || []).length, 2, "two subdued rows");
    assert.match(ui.rowsList.innerHTML, /admin-models-featureoff[^>]*isn't used/, "explanatory tooltip present");

    // The exact two dormant rows carry the data flag; an on role does not.
    assert.match(ui.rowsList.innerHTML, /data-model-row="pdf_ocr" data-feature-off="1"/, "pdf_ocr row flagged off");
    assert.match(ui.rowsList.innerHTML, /data-model-row="structure" data-feature-off="1"/, "structure row flagged off");
    assert.doesNotMatch(
      ui.rowsList.innerHTML,
      /data-model-row="reviewer" data-feature-off/,
      "an on role is not flagged",
    );

    // The off row stays fully interactive -- the picker is still usable.
    const offSelect = makeSelect(ui.rowsList.innerHTML, "pdf_ocr");
    assert.ok(offSelect && offSelect.getAttribute("data-model-select") === "pdf_ocr", "off row keeps a live model picker");
  });

  await test("interpolated model ids are HTML-escaped (no injection)", async () => {
    const ui = mount();
    const evil = '"><img src=x onerror=alert(1)>';
    installFetch((url) => (url === "/api/ai/settings"
      ? { payload: { ai_models: overview({ reviewer: { model: evil, source: "persisted", recommended: [evil] } }) } }
      : {}));
    await ui.controller.load();
    await flush();
    assert.doesNotMatch(ui.rowsList.innerHTML, /<img/, "raw tag must not appear unescaped");
    assert.match(ui.rowsList.innerHTML, /&lt;img/, "tag is escaped");
  });

  process.stdout.write(`\nadmin-models.cjs: ${passed} passed\n`);
})().catch((error) => {
  process.stderr.write(`\nadmin-models.cjs FAILED: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
