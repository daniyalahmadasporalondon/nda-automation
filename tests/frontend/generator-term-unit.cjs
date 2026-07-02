"use strict";

// Generator TERM unit selector (Years / Months).
//
// The Generator's Term field is a +/- stepper with a Years/Months unit selector.
// Internally everything normalises against the Playbook year cap (max_term_years,
// default 5); the months cap is DERIVED as max_term_years * 12 (== 60). A months
// value over the cap snaps to the cap (with a visible note); a sub-year months
// value (18) is preserved. The built payload carries BOTH the term text and the
// chosen unit (term_unit) so the backend never has to guess the unit.
//
// Two parts:
//   1. PURE MODULE (dynamic import of the .mjs): createInitialIntake carries a
//      termUnit; buildDraftPayload emits term + term_unit for both units.
//   2. DOM CONTROLLER (Playwright): the real index.html stepper markup + the real
//      draft-intake.js controller + the real module (exposed as
//      window.createDraftIntake). Drive the unit <select> and assert the hint
//      updates, an out-of-range value clamps with a note, and the payload carries
//      the unit.
//
// Run: node tests/frontend/generator-term-unit.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const url = require("node:url");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const MJS_PATH = path.join(ROOT, "static/js/modules/draft-intake.mjs");
const DRAFT_INTAKE_SRC = fs.readFileSync(path.join(ROOT, "static/js/draft-intake.js"), "utf8");
const MJS_SRC = fs.readFileSync(MJS_PATH, "utf8");
const INDEX_SRC = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");

// A browser-eval'able copy of the module: strip the `export ` keyword (the module
// has no internal imports — it is self-contained) and expose createDraftIntake +
// createInitialIntake on window, exactly the surface the shipped global-bridge
// wires. This lets the classic draft-intake.js controller resolve
// window.createDraftIntake the same way it does in production.
const MJS_AS_SCRIPT =
  MJS_SRC.replace(/^export\s+/gm, "") +
  "\n;window.createDraftIntake = createDraftIntake;" +
  "\nwindow.__createInitialIntake = createInitialIntake;";

