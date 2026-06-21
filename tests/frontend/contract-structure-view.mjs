// Unit proof for the Structure-tab human-insight views (contract-structure-view.js).
//
// The controller is a classic top-level function (createContractStructureController),
// so we load it the same way utility-modules.mjs loads renderInlineRedline: read the
// source into a vm sandbox seeded with the SAME globals the browser provides
// (escapeHtml + jumpToParagraph), then drive render() against a minimal fake `root`
// whose innerHTML we inspect. No live server / browser needed -- these are the three
// new views, asserted from the rendered markup the reviewer actually sees.
//
// Proves:
//   1. Dangling-reference red flag renders when reference_integrity.status===
//      "issues_found" (and the ambiguous_issues amber callout), AND that the
//      resolver fallback (references[].status==="unresolved") fires when the guarded
//      integrity signal is absent.
//   2. The section list nests by parent_id (a child clause is rendered INSIDE its
//      parent's <details>/<ul>, not as a sibling), and low-confidence / non-source-
//      backed nodes are dimmed as the parser's guess.
//   3. Cross-references render as clickable From [source] -> [target], both carrying
//      data-para-ref jump ids, grouped by source section, with no 12-row cap.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTROLLER_PATH = path.join(HERE, "../../static/js/contract-structure-view.js");

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Minimal fake root: the controller writes innerHTML and, after each render, calls
// querySelectorAll('[data-para-ref][role="button"]') to (re)bind keyboard handlers.
// We only need innerHTML for assertions; querySelectorAll returns an empty list (the
// keyboard binding is a no-op here, exercised separately by the real browser tests).
function makeRoot() {
  return {
    innerHTML: "",
    querySelectorAll() {
      return [];
    },
  };
}

function loadController() {
  const sandbox = {
    escapeHtml,
    jumpToParagraph() {},
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CONTROLLER_PATH, "utf8"), sandbox, { filename: "contract-structure-view.js" });
  return vm.runInContext("createContractStructureController", sandbox);
}

const createController = loadController();

function renderWith(latestReviewResult) {
  const root = makeRoot();
  const state = { latestReviewResult };
  const controller = createController({ state, root });
  controller.render();
  return root.innerHTML;
}

// A small but real structure: Clause 9 (with a child 9.1), Schedule 1, plus a
// non-source-backed low-confidence "guess" section. Used across the view tests.
function baseStructure() {
  return {
    sections: [
      {
        id: "section-1",
        kind: "clause",
        label: "Clause 9",
        heading: "Confidentiality",
        number: "9",
        level: 0,
        confidence: "high",
        parent_id: null,
        start_paragraph_id: "p-9",
        paragraph_ids: ["p-9", "p-9a"],
        start_index: 9,
        end_index: 12,
        source: { source_kind: "docx", numbering: { label: "9" } },
      },
      {
        id: "section-2",
        kind: "numbered",
        label: "9.1",
        heading: "Permitted disclosures",
        number: "9.1",
        level: 1,
        confidence: "high",
        parent_id: "section-1",
        start_paragraph_id: "p-9a",
        paragraph_ids: ["p-9a"],
        start_index: 10,
        end_index: 11,
        source: { source_kind: "docx", numbering: { label: "9.1" } },
      },
      {
        id: "section-3",
        kind: "schedule",
        label: "Schedule 1",
        heading: "Data processing",
        number: "1",
        level: 0,
        confidence: "high",
        parent_id: null,
        start_paragraph_id: "p-30",
        paragraph_ids: ["p-30", "p-31"],
        start_index: 30,
        end_index: 35,
        source: { source_kind: "docx" },
      },
      {
        // A parser GUESS: no source, medium confidence -> must render dimmed and NOT
        // be a clickable jump target.
        id: "section-4",
        kind: "heading",
        label: "1 SHELDON SQUARE",
        heading: "1 Sheldon Square",
        number: null,
        level: 0,
        confidence: "medium",
        parent_id: null,
        start_paragraph_id: "p-2",
        paragraph_ids: ["p-2"],
        start_index: 2,
        end_index: 2,
      },
    ],
    stats: { section_count: 4, source_backed_section_count: 3 },
  };
}

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`  ok ${name}`);
}

// --- VIEW 1: dangling/ambiguous reference flags ---------------------------------

test("reference_integrity issues_found renders a RED dangling-reference callout", () => {
  const html = renderWith({
    contract_structure: baseStructure(),
    reference_resolver: { references: [] },
    reference_integrity: {
      applicable: true,
      status: "issues_found",
      issues: [
        { summary: "Clause 9 references Schedule 3, which doesn't exist." },
      ],
      ambiguous_issues: [],
    },
  });
  assert.match(html, /structure-flag-danger/, "danger callout class present");
  assert.match(html, /role="alert"/, "danger callout is an alert");
  assert.ok(
    html.includes("Clause 9 references Schedule 3, which doesn&#39;t exist."),
    "the issue summary is rendered (escaped) in the callout",
  );
});

test("ambiguous_issues render an AMBER callout (not a red defect)", () => {
  const html = renderWith({
    contract_structure: baseStructure(),
    reference_resolver: { references: [] },
    reference_integrity: {
      applicable: true,
      status: "ok",
      issues: [],
      ambiguous_issues: [
        { summary: "Section 2 matches more than one section (restarted numbering); its target is unknown." },
      ],
    },
  });
  assert.match(html, /structure-flag-warn/, "amber callout class present");
  assert.ok(html.includes("matches more than one section"), "ambiguous summary rendered");
  assert.doesNotMatch(html, /structure-flag-danger/, "no red callout for ambiguous-only");
});

