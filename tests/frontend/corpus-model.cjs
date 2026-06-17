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

const { CorpusModel, CorpusRender, CorpusView } = require(path.join(__dirname, "..", "..", "static", "js", "corpus.js"));

// Minimal DOM stub — no jsdom dependency, matching the repo's zero-dep FE
// harness style. Captures the last innerHTML written and no-ops the event
// binding (renderFacetRail/renderGroups call querySelectorAll after setting
// innerHTML; an empty NodeList keeps the markup assertions DOM-free).
function stubNode() {
  return {
    innerHTML: "",
    classList: { add() {}, remove() {}, toggle() {} },
    querySelectorAll() {
      return [];
    },
    querySelector() {
      return null;
    },
  };
}

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

// --- clause-presence facets (non_solicit / non_compete) --------------------
test("clause-presence facets render the 'present' sentinel as 'Present'", () => {
  assert.equal(CorpusModel.richFacetValueLabel("non_solicit", "present"), "Present");
  assert.equal(CorpusModel.richFacetValueLabel("non_compete", "present"), "Present");
  // A non-presence rich facet labels by its own value (governing law codes etc.).
  assert.equal(CorpusModel.richFacetValueLabel("governing_law", "india"), "india");
  assert.ok(CorpusModel.isClausePresenceFacet("non_solicit"));
  assert.ok(CorpusModel.isClausePresenceFacet("non_compete"));
  assert.ok(!CorpusModel.isClausePresenceFacet("governing_law"));
});

test("buildFilter on non_solicit/non_compete matches only matters carrying the clause", () => {
  // Mirrors the backend emit: facets.non_solicit/non_compete = "present" or absent.
  const matters = [
    { counterparty: "Both Co", title: "Both NDA", status: "reviewed", source: "app", artifacts: [], facets: { non_solicit: "present", non_compete: "present" } },
    { counterparty: "Solicit Co", title: "Solicit NDA", status: "reviewed", source: "app", artifacts: [], facets: { non_solicit: "present" } },
    { counterparty: "Plain Co", title: "Plain NDA", status: "reviewed", source: "app", artifacts: [], facets: {} },
  ];
  const nonSolicit = CorpusView.buildFilter(new Map([["non_solicit", new Set(["present"])]]), "");
  assert.deepEqual(matters.filter(nonSolicit).map((m) => m.counterparty), ["Both Co", "Solicit Co"]);
  const nonCompete = CorpusView.buildFilter(new Map([["non_compete", new Set(["present"])]]), "");
  assert.deepEqual(matters.filter(nonCompete).map((m) => m.counterparty), ["Both Co"]);
  // count == filtered parity: 2 matters carry non_solicit, 1 carries non_compete.
  assert.equal(matters.filter(nonSolicit).length, 2);
  assert.equal(matters.filter(nonCompete).length, 1);
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

// --- month lens: monthKey / monthLabel -------------------------------------
test("monthKey derives a sortable YYYY-MM key from created_at", () => {
  assert.equal(CorpusModel.monthKey("2026-06-17T10:00:00Z"), "2026-06");
  assert.equal(CorpusModel.monthKey("2026-01-02"), "2026-01");
  assert.equal(CorpusModel.monthKey("2025-12-31T23:59:00Z"), "2025-12");
});

test("monthKey returns the undated sentinel for blank/invalid dates (sorts last)", () => {
  assert.equal(CorpusModel.monthKey(""), "0000-00");
  assert.equal(CorpusModel.monthKey("not-a-date"), "0000-00");
  assert.equal(CorpusModel.monthKey(undefined), "0000-00");
});

test("monthLabel renders 'Month YYYY' and 'Undated' for the sentinel", () => {
  assert.equal(CorpusModel.monthLabel("2026-06"), "June 2026");
  assert.equal(CorpusModel.monthLabel("2025-12"), "December 2025");
  assert.equal(CorpusModel.monthLabel("0000-00"), "Undated");
  assert.equal(CorpusModel.monthLabel(""), "Undated");
});

test("month keys sort newest-first with undated last", () => {
  const keys = ["2025-12", "2026-06", "0000-00", "2026-01"];
  const sorted = keys.slice().sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));
  assert.deepEqual(sorted, ["2026-06", "2026-01", "2025-12", "0000-00"]);
});

