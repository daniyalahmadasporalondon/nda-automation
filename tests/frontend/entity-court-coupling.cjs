// Browser proof for the Entities & Courts round-trip fixes (BUG A + BUG B).
//
// Proves end-to-end IN A REAL BROWSER against the live app, in the Playbook
// editor's governing_law clause -> "Entities & Courts" table:
//
//   BUG A (law<->court coupling): changing an entity's governing law to a DIFFERENT
//     jurisdiction re-suggests the matching court (so a lone law change no longer
//     trips the backend forum-reconciliation guard / HTTP 400 "forum drift") AND
//     surfaces an inline note so nothing happens silently. The subsequent Save then
//     succeeds (200) instead of 400.
//
//   BUG B (optimistic concurrency): the Save POST body carries the `etag` the editor
//     received on load, so the server can reject a stale write (409) rather than
//     clobbering a concurrent editor.
//
// The server runs on a loopback host (admin-trusted), Gmail HARD-OFF, throwaway
// data dir, on a free port (never 8787).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const PYTHON = process.env.PYTHON || "python3";
const PORT = Number(process.env.ENTITY_COURT_PORT || 26000 + Math.floor(Math.random() * 1000));
const BASE_URL = `http://127.0.0.1:${PORT}`;
const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "entity-court-data-"));
const SHOTS_DIR = process.env.ENTITY_COURT_SHOTS || DATA_DIR;

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
        NDA_AI_REVIEW_ENABLED: "false",
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

    // Open the Playbook editor and select the governing_law clause (which hosts the
    // Entities & Courts table).
    await page.click("#playbookTab");
    await page.waitForSelector("#playbookList .playbook-row");
    await page.click('[data-clause-id="governing_law"]');

    // The Entities & Courts table renders its rows after fetching the registry.
    await page.waitForSelector('[data-entity-courts] [data-entity-court-law="0"]');

    // Pick a row whose law we will flip to a DIFFERENT jurisdiction. We choose the
    // first India-law entity (its court is an India city) and flip it to Delaware.
    const targetIndex = await page.$$eval(
      "[data-entity-court-law]",
      (selects) => {
        for (const sel of selects) {
          if (sel.value === "india") {
            return Number(sel.getAttribute("data-entity-court-law"));
          }
        }
        return -1;
      }
    );
    assert.ok(targetIndex >= 0, "expected an India-law signing entity in the table");

    const lawSel = `[data-entity-court-law="${targetIndex}"]`;
    const courtSel = `[data-entity-court-jurisdiction="${targetIndex}"]`;
    const noteSel = `[data-entity-court-note="${targetIndex}"]`;

    const courtBefore = await page.$eval(courtSel, (el) => el.value);
    assert.ok(/india|bengaluru|gujarat|gandhinagar|mumbai/i.test(courtBefore),
      `expected an India court before the change; got ${courtBefore}`);

    // --- BUG A: flip the law to Delaware. The court must auto-update to a Delaware
    //     court AND an inline note must appear. ------------------------------------
    await page.selectOption(lawSel, "delaware");

    const courtAfter = await page.$eval(courtSel, (el) => el.value);
    assert.notEqual(courtAfter, courtBefore, "the court must change when the law jurisdiction changes");
    assert.ok(/delaware/i.test(courtAfter),
      `the suggested court must match the Delaware law; got ${courtAfter}`);
    const noteShown = await page.$eval(noteSel, (el) => !el.hidden && (el.textContent || "").trim().length > 0);
    assert.ok(noteShown, "an inline note must explain the court was updated to match the new law");

    await page.screenshot({ path: path.join(SHOTS_DIR, "01-law-court-coupled.png"), fullPage: true });

    // --- BUG A + B: Save. The POST must carry an `etag`, and the save must SUCCEED
    //     (200) because the coupled court reconciles with the new law (no 400). -----
    const saveReq = page.waitForRequest(
      (r) => r.url().includes("/api/admin/signing-entities") && r.method() === "POST"
    );
    const saveResp = page.waitForResponse(
      (r) => r.url().includes("/api/admin/signing-entities") && r.request().method() === "POST"
    );
    await page.click("[data-entity-courts-save]");
    const req = await saveReq;
    const body = JSON.parse(req.postData() || "{}");
    assert.ok(Array.isArray(body.entities), "save payload must be {entities:[...]}");
    // BUG B: the optimistic-concurrency token must be echoed on save.
    assert.ok(typeof body.etag === "string" && body.etag.length > 0,
      "the save POST must carry a non-empty etag (optimistic concurrency)");

    const resp = await saveResp;
    assert.equal(resp.status(), 200,
      "a law-only change with the coupled court must SAVE (200), not 400 forum drift");

    await page.screenshot({ path: path.join(SHOTS_DIR, "02-saved.png"), fullPage: true });

    assert.deepEqual(consoleErrors, [], `no console errors expected; got ${consoleErrors.join("; ")}`);

    console.log("PASS entity-court-coupling browser proof");
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
