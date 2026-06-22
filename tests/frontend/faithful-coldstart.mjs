// COLD-START engagement lock for the faithful-DOCX render path.
//
// THE BUG THIS GUARDS (was real, verified by driving the app): the plan gate
// selectFaithfulRenderPlan() requires capability.libraryAvailable === true
// SYNCHRONOUSLY, but the docx-preview vendor libs are LAZY-LOADED -- they only
// inject inside renderFaithfulDocx -> ensureFaithfulDocxLibs, which the gate would
// never let run on a cold page. So on a fresh load window.docx stayed unset, the
// plan stayed page_image/reconstruction, and the faithful upgrade NEVER engaged.
// The whole Phase 1+2 faithful feature was dormant in the real app even though the
// vendored scripts serve fine (HTTP 200). The PRE-EXISTING tests all missed this
// because they PRE-INJECT window.docx, bypassing the cold-start gate entirely.
//
// This test deliberately does NOT pre-inject the library. It simulates the cold
// page -- libraryAvailable() starts FALSE and only flips TRUE after the lazy
// ensureLibs() resolves -- and drives the REAL classic
// maybeUpgradeOriginalSurfaceToFaithfulDocx() (loaded via vm from the shipped
// static/js/review-workstation-rendering.js, no mocks of the code under test).
//
// Proves:
//   (1) COLD START ENGAGES: the first synchronous pass paints NO faithful surface
//       (lib not loaded -> plan page_image), but the fix kicks the lazy-load and,
//       once it resolves, the faithful docx-preview surface ACTUALLY RENDERS into
//       studioDocumentRender (data-faithful-docx present) -- not the reconstruction.
//   (2) LOAD FAILURE -> RECONSTRUCTION (never blank): when the lazy-load rejects,
//       no faithful surface is painted and the pre-painted reconstruction floor is
//       left exactly intact (the pane is never blanked).
//
// jsdom is a devDependency; the test SKIPS cleanly when it is absent (CI parity
// with the other faithful tests), because the upgrade function does real DOM work.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC_JS_DIR = path.join(HERE, "../../static/js");

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
  console.log("SKIP faithful-coldstart: jsdom not installed (run `npm install`). "
    + "The cold-start upgrade does real DOM work; the browser has a DOM.");
  process.exit(0);
}

// Build a vm sandbox that loads the REAL config.js + review-workstation-rendering.js
// over a jsdom DOM, with stubs ONLY for the cross-file glue the Original upgrade
// touches (showStudioDocumentRender lives in another classic file). The faithful
// bridge (window.FaithfulDocxRender) is the COLD-START fake described above.
function buildSandbox({ ensureLibsFails }) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>");
  const { window } = dom;
  const documentRef = window.document;

  // The live render node the upgrade swaps the faithful surface into. Pre-paint a
  // RECONSTRUCTION so we can prove the never-blank floor survives a load failure.
  const studioDocumentRender = documentRef.createElement("div");
  studioDocumentRender.setAttribute("data-review-render-surface", "");
  const reconstruction = documentRef.createElement("section");
  reconstruction.className = "review-reconstruction-surface";
  reconstruction.textContent = "EXISTING RECONSTRUCTION FLOOR";
  studioDocumentRender.appendChild(reconstruction);
  documentRef.body.appendChild(studioDocumentRender);

  // COLD-START faithful bridge. libraryAvailable() starts FALSE (cold page) and only
  // flips TRUE once ensureLibs() resolves -- exactly the lazy-load timeline. render()
  // simulates docx-preview painting real content; it is only ever reachable AFTER the
  // libs "load", proving the gate let the lazy path run.
  const libState = { loaded: false };
  let ensureLibsCalls = 0;
  let renderCalls = 0;
  const faithful = {
    enabled: () => true,
    libraryAvailable: () => libState.loaded,
    ensureLibs: async () => {
      ensureLibsCalls += 1;
      if (ensureLibsFails) {
        throw new Error("simulated lazy-load failure (vendored script 404 / offline)");
      }
      libState.loaded = true; // the lazy <script> injection populated window.docx
      return {};
    },
    render: async (host) => {
      renderCalls += 1;
      // docx-preview emits a top-level .docx node with real text. Mirror that so the
      // upgrade's never-blank content check (in the browser) would be satisfied; here
      // we assert on the surface the upgrade itself builds.
      const docNode = documentRef.createElement("div");
      docNode.className = "docx";
      docNode.textContent = "FAITHFUL DOCX-PREVIEW CONTENT";
      host.appendChild(docNode);
      return { ok: true };
    },
  };
  window.FaithfulDocxRender = faithful;

  const state = {
    selectedMatter: { id: "m-cold", source_filename: "nda.docx" },
    reviewDocumentRender: null,
    documentViewMode: "original",
  };

  const sandbox = {
    console,
    window,
    document: documentRef,
    state,
    studioDocumentRender,
    // Cross-file glue the Original upgrade calls; a no-op here (its only job is to
    // reveal the pane, which jsdom does not need for these assertions).
    showStudioDocumentRender: () => {},
  };
  vm.createContext(sandbox);
  for (const file of ["config.js", "review-workstation-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return {
    sandbox,
    studioDocumentRender,
    counters: { ensureLibs: () => ensureLibsCalls, render: () => renderCalls },
  };
}

