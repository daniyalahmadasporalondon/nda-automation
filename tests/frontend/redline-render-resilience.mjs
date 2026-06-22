// Regression lock for the FE redline-render RESILIENCE fix (the P0 where ONE
// bad/unanchorable redline threw synchronously and blanked the ENTIRE review
// workstation, killing every citation -- the live "Pismo doesn't render"
// symptom).
//
// Three guards are proven here, each driving the REAL shipped code the browser
// runs (the classic static/js/redline-rendering.js via vm, and the real
// RedlineEditContract / inline-diff modules):
//   (a) one MALFORMED redline degrades to plain text -- the document STILL
//       renders, every OTHER paragraph shows, citation anchors (data-paragraph-id)
//       survive, and only the bad paragraph degrades (no blank pane);
//   (b) a NORMAL redline renders identically (no regression);
//   (c) the sanitizer drops the malformed redline before render;
//   plus renderDiffOperations tolerates a stray null/typeless op.

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

// Load the REAL classic render module into a sandbox seeded with exactly the
// bridge globals global-bridge.mjs exposes to the browser, then surface the
// top-level renderReviewDocument as a callable handle.
function loadReviewDocumentRenderer() {
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
  return {
    renderReviewDocument: vm.runInContext("renderReviewDocument", sandbox),
  };
}

const { renderReviewDocument } = loadReviewDocumentRenderer();

// A two-paragraph document. p_good carries a normal, well-formed replace redline
// (must render with tracked-change spans). p_bad carries a redline crafted to
// THROW deep in the render chain when rendered -- a Proxy whose property access
// raises mimics the unanchorable/malformed-edit class of failure that used to
// abort the whole paint. The boundary must contain it to p_bad alone.
const paragraphs = [
  { id: "p_good", text: "The confidentiality obligations survive for seven years." },
  { id: "p_bad", text: "The Recipient must not circumvent the Company." },
];
const clauses = [
  { id: "term_and_survival", matched_paragraph_ids: ["p_good"] },
  { id: "non_circumvention", matched_paragraph_ids: ["p_bad"] },
];

const goodRedline = {
  id: "r_good",
  action: "replace_paragraph",
  clause_id: "term_and_survival",
  paragraph_id: "p_good",
  original_text: "The confidentiality obligations survive for seven years.",
  replacement_text: "The confidentiality obligations survive for three years.",
};

// A redline whose inline_diff_operations access EXPLODES at render time. This is
// the structural stand-in for a malformed/unanchorable edit: any read of a diff
// op throws, exactly the synchronous throw that used to blank the document.
function explodingRedline() {
  const boom = new Proxy(
    { type: "delete" },
    {
      get(target, prop) {
        if (prop === "type" || prop === "token") {
          throw new Error("malformed redline op (simulated unanchorable edit)");
        }
        return target[prop];
      },
    },
  );
  return {
    id: "r_bad",
    action: "replace_paragraph",
    clause_id: "non_circumvention",
    paragraph_id: "p_bad",
    original_text: "The Recipient must not circumvent the Company.",
    replacement_text: "The Recipient shall not circumvent the Company.",
    whole_paragraph: false,
    inline_diff_operations: [boom],
  };
}

function renderDoc(redlines) {
  return renderReviewDocument({
    clauses,
    originalParagraphs: paragraphs,
    paragraphs,
    comments: [],
    redlines,
    selectedClauseId: null,
    viewMode: "redline",
  });
}

// (b) BASELINE / no-regression: a normal redline renders, both paragraphs paint,
// both citation anchors are present, and the proposed replacement text shows.
const baselineHtml = renderDoc([goodRedline]);
assert.ok(baselineHtml.includes('data-paragraph-id="p_good"'), "baseline: good paragraph anchor present");
assert.ok(baselineHtml.includes('data-paragraph-id="p_bad"'), "baseline: other paragraph anchor present");
assert.ok(
  baselineHtml.includes("three years") || baselineHtml.includes("inline-ins"),
  "baseline: the normal redline still renders its proposed change",
);
assert.ok(!baselineHtml.includes("doc-redline-degraded"), "baseline: nothing degrades when all redlines are well-formed");

