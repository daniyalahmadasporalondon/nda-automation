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
// M1: the GUARD is now ORDERED TEXT ALIGNMENT (deliberate contract change).
//
// !!! CONTRACT CHANGE (2026-07-10) !!!
// The old guard ported /tmp/drift/final_guard.mjs: count-exact with tolerance 0.
// That aborted for essentially EVERY real document (live evidence: the Moorwand
// NDA rendered 81 blocks vs 43 review paragraphs -- 37 blank <w:p> spacers, one
// "." straggler, wrapped-line splits, filled-in insertions). The guard now aligns
// each structured paragraph to a CONTIGUOUS RUN of rendered blocks by ordered
// token matching (mirroring the backend review_document.align_document_paragraphs
// find-from-cursor semantics), skipping blank/decoration blocks between runs.
// COUNT MISMATCH WITH ALIGNABLE TEXT NOW COMMITS. What still aborts (fail-closed):
// a structured body paragraph whose tokens cannot be found in order (the M4b
// anti-mis-attach contract).
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

  // COUNT MISMATCH WITH ALIGNABLE TEXT -> COMMIT (the deliberate inversion of the
  // old tolerance-0 rule; the extra rendered block is skippable furniture).
  assert.equal(guard(["a", "b", "c"], S(["a", "b"])), true,
    "CONTRACT CHANGE: count mismatch with alignable text now COMMITS (trailing furniture skipped)");
  // Blank rendered spacers between real paragraphs -> COMMIT (the dominant live cause).
  assert.equal(guard(["a", "", "b", ""], S(["a", "b"])), true,
    "blank <w:p> spacers are skipped, not counted");
  // A structured paragraph with NO rendered counterpart is still fail-closed.
  assert.equal(guard(["a"], S(["a", "b"])), false,
    "a structured paragraph whose text is absent from the surface must abort");

  // checksum drift: a genuinely different paragraph (neither subsequence) -> ABORT.
  // This is the boilerplate mis-attach trap: a prefix/substring check would wrongly
  // pass these as "close enough"; the ordered-token-subsequence check (now applied
  // per aligned RUN) rejects them.
  assert.equal(
    guard(
      ["The Receiving Party shall keep all information confidential at all times."],
      S(["The Disclosing Party may share information with its affiliates as needed."]),
    ),
    false,
    "divergent paragraph must abort (no silent mis-attach)",
  );

  console.log("PASS M1: guard = ordered text alignment (furniture/blanks skipped commit; missing/divergent text aborts).");
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
// M4: alignment commit/abort contracts.
//
// !!! M4a CONTRACT CHANGE (2026-07-10) -- DELIBERATE INVERSION !!!
// M4a used to assert that a COUNT MISMATCH MUST ABORT (the old tolerance-0
// guard). That rule refused essentially every real document (see M1's banner:
// Moorwand rendered=81 vs structured=43). M4a now asserts the OPPOSITE on
// purpose: a count mismatch whose text IS alignable in order COMMITS, the
// unmatched extra block is left unstamped (no interactions on unattributable
// text). M4b (boilerplate/checksum drift must still refuse) is UNCHANGED, and
// M4c pins the fail-closed floor: a structured body paragraph with no rendered
// counterpart still aborts without touching the DOM.
// ===========================================================================
function testAlignmentCommitAndAbortContracts() {
  const reviewParagraphs = [
    { id: "p1", index: 0, source_index: 0, text: "The Receiving Party shall keep all information confidential." },
    { id: "p2", index: 1, source_index: 1, text: "This Agreement is governed by the laws of England and Wales." },
  ];
  // (a) COUNT mismatch with alignable text: 3 rendered vs 2 structured -> COMMITS.
  {
    const { sandbox, window } = freshSandbox({ reviewParagraphs });
    const host = buildRenderedHost(window, [
      "The Receiving Party shall keep all information confidential.",
      "This Agreement is governed by the laws of England and Wales.",
      "An extra rendered paragraph with no structured match.",
    ]);
    const bind = get(sandbox, "bindFaithfulDocxInteractions");
    const committed = bind(host, "redline");
    assert.equal(committed, true,
      "CONTRACT CHANGE: count-mismatch-with-alignable-text must now COMMIT (was: must abort)");
    const ps = Array.from(host.querySelectorAll(".docx p"));
    assert.equal(ps[0].getAttribute("data-paragraph-id"), "p1", "first paragraph anchors to p1");
    assert.equal(ps[1].getAttribute("data-paragraph-id"), "p2", "second paragraph anchors to p2");
    assert.equal(ps[2].getAttribute("data-paragraph-id"), null,
      "the extra (unattributable) rendered block is left UNSTAMPED -- no interactions on furniture");
    assert.equal(ps[2].querySelector(".faithful-paragraph-editable"), null,
      "the extra rendered block is never made editable");
  }
  // (b) CHECKSUM drift: same count, one paragraph diverges (boilerplate mis-attach
  // trap) -> still ABORTS with the DOM untouched. THE M4b CONTRACT SURVIVES: the
  // per-run ordered-token-subsequence verification refuses to bind divergent text.
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
  // (c) GENUINE MISMATCH: a structured body paragraph whose text is absent from the
  // rendered surface (in order) -> ABORT, DOM untouched. (The read-only tracked
  // fallback this abort now routes to is asserted in
  // tests/frontend/faithful-redline-clean-upgrade.mjs, which drives the full
  // maybeUpgradeSurfaceToFaithfulDocx -> attemptFaithfulRedlineFallback path.)
  {
    const { sandbox, window } = freshSandbox({ reviewParagraphs });
    const host = buildRenderedHost(window, [
      "The Receiving Party shall keep all information confidential.",
      // p2's governing-law text is nowhere on the surface.
    ]);
    const bind = get(sandbox, "bindFaithfulDocxInteractions");
    const committed = bind(host, "redline");
    assert.equal(committed, false, "a structured body paragraph with no rendered counterpart must abort");
    host.querySelectorAll(".docx p").forEach((p) => {
      assert.equal(p.getAttribute("data-paragraph-id"), null, "ABORT must not stamp any paragraph");
      assert.equal(p.getAttribute("contenteditable"), null, "ABORT must not make anything editable");
    });
  }
  console.log("PASS M4: alignable count-mismatch COMMITS (extra block unstamped); "
    + "checksum-drift + missing-body-text still ABORT with the DOM untouched.");
}

