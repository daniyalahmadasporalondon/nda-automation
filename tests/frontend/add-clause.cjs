// Browser proof for the Add-Clause feature (Playwright + a real spawned server).
//
// Proves end-to-end IN A REAL BROWSER against the live app:
//   1. Open the Playbook editor, click "Add Clause" -> a new DYNAMIC clause appears
//      and is selected.
//   2. Author it: name, requirement, acceptable language, a trigger term, and EDIT
//      a decision condition (the structured pass/fail/review logic the UI could not
//      author before).
//   3. Validate Draft -> the consistency lint PASSES (valid).
//   4. Publish -> 200; the published playbook (read back via /api/playbook) contains
//      the dynamic clause AND its rule is in the authoritative binding_policy block.
//   5. NEGATIVE: a contradictory clause (delete every fail + review condition) is
//      REJECTED by the publish gate in the browser.
//
// The server runs the key-free AI assessment stub (NDA_AI_ASSESSMENT_STUB=1),
// Gmail polling HARD-OFF, a throwaway data dir, on a free port (never 8787).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = process.env.PYTHON || "python3";
const PORT = Number(process.env.ADD_CLAUSE_PORT || 24000 + Math.floor(Math.random() * 1000));
const BASE_URL = `http://127.0.0.1:${PORT}`;
const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "add-clause-data-"));
const SHOTS_DIR = process.env.ADD_CLAUSE_SHOTS || DATA_DIR;

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

    // Open the Playbook editor.
    await page.click("#playbookTab");
    await page.waitForSelector("#playbookList .playbook-row");

    // --- 1. Add Clause -> new dynamic clause appears + selected -----------------
    const beforeRows = await page.$$eval("#playbookList .playbook-row", (r) => r.length);
    await page.click("#addPlaybookClause");
    await page.waitForSelector("#clauseDetail #playbookEditor");
    const afterRows = await page.$$eval("#playbookList .playbook-row", (r) => r.length);
    assert.equal(afterRows, beforeRows + 1, "Add Clause should append a row");
    // The new clause is dynamic (AI-reviewed badge present somewhere in the list).
    const dynamicBadges = await page.$$eval(
      "#playbookList .playbook-row-dynamic",
      (r) => r.length
    );
    assert.ok(dynamicBadges >= 1, "new clause should be flagged AI-reviewed (dynamic)");

    // The Decision-Logic panel must expose an EDITABLE condition editor (the thing
    // the UI previously could not author).
    await page.click('[data-playbook-panel-tab="decision"]');
    await page.waitForSelector('[data-dynamic-conditions] [data-condition-field="fail_conditions"]');

    // --- 2. Author the clause ---------------------------------------------------
    // Policy panel carries name + requirement + acceptable language + trigger terms.
    await page.click('[data-playbook-panel-tab="policy"]');
    await page.waitForSelector('#playbookEditor textarea[name="requirement"]', { state: "visible" });
    await page.fill('#playbookEditor input[name="name"]', "Exclusive Dealing");
    await page.fill(
      '#playbookEditor textarea[name="requirement"]',
      "The NDA must not require either party to deal exclusively with the other."
    );
    await page.fill(
      '#playbookEditor textarea[name="acceptable_language"]',
      "No exclusive-dealing obligation is imposed."
    );
    await page.fill("#dynamicSearchTermInput", "deal exclusively");
    await page.click("#addDynamicSearchTerm");
    await page.waitForSelector('[data-chip-row="search-term"] .admin-chip');

    // Decision panel: EDIT the fail condition's description (prove conditions are
    // authorable).
    await page.click('[data-playbook-panel-tab="decision"]');
    const failDesc = '[data-condition-field="fail_conditions"][data-condition-index="0"] [data-condition-description]';
    await page.fill(
      failDesc,
      "An exclusive-dealing obligation appears in operative form."
    );

    await page.screenshot({ path: path.join(SHOTS_DIR, "01-authored-clause.png"), fullPage: true });

    // --- 3. Validate Draft -> lint PASSES ---------------------------------------
    await page.click("#validatePlaybookButton");
    await page.waitForFunction(
      () => {
        const el = document.querySelector("#playbookValidation");
        return el && (el.dataset.state === "valid" || el.dataset.state === "invalid");
      },
      { timeout: 15000 }
    );
    const validateState = await page.$eval("#playbookValidation", (el) => el.dataset.state);
    assert.equal(validateState, "valid", "validate should pass for the authored clause");

    // Save Draft first (publish requires a saved, non-dirty draft).
    const saveResp = page.waitForResponse(
      (r) => r.url().includes("/api/playbook/draft") && r.request().method() === "POST"
    );
    await page.click("#savePlaybookButton");
    assert.equal((await saveResp).status(), 200, "save draft should return 200");
    await page.waitForFunction(
      () => document.querySelector("#playbookSaveStatus")?.textContent.includes("Draft saved"),
      { timeout: 15000 }
    );

    // --- 4. Publish -> 200 + clause is live with a binding_policy rule -----------
    const publishResp = page.waitForResponse(
      (r) => r.url().includes("/api/playbook/publish") && r.request().method() === "POST"
    );
    await page.click("#publishPlaybookButton");
    const published = await publishResp;
    assert.equal(published.status(), 200, "publish should return 200");
    await page.waitForFunction(
      () => document.querySelector("#playbookSaveStatus")?.textContent.includes("Playbook published"),
      { timeout: 15000 }
    );

    // Read the live playbook back and assert the dynamic clause is present.
    const live = await (await fetch(`${BASE_URL}/api/playbook`)).json();
    const playbook = live.playbook || live.active?.playbook || live;
    const clauses = playbook.clauses || [];
    const authored = clauses.find((c) => c.name === "Exclusive Dealing");
    assert.ok(authored, "published playbook must contain the authored clause");
    assert.equal(authored.engine, "dynamic", "authored clause must be dynamic");
    assert.ok(
      (authored.search_terms || []).includes("deal exclusively"),
      "authored search term must persist"
    );

    await page.screenshot({ path: path.join(SHOTS_DIR, "02-published.png"), fullPage: true });

    // --- 5. NEGATIVE: contradictory clause is REJECTED by the publish gate -------
    // Add a second clause, then strip every fail + review condition (only-ever-pass)
    // and confirm Validate flags it invalid (the gate would reject publish).
    await page.click("#addPlaybookClause");
    await page.waitForSelector("#clauseDetail #playbookEditor");
    await page.click('[data-playbook-panel-tab="decision"]');
    // Remove all fail + review conditions.
    let removeButtons = await page.$$(
      '[data-condition-field="fail_conditions"] [data-remove-condition], [data-condition-field="review_triggers"] [data-remove-condition]'
    );
    while (removeButtons.length) {
      await removeButtons[0].click();
      await page.waitForTimeout(50);
      removeButtons = await page.$$(
        '[data-condition-field="fail_conditions"] [data-remove-condition], [data-condition-field="review_triggers"] [data-remove-condition]'
      );
    }
    await page.click("#validatePlaybookButton");
    await page.waitForFunction(
      () => {
        const el = document.querySelector("#playbookValidation");
        return el && (el.dataset.state === "valid" || el.dataset.state === "invalid");
      },
      { timeout: 15000 }
    );
    const badValidateState = await page.$eval("#playbookValidation", (el) => el.dataset.state);
    assert.equal(badValidateState, "invalid", "contradictory clause must be invalid");
    // The publish button must be disabled (the gate blocks publishing it).
    const publishDisabled = await page.$eval("#publishPlaybookButton", (el) => el.disabled);
    assert.equal(publishDisabled, true, "publish must be blocked for a contradictory clause");

    await page.screenshot({ path: path.join(SHOTS_DIR, "03-rejected.png"), fullPage: true });

    assert.deepEqual(consoleErrors, [], `no console errors expected; got ${consoleErrors.join("; ")}`);

    console.log("PASS add-clause browser proof");
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