// (a) RESILIENCE: with ONE exploding redline, the document STILL renders. Every
// other paragraph shows, its citation anchor survives, and ONLY the bad
// paragraph degrades (plain text + "Redline unavailable" marker) -- no blank.
const resilientHtml = renderDoc([goodRedline, explodingRedline()]);
assert.ok(resilientHtml.length > 0, "resilience: document is NOT blank when a redline throws");
assert.ok(resilientHtml.includes('data-paragraph-id="p_good"'), "resilience: the good paragraph still renders");
assert.ok(resilientHtml.includes('data-paragraph-id="p_bad"'), "resilience: the bad paragraph still anchors (citations work)");
assert.ok(
  resilientHtml.includes("three years") || resilientHtml.includes("inline-ins"),
  "resilience: the OTHER clause's redline still renders normally",
);
// The bad paragraph degraded to plain text with the unavailable marker.
assert.ok(resilientHtml.includes("doc-redline-degraded"), "resilience: the bad paragraph degrades, not the whole doc");
assert.ok(resilientHtml.includes("Redline unavailable"), "resilience: the degraded paragraph carries the unavailable marker");
// The good paragraph must NOT have been collaterally degraded.
const goodFrameIdx = resilientHtml.indexOf('data-paragraph-id="p_good"');
const badFrameIdx = resilientHtml.indexOf('data-paragraph-id="p_bad"');
const degradedIdx = resilientHtml.indexOf("doc-redline-degraded");
assert.ok(goodFrameIdx >= 0 && badFrameIdx >= 0, "resilience: both frames present");
assert.ok(degradedIdx > goodFrameIdx, "resilience: only the bad paragraph (after the good one) is degraded");

// (c) SANITIZER drops malformed redlines BEFORE render (the live-path guard
// wired into renderResult). normalizeRedlineEdits is the function called there.
const malformedSet = [
  goodRedline,
  { id: "x1", action: "not_a_real_action", paragraph_id: "p_bad", clause_id: "c" }, // unknown action
  { id: "x2", action: "replace_paragraph", clause_id: "c" }, // missing paragraph_id
  null, // outright junk
];
const sanitized = RedlineEditContract.normalizeRedlineEdits(malformedSet);
assert.equal(sanitized.length, 1, "sanitizer: only the well-formed redline survives");
assert.equal(sanitized[0].id, "r_good", "sanitizer: the survivor is the good redline");

// Sanitizer also strips null/typeless inline_diff_operations from an otherwise
// valid edit, so they never reach the diff renderer.
const sanitizedOps = RedlineEditContract.normalizeRedlineEdits([
  {
    id: "r_ops",
    action: "replace_paragraph",
    clause_id: "c",
    paragraph_id: "p1",
    inline_diff_operations: [null, { type: "insert", token: "ok" }, { token: "no-type" }],
  },
]);
assert.equal(sanitizedOps.length, 1, "sanitizer: edit with partially-bad ops still survives");
assert.deepEqual(
  sanitizedOps[0].inline_diff_operations,
  [{ type: "insert", token: "ok" }],
  "sanitizer: only well-typed ops survive",
);

// Defensive hardening of the diff renderer itself: a stray null/typeless op that
// somehow slips the sanitizer must NOT throw.
assert.doesNotThrow(
  () => renderDiffOperations([null, { type: "insert", token: "hi" }, { token: "no-type" }]),
  "renderDiffOperations tolerates null/typeless ops",
);
assert.ok(
  renderDiffOperations([null, { type: "insert", token: "hi" }]).includes("hi"),
  "renderDiffOperations still renders the valid op alongside junk",
);
assert.doesNotThrow(() => renderDiffOperations(undefined), "renderDiffOperations tolerates a non-array input");

console.log("redline-render-resilience: all assertions passed");
