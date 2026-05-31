const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const { chromium } = require("playwright");
const { PNG } = require("pngjs");

const ROOT = path.resolve(__dirname, "../..");
const PORT = Number(process.env.FRONTEND_TEST_PORT || 19000 + Math.floor(Math.random() * 1000));
const BASE_URL = `http://127.0.0.1:${PORT}`;
const PYTHON = process.env.PYTHON || "python3";
const VIEWPORT = { width: 1440, height: 1000 };
const TEST_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "nda-automation-data-"));

const passNda = fs.readFileSync(path.join(ROOT, "samples", "pass-nda.txt"), "utf8").trim();
const redlineNda = [
  "The confidentiality obligations survive for seven years.",
  "The Recipient must not circumvent the Company or deal directly with introduced parties.",
].join("\n\n");
const multiAnchorNda = [
  "The Recipient must not circumvent the Company.",
  "The Recipient shall not deal directly with introduced parties.",
].join("\n\n");
const allActionRedlineNda = [
  "The confidentiality obligations survive for seven years.",
  "The Recipient must not circumvent the Company or deal directly with introduced parties.",
  "For Aspora Technology Services Private Limited\nBy: __________________\nTitle: Director\nDate: 2026-05-30",
  "For Counterparty Limited\nBy: __________________\nTitle: Chief Executive Officer\nDate: 2026-05-30",
].join("\n\n");

const tests = [
  ["exposes accessible tab, toggle, and live-region state", testAccessibleControlState],
  ["surfaces review and export error details", testFailureUxDetails],
  ["guards Save-As picker fallbacks", testSavePickerGuardsAndFallbacks],
  ["renders server-provided inline diff operations", testInlineDiffOperationRendering],
  ["renders backend redlines across all document modes", testBackendRedlineModes],
  ["imports repository matters and re-reviews as fresh text", testRepositoryMatterImportAndFreshReview],
  ["cycles clause-to-paragraph anchors", testClauseAnchorCycling],
  ["exports selected clause decisions and template options", testClauseDecisionControls],
  ["renders manual viewer edits as local redlines", testManualViewerEditRedline],
  ["keeps browser preview aligned with exported DOCX redlines", testPreviewMatchesExportedDocx],
  ["guards source-redline export regression", testSourceRedlineExportRegression],
  ["exports reviewed DOCX and blocks stale edited exports", testExportFlow],
];

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

async function main() {
  const server = startServer();
  let browser;
  try {
    await waitForServer();
    browser = await chromium.launch(browserLaunchOptions());

    for (const [name, test] of tests) {
      const context = await browser.newContext({ acceptDownloads: true, viewport: VIEWPORT });
      const page = await context.newPage();
      try {
        await test(page);
        console.log(`ok - ${name}`);
      } finally {
        await context.close();
      }
    }
  } finally {
    if (browser) await browser.close();
    await stopServer(server);
  }
}

function startServer() {
  const server = spawn(PYTHON, ["-m", "nda_automation.server", "--port", String(PORT)], {
    cwd: ROOT,
    env: {
      ...process.env,
      NDA_DATA_DIR: TEST_DATA_DIR,
      NDA_EXPORTS_DIR: path.join(ROOT, "exports"),
      PYTHONUNBUFFERED: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (chunk) => process.stdout.write(`[server] ${chunk}`));
  server.stderr.on("data", (chunk) => process.stderr.write(`[server] ${chunk}`));
  server.on("exit", (code, signal) => {
    if (server.expectedStop) return;
    if (code !== null && code !== 0) {
      console.error(`frontend test server exited with code ${code}`);
    } else if (signal) {
      console.error(`frontend test server exited with signal ${signal}`);
    }
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
  fs.rmSync(TEST_DATA_DIR, { force: true, recursive: true });
}

async function waitForServer() {
  const startedAt = Date.now();
  while (Date.now() - startedAt < 10000) {
    if (await healthCheck()) return;
    await wait(120);
  }
  throw new Error(`Server did not start at ${BASE_URL}`);
}

function healthCheck() {
  return new Promise((resolve) => {
    const request = http.get(`${BASE_URL}/api/health`, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on("error", () => resolve(false));
    request.setTimeout(500, () => {
      request.destroy();
      resolve(false);
    });
  });
}

function browserLaunchOptions() {
  const options = { headless: true };
  const configuredExecutable = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (configuredExecutable) {
    options.executablePath = configuredExecutable;
  } else if (process.platform === "darwin" && fs.existsSync(macChrome)) {
    options.executablePath = macChrome;
  }
  return options;
}

async function runReview(page, text) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByPlaceholder("Paste NDA text here").fill(text);
  await page.getByRole("button", { name: "Review NDA" }).click();
  await page.waitForSelector("#studioDocumentRender:not([hidden])");
  await page.waitForSelector(".studio-clause-item.pass, .studio-clause-item.check");
}

async function testAccessibleControlState(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  assert.equal(await page.locator("#studioResultMeta").getAttribute("aria-live"), "polite");
  assert.equal(await page.locator("#studioFileMeta").getAttribute("aria-live"), "polite");
  assert.equal(await page.locator("#reviewTab").getAttribute("role"), "tab");
  assert.equal(await page.locator("#clausesTab").getAttribute("role"), "tab");
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#clausesTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#clausesView").getAttribute("hidden"), "");
  const matterCardStyles = await page.locator(".studio-matter-card").evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      borderRadius: styles.borderRadius,
      boxShadow: styles.boxShadow,
    };
  });
  assert.equal(matterCardStyles.borderRadius, "22px");
  assert.match(matterCardStyles.boxShadow, /26, 19, 51/);
  assert.equal(await page.locator(".studio-check-card").count(), 0);
  assert.equal(await page.locator(".studio-playbook > h2").innerText(), "SELECTED CLAUSE");
  assert.equal(await page.locator("#studioMatchSummary").innerText(), "0/6");

  await page.getByRole("tab", { name: "Clauses" }).click();
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#clausesTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#reviewView").getAttribute("hidden"), "");
  const activePlaybookRow = await page.locator(".playbook-row.active").first().evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      borderLeftColor: styles.borderLeftColor,
      borderLeftWidth: styles.borderLeftWidth,
    };
  });
  assert.equal(activePlaybookRow.backgroundColor, "rgb(250, 248, 255)");
  assert.equal(activePlaybookRow.borderLeftColor, "rgb(79, 27, 179)");
  assert.equal(activePlaybookRow.borderLeftWidth, "3px");

  await page.getByRole("tab", { name: "Review" }).click();
  await page.getByRole("button", { name: "Clean" }).click();
  assert.equal(await page.locator('[data-view-mode="redline"]').getAttribute("aria-pressed"), "false");
  assert.equal(await page.locator('[data-view-mode="clean"]').getAttribute("aria-pressed"), "true");
}

