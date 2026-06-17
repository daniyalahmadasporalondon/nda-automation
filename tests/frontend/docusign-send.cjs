"use strict";

// Frontend behavioural test for the DocuSign "Send for signature" composer
// controller (static/js/docusign-send.js). Exercises the two send-composer
// fixes against a tiny hand-rolled DOM + fetch stub (no jsdom — matching the
// repo's zero-dep FE harness style):
//
//   #30 — after a SUCCESSFUL send the submit button is truly inert: a second
//         submit must NOT fire a second /send-for-signature request (no
//         duplicate envelope), and the button reads "Sent for signature".
//   #31 — a 409 { needs_connect, connect_url } renders a GUIDING message (and a
//         clickable Connect link when connect_url is present), not a bare error.
//
// The controller resolves its model + auth guard off `window`, so we wire the
// REAL DocuSignModel (single-source .mjs/.cjs) and the REAL AuthExpired
// parse-guard, and pass a faithful copy of app.js's reviewErrorFromPayload (the
// function app.js hands the controller) so the needs_connect carry-through under
// test is the production shape.

const assert = require("node:assert/strict");
const path = require("node:path");

// --- minimal DOM / window stubs ---------------------------------------------

function makeNode(extra = {}) {
  const listeners = {};
  return {
    listeners,
    hidden: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    title: "",
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(...cs) { cs.forEach((c) => this._set.delete(c)); },
      toggle(c, on) { if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    dispatch(type, event = {}) {
      (listeners[type] || []).forEach((fn) => fn(event));
    },
    ...extra,
  };
}

function installGlobals() {
  const documentStub = {
    body: makeNode(),
    activeElement: null,
    addEventListener() {},
  };
  const windowStub = {
    DocuSignModel: require(path.join(__dirname, "..", "..", "static", "js", "modules", "docusign-model.mjs")).DocuSignModel,
    AuthExpired: require(path.join(__dirname, "..", "..", "static", "js", "auth-expired.js")).AuthExpired,
    escapeHtml: (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (ch) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[ch]
    )),
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    location: { href: "" },
  };
  global.window = windowStub;
  global.document = documentStub;
  return { windowStub, documentStub };
}

// A faithful copy of app.js's reviewErrorFromPayload (the function app.js passes
// into the controller). Mirrors the #31 needs_connect/connect_url carry-through.
function reviewErrorFromPayload(payload, fallbackMessage) {
  const error = new Error((payload && payload.error) || fallbackMessage);
  if (payload && payload.needs_connect) {
    error.needsConnect = true;
    if (payload.connect_url) error.connectUrl = String(payload.connect_url);
  }
  return error;
}

// --- harness -----------------------------------------------------------------

const { createDocuSignSendController } = require(
  path.join(__dirname, "..", "..", "static", "js", "docusign-send.js"),
);

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// One valid signer row, so collectSigners()/validateSigners() pass without
// building real input DOM.
function signerRowsStub() {
  const node = makeNode();
  node.querySelectorAll = () => [
    {
      querySelector(sel) {
        if (sel === "[data-docusign-signer-name]") {
          return { value: "Acme Corp", dataset: { docusignRole: "counterparty" } };
        }
        if (sel === "[data-docusign-signer-email]") return { value: "cp@acme.com" };
        return null;
      },
    },
  ];
  return node;
}

function buildController({ fetchImpl, matterRef }) {
  const submitButton = makeNode({ textContent: "Send for signature" });
  const statusNode = makeNode();
  const form = makeNode();
  global.fetch = fetchImpl;
  const controller = createDocuSignSendController({
    modalNode: makeNode(),
    closeButton: makeNode(),
    cancelButton: makeNode(),
    form,
    signerRows: signerRowsStub(),
    signingOrderControl: null,
    statusNode,
    badgeNode: makeNode(),
    headerBadgeNode: makeNode(),
    envelopeNode: makeNode(),
    downloadSignedLink: makeNode(),
    submitButton,
    triggerButton: null,
    getMatter: () => matterRef.current,
    getAsporaSignatory: () => ({ name: "Daniyal Ahmad", email: "daniyal.ahmad@aspora.com" }),
    reviewErrorFromPayload,
    downloadUrl: () => {},
    onMatterUpdated: (m) => { matterRef.current = m; },
  });
  return { controller, submitButton, statusNode, form };
}

async function submit(form) {
  // The controller's submit listener is async; await each dispatched handler.
  for (const fn of form.listeners.submit || []) {
    await fn({ preventDefault() {} });
  }
}

// --- #30: double-send -> only ONE request -----------------------------------

(async () => {
  installGlobals();

  await test("#30 a second submit after a successful send fires NO second request", async () => {
    let calls = 0;
    const matterRef = { current: { id: "m1", recipient_email: "cp@acme.com", counterparty_name: "Acme Corp" } };
    const fetchImpl = async (url) => {
      calls += 1;
      assert.match(url, /\/send-for-signature$/);
      return {
        ok: true,
        status: 201,
        json: async () => ({ envelope_id: "env-123", status: "sent" }),
      };
    };
    const { submitButton, form } = buildController({ fetchImpl, matterRef });

    await submit(form); // first send
    assert.equal(calls, 1, "first send should fire exactly one request");
    // Matter now carries an active envelope; the button is inert + relabelled.
    assert.equal(submitButton.disabled, true);
    assert.equal(submitButton.textContent, "Sent for signature");

    await submit(form); // second send attempt
    assert.equal(calls, 1, "second submit must NOT fire another request (no duplicate envelope)");
  });

  // --- #31: needs_connect -> guiding message + link -------------------------

  await test("#31 a 409 needs_connect renders a guiding message with a connect link", async () => {
    const matterRef = { current: { id: "m2", recipient_email: "cp@acme.com", counterparty_name: "Acme Corp" } };
    const fetchImpl = async () => ({
      ok: false,
      status: 409,
      json: async () => ({
        error: "DocuSign is not connected.",
        needs_connect: true,
        connect_url: "/api/docusign/connect",
      }),
    });
    const { statusNode, form } = buildController({ fetchImpl, matterRef });

    await submit(form);

    const rendered = statusNode.innerHTML || statusNode.textContent;
    // The apostrophe is HTML-escaped in the innerHTML path (&#039;).
    assert.match(rendered, /DocuSign isn(&#039;|')t connected/);
    assert.match(rendered, /Admin/);
    // The connect_url becomes a clickable link.
    assert.match(statusNode.innerHTML, /<a href="\/api\/docusign\/connect"[^>]*>Connect DocuSign<\/a>/);
    assert.ok(statusNode.classList.contains("error"));
  });

  await test("#31 without connect_url falls back to guiding TEXT only (no link)", async () => {
    const matterRef = { current: { id: "m3", recipient_email: "cp@acme.com", counterparty_name: "Acme Corp" } };
    const fetchImpl = async () => ({
      ok: false,
      status: 409,
      json: async () => ({ error: "DocuSign is not connected.", needs_connect: true }),
    });
    const { statusNode, form } = buildController({ fetchImpl, matterRef });

    await submit(form);
    assert.equal(statusNode.textContent, "DocuSign isn't connected — connect it in Admin → Integrations.");
    assert.doesNotMatch(statusNode.innerHTML || "", /<a /);
  });

  process.stdout.write(`\n${passed} passed\n`);
})().catch((error) => {
  process.stderr.write(`\nFAILED: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
