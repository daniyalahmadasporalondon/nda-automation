// Regression lock for the SECOND "PDF matter renders blank" cause (independent
// of the redline-render-resilience fix): the FE used to REFUSE to even attempt
// a page-image render for an "Original PDF" source matter unless that matter
// carried repository markers (source_type / board_column / document_title /
// review_refresh). A plain inbound PDF (e.g. Pismo) lacked those markers, so
// requestMatterDocumentRenderPreview returned early without fetching
// /render-status -- and the pane stayed blank even though the backend can
// rasterize the PDF fine (document_rendering.py, PyMuPDF, no soffice).
//
// This drives the REAL classic requestMatterDocumentRenderPreview (loaded via
// vm) and proves:
//   (a) a .pdf source LACKING all repository markers now DOES attempt the
//       /render-status fetch (gate no longer returns early);
//   (b) a .docx source is unchanged (still flows through to /render-status);
//   (c) a non-render source (e.g. .txt) is still skipped.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const STATIC_JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../static/js");

// Build a fresh sandbox carrying the REAL render module plus minimal stubs for
// the few globals the request path touches. fetch records every URL it is asked
// for so we can assert whether the early-return gate let the render proceed.
function makeSandbox(matter, reviewDocumentRender) {
  const fetchCalls = [];
  const sandbox = {
    console,
    // Never resolves: we only assert WHETHER fetch was invoked, not its result.
    fetch: (url) => {
      fetchCalls.push(String(url));
      return new Promise(() => {});
    },
    // renderStudioDocumentHighlights is a function DECLARATION in the module, so
    // a sandbox stub for it is shadowed at load. Instead, seed studioDocumentRender
    // as null so the REAL renderStudioDocumentHighlights returns immediately at its
    // `if (!studioDocumentRender) return;` guard -- harmless for this gate test,
    // which only asserts whether the /render-status fetch is attempted.
    studioDocumentRender: null,
    reviewErrorFromPayload: (_payload, message) => new Error(message),
    state: {
      selectedMatter: matter,
      reviewDocumentRender,
    },
  };
  vm.createContext(sandbox);
  for (const file of ["config.js", "review-workstation-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return { sandbox, fetchCalls };
}

function runRequest(matter, reviewDocumentRender) {
  const { sandbox, fetchCalls } = makeSandbox(matter, reviewDocumentRender);
  vm.runInContext("requestMatterDocumentRenderPreview()", sandbox);
  return fetchCalls;
}

// The render-state a plain "Original PDF" source produces (sourcePdfRenderCandidate
// -> normalizeReviewDocumentRender sets sourceFallback). This is the exact shape
// that used to be dropped by the gate.
const pdfFallbackRender = { sourceFallback: true, status: "ready", pdfUrl: "/api/matters/m1/source", sourceLabel: "Original PDF" };

// (a) A .pdf source with NONE of the repository markers must now attempt the
// page-image render (fetch /render-status) instead of returning early.
const plainPdfMatter = {
  id: "m1",
  source_filename: "pismo-nda.pdf",
  // deliberately NO source_type / board_column / document_title / review_refresh
};
const pdfCalls = runRequest(plainPdfMatter, pdfFallbackRender);
assert.equal(pdfCalls.length, 1, "plain PDF source must attempt a render fetch (gate no longer returns early)");
assert.ok(
  pdfCalls[0].includes("/api/matters/m1/render-status"),
  `expected a /render-status fetch, got: ${JSON.stringify(pdfCalls)}`,
);

// (b) A .docx source (which never gets a sourceFallback candidate) is unchanged:
// it still flows straight through to /render-status.
const docxMatter = { id: "m2", source_filename: "vendor-msa.docx" };
const docxCalls = runRequest(docxMatter, null);
assert.equal(docxCalls.length, 1, "docx source still attempts a render fetch");
assert.ok(
  docxCalls[0].includes("/api/matters/m2/render-status"),
  `docx expected a /render-status fetch, got: ${JSON.stringify(docxCalls)}`,
);

// (c) A non-renderable source (.txt) is still skipped -- no fetch attempted.
const txtMatter = { id: "m3", source_filename: "notes.txt" };
const txtCalls = runRequest(txtMatter, null);
assert.equal(txtCalls.length, 0, "non-render source (.txt) must not attempt a render fetch");

// (d) An ALREADY-rendered preview (real pages) short-circuits before fetching,
// so we never re-fetch a render we already have.
const alreadyRendered = { pages: [{ imageUrl: "/p1.png", pageNumber: 1 }], status: "ready" };
const cachedCalls = runRequest({ id: "m4", source_filename: "x.pdf" }, alreadyRendered);
assert.equal(cachedCalls.length, 0, "an already-rendered preview must not re-fetch");

console.log("pdf-source-render-gate: all assertions passed");