async function testFailureUxDetails(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.route("**/api/review", async (route) => {
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        error: "Clause evidence provenance drift.",
        details: ["governing_law: matched_text does not equal matched source paragraphs."],
      }),
    });
  });
  await page.getByPlaceholder("Paste NDA text here").fill("This Agreement shall be governed by the laws of California.");
  await page.getByRole("button", { name: "Review NDA" }).click();
  await waitForText(page, "#studioOverallTitle", "Clause evidence provenance drift.");
  await assertTextContains(page.locator("#studioOverallTitle"), "Clause evidence provenance drift.");
  await assertTextContains(page.locator("#studioResultMeta"), "Review could not run.");
  await assertTextContains(page.locator("#studioResultMeta"), "governing_law: matched_text");
  await page.unroute("**/api/review");

  await runReview(page, passNda);
  await page.route("**/api/export-review-docx", async (route) => {
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        error: "The exported Word document failed its open-health check.",
        details: ["Missing DOCX parts: _rels/.rels."],
      }),
    });
  });
  await page.getByRole("button", { name: "Export DOCX" }).click();
  await waitForText(page, "#studioOverallTitle", "The exported Word document failed its open-health check.");
  await assertTextContains(page.locator("#studioOverallTitle"), "The exported Word document failed its open-health check.");
  await assertTextContains(page.locator("#studioResultMeta"), "Export could not run.");
  await assertTextContains(page.locator("#studioResultMeta"), "Missing DOCX parts: _rels/.rels.");
  await page.unroute("**/api/export-review-docx");
}

async function testRepositoryMatterImportAndFreshReview(page) {
  const docxPath = path.join(os.tmpdir(), `repository-matter-${Date.now()}.docx`);
  makeDocxFixture(docxPath, [
    "This Agreement shall be governed by the laws of California.",
    "The Recipient must not circumvent the Company.",
  ]);

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.locator("#repositoryFileInput").setInputFiles(docxPath);
  await waitForText(page, "#repositoryImportStatus", "repository-matter-");
  await page.waitForSelector(".repository-card");
  assert.equal(await page.locator('[data-repository-count="gmail_demo"]').innerText(), "1");

  await page.locator(".repository-card").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "GMAIL DEMO");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "KEY FAILED CLAUSES");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Non-Circumvention");

  const [matterExportRequest, matterDownload] = await Promise.all([
    page.waitForRequest((request) => request.url().endsWith("/api/export-review-docx")),
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export Redline" }).click(),
  ]);
  const matterExportPayload = matterExportRequest.postDataJSON();
  assert.ok(matterExportPayload.matter_id, "Repository panel export should send a matter id");
  assert.match(matterDownload.suggestedFilename(), /^repository-matter-\d+-redlined\.docx$/);

  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "true");
  await assertTextContains(page.locator("#studioDocTitle"), "repository-matter-");
  await assertTextContains(page.locator("#studioFileMeta"), "Gmail Demo matter loaded");

  await page.getByRole("button", { name: "Review NDA" }).click();
  await waitForText(page, "#studioFileMeta", "Repository text reviewed as a fresh draft");

  const [exportRequest, download] = await Promise.all([
    page.waitForRequest((request) => request.url().endsWith("/api/export-review-docx")),
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export DOCX" }).click(),
  ]);
  const exportPayload = exportRequest.postDataJSON();
  assert.equal(download.suggestedFilename(), "nda-review-report.docx");
  assert.equal(Object.prototype.hasOwnProperty.call(exportPayload, "matter_id"), false);

  fs.rmSync(docxPath, { force: true });
}

