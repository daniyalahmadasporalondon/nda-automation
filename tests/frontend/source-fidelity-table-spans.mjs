// Render-side lock for merged/nested table support in the source-fidelity
// reconstruction surface. Drives the REAL renderSourceFidelityTable (loaded via
// vm from static/js/review-workstation-rendering.js) and proves the structured
// table block from source_fidelity.py renders with:
//   (a) a horizontal merge  -> colspan on the header cell (w:gridSpan);
//   (b) a vertical merge     -> rowspan on the restart cell (w:vMerge);
//   (c) a nested table        -> a <table> inside the parent cell's <td>;
//   (d) a plain table         -> NO colspan/rowspan attributes (unchanged).

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const STATIC_JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../static/js");

// escapeHtml is a browser-provided global bridge (not defined in the module), so
// the sandbox supplies the same escaper the page uses.
function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function loadRenderer() {
  const sandbox = {
    escapeHtml,
    console,
    document: {},
    state: {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const file of ["config.js", "review-workstation-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return sandbox;
}

function renderTable(sandbox, block) {
  sandbox.__tableBlock = block;
  return String(vm.runInContext("renderSourceFidelityTable(__tableBlock)", sandbox));
}

const sandbox = loadRenderer();

// (a) Horizontal grid span -> colspan.
const gridSpanTable = {
  type: "table",
  table_index: 1,
  rows: [
    { row_index: 1, cells: [{ cell_index: 1, col_span: 2, paragraph_ids: ["h"], blocks: [{ type: "paragraph", id: "h", text: "Header spanning two" }] }] },
    { row_index: 2, cells: [
      { cell_index: 1, paragraph_ids: ["l"], blocks: [{ type: "paragraph", id: "l", text: "Left" }] },
      { cell_index: 2, paragraph_ids: ["r"], blocks: [{ type: "paragraph", id: "r", text: "Right" }] },
    ] },
  ],
};
const gridSpanHtml = renderTable(sandbox, gridSpanTable);
assert.ok(gridSpanHtml.includes('colspan="2"'), `expected colspan=2 in: ${gridSpanHtml}`);
assert.ok(!gridSpanHtml.includes("rowspan="), "grid-span-only table must not emit a rowspan");
assert.ok(gridSpanHtml.includes("Header spanning two") && gridSpanHtml.includes("Left") && gridSpanHtml.includes("Right"));

// (b) Vertical merge -> rowspan on the restart cell.
const vMergeTable = {
  type: "table",
  table_index: 1,
  rows: [
    { row_index: 1, cells: [
      { cell_index: 1, row_span: 2, paragraph_ids: ["m"], blocks: [{ type: "paragraph", id: "m", text: "Merged down" }] },
      { cell_index: 2, paragraph_ids: ["a"], blocks: [{ type: "paragraph", id: "a", text: "Row1 col2" }] },
    ] },
    { row_index: 2, cells: [{ cell_index: 2, paragraph_ids: ["b"], blocks: [{ type: "paragraph", id: "b", text: "Row2 col2" }] }] },
  ],
};
const vMergeHtml = renderTable(sandbox, vMergeTable);
assert.ok(vMergeHtml.includes('rowspan="2"'), `expected rowspan=2 in: ${vMergeHtml}`);
assert.ok(!vMergeHtml.includes("colspan="), "vertical-merge-only table must not emit a colspan");

// (c) Nested table -> a <table> inside the parent cell.
const nestedTable = {
  type: "table",
  table_index: 1,
  rows: [
    { row_index: 1, cells: [{
      cell_index: 1,
      paragraph_ids: ["o"],
      blocks: [
        { type: "paragraph", id: "o", text: "Outer cell before" },
        { type: "table", table_index: 2, rows: [
          { row_index: 1, cells: [
            { cell_index: 1, paragraph_ids: ["ia"], blocks: [{ type: "paragraph", id: "ia", text: "Inner A" }] },
            { cell_index: 2, paragraph_ids: ["ib"], blocks: [{ type: "paragraph", id: "ib", text: "Inner B" }] },
          ] },
        ] },
      ],
    }] },
  ],
};
const nestedHtml = renderTable(sandbox, nestedTable);
// A nested <table> lives inside a <td> of the outer table.
const tdIndex = nestedHtml.indexOf("<td");
const nestedTableIndex = nestedHtml.indexOf('data-source-fidelity-table="2"');
assert.ok(nestedTableIndex > tdIndex && tdIndex >= 0, "nested table must render inside the outer cell");
assert.ok(nestedHtml.includes("Inner A") && nestedHtml.includes("Inner B"));
assert.ok(nestedHtml.includes("Outer cell before"));

// (d) Plain table -> no span attributes at all.
const plainTable = {
  type: "table",
  table_index: 1,
  rows: [{ row_index: 1, cells: [
    { cell_index: 1, paragraph_ids: ["x"], blocks: [{ type: "paragraph", id: "x", text: "One" }] },
    { cell_index: 2, paragraph_ids: ["y"], blocks: [{ type: "paragraph", id: "y", text: "Two" }] },
  ] }],
};
const plainHtml = renderTable(sandbox, plainTable);
assert.ok(!plainHtml.includes("colspan=") && !plainHtml.includes("rowspan="), "plain table must not emit any span attributes");

// Defensive clamp: a malformed 0/negative span never emits an attribute.
const clampSpan = vm.runInContext("sourceFidelitySpan(0)", sandbox);
assert.equal(clampSpan, 1, "sourceFidelitySpan clamps <=1 to 1");
assert.equal(vm.runInContext("sourceFidelitySpan(-5)", sandbox), 1);
assert.equal(vm.runInContext("sourceFidelitySpan(3)", sandbox), 3);

console.log("source-fidelity-table-spans: all assertions passed");
