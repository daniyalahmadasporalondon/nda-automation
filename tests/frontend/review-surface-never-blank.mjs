// Regression locks for the Review workstation "never-blank" surface contract,
// against the REAL classic scripts (loaded via vm) -- no mocks of the code under
// test. Covers the owed coverage from the faithful-render audit:
//
//   G1  per-mode non-blank matrix: original/redline/clean/sidebyside each produce
//       a NON-EMPTY review surface for BOTH a DOCX-source and a PDF-source fixture.
//   B   page-image-blank fix: the non-Original surface NEVER emits a fixed-height
//       /render-pdf iframe (the blank-block bug) when page-image rasterization
//       failed -- it falls through so the text reconstruction is the floor; and it
//       reads page_image_status. When page images genuinely exist it DOES paint.
//   G4  never-blank swap: flag ON, faithful render stubbed to fail/empty -> the
//       existing reconstruction surface is preserved (not blanked).
//   G5  stale-async: a delayed faithful render that resolves AFTER the matter/view
//       changed must NOT swap.
//
// renderReviewDocument (redline-rendering.js) is a pure HTML-string builder, so G1
// is asserted on its output directly. The swap/stale logic lives in
// maybeUpgradeOriginalSurfaceToFaithfulDocx (review-workstation-rendering.js); G4/G5
// drive it with a real jsdom DOM + stubbed app globals.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

import { escapeHtml, joinClasses, mergeClauses } from "../../static/js/modules/html-utils.mjs";
import {
  fullReplacementOperations,
  needsInlineSpace,
  renderDiffOperations,
  renderInlineToken,
} from "../../static/js/modules/inline-diff.mjs";
import { clauseStatus } from "../../static/js/modules/clause-status.mjs";
import { RedlineEditContract } from "../../static/js/modules/redline-edit-contract.mjs";

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
  console.log("SKIP review-surface-never-blank: jsdom not installed (run `npm install`).");
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Load the real classic scripts into one vm sandbox with a jsdom DOM and the
// minimal app globals the rendering module reaches for.
// ---------------------------------------------------------------------------
const RENDER_FILES = [
  "config.js",
  "redline-rendering.js",
  "review-workstation-rendering.js",
];

function freshSandbox() {
  const dom = new JSDOM("<!doctype html><html><body><div id=\"doc\"></div></body></html>");
  const { window } = dom;
  window.RedlineEditContract = RedlineEditContract;
  const sandbox = {
    window,
    document: window.document,
    Node: window.Node,
    console,
    // Bridge globals the classic render scripts read as bare identifiers (these are
    // exposed onto the browser globals by global-bridge.mjs in production).
    escapeHtml,
    joinClasses,
    mergeClauses,
    clauseStatus,
    renderDiffOperations,
    renderInlineToken,
    fullReplacementOperations,
    needsInlineSpace,
    RedlineEditContract,
    // App globals the rendering module reads. studioDocumentRender is the live pane.
    state: {
      selectedMatter: null,
      reviewDocumentRender: null,
      documentViewMode: "original",
      reviewClauses: [],
      reviewParagraphs: [],
      latestReviewResult: null,
      selectedReviewClauseId: null,
    },
    studioDocumentRender: window.document.getElementById("doc"),
    // Stubs for cross-file helpers the upgrader calls on a successful swap.
    showStudioDocumentRender: () => {},
    showStudioSourceEditor: () => {},
  };
  // The module guards optional helpers behind typeof checks, so leaving the rest
  // undefined is fine.
  vm.createContext(sandbox);
  for (const file of RENDER_FILES) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return { sandbox, window };
}

// Shared minimal review fixtures (one clause + two paragraphs). Enough that
// renderReviewDocument emits real paragraph frames for every view mode.
function reviewFixture() {
  const paragraphs = [
    { id: "p1", index: 0, source_index: 0, text: "The Receiving Party shall keep all information confidential.", original_text: "The Receiving Party shall keep all information confidential." },
    { id: "p2", index: 1, source_index: 1, text: "This Agreement is governed by the laws of England and Wales.", original_text: "This Agreement is governed by the laws of England and Wales." },
  ];
  const clauses = [
    { id: "c1", clause_type: "confidentiality", matched_paragraph_ids: ["p1"], status: "review" },
    { id: "c2", clause_type: "governing_law", matched_paragraph_ids: ["p2"], status: "pass" },
  ];
  return { paragraphs, clauses };
}

const VIEW_MODES = ["original", "redline", "clean", "sidebyside"];

