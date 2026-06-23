// Headless validation for the faithful-DOCX render path (build/docx-faithful-render).
//
// Two things are proven here, both against the REAL shipped artifacts:
//
//   (A) docx-preview (the VENDORED static/vendor/docx-preview/docx-preview.min.js)
//       actually renders Word tracked changes. We render a fixture .docx that
//       contains BOTH a tracked insertion (w:ins) and a tracked deletion (w:del +
//       w:delText) with `renderChanges: true` and assert the output DOM contains
//       <ins> AND <del> nodes carrying the expected text. This is the load-bearing
//       claim of the whole feature, so it is verified from artifacts, not assumed.
//
//   (B) The shipped module static/js/docx-faithful-render.js honours its
//       never-blank fallback contract: renderFaithfulDocx() resolves to
//       { ok:false } (never throws) when the flag is OFF, when the library is
//       unavailable, and when there are no bytes -- exactly the cases where the
//       caller must keep the existing renderer.
//
// docx-preview needs a real DOM (DOMParser / XMLSerializer / createElementNS), so
// this test uses jsdom. jsdom is a devDependency (see package.json); if it is not
// installed the test SKIPS cleanly (like the pytest verifier-eval gate) rather
// than failing, so CI without the optional dep stays green.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(HERE, "../..");
const VENDOR_DOCX = path.join(REPO, "static/vendor/docx-preview/docx-preview.min.js");
const VENDOR_JSZIP = path.join(REPO, "static/vendor/jszip/jszip.min.js");
const MODULE_FILE = path.join(REPO, "static/js/docx-faithful-render.js");
const FIXTURE = path.join(REPO, "tests/fixtures/tracked-changes-sample.docx");

// Resolve jsdom from the normal module graph, falling back to any directories on
// NODE_PATH (so a worktree-local install can run this without touching the shared
// node_modules). Skip cleanly if it is genuinely absent.
async function loadJsdom() {
  try {
    return (await import("jsdom")).JSDOM;
  } catch (_error) {
    // ignore; try NODE_PATH entries below
  }
  const { createRequire } = await import("node:module");
  const require = createRequire(import.meta.url);
  const roots = String(process.env.NODE_PATH || "").split(path.delimiter).filter(Boolean);
  for (const root of roots) {
    try {
      const entry = require.resolve("jsdom", { paths: [root] });
      return (await import(`file://${entry}`)).JSDOM;
    } catch (_error) {
      // try next root
    }
  }
  return null;
}

const JSDOM = await loadJsdom();
if (!JSDOM) {
  console.log("SKIP docx-faithful-render: jsdom not installed (run `npm install`). "
    + "The faithful-render path needs a DOM only for this headless test; the browser has one.");
  process.exit(0);
}

// ---------------------------------------------------------------------------
// (A) Vendored docx-preview renders tracked changes (w:ins -> <ins>, w:del -> <del>)
// ---------------------------------------------------------------------------
async function testTrackedChangesRender() {
  // Real DOM for docx-preview.
  const dom = new JSDOM("<!doctype html><html><body></body></html>");
  const { window } = dom;
  globalThis.window = window;
  globalThis.document = window.document;
  globalThis.DOMParser = window.DOMParser;
  globalThis.XMLSerializer = window.XMLSerializer;
  globalThis.Node = window.Node;
  globalThis.HTMLElement = window.HTMLElement;
  if (!globalThis.Blob) globalThis.Blob = window.Blob;

  // JSZip global, then the UMD docx-preview which reads it and assigns global.docx.
  const jszipSrc = fs.readFileSync(VENDOR_JSZIP, "utf8");
  const JSZip = new Function("module", "exports", `${jszipSrc}; return module.exports;`)(
    { exports: {} }, {},
  );
  globalThis.JSZip = JSZip;
  window.JSZip = JSZip;

  const docxSrc = fs.readFileSync(VENDOR_DOCX, "utf8");
  const docx = new Function("global", "JSZip", `${docxSrc}; return global.docx;`)(globalThis, JSZip);
  assert.equal(typeof docx.renderAsync, "function", "vendored docx-preview must expose renderAsync");

  const bytes = fs.readFileSync(FIXTURE);
  // Pass an ArrayBuffer: JSZip reads it natively. (jsdom's Blob is not readable by
  // JSZip in Node; a real browser Blob works directly, which is the production path.)
  const ab = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const container = window.document.createElement("div");

  await docx.renderAsync(ab, container, null, { renderChanges: true, inWrapper: false, ignoreFonts: true });

  const insNodes = container.querySelectorAll("ins");
  const delNodes = container.querySelectorAll("del");
  assert.ok(insNodes.length >= 1, `expected at least one <ins> (tracked insertion), got ${insNodes.length}`);
  assert.ok(delNodes.length >= 1, `expected at least one <del> (tracked deletion), got ${delNodes.length}`);

  const insText = Array.from(insNodes).map((n) => n.textContent).join(" ");
  const delText = Array.from(delNodes).map((n) => n.textContent).join(" ");
  assert.ok(insText.includes("shall remain confidential"),
    `inserted text must render inside <ins>; got "${insText}"`);
  assert.ok(delText.includes("forever and ever"),
    `deleted text must render inside <del>; got "${delText}"`);

  // Sanity: with renderChanges OFF the deletion must NOT survive as text (proves the
  // option is the actual gate, not incidental).
  const containerOff = window.document.createElement("div");
  const ab2 = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  await docx.renderAsync(ab2, containerOff, null, { renderChanges: false, inWrapper: false, ignoreFonts: true });
  assert.equal(containerOff.querySelectorAll("del").length, 0,
    "with renderChanges:false there should be no <del> nodes");

  console.log(`PASS (A) tracked-change render: ${insNodes.length} <ins>, ${delNodes.length} <del>; `
    + `inserted+deleted text present; renderChanges gate confirmed.`);
}