async function testSavePickerGuardsAndFallbacks(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  const cases = await page.evaluate(async () => {
    let callCount = 0;
    const handle = { createWritable: async () => ({ write: async () => {}, close: async () => {} }) };
    window.showSaveFilePicker = async () => {
      callCount += 1;
      return handle;
    };

    const webdriverFallback = await chooseExportSaveHandle("fallback.docx");
    const pickedHandle = await chooseExportSaveHandle("picked.docx", { allowAutomation: true });

    window.showSaveFilePicker = async () => {
      const error = new Error("cancelled");
      error.name = "AbortError";
      throw error;
    };
    const cancelled = await chooseExportSaveHandle("cancelled.docx", { allowAutomation: true });

    window.showSaveFilePicker = async () => {
      throw new Error("not available");
    };
    const failedFallback = await chooseExportSaveHandle("failed.docx", { allowAutomation: true });

    return {
      callCount,
      webdriverFallbackType: typeof webdriverFallback,
      picked: pickedHandle === handle,
      cancelled,
      failedFallbackType: typeof failedFallback,
    };
  });

  assert.equal(cases.callCount, 1);
  assert.equal(cases.webdriverFallbackType, "undefined");
  assert.equal(cases.picked, true);
  assert.equal(cases.cancelled, null);
  assert.equal(cases.failedFallbackType, "undefined");
}

async function testInlineDiffOperationRendering(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  const cases = await page.evaluate(() => {
    const revisionState = (html) => {
      const container = document.createElement("div");
      container.innerHTML = html;
      const original = container.cloneNode(true);
      const accepted = container.cloneNode(true);
      original.querySelectorAll(".inline-ins").forEach((node) => node.remove());
      accepted.querySelectorAll(".inline-del").forEach((node) => node.remove());
      return {
        original: original.textContent,
        accepted: accepted.textContent,
        deleted: Array.from(container.querySelectorAll(".inline-del")).map((node) => node.textContent),
        inserted: Array.from(container.querySelectorAll(".inline-ins")).map((node) => node.textContent),
      };
    };
    return {
      emptyInsert: revisionState(renderDiffOperations([
        { type: "insert", token: "Alpha" },
        { type: "insert", token: "," },
        { type: "insert", token: "beta" },
        { type: "insert", token: "." },
      ])),
      emptyDelete: revisionState(renderDiffOperations([
        { type: "delete", token: "Alpha" },
        { type: "delete", token: "," },
        { type: "delete", token: "beta" },
        { type: "delete", token: "." },
      ])),
      punctuation: revisionState(renderDiffOperations([
        { type: "same", token: "This" },
        { type: "same", token: "Agreement" },
        { type: "same", token: "(" },
        { type: "delete", token: "California" },
        { type: "insert", token: "England" },
        { type: "insert", token: "and" },
        { type: "insert", token: "Wales" },
        { type: "same", token: ")" },
        { type: "same", token: "applies" },
        { type: "same", token: "." },
      ])),
      fallback: revisionState(renderDiffOperations(fullReplacementOperations("Old paragraph.", "New paragraph."))),
    };
  });

  assert.equal(cases.emptyInsert.original, "");
  assert.equal(cases.emptyInsert.accepted, "Alpha, beta.");
  assert.deepEqual(cases.emptyInsert.deleted, []);
  assert.deepEqual(cases.emptyInsert.inserted, ["Alpha", ",", " beta", "."]);

  assert.equal(cases.emptyDelete.original, "Alpha, beta.");
  assert.equal(cases.emptyDelete.accepted, "");
  assert.deepEqual(cases.emptyDelete.deleted, ["Alpha", ",", " beta", "."]);
  assert.deepEqual(cases.emptyDelete.inserted, []);

  assert.equal(cases.punctuation.original, "This Agreement (California) applies.");
  assert.equal(cases.punctuation.accepted, "This Agreement (England and Wales) applies.");
  assert.deepEqual(cases.punctuation.deleted, ["California"]);
  assert.deepEqual(cases.punctuation.inserted, ["England", " and", " Wales"]);

  assert.equal(cases.fallback.original, "Old paragraph.");
  assert.equal(cases.fallback.accepted, "New paragraph.");
  assert.deepEqual(cases.fallback.deleted, ["Old paragraph."]);
  assert.deepEqual(cases.fallback.inserted, ["New paragraph."]);
}

