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

test("RICH_FACET_KEYS carries the originals + the 6 master-filter facets", () => {
  assert.deepEqual(CorpusModel.RICH_FACET_KEYS, [
    "governing_law",
    "non_solicit",
    "non_compete",
    "mutuality",
    "term_band",
    "review_outcome",
    "restraint_types",
    "clauses_present",
    "origin",
  ]);
  // The two array facets are flagged multi-value; the scalar ones are not.
  assert.deepEqual(CorpusModel.MULTI_FACET_KEYS, ["restraint_types", "clauses_present"]);
  assert.ok(CorpusModel.isMultiFacet("restraint_types"));
  assert.ok(CorpusModel.isMultiFacet("clauses_present"));
  assert.ok(!CorpusModel.isMultiFacet("mutuality"));
});

// --- buildFilter: AND across keys, OR within a key, free text --------------
test("buildFilter combines facets (AND across keys) and free text", () => {
  const matters = [
    { counterparty: "Acme Corp", title: "Acme NDA", status: "reviewed", source: "app", duplicate: false, artifacts: [{ filename: "a.docx" }] },
    { counterparty: "Globex Inc", title: "Globex NDA", status: "sent", source: "drive", duplicate: true, artifacts: [{ filename: "g.pdf" }] },
  ];
  const facets = new Map();
  facets.set("stage", new Set(["reviewed", "sent"])); // OR within a key
  const filterA = CorpusView.buildFilter(facets, "", false);
  assert.deepEqual(matters.filter(filterA).map((m) => m.counterparty), ["Acme Corp", "Globex Inc"]);

  // AND across keys: stage in {reviewed,sent} AND source=drive -> only Globex.
  facets.set("source", new Set(["drive"]));
  const filterB = CorpusView.buildFilter(facets, "", false);
  assert.deepEqual(matters.filter(filterB).map((m) => m.counterparty), ["Globex Inc"]);

  // Free text over counterparty/title/filenames.
  const filterC = CorpusView.buildFilter(new Map(), "acme", false);
  assert.deepEqual(matters.filter(filterC).map((m) => m.counterparty), ["Acme Corp"]);
  const filterD = CorpusView.buildFilter(new Map(), "g.pdf", false);
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
  const nonSolicit = CorpusView.buildFilter(new Map([["non_solicit", new Set(["present"])]]), "", false);
  assert.deepEqual(matters.filter(nonSolicit).map((m) => m.counterparty), ["Both Co", "Solicit Co"]);
  const nonCompete = CorpusView.buildFilter(new Map([["non_compete", new Set(["present"])]]), "", false);
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
  const filter = CorpusView.buildFilter(facets, "", false);
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
  const filter = CorpusView.buildFilter(new Map([["flags", new Set(["repeat_entity"])]]), "", false);
  const matched = FLAG_MATTERS.filter(filter);
  assert.deepEqual(matched.map((m) => m.title), ["Repeat NDA #1", "Repeat NDA #2"]);
  // Sidebar count (flagMatches) == filtered-result count.
  const count = FLAG_MATTERS.filter((m) => CorpusRender.flagMatches(m, "repeat_entity")).length;
  assert.equal(count, matched.length);
  assert.equal(count, 2);
});

test("duplicate_document facet filters matters with a non-null match (count parity)", () => {
  const filter = CorpusView.buildFilter(new Map([["flags", new Set(["duplicate_document"])]]), "", false);
  const matched = FLAG_MATTERS.filter(filter);
  assert.deepEqual(matched.map((m) => m.title), ["Repeat NDA #2"]);
  const count = FLAG_MATTERS.filter((m) => CorpusRender.flagMatches(m, "duplicate_document")).length;
  assert.equal(count, matched.length);
  assert.equal(count, 1);
});

test("the renamed Drive-copy flag still keys on matter.duplicate", () => {
  const filter = CorpusView.buildFilter(new Map([["flags", new Set(["duplicate"])]]), "", false);
  const matched = FLAG_MATTERS.filter(filter);
  assert.deepEqual(matched.map((m) => m.title), ["Drive NDA"]);
  const count = FLAG_MATTERS.filter((m) => CorpusRender.flagMatches(m, "duplicate")).length;
  assert.equal(count, matched.length);
});