// ===========================================================================
// M9 (NEW): 2:1 wrapped-line split -- every structured paragraph's text spans TWO
// rendered blocks (the pdf2docx one-<w:p>-per-visual-line shape). The alignment
// must COMMIT, anchor BOTH blocks of each run to the right paragraph, keep the
// runs monotonic (never crossing), and edit-lock multi-block runs (run_split) --
// an edit inside one wrapped line cannot be attributed to the whole paragraph.
// ===========================================================================
function testWrappedLineRuns() {
  const reviewParagraphs = [
    { id: "p1", index: 0, source_index: 0, text: "The Receiving Party shall keep all Confidential Information secret and shall not disclose it to any third party." },
    { id: "p2", index: 1, source_index: 1, text: "This Agreement is governed by the laws of England and Wales and the courts of London have exclusive jurisdiction." },
  ];
  const reviewClauses = [
    { id: "c-conf", clause_type: "confidentiality", matched_paragraph_ids: ["p1"], status: "review" },
    { id: "c-gov", clause_type: "governing_law", matched_paragraph_ids: ["p2"], status: "pass" },
  ];
  const { sandbox, window } = freshSandbox({ reviewParagraphs, reviewClauses });
  const host = buildRenderedHost(window, [
    "The Receiving Party shall keep all Confidential Information",
    "secret and shall not disclose it to any third party.",
    "This Agreement is governed by the laws of England and Wales",
    "and the courts of London have exclusive jurisdiction.",
  ]);
  const bind = get(sandbox, "bindFaithfulDocxInteractions");
  assert.equal(bind(host, "redline"), true, "2:1 wrapped-line split must COMMIT");
  const ps = Array.from(host.querySelectorAll(".docx p"));
  // data-paragraph-id attaches to the run's PRIMARY block ONLY (first-match
  // querySelector consumers must never split from the anchors: exactly ONE
  // data-paragraph-id per structured paragraph across the whole surface).
  assert.deepEqual(
    ps.map((p) => p.getAttribute("data-paragraph-id")),
    ["p1", null, "p2", null],
    "each run anchors its PRIMARY block; later blocks carry no duplicate paragraph id",
  );
  // Clause interactions attach to EVERY block of the run: clicking the second
  // wrapped line still selects the paragraph's clause (no dead zones mid-paragraph).
  assert.equal(ps[0].getAttribute("data-clause-ids"), "c-conf");
  assert.equal(ps[1].getAttribute("data-clause-ids"), "c-conf", "clause hooks attach to the run's later blocks too");
  assert.equal(ps[3].getAttribute("data-clause-ids"), "c-gov");
  ps[1].dispatchEvent(new window.Event("click", { bubbles: true }));
  assert.equal(sandbox.__selectedClauseId, "c-conf", "clicking a run's second block selects the run's clause");
  // Multi-block runs are mapped READ-ONLY (run_split): per-block editing cannot be
  // attributed back to the single model paragraph (typing in one block would sync
  // ONLY that block's text over the whole paragraph -> exported redline deletes
  // the other half). No data-editable-paragraph-id anywhere in a run.
  ps.forEach((p, i) => {
    assert.ok(p.classList.contains("faithful-edit-locked"), `block ${i} of a wrapped run is edit-locked`);
    assert.equal(p.getAttribute("data-faithful-lock-reason"), "run_split", `block ${i} lock reason is run_split`);
    assert.equal(p.querySelector(".faithful-paragraph-editable").getAttribute("contenteditable"), "false");
  });
  assert.equal(host.querySelectorAll("[data-editable-paragraph-id]").length, 0,
    "no block of a multi-block run ever carries data-editable-paragraph-id");
  // Comment tools appear ONCE per paragraph (on the run's first block only).
  const toolCounts = ps.map((p) => p.querySelectorAll(".paragraph-comment-tools").length);
  assert.ok(toolCounts[0] <= 1 && toolCounts[1] === 0 && toolCounts[2] <= 1 && toolCounts[3] === 0,
    `comment tools only on each run's first block (got ${JSON.stringify(toolCounts)})`);
  console.log("PASS M9: 2:1 wrapped-line runs commit, anchor primary blocks, cover clicks, lock editing (run_split).");
}

