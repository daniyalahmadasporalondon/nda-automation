// Render-side lock for merged / nested table support in the EDITABLE
// Redline/Clean reconstruction (the CSS-grid renderer in redline-rendering.js).
// The faithful "Original" surface already renders w:gridSpan / w:vMerge / nested
// <w:tbl> geometry (source-fidelity-table-spans.mjs); this proves the EDITABLE
// grid now matches that fidelity while keeping the round-trip (each cell keeps its
// data-editable-paragraph-id) and a byte-identical plain-table path.
//
// Drives the REAL shipped redline-rendering.js via vm:
//   (a) horizontal merge (col_span) -> a cell spanning 2 grid columns;
//   (b) vertical merge (row_span)    -> a cell spanning 2 grid rows, the vMerge
//       "continue" cell FOLDED away (no stray empty cell), and the next real cell
//       in the continuation row pushed to the correct column by the reserved slot;
//   (c) nested table                 -> a studio-doc-table INSIDE the parent cell,
//       in document order after the parent cell's own paragraph;
//   (d) plain table                  -> BYTE-IDENTICAL to the legacy renderReviewTable
//       (regression guard): no --spanned class, no explicit grid placement;
//   (e) round-trip                   -> every cell paragraph renders exactly once
//       with its data-editable-paragraph-id (folded continuation excepted).

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

const STATIC_JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../static/js");

