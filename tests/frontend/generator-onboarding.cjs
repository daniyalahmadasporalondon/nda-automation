"use strict";

// Generator FIRST-RUN ONBOARDING panel.
//
// A brand-new user opening the Generator tab should see a distinct card at the
// top of the view telling them what the page is for ("Draft a new NDA" — pick
// entity + counterparty, set the term, then Generate). The panel:
//   * shows on the first activation of the Generator tab,
//   * self-hides the moment the user engages (any form interaction, or a
//     generate) OR clicks its dismiss "×",
//   * remembers the dismissal in localStorage so it never returns.
//
// The panel is a self-contained element at the TOP of the Generator view (it
// deliberately does NOT touch the intake form itself). It reuses the shared
// .repository-onboarding-card markup/classes so a later integrator can lift the
// repository/corpus/generator onboarding cards into one component.
//
// Two parts:
//   1. SOURCE GUARDS: index.html carries the panel (right classes + copy + the
//      dismiss control) and draft-intake.js carries the show/hide/persist logic.
//   2. DOM CONTROLLER (Playwright): the real panel markup + the real
//      draft-intake.js controller + the real module (window.createDraftIntake).
//      Drive activate()/interaction/dismiss and assert the hidden flag + the
//      persisted localStorage key, exactly as the shipped page runs.
//
// Run: node tests/frontend/generator-onboarding.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const MJS_PATH = path.join(ROOT, "static/js/modules/draft-intake.mjs");
const DRAFT_INTAKE_SRC = fs.readFileSync(path.join(ROOT, "static/js/draft-intake.js"), "utf8");
const MJS_SRC = fs.readFileSync(MJS_PATH, "utf8");
const INDEX_SRC = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");

// A browser-eval'able copy of the module (same technique the term-unit test
// uses): strip `export ` and expose the surface the shipped global-bridge wires.
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
  // index.html: a distinct onboarding panel in the Generator view, reusing the
  // shared onboarding-card classes and carrying a stable data hook + dismiss.
  assert.ok(
    /data-generator-onboarding\b/.test(INDEX_SRC),
    "index.html carries a [data-generator-onboarding] panel",
  );
  assert.ok(
    /data-generator-onboarding-dismiss\b/.test(INDEX_SRC),
    "the generator onboarding panel carries a dismiss control",
  );
  assert.ok(
    /repository-onboarding-card/.test(INDEX_SRC),
    "the panel reuses the shared .repository-onboarding-card markup",
  );
  // The required copy is present verbatim (title, lead, and the step hint).
  assert.ok(/Draft a new NDA/.test(INDEX_SRC), "panel title is 'Draft a new NDA'");
  assert.ok(
    /Create a fresh NDA from your playbook\./.test(INDEX_SRC),
    "panel lead names the playbook",
  );
  assert.ok(
    /Pick your signing entity and the counterparty/.test(INDEX_SRC) &&
      /send it for signature/.test(INDEX_SRC),
    "panel hint walks pick -> set term -> Generate -> download/send/sign",
  );

  // The panel must sit ABOVE the intake form (top of the view) and must NOT be
  // inside the form (another agent owns the form; onboarding stays out of it).
  const panelIdx = INDEX_SRC.indexOf("data-generator-onboarding");
  const formIdx = INDEX_SRC.indexOf('id="draftIntakeForm"');
  assert.ok(panelIdx > -1 && formIdx > -1 && panelIdx < formIdx, "panel is rendered before the intake form");

  // draft-intake.js: the controller shows/hides the panel and persists dismissal.
  assert.ok(
    /onboardingNode/.test(DRAFT_INTAKE_SRC),
    "controller accepts an onboardingNode",
  );
  assert.ok(
    /nda\.generator\.onboardingDismissed/.test(DRAFT_INTAKE_SRC),
    "controller persists dismissal under a stable localStorage key",
  );
}