async function testBackendRedlineModes(page) {
  await runReview(page, redlineNda);
  assert.equal(await page.locator(".studio-check-card").count(), 0);
  const checkPillStyles = await page.locator(".studio-clause-item.check .studio-issue-pill.check").first().evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      color: styles.color,
      boxShadow: styles.boxShadow,
    };
  });
  assert.equal(checkPillStyles.backgroundColor, "rgb(254, 226, 226)");
  assert.equal(checkPillStyles.color, "rgb(180, 35, 24)");
  assert.match(checkPillStyles.boxShadow, /252, 165, 165/);

  const checkDotStyles = await page.locator(".studio-clause-dot.verify").first().evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      boxShadow: styles.boxShadow,
    };
  });
  assert.equal(checkDotStyles.backgroundColor, "rgb(239, 68, 68)");
  assert.match(checkDotStyles.boxShadow, /239, 68, 68/);

  const prohibitedParagraphStyles = await page.locator('[data-paragraph-id="p2"]').evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      hasProhibitedClass: node.classList.contains("prohibited"),
      backgroundColor: styles.backgroundColor,
      borderLeftColor: styles.borderLeftColor,
      borderLeftWidth: styles.borderLeftWidth,
    };
  });
  assert.equal(prohibitedParagraphStyles.hasProhibitedClass, true);
  assert.equal(prohibitedParagraphStyles.borderLeftColor, "rgb(239, 68, 68)");
  assert.equal(prohibitedParagraphStyles.borderLeftWidth, "4px");
  assert.equal(prohibitedParagraphStyles.backgroundColor, "rgba(239, 68, 68, 0.08)");

  await page.locator('[data-studio-clause-id="term_and_survival"]').click();

  const termParagraph = page.locator('[data-paragraph-id="p1"]');
  const termParagraphStyles = await termParagraph.evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      borderLeftColor: styles.borderLeftColor,
    };
  });
  assert.equal(termParagraphStyles.backgroundColor, "rgb(254, 226, 226)");
  assert.equal(termParagraphStyles.borderLeftColor, "rgb(239, 68, 68)");
  await assertRedlinePreview(termParagraph, {
    originalText: "seven",
    insertedText: "fixed period of up to five",
    editableCount: 1,
  });
  await assertRedGreenPixels(termParagraph.locator(".paragraph-redline-preview"));

  await page.getByRole("button", { name: "Clean" }).click();
  const cleanText = await page.locator("#studioDocumentRender").innerText();
  assert.match(cleanText, /fixed period of up to five years/);
  assert.doesNotMatch(cleanText, /seven years/);
  assert.doesNotMatch(cleanText, /must not circumvent/);

  await page.getByRole("button", { name: "Side by Side" }).click();
  const sideBySide = await page.locator('[data-paragraph-id="p1"]').evaluate((node) => ({
    labels: Array.from(node.querySelectorAll(".clause-sxs-tag")).map((label) => label.textContent),
    original: node.querySelector(".clause-sxs-col:first-child div")?.innerText || "",
    redline: node.querySelector(".clause-sxs-col.latest div")?.innerText || "",
    delCount: node.querySelectorAll(".clause-sxs-col.original .inline-del").length,
    insCount: node.querySelectorAll(".clause-sxs-col.latest .inline-ins").length,
  }));
  assert.deepEqual(sideBySide.labels, ["Original", "Proposed"]);
  assert.match(sideBySide.original, /seven years/);
  assert.match(sideBySide.redline, /fixed period of up to five years/);
  assert.ok(sideBySide.delCount >= 1, "side-by-side redline should show deletions");
  assert.ok(sideBySide.insCount >= 1, "side-by-side redline should show insertions");
  await assertRedPixels(page.locator('[data-paragraph-id="p1"] .clause-sxs-col.original'));
  await assertGreenPixels(page.locator('[data-paragraph-id="p1"] .clause-sxs-col.latest'));

  const deletedSideBySide = await page.locator('[data-paragraph-id="p2"]').evaluate((node) => ({
    original: node.querySelector(".clause-sxs-col.original div")?.innerText || "",
    proposed: node.querySelector(".clause-sxs-col.latest div")?.innerText || "",
    originalDeleted: node.querySelectorAll(".clause-sxs-col.original .inline-del").length,
    proposedEmpty: node.querySelector(".clause-sxs-col.latest .sxs-empty")?.textContent || "",
  }));
  assert.match(deletedSideBySide.original, /must not circumvent/);
  assert.equal(deletedSideBySide.originalDeleted, 1);
  assert.equal(deletedSideBySide.proposed, "Removed in proposed text");
  assert.equal(deletedSideBySide.proposedEmpty, "Removed in proposed text");

  const insertedBlocks = await page.locator('[data-redline-anchor-id="p2"]').evaluateAll((nodes) => (
    nodes.map((node) => ({
      original: node.querySelector(".clause-sxs-col.original div")?.innerText || "",
      proposed: node.querySelector(".clause-sxs-col.latest div")?.innerText || "",
      proposedInserted: node.querySelectorAll(".clause-sxs-col.latest .inline-ins").length,
    }))
  ));
  const insertedSideBySide = insertedBlocks.find((block) => block.proposed.includes("For [Party 1 legal name]"));
  assert.ok(insertedSideBySide, "signature insertion should render as a side-by-side inserted block");
  assert.equal(insertedSideBySide.original, "No source paragraph");
  assert.match(insertedSideBySide.proposed, /For \[Party 1 legal name\]/);
  assert.equal(insertedSideBySide.proposedInserted, 1);
}