// ---------------------------------------------------------------------------
// (B) Shipped module never-blank fallback contract + localStorage flag
// ---------------------------------------------------------------------------
// SINGLE control path: localStorage["nda.faithfulDocxRender"]. DEFAULT ON now --
// only the explicit value "false" disables it (the kill-switch); the absent key and
// any other value enable. There is NO window flag any more. We build a tiny
// localStorage stub so the headless test can drive the flag exactly like the browser.
function makeLocalStorage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {
    getItem(key) { return map.has(key) ? map.get(key) : null; },
    setItem(key, value) { map.set(key, String(value)); },
    removeItem(key) { map.delete(key); },
  };
}

function loadFaithfulModule(windowStub, localStorageStub) {
  const win = windowStub || {};
  // Prefer the sandbox-global `localStorage` (the browser's first lookup). We do
  // NOT assign win.localStorage: a real jsdom window exposes it as a getter-only
  // property, so the module's `typeof localStorage !== "undefined"` path is what we
  // exercise here.
  let winLocalStorage;
  try { winLocalStorage = win.localStorage; } catch (_error) { winLocalStorage = undefined; }
  const sandbox = {
    window: win,
    document: win.document,
    console,
    module: { exports: {} },
    fetch: undefined,
    localStorage: localStorageStub || winLocalStorage,
  };
  sandbox.Blob = typeof Blob !== "undefined" ? Blob : undefined;
  sandbox.ArrayBuffer = ArrayBuffer;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(MODULE_FILE, "utf8"), sandbox, { filename: "docx-faithful-render.js" });
  return sandbox.module.exports;
}

// A library-present window: lazy ensureFaithfulDocxLibs() short-circuits because
// window.docx.renderAsync + window.JSZip are already present, so no <script> is
// injected. renderAsync paints whatever `paint` does into the scratch node.
function libraryReadyWindow(paint) {
  return {
    JSZip: {},
    docx: { renderAsync: async (data, container) => { if (paint) paint(container); } },
    document: { createElement: () => ({}) },
  };
}

async function testFallbackContract() {
  // Flag OFF (explicit "false" kill-switch) -> { ok:false, reason:"flag_off" }, never
  // throws. The flag now DEFAULTS ON, so OFF must be set explicitly to "false".
  const offStore = makeLocalStorage({ "nda.faithfulDocxRender": "false" });
  const mod1 = loadFaithfulModule(libraryReadyWindow(), offStore);
  const r1 = await mod1.renderFaithfulDocx({ innerHTML: "", childElementCount: 0, textContent: "" }, { url: "/x" });
  assert.equal(r1.ok, false, "flag OFF must resolve ok:false");
  assert.equal(r1.reason, "flag_off");

  // Flag ON but library genuinely unavailable AND no document to inject a <script>
  // -> ensureFaithfulDocxLibs rejects -> ok:false library_unavailable (no throw).
  const onStore = makeLocalStorage({ "nda.faithfulDocxRender": "1" });
  const mod2 = loadFaithfulModule({ document: undefined }, onStore);
  const r2 = await mod2.renderFaithfulDocx({ innerHTML: "", childElementCount: 0, textContent: "" }, { url: "/x" });
  assert.equal(r2.ok, false, "library unavailable must resolve ok:false");
  assert.equal(r2.reason, "library_unavailable");

  // Flag ON, library present, but no bytes / no url -> ok:false no_bytes (no throw).
  const mod3 = loadFaithfulModule(libraryReadyWindow(), makeLocalStorage({ "nda.faithfulDocxRender": "true" }));
  const r3 = await mod3.renderFaithfulDocx({ innerHTML: "", childElementCount: 0, textContent: "" }, {});
  assert.equal(r3.ok, false, "no bytes must resolve ok:false");
  assert.equal(r3.reason, "no_bytes");

  // No container -> ok:false no_container.
  const r4 = await mod3.renderFaithfulDocx(null, { url: "/x" });
  assert.equal(r4.ok, false);
  assert.equal(r4.reason, "no_container");

  // Flag string parsing (DEFAULT ON): enabled = (value !== "false"). Absent key,
  // legacy "1"/"true"/"on"/"yes", and even "0" all ENABLE now; only the explicit
  // kill-switch value "false" (case/space-insensitive) disables.
  assert.equal(loadFaithfulModule({}, makeLocalStorage({})).faithfulDocxRenderEnabled(), true, "default must be ON (absent key)");
  assert.equal(loadFaithfulModule({}, makeLocalStorage({ "nda.faithfulDocxRender": "on" })).faithfulDocxRenderEnabled(), true);
  assert.equal(loadFaithfulModule({}, makeLocalStorage({ "nda.faithfulDocxRender": "yes" })).faithfulDocxRenderEnabled(), true);
  assert.equal(loadFaithfulModule({}, makeLocalStorage({ "nda.faithfulDocxRender": "1" })).faithfulDocxRenderEnabled(), true);
  assert.equal(loadFaithfulModule({}, makeLocalStorage({ "nda.faithfulDocxRender": "true" })).faithfulDocxRenderEnabled(), true);
  // "0" no longer disables -- only the explicit "false" kill-switch does.
  assert.equal(loadFaithfulModule({}, makeLocalStorage({ "nda.faithfulDocxRender": "0" })).faithfulDocxRenderEnabled(), true);
  assert.equal(loadFaithfulModule({}, makeLocalStorage({ "nda.faithfulDocxRender": "false" })).faithfulDocxRenderEnabled(), false,
    "explicit \"false\" is the kill-switch");
  assert.equal(loadFaithfulModule({}, makeLocalStorage({ "nda.faithfulDocxRender": "FALSE " })).faithfulDocxRenderEnabled(), false,
    "kill-switch is case/space-insensitive");
  // The OLD window flag is inert: with the key absent the default-ON already governs,
  // so the window flag neither enables nor disables -- it is simply ignored.
  assert.equal(loadFaithfulModule({ NDA_FAITHFUL_DOCX_RENDER: false }, makeLocalStorage({})).faithfulDocxRenderEnabled(), true,
    "the removed window flag must be inert (default-ON still governs)");

  // renderChanges:true is in the default options the module passes to docx-preview.
  assert.equal(mod3.faithfulDocxRenderOptions().renderChanges, true,
    "module must request tracked-change rendering");

  console.log("PASS (B) fallback contract: flag_off / library_unavailable / no_bytes / no_container "
    + "all resolve ok:false without throwing; localStorage flag (default ON, \"false\"=kill-switch); window flag inert; renderChanges default true.");
}

