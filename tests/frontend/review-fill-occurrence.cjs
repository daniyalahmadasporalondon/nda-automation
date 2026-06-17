// Targeted regression test for the Fill tool's clean-fill occurrence precision.
//
// review-fill.js is a plain browser script (no module exports), so we load its
// source into a Node `vm` sandbox with minimal `window`/document stubs, capture
// the `createFillController` factory, and drive its detection + apply path
// directly against in-memory paragraph state. We assert the SPECIFIC detected
// occurrence is rewritten — not indexOf's first match — and that all three
// paragraph baselines stay in sync.
//
// Run: node tests/frontend/review-fill-occurrence.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const SOURCE = fs.readFileSync(path.join(ROOT, "static/js/review-fill.js"), "utf8");

// ── A tiny entity registry stub matching the createDraftIntake surface the
// controller leans on (selectedEntity / formatAddressLines / entities, etc.). The
// chosen entity is the "target" we fill blanks / swap identities with.
function makeDraftIntakeStub(entities) {
  return function createDraftIntake() {
    let chosenId = null;
    let chosenAddressId = null;
    const find = (id) => entities.find((e) => e.id === id) || null;
    return {
      entities,
      createInitialIntake() {
        return { entityId: chosenId, addressId: chosenAddressId };
      },
      applyEntitySelection(intake, id) {
        chosenId = id || null;
        const entity = find(id);
        chosenAddressId = entity && entity.addresses && entity.addresses[0]
          ? entity.addresses[0].id
          : null;
        return { entityId: chosenId, addressId: chosenAddressId };
      },
      selectAddress(intake, addressId) {
        chosenAddressId = addressId || null;
        return { entityId: chosenId, addressId: chosenAddressId };
      },
      selectedEntity(intake) {
        return find(intake && intake.entityId);
      },
      selectedAddress(intake) {
        const entity = find(intake && intake.entityId);
        if (!entity) return null;
        const addrId = intake && intake.addressId;
        return (entity.addresses || []).find((a) => a.id === addrId)
          || (entity.addresses || [])[0]
          || null;
      },
      hasMultipleAddresses(entity) {
        return Boolean(entity && entity.addresses && entity.addresses.length > 1);
      },
      formatAddressLines(address) {
        return address ? String(address.text || "") : "";
      },
      entityLabel(entity) {
        return entity ? String(entity.legal_name || entity.short_name || entity.id) : "";
      },
      effectiveGoverningLaw() {
        return null;
      },
    };
  };
}

// Sets the same paragraph text across all three baselines (mirrors how the
// viewer snapshots reviewParagraphs into the two originals at load time).
function setParagraphs(state, paragraphs) {
  const clone = () => paragraphs.map((p) => ({ ...p }));
  state.reviewParagraphs = clone();
  state.reviewOriginalParagraphs = clone();
  state.reviewExportOriginalParagraphs = clone();
}

// Loads review-fill.js into a fresh sandbox and returns a controller whose
// internal detection + apply closures (detectInserts / detectReplacements /
// applyFills / workingFor / api / entity-select) are surfaced for the test, so we
// can drive the real detection → record → applyCleanFill path end to end without
// a DOM or a render.
function loadInternals(entities) {
  const sandbox = {
    console,
    document: { getElementById() { return null; } },
    fetch() { return Promise.reject(new Error("no network in test")); },
  };
  sandbox.window = {
    createDraftIntake: makeDraftIntakeStub(entities),
    escapeHtml: (v) => String(v == null ? "" : v),
  };
  sandbox.global = sandbox;
  vm.createContext(sandbox);
  // Surface the internal detect/apply functions by appending a controller wrapper
  // that returns them. We patch the factory to also expose its closures.
  const patched = SOURCE.replace(
    "return { render, clearHighlights: clearDocHighlights, highlightDocument };",
    "return { render, clearHighlights: clearDocHighlights, highlightDocument, "
      + "__detectInserts: detectInserts, __detectReplacements: detectReplacements, "
      + "__applyFills: applyFills, __workingFor: workingFor, __api: api, "
      + "__applyEntity: (id) => { pick = entityApi.applyEntitySelection(pick, id); } };",
  );
  vm.runInContext(`${patched}\n;globalThis.__createFillController = createFillController;`, sandbox);
  const createFillController = sandbox.__createFillController;
  const state = {
    reviewParagraphs: [],
    reviewOriginalParagraphs: [],
    reviewExportOriginalParagraphs: [],
    filledBlanks: [],
  };
  const controller = createFillController({ state, root: null, rerenderDocument() {} });
  controller.__api(); // init entityApi + pick
  return { controller, state };
}