let passed = 0;
function ok(name) {
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

// --- Source guards ----------------------------------------------------------
function runSourceGuards() {
  // index.html: the static "years" span is replaced by a Years/Months <select>.
  assert.ok(
    /id="draftIntakeTermUnit"[^>]*class="term-stepper-unit-select"/.test(INDEX_SRC) ||
      /class="term-stepper-unit-select"[^>]*id="draftIntakeTermUnit"/.test(INDEX_SRC),
    "index.html term stepper carries a Years/Months unit <select>#draftIntakeTermUnit",
  );
  assert.ok(
    /<option value="years">years<\/option>/.test(INDEX_SRC) &&
      /<option value="months">months<\/option>/.test(INDEX_SRC),
    "the unit select offers years and months options",
  );

  // draft-intake.mjs: payload carries term_unit; initial intake carries termUnit.
  assert.ok(/term_unit:/.test(MJS_SRC), "buildDraftPayload emits term_unit");
  assert.ok(/termUnit:/.test(MJS_SRC), "createInitialIntake carries termUnit");

  // draft-intake.js: the controller tracks the unit, clamps per unit against the
  // derived months cap, and surfaces an adjustment note (no silent clamp).
  assert.ok(/maxTermYears \* 12/.test(DRAFT_INTAKE_SRC), "controller derives the months cap as maxTermYears * 12");
  assert.ok(/Adjusted to /.test(DRAFT_INTAKE_SRC), "controller surfaces a visible adjustment note when the cap changes a value");
  assert.ok(
    !/const\s+MONTHS_CAP\s*=\s*60\b/.test(DRAFT_INTAKE_SRC),
    "the months cap is not hardcoded to 60 (derived from the playbook)",
  );
}

// --- Pure module assertions (dynamic import) --------------------------------
async function runModuleTests() {
  const mod = await import(url.pathToFileURL(MJS_PATH).href);
  const { createInitialIntake, buildDraftPayload, SIGNING_ENTITIES } = mod;

  const initial = createInitialIntake();
  assert.equal(initial.termUnit, "years", "initial intake defaults to years");
  ok("createInitialIntake defaults termUnit to years");

  const entity = SIGNING_ENTITIES[0];
  const base = {
    ...createInitialIntake(),
    counterpartyName: "Acme",
    entityId: entity.id,
    governingLawId: null,
  };

  const yearsPayload = buildDraftPayload({ ...base, term: "3 years", termUnit: "years" });
  assert.equal(yearsPayload.term, "3 years");
  assert.equal(yearsPayload.term_unit, "years");
  ok("buildDraftPayload emits term + term_unit=years");

  const monthsPayload = buildDraftPayload({ ...base, term: "18 months", termUnit: "months" });
  assert.equal(monthsPayload.term, "18 months");
  assert.equal(monthsPayload.term_unit, "months");
  ok("buildDraftPayload emits term + term_unit=months");

  // A malformed/absent unit falls back to years (never leaks an arbitrary value).
  const oddPayload = buildDraftPayload({ ...base, term: "2 years", termUnit: "weeks" });
  assert.equal(oddPayload.term_unit, "years");
  ok("buildDraftPayload coerces an unknown unit to years");
}

// --- DOM controller assertions (Playwright) ---------------------------------
async function runDomTests() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  try {
    // The real index.html term-stepper markup (self-contained; the controller
    // wires these ids). Kept in sync with index.html by the source guard above.
    await page.setContent(`
      <form id="draftForm">
        <span id="draftIntakeTermLabel">Term</span>
        <div class="term-stepper">
          <button class="term-stepper-btn" id="draftIntakeTermDecrement" type="button">-</button>
          <input id="draftIntakeTerm" class="term-stepper-input" type="number" min="1" step="1" value="2">
          <select id="draftIntakeTermUnit" class="term-stepper-unit-select">
            <option value="years">years</option>
            <option value="months">months</option>
          </select>
          <button class="term-stepper-btn" id="draftIntakeTermIncrement" type="button">+</button>
        </div>
        <p class="term-stepper-hint" id="draftIntakeTermHint"></p>
      </form>
    `);

    // Expose the module surface (window.createDraftIntake) then load the real
    // controller as a classic script, exactly as the shipped page does.
    await page.addScriptTag({ content: MJS_AS_SCRIPT });
    await page.addScriptTag({ content: DRAFT_INTAKE_SRC });

    // Boot the controller against the real stepper nodes. activate() loads the
    // registry (the /api/signing-entities fetch fails on this blank page and
    // gracefully falls back to the embedded mirror), initialises the intake, and
    // seeds the stepper to the default term — the same path the app runs.
    await page.evaluate(async () => {
      const $ = (id) => document.getElementById(id);
      window.__controller = createDraftIntakeController({
        form: $("draftForm"),
        termInput: $("draftIntakeTerm"),
        termDecrementButton: $("draftIntakeTermDecrement"),
        termIncrementButton: $("draftIntakeTermIncrement"),
        termHintNode: $("draftIntakeTermHint"),
        termUnitNode: $("draftIntakeTermUnit"),
      });
      await window.__controller.activate();
    });

    // Default (years): hint names the 5-year cap.
    let hint = await page.textContent("#draftIntakeTermHint");
    assert.ok(/caps the term at 5 years/.test(hint), `years hint should name the 5-year cap, got: ${hint}`);
    ok("years hint names the 5-year cap");

    // Switch to months: hint flips to the DERIVED 60-month cap.
    await page.selectOption("#draftIntakeTermUnit", "months");
    hint = await page.textContent("#draftIntakeTermHint");
    assert.ok(/caps the term at 60 months/.test(hint), `months hint should name the 60-month cap, got: ${hint}`);
    // 2 years converts to 24 months.
    const afterSwitch = await page.inputValue("#draftIntakeTerm");
    assert.equal(afterSwitch, "24", "switching years->months converts 2 -> 24");
    ok("switching to months updates the hint (60) and converts the value (2y -> 24mo)");

    // Type an over-cap months value: it clamps to 60 WITH a visible note.
    // page.fill dispatches the input event itself (do NOT dispatch a second one —
    // the re-fired handler would read the already-clamped 60 and drop the note).
    await page.fill("#draftIntakeTerm", "99");
    const clamped = await page.inputValue("#draftIntakeTerm");
    assert.equal(clamped, "60", "99 months clamps to the 60-month cap");
    hint = await page.textContent("#draftIntakeTermHint");
    assert.ok(/Adjusted to 60 months/.test(hint), `an out-of-range value must surface an adjustment note, got: ${hint}`);
    ok("an over-cap months value clamps to 60 with a visible note (no silent clamp)");

    // The built payload carries the months unit AND the months term text. The
    // controller keeps its intake private, so we build the payload through the
    // same module helper (buildDraftPayload) the controller uses, from an intake
    // shaped exactly like the one the stepper just produced (60 months).
    const payload = await page.evaluate(() => {
      const api = window.createDraftIntake({});
      const intake = {
        ...api.createInitialIntake(),
        counterpartyName: "Acme",
        entityId: api.entities[0].id,
        term: "60 months",
        termUnit: "months",
      };
      return api.buildDraftPayload(intake);
    });
    assert.equal(payload.term_unit, "months", "payload carries term_unit=months");
    assert.ok(/months/.test(payload.term), "payload term text is in months");
    ok("the built payload carries the months unit");
  } finally {
    await browser.close();
  }
}

async function main() {
  runSourceGuards();
  ok("source guards: unit selector + term_unit payload + derived months cap");
  await runModuleTests();
  await runDomTests();
  process.stdout.write(`\n${passed} checks passed\n`);
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
