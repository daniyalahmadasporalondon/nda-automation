// Locks for two flag-gated bugs in the faithful redline/clean upgrade path
// (maybeUpgradeSurfaceToFaithfulDocx) in the REAL classic
// static/js/review-workstation-rendering.js, loaded via vm over a jsdom DOM.
//
//   #2  CLEAN view must render ACCEPTED text, not tracked-change markup. The render
//       call used to pass NO options, so docx-preview's renderChanges:true default
//       was forced even for Clean -> <ins>/<del> markup in a view that should show
//       accepted text. The fix passes { renderChanges:false } for the Clean view
//       (belt-and-suspenders on top of the backend serving accepted bytes). The
//       Redline view keeps the default (renderChanges ON).
//
//   #3  The faithful surface must NOT overwrite unsaved in-session edits. The
//       upgrade re-fetches /reviewed-docx (persisted bytes built from persisted
//       reviewer_decisions) on EVERY redline/clean render with no dirty guard, so a
//       stale persisted surface could swap in OVER a correct in-session
//       reconstruction -- hiding the user's live unsaved edit (export still sends
//       the live state). The fix adds a redlineDraftDirty guard: while the draft is
//       dirty, keep the live reconstruction and do not fetch/swap the persisted
//       faithful surface. Never blank.
//
// jsdom is a devDependency; SKIPS cleanly when absent (CI parity).

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
  console.log("SKIP faithful-redline-clean-upgrade: jsdom not installed (run `npm install`).");
  process.exit(0);
}

// Build a sandbox with the REAL scripts over a jsdom DOM. The faithful bridge is a
// fake whose render() captures the options it is called with, and whose libs are
// already "loaded" (libraryAvailable true) so the upgrade reaches the render call
// synchronously. Cross-file glue the upgrade touches is stubbed minimally.
function buildSandbox({
  redlineDraftDirty = false,
  matter = { id: "m-rc", source_filename: "nda.docx" },
  reviewDocumentRender = null,
  // Optional per-URL render outcome override. Receives the requested URL and returns
  // either "ok" (paints faithful content), "fail" (resolves { ok:false }), or
  // an explicit { ok, reason }. Defaults to always-ok (the legacy behavior).
  renderOutcome = () => "ok",
} = {}) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>");
  const { window } = dom;
  const documentRef = window.document;

  const studioDocumentRender = documentRef.createElement("div");
  const reconstruction = documentRef.createElement("section");
  reconstruction.className = "review-reconstruction-surface";
  reconstruction.textContent = "EXISTING RECONSTRUCTION FLOOR";
  studioDocumentRender.appendChild(reconstruction);
  documentRef.body.appendChild(studioDocumentRender);

  const calls = { render: 0, lastOptions: undefined, lastSource: undefined, urls: [] };
  const faithful = {
    enabled: () => true,
    libraryAvailable: () => true, // libs already loaded: reach the render call directly
    ensureLibs: async () => ({}),
    render: async (host, source, options) => {
      calls.render += 1;
      calls.lastOptions = options;
      calls.lastSource = source;
      const url = String(source?.url || "");
      calls.urls.push(url);
      let outcome = renderOutcome(url);
      if (outcome && typeof outcome === "object") outcome = outcome.ok ? "ok" : "fail";
      if (outcome === "fail") return { ok: false, reason: "no_bytes" };
      const docNode = documentRef.createElement("div");
      docNode.className = "docx";
      docNode.textContent = "FAITHFUL CONTENT";
      host.appendChild(docNode);
      return { ok: true };
    },
  };
  window.FaithfulDocxRender = faithful;

  const state = {
    selectedMatter: matter,
    reviewDocumentRender,
    documentViewMode: "redline",
    redlineDraftDirty,
    // Empty review model so the real surface-level binders the upgrade re-runs
    // (highlightSelectedClauseRefs) find no clauses and no-op cleanly.
    reviewClauses: [],
    selectedReviewClauseId: null,
  };

  const sandbox = {
    console,
    window,
    document: documentRef,
    state,
    studioDocumentRender,
    // showStudioDocumentRender lives in another classic file (not loaded here); stub
    // it so the upgrade's reveal call is a no-op. The other surface binders
    // (bindFaithfulDocxInteractions / notifyFillHighlights / highlightSelectedClauseRefs)
    // are REAL functions in review-workstation-rendering.js and run as shipped --
    // the empty review model above makes them no-op cleanly.
    showStudioDocumentRender: () => {},
  };
  vm.createContext(sandbox);
  for (const file of ["config.js", "review-workstation-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return { sandbox, state, studioDocumentRender, calls, documentRef };
}

function flushMicrotasks() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

// ---------------------------------------------------------------------------
// (#2) CLEAN view passes renderChanges:false; REDLINE keeps the default (ON).
// ---------------------------------------------------------------------------
async function testCleanRendersAcceptedText() {
  // CLEAN
  {
    const { sandbox, state, calls } = buildSandbox();
    state.documentViewMode = "clean";
    const upgrade = vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox);
    upgrade("clean");
    await flushMicrotasks();
    await flushMicrotasks();
    assert.equal(calls.render, 1, "clean upgrade must call faithful.render");
    assert.ok(calls.lastOptions && typeof calls.lastOptions === "object",
      "clean upgrade must pass an options object to render");
    assert.equal(calls.lastOptions.renderChanges, false,
      "CLEAN view must pass renderChanges:false so accepted text renders (no <ins>/<del> markup)");
    assert.ok(String(calls.lastSource?.url || "").includes("changes=accepted"),
      "clean upgrade must fetch the accepted-changes reviewed-docx");
  }

  // REDLINE keeps the tracked-change default (renderChanges is NOT forced off).
  {
    const { sandbox, state, calls } = buildSandbox();
    state.documentViewMode = "redline";
    const upgrade = vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox);
    upgrade("redline");
    await flushMicrotasks();
    await flushMicrotasks();
    assert.equal(calls.render, 1, "redline upgrade must call faithful.render");
    // Redline passes undefined options so faithfulDocxRenderOptions() keeps its
    // renderChanges:true default -- it must NOT force renderChanges off.
    assert.ok(
      !calls.lastOptions || calls.lastOptions.renderChanges !== false,
      "REDLINE view must keep tracked-change rendering (renderChanges not forced off)",
    );
    assert.ok(String(calls.lastSource?.url || "").includes("changes=tracked"),
      "redline upgrade must fetch the tracked-changes reviewed-docx");
  }

  console.log("PASS (#2) clean/redline options: CLEAN forces renderChanges:false (accepted text); "
    + "REDLINE keeps tracked-change default.");
}