const ENTITIES = [
  {
    id: "aspora-uk",
    short_name: "Aspora",
    legal_name: "Aspora Technology Services Limited",
    addresses: [{ id: "uk-1", label: "London", text: "1 King Street, London, EC1A 1AA" }],
  },
  {
    id: "vance-in",
    short_name: "Vance",
    legal_name: "Vance Money Private Limited",
    addresses: [{ id: "in-1", label: "Bangalore", text: "42 MG Road, Bangalore, 560001" }],
  },
];

function selectAspora(controller) {
  controller.__applyEntity("aspora-uk");
}

// ── Scenario 1: the same INSERT blank token appears twice in one paragraph; the
// fill flagged for the SECOND occurrence must rewrite the SECOND and leave the
// first untouched.
function testRepeatedBlankFillsFlaggedOccurrence() {
  const { controller, state } = loadInternals(ENTITIES);
  selectAspora(controller);

  // Two identical name blanks in one paragraph. classifyBlank marks each as a
  // party NAME via the trailing ', a company incorporated' tail.
  const text =
    "This Agreement is between ____________, a company incorporated under the laws of England "
    + "(the \"Recipient\"), and ____________, a company incorporated under the laws of England "
    + "(the \"Company\").";
  setParagraphs(state, [{ id: "p1", index: 1, text }]);

  const inserts = controller.__detectInserts();
  const blankOffsets = inserts.map((c) => c.offset);
  assert.equal(inserts.length, 2, "two name blanks should be detected");
  assert.ok(blankOffsets[0] < blankOffsets[1], "offsets ordered");

  const secondOffset = blankOffsets[1];
  const firstOffset = blankOffsets[0];

  // Enable ONLY the second blank; clean mode.
  inserts.forEach((c) => {
    const work = controller.__workingFor(c);
    work.enabled = c.offset === secondOffset;
    work.mode = "clean";
  });

  controller.__applyFills(inserts);

  const filledText = state.reviewParagraphs[0].text;
  const name = "Aspora Technology Services Limited";

  // The SECOND blank is now the entity name; the FIRST blank is still blank.
  assert.equal(
    filledText.indexOf("____________"),
    firstOffset,
    "the FIRST blank must remain untouched",
  );
  assert.ok(filledText.includes(name), "the chosen entity name must be inserted");
  // The inserted name must sit where the second blank was — i.e. after the first
  // (still-present) blank, before the '(the \"Company\")' tail.
  const nameAt = filledText.indexOf(name);
  assert.ok(nameAt > firstOffset, "name inserted at the SECOND (later) position, not the first");
  assert.ok(
    filledText.slice(nameAt).includes('(the "Company")'),
    "the second occurrence (the Company side) got the name",
  );
  // Exactly one blank remains.
  assert.equal(
    (filledText.match(/____________/g) || []).length,
    1,
    "exactly one blank should remain after a single fill",
  );

  // All three baselines stay in sync.
  assert.equal(state.reviewOriginalParagraphs[0].text, filledText, "reviewOriginalParagraphs in sync");
  assert.equal(
    state.reviewExportOriginalParagraphs[0].text,
    filledText,
    "reviewExportOriginalParagraphs in sync",
  );
}