// ===========================================================================
// G1: per-mode non-blank matrix (DOCX-source AND PDF-source).
// ===========================================================================
function testPerModeNonBlankMatrix() {
  const { sandbox } = freshSandbox();
  const renderReviewDocument = vm.runInContext("renderReviewDocument", sandbox);
  const { paragraphs, clauses } = reviewFixture();

  // The reconstruction is source-agnostic, but we assert for BOTH a DOCX-source and
  // a PDF-source matter to lock the contract that neither source can blank.
  for (const sourceKind of ["docx", "pdf"]) {
    for (const viewMode of VIEW_MODES) {
      const html = renderReviewDocument({
        clauses,
        comments: [],
        originalParagraphs: paragraphs,
        paragraphs,
        redlines: [],
        selectedClauseId: null,
        viewMode,
      });
      assert.equal(typeof html, "string");
      assert.ok(html.trim().length > 0,
        `${sourceKind}/${viewMode}: reconstruction HTML must be non-empty`);
      // It must carry real paragraph frames (the interactive hooks), not just chrome.
      assert.ok(/data-paragraph-id="p1"/.test(html),
        `${sourceKind}/${viewMode}: must contain paragraph p1 frame`);
      assert.ok(/data-paragraph-id="p2"/.test(html),
        `${sourceKind}/${viewMode}: must contain paragraph p2 frame`);
      // Non-whitespace text content present (proxy for a non-blank surface).
      const textish = html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
      assert.ok(textish.length > 0, `${sourceKind}/${viewMode}: rendered text must be non-empty`);
    }
  }
  console.log("PASS G1: per-mode non-blank matrix (original/redline/clean/sidebyside) for DOCX + PDF source.");
}

// ===========================================================================
// B: page-image-blank fix on the NON-Original surface (renderPdfDocumentSurface).
// ===========================================================================
function testPageImageBlankFix() {
  const { sandbox } = freshSandbox();
  const renderPdfDocumentSurface = vm.runInContext("renderPdfDocumentSurface", sandbox);
  const normalizeReviewDocumentRender = vm.runInContext("normalizeReviewDocumentRender", sandbox);
  const pageImageSurfaceUsable = vm.runInContext("pageImageSurfaceUsable", sandbox);

  // THE BUG: PDF render succeeded (status ready + pdf_url) but page-image
  // rasterization FAILED -> page_image_status:"failed", pages:[]. The old code took
  // the iframe branch and painted a ~520px blank /render-pdf iframe above the text.
  const failedPageImages = normalizeReviewDocumentRender({
    status: "ready",
    pdf_url: "/api/matters/m1/render-pdf",
    page_image_status: "failed",
    pages: [],
    source_label: "Rendered PDF",
  });
  assert.ok(failedPageImages, "render-state must normalize");
  assert.equal(failedPageImages.pageImageStatus, "failed", "page_image_status must be read into render-state");
  assert.equal(pageImageSurfaceUsable(failedPageImages), false,
    "page-image surface must be UNUSABLE when rasterization failed");
  const surface = renderPdfDocumentSurface(failedPageImages);
  assert.equal(surface, "",
    "non-Original surface must emit NOTHING (no blank iframe) when page images failed");
  assert.ok(!/iframe/.test(surface), "must never contain a /render-pdf iframe in the non-Original surface");

  // Also covers the previously-buggy shape: status ready + pdfUrl + empty pages and
  // NO explicit page_image_status -> still must not emit a blank iframe.
  const readyPdfNoPages = normalizeReviewDocumentRender({
    status: "ready",
    pdf_url: "/api/matters/m1/render-pdf",
    pages: [],
    source_label: "Rendered PDF",
  });
  assert.equal(renderPdfDocumentSurface(readyPdfNoPages), "",
    "ready+pdfUrl+empty-pages must not paint a blank iframe surface");

  // POSITIVE: when page images genuinely exist + good status -> the page surface DOES paint.
  const goodPages = normalizeReviewDocumentRender({
    status: "ready",
    page_image_status: "ready",
    pages: [{ page_number: 1, image_url: "/api/matters/m1/render-page/1", width: 800, height: 1000 }],
    source_label: "Original PDF",
  });
  assert.equal(pageImageSurfaceUsable(goodPages), true, "good page images must be usable");
  const goodSurface = renderPdfDocumentSurface(goodPages);
  assert.ok(goodSurface.includes("review-render-pages"),
    "with real page images the page-image surface must paint");
  assert.ok(goodSurface.includes("Editable text review"),
    "the page surface keeps the divider above the editable reconstruction");

  console.log("PASS B: page-image-blank fix -- failed/empty page images emit no blank iframe; "
    + "page_image_status read; real page images still paint.");
}

// ===========================================================================
// G4 + G5: never-blank swap and stale-async, via maybeUpgradeOriginalSurfaceToFaithfulDocx.
// ===========================================================================
function installFaithful(sandbox, renderImpl, enabled = true) {
  sandbox.window.FaithfulDocxRender = {
    enabled: () => enabled,
    libraryAvailable: () => true,
    render: renderImpl,
  };
}

