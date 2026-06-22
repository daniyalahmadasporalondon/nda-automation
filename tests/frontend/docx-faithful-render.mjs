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
// (B) Shipped module never-blank fallback contract
// ---------------------------------------------------------------------------
function loadFaithfulModule(windowStub) {
  const sandbox = { window: windowStub, document: windowStub.document, console, module: { exports: {} }, fetch: undefined };
  sandbox.Blob = typeof Blob !== "undefined" ? Blob : undefined;
  sandbox.ArrayBuffer = ArrayBuffer;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(MODULE_FILE, "utf8"), sandbox, { filename: "docx-faithful-render.js" });
  return sandbox.module.exports;
}

async function testFallbackContract() {
  // Flag OFF (no window flag) -> { ok:false, reason:"flag_off" }, never throws.
  const win1 = { document: { createElement: () => ({}) } };
  const mod1 = loadFaithfulModule(win1);
  const r1 = await mod1.renderFaithfulDocx({ innerHTML: "", childElementCount: 0, textContent: "" }, { url: "/x" });
  assert.equal(r1.ok, false, "flag OFF must resolve ok:false");
  assert.equal(r1.reason, "flag_off");

  // Flag ON but library unavailable (no window.docx) -> ok:false library_unavailable.
  const win2 = { NDA_FAITHFUL_DOCX_RENDER: true, document: { createElement: () => ({}) } };
  const mod2 = loadFaithfulModule(win2);
  const r2 = await mod2.renderFaithfulDocx({ innerHTML: "", childElementCount: 0, textContent: "" }, { url: "/x" });
  assert.equal(r2.ok, false, "library unavailable must resolve ok:false");
  assert.equal(r2.reason, "library_unavailable");

  // Flag ON, library present, but no bytes / no url -> ok:false no_bytes (no throw).
  const win3 = {
    NDA_FAITHFUL_DOCX_RENDER: "true",
    JSZip: {},
    docx: { renderAsync: async () => {} },
    document: { createElement: () => ({}) },
  };
  const mod3 = loadFaithfulModule(win3);
  const r3 = await mod3.renderFaithfulDocx({ innerHTML: "", childElementCount: 0, textContent: "" }, {});
  assert.equal(r3.ok, false, "no bytes must resolve ok:false");
  assert.equal(r3.reason, "no_bytes");

  // No container -> ok:false no_container.
  const r4 = await mod3.renderFaithfulDocx(null, { url: "/x" });
  assert.equal(r4.ok, false);
  assert.equal(r4.reason, "no_container");

  // Flag string parsing: enabled() honours "1"/"true"/"on"/"yes", rejects "0"/"".
  assert.equal(loadFaithfulModule({ NDA_FAITHFUL_DOCX_RENDER: "on", document: {} }).faithfulDocxRenderEnabled(), true);
  assert.equal(loadFaithfulModule({ NDA_FAITHFUL_DOCX_RENDER: "0", document: {} }).faithfulDocxRenderEnabled(), false);
  assert.equal(loadFaithfulModule({ document: {} }).faithfulDocxRenderEnabled(), false, "default must be OFF");

  // renderChanges:true is in the default options the module passes to docx-preview.
  assert.equal(mod3.faithfulDocxRenderOptions().renderChanges, true,
    "module must request tracked-change rendering");

  console.log("PASS (B) fallback contract: flag_off / library_unavailable / no_bytes / no_container "
    + "all resolve ok:false without throwing; default flag OFF; renderChanges default true.");
}

await testTrackedChangesRender();
await testFallbackContract();
console.log("\nALL PASS: docx-faithful-render headless validation");
