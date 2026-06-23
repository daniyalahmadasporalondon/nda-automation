// Unit lock for the PURE faithful-render selection/precedence function
// selectFaithfulRenderPlan(matter, renderState, capability) and the
// render-state normalization of the working_docx_ready flag, both in the REAL
// classic static/js/review-workstation-rendering.js (loaded via vm).
//
// Proves:
//   * DOCX source + flag ON + library available -> faithful_docx at
//     /api/matters/<id>/source (byte-identical to the shipped T1 behavior).
//   * Flag OFF or library missing -> page_image (no-op; never blanks).
//   * PDF source is INERT: it stays page_image UNTIL renderState.workingDocxReady
//     is true, then -> faithful_docx at /api/matters/<id>/working-docx.
//   * normalizeReviewDocumentRender reads working_docx_ready (snake) /
//     workingDocxReady (camel); absent => undefined (falsy), i.e. dormant.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const STATIC_JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../static/js");

function loadModule() {
  const sandbox = { console, window: {}, document: { createElement: () => ({}) }, state: { selectedMatter: null, reviewDocumentRender: null }, studioDocumentRender: null };
  vm.createContext(sandbox);
  for (const file of ["config.js", "review-workstation-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return {
    selectFaithfulRenderPlan: vm.runInContext("selectFaithfulRenderPlan", sandbox),
    normalizeReviewDocumentRender: vm.runInContext("normalizeReviewDocumentRender", sandbox),
  };
}

const { selectFaithfulRenderPlan, normalizeReviewDocumentRender } = loadModule();

const ON = { flagEnabled: true, libraryAvailable: true };
const docxMatter = { id: "m-docx", source_filename: "nda.docx" };
const pdfMatter = { id: "m-pdf", source_filename: "nda.pdf" };

// DOCX source, capability ON -> faithful_docx at /source (byte-identical to T1).
{
  const plan = selectFaithfulRenderPlan(docxMatter, null, ON);
  assert.equal(plan.render, "faithful_docx");
  assert.equal(plan.url, "/api/matters/m-docx/source");
}

// Flag OFF -> page_image (no-op). Library missing -> page_image. No matter id -> page_image.
assert.equal(selectFaithfulRenderPlan(docxMatter, null, { flagEnabled: false, libraryAvailable: true }).render, "page_image");
assert.equal(selectFaithfulRenderPlan(docxMatter, null, { flagEnabled: true, libraryAvailable: false }).render, "page_image");
assert.equal(selectFaithfulRenderPlan({ source_filename: "nda.docx" }, null, ON).render, "page_image");
assert.equal(selectFaithfulRenderPlan(null, null, ON).render, "page_image");

// PDF source is INERT until workingDocxReady === true.
assert.equal(selectFaithfulRenderPlan(pdfMatter, null, ON).render, "page_image", "PDF must be dormant with no render-state");
assert.equal(selectFaithfulRenderPlan(pdfMatter, {}, ON).render, "page_image", "PDF must be dormant when flag absent");
assert.equal(selectFaithfulRenderPlan(pdfMatter, { workingDocxReady: false }, ON).render, "page_image", "PDF must be dormant when flag false");

// PDF source, capability ON, workingDocxReady true -> faithful_docx at /working-docx.
{
  const plan = selectFaithfulRenderPlan(pdfMatter, { workingDocxReady: true }, ON);
  assert.equal(plan.render, "faithful_docx");
  assert.equal(plan.url, "/api/matters/m-pdf/working-docx");
}

// AUTO-ON (Approach C retro-conversion): a PDF matter with a working DOCX PREFERS the
// faithful render even when the off-by-default nda.faithfulDocxRender flag is OFF -- the
// converted matter's anchors only bind on the faithful DOCX surface. The library must
// still be available (rendering it needs the vendored lib).
{
  const plan = selectFaithfulRenderPlan(
    pdfMatter, { workingDocxReady: true }, { flagEnabled: false, libraryAvailable: true },
  );
  assert.equal(plan.render, "faithful_docx", "PDF + workingDocxReady auto-ons faithful regardless of flag");
  assert.equal(plan.url, "/api/matters/m-pdf/working-docx");
}
// Library missing still gates the auto-on (cannot render faithful without the lib).
assert.equal(
  selectFaithfulRenderPlan(pdfMatter, { workingDocxReady: true }, { flagEnabled: false, libraryAvailable: false }).render,
  "page_image",
);
// The flag default still governs matters WITHOUT a working DOCX: a DOCX source with the
// flag OFF stays page_image (auto-on is PDF+workingDocxReady only).
assert.equal(
  selectFaithfulRenderPlan(docxMatter, null, { flagEnabled: false, libraryAvailable: true }).render,
  "page_image",
  "DOCX source keeps the flag default (no auto-on)",
);
// And a PDF source WITHOUT a working DOCX with the flag OFF stays page_image.
assert.equal(
  selectFaithfulRenderPlan(pdfMatter, { workingDocxReady: false }, { flagEnabled: false, libraryAvailable: true }).render,
  "page_image",
  "PDF without a working DOCX keeps the flag default (no auto-on)",
);

// normalizeReviewDocumentRender: working_docx_ready (snake) -> workingDocxReady true.
{
  const rs = normalizeReviewDocumentRender({ status: "ready", pdf_url: "/api/matters/m/source", working_docx_ready: true });
  assert.equal(rs.workingDocxReady, true);
}
// camelCase variant also read.
{
  const rs = normalizeReviewDocumentRender({ status: "ready", pdf_url: "/api/matters/m/source", workingDocxReady: true });
  assert.equal(rs.workingDocxReady, true);
}
// Absent -> undefined (falsy), i.e. the PDF branch stays dormant.
{
  const rs = normalizeReviewDocumentRender({ status: "ready", pdf_url: "/api/matters/m/source" });
  assert.equal(rs.workingDocxReady, undefined);
}
// Explicit false -> not set (falsy).
{
  const rs = normalizeReviewDocumentRender({ status: "ready", pdf_url: "/api/matters/m/source", working_docx_ready: false });
  assert.ok(!rs.workingDocxReady);
}

// ---------------------------------------------------------------------------
// Fallback-matrix plan selection (per-view).
// ---------------------------------------------------------------------------
// native DOCX, REDLINE view -> faithful_docx at the TRACKED reviewed-docx URL.
{
  const plan = selectFaithfulRenderPlan(docxMatter, null, ON, "redline");
  assert.equal(plan.render, "faithful_docx", "native DOCX redline -> faithful surface");
  assert.equal(plan.url, "/api/matters/m-docx/reviewed-docx?changes=tracked");
}
// native DOCX, CLEAN view -> faithful_docx at the ACCEPTED reviewed-docx URL.
{
  const plan = selectFaithfulRenderPlan(docxMatter, null, ON, "clean");
  assert.equal(plan.render, "faithful_docx", "native DOCX clean -> faithful surface");
  assert.equal(plan.url, "/api/matters/m-docx/reviewed-docx?changes=accepted");
}
// PDF-converted matter (workingDocxReady) -> faithful surface selected from the
// converted DOCX, for the Original view AND the redline view (composed onto the
// working DOCX), even with the off-by-default flag (auto-on).
{
  const original = selectFaithfulRenderPlan(pdfMatter, { workingDocxReady: true }, { flagEnabled: false, libraryAvailable: true });
  assert.equal(original.render, "faithful_docx", "converted PDF original -> faithful (from working DOCX)");
  assert.equal(original.url, "/api/matters/m-pdf/working-docx");

  const redline = selectFaithfulRenderPlan(pdfMatter, { workingDocxReady: true }, ON, "redline");
  assert.equal(redline.render, "faithful_docx", "converted PDF redline -> faithful (composed reviewed-docx)");
  assert.equal(redline.url, "/api/matters/m-pdf/reviewed-docx?changes=tracked");
}
// A PDF source WITHOUT a working DOCX, redline view, flag ON: not eligible for the
// reviewed-docx compose -> reconstruction (no faithful DOCX bytes exist at all).
{
  const plan = selectFaithfulRenderPlan(pdfMatter, { workingDocxReady: false }, ON, "redline");
  assert.equal(plan.render, "reconstruction",
    "PDF redline without a working DOCX has no faithful bytes -> reconstruction");
}

console.log("faithful-render-plan: all assertions passed "
  + "(DOCX->/source; flag/library gate; PDF inert until working_docx_ready; working-docx URL; "
  + "redline/clean reviewed-docx URLs; converted-PDF faithful matrix).");