async function testClauseAnchorCycling(page) {
  await runReview(page, multiAnchorNda);
  const nonCircumventionCard = page.locator('[data-studio-clause-id="non_circumvention"]');

  await nonCircumventionCard.click();
  await page.waitForSelector('[data-paragraph-id="p1"].paragraph-pulse');
  assert.equal(await page.locator('[data-paragraph-id="p2"]').evaluate((node) => node.classList.contains("paragraph-pulse")), false);

  await nonCircumventionCard.click();
  await page.waitForSelector('[data-paragraph-id="p2"].paragraph-pulse');
  assert.equal(await page.locator('[data-paragraph-id="p1"]').evaluate((node) => node.classList.contains("paragraph-pulse")), false);
}

async function testClauseDecisionControls(page) {
  await runReview(page, "This Agreement shall be governed by the laws of California.");

  await page.locator('[data-studio-clause-id="governing_law"]').click();
  await page.getByRole("button", { name: "DIFC This Agreement shall be governed by the laws of the DIFC." }).click();
  await assertTextContains(page.locator(".redline-option.selected"), "DIFC");

  await page.locator('[data-export-clause-id="signatures"][data-export-decision="ignore"]').click();
  await assertTextContains(page.locator('[data-lane-card-id="signatures"] .studio-export-state'), "IGNORED IN EXPORT");

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#studioExportButton").click(),
  ]);
  const exportedPath = await download.path();
  assert.ok(exportedPath, "decision export download path should be available");
  const exportedChanges = readDocxTrackChanges(exportedPath);
  assert.ok(
    exportedChanges.revisionParagraphs.some((paragraph) => (
      normalizeWhitespace(paragraph.original) === "This Agreement shall be governed by the laws of California."
      && normalizeWhitespace(paragraph.accepted) === "This Agreement shall be governed by the laws of the DIFC."
    )),
    "selected template option should drive the exported governing-law redline",
  );
  assert.equal(
    exportedChanges.insertions.some((text) => text.includes("For [Party 1 legal name]")),
    false,
    "ignored signature redline should not be exported",
  );
  assert.equal(
    exportedChanges.insertions.some((text) => text.includes("England and Wales")),
    false,
    "default governing-law template should not leak after choosing DIFC",
  );
}

async function testManualViewerEditRedline(page) {
  await runReview(page, passNda);
  const pasteResult = await page.evaluate(() => {
    const editable = document.querySelector('[data-editable-paragraph-id="p1"]');
    editable.textContent = "Alpha";
    const range = document.createRange();
    range.selectNodeContents(editable);
    range.collapse(false);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);

    const originalExecCommand = document.execCommand;
    let execCommandCalled = false;
    let defaultPrevented = false;
    document.execCommand = () => {
      execCommandCalled = true;
      return false;
    };
    pastePlainText({
      clipboardData: { getData: (type) => type === "text/plain" ? " Beta" : "" },
      preventDefault: () => {
        defaultPrevented = true;
      },
    });
    document.execCommand = originalExecCommand;

    return {
      defaultPrevented,
      execCommandCalled,
      text: editable.textContent,
    };
  });
  assert.deepEqual(pasteResult, {
    defaultPrevented: true,
    execCommandCalled: false,
    text: "Alpha Beta",
  });

  const editedTitle = "Mutual Non-Disclosure AGREEMdasdasdsa";
  await page.locator('[data-editable-paragraph-id="p1"]').fill(editedTitle);
  await page.waitForSelector('[data-paragraph-id="p1"].manual-redline');

  const paragraph = page.locator('[data-paragraph-id="p1"]');
  await assertRedlinePreview(paragraph, {
    originalText: "Agreement",
    insertedText: "AGREEMdasdasdsa",
    editableCount: 1,
  });
  await assertRedGreenPixels(paragraph.locator(".paragraph-redline-preview"));

  assert.equal(await page.locator("#studioExportButton").isEnabled(), true);
  await assertTextContains(page.locator("#studioFileMeta"), "Edited in viewer");

  await page.getByRole("button", { name: "Side by Side" }).click();
  const sideBySide = await page.locator('[data-paragraph-id="p1"]').evaluate((node) => ({
    original: node.querySelector(".clause-sxs-col:first-child div")?.innerText || "",
    redline: node.querySelector(".clause-sxs-col.latest div")?.innerText || "",
    delCount: node.querySelectorAll(".clause-sxs-col.original .inline-del").length,
    insCount: node.querySelectorAll(".clause-sxs-col.latest .inline-ins").length,
  }));
  assert.equal(sideBySide.original, "Mutual Non-Disclosure Agreement");
  assert.match(sideBySide.redline, /AGREEMdasdasdsa/);
  assert.ok(sideBySide.delCount >= 1, "manual side-by-side redline should show deletions");
  assert.ok(sideBySide.insCount >= 1, "manual side-by-side redline should show insertions");
}

