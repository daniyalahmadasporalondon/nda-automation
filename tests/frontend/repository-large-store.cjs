// Large-store frontend resilience proof: the SPA must stay interactive when a
// Gmail-import-storm account holds 1000+ matters.
//
// Seeds a REAL matter store (one JSON record per matter under
// NDA_DATA_DIR/matters/, the production on-disk layout) with 1200 matters, boots
// the real Python server, and asserts against the real SPA:
//
//   (a) INTERACTIVE: after the board has hydrated, a nav tab click responds in
//       under 2 seconds (the old unbounded render froze the main thread).
//   (b) BOUNDED BOARD: the Inbox column renders at most its initial page
//       (RepositoryBoard.INITIAL_CARDS_PER_COLUMN cards) plus a working
//       "Show more" affordance that reveals the next SHOW_MORE_STEP cards.
//   (c) FULL COUNTS: the column count chip shows the FULL column total, not the
//       rendered subset.
//   (d) FULL SEARCH: the board search operates on the full in-memory list -- a
//       needle matter far beyond the rendered subset is found and rendered.
//   (e) BOUNDED DASHBOARD: the intake table caps its rows and points at the
//       Repository for the rest, while the count label keeps the full total.
//
// Also MEASURES (reported, not asserted -- server-side pagination is a later fix):
// the /api/matters payload size and its JSON.parse cost at this store size.
//
// Run: node tests/frontend/repository-large-store.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const PORT = Number(process.env.FRONTEND_TEST_PORT || 19000 + Math.floor(Math.random() * 1000));
const BASE_URL = `http://127.0.0.1:${PORT}`;
const PYTHON = process.env.PYTHON || "python3";
const VIEWPORT = { width: 1440, height: 1000 };
const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "nda-automation-bigstore-"));

// Must mirror the bounds in static/js/repository-board.js / static/app.js.
const INITIAL_CARDS_PER_COLUMN = 30;
const SHOW_MORE_STEP = 50;
const DASHBOARD_INBOX_MAX_ROWS = 30;

const INBOX_MATTERS = 1150;
const GENERATED_MATTERS = 50;
const NEEDLE_SUBJECT = "Zebra Quantum Needle NDA";

function seedStore() {
  const recordsDir = path.join(DATA_DIR, "matters");
  fs.mkdirSync(recordsDir, { recursive: true });
  const base = Date.parse("2026-06-01T00:00:00Z");
  const write = (record) => {
    fs.writeFileSync(path.join(recordsDir, `${record.id}.json`), JSON.stringify(record));
  };
  for (let i = 0; i < INBOX_MATTERS; i += 1) {
    // Newest-first recency: record 0 is the newest. The needle is the very LAST
    // (oldest) inbox matter so it can never sit inside the bounded first page.
    const isNeedle = i === INBOX_MATTERS - 1;
    const when = new Date(base - i * 60_000).toISOString();
    write({
      id: `matter_seedinbox${String(i).padStart(5, "0")}`,
      created_at: when,
      updated_at: when,
      received_at: when,
      source_type: "gmail_inbound",
      source_filename: `inbound-nda-${i}.docx`,
      attachment_filename: `inbound-nda-${i}.docx`,
      document_title: isNeedle ? NEEDLE_SUBJECT : `Inbound NDA ${i}`,
      subject: isNeedle ? NEEDLE_SUBJECT : `NDA request ${i} — Acme Corp ${i}`,
      sender: `counterparty${i}@example.com`,
      message_snippet: "Please review the attached NDA.",
      status: "active",
      board_column: "gmail_demo",
      extracted_text: "",
      review_result: {},
    });
  }
  for (let i = 0; i < GENERATED_MATTERS; i += 1) {
    const when = new Date(base - i * 60_000).toISOString();
    write({
      id: `matter_seedgen${String(i).padStart(5, "0")}`,
      created_at: when,
      updated_at: when,
      source_type: "generated",
      source_filename: `generated-nda-${i}.docx`,
      document_title: `Generated NDA ${i}`,
      subject: `Generated NDA ${i}`,
      status: "active",
      board_column: "generated",
      extracted_text: "",
      review_result: {},
    });
  }
}