// ===========================================================================
// M10 (NEW): empty-paragraph furniture -- blank <w:p> spacers between real
// paragraphs (the dominant live cause of the 81-vs-43 count mismatch). The
// alignment must COMMIT, skip every blank, and keep single-block real paragraphs
// FULLY EDITABLE (blanks must not degrade the mapping quality).
// ===========================================================================
function testEmptyParagraphFurniture() {
  const reviewParagraphs = [
    { id: "p1", index: 0, source_index: 0, text: "The Receiving Party shall keep all information confidential." },
    { id: "p2", index: 1, source_index: 1, text: "This Agreement is governed by the laws of England and Wales." },
  ];
  const { sandbox, window } = freshSandbox({ reviewParagraphs });
  const host = buildRenderedHost(window, [
    "",
    "The Receiving Party shall keep all information confidential.",
    "",
    "",
    "This Agreement is governed by the laws of England and Wales.",
    "",
  ]);
  const bind = get(sandbox, "bindFaithfulDocxInteractions");
  assert.equal(bind(host, "redline"), true, "blank-spacer furniture must COMMIT");
  const ps = Array.from(host.querySelectorAll(".docx p"));
  assert.deepEqual(
    ps.map((p) => p.getAttribute("data-paragraph-id")),
    [null, "p1", null, null, "p2", null],
    "blanks skipped and unstamped; real paragraphs anchored",
  );
  // Blanks never get an editable wrapper; the real single-block paragraphs stay
  // fully rich-editable (same as the old happy path).
  assert.equal(ps[0].querySelector(".faithful-paragraph-editable"), null, "blank block untouched");
  const p1Editable = ps[1].querySelector(".faithful-paragraph-editable");
  assert.equal(p1Editable.getAttribute("contenteditable"), "true", "single-block paragraph stays editable");
  assert.equal(p1Editable.getAttribute("data-editable-paragraph-id"), "p1");
  console.log("PASS M10: blank <w:p> spacers are skipped unstamped; real paragraphs stay editable.");
}

// ===========================================================================
// M11 (NEW): TRUE shared block -- one rendered block contains SEVERAL structured
// paragraphs' text (the extractor split one physical <w:p>; block-split lock
// semantics preserved). The unit commits, the block anchors on the FIRST member,
// carries the UNION of the members' clause ids, and is edit-locked block_split.
// ===========================================================================
function testSharedBlockUnit() {
  const reviewParagraphs = [
    // Both members share source_index 0: one physical <w:p> split by the extractor.
    { id: "p-a", index: 0, source_index: 0, text: "First half of the split block about confidentiality." },
    { id: "p-b", index: 1, source_index: 0, text: "Second half of the split block about governing law." },
    { id: "p-c", index: 2, source_index: 1, text: "A following ordinary paragraph." },
  ];
  const reviewClauses = [
    { id: "c-a", clause_type: "confidentiality", matched_paragraph_ids: ["p-a"], status: "review" },
    { id: "c-b", clause_type: "governing_law", matched_paragraph_ids: ["p-b"], status: "pass" },
  ];
  const { sandbox, window } = freshSandbox({ reviewParagraphs, reviewClauses });
  const host = buildRenderedHost(window, [
    // ONE rendered block carrying BOTH members' text, then the ordinary paragraph.
    "First half of the split block about confidentiality. Second half of the split block about governing law.",
    "A following ordinary paragraph.",
  ]);
  const bind = get(sandbox, "bindFaithfulDocxInteractions");
  assert.equal(bind(host, "redline"), true, "a shared block (block-split unit) must COMMIT");
  const ps = Array.from(host.querySelectorAll(".docx p"));
  assert.equal(ps[0].getAttribute("data-paragraph-id"), "p-a", "shared block anchors on the FIRST member");
  const sharedClauseIds = String(ps[0].getAttribute("data-clause-ids") || "").split(" ").sort();
  assert.deepEqual(sharedClauseIds, ["c-a", "c-b"], "shared block carries the UNION of the members' clause ids");
  assert.ok(ps[0].classList.contains("faithful-edit-locked"), "shared block is edit-locked");
  assert.equal(ps[0].getAttribute("data-faithful-lock-reason"), "block_split", "lock reason is block_split");
  assert.equal(ps[1].getAttribute("data-paragraph-id"), "p-c", "the following paragraph still anchors normally");
  assert.equal(ps[1].querySelector(".faithful-paragraph-editable").getAttribute("contenteditable"), "true");
  console.log("PASS M11: true shared block anchors on first member, unions clause ids, locks block_split.");
}

