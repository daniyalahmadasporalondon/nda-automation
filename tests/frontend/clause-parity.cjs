// Browser proof for the 5-clause round-trip PARITY feature.
//
// Brings the EXISTING (native) policy clauses up to the same editability the
// Add-Clause editor gave new (dynamic) clauses, and proves EACH newly-editable
// lever actually propagates end-to-end (live AI packet + deterministic checker +
// generation), all driven from a real browser against the live app.
//
// Proves IN A REAL BROWSER + against the published playbook:
//   1. mutuality (native): the trigger-term chip editor is LIVE (was read-only).
//      Add a distinctive search term + a distinctive semantic signal + edit a fail
//      condition description, Validate -> valid, Save + Publish -> 200.
//   2. PROPAGATION (the heart of the feature): after publish, the published
//      playbook read back over HTTP carries the edited search_term, semantic
//      signal, and condition; AND a Python probe against the published playbook on
//      disk shows the new search term reaches BOTH consumer paths:
//        * the deterministic checker (nda_automation.checks.mutuality reads
//          search_terms), and
//        * the AI packet (playbook_rules.clause_rules_for_ai carries the edited
//          semantic_signal + condition).
//   3. governing_law / term_and_survival (derived clauses): the preferred-position
//      box is shown READ-ONLY (derived; editing it would be inert), while the REAL
//      live lever (jurisdiction list / max_term_years) is editable -- nothing is
//      editable-but-isn't.
//   4. NEGATIVE: stripping every search term off a native clause makes Validate
//      flag it invalid (the trigger_terms_present publish-lint gate blocks it).
//
// The server runs the key-free AI assessment stub (NDA_AI_ASSESSMENT_STUB=1),
// Gmail sync HARD-OFF, a throwaway data dir, on a free port (never 8787).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = process.env.PYTHON || "python3";
const PORT = Number(process.env.CLAUSE_PARITY_PORT || 25000 + Math.floor(Math.random() * 1000));
const BASE_URL = `http://127.0.0.1:${PORT}`;
const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "clause-parity-data-"));
const SHOTS_DIR = process.env.CLAUSE_PARITY_SHOTS || DATA_DIR;

// Distinctive tokens so a propagation match cannot be a coincidence with seeded text.
const MUT_SEARCH_TERM = "zzbespoke mutuality marker";
const MUT_SEMANTIC_SIGNAL = "zzbespoke reciprocity signal";
const MUT_FAIL_DESC = "zzbespoke one-way obligation appears in operative form.";

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve();
      });
      req.on("error", () => {
        if (Date.now() > deadline) reject(new Error("server did not start"));
        else setTimeout(tick, 200);
      });
    };
    tick();
  });
}

async function selectClause(page, clauseId) {
  await page.click(`#playbookList .playbook-row[data-clause-id="${clauseId}"]`);
  await page.waitForSelector("#clauseDetail #playbookEditor");
}

async function validateState(page) {
  await page.click("#validatePlaybookButton");
  await page.waitForFunction(
    () => {
      const el = document.querySelector("#playbookValidation");
      return el && (el.dataset.state === "valid" || el.dataset.state === "invalid");
    },
    { timeout: 15000 }
  );
  return page.$eval("#playbookValidation", (el) => el.dataset.state);
}

// Probe the PUBLISHED playbook on disk through the real engine consumer paths.
function probePublishedPlaybook() {
  const script = `
import json, sys
from nda_automation.checker import load_playbook
from nda_automation.checks.mutuality import _clause_terms
from nda_automation.playbook_rules import clause_rules_for_ai

pb = load_playbook()
mut = next(c for c in pb["clauses"] if c["id"] == "mutuality")
det_terms = _clause_terms(mut, "search_terms")           # deterministic checker path
packet = clause_rules_for_ai(mut)                          # AI packet path
out = {
  "det_search_terms": det_terms,
  "packet_semantic_signals": packet.get("semantic_signals", []),
  "packet_fail_descriptions": [
    str(c.get("description") or "")
    for c in (packet.get("rules", {}).get("fail_conditions") or [])
  ],
}
print(json.dumps(out))
`;
  const res = spawnSync(PYTHON, ["-c", script], {
    cwd: ROOT,
    env: { ...process.env, NDA_DATA_DIR: DATA_DIR },
    encoding: "utf8",
  });
  if (res.status !== 0) {
    throw new Error(`probe failed: ${res.stderr || res.stdout}`);
  }
  return JSON.parse(res.stdout.trim().split("\n").pop());
}

