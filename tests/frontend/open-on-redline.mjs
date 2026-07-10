// Regression lock for "a reviewed matter must OPEN on the Redline view".
//
// Pre-fix behavior: every PDF-source matter opened on the ORIGINAL view no
// matter its review state, because the initial mode (renderResult ->
// defaultDocumentViewModeForReviewResult) was dominated by the PDF
// sourceFallback render candidate (sourcePdfRenderCandidate ->
// normalizeReviewDocumentRender sets sourceFallback:true), and the redline
// signals were invisible to the decision: the saved draft is applied by
// applyMatterRedlineDraft AFTER renderResult computed the mode, and that
// function never touches documentViewMode.
//
// Fix under test: defaultDocumentViewModeForReviewResult now takes the redline
// signals (result.redline_edits + the matter's saved redline_draft, threaded
// from loadMatterIntoReview through renderResult's options) and prefers
// VIEW_MODE_REDLINE when redline work exists, BEFORE consulting the
// sourceFallback/fidelity preference for Original.
//
// This drives the REAL renderResult + defaultDocumentViewModeForReviewResult
// (whole-module vm load, same trick as pdf-source-render-gate.mjs) with the
// heavy collaborators stubbed AFTER load, and proves:
//   (a) PDF matter + review result WITH redline edits -> initial mode redline;
//   (b) PDF matter + saved redline_draft but a result WITHOUT edits -> redline;
//   (c) PDF matter with NO review edits and NO draft -> original (preserved);
//   (d) DOCX-source reviewed matter -> redline (preserved, no regression);
// plus the call-site wiring in loadMatterIntoReview (viewer.js) that threads
// matter.redline_draft into renderResult.
//
// Run: node tests/frontend/open-on-redline.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

// Build a fresh sandbox carrying the REAL config + rendering modules. The
// matter is seeded as state.selectedMatter so the REAL render-candidate chain
// (reviewDocumentRenderState -> sourcePdfRenderCandidate ->
// normalizeReviewDocumentRender) produces the exact sourceFallback state a
// repository open produces -- we do not hand-craft the render state.
function makeSandbox(matter) {
  const sandbox = {
    console,
    window: {}, // no RedlineEditContract / ReviewWorkstationModel: fail-open paths
    studioNdaText: { value: "" },
    state: {
      selectedMatter: matter,
    },
    // Declared in viewer.js / actions.js, not in the loaded module:
    resetReviewEditHistory: () => {},
    clauseStatus: () => ({ requiresAttention: false }),
  };
  vm.createContext(sandbox);
  for (const file of ["config.js", "review-workstation-rendering.js"]) {
    vm.runInContext(read(path.join("static/js", file)), sandbox, { filename: file });
  }
  // These ARE function declarations inside the loaded module (so a pre-load
  // sandbox stub would be shadowed); reassign the bindings after load to keep
  // the test side-effect free (no DOM, no fetch).
  vm.runInContext(
    "renderStudioResult = () => {}; updateExportButtonState = () => {}; requestMatterDocumentRenderPreview = () => {};",
    sandbox,
  );
  return sandbox;
}

function initialModeAfterOpen(matter, result, options) {
  const sandbox = makeSandbox(matter);
  sandbox.__result = result;
  sandbox.__options = options;
  vm.runInContext('renderResult(__result, "reviewed text", __options)', sandbox);
  return sandbox.state.documentViewMode;
}

const REDLINE_EDIT = {
  id: "edit-1",
  clause_id: "confidentiality",
  paragraph_id: "p-1",
  action: "replace",
};

// (a) PDF-source matter whose completed review produced redline edits must OPEN
// on the Redline view (pre-fix: sourceFallback forced Original).
const pdfMatter = { id: "m1", source_filename: "counterparty-nda.pdf" };
assert.equal(
  initialModeAfterOpen(pdfMatter, { clauses: [], paragraphs: [], redline_edits: [REDLINE_EDIT] }, {}),
  "redline",
  "PDF matter with review redline edits must open on the redline view",
);

// (b) PDF-source matter with a SAVED redline draft but a result without edits
// (e.g. manual-redline work only) must also open on Redline. The draft rides in
// through renderResult's options because applyMatterRedlineDraft runs AFTER the
// mode is computed.
assert.equal(
  initialModeAfterOpen(
    { id: "m2", source_filename: "draft-nda.pdf" },
    { clauses: [], paragraphs: [], redline_edits: [] },
    { redlineDraft: { manual_redline_edits: [{ paragraph_id: "p-3" }] } },
  ),
  "redline",
  "PDF matter with a saved redline draft must open on the redline view",
);

// (c) UNREVIEWED PDF matter (no edits, no draft) keeps opening on Original --
// the guaranteed-faithful surface stays the default when there is no redline
// work to show.
assert.equal(
  initialModeAfterOpen(
    { id: "m3", source_filename: "fresh-inbound.pdf" },
    { clauses: [], paragraphs: [], redline_edits: [] },
    {},
  ),
  "original",
  "PDF matter without review redlines or a draft must keep opening on the original view",
);
// Same without any options object at all (the pasted-text funnel calls
// renderResult with two args).
assert.equal(
  initialModeAfterOpen(
    { id: "m3b", source_filename: "fresh-inbound.pdf" },
    { clauses: [], paragraphs: [], redline_edits: [] },
    undefined,
  ),
  "original",
  "two-arg renderResult call must preserve the original-view default for an unreviewed PDF",
);

// (d) DOCX-source reviewed matter (no sourceFallback candidate) already opened
// on Redline -- must stay true.
assert.equal(
  initialModeAfterOpen(
    { id: "m4", source_filename: "vendor-nda.docx" },
    { clauses: [], paragraphs: [], redline_edits: [REDLINE_EDIT] },
    {},
  ),
  "redline",
  "reviewed DOCX matter must keep opening on the redline view",
);

// (e) Fidelity preference (source_fidelity.preferred_render_mode) is likewise
// outranked by redline work, and still wins when there is none.
const fidelityResult = (edits) => ({
  clauses: [],
  paragraphs: [],
  redline_edits: edits,
  source_fidelity: { preferred_render_mode: "source_pdf_preview" },
});
assert.equal(
  initialModeAfterOpen({ id: "m5", source_filename: "scan.docx" }, fidelityResult([REDLINE_EDIT]), {}),
  "redline",
  "redline edits must outrank the source-fidelity preference for Original",
);
assert.equal(
  initialModeAfterOpen({ id: "m6", source_filename: "scan.docx" }, fidelityResult([]), {}),
  "original",
  "source-fidelity preference for Original must still win without redline work",
);

// WIRING: loadMatterIntoReview (viewer.js) must thread the matter's saved
// redline_draft into renderResult -- without it, case (b) silently regresses to
// Original because the mode is computed before applyMatterRedlineDraft runs.
const viewerSource = read("static/js/review-workstation-viewer.js");
assert.match(
  viewerSource,
  /renderResult\([^;]*\{\s*redlineDraft:\s*matter\.redline_draft,?\s*\}/s,
  "loadMatterIntoReview must pass { redlineDraft: matter.redline_draft } to renderResult",
);

console.log("open-on-redline: all assertions passed");