// ===========================================================================
// M13 (NEW): adversarial gate probes (scratchpad mapping-gate-kit attack matrix).
// Each sub-probe traces a specific way a text aligner can silently mis-bind; the
// asserted outcomes are the DELIBERATE deterministic choices, all of them
// interaction-conservative (ambiguously-owned blocks are read-only).
// ===========================================================================
function testAdversarialGateProbes() {
  const bindHost = (reviewParagraphs, reviewClauses, renderedTexts, hostOptions) => {
    const { sandbox, window } = freshSandbox({ reviewParagraphs, reviewClauses: reviewClauses || [] });
    const host = buildRenderedHost(window, renderedTexts, hostOptions || {});
    const committed = get(sandbox, "bindFaithfulDocxInteractions")(host, "redline");
    return { sandbox, window, host, committed };
  };
  const idsOf = (host) => Array.from(host.querySelectorAll(".docx p"))
    .filter((el) => !el.closest("header,footer"))
    .map((el) => el.getAttribute("data-paragraph-id"));

  // --- A: twin signature blocks (repeated boilerplate) bind IN ORDER -------
  {
    const { sandbox, host, window, committed } = bindHost(
      [
        { id: "sig1", index: 0, source_index: 1, text: "Signed" },
        { id: "dat1", index: 1, source_index: 2, text: "Date" },
        { id: "sig2", index: 2, source_index: 3, text: "Signed __________________" },
        { id: "dat2", index: 3, source_index: 4, text: "Date __________________" },
      ],
      [
        { id: "cA", matched_paragraph_ids: ["sig1"] },
        { id: "cB", matched_paragraph_ids: ["dat1"] },
        { id: "cC", matched_paragraph_ids: ["sig2"] },
        { id: "cD", matched_paragraph_ids: ["dat2"] },
      ],
      ["Signed ", "", "Date", "Signed __________________", "", "Date __________________"],
    );
    assert.equal(committed, true, "A: twin signature blocks commit");
    assert.deepEqual(idsOf(host), ["sig1", null, "dat1", "sig2", null, "dat2"],
      "A: repeated boilerplate binds strictly in order (never the later twin)");
    const ps = Array.from(host.querySelectorAll(".docx p"));
    ps[5].dispatchEvent(new window.Event("click", { bubbles: true }));
    assert.equal(sandbox.__selectedClauseId, "cD", "A: clicking the SECOND Date line selects dat2's clause, never dat1's");
  }

  // --- B1: run boundary must not overrun the next paragraph's home ---------
  {
    const { host, committed } = bindHost(
      [
        { id: "sA", index: 0, source_index: 1, text: "Alpha beta" },
        { id: "sB", index: 1, source_index: 2, text: "beta gamma" },
      ],
      [],
      ["Alpha", "beta", "beta gamma"],
    );
    assert.equal(committed, true, "B1: commits");
    assert.deepEqual(idsOf(host), ["sA", null, "sB"],
      "B1: sA's run stops at its own tokens (r0,r1); sB keeps its home (r2)");
  }

  // --- B2: identical twins bind in order ------------------------------------
  {
    const { host, committed } = bindHost(
      [
        { id: "sA", index: 0, source_index: 1, text: "Signed" },
        { id: "sB", index: 1, source_index: 2, text: "Signed" },
      ],
      [
        { id: "cA", matched_paragraph_ids: ["sA"] },
        { id: "cB", matched_paragraph_ids: ["sB"] },
      ],
      ["Signed", "Signed"],
    );
    assert.equal(committed, true, "B2: commits");
    assert.deepEqual(idsOf(host), ["sA", "sB"], "B2: identical twins bind in document order");
    const ps = Array.from(host.querySelectorAll(".docx p"));
    assert.equal(ps[0].getAttribute("data-clause-ids"), "cA");
    assert.equal(ps[1].getAttribute("data-clause-ids"), "cB");
  }

  // --- B3: ambiguous shifted partition -> DETERMINISTIC + conservative ------
  // rendered ["Signed","Signed","Date"] vs structured ["Signed","Date"] admits two
  // complete monotonic partitions (who owns r1?). There is no in-band ground
  // truth. The DELIBERATE deterministic rule: after a unit completes, contiguous
  // non-blank blocks that could NOT start the next unit are absorbed into ITS run
  // (same rule that binds filled-in insertion lines). So s1 owns [r0,r1] -- and
  // the ambiguity is bound INTERACTION-CONSERVATIVELY: the whole run is
  // edit-locked (run_split), so no edit can ever be attributed through the guess.
  {
    const { host, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Signed" },
        { id: "s2", index: 1, source_index: 2, text: "Date" },
      ],
      [],
      ["Signed", "Signed", "Date"],
    );
    assert.equal(committed, true, "B3: commits deterministically");
    assert.deepEqual(idsOf(host), ["s1", null, "s2"], "B3: deterministic ownership (s1 absorbs the stray twin)");
    const ps = Array.from(host.querySelectorAll(".docx p"));
    assert.equal(ps[0].getAttribute("data-faithful-lock-reason"), "run_split",
      "B3: the ambiguously-extended run is read-only (never editable through a guess)");
    assert.equal(ps[1].getAttribute("data-faithful-lock-reason"), "run_split");
    assert.equal(host.querySelectorAll('[data-editable-paragraph-id="s1"]').length, 0,
      "B3: no editable hook anywhere on the ambiguous run");
  }

  // --- C: one rendered block holding TWO distinct-si paragraphs -> ABORT ----
  // sB's home was consumed inside sA's block; committing would double-bind or
  // orphan sB. Genuine fail-closed abort, zero stamps.
  {
    const { host, committed } = bindHost(
      [
        { id: "sA", index: 0, source_index: 1, text: "Signed" },
        { id: "sB", index: 1, source_index: 2, text: "Date" },
      ],
      [],
      ["Signed Date"],
    );
    assert.equal(committed, false, "C: distinct-si paragraphs merged into one block must ABORT");
    host.querySelectorAll(".docx p").forEach((p) => {
      assert.equal(p.getAttribute("data-paragraph-id"), null, "C: abort leaves zero stamps");
    });
  }
  // C2 (tab-merged signature line, 3-into-1): same fail-closed outcome.
  {
    const { committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Authorised Signatory" },
        { id: "s2", index: 1, source_index: 2, text: "Name" },
        { id: "s3", index: 2, source_index: 3, text: "Position/Title" },
      ],
      [],
      ["Authorised Signatory Name Position/Title"],
    );
    assert.equal(committed, false, "C2: 3 distinct-si paragraphs in one block must ABORT");
  }

  // --- D: empty / whitespace-only structured paragraphs are zero-width ------
  {
    const { host, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Alpha" },
        { id: "s2", index: 1, source_index: 2, text: "" },
        { id: "s3", index: 2, source_index: 3, text: "Gamma" },
      ],
      [],
      ["Alpha", "Gamma"],
    );
    assert.equal(committed, true, "D: empty structured paragraph commits (zero-width)");
    assert.deepEqual(idsOf(host), ["s1", "s3"], "D: the empty paragraph steals nothing; s3 keeps Gamma");
    assert.equal(host.querySelector('[data-paragraph-id="s2"]'), null, "D: the empty paragraph binds NOTHING");
  }
  {
    const { host, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Alpha" },
        { id: "s2", index: 1, source_index: 2, text: "  \n " },
        { id: "s3", index: 2, source_index: 3, text: "Gamma" },
      ],
      [],
      ["Alpha", "Gamma"],
    );
    assert.equal(committed, true, "D: whitespace-only structured paragraph behaves identically");
    assert.equal(host.querySelector('[data-paragraph-id="s2"]'), null);
  }

  // --- E: interior blank inside a wrapped run (contiguity modulo furniture) -
  {
    const { host, committed } = bindHost(
      [
        { id: "sA", index: 0, source_index: 1, text: "Alpha beta" },
        { id: "sB", index: 1, source_index: 2, text: "Gamma" },
      ],
      [{ id: "cA", matched_paragraph_ids: ["sA"] }],
      ["Alpha", "", "beta", "", "Gamma", ""],
    );
    assert.equal(committed, true, "E: interior blank must not break a wrapped run (Moorwand has 37 blanks)");
    assert.deepEqual(idsOf(host), ["sA", null, null, null, "sB", null], "E: primary anchors only");
    const ps = Array.from(host.querySelectorAll(".docx p"));
    assert.equal(ps[2].getAttribute("data-clause-ids"), "cA", "E: the run's second half keeps sA's clause hooks");
    // Blanks: never stamped, never editable, never clause targets.
    [1, 3, 5].forEach((i) => {
      assert.equal(ps[i].getAttribute("data-paragraph-id"), null, `E: blank ${i} unstamped`);
      assert.equal(ps[i].getAttribute("data-clause-ids"), null, `E: blank ${i} carries no clause hooks`);
      assert.equal(ps[i].querySelector("[contenteditable]"), null, `E: blank ${i} never editable`);
    });
  }
  // E-punctuation: furniture-skip is LAZY -- a "." block is skipped only when no
  // structured paragraph claims it; a REAL punctuation-only model paragraph binds.
  {
    const { host, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Alpha" },
        { id: "s2", index: 1, source_index: 2, text: "." },
        { id: "s3", index: 2, source_index: 3, text: "Gamma" },
      ],
      [],
      ["Alpha", ".", "Gamma"],
    );
    assert.equal(committed, true, "E-punct: a real punctuation-only model paragraph still maps");
    assert.deepEqual(idsOf(host), ["s1", "s2", "s3"], "E-punct: '.' binds when claimed, skips when not");
  }

  // --- G2: body paragraph (HAS si) whose text only exists in the footer ------
  // A paragraph with a valid source_index is NEVER waivable: its body home is
  // genuinely absent, so the mapping must ABORT (fail-closed), footer or not.
  {
    const { host, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Alpha" },
        { id: "s2", index: 1, source_index: 2, text: "Moorwand Ltd" },
        { id: "s3", index: 2, source_index: 3, text: "Gamma" },
      ],
      [],
      ["Alpha", "Gamma"],
      { footerText: "Moorwand Ltd | Registered office address: Fora, 3 Lloyds Avenue" },
    );
    assert.equal(committed, false,
      "G2: a source_index paragraph missing from the body ABORTS even when its text appears in the footer");
    host.querySelectorAll(".docx p").forEach((p) => {
      assert.equal(p.getAttribute("data-paragraph-id"), null, "G2: abort leaves zero stamps (footer too)");
    });
  }

  // --- G3: a no-si footer paragraph must not steal a body twin ---------------
  {
    const { sandbox, host, window, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Alpha" },
        { id: "s2", index: 1, source_index: 2, text: "Moorwand Limited" },
        { id: "f1", index: 2, text: "Moorwand Limited" }, // footer: NO source_index
      ],
      [{ id: "cM", matched_paragraph_ids: ["s2"] }],
      ["Alpha", "Moorwand Limited"],
      { footerText: "Moorwand Limited" },
    );
    assert.equal(committed, true, "G3: commits");
    assert.deepEqual(idsOf(host), ["s1", "s2"], "G3: the BODY paragraph owns the body block");
    assert.equal(host.querySelector('[data-paragraph-id="f1"]'), null, "G3: the footer paragraph binds nothing");
    const bodyBlock = host.querySelectorAll(".docx p")[1];
    bodyBlock.dispatchEvent(new window.Event("click", { bubbles: true }));
    assert.equal(sandbox.__selectedClauseId, "cM", "G3: clicking the body block selects s2's clause");
  }

  // --- G4: LOST-source_index body paragraph (the known lost-id tripwire) -----
  // A no-si paragraph whose text IS present among unconsumed body blocks is NOT a
  // footer -- waiving it would leave real body text silently unmapped. ABORT.
  {
    const { host, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Alpha" },
        { id: "s2", index: 1, text: "Beta body paragraph" }, // si LOST
        { id: "s3", index: 2, source_index: 3, text: "Gamma" },
      ],
      [],
      ["Alpha", "Beta body paragraph", "Gamma"],
    );
    assert.equal(committed, false,
      "G4: a lost-si paragraph whose text sits unconsumed in the body must ABORT, never be waived");
    host.querySelectorAll(".docx p").forEach((p) => {
      assert.equal(p.getAttribute("data-paragraph-id"), null, "G4: abort leaves zero stamps");
    });
  }

  // --- I: whole-paragraph insert with no structured counterpart --------------
  // DELIBERATE handling (documented): an unmatched rendered block CONTIGUOUS with
  // the previous paragraph is absorbed into that paragraph's (read-only) run; a
  // blank-separated one is skipped as furniture, unstamped and non-interactive.
  // (An all-<ins> tracked-insert paragraph takes the same two paths; if absorbed,
  // the tracked_changes lock also applies.) Either way it can never be edited and
  // never carries a WRONG paragraph id.
  {
    const { host, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Alpha" },
        { id: "s2", index: 1, source_index: 2, text: "Beta" },
      ],
      [{ id: "cA", matched_paragraph_ids: ["s1"] }],
      ["Alpha", "Wholly new inserted paragraph", "Beta"],
    );
    assert.equal(committed, true, "I-contiguous: commits");
    assert.deepEqual(idsOf(host), ["s1", null, "s2"], "I-contiguous: the insert joins s1's run (no own id)");
    const ps = Array.from(host.querySelectorAll(".docx p"));
    assert.equal(ps[1].getAttribute("data-faithful-lock-reason"), "run_split",
      "I-contiguous: the absorbed insert is read-only");
    assert.equal(ps[1].querySelector('[contenteditable="true"]'), null);
  }
  {
    const { host, committed } = bindHost(
      [
        { id: "s1", index: 0, source_index: 1, text: "Alpha" },
        { id: "s2", index: 1, source_index: 2, text: "Beta" },
      ],
      [],
      ["Alpha", "", "Wholly new inserted paragraph", "", "Beta"],
    );
    assert.equal(committed, true, "I-separated: commits");
    assert.deepEqual(idsOf(host), ["s1", null, null, null, "s2"],
      "I-separated: the blank-separated insert is furniture (unstamped)");
    const insertBlock = Array.from(host.querySelectorAll(".docx p"))[2];
    assert.equal(insertBlock.getAttribute("data-clause-ids"), null, "I-separated: no clause hooks on the insert");
    assert.equal(insertBlock.querySelector("[contenteditable]"), null, "I-separated: never editable");
  }

  console.log("PASS M13: adversarial gate probes (twin boilerplate, run boundaries, ambiguous "
    + "partitions locked, merged-block aborts, empty structured, blanks/punctuation, footer "
    + "waiver discriminator, lost-si tripwire, whole-paragraph inserts).");
}