function loadRenderer() {
  const sandbox = {
    window: { RedlineEditContract },
    escapeHtml,
    joinClasses,
    mergeClauses,
    clauseStatus,
    renderDiffOperations,
    renderInlineToken,
    fullReplacementOperations,
    needsInlineSpace,
    console,
  };
  vm.createContext(sandbox);
  for (const file of ["config.js", "redline-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return sandbox;
}

const sandbox = loadRenderer();

// A deterministic cell renderer so the placement / grouping assertions are exact
// and independent of the full paragraph-frame chain. The real round-trip frame is
// covered separately via renderReviewDocument below.
sandbox.__stubRenderOne = (paragraph) => `<cell data-pid="${paragraph.id}">${paragraph.text}</cell>`;

function render(paragraphs) {
  sandbox.__paras = paragraphs;
  return String(vm.runInContext("renderReviewParagraphsWithTables(__paras, __stubRenderOne)", sandbox));
}
function renderLegacy(paragraphs) {
  sandbox.__paras = paragraphs;
  return String(vm.runInContext("renderReviewTable(__paras, __stubRenderOne)", sandbox));
}

const cell = (id, text, table) => ({ id, index: id, text, table });

// ---------------------------------------------------------------------------
// (a) Horizontal merge -> one cell spanning two grid columns.
const colspan = [
  cell("h", "Header", { table_index: 1, row_index: 1, cell_index: 1, col_span: 2 }),
  cell("l", "Left", { table_index: 1, row_index: 2, cell_index: 1 }),
  cell("r", "Right", { table_index: 1, row_index: 2, cell_index: 2 }),
];
const colspanHtml = render(colspan);
assert.ok(colspanHtml.includes("studio-doc-table--spanned"), "colspan run must use the span-aware renderer");
assert.ok(colspanHtml.includes("--studio-table-cols:2"), `expected 2 columns in: ${colspanHtml}`);
assert.ok(
  colspanHtml.includes('style="grid-column:1 / span 2;grid-row:1 / span 1"'),
  `header must span 2 columns: ${colspanHtml}`,
);
assert.ok(colspanHtml.includes('style="grid-column:1 / span 1;grid-row:2 / span 1"'), "Left at col 1 row 2");
assert.ok(colspanHtml.includes('style="grid-column:2 / span 1;grid-row:2 / span 1"'), "Right at col 2 row 2");
assert.ok(colspanHtml.includes("Header") && colspanHtml.includes("Left") && colspanHtml.includes("Right"));

// ---------------------------------------------------------------------------
// (b) Vertical merge -> one cell spanning two grid rows; the vMerge "continue"
// cell is folded away and the next real cell in row 2 is pushed to column 2.
const rowspan = [
  cell("A", "Merged down", { table_index: 1, row_index: 1, cell_index: 1, row_span: 2 }),
  cell("B", "R1C2", { table_index: 1, row_index: 1, cell_index: 2 }),
  cell("cont", "", { table_index: 1, row_index: 2, cell_index: 1, v_merge: "continue" }),
  cell("C", "R2C2", { table_index: 1, row_index: 2, cell_index: 2 }),
];
const rowspanHtml = render(rowspan);
assert.ok(
  rowspanHtml.includes('style="grid-column:1 / span 1;grid-row:1 / span 2"'),
  `merged cell must span 2 rows: ${rowspanHtml}`,
);
// The continuation cell is folded: never rendered as its own cell.
assert.ok(!rowspanHtml.includes('data-pid="cont"'), "vMerge continuation cell must be folded away");
// C lands at column 2 because A's rowspan reserved (row2,col1).
assert.ok(
  rowspanHtml.includes('style="grid-column:2 / span 1;grid-row:2 / span 1"'),
  `continuation-row cell must be pushed to col 2: ${rowspanHtml}`,
);
assert.ok(rowspanHtml.includes("--studio-table-cols:2"), "vmerge table has 2 columns");

// ---------------------------------------------------------------------------
// (c) Nested table -> a studio-doc-table inside the parent cell, in document order.
const nested = [
  cell("o", "Outer before", { table_index: 1, row_index: 1, cell_index: 1 }),
  cell("ia", "Inner A", {
    table_index: 2, row_index: 1, cell_index: 1,
    parent: { table_index: 1, row_index: 1, cell_index: 1 },
  }),
  cell("ib", "Inner B", {
    table_index: 2, row_index: 1, cell_index: 2,
    parent: { table_index: 1, row_index: 1, cell_index: 1 },
  }),
];
const nestedHtml = render(nested);
const outerCellOpen = nestedHtml.indexOf("studio-doc-table--spanned");
const outerBeforeIndex = nestedHtml.indexOf("Outer before");
const nestedTableIndex = nestedHtml.indexOf("studio-doc-table--spanned", outerBeforeIndex);
assert.ok(outerCellOpen >= 0 && outerBeforeIndex > outerCellOpen, "outer cell text renders inside the outer grid");
assert.ok(nestedTableIndex > outerBeforeIndex, "nested table renders AFTER the outer cell paragraph, inside the cell");
assert.ok(nestedHtml.includes("Inner A") && nestedHtml.includes("Inner B"), "nested cells render");
// The whole nested structure is ONE grouped run (shared root table_index 1).
assert.equal((nestedHtml.match(/<cell /g) || []).length, 3, "o, ia, ib each render exactly once");

// ---------------------------------------------------------------------------
// (d) Plain table -> byte-identical to the legacy renderer (regression guard).
const plain = [
  cell("x", "One", { table_index: 3, row_index: 1, cell_index: 1 }),
  cell("y", "Two", { table_index: 3, row_index: 1, cell_index: 2 }),
];
const plainHtml = render(plain);
assert.equal(plainHtml, renderLegacy(plain), "plain table must render byte-identical to renderReviewTable");
assert.ok(!plainHtml.includes("--spanned"), "plain table never uses the span-aware renderer");
assert.ok(!plainHtml.includes("grid-column:"), "plain table never emits explicit grid placement");
// The legacy renderer keeps its own (1-based cell_index) column-count convention;
// the byte-identity assert above is the real regression guard. Just confirm the
// grid variable is still emitted (the grid is preserved, not flattened).
assert.ok(plainHtml.includes("--studio-table-cols:"), "plain table keeps its grid");

// A plain table interleaved between prose stays a single grouped run.
const withProse = [
  { id: "p0", index: "p0", text: "Intro" },
  ...plain,
  { id: "p1", index: "p1", text: "Outro" },
];
const withProseHtml = render(withProse);
assert.ok(withProseHtml.includes("Intro") && withProseHtml.includes("Outro"));
// One grid wrapper (--studio-table-cols appears once per table, not per cell).
assert.equal((withProseHtml.match(/--studio-table-cols:/g) || []).length, 1, "exactly one plain table grid");

// ---------------------------------------------------------------------------
// (e) Round-trip: drive the REAL renderReviewDocument so each cell paragraph
// keeps its editable hook, and the folded continuation cell is absent.
sandbox.__doc = {
  clauses: [],
  originalParagraphs: rowspan.map((p) => ({ id: p.id, text: p.text })),
  paragraphs: rowspan,
  comments: [],
  redlines: [],
  selectedClauseId: null,
  viewMode: "redline",
};
const docHtml = String(vm.runInContext("renderReviewDocument(__doc)", sandbox));
for (const id of ["A", "B", "C"]) {
  const hooks = docHtml.split(`data-editable-paragraph-id="${id}"`).length - 1;
  assert.equal(hooks, 1, `paragraph ${id} must render exactly one editable hook (round-trip): got ${hooks}`);
}
assert.ok(!docHtml.includes('data-editable-paragraph-id="cont"'), "folded continuation cell has no editable hook");
assert.ok(docHtml.includes("studio-doc-table--spanned"), "renderReviewDocument uses the span-aware grid for merges");
assert.ok(docHtml.includes("grid-row:1 / span 2"), "merged cell spans two rows in the real document render");

// ---------------------------------------------------------------------------
// Generator-surface parameterization: the shared grid renderer accepts the
// generator's class names + cols var (proves generator-editor can reuse it).
sandbox.__genModel = {
  table_index: 1,
  rows: [
    { row_index: 1, cells: [
      { cell_index: 1, col_span: 2, row_span: 1, items: [{ kind: "paragraph", paragraph: { id: "gh", text: "Gen header" } }] },
    ] },
    { row_index: 2, cells: [
      { cell_index: 1, col_span: 1, row_span: 1, items: [{ kind: "paragraph", paragraph: { id: "gl", text: "Gen L" } }] },
      { cell_index: 2, col_span: 1, row_span: 1, items: [{ kind: "paragraph", paragraph: { id: "gr", text: "Gen R" } }] },
    ] },
  ],
};
sandbox.__genRender = (p) => `<gp>${p.text}</gp>`;
const genHtml = String(vm.runInContext(
  'renderMergedTableGrid(__genModel, __genRender, { tableClass: "generator-doc-table", cellClass: "generator-doc-table-cell", colsVar: "gen-table-cols" })',
  sandbox,
));
assert.ok(genHtml.includes("generator-doc-table generator-doc-table--spanned"), "generator table class threads through");
assert.ok(genHtml.includes("--gen-table-cols:2"), "generator cols var threads through");
assert.ok(genHtml.includes('class="generator-doc-table-cell"'), "generator cell class threads through");
assert.ok(genHtml.includes("grid-column:1 / span 2"), "generator header spans two columns");

console.log("review-table-merged-spans: all assertions passed");