test("no flags section when integrity is applicable && ok with no ambiguity", () => {
  const html = renderWith({
    contract_structure: baseStructure(),
    reference_resolver: { references: [] },
    reference_integrity: { applicable: true, status: "ok", issues: [], ambiguous_issues: [] },
  });
  assert.doesNotMatch(html, /structure-flags/, "no flags block when nothing to flag");
});

test("fallback: derives danglers from references[].status===unresolved when integrity absent", () => {
  const html = renderWith({
    contract_structure: baseStructure(),
    reference_resolver: {
      references: [
        {
          id: "reference-0",
          reference_text: "Schedule 3",
          kind: "schedule",
          status: "unresolved",
          unresolved_numbers: ["3"],
          source_section_id: "section-1",
          targets: [],
        },
      ],
    },
    // reference_integrity intentionally ABSENT (deterministic-only / PDF path).
  });
  assert.match(html, /structure-flag-danger/, "fallback still produces a red flag");
  assert.ok(html.includes("Schedule 3"), "fallback names the missing reference");
  assert.ok(html.includes("doesn&#39;t exist in this document"), "fallback human summary phrasing");
});

// --- VIEW 2: nested outline tree -------------------------------------------------

test("sections nest by parent_id (child rendered inside parent's collapsible subtree)", () => {
  const html = renderWith({
    contract_structure: baseStructure(),
    reference_resolver: { references: [] },
  });
  // The parent (Clause 9) must be a <details> branch, and its child (9.1) must appear
  // inside a nested role="group" list -- i.e. AFTER the parent's <summary> opens.
  assert.match(html, /structure-tree-branch/, "parent renders as a collapsible branch");
  assert.match(html, /<details class="structure-tree-details" open>/, "branch is an open <details>");
  const parentIdx = html.indexOf("Clause 9");
  const groupIdx = html.indexOf('role="group"');
  const childIdx = html.indexOf("Permitted disclosures");
  assert.ok(parentIdx >= 0 && groupIdx >= 0 && childIdx >= 0, "parent, group, child all present");
  assert.ok(parentIdx < groupIdx && groupIdx < childIdx, "child 9.1 nests under Clause 9's group");
});

test("low-confidence / non-source-backed nodes are dimmed as the parser's guess", () => {
  const html = renderWith({
    contract_structure: baseStructure(),
    reference_resolver: { references: [] },
  });
  assert.match(html, /structure-row-dim/, "a guessed row is dimmed");
  assert.ok(html.includes("Parser's guess"), "guess badge rendered");
  // The guessed section (no source) must NOT be a clickable jump target.
  assert.doesNotMatch(html, /data-para-ref="p-2"/, "guessed section is not a jump target");
  // A real high-confidence source-backed section IS a jump target.
  assert.match(html, /data-para-ref="p-9"/, "real section is a jump target");
});

// --- VIEW 3: clickable cross-reference links ------------------------------------

test("references render as clickable From [source] -> [target], grouped, no cap", () => {
  const sections = baseStructure().sections;
  const sectionsById = {};
  sections.forEach((section) => { sectionsById[section.id] = section; });
  // 15 references (> the old 12 cap) all from Clause 9 -> Schedule 1, to prove both
  // the clickable link shape and that nothing is capped.
  const references = [];
  for (let i = 0; i < 15; i += 1) {
    references.push({
      id: `reference-${i}`,
      reference_text: "Schedule 1",
      kind: "schedule",
      status: "resolved",
      source_section_id: "section-1",
      resolved_section_ids: ["section-3"],
      targets: [sectionsById["section-3"]],
      unresolved_numbers: [],
    });
  }
  const html = renderWith({
    contract_structure: baseStructure(),
    reference_resolver: { references },
  });
  assert.match(html, /structure-xref-group/, "references grouped by source");
  assert.ok(html.includes("From"), "source-prefixed phrasing");
  // Source side clickable -> Clause 9's first paragraph.
  assert.match(html, /class="structure-xref-from"[^>]*data-para-ref="p-9"/, "source link clickable");
  // Target side clickable -> Schedule 1's first paragraph.
  assert.match(html, /class="structure-xref-to"[^>]*data-para-ref="p-30"/, "target link clickable");
  // No 12-row cap: all 15 reference links present.
  const linkCount = (html.match(/class="structure-xref-link"/g) || []).length;
  assert.equal(linkCount, 15, "all 15 references render (old 12-cap removed)");
});

test("unresolved target renders as plain non-clickable 'No target' / Unresolved", () => {
  const html = renderWith({
    contract_structure: baseStructure(),
    reference_resolver: {
      references: [
        {
          id: "reference-0",
          reference_text: "Schedule 3",
          kind: "schedule",
          status: "unresolved",
          source_section_id: "section-1",
          resolved_section_ids: [],
          targets: [],
          unresolved_numbers: ["3"],
        },
      ],
    },
    reference_integrity: { applicable: true, status: "ok", issues: [], ambiguous_issues: [] },
  });
  assert.match(html, /structure-xref-missing/, "missing-target styling");
  assert.ok(html.includes("Unresolved 3"), "names the unresolved number");
});

console.log(`\ncontract-structure-view: ${passed} assertions passed`);