async function testNeverBlankSwapAndStaleAsync() {
  // ---- G4: faithful render fails/empty -> reconstruction preserved (not blanked).
  {
    const { sandbox } = freshSandbox();
    const maybeUpgrade = vm.runInContext("maybeUpgradeOriginalSurfaceToFaithfulDocx", sandbox);

    // A DOCX-source matter in the Original view, with an existing reconstruction painted.
    sandbox.state.selectedMatter = { id: "m-docx", source_filename: "nda.docx" };
    sandbox.state.documentViewMode = "original";
    sandbox.studioDocumentRender.innerHTML = "<section data-reconstruction>EXISTING RECONSTRUCTION</section>";

    let calls = 0;
    installFaithful(sandbox, async () => { calls += 1; return { ok: false, reason: "empty_render" }; });

    maybeUpgrade();
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(calls, 1, "faithful render must have been attempted for a DOCX source with flag ON");
    assert.ok(sandbox.studioDocumentRender.innerHTML.includes("EXISTING RECONSTRUCTION"),
      "G4: a failed/empty faithful render must leave the reconstruction intact (never blank)");
    assert.ok(!sandbox.studioDocumentRender.innerHTML.includes("review-faithful-original"),
      "G4: no faithful surface must be swapped in on failure");
  }

  // ---- G5: stale async (matter changed before the faithful render resolves) -> no swap.
  {
    const { sandbox } = freshSandbox();
    const maybeUpgrade = vm.runInContext("maybeUpgradeOriginalSurfaceToFaithfulDocx", sandbox);

    sandbox.state.selectedMatter = { id: "m-docx", source_filename: "nda.docx" };
    sandbox.state.documentViewMode = "original";
    sandbox.studioDocumentRender.innerHTML = "<section data-reconstruction>RECON FOR M-DOCX</section>";

    let resolveRender;
    installFaithful(sandbox, (host) => new Promise((resolve) => {
      // Simulate docx-preview painting content into the detached host, then a
      // delayed resolve we control.
      host.innerHTML = "<div class=\"docx\">FAITHFUL BYTES</div>";
      resolveRender = () => resolve({ ok: true });
    }));

    maybeUpgrade();
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The user switches to a DIFFERENT matter while the render is in flight.
    sandbox.state.selectedMatter = { id: "m-other", source_filename: "other.docx" };
    sandbox.studioDocumentRender.innerHTML = "<section data-reconstruction>RECON FOR M-OTHER</section>";

    resolveRender(); // the stale render finally resolves
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.ok(sandbox.studioDocumentRender.innerHTML.includes("RECON FOR M-OTHER"),
      "G5: a stale faithful render (matter changed) must NOT swap over the current surface");
    assert.ok(!sandbox.studioDocumentRender.innerHTML.includes("FAITHFUL BYTES"),
      "G5: stale faithful content must not appear");
  }

  // ---- G5b: stale async via VIEW change (left Original before resolve) -> no swap.
  {
    const { sandbox } = freshSandbox();
    const maybeUpgrade = vm.runInContext("maybeUpgradeOriginalSurfaceToFaithfulDocx", sandbox);

    sandbox.state.selectedMatter = { id: "m-docx", source_filename: "nda.docx" };
    sandbox.state.documentViewMode = "original";
    sandbox.studioDocumentRender.innerHTML = "<section>ORIGINAL RECON</section>";

    let resolveRender;
    installFaithful(sandbox, (host) => new Promise((resolve) => {
      host.innerHTML = "<div class=\"docx\">FAITHFUL BYTES</div>";
      resolveRender = () => resolve({ ok: true });
    }));

    maybeUpgrade();
    await new Promise((resolve) => setTimeout(resolve, 0));

    // User switches to the redline view while bytes are in flight.
    sandbox.state.documentViewMode = "redline";
    sandbox.studioDocumentRender.innerHTML = "<section>REDLINE RECON</section>";

    resolveRender();
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.ok(sandbox.studioDocumentRender.innerHTML.includes("REDLINE RECON"),
      "G5b: leaving Original before resolve must NOT swap the faithful surface in");
    assert.ok(!sandbox.studioDocumentRender.innerHTML.includes("FAITHFUL BYTES"),
      "G5b: stale faithful content must not appear after a view change");
  }

  console.log("PASS G4+G5: never-blank swap (failed render keeps reconstruction) and "
    + "stale-async drops (matter change + view change).");
}

testPerModeNonBlankMatrix();
testPageImageBlankFix();
await testNeverBlankSwapAndStaleAsync();
console.log("\nALL PASS: review-surface-never-blank regression locks");