// --- duplicate signals: model helpers --------------------------------------
test("isRepeatEntity reads repeat_entity (top-level or facets), defaults false", () => {
  assert.equal(CorpusModel.isRepeatEntity({ repeat_entity: true }), true);
  assert.equal(CorpusModel.isRepeatEntity({ facets: { repeat_entity: true } }), true);
  assert.equal(CorpusModel.isRepeatEntity({ repeat_entity: false }), false);
  assert.equal(CorpusModel.isRepeatEntity({}), false);
  assert.equal(CorpusModel.isRepeatEntity(null), false);
});

test("duplicateDocument returns the match object or null", () => {
  const dd = { matched_matter_id: "m-9", matched_title: "Acme Mk II", similarity: 0.91 };
  assert.deepEqual(CorpusModel.duplicateDocument({ duplicate_document: dd }), dd);
  assert.deepEqual(CorpusModel.duplicateDocument({ facets: { duplicate_document: dd } }), dd);
  assert.equal(CorpusModel.duplicateDocument({ duplicate_document: null }), null);
  // A match object missing the id is not a usable link target.
  assert.equal(CorpusModel.duplicateDocument({ duplicate_document: { similarity: 0.9 } }), null);
  assert.equal(CorpusModel.duplicateDocument({}), null);
});

test("similarityLabel rounds a [0,1] similarity to a NN% match label", () => {
  assert.equal(CorpusModel.similarityLabel(0.91), "91% match");
  assert.equal(CorpusModel.similarityLabel(0.925), "93% match"); // round-half-up
  assert.equal(CorpusModel.similarityLabel(1), "100% match");
  assert.equal(CorpusModel.similarityLabel("0.8"), "80% match");
  assert.equal(CorpusModel.similarityLabel(undefined), "");
});

// --- duplicate signals: facet filter + count parity ------------------------
// A 4-matter fixture exercising all three independent flag axes.
const FLAG_MATTERS = [
  // Drive copy only.
  { counterparty: "Drive Co", title: "Drive NDA", status: "reviewed", source: "both", artifacts: [], duplicate: true },
  // Repeat entity only.
  { counterparty: "Repeat Co", title: "Repeat NDA #1", status: "reviewed", source: "app", artifacts: [], repeat_entity: true },
  // Repeat entity + duplicate document.
  {
    counterparty: "Repeat Co",
    title: "Repeat NDA #2",
    status: "reviewed",
    source: "app",
    artifacts: [],
    repeat_entity: true,
    duplicate_document: { matched_matter_id: "rep-1", matched_title: "Repeat NDA #1", similarity: 0.88 },
  },
  // No flags.
  { counterparty: "Clean Co", title: "Clean NDA", status: "reviewed", source: "app", artifacts: [] },
];

test("repeat_entity facet filters matters flagged repeat_entity (count parity)", () => {
  const filter = CorpusView.buildFilter(new Map([["flags", new Set(["repeat_entity"])]]), "");
  const matched = FLAG_MATTERS.filter(filter);
  assert.deepEqual(matched.map((m) => m.title), ["Repeat NDA #1", "Repeat NDA #2"]);
  // Sidebar count (flagMatches) == filtered-result count.
  const count = FLAG_MATTERS.filter((m) => CorpusRender.flagMatches(m, "repeat_entity")).length;
  assert.equal(count, matched.length);
  assert.equal(count, 2);
});

test("duplicate_document facet filters matters with a non-null match (count parity)", () => {
  const filter = CorpusView.buildFilter(new Map([["flags", new Set(["duplicate_document"])]]), "");
  const matched = FLAG_MATTERS.filter(filter);
  assert.deepEqual(matched.map((m) => m.title), ["Repeat NDA #2"]);
  const count = FLAG_MATTERS.filter((m) => CorpusRender.flagMatches(m, "duplicate_document")).length;
  assert.equal(count, matched.length);
  assert.equal(count, 1);
});

test("the renamed Drive-copy flag still keys on matter.duplicate", () => {
  const filter = CorpusView.buildFilter(new Map([["flags", new Set(["duplicate"])]]), "");
  const matched = FLAG_MATTERS.filter(filter);
  assert.deepEqual(matched.map((m) => m.title), ["Drive NDA"]);
  const count = FLAG_MATTERS.filter((m) => CorpusRender.flagMatches(m, "duplicate")).length;
  assert.equal(count, matched.length);
});