test("flags facet ORs within the key across the three duplicate signals", () => {
  const filter = CorpusView.buildFilter(
    new Map([["flags", new Set(["duplicate", "repeat_entity", "duplicate_document"])]]),
    "",
    false
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

// ===========================================================================
// Master-filter facets (mutuality / term_band / restraint_types /
// review_outcome / clauses_present / origin) + the href scheme-allowlist.
// ===========================================================================

// A small corpus carrying every master-filter contract field. Two scalar matters
// plus matters carrying multi-value arrays, so AND/OR/parity can all be exercised.
const MF_MATTERS = [
  {
    counterparty: "Acme", title: "Acme NDA", status: "reviewed", source: "app", artifacts: [],
    facets: {
      mutuality: "mutual", term_band: "<=2y", review_outcome: "clean", origin: "generated",
      restraint_types: ["non_compete", "non_solicit"],
      clauses_present: ["governing_law", "term"],
    },
  },
  {
    counterparty: "Globex", title: "Globex NDA", status: "sent", source: "drive", artifacts: [],
    facets: {
      mutuality: "one_way", term_band: "3-5y", review_outcome: "needs_review", origin: "received",
      restraint_types: ["non_solicit", "non_circumvention"],
      clauses_present: ["governing_law"],
    },
  },
  {
    counterparty: "Initech", title: "Initech NDA", status: "in_review", source: "app", artifacts: [],
    facets: {
      mutuality: "mutual", term_band: ">5y", review_outcome: "has_fail", origin: "generated",
      restraint_types: [],
      clauses_present: ["term"],
    },
  },
];

// Human labels are mapped (not raw values).
test("richFacetValueLabel maps the master-filter scalar facet values", () => {
  assert.equal(CorpusModel.richFacetValueLabel("mutuality", "mutual"), "Mutual");
  assert.equal(CorpusModel.richFacetValueLabel("mutuality", "one_way"), "One-way");
  assert.equal(CorpusModel.richFacetValueLabel("term_band", "<=2y"), "2 years or less");
  assert.equal(CorpusModel.richFacetValueLabel("term_band", "3-5y"), "3–5 years");
  assert.equal(CorpusModel.richFacetValueLabel("term_band", ">5y"), "Over 5 years");
  assert.equal(CorpusModel.richFacetValueLabel("review_outcome", "needs_review"), "Needs review");
  assert.equal(CorpusModel.richFacetValueLabel("origin", "received"), "Received");
  assert.equal(CorpusModel.richFacetValueLabel("restraint_types", "non_circumvention"), "Non-circumvention");
  // Unmapped value falls back to a humanised form.
  assert.equal(CorpusModel.richFacetValueLabel("review_outcome", "weird_state"), "Weird state");
});

test("matterFacetValues reads the multi-value array defensively", () => {
  assert.deepEqual(CorpusModel.matterFacetValues(MF_MATTERS[0], "restraint_types"), ["non_compete", "non_solicit"]);
  assert.deepEqual(CorpusModel.matterFacetValues({ restraint_types: ["non_solicit"] }, "restraint_types"), ["non_solicit"]);
  assert.deepEqual(CorpusModel.matterFacetValues({}, "restraint_types"), []);
  assert.deepEqual(CorpusModel.matterFacetValues({ facets: { restraint_types: "x" } }, "restraint_types"), []); // not an array
});

// Each scalar facet: renders in the rail with the mapped label + parity counts,
// and filters with count == filtered-result parity.
[
  { key: "mutuality", value: "mutual", label: "Mutual", expectN: 2 },
  { key: "term_band", value: "3-5y", label: "3–5 years", expectN: 1 },
  { key: "review_outcome", value: "has_fail", label: "Has fail", expectN: 1 },
  { key: "origin", value: "generated", label: "Generated", expectN: 2 },
].forEach(({ key, value, label, expectN }) => {
  test(`scalar facet ${key} renders (label+count) and filters with parity`, () => {
    const rail = stubNode();
    const payload = { groups: [{ counterparty: "x", matters: MF_MATTERS }] };
    CorpusRender.renderFacetRail(rail, payload, new Map(), {});
    const html = rail.innerHTML;
    assert.ok(html.includes(CorpusModel.RICH_FACET_LABELS[key]), `${key} group title rendered`);
    assert.ok(html.includes(label), `${key} value label "${label}" rendered`);
    const re = new RegExp(
      `data-facet-key="${key}" data-facet-value="${value.replace(/[<>]/g, (c) => (c === "<" ? "&lt;" : "&gt;"))}"[\\s\\S]*?corpus-facet-count">(\\d+)<`
    );
    const m = html.match(re);
    assert.ok(m, `${key}=${value} option rendered with a count`);
    assert.equal(Number(m[1]), expectN, `${key}=${value} sidebar count`);
    // Filter parity: the count equals the number of matters the filter keeps.
    const filter = CorpusView.buildFilter(new Map([[key, new Set([value])]]), "", false);
    assert.equal(MF_MATTERS.filter(filter).length, expectN, `${key}=${value} filtered count == sidebar count`);
  });
});

// Multi-value facets: ANY-match within the group, with count==membership parity.
test("restraint_types (multi-value) counts per-membership and filters ANY-match", () => {
  const rail = stubNode();
  const payload = { groups: [{ counterparty: "x", matters: MF_MATTERS }] };
  CorpusRender.renderFacetRail(rail, payload, new Map(), {});
  const html = rail.innerHTML;
  const countFor = (value) => {
    const m = html.match(new RegExp(`data-facet-key="restraint_types" data-facet-value="${value}"[\\s\\S]*?corpus-facet-count">(\\d+)<`));
    return m ? Number(m[1]) : null;
  };
  // non_compete in 1 matter, non_solicit in 2, non_circumvention in 1.
  assert.equal(countFor("non_compete"), 1);
  assert.equal(countFor("non_solicit"), 2);
  assert.equal(countFor("non_circumvention"), 1);
  // Filter parity: non_solicit -> the 2 matters that carry it (ANY-match).
  const fSolicit = CorpusView.buildFilter(new Map([["restraint_types", new Set(["non_solicit"])]]), "", false);
  assert.deepEqual(MF_MATTERS.filter(fSolicit).map((m) => m.counterparty), ["Acme", "Globex"]);
  assert.equal(MF_MATTERS.filter(fSolicit).length, 2);
  // OR within the group: {non_compete, non_circumvention} -> Acme (compete) + Globex (circ).
  const fOr = CorpusView.buildFilter(new Map([["restraint_types", new Set(["non_compete", "non_circumvention"])]]), "", false);
  assert.deepEqual(MF_MATTERS.filter(fOr).map((m) => m.counterparty), ["Acme", "Globex"]);
  // A matter with an empty array matches nothing.
  const fNone = CorpusView.buildFilter(new Map([["restraint_types", new Set(["non_compete"])]]), "", false);
  assert.ok(!MF_MATTERS.filter(fNone).some((m) => m.counterparty === "Initech"));
});

test("clauses_present (multi-value) filters ANY-match with parity", () => {
  const fGovlaw = CorpusView.buildFilter(new Map([["clauses_present", new Set(["governing_law"])]]), "", false);
  assert.deepEqual(MF_MATTERS.filter(fGovlaw).map((m) => m.counterparty), ["Acme", "Globex"]);
  const fTerm = CorpusView.buildFilter(new Map([["clauses_present", new Set(["term"])]]), "", false);
  assert.deepEqual(MF_MATTERS.filter(fTerm).map((m) => m.counterparty), ["Acme", "Initech"]);
});

// Combination: AND across groups, OR within each group.
test("master-filter facets combine AND-across-groups / OR-within-group", () => {
  // mutuality=mutual (Acme, Initech) AND restraint_types has non_compite/non_solicit...
  // mutuality=mutual AND origin=generated -> Acme + Initech.
  const f1 = CorpusView.buildFilter(new Map([
    ["mutuality", new Set(["mutual"])],
    ["origin", new Set(["generated"])],
  ]), "", false);
  assert.deepEqual(MF_MATTERS.filter(f1).map((m) => m.counterparty), ["Acme", "Initech"]);
  // AND with a multi-value group: mutuality=mutual AND restraint_types∋non_solicit -> Acme only.
  const f2 = CorpusView.buildFilter(new Map([
    ["mutuality", new Set(["mutual"])],
    ["restraint_types", new Set(["non_solicit"])],
  ]), "", false);
  assert.deepEqual(MF_MATTERS.filter(f2).map((m) => m.counterparty), ["Acme"]);
  // OR within a scalar group across groups: term_band∈{<=2y,>5y} AND review_outcome∈{clean,has_fail}
  // -> Acme (<=2y, clean) + Initech (>5y, has_fail); Globex (3-5y, needs_review) excluded.
  const f3 = CorpusView.buildFilter(new Map([
    ["term_band", new Set(["<=2y", ">5y"])],
    ["review_outcome", new Set(["clean", "has_fail"])],
  ]), "", false);
  assert.deepEqual(MF_MATTERS.filter(f3).map((m) => m.counterparty), ["Acme", "Initech"]);
});

test("a degraded master-filter group renders when no matter carries the field", () => {
  const rail = stubNode();
  const bare = [{ counterparty: "x", title: "x", status: "reviewed", source: "app", artifacts: [], facets: {} }];
  CorpusRender.renderFacetRail(rail, { groups: [{ counterparty: "x", matters: bare }] }, new Map(), {});
  // The group title still shows (degraded), but no active option exists for it.
  assert.ok(rail.innerHTML.includes("Mutuality"), "degraded Mutuality group still titled");
  assert.ok(!/data-facet-key="mutuality" data-facet-value="mutual"/.test(rail.innerHTML), "no live mutual option");
});

// --- security: href scheme-allowlist ---------------------------------------
test("safeHref allows http/https and relative URLs", () => {
  assert.equal(CorpusModel.safeHref("https://drive.google.com/x"), "https://drive.google.com/x");
  assert.equal(CorpusModel.safeHref("http://example.com/a?b=1#c"), "http://example.com/a?b=1#c");
  assert.equal(CorpusModel.safeHref("HTTPS://EX.com/x"), "HTTPS://EX.com/x"); // scheme case-insensitive
  assert.equal(CorpusModel.safeHref("/api/download/123"), "/api/download/123");
  assert.equal(CorpusModel.safeHref("relative/path.docx"), "relative/path.docx");
  assert.equal(CorpusModel.safeHref("#frag"), "#frag");
});

test("safeHref blocks javascript: and other hostile schemes", () => {
  assert.equal(CorpusModel.safeHref("javascript:alert(1)"), "");
  assert.equal(CorpusModel.safeHref("JaVaScRiPt:alert(1)"), "");
  // Control-char smuggling — browsers strip the tab, so it must still be blocked.
  assert.equal(CorpusModel.safeHref("java\tscript:alert(1)"), "");
  assert.equal(CorpusModel.safeHref("java\nscript:alert(1)"), "");
  assert.equal(CorpusModel.safeHref(" javascript:alert(1)"), "");
  assert.equal(CorpusModel.safeHref("data:text/html;base64,PHN2Zz4="), "");
  assert.equal(CorpusModel.safeHref("vbscript:msgbox(1)"), "");
  assert.equal(CorpusModel.safeHref("file:///etc/passwd"), "");
  assert.equal(CorpusModel.safeHref("//evil.example.com"), ""); // protocol-relative -> dropped
  assert.equal(CorpusModel.safeHref(null), "");
  assert.equal(CorpusModel.safeHref(""), "");
});

test("renderMatter drops a javascript: open_in_drive_url (no hostile href reaches the DOM)", () => {
  const list = stubNode();
  const evil = {
    matter_id: "m1", counterparty: "Bad Co", title: "Bad NDA", status: "reviewed", source: "drive",
    in_app: false, artifacts: [], open_in_drive_url: "javascript:alert(document.cookie)",
  };
  const safe = {
    matter_id: "m2", counterparty: "Good Co", title: "Good NDA", status: "reviewed", source: "drive",
    in_app: false, artifacts: [], open_in_drive_url: "https://drive.google.com/ok",
  };
  CorpusRender.renderGroups(list, { groups: [{ counterparty: "x", matters: [evil, safe] }] }, {}, () => true, "counterparty");
  const html = list.innerHTML;
  assert.ok(!/javascript:/i.test(html), "no javascript: scheme survived into the markup");
  assert.ok(html.includes("https://drive.google.com/ok"), "the legitimate https Drive link is preserved");
});

test("renderArtifacts never emits a per-file Download button", () => {
  // The in-app download_url returns a broken/error page, not the file, so the
  // Corpus must not offer a per-file Download. Even a well-formed download_url
  // must NOT produce a Download link or surface its href — Corpus files live in
  // Drive. Rows with their own Drive file link show "View in Drive"; rows
  // without one show the non-interactive "In Drive" marker.
  const list = stubNode();
  const matter = {
    matter_id: "m3", counterparty: "Co", title: "NDA", status: "reviewed", source: "app", in_app: true,
    artifacts: [
      { role: "generated", download_url: "javascript:alert(1)" },
      { role: "reviewed", download_url: "https://app.example.com/d/2" },
      { role: "signed", drive_file_url: "https://drive.example.com/file/3" },
    ],
  };
  CorpusRender.renderGroups(list, { groups: [{ counterparty: "x", matters: [matter] }] }, {}, () => true, "counterparty");
  const html = list.innerHTML;
  assert.ok(!/javascript:/i.test(html), "no javascript: href survived");
  assert.ok(!/>Download</i.test(html), "no per-file Download button is rendered");
  assert.ok(!/corpus-artifact-download/.test(html), "the download-link class is gone");
  assert.ok(!html.includes("https://app.example.com/d/2"), "the broken in-app download href is not surfaced");
  // Drive affordances replace it: a per-file Drive link where present, else the
  // "In Drive" marker directing to the matter card's Open in Drive.
  assert.ok(html.includes("https://drive.example.com/file/3"), "a per-file Drive link is surfaced as View in Drive");
  assert.ok(/View in Drive/.test(html), "View in Drive is offered for a Drive-linked file");
  assert.ok(/corpus-artifact-in-drive/.test(html), "rows without a per-file Drive link show the In Drive marker");
});

test("matter card keeps Open in Drive", () => {
  const list = stubNode();
  const matter = {
    matter_id: "m4", counterparty: "Co", title: "NDA", status: "reviewed", source: "drive", in_app: true,
    open_in_drive_url: "https://drive.example.com/folder/m4",
    artifacts: [{ role: "reviewed", download_url: "https://app.example.com/d/9" }],
  };
  CorpusRender.renderGroups(list, { groups: [{ counterparty: "x", matters: [matter] }] }, {}, () => true, "counterparty");
  const html = list.innerHTML;
  assert.ok(/Open in Drive/.test(html), "the matter-card Open in Drive affordance remains");
  assert.ok(html.includes("https://drive.example.com/folder/m4"), "the Drive folder href is preserved");
});

// --- Option A: executed-only default + toggle -------------------------------
// A mixed set: two executed (facets.signed === true), one in-progress
// (signed === false), one unknown (signed === null). The library default must
// show ONLY the two executed; widening shows all four.
const EXEC_MATTERS = [
  { matter_id: "e1", counterparty: "Signed Co", title: "Executed A", status: "reviewed", source: "app", in_app: true, artifacts: [], facets: { signed: true } },
  { matter_id: "e2", counterparty: "Signed Co", title: "Executed B", status: "sent", source: "drive", in_app: false, artifacts: [], facets: { signed: true } },
  { matter_id: "e3", counterparty: "Pending Co", title: "In progress", status: "in_review", source: "app", in_app: true, artifacts: [], facets: { signed: false } },
  { matter_id: "e4", counterparty: "Unknown Co", title: "Unknown", status: "", source: "drive", in_app: false, artifacts: [], facets: { signed: null } },
];

test("isExecuted is true only for a strict facets.signed === true", () => {
  assert.equal(CorpusModel.isExecuted(EXEC_MATTERS[0]), true);
  assert.equal(CorpusModel.isExecuted(EXEC_MATTERS[2]), false, "signed:false is in-progress");
  assert.equal(CorpusModel.isExecuted(EXEC_MATTERS[3]), false, "signed:null is in-progress");
  assert.equal(CorpusModel.isExecuted({}), false, "no facets -> in-progress");
  // Defensive top-level fallback.
  assert.equal(CorpusModel.isExecuted({ signed: true }), true);
  assert.equal(CorpusModel.isExecuted({ executed: true }), true);
});

test("buildFilter DEFAULTS to executed-only (the library)", () => {
  const filter = CorpusView.buildFilter(new Map(), "");
  assert.deepEqual(
    EXEC_MATTERS.filter(filter).map((m) => m.title),
    ["Executed A", "Executed B"]
  );
});

test("buildFilter widened (executedOnly=false) shows ALL matters", () => {
  const filter = CorpusView.buildFilter(new Map(), "", false);
  assert.deepEqual(
    EXEC_MATTERS.filter(filter).map((m) => m.title),
    ["Executed A", "Executed B", "In progress", "Unknown"]
  );
});

test("executedGate is a clean one-predicate gate, on by default, off when widened", () => {
  const onGate = CorpusView.executedGate(true);
  const offGate = CorpusView.executedGate(false);
  assert.equal(onGate(EXEC_MATTERS[0]), true);
  assert.equal(onGate(EXEC_MATTERS[2]), false);
  assert.equal(offGate(EXEC_MATTERS[2]), true, "widened gate admits in-progress");
});

test("the executed gate composes with facet + text filters (AND)", () => {
  // Free text "Signed" matches both Signed Co executed matters; still gated.
  const textFilter = CorpusView.buildFilter(new Map(), "executed");
  assert.deepEqual(
    EXEC_MATTERS.filter(textFilter).map((m) => m.title),
    ["Executed A", "Executed B"]
  );
  // A facet that only the in-progress matter satisfies yields nothing in
  // executed-only mode (gate wins), but matches once widened.
  const stage = new Map([["stage", new Set(["in_review"])]]);
  assert.equal(EXEC_MATTERS.filter(CorpusView.buildFilter(stage, "")).length, 0);
  assert.equal(EXEC_MATTERS.filter(CorpusView.buildFilter(stage, "", false)).length, 1);
});

test("facet rail counts == filtered set in BOTH modes (count parity)", () => {
  const payload = { groups: [{ counterparty: "x", matters: EXEC_MATTERS }] };
  const stageCount = (html, value) => {
    const m = html.match(
      new RegExp(`data-facet-key="stage" data-facet-value="${value}"[\\s\\S]*?corpus-facet-count">(\\d+)<`)
    );
    return m ? Number(m[1]) : null;
  };

  // Executed-only: the in_review stage (only the in-progress matter) counts 0,
  // and that count equals the filtered result for that facet.
  const railExec = stubNode();
  CorpusRender.renderFacetRail(railExec, payload, new Map(), {}, CorpusView.executedGate(true));
  assert.equal(stageCount(railExec.innerHTML, "in_review"), 0, "executed-only hides in_review count");
  assert.equal(stageCount(railExec.innerHTML, "reviewed"), 1, "executed reviewed count");
  const execStageFilter = CorpusView.buildFilter(new Map([["stage", new Set(["in_review"])]]), "", true);
  assert.equal(EXEC_MATTERS.filter(execStageFilter).length, 0, "count == filtered (executed-only)");

  // Widened: the in_review stage now counts 1, matching the filtered result.
  const railAll = stubNode();
  CorpusRender.renderFacetRail(railAll, payload, new Map(), {}, CorpusView.executedGate(false));
  assert.equal(stageCount(railAll.innerHTML, "in_review"), 1, "widened reveals in_review count");
  const allStageFilter = CorpusView.buildFilter(new Map([["stage", new Set(["in_review"])]]), "", false);
  assert.equal(EXEC_MATTERS.filter(allStageFilter).length, 1, "count == filtered (widened)");
});

process.stdout.write(`\ncorpus-model: ${passed} passed\n`);