// ===========================================================================
// M12 (NEW): THE MOORWAND FIXTURE -- the exact live-prod shape that aborted with
// `count_mismatch rendered=81 structured=43` for weeks. Encodes the real arrays'
// anatomy (extracted from Render prod 2026-07-10): 81 rendered blocks of which 37
// are blank <w:p> spacers and one is a punctuation-only "." straggler; 43 review
// paragraphs of which 3 are FOOTER lines with NO source_index (their text lives
// in the DOM's excluded <footer> region); one filled-in-value insertion run
// (i6 "and: Registered No.:..." -> "and: Vance Inc Registered No.:..." + a
// wrapped address line contributing ZERO structured tokens); two wrapped-line
// clause runs (i13/i14); signature-block insertions ("Parth Pramendra Garg",
// "CEO"). The alignment MUST COMMIT and anchor every named run correctly.
// ===========================================================================
function testMoorwandLiveFixture() {
  // ---- structured (43): body i1-i40 with source_index, footers i41-43 without.
  const body = [
    "MUTUAL CONFIDENTIALITY AND NON-DISCLOSURE AGREEMENT",
    '(the "Agreement")',
    "Made and entered into",
    "BETWEEN:",
    'Moorwand Limited (Registered No.08491211) with offices located at Fora, 3 Lloyds Avenue, London EC3N 3DS ("MOORWAND")',
    "and: Registered No.: with offices located at:",
    '("COMPANY").',
    'Effective as of the day of 2026 (the "Effective Date").',
    "WHEREAS, in the course of business discussions, COMPANY and MOORWAND may disclose confidential information to each other;",
    "WHEREAS, as a condition to such disclosure, each Party agrees to protect the other's confidential information;",
    "NOW, THEREFORE, IN CONSIDERATION of the mutual Agreements contained herein, the Parties agree as follows:",
    "Confidential Information means any tangible or intangible information disclosed by one Party to the other Party.",
    // i13: wrapped over TWO rendered lines (16+17).
    "Confidential Information does not include information that: (i) is already known to the Receiving Party; or (ii) is or later becomes generally available to the public through no fault of the Receiving",
    // i14: wrapped over TWO rendered lines (20+21).
    "Receiving Party; or (iii) Receiving Party develops independently without reference to the Confidential Information, provided that such independent development shall be on the Receiving Party's own time and record.",
    "The obligations in this Agreement shall not apply to any information that the Disclosing Party agrees in writing is free of such restrictions.",
    "The Receiving Party agrees (i) to adopt measures to protect the Confidential Information no less protective than for its own.",
    "The Receiving Party may use the Disclosing Party's Confidential Information solely for the Purpose.",
    "A Receiving Party may disclose Confidential Information if compelled by law, subject to notice requirements.",
    "The Parties shall promptly advise each other in writing of any misappropriation or",
    "unauthorised disclosure of Confidential Information by any person which may come to its attention.",
    "The Parties agree that, in the event of a breach of this Agreement, damages may not be an adequate remedy and equitable relief may be sought.",
    "The provisions of this Agreement shall remain in full force and effect for a period of five (5) years from the Effective Date.",
    "No Party may assign its rights under this Agreement without the prior written consent of the other Party.",
    "This Agreement expresses the entire agreement between the Parties with respect to its subject matter.",
    "Save and except as expressly provided in this Agreement, no rights are granted.",
    "This Agreement may be executed in one or more counterparts, each of which shall be deemed an original.",
    "Unless expressly provided in this Agreement, no term of this Agreement is enforceable by a person who is not a party to it.",
    "This Agreement and all matters arising from it shall be governed by the laws of England and Wales.",
    "In the event that any of the provisions of this Agreement are held to be unenforceable, the remainder shall continue in effect.",
    "This Agreement has been signed on the date appearing on page one.",
    "Moorwand Limited",
    "Signed",
    "Authorised Signatory Name Position/Title",
    "Date",
    "Luc Gueriane CEO",
    '("COMPANY").',
    "Signed __________________",
    "Authorised Signatory",
    "Position/Title",
    "Date __________________",
  ];
  const footerLine = "Moorwand Ltd | Registered office address: Fora, 3 Lloyds Avenue | London EC3N 3DS";
  const reviewParagraphs = [
    ...body.map((text, i) => ({ id: `i${i + 1}`, index: i, source_index: i, text })),
    // Footers: NO source_index (exactly as extracted from prod).
    { id: "i41", index: 40, text: footerLine },
    { id: "i42", index: 41, text: footerLine },
    { id: "i43", index: 42, text: footerLine },
  ];
  assert.equal(reviewParagraphs.length, 43, "fixture anatomy: 43 structured paragraphs");

  // ---- rendered (81): the docx-preview block stream, blanks and all.
  const rendered = [
    /* 0*/ body[0],
    /* 1*/ body[1],
    /* 2*/ body[2],
    /* 3*/ "",
    /* 4*/ body[3],
    /* 5*/ "",
    /* 6*/ body[4],
    // i6 with the FILLED-IN counterparty ("Vance Inc") + a wrapped address line
    // that contributes ZERO structured tokens (pure insertion block).
    /* 7*/ "and: Vance Inc Registered No.: with offices located at:",
    /* 8*/ " Office no. 1271 Register 08, 1000 N. West Street Suite 1200, Wilmington, Delaware 19801",
    /* 9*/ body[6],
    /*10*/ body[7],
    /*11*/ "",
    /*12*/ body[8],
    /*13*/ body[9],
    /*14*/ body[10],
    /*15*/ body[11],
    // i13 wrapped across two visual lines.
    /*16*/ "Confidential Information does not include information that: (i) is already known to the Receiving",
    /*17*/ "Party; or (ii) is or later becomes generally available to the public through no fault of the Receiving",
    /*18*/ "",
    /*19*/ "",
    // i14 wrapped across two visual lines.
    /*20*/ "Receiving Party; or (iii) Receiving Party develops independently without reference to the Confidential Information, provided that such",
    /*21*/ "independent development shall be on the Receiving Party's own time and record.",
    /*22*/ body[14],
    /*23*/ "",
    /*24*/ body[15],
    /*25*/ "",
    /*26*/ body[16],
    /*27*/ "",
    /*28*/ body[17],
    /*29*/ "",
    /*30*/ ".", // the live punctuation-only straggler
    /*31*/ "",
    /*32*/ body[18],
    /*33*/ "",
    /*34*/ "",
    /*35*/ body[19],
    /*36*/ "",
    /*37*/ body[20],
    /*38*/ "",
    /*39*/ body[21],
    /*40*/ body[22],
    /*41*/ "",
    /*42*/ body[23],
    /*43*/ "",
    /*44*/ body[24],
    /*45*/ "",
    /*46*/ "",
    /*47*/ body[25],
    /*48*/ "",
    /*49*/ body[26],
    /*50*/ "",
    /*51*/ "",
    /*52*/ body[27],
    /*53*/ "",
    /*54*/ "",
    /*55*/ body[28],
    /*56*/ "",
    /*57*/ "",
    /*58*/ "",
    /*59*/ body[29],
    /*60*/ "",
    /*61*/ body[30],
    /*62*/ "",
    /*63*/ body[31],
    /*64*/ "",
    /*65*/ body[32],
    /*66*/ body[33],
    /*67*/ body[34],
    /*68*/ "",
    /*69*/ "",
    /*70*/ "",
    /*71*/ body[35],
    /*72*/ "",
    /*73*/ body[36],
    /*74*/ "",
    // Signature-block insertions (filled-in name/title).
    /*75*/ "Authorised Signatory Parth Pramendra Garg ",
    /*76*/ "",
    /*77*/ "Position/Title CEO",
    /*78*/ "",
    /*79*/ "",
    /*80*/ body[39],
  ];
  assert.equal(rendered.length, 81, "fixture anatomy: 81 rendered blocks");
  assert.equal(rendered.filter((t) => t === "").length, 37, "fixture anatomy: 37 blank spacers");

  const reviewClauses = [
    { id: "c-gov", clause_type: "governing_law", matched_paragraph_ids: ["i28"], status: "pass" },
    { id: "c-parties", clause_type: "parties", matched_paragraph_ids: ["i6"], status: "review" },
  ];
  const { sandbox, window } = freshSandbox({ reviewParagraphs, reviewClauses });
  const host = buildRenderedHost(window, rendered, { footerText: footerLine });
  const bind = get(sandbox, "bindFaithfulDocxInteractions");
  const committed = bind(host, "redline");
  assert.equal(committed, true,
    "THE LIVE MOORWAND SHAPE MUST COMMIT (this exact document aborted count_mismatch 81/43 in prod)");

  const ps = Array.from(host.querySelectorAll(".docx p")).filter((el) => !el.closest("header,footer"));
  const idAt = (i) => ps[i].getAttribute("data-paragraph-id");
  // i5 -> ONE block, exact text -> stays editable.
  assert.equal(idAt(6), "i5", "i5 anchors its single block");
  // i6 -> a 2-block run: the filled-in line (primary) AND the wrapped
  // pure-insertion address line (clause hooks, no duplicate paragraph id).
  assert.equal(idAt(7), "i6", "i6 anchors its primary (filled-in insertion) line");
  assert.equal(idAt(8), null, "the absorbed address line carries NO duplicate paragraph id");
  assert.equal(ps[8].getAttribute("data-clause-ids"), "c-parties",
    "the absorbed address line still carries i6's clause hooks (click coverage)");
  ps[8].dispatchEvent(new window.Event("click", { bubbles: true }));
  assert.equal(sandbox.__selectedClauseId, "c-parties",
    "clicking the filled-in address line selects i6's clause (no dead zone)");
  // i6's run is multi-block AND insertion-tolerated -> read-only everywhere.
  assert.equal(ps[7].getAttribute("data-faithful-lock-reason"), "run_split", "i6's multi-block run is edit-locked");
  assert.equal(ps[7].querySelector(".faithful-paragraph-editable").getAttribute("contenteditable"), "false");
  assert.equal(idAt(9), "i7", "the paragraph AFTER the insertion run still anchors correctly");
  // i13/i14 -> wrapped 2-block runs (primary anchor + clause-hooked continuation).
  assert.equal(idAt(16), "i13");
  assert.equal(idAt(17), null, "i13's wrapped second line joins the run without a duplicate id");
  assert.ok(ps[17].classList.contains("studio-doc-paragraph"), "i13's second line is part of the mapped run");
  assert.equal(ps[17].getAttribute("data-faithful-lock-reason"), "run_split");
  assert.equal(idAt(20), "i14");
  assert.equal(ps[21].getAttribute("data-faithful-lock-reason"), "run_split",
    "i14's wrapped second line joins its run (locked)");
  // The governing-law clause lands on the governing-law paragraph (no mis-attach
  // across the 38 skipped furniture blocks).
  const govBlock = ps.find((p) => p.getAttribute("data-paragraph-id") === "i28");
  assert.ok(govBlock, "i28 (governing law) is mapped");
  assert.equal(govBlock.getAttribute("data-clause-ids"), "c-gov", "governing-law clause anchors on i28");
  assert.match(govBlock.textContent, /governed by the laws of England and Wales/,
    "i28's block really is the governing-law text");
  // Signature insertions bind to the right paragraphs -- but are EDIT-LOCKED
  // (text_drift): the rendered text carries filled-in tokens the model lacks, so
  // an in-place edit would sync the rendered variant over the model text.
  const signatory = ps.find((p) => p.getAttribute("data-paragraph-id") === "i38");
  assert.match(signatory.textContent, /Parth Pramendra Garg/, "i38 binds the filled-in signatory line");
  assert.equal(signatory.getAttribute("data-faithful-lock-reason"), "text_drift",
    "an insertion-tolerated single block is edit-locked (text_drift), never editable");
  assert.equal(signatory.querySelector(".faithful-paragraph-editable").getAttribute("contenteditable"), "false");
  const title = ps.find((p) => p.getAttribute("data-paragraph-id") === "i39");
  assert.match(title.textContent, /Position\/Title CEO/, "i39 binds the filled-in title line");
  assert.equal(title.getAttribute("data-faithful-lock-reason"), "text_drift", "i39 locked (text_drift)");
  // Every blank block and the "." straggler stay unstamped.
  rendered.forEach((text, i) => {
    if (text === "" || text === ".") {
      assert.equal(idAt(i), null, `furniture block ${i} (${JSON.stringify(text)}) must stay unstamped`);
      assert.equal(ps[i].getAttribute("data-clause-ids"), null,
        `furniture block ${i} carries no clause hooks`);
    }
  });
  // The FOOTER paragraphs (no source_index; text lives in the excluded <footer>)
  // are tolerated as unmatched: they bind NOTHING and must not abort the mapping.
  for (const fid of ["i41", "i42", "i43"]) {
    assert.equal(ps.find((p) => p.getAttribute("data-paragraph-id") === fid), undefined,
      `${fid} (footer) binds no body block`);
  }
  const footerEl = host.querySelector("footer p");
  assert.equal(footerEl.getAttribute("data-paragraph-id"), null, "the <footer> DOM itself is never stamped");
  // Exactly ONE data-paragraph-id per structured paragraph across the surface.
  const allIds = Array.from(host.querySelectorAll("[data-paragraph-id]"))
    .map((el) => el.getAttribute("data-paragraph-id"));
  assert.equal(allIds.length, new Set(allIds).size, "no duplicate data-paragraph-id anywhere");
  // Single-block exact prose stays editable (blanks must not degrade mapping quality).
  const prose = ps.find((p) => p.getAttribute("data-paragraph-id") === "i12");
  assert.equal(prose.querySelector(".faithful-paragraph-editable").getAttribute("contenteditable"), "true",
    "single-block exact prose stays rich-editable");

  console.log("PASS M12: the LIVE Moorwand 81-vs-43 shape commits with correct anchors "
    + "(blanks + '.' skipped; insertion + wrapped runs locked; footers tolerated unmatched).");
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
testAlignmentCommitAndAbortContracts();
testRichRoundTrip();
testReadBackFailureAborts();
testReadBackSuccess();
testPreLockedClasses();
testToggleCarriesEdit();
testWrappedLineRuns();
testEmptyParagraphFurniture();
testSharedBlockUnit();
testAdversarialGateProbes();
testMoorwandLiveFixture();
console.log("\nALL PASS: faithful-mapping (alignment guard / runs / clause ids / abort hygiene / "
  + "round-trip / read-back / toggle / gate probes / Moorwand live fixture).");