async function testPreviewMatchesExportedDocx(page) {
  await runReview(page, allActionRedlineNda);

  await page.getByRole("button", { name: "Side by Side" }).click();
  const preview = await page.evaluate(() => {
    const textWithoutDeleted = (node) => {
      const clone = node?.cloneNode(true);
      clone?.querySelectorAll(".inline-del").forEach((item) => item.remove());
      return clone?.innerText || "";
    };
    const paragraphPreview = (edit) => {
      const paragraphId = edit.paragraph_id;
      const paragraph = document.querySelector(`[data-paragraph-id="${paragraphId}"]`);
      const latest = paragraph?.querySelector(".clause-sxs-col.latest div");
      const accepted = latest?.querySelector(".sxs-empty") ? "" : textWithoutDeleted(latest);
      return {
        original: paragraph?.querySelector(".clause-sxs-col:first-child div")?.innerText || "",
        redline: latest?.innerText || "",
        accepted,
      };
    };
    const insertionPreview = (edit) => {
      const insertion = document.querySelector(`[data-redline-edit-id="${edit.id}"]`);
      const latest = insertion?.querySelector(".clause-sxs-col.latest div");
      return {
        original: "",
        redline: latest?.innerText || "",
        accepted: textWithoutDeleted(latest),
      };
    };
    return state.reviewRedlines.map((edit) => ({
      edit,
      preview: edit.action === REDLINE_INSERT_AFTER_PARAGRAPH
        ? insertionPreview(edit)
        : paragraphPreview(edit),
    }));
  });

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#studioExportButton").click(),
  ]);
  const exportedPath = await download.path();
  assert.ok(exportedPath, "download path should be available");
  const exportedChanges = readDocxTrackChanges(exportedPath);
  assert.ok(preview.some(({ edit }) => edit.action === "replace_paragraph"), "fixture should include replace redlines");
  assert.ok(preview.some(({ edit }) => edit.action === "insert_after_paragraph"), "fixture should include insert redlines");
  assert.ok(preview.some(({ edit }) => edit.action === "delete_paragraph"), "fixture should include delete redlines");

  for (const { edit, preview: previewParagraph } of preview) {
    const expectedOriginal = edit.action === "insert_after_paragraph" ? "" : edit.original_text;
    const expectedAccepted = edit.action === "delete_paragraph"
      ? ""
      : edit.action === "insert_after_paragraph"
        ? edit.insert_text
        : edit.replacement_text;
    assert.equal(normalizeWhitespace(previewParagraph.original), normalizeWhitespace(expectedOriginal), `${edit.id} preview original`);
    assert.equal(normalizeWhitespace(previewParagraph.accepted), normalizeWhitespace(expectedAccepted), `${edit.id} preview accepted`);

    const exportedParagraph = exportedChanges.revisionParagraphs.find((paragraph) => (
      normalizeWhitespace(paragraph.original) === normalizeWhitespace(previewParagraph.original)
      && normalizeWhitespace(paragraph.accepted) === normalizeWhitespace(previewParagraph.accepted)
    ));
    assert.ok(
      exportedParagraph,
      `${edit.id} ${edit.action} should match a DOCX revision paragraph`,
    );
    if (edit.action === "replace_paragraph") {
      assert.equal(
        exportedParagraph.deletions.some((text) => normalizeWhitespace(text) === normalizeWhitespace(previewParagraph.original)),
        false,
        `${edit.id} replacement redline should be word-level, not a whole-paragraph deletion`,
      );
    }
  }
}