// --- DOM controller assertions (Playwright) ---------------------------------
async function runDomTests() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  try {
    // The real onboarding panel + a minimal form (the controller only needs the
    // form node to bind its interaction-dismiss listener). Kept in sync with
    // index.html by the source guards above.
    await page.setContent(`
      <div class="repository-onboarding generator-onboarding" data-generator-onboarding hidden>
        <div class="repository-onboarding-card">
          <button type="button" data-generator-onboarding-dismiss aria-label="Dismiss">×</button>
          <h2 class="repository-onboarding-title">Draft a new NDA</h2>
          <p class="repository-onboarding-lead">Create a fresh NDA from your playbook.</p>
        </div>
      </div>
      <form id="draftForm">
        <input id="draftIntakeCounterpartyName" type="text">
      </form>
    `);

    await page.addScriptTag({ content: MJS_AS_SCRIPT });
    await page.addScriptTag({ content: DRAFT_INTAKE_SRC });

    // A plain-object-backed storage matching the localStorage seam the controller
    // reads (onboardingStorage). We inject it explicitly so the test never touches
    // the real localStorage — page.setContent runs on an opaque origin where
    // localStorage access is denied — and so this exercises the very seam the
    // controller exposes for persistence. Persisted across boots on window.__store.
    async function installStore() {
      await page.evaluate(() => {
        const backing = {};
        window.__store = {
          getItem: (k) => (k in backing ? backing[k] : null),
          setItem: (k, v) => {
            backing[k] = String(v);
          },
          clear: () => {
            for (const k of Object.keys(backing)) delete backing[k];
          },
        };
      });
    }

    // Boot a controller wired to the onboarding nodes + the form + the injected
    // storage seam. A helper so each scenario re-activates against the same store.
    async function bootController() {
      await page.evaluate(async () => {
        const $ = (sel) => document.querySelector(sel);
        window.__controller = createDraftIntakeController({
          form: $("#draftForm"),
          counterpartyNameInput: $("#draftIntakeCounterpartyName"),
          onboardingNode: $("[data-generator-onboarding]"),
          onboardingDismissButton: $("[data-generator-onboarding-dismiss]"),
          onboardingStorage: window.__store,
        });
        await window.__controller.activate();
      });
    }

    const panelHidden = () =>
      page.evaluate(() => document.querySelector("[data-generator-onboarding]").hidden);
    const dismissedFlag = () =>
      page.evaluate(() => window.__store.getItem("nda.generator.onboardingDismissed"));

    // 1. Fresh user: activate() reveals the panel.
    await installStore();
    await bootController();
    assert.equal(await panelHidden(), false, "the panel is shown on first activation");
    assert.equal(await dismissedFlag(), null, "nothing persisted merely by showing it");
    ok("a fresh user sees the onboarding panel on first Generator activation");

    // 2. Interacting with the form retires the panel AND remembers it.
    await page.fill("#draftIntakeCounterpartyName", "Acme");
    assert.equal(await panelHidden(), true, "typing in the form hides the panel");
    assert.equal(await dismissedFlag(), "1", "interaction persists the dismissal");
    ok("engaging with the form hides the panel and remembers the dismissal");

    // 3. A returning user (flag already set) never sees the panel again.
    await bootController();
    assert.equal(await panelHidden(), true, "a dismissed panel stays hidden on re-activation");
    ok("a returning user does not see the panel again");

    // 4. The dismiss "×" also hides + persists (independent of form interaction).
    await page.evaluate(() => window.__store.clear());
    await bootController();
    assert.equal(await panelHidden(), false, "shown again once the flag is cleared");
    await page.click("[data-generator-onboarding-dismiss]");
    assert.equal(await panelHidden(), true, "the dismiss button hides the panel");
    assert.equal(await dismissedFlag(), "1", "the dismiss button persists the dismissal");
    ok("the dismiss button hides the panel and remembers it");
  } finally {
    await browser.close();
  }
}

async function main() {
  runSourceGuards();
  ok("source guards: panel markup + copy + persist logic");
  await runDomTests();
  process.stdout.write(`\n${passed} checks passed\n`);
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