// ---------------------------------------------------------------------------
// (#3) A dirty in-session draft is NOT overwritten by the faithful re-fetch.
// ---------------------------------------------------------------------------
async function testDirtyDraftNotOverwritten() {
  const { sandbox, studioDocumentRender, calls } = buildSandbox({ redlineDraftDirty: true });
  const upgrade = vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox);

  upgrade("redline");
  await flushMicrotasks();
  await flushMicrotasks();

  // With unsaved edits, the upgrade must NOT fetch/render/swap the persisted bytes.
  assert.equal(calls.render, 0,
    "a dirty in-session draft must NOT trigger a faithful re-fetch (it would overwrite live edits)");
  assert.equal(studioDocumentRender.querySelector("[data-faithful-docx]"), null,
    "no faithful surface must be swapped in while the draft is dirty");
  assert.ok(studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
    "the live reconstruction (carrying the in-session edit) must remain (never overwritten, never blank)");

  // Sanity: once the draft is saved (not dirty), the faithful surface DOES re-engage.
  const clean = buildSandbox({ redlineDraftDirty: false });
  const upgrade2 = vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", clean.sandbox);
  upgrade2("redline");
  await flushMicrotasks();
  await flushMicrotasks();
  assert.equal(clean.calls.render, 1,
    "after the draft is saved (not dirty) the faithful surface re-engages");

  console.log("PASS (#3) dirty-draft guard: unsaved in-session edit is NOT overwritten by the "
    + "persisted faithful re-fetch (reconstruction stands); re-engages once saved.");
}