async function testSourceRedlineExportRegression(page) {
  const tmpDir = fs.mkdtempSync(path.join(osTmpDir(), "nda-source-redline-"));
  const sourceDocxPath = path.join(tmpDir, "Source Redline NDA.docx");
  makeDocxFixture(sourceDocxPath, [
    "NON-DISCLOSURE AGREEMENT (NDA)",
    "This Agreement shall be governed by the laws of California.",
    "The Recipient must not circumvent the Company.",
  ]);

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.locator("#fileInput").setInputFiles(sourceDocxPath);
  await assertTextContains(page.locator("#studioFileMeta"), "ready for review");
  await page.getByRole("button", { name: "Review NDA" }).click();
  await page.waitForSelector("#studioDocumentRender:not([hidden])");
  await page.waitForSelector(".studio-clause-item.pass, .studio-clause-item.check");

  assert.equal(await page.locator("#studioDocTitle").innerText(), "Source Redline NDA.docx");
  assert.ok(await page.locator(".studio-clause-item.check").count() > 0, "source-redline review should produce CHECK findings");

  await page.locator('[data-editable-paragraph-id="p1"]').fill("Do you see problem?");
  await page.waitForSelector('[data-paragraph-id="p1"].manual-redline');
  await assertTextContains(page.locator("#studioFileMeta"), "Edited in viewer");

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#studioExportButton").click(),
  ]);
  assert.equal(download.suggestedFilename(), "Source-Redline-NDA-redlined.docx");
  const exportedPath = await download.path();
  assert.ok(exportedPath, "source-redline export download path should be available");

  const exportedDocx = readDocxTrackChanges(exportedPath);
  assert.equal(exportedDocx.hasTrackRevisions, true, "source-redline export must enable Word track revisions");
  assert.ok(!exportedDocx.documentXml.includes("NDA Redline"), "source export must not become the report wrapper");
  assert.ok(!exportedDocx.documentXml.includes("Review Notes"), "source export must not leak review notes");
  assert.ok(exportedDocx.documentXml.includes("The Recipient must not circumvent the Company."), "source paragraphs must survive export");
  assert.ok(
    exportedDocx.revisionParagraphs.some((paragraph) => (
      normalizeWhitespace(paragraph.original) === "NON-DISCLOSURE AGREEMENT (NDA)"
      && normalizeWhitespace(paragraph.accepted) === "Do you see problem?"
    )),
    "viewer edit must export as a native Word tracked change on the uploaded source",
  );
  assert.ok(
    exportedDocx.revisionParagraphs.some((paragraph) => (
      normalizeWhitespace(paragraph.original) === "This Agreement shall be governed by the laws of California."
      && normalizeWhitespace(paragraph.accepted) === "This Agreement shall be governed by the laws of England and Wales."
    )),
    "clause redline must still map to and revise the exact source paragraph",
  );
  assert.ok(
    exportedDocx.revisionParagraphs.some((paragraph) => (
      normalizeWhitespace(paragraph.original) === "The Recipient must not circumvent the Company."
      && normalizeWhitespace(paragraph.accepted) === ""
    )),
    "delete redline must remain a native deletion against the source paragraph",
  );
}

async function testExportFlow(page) {
  await runReview(page, passNda);
  const exportButton = page.locator("#studioExportButton");
  assert.equal(await exportButton.isEnabled(), true);

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    exportButton.click(),
  ]);
  assert.equal(download.suggestedFilename(), "nda-review-report.docx");
  const downloadedPath = await download.path();
  assert.ok(downloadedPath, "download path should be available");
  assert.ok(fs.statSync(downloadedPath).size > 1000, "exported DOCX should not be empty");
  await assertTextContains(page.locator("#studioFileMeta"), "Saved export:");
  await assertTextContains(page.locator("#studioFileMeta"), "/exports/nda-review-report.docx");
  await assertTextContains(page.locator("#studioFileMeta"), "Word package verified");
  await assertTextContains(page.locator("#studioFileMeta"), "Track Changes enabled");
  await assertTextContains(page.locator("#studioFileMeta a.download-again"), "Download again");
  assert.equal(await page.locator("#studioFileMeta a.download-again").getAttribute("href"), "/exports/nda-review-report.docx");
  assert.equal(await page.locator("#studioFileMeta a.download-again").getAttribute("download"), "nda-review-report.docx");

  await page.locator('[data-editable-paragraph-id="p1"]').fill("Mutual Non-Disclosure Agreement with edits");
  await page.waitForSelector('[data-paragraph-id="p1"].manual-redline');
  assert.equal(await exportButton.isEnabled(), true);

  const [editedDownload] = await Promise.all([
    page.waitForEvent("download"),
    exportButton.click(),
  ]);
  const editedDownloadedPath = await editedDownload.path();
  assert.ok(editedDownloadedPath, "edited export download path should be available");
  const editedChanges = readDocxTrackChanges(editedDownloadedPath);
  assert.ok(
    editedChanges.revisionParagraphs.some((paragraph) => (
      normalizeWhitespace(paragraph.original) === "Mutual Non-Disclosure Agreement"
      && normalizeWhitespace(paragraph.accepted) === "Mutual Non-Disclosure Agreement with edits"
    )),
    "edited export should preserve the browser manual edit as a native Word tracked change",
  );
}

async function assertRedlinePreview(paragraphLocator, { originalText, insertedText, editableCount }) {
  const data = await paragraphLocator.evaluate((node) => ({
    editableCount: node.querySelectorAll("[data-editable-paragraph-id]").length,
    previewHidden: node.querySelector(".paragraph-redline-preview")?.hidden ?? true,
    deletedText: Array.from(node.querySelectorAll(".paragraph-redline-preview .inline-del"))
      .map((item) => item.textContent)
      .join(" "),
    insertedText: Array.from(node.querySelectorAll(".paragraph-redline-preview .inline-ins"))
      .map((item) => item.textContent)
      .join(" "),
  }));
  assert.equal(data.editableCount, editableCount);
  assert.equal(data.previewHidden, false);
  assert.match(normalizeWhitespace(data.deletedText), new RegExp(escapeRegExp(originalText)));
  assert.match(normalizeWhitespace(data.insertedText), new RegExp(escapeRegExp(insertedText)));
}

