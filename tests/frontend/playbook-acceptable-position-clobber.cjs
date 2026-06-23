// Regression proof for the native-clause rules.acceptable_position clobber bug.
//
// THE BUG (fixed in static/js/playbook-view.js syncStructuredRules): editing the
// "Preferred Standard Position" (preferred_position) of a NATIVE clause used to
// UNCONDITIONALLY mirror that text onto rules.acceptable_position
// (`clause.rules.acceptable_position = clause.preferred_position;`), silently
// clobbering the separately-authored rules.acceptable_position -- a field with no
// editable surface that the AI reviewer judges against. That corruption survived
// all backend guards and degraded live AI review on publish.
//
// This test drives a REAL browser against the live app:
//   1. Read the published mutuality clause; capture its ORIGINAL distinct
//      rules.acceptable_position (authored separately from preferred_position).
//   2. In the editor, type a NEW distinctive preferred_position and save + publish.
//   3. Assert the published mutuality.rules.acceptable_position STILL equals the
//      original authored value -- i.e. it was NOT clobbered to the new
//      preferred_position text.
//
// The dynamic-clause mirroring branch (kept intentionally) is out of scope here.
//
// Key-free AI stub, Gmail HARD-OFF, throwaway data dir, random free port (never 8787).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = process.env.PYTHON || "python3";
const PORT = Number(process.env.ACCEPTABLE_POS_PORT || 26000 + Math.floor(Math.random() * 1000));
const BASE_URL = `http://127.0.0.1:${PORT}`;
const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "acceptable-pos-data-"));

// A distinctive new preferred_position. If the bug were present, this exact text
// would leak into rules.acceptable_position.
const NEW_PREFERRED = "zzbespoke preferred standard position marker (must not clobber acceptable_position)";

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

async function fetchMutuality() {
  const live = await (await fetch(`${BASE_URL}/api/playbook`)).json();
  const playbook = live.playbook || live.active?.playbook || live;
  const mut = (playbook.clauses || []).find((c) => c.id === "mutuality");
  assert.ok(mut, "playbook must contain mutuality");
  return mut;
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

    // ----- baseline: the ORIGINAL, separately-authored acceptable_position -----
    const before = await fetchMutuality();
    const originalAcceptable = String((before.rules || {}).acceptable_position || "");
    const originalPreferred = String(before.preferred_position || "");
    assert.ok(
      originalAcceptable.length > 0,
      "mutuality must seed a non-empty rules.acceptable_position for this regression to be meaningful"
    );
    assert.notEqual(
      originalAcceptable,
      originalPreferred,
      "premise: mutuality's authored rules.acceptable_position must differ from preferred_position"
    );
    assert.notEqual(
      originalAcceptable,
      NEW_PREFERRED,
      "premise: the original acceptable_position must differ from the new preferred text we type"
    );

    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const consoleErrors = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.click("#playbookTab");
    await page.waitForSelector("#playbookList .playbook-row");

    // ----- edit preferred_position on the native mutuality clause -----
    await selectClause(page, "mutuality");
    const prefField = '#playbookEditor [name="preferred_position"]';
    await page.waitForSelector(prefField, { state: "visible" });
    await page.fill(prefField, NEW_PREFERRED);
    // Make sure the input handler ran the serialization (syncStructuredRules).
    await page.dispatchEvent(prefField, "input");

    // Save Draft.
    const saveResp = page.waitForResponse(
      (r) => r.url().includes("/api/playbook/draft") && r.request().method() === "POST"
    );
    await page.click("#savePlaybookButton");
    assert.equal((await saveResp).status(), 200, "save draft should return 200");
    await page.waitForFunction(
      () => document.querySelector("#playbookSaveStatus")?.textContent.includes("Draft saved"),
      { timeout: 15000 }
    );

    // Publish.
    const publishResp = page.waitForResponse(
      (r) => r.url().includes("/api/playbook/publish") && r.request().method() === "POST"
    );
    await page.click("#publishPlaybookButton");
    assert.equal((await publishResp).status(), 200, "publish should return 200");
    await page.waitForFunction(
      () => document.querySelector("#playbookSaveStatus")?.textContent.includes("Playbook published"),
      { timeout: 15000 }
    );

    // ----- THE REGRESSION ASSERTION -----
    const after = await fetchMutuality();
    const afterAcceptable = String((after.rules || {}).acceptable_position || "");
    const afterPreferred = String(after.preferred_position || "");

    // The edit must have taken effect on preferred_position...
    assert.equal(
      afterPreferred,
      NEW_PREFERRED,
      "the preferred_position edit must persist to the published playbook"
    );
    // ...but rules.acceptable_position must SURVIVE unchanged (the bug clobbered it).
    assert.notEqual(
      afterAcceptable,
      NEW_PREFERRED,
      `REGRESSION: editing preferred_position clobbered rules.acceptable_position to the preferred text. ` +
        `acceptable_position=${JSON.stringify(afterAcceptable)}`
    );
    assert.equal(
      afterAcceptable,
      originalAcceptable,
      `rules.acceptable_position must retain its original authored value; ` +
        `expected ${JSON.stringify(originalAcceptable)} got ${JSON.stringify(afterAcceptable)}`
    );

    assert.deepEqual(consoleErrors, [], `no console errors expected; got ${consoleErrors.join("; ")}`);

    console.log("PASS playbook-acceptable-position-clobber: rules.acceptable_position survived a preferred_position edit");
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
