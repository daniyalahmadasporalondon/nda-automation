"use strict";

// Frontend unit test for the pure CorpusModel + CorpusView matcher helpers.
//
// corpus.js is a classic browser script that exposes CommonJS exports behind a
// `typeof module !== "undefined"` guard (a no-op in the browser). We require it
// here with a minimal RepositoryModel stub on the global so the board-column
// vocabulary path is exercised exactly as it is in the shipped page (where
// repository-model.js loads first).

const assert = require("node:assert/strict");
const path = require("node:path");

// Stub the Repository model the way repository-model.js exposes it in the page.
const BOARD_COLUMNS = [
  { id: "generated", label: "Generated" },
  { id: "manual_upload", label: "Upload" },
  { id: "gmail_demo", label: "Inbox" },
  { id: "in_review", label: "In Review" },
  { id: "reviewed", label: "Reviewed" },
  { id: "sent", label: "Sent" },
];
const BOARD_COLUMN_IDS = new Set(BOARD_COLUMNS.map((c) => c.id));
global.RepositoryModel = {
  BOARD_COLUMNS,
  boardColumnLabel(boardColumn) {
    if (BOARD_COLUMN_IDS.has(boardColumn)) {
      return BOARD_COLUMNS.find((c) => c.id === boardColumn).label;
    }
    return "Inbox";
  },
};

const { CorpusModel, CorpusView } = require(path.join(__dirname, "..", "..", "static", "js", "corpus.js"));

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- FIX A: status chip = Repository board-column vocabulary ----------------
test("statusChip maps board_column to the Repository label", () => {
  assert.equal(CorpusModel.statusChip({ status: "reviewed" }), "Reviewed");
  assert.equal(CorpusModel.statusChip({ status: "in_review" }), "In Review");
  assert.equal(CorpusModel.statusChip({ status: "generated" }), "Generated");
  assert.equal(CorpusModel.statusChip({ status: "sent" }), "Sent");
  assert.equal(CorpusModel.statusChip({ status: "manual_upload" }), "Upload");
  assert.equal(CorpusModel.statusChip({ status: "gmail_demo" }), "Inbox");
});

test("statusChip returns empty for a missing board_column (drive-only -> dash)", () => {
  assert.equal(CorpusModel.statusChip({ status: "" }), "");
  assert.equal(CorpusModel.statusChip({}), "");
  assert.equal(CorpusModel.statusChip(null), "");
});

test("status vocabulary stays inside the 5 visible board columns (+ Upload)", () => {
  const labels = BOARD_COLUMNS.map((c) => CorpusModel.statusChip({ status: c.id }));
  // No phantom Negotiation/Executed/Intake phases leak through.
  ["Negotiation", "Executed", "Intake", "Approval"].forEach((phantom) => {
    assert.ok(!labels.includes(phantom), `phantom phase leaked: ${phantom}`);
  });
});

// --- FIX B: ROLE_STAGE_LABELS parity + lifecycle rail -----------------------
test("ROLE_STAGE_LABELS is at full 7-role parity incl. sent + signed", () => {
  assert.deepEqual(CorpusModel.ROLE_STAGE_LABELS, {
    original: "received",
    generated: "draft",
    redline: "ai_redline",
    reviewed: "legal_review",
    sent: "sent",
    counter: "counter",
    signed: "signed",
  });
});

test("artifactStageLabel falls back through the role map (sent/signed)", () => {
  assert.equal(CorpusModel.artifactStageLabel({ role: "sent" }), "sent");
  assert.equal(CorpusModel.artifactStageLabel({ role: "signed" }), "signed");
  // stage_label wins when present.
  assert.equal(CorpusModel.artifactStageLabel({ role: "sent", stage_label: "custom" }), "custom");
});

test("railSteps returns the 7 ordered steps with filled flags", () => {
  const matter = {
    artifacts: [{ role: "original" }, { stage_label: "legal_review" }, { role: "signed" }],
  };
  const steps = CorpusModel.railSteps(matter);
  assert.deepEqual(
    steps.map((s) => s.stage),
    ["received", "draft", "ai_redline", "legal_review", "sent", "counter", "signed"]
  );
  const filled = steps.filter((s) => s.filled).map((s) => s.stage);
  assert.deepEqual(filled, ["received", "legal_review", "signed"]);
});

// --- rich facets read defensively + degrade --------------------------------
test("matterFacetValue reads matter.facets[key] then top-level, else undefined", () => {
  assert.equal(CorpusModel.matterFacetValue({ facets: { governing_law: "India" } }, "governing_law"), "India");
  assert.equal(CorpusModel.matterFacetValue({ governing_law: "Delaware" }, "governing_law"), "Delaware");
  assert.equal(CorpusModel.matterFacetValue({}, "governing_law"), undefined);
  assert.equal(CorpusModel.matterFacetValue({ facets: { governing_law: "" } }, "governing_law"), undefined);
});

test("RICH_FACET_KEYS = governing_law / non_solicit / non_compete", () => {
  assert.deepEqual(CorpusModel.RICH_FACET_KEYS, ["governing_law", "non_solicit", "non_compete"]);
});

// --- buildFilter: AND across keys, OR within a key, free text --------------
test("buildFilter combines facets (AND across keys) and free text", () => {
  const matters = [
    { counterparty: "Acme Corp", title: "Acme NDA", status: "reviewed", source: "app", duplicate: false, artifacts: [{ filename: "a.docx" }] },
    { counterparty: "Globex Inc", title: "Globex NDA", status: "sent", source: "drive", duplicate: true, artifacts: [{ filename: "g.pdf" }] },
  ];
  const facets = new Map();
  facets.set("stage", new Set(["reviewed", "sent"])); // OR within a key
  const filterA = CorpusView.buildFilter(facets, "");
  assert.deepEqual(matters.filter(filterA).map((m) => m.counterparty), ["Acme Corp", "Globex Inc"]);

  // AND across keys: stage in {reviewed,sent} AND source=drive -> only Globex.
  facets.set("source", new Set(["drive"]));
  const filterB = CorpusView.buildFilter(facets, "");
  assert.deepEqual(matters.filter(filterB).map((m) => m.counterparty), ["Globex Inc"]);

  // Free text over counterparty/title/filenames.
  const filterC = CorpusView.buildFilter(new Map(), "acme");
  assert.deepEqual(matters.filter(filterC).map((m) => m.counterparty), ["Acme Corp"]);
  const filterD = CorpusView.buildFilter(new Map(), "g.pdf");
  assert.deepEqual(matters.filter(filterD).map((m) => m.counterparty), ["Globex Inc"]);
});

test("buildFilter flags facet matches the duplicate flag", () => {
  const matters = [
    { duplicate: true, status: "x", source: "app", artifacts: [] },
    { duplicate: false, status: "y", source: "app", artifacts: [] },
  ];
  const facets = new Map([["flags", new Set(["duplicate"])]]);
  const filter = CorpusView.buildFilter(facets, "");
  assert.equal(matters.filter(filter).length, 1);
});

process.stdout.write(`\ncorpus-model: ${passed} passed\n`);