async function assertRedGreenPixels(locator) {
  const { redPixels, greenPixels } = await colorPixelCounts(locator);
  assert.ok(redPixels > 10, `expected visible redline deletion pixels, found ${redPixels}`);
  assert.ok(greenPixels > 10, `expected visible redline insertion pixels, found ${greenPixels}`);
}

async function assertRedPixels(locator) {
  const { redPixels } = await colorPixelCounts(locator);
  assert.ok(redPixels > 10, `expected visible red pixels, found ${redPixels}`);
}

async function assertGreenPixels(locator) {
  const { greenPixels } = await colorPixelCounts(locator);
  assert.ok(greenPixels > 10, `expected visible green pixels, found ${greenPixels}`);
}

async function colorPixelCounts(locator) {
  const png = PNG.sync.read(await locator.screenshot());
  let redPixels = 0;
  let greenPixels = 0;
  for (let offset = 0; offset < png.data.length; offset += 4) {
    const red = png.data[offset];
    const green = png.data[offset + 1];
    const blue = png.data[offset + 2];
    const alpha = png.data[offset + 3];
    if (alpha < 80) continue;
    if (red > 120 && red > green * 1.18 && red > blue * 1.18) redPixels += 1;
    if (green > 80 && green > red * 1.05 && green > blue * 1.05) greenPixels += 1;
  }
  return { redPixels, greenPixels };
}

async function assertTextContains(locator, expected) {
  const text = await locator.innerText();
  assert.ok(text.includes(expected), `expected "${text}" to include "${expected}"`);
}

async function waitForText(page, selector, expected) {
  await page.waitForFunction(
    ({ selector, expected }) => document.querySelector(selector)?.innerText.includes(expected),
    { selector, expected },
  );
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeWhitespace(value) {
  return String(value).replace(/\s+/g, " ").trim();
}

function osTmpDir() {
  return fs.realpathSync(os.tmpdir());
}

function makeDocxFixture(docxPath, paragraphs) {
  const script = `
import sys
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape

docx_path = sys.argv[1]
paragraphs = sys.argv[2:]
body = "".join(
    '<w:p><w:r><w:t xml:space="preserve">{}</w:t></w:r></w:p>'.format(escape(paragraph))
    for paragraph in paragraphs
)
document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body}</w:body>
</w:document>'''
with ZipFile(docx_path, "w", ZIP_DEFLATED) as archive:
    archive.writestr("word/document.xml", document_xml)
`;
  const result = spawnSync(PYTHON, ["-c", script, docxPath, ...paragraphs], { encoding: "utf8" });
  if (result.status !== 0) {
    throw new Error(`Could not create DOCX fixture: ${result.stderr || result.stdout}`);
  }
}

function readDocxTrackChanges(docxPath) {
  const script = `
import json
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
with ZipFile(sys.argv[1]) as archive:
    document_xml = archive.read("word/document.xml").decode("utf-8")
    root = ET.fromstring(document_xml)
    settings_xml = archive.read("word/settings.xml").decode("utf-8") if "word/settings.xml" in archive.namelist() else ""

def text_for_revision_state(node, accepted):
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "del":
        return "".join((item.text or "") for item in node.findall(".//w:delText", W_NS)) if not accepted else ""
    if tag == "ins":
        return "".join((item.text or "") for item in node.findall(".//w:t", W_NS)) if accepted else ""
    if tag == "t":
        return node.text or ""
    if tag == "br":
        return "\\n"
    return "".join(text_for_revision_state(child, accepted) for child in list(node))

deletions = [
    "".join(node.text or "" for node in deletion.findall(".//w:delText", W_NS))
    for deletion in root.findall(".//w:del", W_NS)
]
insertions = [
    "".join(node.text or "" for node in insertion.findall(".//w:t", W_NS))
    for insertion in root.findall(".//w:ins", W_NS)
]
revision_paragraphs = []
for paragraph in root.findall(".//w:p", W_NS):
    paragraph_deletions = [
        "".join(node.text or "" for node in deletion.findall(".//w:delText", W_NS))
        for deletion in paragraph.findall(".//w:del", W_NS)
    ]
    paragraph_insertions = [
        "".join(node.text or "" for node in insertion.findall(".//w:t", W_NS))
        for insertion in paragraph.findall(".//w:ins", W_NS)
    ]
    if paragraph_deletions or paragraph_insertions:
        revision_paragraphs.append({
            "original": text_for_revision_state(paragraph, False),
            "accepted": text_for_revision_state(paragraph, True),
            "deletions": paragraph_deletions,
            "insertions": paragraph_insertions,
        })
print(json.dumps({
    "deletions": deletions,
    "insertions": insertions,
    "revisionParagraphs": revision_paragraphs,
    "documentXml": document_xml,
    "hasTrackRevisions": "<w:trackRevisions" in settings_xml,
}))
`;
  const result = spawnSync(PYTHON, ["-c", script, docxPath], { encoding: "utf8" });
  if (result.status !== 0) {
    throw new Error(`Could not read exported DOCX track changes: ${result.stderr || result.stdout}`);
  }
  return JSON.parse(result.stdout);
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
