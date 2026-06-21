"use strict";

// Regression test for the Guide-tab "developer detail leak" cleanup.
//
// The Guide tab (non-admin) renders three static panels straight from
// static/index.html:
//   - #adminDocumentPanel   (Core Python modules / Architecture)
//   - #adminCheckersPanel    (per-clause checker cards)
//   - #adminAiGuidePanel     (AI review methodology)
//
// These surfaces are visible to NON-admin users. They must NOT leak internal
// developer detail: no Python module/file paths (`nda_automation/...`, `*.py`)
// and no environment-variable KEY names (`NDA_*`, `OPENROUTER_API_KEY`).
//
// The admin-only panels (ai / health / email / docusign / access ...) are
// intentionally left RAW so admins can debug, so this test scopes its
// assertions to EXACTLY the three Guide-tab panel ids and does not look at the
// rest of the document.
//
// Run: node tests/frontend/guide-no-dev-detail.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const INDEX = path.join(__dirname, "..", "..", "static", "index.html");
const html = fs.readFileSync(INDEX, "utf8");

// The three non-admin Guide-tab panels, identified by their element ids.
const GUIDE_PANEL_IDS = [
  "adminDocumentPanel",
  "adminCheckersPanel",
  "adminAiGuidePanel",
];

// Extract the markup of a single <section ... id="<id>" ...> ... </section>
// block by balancing <section>/</section> tags from the opening tag of the
// panel. The Guide panels are top-level admin-section-panel sections, so the
// first balanced close is the panel boundary.
function extractSection(source, id) {
  const openRe = new RegExp(`<section\\b[^>]*\\bid="${id}"[^>]*>`);
  const open = openRe.exec(source);
  assert.ok(open, `index.html must contain a <section id="${id}">`);
  let depth = 0;
  const tagRe = /<\/?section\b[^>]*>/g;
  tagRe.lastIndex = open.index;
  let m;
  let start = open.index;
  while ((m = tagRe.exec(source)) !== null) {
    if (m[0].startsWith("</")) {
      depth -= 1;
      if (depth === 0) {
        return source.slice(start, tagRe.lastIndex);
      }
    } else {
      depth += 1;
    }
  }
  throw new Error(`unbalanced <section> for id="${id}"`);
}

// Forbidden developer-detail patterns for the non-admin Guide surface.
const FORBIDDEN = [
  { name: "Python package path (nda_automation/)", re: /nda_automation\// },
  { name: "Python file path (.py)", re: /\.py\b/ },
  { name: "NDA_* env var name", re: /\bNDA_[A-Z0-9_]+/ },
  { name: "OPENROUTER_API_KEY env var name", re: /OPENROUTER_API_KEY/ },
];

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

for (const id of GUIDE_PANEL_IDS) {
  test(`#${id} renders no developer detail (paths / env-var names)`, () => {
    const block = extractSection(html, id);
    for (const { name, re } of FORBIDDEN) {
      const hit = re.exec(block);
      assert.equal(
        hit,
        null,
        `#${id} leaks ${name}` + (hit ? `: ...${block.slice(Math.max(0, hit.index - 40), hit.index + 40)}...` : ""),
      );
    }
  });
}

// Positive guard: the functional replacements actually landed, so the test
// can't pass simply because a panel went missing/empty.
test("functional replacement titles are present in the Guide panels", () => {
  const docPanel = extractSection(html, "adminDocumentPanel");
  assert.ok(/Review Engine/.test(docPanel), "Architecture panel must show 'Review Engine'");
  assert.ok(/Word Export/.test(docPanel), "Architecture panel must show 'Word Export'");

  const checkers = extractSection(html, "adminCheckersPanel");
  assert.ok(/Mutuality Check/.test(checkers), "Checkers panel must show 'Mutuality Check'");
  assert.ok(/Signatures Check/.test(checkers), "Checkers panel must show 'Signatures Check'");

  const aiGuide = extractSection(html, "adminAiGuidePanel");
  assert.ok(
    /AI Review Service/.test(aiGuide),
    "AI methodology panel must reference the 'AI Review Service' (no module path)",
  );
});

process.stdout.write(`\n${passed} passed\n`);
