// Unit proof for the Review-workstation humanization views — the display-only label
// maps that keep parser-internal enum tokens out of the reviewer's face.
//
// Like contract-structure-view.mjs, the workstation modules are classic top-level
// function files, so we load each into a vm sandbox seeded with the SAME globals the
// browser provides (escapeHtml + a minimal `state` + a `window`), then call the
// individual label/render functions and assert on their output. No live server or
// browser needed.
//
// Proves (all DISPLAY-string changes, never the underlying values/logic):
//   1. Grounding block: "Evidence check" label (not "Grounding"); each
//      matched_paragraph_ids id runs through paragraphDisplayLabel ("Paragraph 15",
//      not "p15"); grounding.status maps to plain English.
//   2. Source Fidelity style label: only meaningful Word styles surface (Heading*->
//      "Heading", Title->"Title", ListParagraph->"List item"); every other style id
//      is hidden, mirroring the Structure tab's suppression.
//   3. proposedChange action/safety labels: unknown codes fall back to a generic
//      phrase ("Proposed change" / "Reviewer approval needed"), never the raw token.
//   4. approveBlockReasonLabel: known codes mapped, unresolved_clause:<id> names the
//      clause, unknown codes -> a generic instruction; NEVER returns the raw code.
//   5. staleReviewMessage: the jargon copy is reworded to plain English.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const RENDERING_PATH = path.join(HERE, "../../static/js/review-workstation-rendering.js");
const ACTIONS_PATH = path.join(HERE, "../../static/js/review-workstation-actions.js");

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Load a workstation module into a sandbox and return a getter for its top-level
// functions. `state` is the minimal review state the targeted functions read.
function loadModule(filePath, state) {
  const sandbox = { escapeHtml, console, document: {}, state };
  sandbox.window = sandbox; // top-level `function foo` is reachable as window.foo
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(filePath, "utf8"), sandbox, { filename: path.basename(filePath) });
  return (name) => vm.runInContext(name, sandbox);
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`  ok ${name}`);
}

// --- rendering.js -----------------------------------------------------------------

const rendering = loadModule(RENDERING_PATH, {
  reviewParagraphs: [{ id: "p15", index: 15 }],
});

test("Grounding block is labelled 'Evidence check' (NOT 'Grounding')", () => {
  const renderGroundingAuditBlock = rendering("renderGroundingAuditBlock");
  const html = renderGroundingAuditBlock({
    grounding: { status: "grounded" },
    structured_evidence: [{}],
    matched_paragraph_ids: ["p15"],
  });
  assert.ok(html.includes("Evidence check"), "label reworded to 'Evidence check'");
  assert.doesNotMatch(html, />\s*Grounding\s*</, "the old 'Grounding' label is gone");
});

test("Grounding block shows 'Paragraph 15' for id 'p15' (paragraphDisplayLabel)", () => {
  const renderGroundingAuditBlock = rendering("renderGroundingAuditBlock");
  const html = renderGroundingAuditBlock({
    grounding: { status: "grounded" },
    structured_evidence: [{}],
    matched_paragraph_ids: ["p15"],
  });
  assert.ok(html.includes("Paragraph 15"), "id resolved to a human paragraph label");
  assert.doesNotMatch(html, /Paragraphs: p15/, "raw 'p15' token must not leak");
});

test("Grounding status maps to plain English for grounded/ungrounded/not_recorded", () => {
  const renderGroundingAuditBlock = rendering("renderGroundingAuditBlock");
  const grounded = renderGroundingAuditBlock({ grounding: { status: "grounded" }, structured_evidence: [{}], matched_paragraph_ids: [] });
  assert.ok(grounded.includes("Backed by evidence in the document"), "grounded -> plain English");
  const ungrounded = renderGroundingAuditBlock({ grounding: { status: "ungrounded" }, structured_evidence: [], matched_paragraph_ids: [] });
  assert.ok(ungrounded.includes("No matching evidence found"), "ungrounded -> plain English");
  const notRecorded = renderGroundingAuditBlock({ grounding: { status: "not_recorded" }, structured_evidence: [], matched_paragraph_ids: [] });
  assert.ok(notRecorded.includes("Evidence check not recorded"), "not_recorded -> plain English");
  assert.doesNotMatch(notRecorded, /not_recorded/, "raw 'not_recorded' token must not leak");
});