test("flags facet ORs within the key across the three duplicate signals", () => {
  const filter = CorpusView.buildFilter(
    new Map([["flags", new Set(["duplicate", "repeat_entity", "duplicate_document"])]]),
    ""
  );
  // Everything except the clean matter.
  assert.deepEqual(FLAG_MATTERS.filter(filter).map((m) => m.title), [
    "Drive NDA",
    "Repeat NDA #1",
    "Repeat NDA #2",
  ]);
});

test("the sidebar rail renders all three flag facets with parity counts", () => {
  const rail = stubNode();
  const payload = { groups: [{ counterparty: "x", matters: FLAG_MATTERS }] };
  CorpusRender.renderFacetRail(rail, payload, new Map(), {});
  const html = rail.innerHTML;
  // Renamed label is honest about being the Drive signal.
  assert.ok(html.includes("Drive copy"), "Drive copy facet label rendered");
  assert.ok(html.includes("Repeat entity"), "Repeat entity facet label rendered");
  assert.ok(html.includes("Duplicate document"), "Duplicate document facet label rendered");
  // Each flag option carries its parity count.
  const optionFor = (value) => {
    const re = new RegExp(
      `data-facet-key="flags" data-facet-value="${value}"[\\s\\S]*?corpus-facet-count">(\\d+)<`
    );
    const m = html.match(re);
    return m ? Number(m[1]) : null;
  };
  assert.equal(optionFor("duplicate"), 1);
  assert.equal(optionFor("repeat_entity"), 2);
  assert.equal(optionFor("duplicate_document"), 1);
});

// --- Option 1: group-header repeat-entity badge -----------------------------
test("the group header shows a 'Repeat entity · N NDAs' badge for a repeat group", () => {
  const list = stubNode();
  const repeatGroup = {
    counterparty: "Repeat Co",
    matters: [FLAG_MATTERS[1], FLAG_MATTERS[2]],
  };
  CorpusRender.renderGroups(list, { groups: [repeatGroup] }, {}, () => true, "counterparty");
  const html = list.innerHTML;
  assert.ok(html.includes("corpus-repeat-entity-badge"), "badge element present");
  assert.ok(html.includes("Repeat entity · 2 NDAs"), `badge text/N wrong: ${html}`);
});

test("the group header omits the badge when no matter is a repeat entity", () => {
  const list = stubNode();
  const cleanGroup = { counterparty: "Clean Co", matters: [FLAG_MATTERS[3]] };
  CorpusRender.renderGroups(list, { groups: [cleanGroup] }, {}, () => true, "counterparty");
  assert.ok(!list.innerHTML.includes("corpus-repeat-entity-badge"));
});

// --- Option 2: duplicate-document card chip links to the match --------------
test("the duplicate-document chip renders the %-match + matched title and links by id", () => {
  const list = stubNode();
  const group = { counterparty: "Repeat Co", matters: [FLAG_MATTERS[2]] };
  CorpusRender.renderGroups(list, { groups: [group] }, {}, () => true, "counterparty");
  const html = list.innerHTML;
  assert.ok(html.includes("corpus-dupdoc-chip"), "chip element present");
  // Links to the matched matter id (the jump target).
  assert.ok(
    html.includes('data-corpus-dupdoc-target="rep-1"'),
    "chip carries the matched_matter_id jump target"
  );
  // 0.88 -> "88% match", arrow, and the matched title.
  assert.ok(html.includes("Duplicate document · 88% match"), `chip label wrong: ${html}`);
  assert.ok(html.includes("Repeat NDA #1"), "chip names the matched title");
});

test("a matter without duplicate_document renders no chip", () => {
  const list = stubNode();
  const group = { counterparty: "Clean Co", matters: [FLAG_MATTERS[3]] };
  CorpusRender.renderGroups(list, { groups: [group] }, {}, () => true, "counterparty");
  assert.ok(!list.innerHTML.includes("corpus-dupdoc-chip"));
});

process.stdout.write(`\ncorpus-model: ${passed} passed\n`);