function startServer() {
  const server = spawn(PYTHON, ["-m", "nda_automation.server", "--port", String(PORT)], {
    cwd: ROOT,
    env: {
      ...process.env,
      NDA_DATA_DIR: DATA_DIR,
      NDA_EXPORTS_DIR: path.join(ROOT, "exports"),
      PYTHONUNBUFFERED: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stderr.on("data", (chunk) => process.stderr.write(`[server] ${chunk}`));
  server.on("exit", (code, signal) => {
    if (server.expectedStop) return;
    if (code !== null && code !== 0) console.error(`test server exited with code ${code}`);
    else if (signal) console.error(`test server exited with signal ${signal}`);
  });
  return server;
}

async function stopServer(server) {
  if (!server || server.killed) return;
  server.expectedStop = true;
  server.kill();
  await new Promise((resolve) => {
    const timeout = setTimeout(resolve, 2500);
    server.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
  fs.rmSync(DATA_DIR, { force: true, recursive: true });
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForServer() {
  const startedAt = Date.now();
  while (Date.now() - startedAt < 20000) {
    const ok = await new Promise((resolve) => {
      const request = http.get(`${BASE_URL}/`, (response) => {
        response.resume();
        resolve(response.statusCode === 200);
      });
      request.on("error", () => resolve(false));
      request.setTimeout(500, () => {
        request.destroy();
        resolve(false);
      });
    });
    if (ok) return;
    await wait(150);
  }
  throw new Error(`Server did not start at ${BASE_URL}`);
}

function browserLaunchOptions() {
  const options = { headless: true };
  const configuredExecutable = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (configuredExecutable) options.executablePath = configuredExecutable;
  else if (process.platform === "darwin" && fs.existsSync(macChrome)) options.executablePath = macChrome;
  return options;
}

// Node-side measurement of the /api/matters payload: raw bytes on the wire and
// the JSON.parse cost of the full body. Reported, not asserted -- this is the
// data point for the later server-side pagination fix.
function measureMattersPayload() {
  return new Promise((resolve, reject) => {
    const startedAt = process.hrtime.bigint();
    http.get(`${BASE_URL}/api/matters`, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        const fetchedAt = process.hrtime.bigint();
        const body = Buffer.concat(chunks);
        const parseStart = process.hrtime.bigint();
        let payload;
        try {
          payload = JSON.parse(body.toString("utf8"));
        } catch (error) {
          reject(error);
          return;
        }
        const parseEnd = process.hrtime.bigint();
        resolve({
          bytes: body.length,
          fetchMs: Number(fetchedAt - startedAt) / 1e6,
          parseMs: Number(parseEnd - parseStart) / 1e6,
          matterCount: Array.isArray(payload.matters) ? payload.matters.length : 0,
        });
      });
    }).on("error", reject);
  });
}

async function main() {
  seedStore();
  const server = startServer();
  let browser;
  const failures = [];
  const check = async (label, fn) => {
    try {
      await fn();
      console.log(`ok - ${label}`);
    } catch (error) {
      failures.push(label);
      console.error(`FAIL - ${label}\n   ${error.message}`);
    }
  };

  try {
    await waitForServer();

    const payload = await measureMattersPayload();
    console.log(
      `# /api/matters at ${payload.matterCount} matters: `
      + `${(payload.bytes / 1024 / 1024).toFixed(2)} MB, `
      + `fetch ${payload.fetchMs.toFixed(0)} ms, JSON.parse ${payload.parseMs.toFixed(1)} ms`,
    );
    assert.equal(payload.matterCount, INBOX_MATTERS + GENERATED_MATTERS, "seeded store did not serve every matter");

    browser = await chromium.launch(browserLaunchOptions());
    const context = await browser.newContext({ viewport: VIEWPORT });
    const page = await context.newPage();

    const loadStartedAt = Date.now();
    await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
    // Board hydration done = the Inbox count chip carries the full total.
    await page.waitForFunction(
      (expected) => document.querySelector('[data-repository-count="gmail_demo"]')?.textContent === String(expected),
      INBOX_MATTERS,
      { timeout: 30000 },
    );
    console.log(`# load -> board hydrated in ${Date.now() - loadStartedAt} ms`);

    await check("dashboard intake table caps its rows and keeps the full count label", async () => {
      const rows = await page.locator("[data-dashboard-inbox-body] tr").count();
      assert.equal(
        rows,
        DASHBOARD_INBOX_MAX_ROWS + 1,
        `expected ${DASHBOARD_INBOX_MAX_ROWS} rows + 1 truncation row, got ${rows}`,
      );
      const truncation = await page.locator(".dashboard-inbox-more-row").textContent();
      assert.match(truncation, new RegExp(`Showing ${DASHBOARD_INBOX_MAX_ROWS} of ${INBOX_MATTERS}`));
      const countLabel = await page.locator("[data-dashboard-inbox-count]").textContent();
      assert.equal(countLabel.trim(), `${INBOX_MATTERS} documents`);
    });

    await check("nav tab click responds in under 2 seconds", async () => {
      const clickedAt = Date.now();
      await page.getByRole("tab", { name: "Repository" }).click();
      await page.waitForFunction(
        () => document.querySelector('[data-view="repository"]')?.classList.contains("active")
          && document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length > 0,
        undefined,
        { timeout: 2000 },
      );
      const elapsed = Date.now() - clickedAt;
      console.log(`# Repository tab click -> board visible in ${elapsed} ms`);
      assert.ok(elapsed < 2000, `tab activation took ${elapsed} ms`);
    });

    await check("Inbox column renders the bounded initial page with full-total count", async () => {
      // A background poll may re-render; assert on the settled state.
      await page.waitForFunction(
        (expected) => document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length === expected,
        INITIAL_CARDS_PER_COLUMN,
        { timeout: 5000 },
      );
      const countChip = await page.locator('[data-repository-count="gmail_demo"]').textContent();
      assert.equal(countChip, String(INBOX_MATTERS), "column count must reflect the FULL total");
      const showMore = page.locator('[data-repository-show-more="gmail_demo"]');
      assert.equal(await showMore.count(), 1, "Show more affordance missing");
      const label = (await showMore.textContent()).replace(/\s+/g, " ").trim();
      assert.match(
        label,
        new RegExp(`Show ${SHOW_MORE_STEP} more \\(${INBOX_MATTERS - INITIAL_CARDS_PER_COLUMN} hidden\\)`),
      );
    });

    await check("Show more reveals the next batch and keeps the affordance", async () => {
      await page.locator('[data-repository-show-more="gmail_demo"]').click();
      await page.waitForFunction(
        (expected) => document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length === expected,
        INITIAL_CARDS_PER_COLUMN + SHOW_MORE_STEP,
        { timeout: 5000 },
      );
      const showMore = page.locator('[data-repository-show-more="gmail_demo"]');
      assert.equal(await showMore.count(), 1, "Show more should remain while cards stay hidden");
      const label = (await showMore.textContent()).replace(/\s+/g, " ").trim();
      assert.match(
        label,
        new RegExp(`\\(${INBOX_MATTERS - INITIAL_CARDS_PER_COLUMN - SHOW_MORE_STEP} hidden\\)`),
      );
    });

    await check("search finds a matter far beyond the rendered subset", async () => {
      await page.fill("#repositorySearchInput", "zebra quantum");
      // The search render is debounced 300ms.
      await page.waitForFunction(
        (needle) => {
          const cards = document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card');
          return cards.length === 1 && cards[0].textContent.includes(needle);
        },
        NEEDLE_SUBJECT,
        { timeout: 5000 },
      );
      const countChip = await page.locator('[data-repository-count="gmail_demo"]').textContent();
      assert.equal(countChip, "1", "search count must reflect the full-list match total");
    });

    await check("clearing the search restores the bounded first page", async () => {
      await page.fill("#repositorySearchInput", "");
      await page.waitForFunction(
        (expected) => document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length === expected,
        INITIAL_CARDS_PER_COLUMN,
        { timeout: 5000 },
      );
      const countChip = await page.locator('[data-repository-count="gmail_demo"]').textContent();
      assert.equal(countChip, String(INBOX_MATTERS));
    });

    await check("a torn card list is repaired by the next render (skip audit)", async () => {
      // Simulate a stranded chunk continuation: surgically strip the column down
      // to a partial page with no Show-more control, then drive the same
      // renderBoard the 15s poll drives. The unchanged SIGNATURE must not freeze
      // the torn DOM in place -- the actual-DOM audit forces a repaint.
      await page.evaluate(() => {
        const list = document.querySelector('[data-repository-list="gmail_demo"]');
        while (list.childElementCount > 10) list.removeChild(list.lastElementChild);
        repositoryController.renderBoard();
      });
      await page.waitForFunction(
        (expected) => (
          document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length === expected
          && document.querySelectorAll('[data-repository-show-more="gmail_demo"]').length === 1
        ),
        INITIAL_CARDS_PER_COLUMN,
        { timeout: 5000 },
      );
    });

    await check("a hung matter-list fetch aborts, settles, and keeps the board un-wiped", async () => {
      // The poll fetches carry AbortSignal.timeout(45s) so a stalled-but-open
      // connection cannot wedge the 15s poll's in-flight guard forever. 45s is
      // too slow for a test, so clamp the page's AbortSignal.timeout to 1s, then
      // BLACK-HOLE /api/matters (never fulfilled) and drive the same loadMatters
      // the poll drives: it must SETTLE via the timeout (that settling is what
      // releases the guard) and must treat the abort as TRANSIENT -- the board
      // keeps its cards, no error dropzone.
      await page.evaluate(() => {
        window.__realAbortTimeout = AbortSignal.timeout.bind(AbortSignal);
        AbortSignal.timeout = (ms) => window.__realAbortTimeout(Math.min(ms, 1000));
      });
      await page.route("**/api/matters", () => { /* black-hole: never respond */ });
      try {
        const outcome = await page.evaluate(() => Promise.race([
          repositoryController.loadMatters().then(() => "settled", () => "settled-rejected"),
          new Promise((resolve) => setTimeout(() => resolve("wedged"), 6000)),
        ]));
        assert.equal(outcome, "settled", "loadMatters must settle via the fetch timeout, not hang");
        const board = await page.evaluate(() => ({
          cards: document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length,
          dropzones: document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-dropzone').length,
        }));
        assert.equal(board.cards, INITIAL_CARDS_PER_COLUMN, "board must survive a transient poll timeout un-wiped");
        assert.equal(board.dropzones, 0, "no error dropzone for a transient poll timeout");
      } finally {
        await page.unroute("**/api/matters");
        await page.evaluate(() => {
          AbortSignal.timeout = window.__realAbortTimeout;
          delete window.__realAbortTimeout;
        });
      }
    });

    await context.close();

    // ------------------------------------------------------------------
    // OCCLUDED-PAGE REGRESSION (the live-preview torn Show-more bug): with
    // requestAnimationFrame stalled -- exactly what browsers do to occluded /
    // backgrounded pages -- a Show-more click must STILL complete its chunked
    // render via the setTimeout backstop: the full new page of cards AND the
    // Show-more control with the updated hidden count. Playwright's own
    // waitForFunction polls via rAF by default, so every wait here pins
    // { polling: <ms> }.
    // ------------------------------------------------------------------
    const stalledContext = await browser.newContext({ viewport: VIEWPORT });
    const stalledPage = await stalledContext.newPage();
    await stalledPage.addInitScript(() => {
      window.requestAnimationFrame = () => 0;
    });

    await check("Show more completes under a stalled requestAnimationFrame (occluded page)", async () => {
      await stalledPage.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
      await stalledPage.waitForFunction(
        (expected) => document.querySelector('[data-repository-count="gmail_demo"]')?.textContent === String(expected),
        INBOX_MATTERS,
        { polling: 100, timeout: 30000 },
      );
      await stalledPage.getByRole("tab", { name: "Repository" }).click();
      await stalledPage.waitForFunction(
        (expected) => document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length === expected,
        INITIAL_CARDS_PER_COLUMN,
        { polling: 100, timeout: 5000 },
      );
      await stalledPage.locator('[data-repository-show-more="gmail_demo"]').click();
      await stalledPage.waitForFunction(
        (expected) => document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length === expected,
        INITIAL_CARDS_PER_COLUMN + SHOW_MORE_STEP,
        { polling: 100, timeout: 5000 },
      );
      // Settle past any further backstop timers, then assert the FINAL state:
      // exactly one full new page of cards and the Show-more control intact.
      await wait(600);
      const settled = await stalledPage.evaluate(() => ({
        cards: document.querySelectorAll('[data-repository-list="gmail_demo"] .repository-card').length,
        buttons: document.querySelectorAll('[data-repository-show-more="gmail_demo"]').length,
        label: (document.querySelector('[data-repository-show-more="gmail_demo"]')?.textContent || "").replace(/\s+/g, " ").trim(),
      }));
      assert.equal(settled.cards, INITIAL_CARDS_PER_COLUMN + SHOW_MORE_STEP, "card count after Show more under stalled rAF");
      assert.equal(settled.buttons, 1, "Show more control must survive the completed chunked render");
      assert.match(
        settled.label,
        new RegExp(`Show ${SHOW_MORE_STEP} more \\(${INBOX_MATTERS - INITIAL_CARDS_PER_COLUMN - SHOW_MORE_STEP} hidden\\)`),
        "Show more label must carry the updated hidden count",
      );
    });

    await stalledContext.close();
  } catch (error) {
    failures.push("harness");
    console.error(error);
  } finally {
    if (browser) await browser.close();
    await stopServer(server);
  }

  if (failures.length) {
    console.error(`repository-large-store.cjs FAIL (${failures.length}): ${failures.join(", ")}`);
    process.exit(1);
  }
  console.log("repository-large-store.cjs PASS");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
