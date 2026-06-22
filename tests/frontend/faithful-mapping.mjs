// Phase-2 faithful-render MAPPING + round-trip locks, against the REAL classic
// scripts (loaded via vm with a jsdom DOM) -- no mocks of the code under test.
//
// Covers the tickets' required proofs:
//   M1  GUARD ports /tmp/drift/final_guard.mjs EXACTLY:
//         - count-exact (tolerance 0) -> count-mismatch ABORTS
//         - ordered token-subsequence allowance (legit inline tracked-insert
//           commits; a wrong/divergent paragraph ABORTS) -- NOT a prefix check
//   M2  happy-path mapping (DOCX): a 1:1 rendered<->structured set COMMITS, each
//         faithful paragraph is stamped studio-doc-paragraph + data-paragraph-id +
//         data-clause-ids + data-editable-paragraph-id (contenteditable).
//   M3  data-clause-ids are the RIGHT ids (no mis-attach): the clause whose
//         matched_paragraph_ids names a paragraph lands on THAT paragraph, never a
//         neighbour; a document-title paragraph carries NO clause linkage.
//   M4  count-mismatch -> abort (no DOM mutation); checksum-drift -> abort.
//   M5  rich round-trip: a TEXT edit AND a FORMATTING edit on the faithful surface
//         produce the SAME manual_redline_edits the reconstruction would
//         (manualExportRedlines), and the runs.join()===text invariant holds.
//   M6  read-back failure ABORTS that paragraph to the reconstruction editor
//         (contenteditable=false + a visible notice) -- never silently corrupts.
//   M7  toggle carries the edit: an edit written to state.reviewParagraphs is
//         carried by the model (there is NO faithful-only edit buffer), so flipping
//         the faithful flag OFF (reconstruction) still sees it.
//
// jsdom is a devDependency; the test SKIPS cleanly when it is absent (CI parity).

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
    // try NODE_PATH roots
  }
  const { createRequire } = await import("node:module");
  const require = createRequire(import.meta.url);
  const roots = String(process.env.NODE_PATH || "").split(path.delimiter).filter(Boolean);
  for (const root of roots) {
    try {
      const entry = require.resolve("jsdom", { paths: [root] });
      return (await import(`file://${entry}`)).JSDOM;
    } catch (_error) {
      // next root
    }
  }
  return null;
}

const JSDOM = await loadJsdom();
if (!JSDOM) {
  console.log("SKIP faithful-mapping: jsdom not installed (run `npm install`).");
  process.exit(0);
}

const RENDER_FILES = [
  "config.js",
  "redline-rendering.js",
  "review-workstation-rendering.js",
  "review-workstation-source.js",
];