// Resolves after the microtask chain (ensureLibs -> reupgrade -> render) drains.
function flushMicrotasks() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

// ---------------------------------------------------------------------------
// (1) COLD START ENGAGES the faithful surface once the lazy-load resolves.
// ---------------------------------------------------------------------------
async function testColdStartEngages() {
  const { sandbox, studioDocumentRender, counters } = buildSandbox({ ensureLibsFails: false });
  const upgrade = vm.runInContext("maybeUpgradeOriginalSurfaceToFaithfulDocx", sandbox);

  // FIRST synchronous pass: library is NOT loaded yet, so the plan is page_image and
  // NO faithful surface is painted. The reconstruction floor is still in place. This
  // is the exact state the OLD code froze in forever.
  upgrade();
  assert.equal(
    studioDocumentRender.querySelector("[data-faithful-docx]"),
    null,
    "cold first pass must NOT have painted a faithful surface (lib not loaded yet)",
  );
  assert.ok(
    studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
    "reconstruction floor must still be present after the cold first pass",
  );

  // The lazy-load is kicked off the cold pass and the re-upgrade + render follows on
  // the microtask chain; let it drain.
  await flushMicrotasks();
  await flushMicrotasks();
  assert.equal(counters.ensureLibs(), 1, "the fix must have KICKED the lazy-load from the cold pass");

  // THE FIX: once the lazy-load resolved, the re-invoked upgrade saw libraryAvailable()
  // === true, the plan engaged faithful_docx, and the real docx-preview surface was
  // rendered into the live node -- replacing the reconstruction.
  const faithfulSurface = studioDocumentRender.querySelector("[data-faithful-docx]");
  assert.ok(faithfulSurface, "after the lazy-load resolves the faithful surface MUST engage (this was the bug)");
  assert.ok(
    studioDocumentRender.textContent.includes("FAITHFUL DOCX-PREVIEW CONTENT"),
    "the engaged surface must hold the real docx-preview content, not the reconstruction",
  );
  assert.ok(
    !studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
    "the faithful surface should have replaced the reconstruction floor",
  );
  assert.equal(counters.render(), 1, "faithful.render() must have run exactly once on the re-upgrade");

  console.log("PASS (1) cold start: first pass no-op (lib unloaded) -> lazy-load kicked -> "
    + "faithful surface ENGAGES with real docx-preview content (the dormant-feature bug is fixed).");
}

// ---------------------------------------------------------------------------
// (2) LOAD FAILURE degrades to the reconstruction (NEVER blank).
// ---------------------------------------------------------------------------
async function testLoadFailureFallsBackNeverBlank() {
  const { sandbox, studioDocumentRender, counters } = buildSandbox({ ensureLibsFails: true });
  const upgrade = vm.runInContext("maybeUpgradeOriginalSurfaceToFaithfulDocx", sandbox);

  upgrade();

  await flushMicrotasks();
  await flushMicrotasks();
  assert.equal(counters.ensureLibs(), 1, "a failing lazy-load is still attempted from the cold pass");

  // The lazy-load rejected: no re-upgrade, no faithful surface, render() never called,
  // and -- critically -- the pre-painted reconstruction is left EXACTLY intact. Never blank.
  assert.equal(
    studioDocumentRender.querySelector("[data-faithful-docx]"),
    null,
    "a failed lazy-load must NOT paint a faithful surface",
  );
  assert.equal(counters.render(), 0, "faithful.render() must NEVER run when the lazy-load fails");
  assert.ok(
    studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
    "the reconstruction floor MUST survive a lazy-load failure (never blank)",
  );
  assert.ok(studioDocumentRender.textContent.trim().length > 0, "the pane must never be blank");

  console.log("PASS (2) load failure: lazy-load rejects -> no faithful surface, render() never runs, "
    + "reconstruction floor intact (never blank).");
}

await testColdStartEngages();
await testLoadFailureFallsBackNeverBlank();
console.log("\nALL PASS: faithful-coldstart (lazy-load engagement + never-blank fallback).");