// ---------------------------------------------------------------------------
// (#4) REDLINE 409/error -> FAITHFUL fallback (Clean/Original), NOT the plain
//      reconstruction. The tracked reviewed-docx fails (409 no-artifact); the
//      surface must resolve to a faithful read-only document with an honest note,
//      and the plain reconstruction must NOT be the final surface.
// ---------------------------------------------------------------------------
async function testRedline409FallsBackToFaithful() {
  // The tracked reviewed-docx (changes=tracked) fails; the accepted (clean) AND the
  // source bytes still paint -> the fallback should pick the FIRST that paints (clean).
  {
    const { sandbox, studioDocumentRender, calls } = buildSandbox({
      renderOutcome: (url) => (url.includes("changes=tracked") ? "fail" : "ok"),
    });
    const upgrade = vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox);
    upgrade("redline");
    await flushMicrotasks();
    await flushMicrotasks();
    await flushMicrotasks();

    const surface = studioDocumentRender.querySelector("[data-faithful-docx]");
    assert.ok(surface, "a FAITHFUL surface (not the plain reconstruction) must be swapped in on redline 409");
    assert.ok(surface.hasAttribute("data-faithful-fallback"),
      "the swapped-in surface must be the faithful FALLBACK (clean/original), tagged data-faithful-fallback");
    assert.equal(surface.getAttribute("data-faithful-fallback"), "clean",
      "the first painting fallback candidate (accepted/clean) must win");
    assert.ok(studioDocumentRender.querySelector(".review-faithful-fallback-note"),
      "an honest note must explain tracked redlines live on the Clean/Original tabs");
    assert.ok(!studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
      "the plain reconstruction must NOT be the final surface (faithful fallback replaces it)");
    // tracked (failed primary) + accepted (clean fallback) were both requested.
    assert.ok(calls.urls.some((u) => u.includes("changes=tracked")), "must have tried tracked first");
    assert.ok(calls.urls.some((u) => u.includes("changes=accepted")), "must have fallen back to accepted/clean");
  }

  // Tracked AND accepted both fail (no reviewed artifact at all) -> the faithful
  // ORIGINAL /source is the floor; it paints, so the surface is faithful (original).
  {
    const { sandbox, studioDocumentRender } = buildSandbox({
      renderOutcome: (url) => (url.includes("/reviewed-docx") ? "fail" : "ok"),
    });
    const upgrade = vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox);
    upgrade("redline");
    await flushMicrotasks();
    await flushMicrotasks();
    await flushMicrotasks();

    const surface = studioDocumentRender.querySelector("[data-faithful-docx][data-faithful-fallback]");
    assert.ok(surface, "with no reviewed artifact, the faithful ORIGINAL source must be the fallback floor");
    assert.equal(surface.getAttribute("data-faithful-fallback"), "original",
      "the original source document must be the fallback when no reviewed bytes exist");
    assert.ok(!studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
      "the faithful original replaces the plain reconstruction");
  }

  console.log("PASS (#4) redline 409 -> faithful fallback: clean wins when accepted paints; "
    + "original is the floor when no reviewed artifact exists; never the plain reconstruction.");
}

// ---------------------------------------------------------------------------
// (#5) NO docx bytes at all (every faithful render fails) -> the plain
//      reconstruction stands (never blank). This is the only case that keeps the
//      reconstruction.
// ---------------------------------------------------------------------------
async function testNoDocxBytesKeepsReconstruction() {
  const { sandbox, studioDocumentRender } = buildSandbox({
    renderOutcome: () => "fail", // tracked, accepted, AND source all yield no bytes.
  });
  const upgrade = vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox);
  upgrade("redline");
  await flushMicrotasks();
  await flushMicrotasks();
  await flushMicrotasks();

  assert.equal(studioDocumentRender.querySelector("[data-faithful-docx]"), null,
    "no faithful surface must be swapped in when there are no DOCX bytes at all");
  assert.ok(studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
    "the never-blank reconstruction floor must stand when even the faithful original is unavailable");

  console.log("PASS (#5) no docx bytes: reconstruction floor stands (never blank) when even faithful original fails.");
}

await testCleanRendersAcceptedText();
await testDirtyDraftNotOverwritten();
await testRedline409FallsBackToFaithful();
await testNoDocxBytesKeepsReconstruction();
console.log("\nALL PASS: faithful-redline-clean-upgrade (#2 clean accepted-text + #3 dirty-draft guard "
  + "+ #4 redline-409 faithful fallback + #5 no-bytes reconstruction floor).");
