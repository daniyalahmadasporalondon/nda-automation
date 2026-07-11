// Locks for the "SILENT faithful->reconstruction downgrade" fixes (D3/D9) and the
// view-aware fallback wording (D11) in the REAL classic
// static/js/review-workstation-rendering.js, loaded via vm over a jsdom DOM.
//
//   D3  A dirty in-session draft skips the persisted faithful swap (correct), but a
//       BARE return there is a silent visual revert -- indistinguishable from the
//       "review silently reverted to the original" bug. The fix paints a persistent
//       dirty_draft notice on the reconstruction floor before returning.
//
//   D9  When every faithful fallback candidate fails to render, the reconstruction
//       floor stands -- silently. The fix paints a faithful_unavailable notice (with
//       a retry affordance) so the degrade is explicit.
//
//   D11 The non-read-only fallback notice hardcoded "Tracked redlines couldn't be
//       displayed" even on the CLEAN view (which never shows tracked redlines). The
//       fix threads failedViewMode so the wording matches the view.
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
  console.log("SKIP faithful-reconstruction-notice: jsdom not installed (run `npm install`).");
  process.exit(0);
}

function buildSandbox({
  redlineDraftDirty = false,
  documentViewMode = "redline",
  matter = { id: "m-rn", source_filename: "nda.docx" },
  // "ok" paints faithful content; "fail" resolves { ok:false, reason:"no_bytes" }.
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

  const calls = { render: 0, urls: [] };
  const faithful = {
    enabled: () => true,
    libraryAvailable: () => true,
    ensureLibs: async () => ({}),
    render: async (host, source) => {
      calls.render += 1;
      const url = String(source?.url || "");
      calls.urls.push(url);
      if (renderOutcome(url) === "fail") return { ok: false, reason: "no_bytes" };
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
    reviewDocumentRender: null,
    documentViewMode,
    redlineDraftDirty,
    reviewClauses: [],
    reviewParagraphs: [],
    reviewComments: [],
    selectedReviewClauseId: null,
  };
  const toasts = [];
  const sandbox = {
    console,
    window,
    document: documentRef,
    state,
    studioDocumentRender,
    notificationsController: { notify: (t, s) => toasts.push({ title: String(t || ""), subtitle: String(s || "") }) },
    showStudioDocumentRender: () => {},
  };
  vm.createContext(sandbox);
  for (const file of ["config.js", "review-workstation-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return { sandbox, state, studioDocumentRender, calls };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

// D3: a dirty draft paints the dirty_draft notice and keeps the reconstruction.
async function testDirtyDraftNotice() {
  const { sandbox, studioDocumentRender, calls } = buildSandbox({ redlineDraftDirty: true });
  vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox)("redline");
  await flush();
  await flush();

  assert.equal(calls.render, 0, "dirty draft must not fetch/render the persisted faithful bytes");
  assert.equal(studioDocumentRender.querySelector("[data-faithful-docx]"), null,
    "no faithful surface may swap in while the draft is dirty");
  const notice = studioDocumentRender.querySelector('[data-faithful-reconstruction-notice="dirty_draft"]');
  assert.ok(notice, "D3: a visible dirty_draft downgrade notice must be painted (not a silent return)");
  assert.match(notice.textContent, /unsaved edits/i, "the notice explains the unsaved-edits downgrade");
  assert.match(notice.textContent, /save/i, "the notice tells the reviewer how to restore the faithful view");
  assert.equal(notice.querySelector("[data-faithful-reconstruction-retry]"), null,
    "the dirty_draft notice carries NO retry (saving is the action, not retrying)");
  assert.ok(studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
    "the reconstruction floor still stands under the notice");
  // The notice must NOT masquerade as a faithful surface (clobber-guard keys off it).
  assert.equal(notice.hasAttribute("data-faithful-docx"), false,
    "the notice must not carry data-faithful-docx");
  console.log("PASS (D3) dirty-draft downgrade paints a visible notice; reconstruction stands.");
}

// D9: exhausting every faithful candidate paints the faithful_unavailable notice
// (with a retry) instead of silently keeping the reconstruction.
async function testFaithfulUnavailableNotice() {
  const { sandbox, studioDocumentRender } = buildSandbox({ renderOutcome: () => "fail" });
  vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox)("redline");
  await flush();
  await flush();
  await flush();

  assert.equal(studioDocumentRender.querySelector("[data-faithful-docx]"), null,
    "no faithful surface swaps in when every candidate fails to render");
  const notice = studioDocumentRender.querySelector('[data-faithful-reconstruction-notice="faithful_unavailable"]');
  assert.ok(notice, "D9: an exhausted faithful fallback must paint a visible notice (not degrade silently)");
  assert.match(notice.textContent, /reconstruction/i, "the notice names the reconstruction state");
  assert.ok(notice.querySelector("[data-faithful-reconstruction-retry]"),
    "the faithful_unavailable notice carries a retry affordance");
  assert.ok(studioDocumentRender.textContent.includes("EXISTING RECONSTRUCTION FLOOR"),
    "the reconstruction floor still stands under the notice");
  console.log("PASS (D9) exhausted faithful fallback paints a visible retry notice; reconstruction stands.");
}

// D11: a CLEAN-view fallback notice must NOT say "Tracked redlines couldn't be
// displayed" -- the Clean tab never shows tracked redlines.
async function testCleanViewFallbackWording() {
  // Accepted (clean) reviewed-docx fails; the faithful ORIGINAL source paints, so
  // the fallback surface is "original" with a non-read-only notice.
  const { sandbox, studioDocumentRender } = buildSandbox({
    documentViewMode: "clean",
    renderOutcome: (url) => (url.includes("/reviewed-docx") ? "fail" : "ok"),
  });
  vm.runInContext("maybeUpgradeSurfaceToFaithfulDocx", sandbox)("clean");
  await flush();
  await flush();
  await flush();

  const surface = studioDocumentRender.querySelector("[data-faithful-docx][data-faithful-fallback]");
  assert.ok(surface, "the clean-view fallback must swap in a faithful surface (original floor)");
  assert.equal(surface.getAttribute("data-faithful-fallback"), "original",
    "with the accepted reviewed-docx failing, the original source is the clean-view floor");
  const notice = surface.querySelector("[data-faithful-fallback-notice]");
  assert.ok(notice, "the fallback surface carries its persistent notice");
  assert.doesNotMatch(notice.textContent, /tracked redlines couldn't be displayed/i,
    "D11: the CLEAN-view fallback must NOT claim tracked redlines couldn't be displayed");
  assert.match(notice.textContent, /clean/i, "D11: the CLEAN-view fallback wording names the clean document");
  assert.doesNotMatch(notice.textContent, /tracked-changes/i,
    "D11: the CLEAN-view reason must not describe the doc as tracked-changes");
  console.log("PASS (D11) clean-view fallback wording matches the view (no 'tracked redlines').");
}

await testDirtyDraftNotice();
await testFaithfulUnavailableNotice();
await testCleanViewFallbackWording();
console.log("\nALL PASS: faithful-reconstruction-notice (D3 dirty-draft notice + D9 unavailable notice/retry + D11 clean-view wording).");