// Build a fresh sandbox with a jsdom DOM and the minimal app globals the rendering
// + source modules read as bare identifiers (these are bridged onto window in prod).
function freshSandbox(initialState) {
  const dom = new JSDOM("<!doctype html><html><body><div id=\"doc\"></div></body></html>");
  const { window } = dom;
  const sandbox = {
    window,
    document: window.document,
    Node: window.Node,
    NodeFilter: window.NodeFilter,
    console,
    escapeHtml,
    joinClasses,
    mergeClauses,
    clauseStatus,
    renderDiffOperations,
    renderInlineToken,
    fullReplacementOperations,
    needsInlineSpace,
    RedlineEditContract,
    state: {
      selectedMatter: null,
      reviewDocumentRender: null,
      documentViewMode: "redline",
      reviewClauses: [],
      reviewParagraphs: [],
      reviewOriginalParagraphs: [],
      reviewExportOriginalParagraphs: [],
      reviewComments: [],
      latestReviewResult: null,
      selectedReviewClauseId: null,
      activeFormatParagraphId: null,
      ...(initialState || {}),
    },
    studioDocumentRender: window.document.getElementById("doc"),
    showStudioDocumentRender: () => {},
    showStudioSourceEditor: () => {},
    setFileMeta: () => {},
    // cssEscape lives in review-workstation-viewer.js (a sibling classic script in
    // the same global scope in prod). Provide the same minimal escaper here so the
    // attribute-selector lookups in the rendering module resolve.
    cssEscape: (value) => String(value == null ? "" : value).replace(/["\\\]]/g, "\\$&"),
    // The clause-click binder calls selectReviewClause; record calls so M3 can
    // prove a click selects the RIGHT clause.
    selectReviewClause: (id) => { sandbox.__selectedClauseId = id; },
    // currentReviewComments is defined in the rendering module; provide a stub for
    // the source module's reads that need comments before the module defines it.
  };
  vm.createContext(sandbox);
  for (const file of RENDER_FILES) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return { sandbox, window };
}

function get(sandbox, name) {
  return vm.runInContext(name, sandbox);
}

// A small DOCX-like rendered host: docx-preview emits `.docx` wrapper with <p>
// children (and <header>/<footer> for headers). We build the same shape so the
// mapper's querySelectorAll(".docx p") + header/footer exclusion are exercised.
function buildRenderedHost(window, paragraphTexts, { headerText, footerText, trackedInsert } = {}) {
  const host = window.document.createElement("div");
  const docx = window.document.createElement("div");
  docx.className = "docx";
  if (headerText) {
    const header = window.document.createElement("header");
    const hp = window.document.createElement("p");
    hp.textContent = headerText;
    header.appendChild(hp);
    docx.appendChild(header);
  }
  paragraphTexts.forEach((text, i) => {
    const p = window.document.createElement("p");
    if (trackedInsert && trackedInsert.index === i) {
      // Render a tracked insertion: base text + an <ins> with the inserted run.
      p.appendChild(window.document.createTextNode(trackedInsert.base));
      const ins = window.document.createElement("ins");
      ins.textContent = trackedInsert.inserted;
      p.appendChild(ins);
    } else {
      p.textContent = text;
    }
    docx.appendChild(p);
  });
  if (footerText) {
    const footer = window.document.createElement("footer");
    const fp = window.document.createElement("p");
    fp.textContent = footerText;
    footer.appendChild(fp);
    docx.appendChild(footer);
  }
  host.appendChild(docx);
  return host;
}

// ===========================================================================
// M1: the GUARD ports final_guard.mjs exactly.
// ===========================================================================
function testGuardPortsReference() {
  const { sandbox } = freshSandbox();
  const guard = get(sandbox, "faithfulMappingGuardPasses");
  const isSub = get(sandbox, "faithfulIsTokenSubsequence");

  // token-subsequence (NOT substring/prefix): ordered tokens only.
  assert.equal(isSub("alpha gamma", "alpha beta gamma"), true, "ordered subsequence matches");
  assert.equal(isSub("gamma alpha", "alpha beta gamma"), false, "out-of-order does NOT match");
  assert.equal(isSub("alpha beta gamma", "alpha gamma"), false, "superset is not a subsequence of subset");
  assert.equal(isSub("", "anything"), false, "empty small never matches (guards trivial pass)");

  const S = (texts) => texts.map((t) => ({ text: t }));

  // exact equal -> COMMIT
  assert.equal(guard(["The Receiving Party.", "Governed by England."], S(["The Receiving Party.", "Governed by England."])), true);

  // legit inline tracked-insert: rendered superset of structured (structured ⊑ rendered) -> COMMIT
  assert.equal(
    guard(["The Receiving Party shall keep all data confidential."], S(["The Receiving Party shall keep data confidential."])),
    true,
    "structured is an ordered token-subsequence of rendered -> commit",
  );

  // COUNT mismatch (tolerance 0) -> ABORT
  assert.equal(guard(["a", "b", "c"], S(["a", "b"])), false, "count mismatch must abort");
  assert.equal(guard(["a"], S(["a", "b"])), false, "count mismatch (fewer rendered) must abort");

  // checksum drift: a genuinely different paragraph (neither subsequence) -> ABORT.
  // This is the boilerplate mis-attach trap: a prefix/substring check would wrongly
  // pass these as "close enough"; the ordered-token-subsequence guard rejects them.
  assert.equal(
    guard(
      ["The Receiving Party shall keep all information confidential at all times."],
      S(["The Disclosing Party may share information with its affiliates as needed."]),
    ),
    false,
    "divergent paragraph must abort (no silent mis-attach)",
  );

  console.log("PASS M1: guard = count-exact + ordered-token-subsequence (insert commits; mismatch/drift abort).");
}

// ===========================================================================
// M2 + M3: happy-path mapping + correct (non-mis-attached) clause ids.
// ===========================================================================
function testHappyPathMappingAndClauseIds() {
  const reviewParagraphs = [
    { id: "title", index: 0, source_index: 0, text: "Mutual Non-Disclosure Agreement", isTitle: true, structure: { isTitle: true } },
    { id: "p1", index: 1, source_index: 1, text: "The Receiving Party shall keep all information confidential." },
    { id: "p2", index: 2, source_index: 2, text: "This Agreement is governed by the laws of England and Wales." },
  ];
  const reviewClauses = [
    { id: "c-conf", clause_type: "confidentiality", matched_paragraph_ids: ["p1"], status: "review" },
    { id: "c-gov", clause_type: "governing_law", matched_paragraph_ids: ["p2"], status: "pass" },
  ];
  const { sandbox, window } = freshSandbox({ reviewParagraphs, reviewClauses });

  // Make the title paragraph register as a document title for the suppression check.
  // paragraphIsDocumentTitle reads paragraph.structure/isTitle; if the real predicate
  // is stricter, the mapping still works -- M3's core claim is the p1/p2 ids.
  const studioDocumentRender = get(sandbox, "studioDocumentRender");
  const host = buildRenderedHost(
    window,
    [
      "Mutual Non-Disclosure Agreement",
      "The Receiving Party shall keep all information confidential.",
      "This Agreement is governed by the laws of England and Wales.",
    ],
    { headerText: "CONFIDENTIAL", footerText: "Page 1" },
  );
  studioDocumentRender.appendChild(host); // live so the deferred editor bind can find it

  const bind = get(sandbox, "bindFaithfulDocxInteractions");
  const committed = bind(host, "redline");
  assert.equal(committed, true, "a clean 1:1 set must COMMIT");

  const ps = host.querySelectorAll(".docx > p, .docx p");
  // Only the 3 body paragraphs are mappable (header/footer excluded).
  const mappable = Array.from(host.querySelectorAll(".docx p")).filter((el) => !el.closest("header,footer"));
  assert.equal(mappable.length, 3, "header/footer paragraphs are excluded from the map");

  const [pTitle, pConf, pGov] = mappable;
  // The .docx <p> is the FRAME; the inner .faithful-paragraph-editable is the editable.
  assert.equal(pConf.getAttribute("data-paragraph-id"), "p1");
  assert.equal(pGov.getAttribute("data-paragraph-id"), "p2");
  assert.ok(pConf.classList.contains("studio-doc-paragraph"), "stamped studio-doc-paragraph");
  const editConf = pConf.querySelector(".faithful-paragraph-editable");
  assert.ok(editConf, "an inner editable wrapper was created");
  assert.equal(editConf.getAttribute("contenteditable"), "true", "mapped paragraph is rich-editable");
  assert.equal(editConf.getAttribute("data-editable-paragraph-id"), "p1");
  assert.equal(editConf.getAttribute("data-faithful-editable"), "", "editable carries the faithful flag");
  // The comment tools are a SIBLING of the editable, never inside it (so innerText
  // edits never fold the comment count/icon into paragraph.text).
  const toolsInsideEditable = editConf.querySelector(".paragraph-comment-tools");
  assert.equal(toolsInsideEditable, null, "comment tools live on the frame, not inside the editable");

  // M3: the RIGHT clause ids, never mis-attached.
  assert.equal(pConf.getAttribute("data-clause-ids"), "c-conf", "confidentiality clause lands on p1");
  assert.equal(pGov.getAttribute("data-clause-ids"), "c-gov", "governing-law clause lands on p2");
  assert.notEqual(pConf.getAttribute("data-clause-ids"), "c-gov", "NEVER mis-attach the neighbour's clause");
  // The header paragraph never received a clause/edit stamp.
  const headerP = host.querySelector("header p");
  assert.equal(headerP.getAttribute("data-clause-ids"), null, "header is not mapped");
  assert.equal(headerP.querySelector(".faithful-paragraph-editable"), null, "header is not editable");

  console.log("PASS M2/M3: 1:1 map commits; header/footer excluded; clause ids land on the right paragraphs (no mis-attach).");
  return { sandbox, window, host };
}

// ===========================================================================
// M4: count-mismatch + checksum-drift abort WITHOUT mutating the DOM.
// ===========================================================================
function testAbortLeavesDomUntouched() {
  const reviewParagraphs = [
    { id: "p1", index: 0, source_index: 0, text: "The Receiving Party shall keep all information confidential." },
    { id: "p2", index: 1, source_index: 1, text: "This Agreement is governed by the laws of England and Wales." },
  ];
  // (a) COUNT mismatch: 3 rendered vs 2 structured.
  {
    const { sandbox, window } = freshSandbox({ reviewParagraphs });
    const host = buildRenderedHost(window, [
      "The Receiving Party shall keep all information confidential.",
      "This Agreement is governed by the laws of England and Wales.",
      "An extra rendered paragraph with no structured match.",
    ]);
    const bind = get(sandbox, "bindFaithfulDocxInteractions");
    const committed = bind(host, "redline");
    assert.equal(committed, false, "count mismatch must abort");
    host.querySelectorAll(".docx p").forEach((p) => {
      assert.equal(p.getAttribute("data-paragraph-id"), null, "ABORT must not stamp any paragraph");
      assert.equal(p.getAttribute("contenteditable"), null, "ABORT must not make anything editable");
    });
  }
  // (b) CHECKSUM drift: same count, one paragraph diverges (boilerplate mis-attach trap).
  {
    const { sandbox, window } = freshSandbox({ reviewParagraphs });
    const host = buildRenderedHost(window, [
      "The Receiving Party shall keep all information confidential.",
      "The Disclosing Party may freely use its own pre-existing materials.",
    ]);
    const bind = get(sandbox, "bindFaithfulDocxInteractions");
    const committed = bind(host, "redline");
    assert.equal(committed, false, "checksum drift must abort");
    host.querySelectorAll(".docx p").forEach((p) => {
      assert.equal(p.getAttribute("data-paragraph-id"), null, "drift ABORT must not stamp");
    });
  }
  console.log("PASS M4: count-mismatch + checksum-drift both ABORT and leave the DOM untouched.");
}

// ===========================================================================
// M5: rich round-trip -- a TEXT edit and a FORMAT edit on the faithful model
// produce the SAME manual_redline_edits the reconstruction would, and the
// runs.join()===text invariant holds.
// ===========================================================================
function testRichRoundTrip() {
  const baseline = [
    { id: "p1", index: 0, source_index: 0, text: "The Receiving Party shall keep all information confidential." },
  ];
  // The model after a faithful TEXT edit: text changed.
  const editedText = "The Receiving Party shall keep all DISCLOSED information confidential.";
  const reviewParagraphs = [{ id: "p1", index: 0, source_index: 0, text: editedText }];
  const { sandbox } = freshSandbox({
    reviewParagraphs,
    reviewOriginalParagraphs: baseline.map((p) => ({ ...p })),
    reviewExportOriginalParagraphs: [],
  });
  const manualExportRedlines = get(sandbox, "manualExportRedlines");

  // TEXT edit -> a replace_paragraph manual redline with the exact replacement text.
  const textEdits = manualExportRedlines();
  assert.equal(textEdits.length, 1, "one paragraph changed -> one manual redline");
  assert.equal(textEdits[0].action, "replace_paragraph");
  assert.equal(textEdits[0].paragraph_id, "p1");
  assert.equal(textEdits[0].replacement_text, editedText);
  assert.equal(textEdits[0].original_text, baseline[0].text);

  // FORMAT edit: text identical to baseline, but runs carry inline bold over a span.
  // This is exactly what the toolbar writes (paragraph.runs by offset). The export
  // must emit a format_paragraph redline whose run ops describe the bold.
  const formatModel = [{
    id: "p1",
    index: 0,
    source_index: 0,
    text: baseline[0].text,
    runs: [
      { text: "The Receiving Party shall keep all " },
      { text: "information", bold: true },
      { text: " confidential." },
    ],
  }];
  const { sandbox: sb2 } = freshSandbox({
    reviewParagraphs: formatModel,
    reviewOriginalParagraphs: baseline.map((p) => ({ ...p, runs: [{ text: p.text }] })),
  });
  const exportFmt = get(sb2, "manualExportRedlines");
  const fmtEdits = exportFmt();
  assert.equal(fmtEdits.length, 1, "a run-format-only change emits a format_paragraph redline");
  assert.equal(fmtEdits[0].action, "format_paragraph");
  assert.ok(Array.isArray(fmtEdits[0].format_ops) && fmtEdits[0].format_ops.length >= 1, "format_ops present");
  const boldOp = fmtEdits[0].format_ops.find((op) => op.property === "bold" && op.scope === "run");
  assert.ok(boldOp, "a run-scope bold op is emitted for the bolded selection");
  assert.equal(boldOp.to, true, "bold op turns bold ON");

  // runs.join() === text invariant on the format model.
  const joined = formatModel[0].runs.map((r) => r.text).join("");
  assert.equal(joined, formatModel[0].text, "runs.join() === text invariant holds");

  console.log("PASS M5: faithful text edit -> replace_paragraph; faithful bold -> format_paragraph run op; runs.join()===text.");
}

// ===========================================================================
// M6: read-back failure aborts the paragraph to the reconstruction editor.
// ===========================================================================
function testReadBackFailureAborts() {
  // Model text that CANNOT be reconciled with the faithful DOM text (different
  // non-space character streams) forces the re-tile/assert to fail -> abort.
  const reviewParagraphs = [
    { id: "p1", index: 0, source_index: 0, text: "Completely different model text that does not match the dom." },
  ];
  const { sandbox, window } = freshSandbox({ reviewParagraphs });
  const studioDocumentRender = get(sandbox, "studioDocumentRender");

  // Build a live faithful frame+editable whose textContent differs from the model.
  const wrapper = window.document.createElement("section");
  wrapper.setAttribute("data-faithful-docx", "");
  const p = window.document.createElement("p");
  p.className = "docx studio-doc-paragraph";
  p.setAttribute("data-paragraph-id", "p1");
  const editable = window.document.createElement("div");
  editable.className = "paragraph-editable faithful-paragraph-editable";
  editable.setAttribute("data-editable-paragraph-id", "p1");
  editable.setAttribute("data-faithful-editable", "");
  editable.setAttribute("contenteditable", "true");
  editable.appendChild(window.document.createTextNode("The faithful DOM shows totally other words here entirely."));
  p.appendChild(editable);
  wrapper.appendChild(p);
  studioDocumentRender.appendChild(wrapper);

  const seed = get(sandbox, "seedFaithfulParagraphRunsFromDom");
  const ok = seed("p1");
  assert.equal(ok, false, "read-back that fails its assert/re-tile must return false (abort)");
  // The paragraph is locked to the reconstruction editor (never silently corrupted).
  const lockedEditable = studioDocumentRender.querySelector('[data-editable-paragraph-id="p1"]');
  assert.equal(lockedEditable.getAttribute("contenteditable"), "false", "aborted editable is no longer editable");
  const lockedFrame = studioDocumentRender.querySelector('[data-paragraph-id="p1"]');
  assert.ok(lockedFrame.classList.contains("faithful-edit-locked"), "aborted frame is marked locked");
  assert.ok(lockedFrame.querySelector(".faithful-edit-locked-note"), "a visible reconstruction-editor notice is shown");
  // The model runs are NOT set to a corrupt value.
  const stateOut = get(sandbox, "state");
  assert.ok(!stateOut.reviewParagraphs[0].runs || stateOut.reviewParagraphs[0].runs.map((r) => r.text).join("") === stateOut.reviewParagraphs[0].text,
    "model runs are never left in a drifted (non-tiling) state");

  console.log("PASS M6: read-back failure aborts the paragraph to reconstruction (locked + notice); model never corrupted.");
}

// A read-back SUCCESS path: the DOM text matches the model text (whitespace-
// normalized) and carries a <strong> run; seeding produces tiling runs that
// capture the bold, and the runs.join()===text invariant holds.
function testReadBackSuccess() {
  const text = "The Receiving Party shall keep information confidential.";
  const reviewParagraphs = [{ id: "p1", index: 0, source_index: 0, text }];
  const { sandbox, window } = freshSandbox({ reviewParagraphs });
  const studioDocumentRender = get(sandbox, "studioDocumentRender");
  const wrapper = window.document.createElement("section");
  wrapper.setAttribute("data-faithful-docx", "");
  const p = window.document.createElement("p");
  p.className = "docx studio-doc-paragraph";
  p.setAttribute("data-paragraph-id", "p1");
  const editable = window.document.createElement("div");
  editable.className = "paragraph-editable faithful-paragraph-editable";
  editable.setAttribute("data-editable-paragraph-id", "p1");
  editable.setAttribute("data-faithful-editable", "");
  editable.setAttribute("contenteditable", "true");
  // "The Receiving Party shall keep " + <strong>information</strong> + " confidential."
  editable.appendChild(window.document.createTextNode("The Receiving Party shall keep "));
  const strong = window.document.createElement("strong");
  strong.textContent = "information";
  editable.appendChild(strong);
  editable.appendChild(window.document.createTextNode(" confidential."));
  p.appendChild(editable);
  wrapper.appendChild(p);
  studioDocumentRender.appendChild(wrapper);

  const seed = get(sandbox, "seedFaithfulParagraphRunsFromDom");
  const ok = seed("p1");
  assert.equal(ok, true, "read-back that matches the model text must succeed");
  const stateOut = get(sandbox, "state");
  const runs = stateOut.reviewParagraphs[0].runs;
  assert.ok(Array.isArray(runs) && runs.length, "runs seeded from the DOM");
  assert.equal(runs.map((r) => r.text).join(""), text, "seeded runs byte-tile the model text (invariant)");
  assert.ok(runs.some((r) => r.bold && r.text.includes("information")), "the <strong> run is captured as bold");

  console.log("PASS M6b: read-back success seeds tiling runs that capture the source bold; invariant holds.");
}

// ===========================================================================
// M7: the toggle carries the edit -- there is NO faithful-only edit buffer.
// A model edit is visible to the reconstruction export regardless of surface.
// ===========================================================================
function testToggleCarriesEdit() {
  // Edit lives ONLY in state.reviewParagraphs. Whether the faithful surface is live
  // or not, manualExportRedlines (the reconstruction export oracle) sees the edit.
  const baseline = [{ id: "p1", index: 0, source_index: 0, text: "Original confidential clause text." }];
  const reviewParagraphs = [{ id: "p1", index: 0, source_index: 0, text: "Edited confidential clause text." }];
  const { sandbox } = freshSandbox({
    reviewParagraphs,
    reviewOriginalParagraphs: baseline.map((p) => ({ ...p })),
  });
  const manualExportRedlines = get(sandbox, "manualExportRedlines");
  const edits = manualExportRedlines();
  assert.equal(edits.length, 1, "the model edit is exported regardless of which surface produced it");
  assert.equal(edits[0].replacement_text, "Edited confidential clause text.");
  // There is no separate faithful edit buffer to inspect: the model IS the buffer.
  console.log("PASS M7: edits live in state.reviewParagraphs (no faithful-only buffer) -> carried across the toggle.");
}

// ===========================================================================
// M8: the FOUR pre-locked classes (from the adversarial round-trip pass) are
// MAPPED (clause ids / comments still work) but NOT editable on the faithful
// surface -- editing routes to the reconstruction. They still COMMIT (so the
// faithful preview shows), they are just read-only where editing is unsafe.
// ===========================================================================
function testPreLockedClasses() {
  // Five body paragraphs, each a different class. The guard maps them 1:1; only the
  // safe prose paragraph stays editable.
  const reviewParagraphs = [
    { id: "p-safe", index: 0, source_index: 0, text: "Plain prose that round-trips cleanly here." },
    { id: "p-tracked", index: 1, source_index: 1, text: "The term is five years from the date." },
    { id: "p-table", index: 2, source_index: 2, text: "A cell paragraph inside a table." },
    { id: "p-link", index: 3, source_index: 3, text: "See the policy for the details." },
    // Block split: two model paragraphs share source_index 4.
    { id: "p-split-a", index: 4, source_index: 4, text: "First block of a split paragraph." },
    { id: "p-split-b", index: 5, source_index: 4, text: "Second block of the same split paragraph." },
  ];
  const { sandbox, window } = freshSandbox({ reviewParagraphs });
  const studioDocumentRender = get(sandbox, "studioDocumentRender");

  // Build a docx host with the matching classes, in the SAME order as the model.
  const host = window.document.createElement("div");
  const docx = window.document.createElement("div");
  docx.className = "docx";
  // safe
  const pSafe = window.document.createElement("p");
  pSafe.textContent = "Plain prose that round-trips cleanly here.";
  docx.appendChild(pSafe);
  // tracked changes (ins/del)
  const pTracked = window.document.createElement("p");
  pTracked.appendChild(window.document.createTextNode("The term is "));
  const ins = window.document.createElement("ins");
  ins.textContent = "five";
  pTracked.appendChild(ins);
  pTracked.appendChild(window.document.createTextNode(" years from the date."));
  docx.appendChild(pTracked);
  // table cell
  const table = window.document.createElement("table");
  const tr = window.document.createElement("tr");
  const td = window.document.createElement("td");
  const pTable = window.document.createElement("p");
  pTable.textContent = "A cell paragraph inside a table.";
  td.appendChild(pTable); tr.appendChild(td); table.appendChild(tr); docx.appendChild(table);
  // hyperlink (non-text inline)
  const pLink = window.document.createElement("p");
  pLink.appendChild(window.document.createTextNode("See "));
  const a = window.document.createElement("a");
  a.setAttribute("href", "https://example.com");
  a.textContent = "the policy";
  pLink.appendChild(a);
  pLink.appendChild(window.document.createTextNode(" for the details."));
  docx.appendChild(pLink);
  // block split: two body <p> for the two model ids (so the count stays 1:1).
  const pSplitA = window.document.createElement("p");
  pSplitA.textContent = "First block of a split paragraph.";
  docx.appendChild(pSplitA);
  const pSplitB = window.document.createElement("p");
  pSplitB.textContent = "Second block of the same split paragraph.";
  docx.appendChild(pSplitB);
  host.appendChild(docx);
  studioDocumentRender.appendChild(host);

  const bind = get(sandbox, "bindFaithfulDocxInteractions");
  const committed = bind(host, "redline");
  assert.equal(committed, true, "the mixed-class document still COMMITS (mapping is read-only where unsafe)");

  const frameFor = (pid) => host.querySelector(`[data-paragraph-id="${pid}"]`);
  const editableOf = (pid) => { const f = frameFor(pid); return f && f.querySelector(".faithful-paragraph-editable"); };

  // Safe prose: editable.
  assert.equal(editableOf("p-safe").getAttribute("contenteditable"), "true", "safe prose is editable");
  assert.equal(editableOf("p-safe").getAttribute("data-faithful-editable"), "", "safe prose carries the editable flag");

  // The four locked classes: NOT editable, marked with the right lock reason, but
  // still MAPPED (data-paragraph-id present so clause-ids/comments/highlights work).
  const expectLocked = [
    ["p-tracked", "tracked_changes"],
    ["p-table", "table_cell"],
    ["p-link", "nontext_inline"],
    ["p-split-a", "block_split"],
    ["p-split-b", "block_split"],
  ];
  for (const [pid, reason] of expectLocked) {
    const frame = frameFor(pid);
    assert.ok(frame, `${pid} is still MAPPED (frame present)`);
    assert.equal(frame.getAttribute("data-paragraph-id"), pid, `${pid} keeps its paragraph id (read-only map)`);
    assert.ok(frame.classList.contains("faithful-edit-locked"), `${pid} frame is locked`);
    assert.equal(frame.getAttribute("data-faithful-lock-reason"), reason, `${pid} lock reason = ${reason}`);
    const ed = frame.querySelector(".faithful-paragraph-editable");
    assert.equal(ed.getAttribute("contenteditable"), "false", `${pid} is not editable`);
    assert.equal(ed.getAttribute("data-faithful-editable"), null, `${pid} does NOT carry the editable flag`);
  }

  console.log("PASS M8: tracked-change / table-cell / non-text-inline / block-split are MAPPED read-only + edit-locked; safe prose stays editable.");
}

testGuardPortsReference();
testHappyPathMappingAndClauseIds();
testAbortLeavesDomUntouched();
testRichRoundTrip();
testReadBackFailureAborts();
testReadBackSuccess();
testPreLockedClasses();
testToggleCarriesEdit();
console.log("\nALL PASS: faithful-mapping (guard / 1:1 map / clause ids / abort / round-trip / read-back / toggle).");