async function main() {
  assert.notEqual(PORT, 8787, "must never use 8787");
  const server = spawn(
    PYTHON,
    ["-m", "nda_automation.server", "--host", "127.0.0.1", "--port", String(PORT)],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        NDA_DATA_DIR: DATA_DIR,
        NDA_GMAIL_SYNC_ENABLED: "false",
        NDA_AI_REVIEW_ENABLED: "true",
        NDA_AI_ASSESSMENT_STUB: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  server.stdout.on("data", (d) => process.stdout.write(`[server] ${d}`));
  server.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));

  let browser;
  try {
    await waitForServer(`${BASE_URL}/`, 20000);
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const consoleErrors = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.click("#playbookTab");
    await page.waitForSelector("#playbookList .playbook-row");

    // ===================================================================
    // 1. mutuality (NATIVE): trigger-term chip editor + condition editor LIVE
    // ===================================================================
    await selectClause(page, "mutuality");

    // The consolidated editor carries an EDITABLE trigger-term chip editor for the
    // native clause inline (these were previously read-only display chips only).
    // There are no sub-tabs -- everything is on one scrolling screen.
    await page.waitForSelector("#dynamicSearchTermInput", { state: "visible" });
    await page.waitForSelector("#dynamicSemanticSignalInput", { state: "visible" });

    await page.fill("#dynamicSearchTermInput", MUT_SEARCH_TERM);
    await page.click("#addDynamicSearchTerm");
    await page.waitForFunction(
      (t) =>
        [...document.querySelectorAll('[data-chip-row="search-term"] .admin-chip')].some(
          (el) => el.textContent.includes(t)
        ),
      MUT_SEARCH_TERM
    );
    await page.fill("#dynamicSemanticSignalInput", MUT_SEMANTIC_SIGNAL);
    await page.click("#addDynamicSemanticSignal");
    await page.waitForFunction(
      (t) =>
        [...document.querySelectorAll('[data-chip-row="semantic-signal"] .admin-chip')].some(
          (el) => el.textContent.includes(t)
        ),
      MUT_SEMANTIC_SIGNAL
    );

    // The structured condition editor is LIVE for the native clause and visible
    // inline (no decision sub-tab anymore).
    const failDesc =
      '[data-condition-field="fail_conditions"][data-condition-index="0"] [data-condition-description]';
    await page.waitForSelector(failDesc, { state: "visible" });
    await page.fill(failDesc, MUT_FAIL_DESC);

    await page.screenshot({
      path: path.join(SHOTS_DIR, "01-mutuality-edited.png"),
      fullPage: true,
    });

    // Validate -> valid.
    assert.equal(
      await validateState(page),
      "valid",
      "mutuality with an added trigger term + edited condition must validate"
    );

    // Save Draft (publish requires a saved, non-dirty draft).
    const saveResp = page.waitForResponse(
      (r) => r.url().includes("/api/playbook/draft") && r.request().method() === "POST"
    );
    await page.click("#savePlaybookButton");
    assert.equal((await saveResp).status(), 200, "save draft should return 200");
    await page.waitForFunction(
      () => document.querySelector("#playbookSaveStatus")?.textContent.includes("Draft saved"),
      { timeout: 15000 }
    );

    // Publish -> 200.
    const publishResp = page.waitForResponse(
      (r) => r.url().includes("/api/playbook/publish") && r.request().method() === "POST"
    );
    await page.click("#publishPlaybookButton");
    assert.equal((await publishResp).status(), 200, "publish should return 200");
    await page.waitForFunction(
      () => document.querySelector("#playbookSaveStatus")?.textContent.includes("Playbook published"),
      { timeout: 15000 }
    );

    // ===================================================================
    // 2. PROPAGATION: HTTP read-back + engine-consumer probe
    // ===================================================================
    const live = await (await fetch(`${BASE_URL}/api/playbook`)).json();
    const playbook = live.playbook || live.active?.playbook || live;
    const mut = (playbook.clauses || []).find((c) => c.id === "mutuality");
    assert.ok(mut, "published playbook must contain mutuality");
    assert.ok(
      (mut.search_terms || []).includes(MUT_SEARCH_TERM),
      "edited search term must persist in the published playbook"
    );
    assert.ok(
      (mut.semantic_signals || []).includes(MUT_SEMANTIC_SIGNAL),
      "edited semantic signal must persist in the published playbook"
    );
    const failDescs = ((mut.rules || {}).fail_conditions || []).map((c) => String(c.description || ""));
    assert.ok(
      failDescs.includes(MUT_FAIL_DESC),
      "edited fail condition description must persist in the published playbook"
    );

    // The decisive end-to-end propagation: the published playbook, read through the
    // real engine consumers, carries the edits into BOTH the deterministic checker
    // and the AI packet.
    const probe = probePublishedPlaybook();
    assert.ok(
      probe.det_search_terms.includes(MUT_SEARCH_TERM),
      `deterministic mutuality checker must read the new search term; got ${JSON.stringify(probe.det_search_terms)}`
    );
    assert.ok(
      probe.packet_semantic_signals.includes(MUT_SEMANTIC_SIGNAL),
      `AI packet must carry the new semantic signal; got ${JSON.stringify(probe.packet_semantic_signals)}`
    );
    assert.ok(
      probe.packet_fail_descriptions.includes(MUT_FAIL_DESC),
      `AI packet must carry the edited fail condition; got ${JSON.stringify(probe.packet_fail_descriptions)}`
    );

    // ===================================================================
    // 3. DERIVED clauses: read-only standard box + the REAL lever editable
    // ===================================================================
    // governing_law: preferred-position is derived (read-only); jurisdiction list editable.
    await selectClause(page, "governing_law");
    await page.waitForSelector('[data-derived-standard="1"]', { state: "visible" });
    // The derived standard box exposes NO editable preferred_position/check_trigger field.
    const govEditableStandard = await page.$$eval(
      '#playbookEditor [name="preferred_position"], #playbookEditor [name="check_trigger"]',
      (els) => els.filter((el) => !el.disabled).length
    );
    assert.equal(govEditableStandard, 0, "governing_law must not expose an editable (inert) standard box");
    // The REAL live lever IS editable: the approved-jurisdiction value inputs.
    const govLawInputs = await page.$$eval(
      "#playbookEditor [data-governing-law-value]",
      (els) => els.filter((el) => !el.disabled).length
    );
    assert.ok(govLawInputs >= 1, "governing_law jurisdiction list must be editable (the real lever)");

    // term_and_survival: derived standard read-only; max_term_years editable.
    await selectClause(page, "term_and_survival");
    await page.waitForSelector('[data-derived-standard="1"]', { state: "visible" });
    const termEditableStandard = await page.$$eval(
      '#playbookEditor [name="preferred_position"], #playbookEditor [name="check_trigger"]',
      (els) => els.filter((el) => !el.disabled).length
    );
    assert.equal(termEditableStandard, 0, "term_and_survival must not expose an editable (inert) standard box");
    const maxTermEditable = await page.$eval(
      '#playbookEditor [name="max_term_years"]',
      (el) => !el.disabled
    );
    assert.ok(maxTermEditable, "term_and_survival max_term_years must be editable (the real lever)");

    await page.screenshot({
      path: path.join(SHOTS_DIR, "02-derived-clauses.png"),
      fullPage: true,
    });

    // ===================================================================
    // 4. NEGATIVE: stripping every search term off a native clause => invalid
    // ===================================================================
    await selectClause(page, "confidential_information");
    await page.waitForSelector('[data-chip-row="search-term"] .admin-chip', { state: "visible" });
    let removeChips = await page.$$('[data-chip-row="search-term"] [data-remove-chip]');
    while (removeChips.length) {
      await removeChips[0].click();
      await page.waitForTimeout(50);
      removeChips = await page.$$('[data-chip-row="search-term"] [data-remove-chip]');
    }
    assert.equal(
      await validateState(page),
      "invalid",
      "a native clause with no search terms must be rejected by the publish gate"
    );
    const publishDisabled = await page.$eval("#publishPlaybookButton", (el) => el.disabled);
    assert.equal(publishDisabled, true, "publish must be blocked when a clause has no trigger terms");

    await page.screenshot({
      path: path.join(SHOTS_DIR, "03-no-terms-rejected.png"),
      fullPage: true,
    });

    assert.deepEqual(consoleErrors, [], `no console errors expected; got ${consoleErrors.join("; ")}`);

    console.log("PASS clause-parity browser proof");
    console.log(`screenshots in ${SHOTS_DIR}`);
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