// ---------------------------------------------------------------------------
// (C) Empty-body guard: a CSS-only (empty-body) render must NOT falsely pass.
// ---------------------------------------------------------------------------
// docx-preview injects a <style> into the (style)container even when the body is
// empty. The guard must measure VISIBLE content, excluding <style>/<script>, and
// must require a real rendered element -- otherwise the injected CSS text would
// falsely satisfy a textContent check and a blank page would swap over the user's
// content. We drive this with a real jsdom window so the DOM traversal is exercised.
async function testEmptyBodyGuard() {
  const dom = new JSDOM("<!doctype html><html><body></body></html>");
  const { window } = dom;
  const onStore = makeLocalStorage({ "nda.faithfulDocxRender": "1" });
  // Stub a "library" that simulates docx-preview's empty-body behaviour: it injects
  // ONLY a <style> element into the styleContainer (3rd arg) and nothing into the
  // render node. A naive textContent guard would count the CSS and pass.
  window.JSZip = {};
  window.docx = {
    renderAsync: async (_data, _renderNode, styleContainer) => {
      const host = styleContainer || _renderNode;
      const style = window.document.createElement("style");
      style.textContent = ".docx{color:red} /* lots and lots of injected css text */";
      host.appendChild(style);
    },
  };

  const mod = loadFaithfulModule(window, onStore);

  // faithfulDocxVisibleTextLength excludes <style>/<script>.
  const probe = window.document.createElement("div");
  const styleOnly = window.document.createElement("style");
  styleOnly.textContent = "body{color:blue} /* css */";
  probe.appendChild(styleOnly);
  assert.equal(mod.faithfulDocxVisibleTextLength(probe), 0,
    "visible-text length must exclude <style> text");

  // faithfulDocxContainerHasContent: a node holding only a <style> has NO content.
  assert.equal(mod.faithfulDocxContainerHasContent(probe), false,
    "a style-only node must report no content");

  // End-to-end: render an empty body -> ok:false empty_render, container untouched.
  const live = window.document.createElement("div");
  live.appendChild(window.document.createTextNode("EXISTING RECONSTRUCTION"));
  const bytes = new Uint8Array([1, 2, 3]); // any bytes; stub renderAsync ignores them
  const result = await mod.renderFaithfulDocx(live, { bytes });
  assert.equal(result.ok, false, "empty-body render must NOT pass");
  assert.equal(result.reason, "empty_render");
  assert.ok(live.textContent.includes("EXISTING RECONSTRUCTION"),
    "the live container must be left intact (never blanked) on an empty render");

  console.log("PASS (C) empty-body guard: CSS-only render reports empty_render; "
    + "visible-text excludes <style>; live container left intact.");
}

await testTrackedChangesRender();
await testFallbackContract();
await testEmptyBodyGuard();
console.log("\nALL PASS: docx-faithful-render headless validation");