test("Source Fidelity surfaces only meaningful Word styles; hides the rest", () => {
  const renderSourceFidelityParagraphBlock = rendering("renderSourceFidelityParagraphBlock");
  const heading = renderSourceFidelityParagraphBlock({ id: "p1", text: "x", style_name: "Heading2" });
  assert.ok(heading.includes(">Heading<"), "Heading2 -> 'Heading'");
  assert.doesNotMatch(heading, /Heading2/, "raw 'Heading2' style id must not leak");
  const title = renderSourceFidelityParagraphBlock({ id: "p1", text: "x", style_name: "Title" });
  assert.ok(title.includes(">Title<"), "Title -> 'Title'");
  const list = renderSourceFidelityParagraphBlock({ id: "p1", text: "x", style_name: "ListParagraph" });
  assert.ok(list.includes(">List item<"), "ListParagraph -> 'List item'");
  assert.doesNotMatch(list, /ListParagraph/, "raw 'ListParagraph' style id must not leak");
  // A meaningless style id (e.g. "Normal", "BodyText") renders NO badge at all.
  const normal = renderSourceFidelityParagraphBlock({ id: "p1", text: "x", style_name: "Normal" });
  assert.doesNotMatch(normal, /source-fidelity-style/, "meaningless style id is suppressed (no badge)");
  assert.doesNotMatch(normal, /has-style/, "no has-style class when the style is suppressed");
});

test("proposedChange action/safety labels never echo a raw token", () => {
  const proposedChangeActionLabel = rendering("proposedChangeActionLabel");
  const proposedChangeSafetyLabel = rendering("proposedChangeSafetyLabel");
  // Known codes still map.
  assert.equal(proposedChangeActionLabel("replace"), "Replace text");
  assert.equal(proposedChangeSafetyLabel("comment_only"), "Comment only");
  // Unknown/new codes fall back to a generic phrase, not "brand new action".
  assert.equal(proposedChangeActionLabel("brand_new_action"), "Proposed change");
  assert.equal(proposedChangeSafetyLabel("brand_new_status"), "Reviewer approval needed");
});

// --- actions.js -------------------------------------------------------------------

const actions = loadModule(ACTIONS_PATH, {
  reviewClauses: [{ id: "non_solicitation", name: "Non-Solicitation" }],
});

test("approveBlockReasonLabel maps known codes + names unresolved clauses", () => {
  const approveBlockReasonLabel = actions("approveBlockReasonLabel");
  assert.match(approveBlockReasonLabel("stale_playbook"), /stale/i, "stale_playbook mapped");
  assert.equal(
    approveBlockReasonLabel("unresolved_clause:non_solicitation"),
    "Resolve the Non-Solicitation clause before approving.",
    "names a known clause",
  );
  assert.equal(
    approveBlockReasonLabel("unresolved_clause:term_years"),
    "Resolve the Term Years clause before approving.",
    "humanizes an unknown clause id",
  );
});

test("approveBlockReasonLabel NEVER returns the raw code for an unknown reason", () => {
  const approveBlockReasonLabel = actions("approveBlockReasonLabel");
  const out = approveBlockReasonLabel("some_future_code");
  assert.equal(out, "Approval is blocked — resolve the listed issue and try again.");
  assert.doesNotMatch(out, /some_future_code/, "raw code must not be echoed");
});

test("staleReviewMessage rewords the engine/runtime jargon to plain English", () => {
  const staleReviewMessage = actions("staleReviewMessage");
  assert.equal(
    staleReviewMessage({ stale_reasons: ["review_engine_version_changed"] }),
    "The review rules were updated — refresh before sending.",
  );
  assert.equal(
    staleReviewMessage({ stale_reasons: ["missing_playbook_runtime"] }),
    "This review predates the current rules — refresh before sending.",
  );
});

console.log(`\nreview-workstation-humanize: ${passed} assertions passed`);