// ── Scenario 2: the Aspora entity name appears alongside a SIMILAR string in the
// same paragraph, and the blank we flag is the SECOND name slot. The fill must
// land on the flagged blank's exact detected offset — never on the look-alike
// name text that sits earlier in the paragraph. (Detection's classifyBlank flags
// the empty slots; the literal Aspora name nearby is a decoy for any code that
// re-searches the paragraph instead of honoring the detected offset.)
function testAsporaNameFillsFlaggedSlotNotLookalike() {
  const { controller, state } = loadInternals(ENTITIES);
  selectAspora(controller);
  const aspora = "Aspora Technology Services Limited"; // the value we will insert

  // The chosen entity's own name appears verbatim in a recital (the "similar
  // string" decoy), then two genuine empty NAME blanks follow. We flag the SECOND
  // blank. Buggy code that rewrote text.indexOf(value-or-find) could collide with
  // the recital text; the fix splices at the recorded offset of the flagged blank.
  const text =
    `Reference is made to ${aspora} under a prior arrangement. `
    + `This Agreement is between ____________, a company incorporated under English law `
    + `(the "Discloser"), and ____________, a company incorporated under English law `
    + `(the "Recipient").`;
  setParagraphs(state, [{ id: "p2", index: 4, text }]);

  const inserts = controller.__detectInserts();
  const blanks = inserts.filter((c) => c.find === "____________").sort((a, b) => a.offset - b.offset);
  assert.equal(blanks.length, 2, "two empty name blanks should be detected");

  const firstBlankOffset = blanks[0].offset;
  const secondBlankOffset = blanks[1].offset;
  // The decoy (literal Aspora name) sits BEFORE both blanks.
  assert.ok(text.indexOf(aspora) < firstBlankOffset, "decoy name precedes the blanks");

  // Flag ONLY the second blank (the Recipient side).
  inserts.forEach((c) => {
    const work = controller.__workingFor(c);
    work.enabled = c.offset === secondBlankOffset;
    work.mode = "clean";
  });

  controller.__applyFills(inserts);

  const out = state.reviewParagraphs[0].text;

  // The first blank is still blank; exactly one blank remains.
  assert.equal(out.indexOf("____________"), firstBlankOffset, "the FIRST blank stays empty");
  assert.equal(
    (out.match(/____________/g) || []).length,
    1,
    "exactly one blank remains — the second was filled, the first untouched",
  );
  // The recital decoy is preserved (not consumed/duplicated): the Aspora name now
  // appears exactly twice — the original recital + the newly filled second slot.
  assert.equal(
    (out.split(aspora).length - 1),
    2,
    "decoy recital name preserved AND the flagged slot filled (two total)",
  );
  // The fill sits at the second blank's position (after the first blank), not at
  // the recital decoy.
  const lastAt = out.lastIndexOf(aspora);
  assert.ok(lastAt > firstBlankOffset, "the inserted name is at the SECOND slot, past the first blank");
  assert.ok(out.slice(lastAt).includes('(the "Recipient")'), "the Recipient-side slot got the name");

  // Baselines stay in sync.
  assert.equal(state.reviewOriginalParagraphs[0].text, out, "reviewOriginalParagraphs in sync");
  assert.equal(state.reviewExportOriginalParagraphs[0].text, out, "reviewExportOriginalParagraphs in sync");
}

// ── Guard: with no recorded offset, an AMBIGUOUS token is skipped (never guesses
// the first of several), but an unambiguous one still applies (legacy fills).
function testLegacyAmbiguousOffsetIsSkipped() {
  const { controller, state } = loadInternals(ENTITIES);
  selectAspora(controller);
  setParagraphs(state, [{ id: "p3", index: 2, text: "Foo ____________ and ____________ bar." }]);

  const inserts = controller.__detectInserts();
  // Strip the recorded offsets to simulate a legacy/driftless record.
  inserts.forEach((c) => {
    c.offset = null;
    const work = controller.__workingFor(c);
    work.enabled = true;
    work.mode = "clean";
  });
  // Detection classifies these as NOT name/address (no party tail), so guard via
  // an explicit fabricated candidate set if detection found none.
  const candidates = inserts.length
    ? inserts
    : [{
        id: "ins-p3-x", mode: "insert", slot: "name", paragraph_id: "p3",
        paragraph_index: 2, find: "____________", offset: null,
        context: state.reviewParagraphs[0].text,
      }];
  candidates.forEach((c) => {
    const work = controller.__workingFor(c);
    work.enabled = true;
    work.mode = "clean";
  });
  controller.__applyFills(candidates);
  // Ambiguous + no offset → skipped: both blanks remain.
  assert.equal(
    (state.reviewParagraphs[0].text.match(/____________/g) || []).length,
    2,
    "ambiguous offset-less fill must be skipped, not applied to the first match",
  );
}

const tests = [
  ["repeated blank fills the flagged (second) occurrence, not the first", testRepeatedBlankFillsFlaggedOccurrence],
  ["aspora name fills the flagged slot, not a look-alike string", testAsporaNameFillsFlaggedSlotNotLookalike],
  ["legacy offset-less fill skips an ambiguous repeated token", testLegacyAmbiguousOffsetIsSkipped],
];

let failures = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`ok  - ${name}`);
  } catch (error) {
    failures += 1;
    console.error(`FAIL - ${name}`);
    console.error(error && error.stack ? error.stack : error);
  }
}

if (failures) {
  console.error(`\n${failures} test(s) failed`);
  process.exit(1);
}
console.log(`\nall ${tests.length} review-fill occurrence tests passed`);
