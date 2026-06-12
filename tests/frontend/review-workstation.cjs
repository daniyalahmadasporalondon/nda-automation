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
// Both test servers run the AI-first engine with the deterministic, key-free AI
// assessment stub so review flows exercise the production review policy without
// live provider calls.
const AI_FIRST_PORT = PORT + 1;
const AI_FIRST_BASE_URL = `http://127.0.0.1:${AI_FIRST_PORT}`;
const PYTHON = process.env.PYTHON || "python3";
const VIEWPORT = { width: 1440, height: 1000 };
const TEST_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "nda-automation-data-"));
const AI_FIRST_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "nda-automation-aifirst-"));

const passNda = fs.readFileSync(path.join(ROOT, "samples", "pass-nda.txt"), "utf8").trim();
const redlineNda = [
  "The confidentiality obligations survive for seven years.",
  "The Recipient must not circumvent the Company or deal directly with introduced parties.",
].join("\n\n");
const termOnlyRedlineNda = [
  "Each party may disclose Confidential Information and each party acts as both a Disclosing Party and Receiving Party.",
  "Confidential Information means all non-public business, financial, technical, customer, employee, supplier, pricing, market, product, trade secret, proprietary, and source code information.",
  "This Agreement shall be governed by the laws of Delaware.",
  "The confidentiality obligations survive for seven years.",
  "Neither party is restricted from ordinary third-party dealings outside this Agreement.",
  "For Aspora Ltd\nBy: A. Signatory\nTitle: Director\nDate: 2026-05-30\n\nFor Counterparty Ltd\nBy: B. Signatory\nTitle: CEO\nDate: 2026-05-30",
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
  ["edits playbook admin drafts with Pass/Check policy framing", testPlaybookAdminEditor],
  ["renders contract structure map in review and engine logic in admin", testContractStructureReviewPanel],
  ["surfaces review and export error details", testFailureUxDetails],
  ["renders progressive PDF preview with text fallback", testProgressivePdfPreviewFallback],
  ["renders page image preview with text fallback", testRenderedPageImagePreview],
  ["loads page image preview from render-status", testRenderStatusPageImagePreviewFetch],
  ["toggles the Original page-image view and shows the graceful fallback", testOriginalViewToggle],
  ["marks up the Original PDF view with comments, highlights, and a download", testPdfMarkupOriginalView],
  ["renders rich document structure while preserving clause/redline/comment anchoring", testRichDocumentStructureRendering],
  ["renders the seven-section clause card schema", testStructuredEvidenceAndRationale],
  ["renders structured proposed changes in the review inspector", testStructuredProposedChangePanel],
  ["renders interactive jurisdiction picker in needs-review card", testNeedsReviewJurisdictionPicker],
  ["keeps AI second opinion controls out of the review inspector", testAiSecondOpinionButton],
  ["keeps AI draft validation controls out of redline suggestions", testAiDraftFixValidationButton],
  ["toggles per-clause reviewed state from the lane", testPerClauseReviewedToggle],
  ["updates the review status summary after human sign-off", testReviewedMatterStatusSummary],
  ["sends the currently loaded review matter after switching documents", testReviewSendUsesCurrentMatterAfterSwitch],
  ["sends review email with a typed recipient when none was detected", testReviewSendAcceptsManualRecipient],
  ["opens the Generator tab, generates an NDA, and downloads the saved document", testDraftIntakeGenerateNda],
  ["surfaces generation self-check warnings while staging the artifact", testDraftIntakeGenerateSelfCheckWarning],
  ["degrades the Generate button gracefully when generation is not deployed", testDraftIntakeGenerateDegradesOn404],
  ["guards Save-As picker fallbacks", testSavePickerGuardsAndFallbacks],
  ["renders server-provided inline diff operations", testInlineDiffOperationRendering],
  ["renders backend redlines across all document modes", testBackendRedlineModes],
  ["imports repository matters and re-reviews as fresh text", testRepositoryMatterImportAndFreshReview],
  ["opens repository matters into review repeatedly", testRepositoryOpenReviewRepeatedly],
  ["wires stale review refresh controls", testStaleReviewRefreshWiring],
  ["flags stale matters on the board and refreshes from the inspector", testRepositoryStaleBadgeAndRefresh],
  ["clears repository board after load errors", testRepositoryLoadErrorClearsBoard],
  ["uploads local NDAs through the dashboard upload modal", testManualUploadModal],
  ["sends repository redline email with composer details", testRepositoryOutboundSendComposer],
  ["syncs a matter's artifacts to its Drive folder from the inspector", testRepositorySaveToDriveSuccess],
  ["shows the up-to-date message when no Drive files needed syncing", testRepositorySaveToDriveUpToDate],
  ["prompts to connect Drive when the matter NDA upload is unauthorized", testRepositorySaveToDriveNotConnected],
  ["renders the admin Drive connect status and saves folder settings", testAdminDriveSection],
  ["renders Admin Personalisation fields and saves sign-off settings", testAdminPersonalisationSection],
  ["sends review redline email from editable composer", testReviewOutboundSendModal],
  ["blocks repository outbound send when Gmail is not ready", testRepositoryOutboundSendBlocked],
  ["shows Gmail setup required instead of stale sync errors", testGmailSetupRequiredStatus],
  ["renders user Gmail session controls and sync history", testUserGmailSessionControls],
  ["uses shared Gmail profile identity in the account menu", testSharedGmailProfileAccountMenu],
  ["persists matter redline drafts", testMatterRedlineDraftPersistence],
  ["exports selected clause decisions and template options", testClauseDecisionControls],
  ["renders manual viewer edits as local redlines", testManualViewerEditRedline],
  ["preserves viewer caret through auto-refresh", testViewerAutoRefreshSelection],
  ["keeps browser preview aligned with exported DOCX redlines", testPreviewMatchesExportedDocx],
  ["guards source-redline export regression", testSourceRedlineExportRegression],
  ["marks the exported matter ready after a mid-export switch", testExportMarksCapturedMatterReady],
  ["exports reviewed DOCX and blocks stale edited exports", testExportFlow],
  ["shows reconstructed PDF export metadata in the review download menu", testReviewDownloadMenuPdfReconstructionMetadata],
  ["renders the playbook preferred position and span highlight on a clause", testPlaybookPositionAndSpanHighlight],
  ["renders backend redline rationale beside the suggested edit", testRedlineRationaleBlock],
  ["collapses the reasoning trail and remembers its open state", testReasoningTrailCollapse],
  ["gates Approve Review on staleness only", testApproveReviewGate],
  ["labels the document verdict with text and icon, not colour alone", testDocumentVerdictLabel],
  ["guards unsaved redline edits before refreshing the review", testRefreshUnsavedEditsGuard],
  ["honours the reduced-motion preference", testReducedMotionPreference],
  ["renders the AI review health panel with status banner and metrics", testAdminHealthPanel],
  ["filters dashboard matters with the smart-search chips and opens a result", testDashboardSmartSearch],
  ["translates a natural-language query into a filter and falls back to keyword search", testDashboardSmartSearchV2],
  ["pops an in-app toast when a new inbound NDA arrives", testInboundNotificationToast],
];

// Tests that run against the AI-first + stub-reviewer server (AI_FIRST_BASE_URL),
// where the dynamic non_circumvention clause is actually produced.
const aiFirstTests = [
  ["renders the dynamic prohibited clause with prohibited styling and a delete redline", testDynamicProhibitedClauseRendering],
  ["cycles clause-to-paragraph anchors", testClauseAnchorCycling],
];

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

async function main() {
  const server = startServer();
  const aiFirstServer = startServer({
    port: AI_FIRST_PORT,
    dataDir: AI_FIRST_DATA_DIR,
    env: {
      NDA_ACTIVE_REVIEW_ENGINE: "ai_first",
      NDA_AI_REVIEW_ENABLED: "true",
      NDA_AI_ASSESSMENT_STUB: "1",
    },
  });
  let browser;
  try {
    await waitForServer();
    await waitForServer(AI_FIRST_BASE_URL);
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

    for (const [name, test] of aiFirstTests) {
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
    await stopServer(aiFirstServer);
  }
}

function startServer({ port = PORT, dataDir = TEST_DATA_DIR, env = {} } = {}) {
  const server = spawn(PYTHON, ["-m", "nda_automation.server", "--port", String(port)], {
    cwd: ROOT,
    env: {
      ...process.env,
      NDA_ACTIVE_REVIEW_ENGINE: "ai_first",
      NDA_AI_REVIEW_ENABLED: "true",
      NDA_AI_ASSESSMENT_STUB: "1",
      NDA_AI_FIRST_REVIEW_ENABLED: "true",
      NDA_DATA_DIR: dataDir,
      NDA_EXPORTS_DIR: path.join(ROOT, "exports"),
      PYTHONUNBUFFERED: "1",
      ...env,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.dataDir = dataDir;
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
  if (server.dataDir) fs.rmSync(server.dataDir, { force: true, recursive: true });
}

async function waitForServer(baseUrl = BASE_URL) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < 10000) {
    if (await healthCheck(baseUrl)) return;
    await wait(120);
  }
  throw new Error(`Server did not start at ${baseUrl}`);
}

function healthCheck(baseUrl = BASE_URL) {
  return new Promise((resolve) => {
    const request = http.get(`${baseUrl}/`, (response) => {
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

async function runReview(page, text, { baseUrl = BASE_URL } = {}) {
  await page.goto(`${baseUrl}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  await page.getByPlaceholder("Paste NDA text here").fill(text);
  await page.evaluate(async (sourceText) => {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: sourceText }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Review could not run");
    }
    renderResult(payload, payload.extracted_text || sourceText);
  }, text);
  await page.waitForSelector("#studioDocumentRender:not([hidden])");
  await page.waitForSelector(".studio-clause-item.pass, .studio-clause-item.check");
}

async function testAccessibleControlState(page) {
  await page.route("**/api/ai/settings", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ai_review: {
          api_key_configured: true,
          enabled: true,
          model: "x-ai/grok-4.3",
          provider: "openrouter",
        },
        active_review_engine: {
          active_engine: "ai_first",
        },
        operational_warnings: [],
        settings_audit: [],
      }),
    });
  });
  const gmailStatusRoute = "**/api/gmail/status*";
  await page.route(gmailStatusRoute, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: { ready: true },
          outbound: { ready: false },
        },
      }),
    });
  });
  await page.route("**/api/drive/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        connected: false,
        connect_url: "/auth/drive/start",
        enabled: false,
        needs_connect: true,
        recovery: {
          action: "connect_google",
          connect_url: "/auth/drive/start",
          message: "Connect Drive to create a drive token for this account.",
          state: "missing_token",
        },
        setup: {
          action: "connect_google",
          connect_url: "/auth/drive/start",
          google_oauth_configured: true,
          message: "Connect Drive for the signed-in Google account.",
          signed_in: true,
          state: "ready_to_connect",
        },
        signed_in: true,
        token: { configured: false, label: "Connect Google for drive", source: "missing" },
        user_scoped: true,
      }),
    });
  });
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  assert.equal(await page.locator("#studioResultMeta").getAttribute("aria-live"), "polite");
  assert.equal(await page.locator("#studioFileMeta").getAttribute("aria-live"), "polite");
  assert.equal(await page.getByRole("tablist", { name: "Workspace" }).count(), 1);
  assert.equal(await page.locator("#dashboardTab").getAttribute("role"), "tab");
  assert.equal(await page.locator("#reviewTab").getAttribute("role"), "tab");
  assert.equal(await page.locator("#playbookTab").getAttribute("role"), "tab");
  assert.equal(await page.locator("#adminTab").getAttribute("role"), "tab");
  assert.equal(await page.locator("#guideTab").getAttribute("role"), "tab");
  assert.equal(await page.getByRole("tab", { name: "Upload" }).count(), 0);
  assert.equal(await page.locator("#dashboardTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#playbookTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#adminTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#guideTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#dashboardView").isHidden(), false);
  // Greeting uses the person's name when identity is available, else a plain
  // placeholder-free "Welcome back" — never the old "Counsel" stand-in.
  await assertTextContains(page.locator("#dashboardHeroTitle"), "Welcome back");
  assert.equal((await page.locator("#dashboardHeroTitle").innerText()).includes("Counsel"), false);
  await assertTextContains(page.locator("#dashboardView"), "Submit for Review");
  await assertTextContains(page.locator("#dashboardView"), "Send Document");
  assert.equal(await page.getByRole("button", { name: "Send Document" }).isDisabled(), false);
  assert.equal(
    await page.locator(".dashboard-send-document-button").getAttribute("data-dashboard-send-document"),
    "",
  );
  await page.waitForFunction(() => document.querySelector('[data-dashboard-health="ai"]')?.classList.contains("ready"));
  await page.waitForFunction(() => document.querySelector('[data-dashboard-health="email"]')?.classList.contains("warning"));
  await assertTextContains(page.locator('[data-dashboard-health="ai"]'), "AI Review");
  await assertTextContains(page.locator('[data-dashboard-health="email"]'), "Email");
  const dashboardHealthText = await page.locator(".dashboard-health-list").innerText();
  assert.match(dashboardHealthText, /AI Review|Email|Drive/);
  assertAttributeMatches(page.locator('[data-dashboard-health="email"]'), "aria-label", /Email: Outbound needs setup/);
  assert.equal(await page.locator('[data-dashboard-health="email"] .dashboard-health-name').isVisible(), true);
  assert.equal(await page.locator('[data-dashboard-health="email"] .dashboard-health-detail').evaluate((node) => {
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.position === "absolute" && rect.width <= 1 && rect.height <= 1;
  }), true);
  assert.equal(await page.locator('[data-dashboard-health="ai"]').evaluate((node) => node.classList.contains("ready")), true);
  assert.equal(await page.locator('[data-dashboard-health="email"]').evaluate((node) => node.classList.contains("warning")), true);
  // Drive is an optional integration: not connected in the harness -> warning (amber), not blocked.
  await page.waitForFunction(() => document.querySelector('[data-dashboard-health="drive"]')?.classList.contains("warning"));
  await assertTextContains(page.locator('[data-dashboard-health="drive"]'), "Drive");
  assertAttributeMatches(page.locator('[data-dashboard-health="drive"]'), "title", /Drive token missing/);
  assert.equal(await page.locator('[data-dashboard-health="drive"] .dashboard-health-name').isVisible(), true);
  assert.equal(await page.locator('[data-dashboard-health="drive"]').evaluate((node) => node.classList.contains("warning")), true);
  const dashboardHealthLayout = await page.evaluate(() => {
    const ai = document.querySelector('[data-dashboard-health="ai"]').getBoundingClientRect();
    const email = document.querySelector('[data-dashboard-health="email"]').getBoundingClientRect();
    const drive = document.querySelector('[data-dashboard-health="drive"]').getBoundingClientRect();
    const dotLabelGaps = Array.from(document.querySelectorAll(".dashboard-health-head")).map((head) => {
      const dot = head.querySelector(".dashboard-health-dot").getBoundingClientRect();
      const label = head.querySelector(".dashboard-health-name").getBoundingClientRect();
      return Math.round(label.left - dot.right);
    });
    return {
      clearDotLabelGaps: dotLabelGaps.every((gap) => gap >= 6),
      sameRow: Math.abs(ai.top - email.top) <= 2 && Math.abs(email.top - drive.top) <= 2,
      emailAfterAi: email.left > ai.left,
      driveAfterEmail: drive.left > email.left,
    };
  });
  assert.deepEqual(dashboardHealthLayout, {
    clearDotLabelGaps: true,
    sameRow: true,
    emailAfterAi: true,
    driveAfterEmail: true,
  });
  assert.equal(await page.locator("#clausesView").getAttribute("hidden"), "");
  assert.equal(await page.locator("#reviewView").getAttribute("hidden"), "");
  assert.equal(await page.getByRole("textbox", { name: "NDA source text" }).count(), 0);
  const dashboardDocxPath = path.join(TEST_DATA_DIR, `dashboard-submit-${Date.now()}.docx`);
  const dashboardFilename = path.basename(dashboardDocxPath);
  makeDocxFixture(dashboardDocxPath, [
    "This Agreement shall be governed by the laws of California.",
  ]);
  await page.getByRole("button", { name: "Submit for Review" }).click();
  await page.waitForSelector("#manualUploadModal:not([hidden])");
  assert.equal(await page.locator("#dashboardTab").getAttribute("aria-selected"), "true");
  await page.locator("#manualUploadFileInput").setInputFiles(dashboardDocxPath);
  await assertTextContains(page.locator("#manualUploadSelectedFile"), dashboardFilename);
  await page.getByRole("button", { name: "Close upload dialog" }).click();
  await page.waitForSelector("#manualUploadModal[hidden]", { state: "attached" });
  await page.getByRole("tab", { name: "Review" }).click();
  assert.equal(await page.getByRole("textbox", { name: "NDA source text" }).count(), 1);
  assert.equal(await page.locator("#studioReviewedDocxButton").count(), 0);
  assert.equal(await page.locator("#studioExportButton").count(), 1);
  assert.equal(await page.locator("#studioSendButton").count(), 1);
  const matterCardStyles = await page.locator(".studio-matter-card").evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      borderRadius: styles.borderRadius,
      boxShadow: styles.boxShadow,
    };
  });
  assert.equal(matterCardStyles.borderRadius, "14px");
  assert.equal(matterCardStyles.boxShadow, "rgba(26, 19, 51, 0.2) 0px 10px 30px -20px");
  assert.equal(await page.locator(".studio-check-card").count(), 0);
  assert.equal(await page.locator(".studio-playbook > h2").innerText(), "SELECTED CLAUSE");
  assert.equal(await page.locator("#studioMatchSummary").innerText(), "0/6");

  await page.locator("#dashboardTab").focus();
  await page.locator("#dashboardTab").press("ArrowRight");
  // The Generator tab sits between Dashboard and Repository in the tab order.
  assert.equal(await page.locator("#generatorTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#generatorTab").getAttribute("tabindex"), "0");
  assert.equal(await page.locator("#dashboardTab").getAttribute("tabindex"), "-1");
  await page.locator("#generatorTab").press("ArrowRight");
  assert.equal(await page.locator("#repositoryTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#repositoryTab").getAttribute("tabindex"), "0");
  await page.locator("#repositoryTab").press("Home");
  assert.equal(await page.locator("#dashboardTab").getAttribute("aria-selected"), "true");
  await page.locator("#dashboardTab").press("End");
  assert.equal(await page.locator("#guideTab").getAttribute("aria-selected"), "true");
  await page.locator("#guideTab").press("Home");
  assert.equal(await page.locator("#dashboardTab").getAttribute("aria-selected"), "true");

  await page.getByRole("tab", { name: "Playbook" }).click();
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#playbookTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#reviewView").getAttribute("hidden"), "");
  assert.equal(await page.locator("#clausesView").getAttribute("data-admin-surface"), "playbook");
  assert.equal(await page.locator(".admin-nav").isHidden(), true);
  const activePlaybookRow = await page.locator(".playbook-row.active").first().evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      borderLeftColor: styles.borderLeftColor,
      borderLeftWidth: styles.borderLeftWidth,
    };
  });
  assert.equal(activePlaybookRow.backgroundColor, "rgba(0, 0, 0, 0)");
  assert.equal(activePlaybookRow.borderLeftWidth, "0px");
  await page.getByRole("tab", { name: "Admin" }).click();
  assert.equal(await page.locator("#clausesView").getAttribute("data-admin-surface"), "admin");
  await assertTextContains(page.locator("#adminAiPanel"), "AI runtime");
  assert.equal(await page.locator("#adminAiPanel").isHidden(), false);
  await page.getByRole("tab", { name: "Guide" }).click();
  assert.equal(await page.locator("#clausesView").getAttribute("data-admin-surface"), "guide");
  await assertTextContains(page.locator("#adminDocumentPanel"), "Structure, references, and concepts");
  assert.equal(await page.locator("#adminDocumentPanel").isHidden(), false);

  await page.getByRole("tab", { name: "Review" }).click();
  await page.getByRole("button", { name: "Clean" }).click();
  assert.equal(await page.locator('[data-view-mode="redline"]').getAttribute("aria-pressed"), "false");
  assert.equal(await page.locator('[data-view-mode="clean"]').getAttribute("aria-pressed"), "true");
  await page.unroute("**/api/ai/settings");
  await page.unroute(gmailStatusRoute);
  await page.unroute("**/api/drive/status");
}

async function testFailureUxDetails(page) {
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
  await chooseDownloadFormat(page.locator("#studioExportButton"), "docx");
  await waitForText(page, "#studioOverallTitle", "The exported Word document failed its open-health check.");
  await assertTextContains(page.locator("#studioOverallTitle"), "The exported Word document failed its open-health check.");
  await assertTextContains(page.locator("#studioResultMeta"), "Export could not run.");
  await assertTextContains(page.locator("#studioResultMeta"), "Missing DOCX parts: _rels/.rels.");
  await page.unroute("**/api/export-review-docx");
}

async function testPlaybookAdminEditor(page) {
  const gmailStatusPayload = {
    gmail: {
      settings: {
        inbound_enabled: true,
        outbound_enabled: true,
        sync_frequency: "10_minutes",
        last_sync_at: "2026-05-31T12:34:00+00:00",
        last_sync_imported_count: 2,
        last_sync_skipped_count: 1,
        sync_history: [{
          deduplicated_count: 2,
          duplicate_count: 1,
          error: "",
          finished_at: "2026-05-31T12:34:00+00:00",
          imported_count: 2,
          query: 'has:attachment (filename:docx OR filename:pdf) newer_than:30d (subject:NDA OR subject:"confidentiality agreement")',
          review_failed_count: 0,
          skipped_count: 1,
          started_at: "2026-05-31T12:33:58+00:00",
          status: "success",
        }],
      },
      inbound: {
        configured: true,
        email: "inbound@example.com",
        enabled: true,
        query: 'has:attachment (filename:docx OR filename:pdf) newer_than:30d (subject:NDA OR subject:"confidentiality agreement")',
        ready: true,
        token: {
          configured: true,
          label: "data/gmail/inbound-token.json",
          source: "local_data",
        },
      },
      outbound: {
        configured: true,
        email: "outbound@example.com",
        enabled: true,
        ready: true,
        token: {
          configured: true,
          label: "NDA_GMAIL_OUTBOUND_TOKEN_PATH",
          source: "environment",
        },
      },
    },
  };
  const gmailSettingsPayloads = [];
  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(gmailStatusPayload),
    });
  });
  await page.route("**/api/gmail/settings", async (route) => {
    const gmailSettingsPayload = route.request().postDataJSON();
    gmailSettingsPayloads.push(gmailSettingsPayload);
    if (Object.prototype.hasOwnProperty.call(gmailSettingsPayload, "inbound_enabled")) {
      gmailStatusPayload.gmail.inbound.enabled = gmailSettingsPayload.inbound_enabled;
      gmailStatusPayload.gmail.inbound.ready = gmailSettingsPayload.inbound_enabled;
      gmailStatusPayload.gmail.settings.inbound_enabled = gmailSettingsPayload.inbound_enabled;
    }
    if (Object.prototype.hasOwnProperty.call(gmailSettingsPayload, "outbound_enabled")) {
      gmailStatusPayload.gmail.outbound.enabled = gmailSettingsPayload.outbound_enabled;
      gmailStatusPayload.gmail.outbound.ready = gmailSettingsPayload.outbound_enabled;
      gmailStatusPayload.gmail.settings.outbound_enabled = gmailSettingsPayload.outbound_enabled;
    }
    if (Object.prototype.hasOwnProperty.call(gmailSettingsPayload, "sync_frequency")) {
      gmailStatusPayload.gmail.settings.sync_frequency = gmailSettingsPayload.sync_frequency;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: gmailStatusPayload.gmail,
        gmail_settings: gmailStatusPayload.gmail.settings,
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        matters: [{
          id: "matter_sent",
          last_outbound_account: "outbound@example.com",
          last_outbound_at: "2026-05-31T20:30:00+00:00",
          last_outbound_subject: "Re: NDA",
          last_outbound_to: "counterparty@example.com",
          subject: "NDA",
        }],
      }),
    });
  });
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Playbook" }).click();
  assert.equal(await page.locator("#clausesView").getAttribute("data-admin-surface"), "playbook");
  await assertTextContains(page.locator("#adminPlaybookPanel"), "Aspora playbook");
  await assertTextContains(page.locator("#clauseDetail"), "Edit Clause: Mutuality");
  await assertTextContains(page.locator("#clauseDetail"), "Policy");
  await assertTextContains(page.locator("#clauseDetail"), "Redline");
  await assertTextContains(page.locator("#clauseDetail"), "Decision Logic");
  await assertTextContains(page.locator("#clauseDetail"), "Audit");
  await assertTextContains(page.locator("#clauseDetail"), "Check Trigger Position");
  await assertTextContains(page.locator("#clauseDetail"), "Required - Check if absent or deficient");
  await page.getByRole("button", { name: "Decision Logic" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "Shared Structure Layer");
  await assertTextContains(page.locator("#clauseDetail"), "Decision Logic Visibility");
  await assertTextContains(page.locator("#clauseDetail"), "AUDIT READING ORDER");
  await assertTextContains(page.locator("#clauseDetail"), "REASON-CODE TAXONOMY");
  await assertTextContains(page.locator("#clauseDetail"), "HARDENING GUARDS");
  await assertTextContains(page.locator("#clauseDetail"), "ANALYSIS PURPOSE");
  await assertTextContains(page.locator("#clauseDetail"), "PRIMARY INPUTS");
  await assertTextContains(page.locator("#clauseDetail"), "HUMAN-REVIEW BOUNDARY");
  await assertTextContains(page.locator("#clauseDetail"), "SIGNAL BUCKETS");
  await assertTextContains(page.locator("#clauseDetail"), "structure_context");
  await assertTextContains(page.locator("#clauseDetail"), "review_state");
  await assertTextContains(page.locator("#clauseDetail"), "structured_evidence");
  await assertTextContains(page.locator("#clauseDetail"), "audit_trace");
  await assertTextContains(page.locator("#clauseDetail"), "mutuality_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "weak_mutuality_paragraph_ids");
  await page.getByRole("button", { name: "Audit" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "Policy Version History");
  await assertTextContains(page.locator("#clauseDetail"), "analysis_purpose");
  await assertTextContains(page.locator("#clauseDetail"), "primary_inputs");
  await assertTextContains(page.locator("#clauseDetail"), "reason_code_taxonomy");
  await assertTextContains(page.locator("#clauseDetail"), "hardening_guards");
  await assertTextContains(page.locator("#clauseDetail"), "mutuality");
  assert.equal(await page.getByText("Walk-away", { exact: false }).count(), 0);
  assert.equal(await page.getByText("Negotiate", { exact: false }).count(), 0);
  assert.equal(await page.getByText("Severity", { exact: false }).count(), 0);
  assert.equal(await page.getByText("Category Group", { exact: false }).count(), 0);
  await page.getByRole("button", { name: "Confidential Information" }).click();
  await page.locator("#clauseDetail").getByRole("button", { name: "Redline" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "Standard Exclusions Language");
  await page.locator("#clauseDetail").getByRole("button", { name: "Decision Logic" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "confidential_information_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "usage_right_review_paragraph_ids");
  assert.equal(await page.getByText("Confidential-Info Exclusions Allowlist", { exact: false }).count(), 0);
  assert.equal(await page.getByPlaceholder("Add exclusion key").count(), 0);
  await page.locator("#clauseDetail").getByRole("button", { name: "Redline" }).click();
  await page.locator('textarea[name="standard_exclusions_template"]').fill("Publicly known information is excluded.");
  await page.locator("#clauseDetail").getByRole("button", { name: "Audit" }).click();
  await assertTextContains(page.locator("#playbookDraftDiff"), "standard_exclusions_template");
  await page.locator('[data-clause-id="term_and_survival"]').click();
  await assertTextContains(page.locator("#clauseDetail"), "Ordinary Confidentiality Cap (years)");
  await assertTextContains(page.locator("#clauseDetail"), "Permitted Perpetual / Longer Survival Carve-outs");
  await assertTextContains(page.locator("#clauseDetail"), "Perpetual / Indefinite Trigger Terms");
  await page.locator("#clauseDetail").getByRole("button", { name: "Decision Logic" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "Checker Logic Visibility");
  await assertTextContains(page.locator("#clauseDetail"), "REFERENCE RESOLVER");
  await assertTextContains(page.locator("#clauseDetail"), "CONCEPT CLASSIFIER");
  await assertTextContains(page.locator("#clauseDetail"), "term_or_survival");
  await assertTextContains(page.locator("#clauseDetail"), "term_survival_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "Claims survive for three years");
  await assertTextContains(page.locator("#clauseDetail"), "unresolved_reference_count");
  await page.locator("#clauseDetail").getByRole("button", { name: "Redline" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "Template Preview");
  await assertTextContains(page.locator("#clauseDetail"), "{max_term_years_label}");
  await assertTextContains(page.locator("#clauseDetail"), "up to five years");
  const termTemplate = await page.locator('textarea[name="redline_template"]').inputValue();
  await page.locator('textarea[name="redline_template"]').fill("Bad {unknown_placeholder}");
  await assertTextContains(page.locator("#clauseDetail"), "Unknown placeholder: unknown_placeholder.");
  assert.equal(await page.getByRole("button", { name: "Save Draft" }).isEnabled(), false);
  await page.locator('textarea[name="redline_template"]').fill(termTemplate);
  await assertTextContains(page.locator("#clauseDetail"), "up to five years");
  await page.locator("#clauseDetail").getByRole("button", { name: "Policy" }).click();
  await page.getByPlaceholder("Add carve-out term").fill("regulatory obligation");
  await page.locator("#addSurvivalCarveOut").click();
  await assertTextContains(page.locator("#clauseDetail"), "regulatory obligation");
  await page.locator("#clauseDetail").getByRole("button", { name: "Audit" }).click();
  await assertTextContains(page.locator("#playbookDraftDiff"), "longer_survival_carve_out_terms");
  await page.locator('[data-clause-id="governing_law"]').click();
  await assertTextContains(page.locator("#clauseDetail"), "Approved Governing Laws");
  assert.equal(await page.locator('textarea[name="redline_template"]').count(), 0);
  await assertTextContains(page.locator("#clauseDetail"), "Draft phrase");
  await page.locator("#clauseDetail").getByRole("button", { name: "Redline" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "Generated Governing Law Redlines");
  await assertTextContains(page.locator("#clauseDetail"), "This Agreement shall be governed by the laws of India.");
  await page.locator("#clauseDetail").getByRole("button", { name: "Decision Logic" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "governing_law_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "heading_only_paragraph_ids");
  await page.locator("#clauseDetail").getByRole("button", { name: "Policy" }).click();
  await page.getByPlaceholder("Add approved jurisdiction").fill("UAE");
  await page.locator("#addGoverningLaw").click();
  const uaeGoverningLawIndex = (await page.locator("[data-governing-law-row]").count()) - 1;
  assert.equal(await page.locator(`input[name="governing_law_value_${uaeGoverningLawIndex}"]`).inputValue(), "UAE");
  await page.locator(`input[name="governing_law_phrase_${uaeGoverningLawIndex}"]`).fill("the UAE");
  await page.locator("#clauseDetail").getByRole("button", { name: "Redline" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "This Agreement shall be governed by the laws of the UAE.");
  await page.locator("#clauseDetail").getByRole("button", { name: "Policy" }).click();
  await page.locator(`input[name="preferred_law_index"][value="${uaeGoverningLawIndex}"]`).check();
  await page.locator("#clauseDetail").getByRole("button", { name: "Audit" }).click();
  await assertTextContains(page.locator("#playbookDraftDiff"), "approved_laws");
  await assertTextContains(page.locator("#playbookDraftDiff"), "rules.approved_options");
  await page.locator('[data-clause-id="non_circumvention"]').click();
  await page.locator("#clauseDetail").getByRole("button", { name: "Decision Logic" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "non_circumvention_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "negated_reference_paragraph_ids");
  await assertTextContains(page.locator("#clauseDetail"), "may not include non-solicitation obligations");
  await page.locator('[data-clause-id="mutuality"]').click();
  await page.getByRole("button", { name: "Policy" }).click();

  // The version banner distinguishes the active published Playbook from the draft.
  // The real server serves the legacy single-playbook GET, so active == draft and
  // the draft starts in sync; editing flips it to an unsaved-changes state.
  // (.eyebrow renders uppercased via CSS text-transform, which innerText reflects.)
  await assertTextContains(page.locator(".playbook-version-card.active"), "ACTIVE PUBLISHED");
  await assertTextContains(page.locator(".playbook-version-card.active"), "Used by the review engine right now.");
  await assertTextContains(page.locator(".playbook-version-card.draft"), "WORKING DRAFT");

  await page.locator('textarea[name="check_trigger"]').fill("One-way obligations need Check review.");
  await assertTextContains(page.locator("#playbookDraftDiff"), "check_trigger");
  await assertTextContains(page.locator(".playbook-version-card.draft"), "Unsaved changes");
  assert.equal(await page.getByRole("button", { name: "Save Draft" }).isEnabled(), true);
  // Publish is blocked while there are unsaved draft edits.
  assert.equal(await page.getByRole("button", { name: "Publish Playbook" }).isEnabled(), false);

  // --- Save Draft: persists the working clauses to the draft only ---
  // The draft block nests version/hash under `metadata` (draft_id / draft_hash),
  // matching the backend's public draft payload shape.
  let savedDraftPayload;
  await page.route("**/api/playbook/draft", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    savedDraftPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        active: { playbook: savedDraftPayload.playbook, metadata: { active_version_id: "pbv_active", active_hash: "active11a" } },
        draft: {
          playbook: savedDraftPayload.playbook,
          metadata: { draft_id: "drf_8", draft_hash: "draft888a", draft_updated_at: "2026-05-31T20:00:00+00:00" },
          has_unpublished_changes: true,
        },
        history: [],
        saved_draft_at: "2026-05-31T20:00:00+00:00",
      }),
    });
  });
  await page.getByRole("button", { name: "Save Draft" }).click();
  await page.waitForFunction(() => document.querySelector("#playbookSaveStatus")?.textContent.includes("Draft saved."));
  await page.waitForFunction(() => document.querySelector("#playbookDraftDiff")?.textContent.includes("No unsaved changes."));
  // Saved draft is now ahead of the active version and the draft hash label shows.
  await assertTextContains(page.locator(".playbook-version-card.draft"), "draft888");
  await assertTextContains(page.locator(".playbook-version-card.draft"), "ahead of the active version");
  assert.equal(savedDraftPayload.playbook.clauses[0].check_trigger, "One-way obligations need Check review.");
  const savedConfidentialInfo = savedDraftPayload.playbook.clauses.find((clause) => clause.id === "confidential_information");
  assert.equal(savedConfidentialInfo.standard_exclusions_template, "Publicly known information is excluded.");
  const savedTerm = savedDraftPayload.playbook.clauses.find((clause) => clause.id === "term_and_survival");
  assert.ok(savedTerm.longer_survival_carve_out_terms.includes("regulatory obligation"));
  const savedGoverningLaw = savedDraftPayload.playbook.clauses.find((clause) => clause.id === "governing_law");
  assert.ok(savedGoverningLaw.approved_laws.includes("UAE"));
  assert.equal(savedGoverningLaw.preferred_law, "UAE");
  assert.equal(savedGoverningLaw.law_phrases.UAE, "the UAE");
  assert.equal(Object.prototype.hasOwnProperty.call(savedGoverningLaw, "redline_template"), false);
  assert.deepEqual(
    savedGoverningLaw.rules.approved_options.map((option) => [option.value, option.default === true]),
    savedGoverningLaw.approved_laws.map((law) => [law, law === "UAE"]),
  );

  // --- Validate Draft: surfaces server validation errors, then a clean pass ---
  // Errors use the backend's {location, clause, field, message, severity} shape.
  let validateCount = 0;
  await page.route("**/api/playbook/validate-draft", async (route) => {
    validateCount += 1;
    const body = validateCount === 1
      ? { valid: false, errors: [{ location: "mutuality.check_trigger", clause: "mutuality", field: "check_trigger", message: "Check trigger is too vague.", severity: "error" }] }
      : { valid: true, errors: [] };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.getByRole("button", { name: "Validate Draft" }).click();
  await page.waitForFunction(() => document.querySelector("#playbookValidation")?.getAttribute("data-state") === "invalid");
  await assertTextContains(page.locator("#playbookValidation"), "Check trigger is too vague.");
  await assertTextContains(page.locator("#playbookValidation"), "Mutuality");
  // A failed validation blocks Publish.
  assert.equal(await page.getByRole("button", { name: "Publish Playbook" }).isEnabled(), false);
  await page.getByRole("button", { name: "Validate Draft" }).click();
  await page.waitForFunction(() => document.querySelector("#playbookValidation")?.getAttribute("data-state") === "valid");
  await assertTextContains(page.locator("#playbookValidation"), "Draft passed validation.");
  // Clean validation + saved draft ahead of active → Publish is enabled.
  assert.equal(await page.getByRole("button", { name: "Publish Playbook" }).isEnabled(), true);

  // --- Publish: promotes the draft to the active published version ---
  // Publish returns the new active block and a null draft (the server draft is
  // consumed); the editor re-baselines the draft to the published active version.
  let publishedPayload;
  await page.route("**/api/playbook/publish", async (route) => {
    publishedPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        playbook: publishedPayload.playbook,
        active: {
          playbook: publishedPayload.playbook,
          metadata: { active_version_id: "pbv_8", active_hash: "draft888a", published_at: "2026-05-31T20:05:00+00:00" },
        },
        draft: null,
        history: [{
          id: "pbv_frontend_test",
          recorded_at: "2026-05-31T20:05:00+00:00",
          actor: "admin",
          action: "publish",
          summary: "Published changes to Mutuality.",
          changed_clause_ids: ["mutuality"],
        }],
        published_at: "2026-05-31T20:05:00+00:00",
      }),
    });
  });
  await page.getByRole("button", { name: "Publish Playbook" }).click();
  await page.waitForFunction(() => document.querySelector("#playbookSaveStatus")?.textContent.includes("Playbook published."));
  assert.equal(publishedPayload.playbook.clauses[0].check_trigger, "One-way obligations need Check review.");
  // Active now shows a human-readable "Published ..." headline (not the raw id)
  // and the short hash fingerprint; the raw id is preserved in the hover tooltip.
  await assertTextContains(page.locator(".playbook-version-card.active"), "Published");
  await assertTextContains(page.locator(".playbook-version-card.active"), "draft888");
  assert.equal(await page.locator(".playbook-version-card.active strong").innerText().then((t) => t.includes("pbv_8")), false);
  assert.equal(await page.locator(".playbook-version-card.active strong").getAttribute("title").then((t) => (t || "").includes("pbv_8")), true);
  await assertTextContains(page.locator(".playbook-version-card.draft"), "Matches the active published version.");
  // Publishing with no further changes is a no-op, so Publish disables again.
  assert.equal(await page.getByRole("button", { name: "Publish Playbook" }).isEnabled(), false);
  await page.getByRole("button", { name: "Audit" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "Published changes to Mutuality.");
  await page.getByRole("tab", { name: "Admin" }).click();
  assert.equal(await page.locator("#clausesView").getAttribute("data-admin-surface"), "admin");
  await page.locator('[data-admin-section="email"]').click();
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Gmail");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "INBOUND ACCOUNT");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "inbound@example.com");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "OUTBOUND ACCOUNT");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "outbound@example.com");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "CONNECTION SETUP");
  await waitForText(page, "#adminGmailSetupPanel", "inbound@example.com");
  await assertTextContains(page.locator("#adminGmailSetupPanel"), "inbound@example.com");
  await assertTextContains(page.locator("#adminGmailSetupPanel"), "outbound@example.com");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Local data: data/gmail/inbound-token.json");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Environment: NDA_GMAIL_OUTBOUND_TOKEN_PATH");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Ready for scheduled sync.");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Ready to send redlines.");
  assert.equal(await page.locator("#adminGmailEnabledToggle").getAttribute("aria-checked"), "true");
  assert.equal(await page.locator("#adminGmailInboundToggle").count(), 0);
  assert.equal(await page.locator("#adminGmailOutboundToggle").count(), 0);
  assert.equal(await page.locator('[data-gmail-frequency="manual"]').count(), 0);
  assert.equal(await page.locator('[data-gmail-frequency="10_minutes"]').getAttribute("aria-pressed"), "true");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "SYNC FREQUENCY");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Every 10 minutes.");
  await page.locator('[data-gmail-frequency="30_minutes"]').click();
  await page.waitForFunction(() => document.querySelector('[data-gmail-frequency="30_minutes"]')?.getAttribute("aria-pressed") === "true");
  assert.deepEqual(gmailSettingsPayloads[gmailSettingsPayloads.length - 1], { sync_frequency: "30_minutes" });
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Every 30 minutes.");
  await page.locator("#adminGmailEnabledToggle").click();
  await page.waitForFunction(() => document.querySelector("#adminGmailEnabledToggle")?.getAttribute("aria-checked") === "false");
  assert.deepEqual(gmailSettingsPayloads[gmailSettingsPayloads.length - 1], { inbound_enabled: false, outbound_enabled: false });
  assert.equal(await page.locator("#adminGmailSyncButton").count(), 0);
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "DEFAULT IMPORT QUERY");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "subject:NDA");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "confidentiality agreement");
  assert.equal(await page.getByRole("button", { name: "Sync Gmail" }).count(), 0);
  const serverSyncLabel = await page.evaluate(() => new Date("2026-05-31T12:34:00+00:00").toLocaleString(undefined, {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "short",
  }));
  await assertTextContains(page.locator("#adminIntegrationsPanel"), serverSyncLabel);
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "2 imported / 1 skipped");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "SYNC AUDIT");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "2 imported / 1 skipped / 1 duplicates / 2 stale duplicates removed / 0 review failures");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "RECENT OUTBOUND");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "counterparty@example.com");
  await page.unroute("**/api/playbook/draft");
  await page.unroute("**/api/playbook/validate-draft");
  await page.unroute("**/api/playbook/publish");
  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/gmail/settings");
  await page.unroute("**/api/matters");
}

async function testContractStructureReviewPanel(page) {
  const aiSettingsPayloads = [];
  const aiKeyPayloads = [];
  let aiEnabled = false;
  let aiKeyConfigured = false;
  let aiKeySource = "";
  let aiProvider = "openrouter";
  let aiModel = "x-ai/grok-4.3";
  let activeReviewEngine = "ai_first";
  let runtimeSource = "default";
  let settingsAudit = [];
  const aiSettingsResponse = () => ({
    ai_review: {
      version: 1,
      enabled: aiEnabled,
      stored_enabled: aiSettingsPayloads.length || aiKeyPayloads.length ? aiEnabled : null,
      environment_enabled: false,
      provider: aiProvider,
      model: aiModel,
      confidence_threshold: 0.75,
      api_key_configured: aiKeyConfigured,
      api_key_source: aiKeySource,
      target_clause_ids: ["mutuality", "confidential_information", "governing_law", "term_and_survival", "non_circumvention"],
    },
    ai_verifier: {
      version: 2,
      enabled: true,
      active_kind: aiKeyConfigured ? "ai" : "offline",
      model: "deepseek/deepseek-v4-pro",
      default_model: "deepseek/deepseek-v4-pro",
      api_key_configured: aiKeyConfigured,
      api_key_source: aiKeySource,
      fallback_reason: aiKeyConfigured ? "" : "missing_openrouter_api_key",
    },
    active_review_engine: {
      active_engine: activeReviewEngine,
      engine_source: runtimeSource,
      engine_source_key: runtimeSource === "runtime_settings" ? "review_runtime.active_review_engine" : "",
      stored_active_engine: runtimeSource === "runtime_settings" ? activeReviewEngine : null,
      environment_active_engine: "",
      supported_engines: ["ai_first"],
    },
    operational_warnings: [
      activeReviewEngine === "ai_first" && !aiKeyConfigured
        ? { code: "ai_first_without_key", message: "AI-first is active but no AI API key is configured." }
        : null,
      !aiKeyConfigured
        ? { code: "ai_verifier_offline_fallback", message: "AI verifier is enabled but is running the offline fallback. Configure an OpenRouter key for DeepSeek verification." }
        : null,
    ].filter(Boolean),
    settings_audit: settingsAudit,
  });
  await page.route("**/api/ai/settings", async (route) => {
    if (route.request().method() === "POST") {
      const payload = route.request().postDataJSON();
      aiSettingsPayloads.push(payload);
      if (Object.prototype.hasOwnProperty.call(payload, "enabled")) {
        aiEnabled = payload.enabled === true;
      }
      if (payload.active_review_engine) {
        activeReviewEngine = payload.active_review_engine;
        runtimeSource = "runtime_settings";
      }
      if (payload.active_review_engine) {
        settingsAudit = [{
          recorded_at: "2026-06-04T10:00:00+00:00",
          actor: "admin",
          action: "admin_settings_update",
          changes: [
            payload.active_review_engine ? { setting: "review_runtime.active_review_engine", before: "", after: payload.active_review_engine } : null,
          ].filter(Boolean),
        }, ...settingsAudit];
      }
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(aiSettingsResponse()),
    });
  });
  await page.route("**/api/ai/api-key", async (route) => {
    if (route.request().method() === "POST") {
      const payload = route.request().postDataJSON();
      aiKeyPayloads.push(payload);
      aiEnabled = payload.enabled !== false;
      aiKeyConfigured = true;
      aiKeySource = "local_settings";
      settingsAudit = [{
        recorded_at: "2026-06-04T09:59:00+00:00",
        actor: "admin",
        action: "ai_api_key_saved",
        changes: [{ setting: "ai_review.api_key", before: "", after: "saved" }],
      }, ...settingsAudit];
    } else if (route.request().method() === "DELETE") {
      aiKeyConfigured = false;
      aiKeySource = "";
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(aiSettingsResponse()),
    });
  });
  const structureNda = [
    "MUTUAL NON-DISCLOSURE AGREEMENT",
    "Clause 1: Definitions",
    "Confidential Information means non-public business information.",
    "Clause 1A Supplemental Definitions",
    "Supplemental definition text.",
    "Clause 2 - Confidentiality",
    "Each party shall protect the other party's Confidential Information.",
    "10. General",
    "This section introduces general terms.",
    "10.1 Return of Materials",
    "The Receiving Party must return materials on request.",
    "10.1A Certificate of Destruction",
    "The Receiving Party must certify destruction.",
    "Section 10b Data Processing",
    "Data processing terms.",
    "Article II Confidentiality Schedule",
    "The parties must follow the confidentiality schedule.",
    "Section II.A Permitted Disclosures",
    "Permitted disclosures are limited to representatives.",
    "Clause IV - Term",
    "The obligations survive for three years.",
    "Clauses 1, 1A, 2 and IV survive this Agreement. Section II.A also survives.",
  ].join("\n\n");

  await runReview(page, structureNda);
  await page.evaluate(() => {
    delete state.latestReviewResult.contract_structure;
    delete state.latestReviewResult.reference_resolver;
  });
  await page.locator('[data-review-inspector="structure"]').click();
  await page.waitForSelector("#studioDetailPanel .structure-row");

  const reviewPanel = page.locator("#studioDetailPanel");
  await assertTextContains(reviewPanel, "Clause 1");
  await assertTextContains(reviewPanel, "Clause 1A");
  await assertTextContains(reviewPanel, "Clause 2");
  await assertTextContains(reviewPanel, "10.1");
  await assertTextContains(reviewPanel, "10.1A");
  await assertTextContains(reviewPanel, "Section 10b");
  await assertTextContains(reviewPanel, "Article II");
  await assertTextContains(reviewPanel, "Section II.A");
  await assertTextContains(reviewPanel, "Clause IV");
  await assertTextContains(reviewPanel, "Parent section-2");
  await assertTextContains(reviewPanel, "Parent section-6");
  await assertTextContains(reviewPanel, "Parent section-9");
  // The Resolver-aliases panel was removed from the Structure tab (internal debug
  // noise, not user-relevant); the section map + resolved references stay.
  await assertTextContains(reviewPanel, "RESOLVED REFERENCES");
  await assertTextContains(reviewPanel, "Clauses 1, 1A, 2 and IV");
  await assertTextContains(reviewPanel, "Clause 1, Clause 1A, Clause 2, Clause IV");
  await assertTextContains(reviewPanel, "Section II.A");
  const referenceIndex = await page.evaluate(() => state.latestReviewResult.contract_structure.reference_index);
  assert.equal(referenceIndex.version, 2);
  assert.equal(referenceIndex.alias_to_section_id["clause:1a"], "section-3");
  assert.equal(referenceIndex.alias_to_section_id["section:10b"], "section-8");
  assert.equal(referenceIndex.alias_to_section_id["section:ii.a"], "section-10");
  assert.equal(referenceIndex.paragraph_to_section_id.p14, "section-8");
  assert.equal(referenceIndex.paragraph_to_section_id.p18, "section-10");
  assert.equal(referenceIndex.sections_by_id["section-10"].parent_id, "section-9");
  assert.deepEqual(
    Object.keys(referenceIndex.sections_by_id["section-10"]).sort(),
    ["end_index", "heading", "id", "kind", "label", "level", "number", "paragraph_ids", "parent_id", "start_index"]
  );
  const referenceResolver = await page.evaluate(() => state.latestReviewResult.reference_resolver);
  assert.equal(referenceResolver.version, 1);
  assert.equal(referenceResolver.stats.reference_count, 2);
  assert.equal(referenceResolver.references[0].reference_text, "Clauses 1, 1A, 2 and IV");
  assert.deepEqual(referenceResolver.references[0].resolved_section_ids, ["section-2", "section-3", "section-4", "section-11"]);
  assert.equal(referenceResolver.references[1].reference_text, "Section II.A");
  assert.deepEqual(referenceResolver.references[1].resolved_section_ids, ["section-10"]);

  await page.locator('[data-review-inspector="clause"]').click();
  await assertTextContains(page.locator("#studioDetailPanel"), "PLAYBOOK POSITION");
  await assertTextContains(page.locator("#studioDetailPanel"), "RULE PURPOSE");

  await page.getByRole("tab", { name: "Guide" }).click();
  assert.equal(await page.locator("#clausesView").getAttribute("data-admin-surface"), "guide");
  await page.locator('[data-admin-section="document"]').click();
  await page.waitForSelector("#adminDocumentPanel .engine-card");

  const documentPanel = page.locator("#adminDocumentPanel");
  await assertTextContains(documentPanel, "Structure, references, and concepts");
  await assertTextContains(documentPanel, "Ingest");
  await assertTextContains(documentPanel, "STRUCTURE MAPPING");
  await assertTextContains(documentPanel, "REFERENCE RESOLVER");
  await assertTextContains(documentPanel, "CONCEPT CLASSIFIER");
  await assertTextContains(documentPanel, "DOCX STRUCTURE EXTRACTION");
  await assertTextContains(documentPanel, "nda_automation/contract_structure.py");
  await assertTextContains(documentPanel, "nda_automation/docx_text.py");
  await assertTextContains(documentPanel, "nda_automation/reference_resolver.py");
  await assertTextContains(documentPanel, "nda_automation/concept_classifier.py");
  await assertTextContains(documentPanel, "Evidence provenance validation");
  await assertTextContains(page.locator("#adminReferencePanel"), "Cross-reference resolution");
  await assertTextContains(page.locator("#adminReferencePanel"), "How explicit cross-references are resolved");
  await assertTextContains(page.locator("#adminReferencePanel"), "Supported references");
  await assertTextContains(page.locator("#adminReferencePanel"), "NO FIXED NUMBERING ASSUMPTION");
  await assertTextContains(page.locator("#adminReferencePanel"), "Term and Survival");
  await assertTextContains(page.locator("#adminConceptsPanel"), "Deterministic concept tagging");
  await assertTextContains(page.locator("#adminConceptsPanel"), "How deterministic concepts are tagged");
  await assertTextContains(page.locator("#adminConceptsPanel"), "Concepts");
  await assertTextContains(page.locator("#adminConceptsPanel"), "concept_classifier");

  await page.locator('[data-admin-section="checkers"]').click();
  const checkersPanel = page.locator("#adminCheckersPanel");
  await assertTextContains(checkersPanel, "Clause decision logic");
  await assertTextContains(checkersPanel, "How clause decisions become pass, review, or check");
  await assertTextContains(checkersPanel, "DECISION READING ORDER");
  await assertTextContains(checkersPanel, "REASON-CODE TAXONOMY");
  await assertTextContains(checkersPanel, "HARDENING GUARDS");
  await assertTextContains(checkersPanel, "PAYLOAD FIELDS");
  await assertTextContains(checkersPanel, "semantic_confidence_below_threshold");
  await assertTextContains(checkersPanel, "Claims survive for three years");
  await assertTextContains(checkersPanel, "PURPOSE");
  await assertTextContains(checkersPanel, "INPUTS");
  await assertTextContains(checkersPanel, "PASS");
  await assertTextContains(checkersPanel, "CHECK");
  await assertTextContains(checkersPanel, "REDLINE BEHAVIOR");
  await assertTextContains(checkersPanel, "HUMAN-REVIEW BOUNDARY");
  await assertTextContains(checkersPanel, "REVIEW STATE");
  await assertTextContains(checkersPanel, "REASON CODES");
  await assertTextContains(checkersPanel, "STRUCTURED EVIDENCE");
  await assertTextContains(checkersPanel, "AUDIT TRACE");
  await assertTextContains(checkersPanel, "mutuality_analysis");
  await assertTextContains(checkersPanel, "confidential_information_analysis");
  await assertTextContains(checkersPanel, "governing_law_analysis");
  await assertTextContains(checkersPanel, "term_survival_analysis");
  await assertTextContains(checkersPanel, "non_circumvention_analysis");
  await assertTextContains(checkersPanel, "SIGNATURES");
  await assertTextContains(checkersPanel, "separate from the legal-concept review-state upgrades");

  await page.locator('[data-admin-section="ai_guide"]').click();
  const aiGuidePanel = page.locator("#adminAiGuidePanel");
  await assertTextContains(aiGuidePanel, "AI review methodology");
  await assertTextContains(aiGuidePanel, "How AI-first review works");
  await assertTextContains(aiGuidePanel, "OPENROUTER_API_KEY");
  await assertTextContains(aiGuidePanel, "ai_first_assessment");
  await assertTextContains(aiGuidePanel, "fail closed");

  await page.getByRole("tab", { name: "Admin" }).click();
  assert.equal(await page.locator("#clausesView").getAttribute("data-admin-surface"), "admin");
  const aiPanel = page.locator("#adminAiPanel");
  await assertTextContains(aiPanel, "AI runtime");
  await assertTextContains(aiPanel, "AI Runtime");
  await page.waitForFunction(() => document.querySelector("#adminAiEnabledToggle")?.getAttribute("aria-checked") === "false");
  assert.equal(await page.locator('[data-admin-ai="enabled-copy"]').innerText(), "Off");
  assert.equal(await page.locator('[data-admin-ai="api-key"]').innerText(), "Missing AI API key");
  assert.equal(await page.locator('[data-admin-ai="verifier-kind"]').innerText(), "Offline fallback");
  assert.equal(await page.locator('[data-admin-ai="verifier-key"]').innerText(), "Missing OpenRouter key");
  await page.locator("#adminAiApiKeyInput").fill("browser-gemini-local-key");
  await page.locator("#adminAiSaveKeyButton").click();
  await page.waitForFunction(() => document.querySelector("#adminAiEnabledToggle")?.getAttribute("aria-checked") === "true");
  assert.deepEqual(aiKeyPayloads[aiKeyPayloads.length - 1], { api_key: "browser-gemini-local-key", enabled: true });
  assert.equal(await page.locator("#adminAiApiKeyInput").inputValue(), "");
  assert.equal(await page.locator('[data-admin-ai="enabled-copy"]').innerText(), "On");
  assert.equal(await page.locator('[data-admin-ai="provider"]').innerText(), "openrouter");
  assert.equal(await page.locator('[data-admin-ai="model"]').innerText(), "x-ai/grok-4.3");
  assert.equal(await page.locator('[data-admin-ai="api-key"]').innerText(), "Configured from saved local OpenRouter key");
  assert.equal(await page.locator('[data-admin-ai="verifier-kind"]').innerText(), "AI via OpenRouter");
  assert.equal(await page.locator('[data-admin-ai="verifier-model"]').innerText(), "deepseek/deepseek-v4-pro");
  assert.equal(await page.locator('[data-admin-ai="verifier-key"]').innerText(), "Configured from saved local OpenRouter key");
  assert.equal(await page.locator('[data-admin-ai="source"]').innerText(), "Admin toggle");
  assert.equal(await page.locator('[data-admin-ai="active-engine"]').innerText(), "AI-first");
  assert.equal(await page.locator('[data-admin-ai="runtime-source"]').innerText(), "Default runtime");
  assert.equal(await page.locator('[data-admin-ai="operational-warnings"]').innerText(), "None");
  assert.equal(await page.locator("#adminAiOverall").innerText(), "ON");
  await page.locator("#adminActiveReviewEngineSelect").selectOption("ai_first");
  await page.locator("#adminRuntimeSaveButton").click();
  await page.waitForFunction(() => document.querySelector('[data-admin-ai="runtime-source"]')?.textContent?.trim() === "Admin runtime settings");
  assert.deepEqual(aiSettingsPayloads[aiSettingsPayloads.length - 1], {
    active_review_engine: "ai_first",
  });
  assert.equal(await page.locator('[data-admin-ai="runtime-source"]').innerText(), "Admin runtime settings");
  assert.equal(await page.locator('[data-admin-ai="last-settings-change"]').innerText(), "admin_settings_update: review_runtime.active_review_engine");
  await page.locator("#adminAiClearKeyButton").click();
  await page.waitForFunction(() => document.querySelector('[data-admin-ai="api-key"]')?.textContent?.trim() === "Missing AI API key");
  assert.equal(await page.locator("#adminAiOverall").innerText(), "NEEDS KEY");
  await page.locator("#adminAiEnabledToggle").click();
  await page.waitForFunction(() => document.querySelector("#adminAiEnabledToggle")?.getAttribute("aria-checked") === "false");
  assert.deepEqual(aiSettingsPayloads[aiSettingsPayloads.length - 1], { enabled: false });
  assert.equal(await page.locator('[data-admin-ai="enabled-copy"]').innerText(), "Off");
  assert.equal(await page.locator('[data-admin-ai="source"]').innerText(), "Admin toggle");
  await page.unroute("**/api/ai/settings");
  await page.unroute("**/api/ai/api-key");
}

async function testProgressivePdfPreviewFallback(page) {
  const renderText = "Rendered PDF fallback paragraph.";
  const reviewResult = {
    checked_at: "2026-06-05T09:00:00+01:00",
    clauses: [{
      decision: "pass",
      id: "mutuality",
      issue_label: "Pass",
      matched_paragraph_ids: ["p1"],
      name: "Mutuality",
      passes: true,
      review_state: { state: "pass" },
      status: "pass",
    }],
    document_render: {
      page_count: 2,
      pdf_url: "/api/rendered-documents/rendered-preview.pdf",
      source_label: "Converted DOCX",
      status: "ready",
    },
    overall_status: "meets_requirements",
    paragraphs: [{ id: "p1", index: 1, source_index: 1, text: renderText }],
    redline_edits: [],
    requirements_failed: 0,
    requirements_needs_review: 0,
    requirements_passed: 1,
  };

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  await page.evaluate((payload) => {
    renderResult(payload, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);

  await page.waitForSelector('[data-review-pdf-surface][data-render-status="ready"]');
  assert.equal(await page.locator(".review-pdf-frame").getAttribute("src"), "/api/rendered-documents/rendered-preview.pdf");
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "High-resolution preview");
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "2 pages");
  await assertTextContains(page.locator("#studioDocumentRender"), renderText);

  const readyState = await page.evaluate(() => state.reviewDocumentRender);
  assert.deepEqual(readyState, {
    error: "",
    pageCount: 2,
    pdfUrl: "/api/rendered-documents/rendered-preview.pdf",
    sourceLabel: "Converted DOCX",
    status: "ready",
  });

  await page.evaluate((payload) => {
    renderResult({
      ...payload,
      document_render: {
        error: "Conversion service is not available.",
        status: "failed",
      },
    }, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);

  await page.waitForSelector('[data-review-pdf-surface][data-render-status="error"]');
  assert.equal(await page.locator(".review-pdf-frame").count(), 0);
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "Conversion service is not available.");
  await assertTextContains(page.locator("#studioDocumentRender"), renderText);

  await page.evaluate((payload) => {
    state.selectedMatter = {
      id: "matter_pdf_source",
      source_filename: "Source NDA.pdf",
    };
    renderResult({
      ...payload,
      document_render: null,
    }, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);

  await page.waitForSelector('[data-review-pdf-surface][data-render-status="ready"]');
  assert.equal(await page.locator(".review-pdf-frame").getAttribute("src"), "/api/matters/matter_pdf_source/source");
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "Original PDF");
}

async function testRenderedPageImagePreview(page) {
  const renderText = "Rendered page fallback paragraph.";
  const matterId = "rendered_pages";
  const pagePng = testPngBuffer(6, 8);
  await page.route(`**/api/matters/${matterId}/render-page/*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "image/png",
      body: pagePng,
    });
  });

  const reviewResult = {
    checked_at: "2026-06-05T09:15:00+01:00",
    clauses: [{
      decision: "pass",
      id: "mutuality",
      issue_label: "Pass",
      matched_paragraph_ids: ["p1"],
      name: "Mutuality",
      passes: true,
      review_state: { state: "pass" },
      status: "pass",
    }],
    document_render: {
      document_overlay: {
        anchors: [{
          boxes: [],
          clause_id: "mutuality",
          confidence: 0.6,
          page_number: 1,
          paragraph_id: "p1",
          target_type: "evidence",
        }],
        fallback_mode: "text_dom_scroll",
        precision: "page",
        status: "partial",
        version: 1,
      },
      error: "",
      error_code: "",
      pages: [
        {
          dpi: 180,
          height: 2200,
          image_url: `/api/matters/${matterId}/render-page/1`,
          page_number: 1,
          width: 1700,
        },
        {
          dpi: 180,
          height: 2200,
          image_url: `/api/matters/${matterId}/render-page/2`,
          page_number: 2,
          width: 1700,
        },
      ],
      pdf_url: `/api/matters/${matterId}/render-pdf`,
      source_label: "Converted DOCX",
      status: "ready",
    },
    overall_status: "meets_requirements",
    paragraphs: [{ id: "p1", index: 1, source_index: 1, text: renderText }],
    redline_edits: [],
    requirements_failed: 0,
    requirements_needs_review: 0,
    requirements_passed: 1,
  };

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  await page.evaluate((payload) => {
    renderResult(payload, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);

  await page.waitForSelector('[data-review-render-page="1"] img');
  assert.equal(await page.locator(".review-render-page img").count(), 2);
  assert.equal(await page.locator(".review-pdf-frame").count(), 0);
  assert.equal(
    await page.locator('[data-review-render-page="1"] img').getAttribute("src"),
    `/api/matters/${matterId}/render-page/1`,
  );
  await page.waitForFunction(() => Array.from(document.querySelectorAll(".review-render-page img"))
    .every((image) => image.complete && image.naturalWidth > 0 && image.naturalHeight > 0));
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "Converted DOCX");
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "2 pages");
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "Page image preview");
  await assertTextContains(page.locator('[data-review-render-page="1"]'), "Selected clause evidence");
  assert.equal(await page.locator('[data-review-render-page="1"]').getAttribute("data-overlay-clause-ids"), "mutuality");
  await assertTextContains(page.locator("#studioDocumentRender"), renderText);

  const readyState = await page.evaluate(() => state.reviewDocumentRender);
  assert.deepEqual(readyState, {
    documentOverlay: {
      anchors: [{
        boxes: [],
        clauseId: "mutuality",
        confidence: 0.6,
        pageNumber: 1,
        paragraphId: "p1",
        targetType: "evidence",
      }],
      fallbackMode: "text_dom_scroll",
      precision: "page",
      status: "partial",
      version: 1,
    },
    error: "",
    pageCount: 2,
    pages: [
      {
        dpi: 180,
        height: 2200,
        imageUrl: `/api/matters/${matterId}/render-page/1`,
        pageNumber: 1,
        width: 1700,
      },
      {
        dpi: 180,
        height: 2200,
        imageUrl: `/api/matters/${matterId}/render-page/2`,
        pageNumber: 2,
        width: 1700,
      },
    ],
    pdfUrl: `/api/matters/${matterId}/render-pdf`,
    sourceLabel: "Converted DOCX",
    status: "ready",
  });

  await page.unroute(`**/api/matters/${matterId}/render-page/*`);
}

async function testRenderStatusPageImagePreviewFetch(page) {
  const renderText = "Render job preview paragraph.";
  const matterId = "render_status_pages";
  const pagePng = testPngBuffer(6, 8);
  let renderStatusRequested = false;

  await page.route(`**/api/matters/${matterId}/render-status`, async (route) => {
    renderStatusRequested = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        document_render: {
          cached: true,
          cache_key: "render-status-cache-key",
          document_overlay: {
            anchors: [{
              boxes: [],
              clause_id: "term",
              confidence: 0.6,
              confidence_reason: "Page-level paragraph fallback.",
              fallback: {
                mode: "text_dom_scroll",
                selector: '[data-paragraph-id="p1"]',
              },
              page_number: 1,
              paragraph_id: "p1",
              target_type: "evidence",
            }],
            fallback_mode: "text_dom_scroll",
            pages: [{
              dpi: 180,
              height: 2200,
              image_url: `/api/matters/${matterId}/render-page/1`,
              page_number: 1,
              scale: 2,
              width: 1700,
            }],
            precision: "page",
            status: "partial",
            version: 1,
            warnings: [],
          },
          page_image_status: "ready",
          page_images: {
            cached: true,
            dpi: 180,
            pages: [{
              dpi: 180,
              height: 2200,
              image_url: `/api/matters/${matterId}/render-page/1`,
              page_number: 1,
              scale: 2,
              width: 1700,
            }],
            scale: 2,
            status: "ready",
          },
          pages: [{
            dpi: 180,
            height: 2200,
            image_url: `/api/matters/${matterId}/render-page/1`,
            page_number: 1,
            scale: 2,
            width: 1700,
          }],
          pdf_url: `/api/matters/${matterId}/render-pdf`,
          source_kind: "docx",
          source_label: "Converted DOCX",
          status: "ready",
        },
      }),
    });
  });
  await page.route(`**/api/matters/${matterId}/render-page/*`, async (route) => {
    await route.fulfill({ status: 200, contentType: "image/png", body: pagePng });
  });

  const reviewResult = {
    checked_at: "2026-06-10T09:15:00+01:00",
    clauses: [{
      decision: "pass",
      id: "term",
      issue_label: "Pass",
      matched_paragraph_ids: ["p1"],
      name: "Term",
      passes: true,
      review_state: { state: "pass" },
      status: "pass",
    }],
    document_render: null,
    overall_status: "meets_requirements",
    paragraphs: [{ id: "p1", index: 1, source_index: 1, text: renderText }],
    redline_edits: [],
    requirements_failed: 0,
    requirements_needs_review: 0,
    requirements_passed: 1,
  };

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  await page.evaluate((payload) => {
    state.selectedMatter = {
      document_title: "Rendered status NDA",
      id: payload.matterId,
      source_filename: "render-status-source.docx",
      source_type: "repository",
    };
    renderResult(payload.reviewResult, payload.reviewResult.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, { matterId, reviewResult });

  await page.waitForSelector('[data-review-render-page="1"] img');
  assert.equal(renderStatusRequested, true);
  assert.equal(
    await page.locator('[data-review-render-page="1"] img').getAttribute("src"),
    `/api/matters/${matterId}/render-page/1`,
  );
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "Converted DOCX");
  await assertTextContains(page.locator("[data-review-pdf-surface]"), "1 page");
  await assertTextContains(page.locator('[data-review-render-page="1"]'), "Selected clause evidence");
  assert.equal(await page.locator('[data-review-render-page="1"]').getAttribute("data-overlay-clause-ids"), "term");
  assert.equal(await page.locator('[data-review-render-page="1"]').getAttribute("data-overlay-paragraph-ids"), "p1");

  const readyState = await page.evaluate(() => state.reviewDocumentRender);
  assert.deepEqual(readyState, {
    documentOverlay: {
      anchors: [{
        boxes: [],
        clauseId: "term",
        confidence: 0.6,
        pageNumber: 1,
        paragraphId: "p1",
        targetType: "evidence",
      }],
      fallbackMode: "text_dom_scroll",
      precision: "page",
      status: "partial",
      version: 1,
    },
    error: "",
    pageCount: 1,
    pages: [{
      dpi: 180,
      height: 2200,
      imageUrl: `/api/matters/${matterId}/render-page/1`,
      pageNumber: 1,
      width: 1700,
    }],
    pdfUrl: `/api/matters/${matterId}/render-pdf`,
    sourceLabel: "Converted DOCX",
    status: "ready",
  });

  await page.unroute(`**/api/matters/${matterId}/render-status`);
  await page.unroute(`**/api/matters/${matterId}/render-page/*`);
}

async function testOriginalViewToggle(page) {
  const renderText = "Original toggle paragraph.";
  const matterId = "original_view";
  const pagePng = testPngBuffer(6, 8);
  await page.route(`**/api/matters/${matterId}/render-page/*`, async (route) => {
    await route.fulfill({ status: 200, contentType: "image/png", body: pagePng });
  });

  const reviewResult = {
    checked_at: "2026-06-07T09:00:00+01:00",
    clauses: [{
      decision: "pass",
      id: "mutuality",
      issue_label: "Pass",
      matched_paragraph_ids: ["p1"],
      name: "Mutuality",
      passes: true,
      review_state: { state: "pass" },
      status: "pass",
    }],
    document_render: {
      pages: [{
        dpi: 180,
        height: 2200,
        image_url: `/api/matters/${matterId}/render-page/1`,
        page_number: 1,
        width: 1700,
      }],
      pdf_url: `/api/matters/${matterId}/render-pdf`,
      source_label: "Converted DOCX",
      status: "ready",
    },
    overall_status: "meets_requirements",
    paragraphs: [{ id: "p1", index: 1, source_index: 1, text: renderText }],
    redline_edits: [],
    requirements_failed: 0,
    requirements_needs_review: 0,
    requirements_passed: 1,
  };

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  await page.evaluate((payload) => {
    renderResult(payload, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);
  await page.waitForSelector("#studioDocumentRender:not([hidden])");

  // Original view: the faithful page-image surface is the focus and the text
  // reconstruction is suppressed.
  await page.getByRole("button", { name: "Original", exact: true }).click();
  await page.waitForSelector('[data-original-surface][data-render-status="ready"]');
  assert.equal(await page.locator('[data-original-surface] .review-render-page img').count(), 1);
  assert.equal(await page.locator("#studioDocumentRender .studio-doc-paragraph").count(), 0);
  assert.equal(await page.locator("#studioDocumentRender [data-editable-paragraph-id]").count(), 0);
  assert.equal(await page.locator('[data-original-surface][aria-pressed]').count(), 0);
  assert.equal(await page.locator('[data-view-mode="original"]').getAttribute("aria-pressed"), "true");

  // Switching back to Redline restores the editable text view.
  await page.locator('.studio-view-switch [data-view-mode="redline"]').click();
  await page.waitForSelector('#studioDocumentRender [data-paragraph-id="p1"]');
  assert.equal(await page.locator('[data-original-surface]').count(), 0);
  await assertTextContains(page.locator("#studioDocumentRender"), renderText);

  // DOCX source-fidelity fallback: when exact page images are unavailable but
  // the backend exposes source blocks, Original shows the extracted source
  // layout rather than a dead-end unavailable panel.
  await page.evaluate((payload) => {
    renderResult({
      ...payload,
      document_render: null,
      source_fidelity: {
        version: 1,
        source_type: "docx",
        analysis_model: "paragraphs",
        render_model: "source_blocks",
        capabilities: {
          structured_tables: true,
          inline_runs: true,
          run_colors: true,
          pdf_page_references: false,
        },
        summary: {
          paragraph_count: 3,
          block_count: 2,
          table_count: 1,
          styled_run_count: 1,
          color_run_count: 1,
          styled_table_cell_count: 1,
          table_cell_background_count: 1,
          pdf_page_reference_count: 0,
        },
        limitations: [],
        blocks: [
          {
            id: "p1",
            index: 1,
            text: "Intro red text.",
            type: "paragraph",
            runs: [
              { text: "Intro " },
              { text: "red", color: "#ff0000" },
              { text: " text." },
            ],
          },
          {
            table_index: 1,
            type: "table",
            rows: [{
              cells: [
                {
                  paragraph_ids: ["p2"],
                  style: {
                    background_color: "#d9ead3",
                    width: { value: 2400, type: "dxa" },
                  },
                  blocks: [{ id: "p2", text: "Party", type: "paragraph" }],
                },
                { paragraph_ids: ["p3"], blocks: [{ id: "p3", text: "Signature", type: "paragraph", style: { style_name: "Table Text" } }] },
              ],
            }],
          },
        ],
      },
    }, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);
  await page.getByRole("button", { name: "Original", exact: true }).click();
  await page.waitForSelector("[data-source-fidelity-surface]");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "DOCX source layout preview");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "1 table");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "1 styled cell");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "Intro red text.");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "Signature");
  assert.equal(await page.locator(".source-fidelity-table td").count(), 2);
  const styledCell = page.locator(".source-fidelity-table td").first();
  assert.equal(await styledCell.getAttribute("data-source-fidelity-cell-background"), "#d9ead3");
  assert.equal(await styledCell.getAttribute("data-source-fidelity-cell-width"), "160px");
  const styledCellBackground = await styledCell.evaluate((cell) => getComputedStyle(cell).backgroundColor);
  assert.ok(/rgb\(217,\s*234,\s*211\)/.test(styledCellBackground), `expected styled table cell background, got ${styledCellBackground}`);
  const redRunColor = await page.locator("[data-source-fidelity-surface]").evaluate((surface) => {
    const spans = Array.from(surface.querySelectorAll("span"));
    const redRun = spans.find((span) => span.textContent === "red");
    return redRun ? getComputedStyle(redRun).color : "";
  });
  assert.ok(/rgb\(255,\s*0,\s*0\)/.test(redRunColor), `expected red source run, got ${redRunColor}`);
  await page.locator('.studio-view-switch [data-view-mode="redline"]').click();
  await page.waitForSelector('#studioDocumentRender [data-paragraph-id="p1"]');

  // PDF source blocks are an analysis fallback, not a reconstructed document
  // layout. The UI must point users back to the Original PDF/page preview for
  // visual fidelity instead of implying a DOCX-like preview.
  await page.evaluate((payload) => {
    renderResult({
      ...payload,
      document_render: null,
      source_fidelity: {
        version: 1,
        source_type: "pdf",
        analysis_model: "paragraphs",
        render_model: "source_blocks",
        capabilities: {
          structured_tables: false,
          inline_runs: false,
          run_colors: false,
          pdf_page_references: true,
          pdf_visual_profile: true,
          pdf_visual_elements: true,
        },
        summary: {
          paragraph_count: 1,
          block_count: 1,
          table_count: 0,
          styled_run_count: 0,
          color_run_count: 0,
          pdf_page_reference_count: 1,
        },
        preferred_render_mode: "source_pdf_preview",
        pdf_fidelity: {
          analysis_mode: "extracted_text_only",
          layout_mode: "original_pdf_page_preview",
          word_conversion: "unsupported_for_fidelity",
          redlined_docx: "unavailable",
          requires_source_preview: true,
          message: "PDF matters use extracted text for clause analysis and the preserved original PDF/page preview for visual fidelity. Extracted text must not be presented as a faithful Word conversion.",
          visual_profile: {
            status: "ready",
            requires_source_preview: true,
            non_black_text_span_count: 3,
            drawing_count: 2,
            image_count: 1,
            visual_features: ["colored_text", "drawings_or_borders", "images"],
          },
        },
        limitations: [],
        blocks: [{ id: "pdf-p1", index: 1, text: "Extracted PDF text only.", type: "paragraph" }],
      },
    }, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);
  await page.waitForSelector("[data-source-fidelity-surface]");
  assert.equal(await page.locator('[data-view-mode="original"]').getAttribute("aria-pressed"), "true");
  await page.waitForSelector("[data-source-fidelity-surface]");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "PDF source analysis preview");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "preserved original PDF/page preview for visual fidelity");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "3 non-black text spans");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "2 drawing or border items");
  await assertTextContains(page.locator("[data-source-fidelity-surface]"), "1 image item");
  await page.locator('.studio-view-switch [data-view-mode="redline"]').click();
  await page.waitForSelector('#studioDocumentRender [data-paragraph-id="p1"]');
  assert.equal(await page.locator('[data-view-mode="redline"]').getAttribute("aria-pressed"), "true");

  // Graceful fallback: no render available (DOCX without a document server).
  await page.evaluate((payload) => {
    state.selectedMatter = null;
    renderResult({ ...payload, document_render: null }, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);
  await page.getByRole("button", { name: "Original", exact: true }).click();
  await page.waitForSelector(".review-original-empty");
  await assertTextContains(page.locator(".review-original-empty"), "High-fidelity preview isn't available here");
  assert.equal(await page.locator("#studioDocumentRender .studio-doc-paragraph").count(), 0);

  // The fallback button returns to the structured view (and re-renders text).
  await page.locator(".review-original-fallback-button").click();
  await page.waitForSelector('#studioDocumentRender [data-paragraph-id="p1"]');
  assert.equal(await page.locator('[data-view-mode="redline"]').getAttribute("aria-pressed"), "true");
  await assertTextContains(page.locator("#studioDocumentRender"), renderText);

  await page.unroute(`**/api/matters/${matterId}/render-page/*`);
}

// Interactive PDF markup on the Original page-image view: existing annotations
// render on the right page, a new comment + highlight POST normalized rects, a
// comment delete fires DELETE and removes the pin, and Download hits
// /marked-up-pdf. All endpoints are mocked with page.route.
async function testPdfMarkupOriginalView(page) {
  const renderText = "Markup target paragraph.";
  const matterId = "markup_matter";
  const pagePng = testPngBuffer(6, 8);

  await page.route(`**/api/matters/${matterId}/render-page/*`, async (route) => {
    await route.fulfill({ status: 200, contentType: "image/png", body: pagePng });
  });

  // Server-stored annotations: one comment on page 1, one highlight on page 1.
  const storedAnnotations = [
    {
      id: "ann-comment-1",
      page: 1,
      type: "comment",
      rect: { x: 0.25, y: 0.3, w: 0, h: 0 },
      text: "Existing reviewer note",
      author: "Reviewer",
      created_at: "2026-06-07T10:00:00+01:00",
    },
    {
      id: "ann-highlight-1",
      page: 1,
      type: "highlight",
      rect: { x: 0.1, y: 0.6, w: 0.4, h: 0.08 },
      color: "rgba(250, 204, 21, 0.4)",
    },
  ];

  const postedBodies = [];
  const deletedIds = [];
  let markedUpPdfRequested = false;
  let createdSeq = 0;

  await page.route(`**/api/matters/${matterId}/pdf-annotations`, async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON();
      postedBodies.push(body);
      createdSeq += 1;
      const annotation = {
        id: `ann-new-${createdSeq}`,
        page: body.page,
        type: body.type,
        rect: body.rect,
        text: body.text || "",
        color: body.color || "",
        author: "Reviewer",
        created_at: "2026-06-07T11:00:00+01:00",
      };
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({ annotation }),
      });
      return;
    }
    // GET
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ annotations: storedAnnotations }),
    });
  });

  await page.route(`**/api/matters/${matterId}/pdf-annotations/*`, async (route) => {
    if (route.request().method() === "DELETE") {
      const url = route.request().url();
      deletedIds.push(url.split("/").pop());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true }),
      });
      return;
    }
    await route.fallback();
  });

  await page.route(`**/api/matters/${matterId}/marked-up-pdf`, async (route) => {
    markedUpPdfRequested = true;
    await route.fulfill({
      status: 200,
      contentType: "application/pdf",
      body: Buffer.from("%PDF-1.4 marked up"),
    });
  });

  const reviewResult = {
    checked_at: "2026-06-07T09:00:00+01:00",
    clauses: [{
      decision: "pass",
      id: "mutuality",
      issue_label: "Pass",
      matched_paragraph_ids: ["p1"],
      name: "Mutuality",
      passes: true,
      review_state: { state: "pass" },
      status: "pass",
    }],
    document_render: {
      pages: [{
        dpi: 180,
        height: 2200,
        image_url: `/api/matters/${matterId}/render-page/1`,
        page_number: 1,
        width: 1700,
      }],
      pdf_url: `/api/matters/${matterId}/render-pdf`,
      source_label: "Original PDF",
      status: "ready",
    },
    overall_status: "meets_requirements",
    paragraphs: [{ id: "p1", index: 1, source_index: 1, text: renderText }],
    redline_edits: [],
    requirements_failed: 0,
    requirements_needs_review: 0,
    requirements_passed: 1,
  };

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  await page.evaluate((payload) => {
    // A loaded PDF matter so the markup controller mounts (it gates on an id).
    state.selectedMatter = { id: payload.matterId, source_filename: "agreement.pdf" };
    renderResult(payload.reviewResult, payload.reviewResult.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, { matterId, reviewResult });
  await page.waitForSelector("#studioDocumentRender:not([hidden])");

  // Enter the Original view -> markup toolbar mounts and stored annotations load.
  await page.getByRole("button", { name: "Original", exact: true }).click();
  await page.waitForSelector('[data-original-surface][data-render-status="ready"]');
  await page.waitForSelector("[data-pdf-markup-toolbar]");

  // Both existing overlays render on page 1 at sensible positions.
  await page.waitForSelector('[data-annotation-id="ann-comment-1"]');
  await page.waitForSelector('[data-annotation-id="ann-highlight-1"]');
  let pageImageBox = await page.locator('[data-review-render-page="1"] .review-render-page-image').boundingBox();
  const pinBox = await page.locator('[data-annotation-id="ann-comment-1"]').boundingBox();
  // The comment pin sits near x=0.25,y=0.3 of the page image (pin is anchored at
  // its bottom-left, so compare against the page-relative point).
  const pinRelX = (pinBox.x + pinBox.width / 2 - pageImageBox.x) / pageImageBox.width;
  assert.ok(pinRelX > 0.15 && pinRelX < 0.35, `pin x ${pinRelX} should track 0.25`);
  const highlightBox = await page.locator('[data-annotation-id="ann-highlight-1"]').boundingBox();
  const highlightRelX = (highlightBox.x - pageImageBox.x) / pageImageBox.width;
  assert.ok(highlightRelX > 0.04 && highlightRelX < 0.18, `highlight x ${highlightRelX} should track 0.1`);

  // --- Add a comment: select the Comment tool, click the page, type, confirm.
  await page.locator('[data-pdf-markup-tool="comment"]').click();
  assert.equal(await page.locator('[data-pdf-markup-tool="comment"]').getAttribute("aria-pressed"), "true");
  const commentClickX = pageImageBox.x + pageImageBox.width * 0.5;
  const commentClickY = pageImageBox.y + pageImageBox.height * 0.4;
  await page.mouse.click(commentClickX, commentClickY);
  await page.waitForSelector("[data-pdf-markup-composer]");
  await page.locator("[data-pdf-markup-comment-input]").fill("Fresh comment from test");
  await page.locator("[data-pdf-markup-comment-confirm]").click();
  await page.waitForSelector('[data-annotation-id="ann-new-1"]');
  pageImageBox = await page.locator('[data-review-render-page="1"] .review-render-page-image').boundingBox();

  const commentPost = postedBodies.find((body) => body.type === "comment");
  assert.ok(commentPost, "a comment was POSTed");
  assert.equal(commentPost.type, "comment");
  assert.equal(commentPost.text, "Fresh comment from test");
  assert.equal(commentPost.page, 1);
  assert.ok(commentPost.rect.x >= 0 && commentPost.rect.x <= 1, "comment rect.x normalized");
  assert.ok(commentPost.rect.y >= 0 && commentPost.rect.y <= 1, "comment rect.y normalized");
  assert.ok(Math.abs(commentPost.rect.w) < 1e-6 && Math.abs(commentPost.rect.h) < 1e-6, "comment is a point");
  assert.ok(Math.abs(commentPost.rect.x - 0.5) < 0.1, `comment rect.x ${commentPost.rect.x} tracks the click`);

  // --- Add a highlight: select the Highlight tool, press-drag a box.
  await page.locator('[data-pdf-markup-tool="highlight"]').click();
  assert.equal(await page.locator('[data-pdf-markup-tool="highlight"]').getAttribute("aria-pressed"), "true");
  pageImageBox = await page.locator('[data-review-render-page="1"] .review-render-page-image').boundingBox();
  const highlightStartX = 0.55;
  const highlightStartY = 0.45;
  const dragStartX = pageImageBox.x + pageImageBox.width * highlightStartX;
  const dragStartY = pageImageBox.y + pageImageBox.height * highlightStartY;
  const dragEndX = pageImageBox.x + pageImageBox.width * 0.85;
  const dragEndY = pageImageBox.y + pageImageBox.height * 0.55;
  await page.mouse.move(dragStartX, dragStartY);
  await page.mouse.down();
  await page.mouse.move((dragStartX + dragEndX) / 2, (dragStartY + dragEndY) / 2);
  await page.mouse.move(dragEndX, dragEndY);
  await page.mouse.up();
  await page.waitForSelector('[data-annotation-id="ann-new-2"]');

  const highlightPost = postedBodies.find((body) => body.type === "highlight");
  assert.ok(highlightPost, "a highlight was POSTed");
  assert.equal(highlightPost.type, "highlight");
  assert.equal(highlightPost.page, 1);
  assert.ok(highlightPost.rect.w > 0 && highlightPost.rect.h > 0, "highlight has a non-zero box");
  assert.ok(highlightPost.rect.w <= 1 && highlightPost.rect.h <= 1, "highlight box normalized");
  assert.ok(Math.abs(highlightPost.rect.x - highlightStartX) < 0.1, `highlight rect.x ${highlightPost.rect.x} tracks the drag start`);

  // --- Delete the existing comment via its popover.
  await page.locator('[data-annotation-id="ann-comment-1"]').click();
  await page.waitForSelector('[data-pdf-markup-popover="ann-comment-1"]');
  await assertTextContains(page.locator('[data-pdf-markup-popover="ann-comment-1"]'), "Existing reviewer note");
  await page.locator("[data-pdf-markup-popover-delete]").click();
  await page.waitForSelector('[data-annotation-id="ann-comment-1"]', { state: "detached" });
  assert.deepEqual(deletedIds, ["ann-comment-1"]);

  // --- Download the marked-up PDF.
  const downloadPromise = page.waitForEvent("download");
  await page.locator("[data-pdf-markup-download]").click();
  const download = await downloadPromise;
  assert.ok(markedUpPdfRequested, "the marked-up PDF endpoint was hit");
  assert.match(await download.suggestedFilename(), /marked-up\.pdf$/);

  // --- Leaving the Original view tears down the toolbar/overlays.
  await page.locator('.studio-view-switch [data-view-mode="redline"]').click();
  await page.waitForSelector("[data-pdf-markup-toolbar]", { state: "detached" });
  assert.equal(await page.locator("[data-annotation-id]").count(), 0);

  await page.unroute(`**/api/matters/${matterId}/render-page/*`);
  await page.unroute(`**/api/matters/${matterId}/pdf-annotations`);
  await page.unroute(`**/api/matters/${matterId}/pdf-annotations/*`);
  await page.unroute(`**/api/matters/${matterId}/marked-up-pdf`);
}

async function testRichDocumentStructureRendering(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();

  // Paragraphs carrying the extractor's additive structure metadata: a heading
  // with run-level bold/italic/underline, a numbered list item, and a table cell.
  const headingText = "Confidential Information Bold italic underlined";
  const reviewResult = {
    checked_at: "2026-06-07T10:00:00+01:00",
    clauses: [{
      decision: "fail",
      evidence_paragraphs: [{ id: "p1", index: 1, source_index: 1, text: headingText }],
      id: "confidential_information",
      issue_label: "Present but wrong",
      matched_paragraph_ids: ["p1"],
      name: "Confidential Information",
      needs_review: false,
      passes: false,
      reason: "The definition is too narrow.",
      review_state: { requires_redline: true, state: "check" },
      status: "check",
    }],
    overall_status: "needs_redline",
    paragraphs: [
      {
        id: "p1",
        index: 1,
        source_index: 1,
        text: headingText,
        heading_level: 1,
        style_name: "heading 1",
        runs: [
          { text: "Confidential Information ", bold: false, italic: false, underline: false },
          { text: "Bold", bold: true, italic: false, underline: false },
          { text: " ", bold: false, italic: false, underline: false },
          { text: "italic", bold: false, italic: true, underline: false },
          { text: " ", bold: false, italic: false, underline: false },
          { text: "underlined", bold: false, italic: false, underline: true },
        ],
      },
      {
        id: "p2",
        index: 2,
        source_index: 2,
        text: "First numbered obligation.",
        numbering: { num_id: "1", level: 0, label: "1." },
        structure_label: "1.",
      },
      {
        id: "p3",
        index: 3,
        source_index: 3,
        text: "Signature table cell text.",
        table: { table_index: 1, row_index: 1, cell_index: 1 },
      },
    ],
    redline_edits: [{
      action: "replace_paragraph",
      action_label: "Replace paragraph",
      clause_id: "confidential_information",
      id: "rich-redline-confidential-information",
      original_text: headingText,
      paragraph_id: "p1",
      paragraph_index: 1,
      replacement_text: "Confidential Information means all non-public information.",
      status: "proposed",
    }],
    requirements_failed: 1,
    requirements_needs_review: 0,
    requirements_passed: 0,
  };

  await page.evaluate((payload) => {
    renderResult(payload, payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  }, reviewResult);
  await page.waitForSelector("#studioDocumentRender:not([hidden])");

  // Run-level formatting renders inside the editable body without changing its id.
  const p1 = page.locator('[data-paragraph-id="p1"]');
  assert.equal(await p1.locator('[data-editable-paragraph-id="p1"]').count(), 1);
  assert.equal(await p1.locator("strong").first().innerText(), "Bold");
  assert.equal(await p1.locator("em").first().innerText(), "italic");
  assert.equal(await p1.locator("u").first().innerText(), "underlined");
  // The flat text round-trips: innerText still equals the authoritative text.
  assert.equal(
    normalizeWhitespace(await p1.locator('[data-editable-paragraph-id="p1"]').innerText()),
    headingText,
  );

  // Heading typography class is applied to the frame.
  assert.ok(await p1.evaluate((node) => node.classList.contains("doc-heading") && node.classList.contains("doc-heading-1")));

  // List paragraph indents and exposes its captured marker.
  const p2 = page.locator('[data-paragraph-id="p2"]');
  assert.ok(await p2.evaluate((node) => node.classList.contains("doc-list")));
  assert.equal(await p2.getAttribute("data-structure-label"), "1.");

  // Table-cell paragraph gets the safe bordered treatment and keeps its hooks.
  const p3 = page.locator('[data-paragraph-id="p3"]');
  assert.ok(await p3.evaluate((node) => node.classList.contains("doc-table-cell")));
  assert.equal(await p3.getAttribute("data-table-index"), "1");
  assert.equal(await p3.locator('[data-editable-paragraph-id="p3"]').count(), 1);

  // HARD GUARD: clause selection still works after the richer rendering.
  await p1.click();
  await page.waitForSelector('[data-paragraph-id="p1"].selected');
  assert.equal(await page.evaluate(() => state.selectedReviewClauseId), "confidential_information");

  // HARD GUARD: the backend redline still anchors and previews on p1.
  await assertRedlinePreview(p1, {
    originalText: "Confidential Information Bold italic underlined",
    insertedText: "non-public information",
    editableCount: 1,
  });
  assert.equal(await p1.locator('[data-redline-edit-id="rich-redline-confidential-information"]').count(), 0);
  assert.equal(
    await page.evaluate(() => effectiveReviewRedlines().some((edit) => edit.paragraph_id === "p1")),
    true,
  );

  // HARD GUARD: the per-paragraph comment composer still opens on a rich paragraph.
  await page.evaluate(() => {
    const paragraph = document.querySelector('[data-paragraph-id="p2"]');
    const target = paragraph.querySelector('[data-editable-paragraph-id="p2"]') || paragraph;
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => node.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT,
    });
    const textNode = walker.nextNode();
    const range = document.createRange();
    range.setStart(textNode, 0);
    range.setEnd(textNode, Math.min(textNode.nodeValue.length, 5));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
  await page.waitForSelector('[data-paragraph-id="p2"].has-selection .paragraph-comment-add');
  await page.locator('[data-paragraph-id="p2"] .paragraph-comment-add').click();
  await page.waitForSelector('[data-paragraph-id="p2"] .comment-thread-card .comment-compose');
  assert.equal(await page.locator('[data-paragraph-id="p2"] .comment-compose-input').getAttribute("placeholder"), "Add a comment");
}

async function testStructuredEvidenceAndRationale(page) {
  await runReview(page, "This Agreement shall be governed by the laws of California.");
  await page.getByRole("button", { name: /Governing Law/ }).click();

  const detailPanel = page.locator("#studioDetailPanel");
  assert.deepEqual(
    await detailPanel.locator("[data-card-section]").evaluateAll((nodes) => nodes.map((node) => node.dataset.cardSection)),
    ["assessment", "document", "playbook", "recommended-change", "actions", "reasoning"],
  );
  await assertTextContains(detailPanel.locator(".active-clause-status"), "NEEDS REVIEW");
  assert.equal((await page.locator("#studioDetailPanel").innerText()).includes("ISSUE TYPE"), false);
  assert.equal((await detailPanel.innerText()).includes("RATIONALE"), false);
  await assertTextContains(detailPanel.locator('[data-card-section="document"]'), "No grounded quote was recorded");
  await assertTextContains(detailPanel.locator('[data-card-section="assessment"]'), "Stub reviewer: no issue.");
  await assertTextContains(detailPanel.locator('[data-card-section="actions"]'), "ATTACH COMMENT");

  await page.evaluate(() => {
    state.latestReviewResult.ai_review = {
      model: "x-ai/grok-4.3",
      provider: "openrouter",
      status: "completed",
    };
    const governingLaw = state.reviewClauses.find((clause) => clause.id === "governing_law");
    governingLaw.ai_review_analysis = {
      ai_confidence: 0.95,
      ai_decision: "fail",
      ai_reason: "California is outside the approved governing-law set.",
      cited_spans: [{
        paragraph_id: "p1",
        quote: "This Agreement shall be governed by the laws of California.",
        relevance: "Unapproved governing law.",
      }],
      disagreement: false,
      deterministic_decision: "fail",
      issues: ["unapproved_governing_law"],
      reason: "AI semantic review confirmed the deterministic decision.",
      status: "confirmed",
      suggested_fix: "Use Delaware, India, England and Wales, or DIFC.",
      validation_errors: [],
    };
    renderStudioResult({ clauses: state.reviewClauses });
  });
  await assertTextContains(detailPanel.locator('[data-card-section="document"]'), "This Agreement shall be governed by the laws of California.");
  assert.doesNotMatch(await page.locator("#studioDetailPanel").innerText(), /AI agrees|No contrary reason/);
}

async function testAiSecondOpinionButton(page) {
  await runReview(page, passNda);

  await assertTextContains(page.locator("#studioDetailPanel"), "ATTACH COMMENT");
  assert.equal(await page.locator('[data-ai-second-opinion-clause-id]').count(), 0);
  assert.equal(await page.locator(".ai-second-opinion-button").count(), 0);
  assert.equal(await page.locator(".ai-actions-block").count(), 0);
  assert.equal(await page.locator(".ai-summary-block").count(), 0);
  assert.equal(await page.getByRole("button", { name: /second opinion/i }).count(), 0);
}

async function testAiDraftFixValidationButton(page) {
  await runReview(page, termOnlyRedlineNda);

  await assertTextContains(page.locator("#studioDetailPanel"), "ATTACH COMMENT");
  assert.equal(await page.locator('[data-ai-draft-validation-redline-id]').count(), 0);
  assert.equal(await page.getByRole("button", { name: /validate draft fix/i }).count(), 0);
  assert.doesNotMatch(await page.locator("#studioDetailPanel").innerText(), /AI DRAFT/i);
}

async function testPerClauseReviewedToggle(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  await page.evaluate(() => {
    const paragraphs = [
      { id: "p1", index: 1, source_index: 1, text: "Confidential Information means all business information." },
      { id: "p2", index: 2, source_index: 2, text: "Survival applies as set out in the referenced schedule." },
    ];
    renderResult({
      checked_at: "2026-06-04T09:00:00+00:00",
      clauses: [
        {
          decision: "review",
          evidence_paragraphs: [paragraphs[0]],
          id: "confidential_information",
          issue_label: "Needs review",
          name: "Confidential Information",
          needs_review: true,
          reason: "Broad confidential information definition needs human review.",
          review_state: { blocks_send: true, requires_human_review: true, state: "review" },
          status: "review",
        },
        {
          decision: "review",
          evidence_paragraphs: [paragraphs[1]],
          id: "term_and_survival",
          issue_label: "Needs review",
          name: "Term and Survival",
          needs_review: true,
          reason: "Survival reference needs human review.",
          review_state: { blocks_send: true, requires_human_review: true, state: "review" },
          status: "review",
        },
      ],
      overall_status: "needs_review",
      paragraphs,
      redline_edits: [],
      requirements_failed: 0,
      requirements_needs_review: 2,
      requirements_passed: 0,
    }, paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  });

  const confidentialCard = page.locator('[data-studio-lane-id="confidential_information"]');
  const termCard = page.locator('[data-studio-lane-id="term_and_survival"]');
  const activeReviewToggle = page.locator('#studioDetailPanel [data-review-action="mark-reviewed"]');
  await confidentialCard.click();
  await assertTextContains(activeReviewToggle, "NEEDS REVIEW");
  assert.equal(await activeReviewToggle.getAttribute("aria-pressed"), "false");
  assert.match(await activeReviewToggle.getAttribute("title"), /Mark reviewed/);
  await assertAttributeMatches(confidentialCard, "aria-label", /Needs review/);
  await assertAttributeMatches(termCard, "aria-label", /Needs review/);

  await activeReviewToggle.click();
  await page.waitForFunction(() => state.reviewedClauseIds.confidential_information === true);
  await assertTextContains(activeReviewToggle, "REVIEWED");
  assert.equal(await activeReviewToggle.getAttribute("aria-pressed"), "true");
  assert.match(await activeReviewToggle.getAttribute("title"), /Mark as needs review/);
  await assertAttributeMatches(confidentialCard, "aria-label", /Reviewed/);
  await assertAttributeMatches(termCard, "aria-label", /Needs review/);
  assert.deepEqual(
    await page.evaluate(() => ({
      confidential: state.reviewedClauseIds.confidential_information,
      term: state.reviewedClauseIds.term_and_survival,
    })),
    { confidential: true, term: undefined },
  );

  await activeReviewToggle.focus();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => state.reviewedClauseIds.confidential_information === false);
  await assertTextContains(activeReviewToggle, "NEEDS REVIEW");
  assert.equal(await activeReviewToggle.getAttribute("aria-pressed"), "false");
  await assertAttributeMatches(confidentialCard, "aria-label", /Needs review/);
  await assertAttributeMatches(termCard, "aria-label", /Needs review/);
}

async function testReviewedMatterStatusSummary(page) {
  let reviewedPayload = null;
  await page.route("**/api/matters/matter_review_panel/reviewed", async (route) => {
    reviewedPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        matter: {
          id: "matter_review_panel",
          can_send_redline: true,
          human_reviewed: true,
          recipient_email: "counterparty@example.com",
        },
      }),
    });
  });
  await loadReviewWithMatter(page, {
    matter: {
      can_send_redline: true,
      human_reviewed: false,
      recipient_email: "counterparty@example.com",
      review_result: {
        overall_status: "needs_review",
        requirements_failed: 0,
        requirements_needs_review: 2,
        requirements_passed: 0,
      },
    },
    clauses: [
      {
        decision: "review",
        evidence_paragraphs: [{ id: "p1", index: 1, text: "Confidential Information means all business information." }],
        id: "confidential_information",
        issue_label: "Needs review",
        name: "Confidential Information",
        needs_review: true,
        reason: "Broad confidential information definition needs human review.",
        review_state: { blocks_send: true, requires_human_review: true, state: "review" },
        status: "review",
      },
      {
        decision: "review",
        evidence_paragraphs: [{ id: "p2", index: 2, text: "Survival applies as set out in the referenced schedule." }],
        id: "term_and_survival",
        issue_label: "Needs review",
        name: "Term and Survival",
        needs_review: true,
        reason: "Survival reference needs human review.",
        review_state: { blocks_send: true, requires_human_review: true, state: "review" },
        status: "review",
      },
    ],
    paragraphs: [
      { id: "p1", index: 1, source_index: 1, text: "Confidential Information means all business information." },
      { id: "p2", index: 2, source_index: 2, text: "Survival applies as set out in the referenced schedule." },
    ],
    result: {
      overall_status: "needs_review",
      requirements_failed: 0,
      requirements_needs_review: 2,
      requirements_passed: 0,
    },
  });

  await assertTextContains(page.locator("#studioOverallTitle"), "Needs review");
  await assertTextContains(page.locator("#studioResultMeta"), "human review before send");
  await assertTextContains(page.locator("#studioSendButton"), "Needs Review");
  const reviewedButton = page.locator("#studioReviewedButton");
  await page.waitForFunction(() => !document.querySelector("#studioReviewedButton")?.hidden);
  await reviewedButton.click();
  await page.waitForFunction(() => state.selectedMatter?.human_reviewed === true);
  assert.deepEqual(reviewedPayload, { reviewed: true });
  await assertTextContains(page.locator("#studioOverallTitle"), "Reviewed");
  await assertTextContains(page.locator("#studioResultMeta"), "All human-review clauses have been reviewed.");
  assert.equal((await page.locator("#studioOverallTitle").innerText()).includes("Needs review"), false);
  assert.equal((await page.locator("#studioResultMeta").innerText()).includes("human review before send"), false);
  assert.notEqual(await page.locator("#studioSendButton").innerText(), "Needs Review");
  assert.equal(await reviewedButton.isHidden(), true);
  await page.unroute("**/api/matters/matter_review_panel/reviewed");
}

async function testReviewSendUsesCurrentMatterAfterSwitch(page) {
  const buildMatter = ({ id, title, text, redlineText, commentText }) => ({
    id,
    board_column: "in_review",
    can_send_redline: true,
    document_title: title,
    extracted_text: text,
    human_reviewed: true,
    recipient_email: `${id}@example.com`,
    requirements_failed: 1,
    requirements_needs_review: 0,
    requirements_passed: 5,
    review_result: {
      checked_at: "2026-06-04T09:00:00+00:00",
      clauses: [{
        decision: "fail",
        evidence_paragraphs: [{ id: "p1", index: 1, source_index: 1, text }],
        id: "confidential_information",
        issue_label: "Present but wrong",
        matched_paragraph_ids: ["p1"],
        name: "Confidential Information",
        needs_review: false,
        passes: false,
        reason: "The definition is too narrow.",
        review_state: { requires_redline: true, state: "check" },
        status: "check",
      }],
      overall_status: "needs_redline",
      paragraphs: [{ id: "p1", index: 1, source_index: 1, text }],
      redline_edits: [{
        action: "replace_paragraph",
        action_label: "Replace paragraph",
        clause_id: "confidential_information",
        id: `${id}-redline-confidential-information`,
        original_text: text,
        paragraph_id: "p1",
        paragraph_index: 1,
        replacement_text: redlineText,
        status: "proposed",
      }],
      requirements_failed: 1,
      requirements_needs_review: 0,
      requirements_passed: 5,
    },
    review_comments: [],
    redline_draft: {
      manual_redline_edits: [],
      review_comments: [{
        author: "Reviewer",
        clause_id: "confidential_information",
        clause_name: "Confidential Information",
        id: `${id}-comment-confidential-information`,
        paragraph_id: "p1",
        paragraph_index: 1,
        scope: "clause",
        text: commentText,
      }],
    },
    sender: `${title} Sender <${id}@example.com>`,
    source_filename: `${title}.docx`,
    source_type: "gmail_inbound",
    subject: title,
    triage_status: "needs_redline",
  });
  const matters = [
    buildMatter({
      id: "matter_alpha_send",
      title: "Alpha NDA",
      text: "Alpha Confidential Information only includes marked information.",
      redlineText: "Alpha replacement confidential information language.",
      commentText: "Alpha comment only.",
    }),
    buildMatter({
      id: "matter_beta_send",
      title: "Beta NDA",
      text: "Beta Confidential Information only includes labelled information.",
      redlineText: "Beta replacement confidential information language.",
      commentText: "Beta comment only.",
    }),
  ];
  const sendPayloads = [];

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: { configured: true, email: "inbound@aspora.com", ready: true },
          outbound: { configured: true, email: "outbound@aspora.com", ready: true },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters }),
    });
  });
  await page.route("**/api/gmail/send-redline", async (route) => {
    const payload = route.request().postDataJSON();
    sendPayloads.push(payload);
    const matter = matters.find((item) => item.id === payload.matter_id);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        filename: `${matter.document_title.replaceAll(" ", "-")}-redlined.docx`,
        matter: {
          ...matter,
          board_column: "sent",
          last_outbound_subject: payload.subject,
          last_outbound_to: matter.recipient_email,
        },
        sent: {
          message_id: `${matter.id}-message`,
          outbound_account: "outbound@aspora.com",
          sent_at: "2026-06-04T09:00:00+00:00",
          subject: payload.subject,
          thread_id: `${matter.id}-thread`,
          to: matter.recipient_email,
        },
      }),
    });
  });
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  async function loadMatterInReview(index) {
    await page.evaluate((matter) => {
      state.selectedMatter = matter;
      state.selectedDocument = null;
      setSourceText(matter.extracted_text);
      setSourcePlaceholder(SOURCE_PLACEHOLDER);
      setDocumentTitle(matter.document_title);
      setCounterpartyMeta(matter.recipient_email);
      renderResult(matter.review_result, matter.extracted_text);
      applyMatterRedlineDraft(matter.redline_draft);
      activateTab("review");
      updateExportButtonState();
    }, matters[index]);
    await page.waitForSelector("#reviewView:not([hidden])");
    await page.waitForSelector("#studioSendButton:not(:disabled):not(.blocked)");
  }

  async function sendLoadedMatter(expectedTitle) {
    await page.locator("#studioSendButton").click();
    await page.waitForSelector("#studioSendModal:not([hidden])");
    assert.equal(await page.locator("#studioSendSubject").inputValue(), `Redline for ${expectedTitle}`);
    await page.locator("#studioSendConfirmButton").click();
    await page.waitForSelector("#studioSendModal[hidden]", { state: "attached" });
  }

  await loadMatterInReview(0);
  await sendLoadedMatter("Alpha NDA");
  await loadMatterInReview(1);
  await sendLoadedMatter("Beta NDA");

  assert.equal(sendPayloads.length, 2);
  assert.equal(sendPayloads[0].matter_id, "matter_alpha_send");
  assert.equal(sendPayloads[0].text, "Alpha Confidential Information only includes marked information.");
  assert.equal(sendPayloads[0].export_redline_edits[0].replacement_text, "Alpha replacement confidential information language.");
  assert.equal(sendPayloads[0].review_comments[0].text, "Alpha comment only.");
  assert.equal(sendPayloads[1].matter_id, "matter_beta_send");
  assert.equal(sendPayloads[1].to, "matter_beta_send@example.com");
  assert.equal(sendPayloads[1].text, "Beta Confidential Information only includes labelled information.");
  assert.equal(sendPayloads[1].export_redline_edits[0].replacement_text, "Beta replacement confidential information language.");
  assert.equal(sendPayloads[1].review_comments[0].text, "Beta comment only.");
  assert.doesNotMatch(sendPayloads[1].body, /Alpha/);
  assert.match(sendPayloads[1].body, /Beta/);

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/gmail/send-redline");
}

async function testReviewSendAcceptsManualRecipient(page) {
  const matter = {
    id: "matter_manual_recipient_send",
    board_column: "in_review",
    can_send_redline: false,
    document_title: "Manual Upload NDA",
    extracted_text: "The confidentiality obligations survive for seven years.",
    human_reviewed: true,
    recipient_email: "",
    requirements_failed: 1,
    requirements_needs_review: 0,
    requirements_passed: 4,
    send_block_reason: "Matter does not have a valid reply recipient email address.",
    review_result: {
      checked_at: "2026-06-04T09:00:00+00:00",
      clauses: [{
        decision: "fail",
        evidence_paragraphs: [{
          id: "p1",
          index: 1,
          source_index: 1,
          text: "The confidentiality obligations survive for seven years.",
        }],
        id: "term_and_survival",
        issue_label: "Requires redline",
        matched_paragraph_ids: ["p1"],
        name: "Term and Survival",
        needs_review: false,
        passes: false,
        reason: "Survival is longer than the playbook cap.",
        review_state: { requires_redline: true, state: "check" },
        status: "check",
      }],
      overall_status: "needs_redline",
      paragraphs: [{
        id: "p1",
        index: 1,
        source_index: 1,
        text: "The confidentiality obligations survive for seven years.",
      }],
      redline_edits: [{
        action: "replace_paragraph",
        action_label: "Replace paragraph",
        clause_id: "term_and_survival",
        id: "manual-recipient-term-redline",
        original_text: "The confidentiality obligations survive for seven years.",
        paragraph_id: "p1",
        paragraph_index: 1,
        replacement_text: "The confidentiality obligations survive for five years.",
        status: "proposed",
      }],
      requirements_failed: 1,
      requirements_needs_review: 0,
      requirements_passed: 4,
    },
    sender: "Manual upload",
    source_filename: "Manual Upload NDA.docx",
    source_type: "upload",
    subject: "Manual Upload NDA",
    triage_status: "needs_redline",
  };
  let capturedSendPayload = null;

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: { configured: true, email: "inbound@aspora.com", ready: true },
          outbound: { configured: true, email: "outbound@aspora.com", ready: true },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [matter] }),
    });
  });
  await page.route("**/api/gmail/send-redline", async (route) => {
    capturedSendPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        filename: "Manual-Upload-NDA-redlined.docx",
        matter: {
          ...matter,
          board_column: "sent",
          last_outbound_subject: capturedSendPayload.subject,
          last_outbound_to: capturedSendPayload.to,
        },
        sent: {
          message_id: "manual-recipient-message",
          outbound_account: "outbound@aspora.com",
          sent_at: "2026-06-04T09:15:00+00:00",
          subject: capturedSendPayload.subject,
          thread_id: "manual-recipient-thread",
          to: capturedSendPayload.to,
        },
      }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.evaluate((loadedMatter) => {
    state.selectedMatter = loadedMatter;
    state.selectedDocument = null;
    setSourceText(loadedMatter.extracted_text);
    setSourcePlaceholder(SOURCE_PLACEHOLDER);
    setDocumentTitle(loadedMatter.document_title);
    setCounterpartyMeta("");
    renderResult(loadedMatter.review_result, loadedMatter.extracted_text);
    activateTab("review");
    updateExportButtonState();
  }, matter);
  await page.waitForSelector("#reviewView:not([hidden])");
  await page.waitForSelector("#studioSendButton.blocked");
  assert.equal(await page.locator("#studioSendButton").getAttribute("aria-label"), "No Reply");
  assert.match(await page.locator("#studioSendButton").getAttribute("title"), /valid reply recipient/);
  assert.equal(await page.locator("#studioSendButton .send-button-label").innerText(), "No Reply");
  const sendButtonLabelBox = await page.locator("#studioSendButton .send-button-label").evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const styles = getComputedStyle(node);
    return {
      height: rect.height,
      position: styles.position,
      width: rect.width,
    };
  });
  assert.deepEqual(sendButtonLabelBox, { height: 1, position: "absolute", width: 1 });

  await page.locator("#studioSendButton").click();
  await page.waitForSelector("#studioSendModal:not([hidden])");
  assert.equal(await page.locator("#studioSendTo").inputValue(), "");
  await assertTextContains(page.locator("#studioSendStatus"), "Enter a recipient email address before sending.");

  await page.locator("#studioSendConfirmButton").click();
  await assertTextContains(page.locator("#studioSendStatus"), "Enter a valid recipient email address.");
  await page.locator("#studioSendTo").fill("counterparty@example.com");
  await page.locator("#studioSendConfirmButton").click();
  await page.waitForSelector("#studioSendModal[hidden]", { state: "attached" });
  await waitForText(page, "#studioFileMeta", "Sent redline to counterparty@example.com");

  assert.equal(capturedSendPayload.matter_id, "matter_manual_recipient_send");
  assert.equal(capturedSendPayload.to, "counterparty@example.com");
  assert.equal(capturedSendPayload.export_redline_edits.length, 1);
  assert.equal(capturedSendPayload.export_redline_edits[0].clause_id, "term_and_survival");

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/gmail/send-redline");
}

// The draft-intake "Generate NDA" button (un-stubbed): opening the modal, picking
// a signing entity + counterparty, and clicking Generate POSTs buildDraftPayload's
// shape to /api/generate-nda, then surfaces the saved-NDA success and downloads
// the generated document from the matter-source download_url the endpoint returns.
async function testDraftIntakeGenerateNda(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  let capturedGeneratePayload = null;
  await page.route("**/api/generate-nda", async (route) => {
    capturedGeneratePayload = route.request().postDataJSON();
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        matter_id: "mat_generated_1",
        artifact_id: "art_generated_1",
        status: "generated",
        download_url: "/api/matters/mat_generated_1/source",
        document_downloads: {
          source: {
            formats: {
              docx: {
                available: true,
                content_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                download_url: "/api/matters/mat_generated_1/source",
                filename: "Acme-Corporation-NDA.docx",
                format: "docx",
              },
              pdf: {
                available: false,
                content_type: "application/pdf",
                filename: "Acme-Corporation-NDA.pdf",
                format: "pdf",
                unavailable_reason: "PDF conversion is not configured.",
              },
            },
            label: "Generated document",
          },
        },
        self_check: { passed: true, overall_status: "pass", native_failures: [], dynamic_failures: [] },
        manifest: {
          entity_id: "aspora_technology",
          governing_law_value: "England and Wales",
          governing_law_option_id: "england_and_wales",
          governing_law_overridden: true,
          entity_default_governing_law_value: "India",
          term_years: 2,
          sanitized_fields: [],
        },
      }),
    });
  });
  // The generated document is fetched from the matter-source URL when Send
  // attaches it to the Send Document modal.
  await page.route("**/api/matters/mat_generated_1/source", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      body: "PK generated-nda-docx-bytes",
    });
  });
  // The Generator is its own top-nav tab. Open it from the nav, confirm the tab
  // panel (not a modal) is shown, pick our signing entity + a counterparty so the
  // Generate button enables, then generate.
  await page.locator("#generatorTab").click();
  await page.waitForSelector("#generatorView:not([hidden])");
  assert.equal(await page.locator("#generatorTab").getAttribute("aria-selected"), "true");
  // activate() loads the registry + populates the entity options asynchronously.
  // Wait for the options to fill (an <option> can't be waited on via a visibility
  // selector, so poll the select's option count) before picking our entity.
  await page.waitForFunction(
    () => document.querySelector("#draftIntakeEntitySelect")?.options.length > 1,
  );
  await page.locator("#draftIntakeEntitySelect").selectOption("aspora_technology");
  await page.locator("#draftIntakeCounterpartyName").fill("Acme Corporation");
  await page.locator("#draftIntakeCounterpartyEmail").fill("deals@acme.com");
  await page.waitForSelector("#draftIntakeGenerateButton:not([disabled])");

  // Generate no longer auto-downloads — it stages the Download/Send actions and
  // reports the saved state. Click Generate and confirm the success line.
  await page.locator("#draftIntakeGenerateButton").click();
  await waitForText(page, "#draftIntakeStatus", "NDA generated and saved");
  await assertTextContains(page.locator("#draftIntakeStatus"), "Acme Corporation");
  // The success line confirms the generated terms from the manifest at a glance,
  // including the server-authoritative governing-law override provenance.
  await assertTextContains(page.locator("#draftIntakeStatus"), "England and Wales (overridden from India)");
  await assertTextContains(page.locator("#draftIntakeStatus"), "2-year term");

  // Download + Send come online only after a successful generation; clicking
  // Download offers the generated document from the returned matter-source URL,
  // which the browser surfaces as a download event (context has acceptDownloads).
  await page.waitForSelector("#draftIntakeDownloadButton:not([disabled])");
  await page.waitForSelector("#draftIntakeSendButton:not([disabled])");
  const formatMenu = await openDownloadMenu(page.locator("#draftIntakeDownloadButton"));
  await assertTextContains(formatMenu, "DOCX");
  await assertTextContains(formatMenu, "PDF conversion is not configured.");
  assert.equal(await formatMenu.locator('[data-download-format="pdf"]').isDisabled(), true);
  await page.keyboard.press("Escape");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    chooseDownloadFormat(page.locator("#draftIntakeDownloadButton"), "docx"),
  ]);
  assert.match(download.url(), /\/api\/matters\/mat_generated_1\/source$/);

  // The POST carries buildDraftPayload's shape: the coupled signing-entity bundle
  // and the counterparty block the endpoint resolves the entity + intake from.
  assert.ok(capturedGeneratePayload, "expected a /api/generate-nda POST");
  assert.equal(capturedGeneratePayload.signing_entity.id, "aspora_technology");
  assert.ok(capturedGeneratePayload.signing_entity.legal_name);
  assert.equal(capturedGeneratePayload.counterparty.name, "Acme Corporation");
  assert.ok(capturedGeneratePayload.signing_entity.governing_law.playbook_option_id);

  // Send opens the Send Document modal with the counterparty email linked as the
  // Recipient Email immediately — the link is not gated on the document download.
  await page.locator("#draftIntakeSendButton").click();
  await page.waitForSelector("#sendDocumentModal:not([hidden])");
  assert.equal(await page.locator("#sendDocumentRecipientInput").inputValue(), "deals@acme.com");
  // The generated NDA is fetched from the matter-source URL and attached to the
  // modal, so it can actually be emailed (Send document enables once attached).
  await page.waitForFunction(() => {
    const n = document.querySelector("#sendDocumentSelectedFile");
    return n && !n.classList.contains("empty") && !/attaching/i.test(n.textContent || "");
  });
  await page.waitForSelector("#sendDocumentSubmitButton:not([disabled])");
  await page.locator("#sendDocumentModalClose").click();

  await page.unroute("**/api/generate-nda");
  await page.unroute("**/api/matters/mat_generated_1/source");
}

async function testDraftIntakeGenerateSelfCheckWarning(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  await page.route("**/api/generate-nda", async (route) => {
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        matter_id: "mat_generated_warning",
        artifact_id: "art_generated_warning",
        status: "generated",
        download_url: "/api/matters/mat_generated_warning/source",
        self_check: {
          dynamic_failures: [{ clause_id: "term", reason: "Term did not match Playbook preference." }],
          native_failures: [],
          overall_status: "check",
          passed: false,
        },
        manifest: {
          entity_id: "aspora_technology",
          governing_law_value: "England and Wales",
          term_years: 2,
        },
      }),
    });
  });
  await page.route("**/api/matters/mat_generated_warning/source", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      body: "PK generated-warning-docx-bytes",
    });
  });

  await page.locator("#generatorTab").click();
  await page.waitForSelector("#generatorView:not([hidden])");
  await page.waitForFunction(
    () => document.querySelector("#draftIntakeEntitySelect")?.options.length > 1,
  );
  await page.locator("#draftIntakeEntitySelect").selectOption("aspora_technology");
  await page.locator("#draftIntakeCounterpartyName").fill("Warning Corp");
  await page.locator("#draftIntakeCounterpartyEmail").fill("legal@warning.example");
  await page.waitForSelector("#draftIntakeGenerateButton:not([disabled])");
  await page.locator("#draftIntakeGenerateButton").click();

  await waitForText(page, "#draftIntakeStatus", "self-check flagged it");
  await assertTextContains(page.locator("#draftIntakeStatus"), "Warning Corp");
  await assertTextContains(page.locator("#draftIntakeStatus"), "England and Wales");
  assert.equal(await page.locator("#draftIntakeStatus").evaluate((node) => node.classList.contains("error")), true);
  await page.waitForSelector("#draftIntakeDownloadButton:not([disabled])");
  await page.waitForSelector("#draftIntakeSendButton:not([disabled])");

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    chooseDownloadFormat(page.locator("#draftIntakeDownloadButton"), "docx"),
  ]);
  assert.match(download.url(), /\/api\/matters\/mat_generated_warning\/source$/);

  await page.unroute("**/api/generate-nda");
  await page.unroute("**/api/matters/mat_generated_warning/source");
}

// When the endpoint is not deployed on this base (404 — generation lives on
// another branch until integration), the form degrades gracefully to a neutral
// "not available" notice rather than showing a hard generation failure.
async function testDraftIntakeGenerateDegradesOn404(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  await page.route("**/api/generate-nda", async (route) => {
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: "not found" }) });
  });

  await page.locator("#generatorTab").click();
  await page.waitForSelector("#generatorView:not([hidden])");
  await page.waitForFunction(
    () => document.querySelector("#draftIntakeEntitySelect")?.options.length > 1,
  );
  await page.locator("#draftIntakeEntitySelect").selectOption("aspora_technology");
  await page.locator("#draftIntakeCounterpartyName").fill("Acme Corporation");
  await page.waitForSelector("#draftIntakeGenerateButton:not([disabled])");
  await page.locator("#draftIntakeGenerateButton").click();

  await waitForText(page, "#draftIntakeStatus", "not available on this build yet");
  // Degradation is a notice, not an error tone.
  assert.equal(await page.locator("#draftIntakeStatus.error").count(), 0);

  await page.unroute("**/api/generate-nda");
}

async function testRepositoryMatterImportAndFreshReview(page) {
  const docxPath = path.join(os.tmpdir(), `repository-matter-${Date.now()}.docx`);
  const deleteDocxPath = path.join(os.tmpdir(), `repository-delete-${Date.now()}.docx`);
  const deleteStem = path.basename(deleteDocxPath, ".docx");
  makeDocxFixture(docxPath, [
    "This Agreement shall be governed by the laws of California.",
    "The Recipient must not circumvent the Company.",
  ]);
  makeDocxFixture(deleteDocxPath, [
    "This Agreement shall be governed by the laws of Delaware.",
  ]);

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          settings: {
            sync_frequency: "10_minutes",
            last_sync_at: "2026-05-31T12:34:00+00:00",
            last_sync_imported_count: 0,
            last_sync_skipped_count: 1,
          },
          inbound: { ready: true, email: "inbound@example.com" },
          outbound: { ready: true, email: "outbound@example.com" },
        },
      }),
    });
  });
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  assert.equal(await page.locator("#gmailDemoStatus").count(), 0);
  assert.equal(await page.locator("#gmailLastSync").count(), 0);
  assert.equal(await page.locator("#gmailSyncButton").count(), 0);
  await page.getByRole("tab", { name: "Admin" }).click();
  await page.locator('[data-admin-section="email"]').click();
  const serverSyncLabel = await page.evaluate(() => new Date("2026-05-31T12:34:00+00:00").toLocaleString(undefined, {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "short",
  }));
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "inbound@example.com");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), serverSyncLabel);
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "0 imported / 1 skipped");
  assert.equal(await page.getByRole("button", { name: "Sync Gmail" }).count(), 0);
  await page.getByRole("tab", { name: "Repository" }).click();

  assert.equal(await page.locator("#repositoryFileInput").count(), 0);
  assert.equal(await page.getByText("Import NDA", { exact: true }).count(), 0);
  await createRepositoryMatter(page, docxPath, { received_at: "2026-05-31T12:00:00+00:00" });
  await createRepositoryMatter(page, deleteDocxPath, { received_at: "2026-06-01T12:00:00+00:00" });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  assert.equal(await page.locator('[data-repository-count="manual_upload"]').innerText(), "2");
  assert.equal(await page.locator('[data-repository-count="in_review"]').innerText(), "0");
  await page.getByRole("searchbox", { name: "Search repository cards" }).fill(deleteStem);
  assert.equal(await page.locator(".repository-card").count(), 1);
  await assertTextContains(page.locator(".repository-card"), deleteStem);
  assert.equal(await page.locator('[data-repository-count="manual_upload"]').innerText(), "1");
  await page.getByRole("searchbox", { name: "Search repository cards" }).fill("no matching nda");
  assert.equal(await page.locator(".repository-card").count(), 0);
  await assertTextContains(page.locator('[data-repository-list="manual_upload"]'), "No matching documents");
  await page.getByRole("searchbox", { name: "Search repository cards" }).fill("");
  assert.equal(await page.locator(".repository-card").count(), 2);
  assert.equal(await page.locator('[data-repository-count="manual_upload"]').innerText(), "2");
  await assertTextContains(page.locator(".repository-card").first(), deleteStem);
  const deleteCard = page.locator(".repository-card").filter({ hasText: deleteStem });
  await deleteCard.getByRole("button", { name: "Delete matter" }).click();
  await assertTextContains(deleteCard, "Delete matter and stored document?");
  assert.equal(await page.locator(".repository-card").filter({ hasText: deleteStem }).count(), 1);
  assert.equal(await page.locator('[data-repository-count="manual_upload"]').innerText(), "2");
  await deleteCard.getByRole("button", { name: "Cancel delete matter" }).click();
  assert.equal(await deleteCard.getByRole("group", { name: "Delete matter confirmation" }).count(), 0);
  await deleteCard.getByRole("button", { name: "Delete matter" }).click();
  await deleteCard.getByRole("button", { name: "Confirm delete matter" }).click();
  await waitForRepositoryCount(page, "manual_upload", "1");
  assert.equal(await page.locator(".repository-card").filter({ hasText: deleteStem }).count(), 0);
  assert.equal(await page.locator("#repositoryMatterPanel:not([hidden])").count(), 0);
  assert.equal(await page.locator('[data-repository-count="reviewed"]').innerText(), "0");
  await assertTextContains(page.locator(".repository-card"), "Manual upload");
  await assertTextContains(page.locator(".repository-card"), "Manual Upload");
  await assertTextContains(page.locator(".repository-card"), "Manual upload of repository-matter");

  await page.locator(".repository-card").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "MANUAL UPLOAD");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Manual upload");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "repository-matter-");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "KEY FAILED CLAUSES");
  assert.equal(await page.locator("#studioSendButton").isEnabled(), false);

  const [matterExportRequest, matterDownload] = await Promise.all([
    page.waitForRequest((request) => request.url().endsWith("/api/export-review-docx")),
    page.waitForEvent("download"),
    chooseDownloadFormat(page.getByRole("button", { name: "Download" }), "docx"),
  ]);
  const matterExportPayload = matterExportRequest.postDataJSON();
  assert.ok(matterExportPayload.matter_id, "Repository panel export should send a matter id");
  assert.match(matterDownload.suggestedFilename(), /^repository-matter-\d+-redlined(?:-[0-9a-f]{12})?\.docx$/);
  await assertTextContains(page.locator("#repositoryMatterPanel"), "still needs human review");
  await waitForRepositoryCount(page, "manual_upload", "1");
  await waitForRepositoryCount(page, "in_review", "0");
  await waitForRepositoryCount(page, "reviewed", "0");

  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "true");
  await assertTextContains(page.locator("#studioDocTitle"), "repository-matter-");
  await waitForText(page, "#studioFileMeta", "Manual Upload matter loaded");
  await assertTextContains(page.locator("#studioFileMeta"), "Manual Upload matter loaded");
  await waitForRepositoryCount(page, "manual_upload", "1");
  await waitForRepositoryCount(page, "in_review", "0");
  await waitForRepositoryCount(page, "reviewed", "0");
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector("#repositoryMatterPanel[hidden]", { state: "attached" });
  assert.equal(await page.locator(".repository-card.active").count(), 0);
  await page.getByRole("tab", { name: "Review" }).click();

  const [reviewMatterExportRequest, reviewMatterDownload] = await Promise.all([
    page.waitForRequest((request) => request.url().endsWith("/api/export-review-docx")),
    page.waitForEvent("download"),
    chooseDownloadFormat(page.locator("#studioExportButton"), "docx"),
  ]);
  const reviewMatterExportPayload = reviewMatterExportRequest.postDataJSON();
  assert.ok(reviewMatterExportPayload.matter_id, "Loaded repository matter export should send a matter id");
  assert.match(reviewMatterDownload.suggestedFilename(), /^repository-matter-\d+-redlined(?:-[0-9a-f]{12})?\.docx$/);
  await waitForRepositoryCount(page, "manual_upload", "1");
  await waitForRepositoryCount(page, "in_review", "0");
  await waitForRepositoryCount(page, "reviewed", "0");
  assert.equal(await page.getByRole("button", { name: "Review NDA" }).count(), 0);

  await page.getByRole("tab", { name: "Repository" }).click();
  await page.locator(".repository-card").filter({ hasText: path.basename(docxPath, ".docx") }).click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  assert.equal(await page.getByRole("button", { name: "Close Matter", exact: true }).count(), 0);
  await page.getByRole("button", { name: "Close matter inspector" }).click();
  await page.waitForSelector("#repositoryMatterPanel[hidden]", { state: "attached" });
  assert.equal(await page.getByRole("button", { name: "Reset Demo" }).count(), 0);
  assert.equal(await page.locator("#repositoryImportStatus").count(), 0);
  await page.evaluate(async () => {
    const response = await fetch("/api/demo/reset", { method: "POST" });
    if (!response.ok) throw new Error("Demo reset failed");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await waitForRepositoryCount(page, "sent", "0");

  fs.rmSync(docxPath, { force: true });
}

async function testRepositoryOpenReviewRepeatedly(page) {
  const buildMatter = (id, title, text) => ({
    id,
    attachment_filename: `${title}.docx`,
    board_column: "in_review",
    can_send_redline: false,
    created_at: "2026-06-01T09:00:00+00:00",
    document_title: title,
    extracted_text: text,
    issue_count: 0,
    message_snippet: text,
    received_at: "2026-06-01T09:00:00+00:00",
    requirements_failed: 0,
    requirements_needs_review: 0,
    requirements_passed: 1,
    review_result: {
      checked_at: "2026-06-01T09:01:00+00:00",
      clauses: [{
        decision: "pass",
        id: "mutuality",
        issue_label: "No issue",
        name: "Mutuality",
        passes: true,
        requirement: "The NDA must bind both parties symmetrically.",
        why: "Mutual obligation language found.",
      }],
      overall_status: "meets_requirements",
      paragraphs: [{ id: "p1", index: 1, source_index: 1, text }],
      redline_edits: [],
      requirements_failed: 0,
      requirements_needs_review: 0,
      requirements_passed: 1,
    },
    sender: "Legal Team <legal@example.com>",
    source_filename: `${title}.docx`,
    source_type: "manual_upload",
    subject: title,
    triage_status: "approved",
    updated_at: "2026-06-01T09:01:00+00:00",
  });
  const matters = [
    buildMatter("matter_alpha_review", "Alpha Review NDA", "Alpha document text for repeated review opening."),
    buildMatter("matter_beta_review", "Beta Review NDA", "Beta document text for the second review opening."),
  ];
  const matterById = new Map(matters.map((matter) => [matter.id, matter]));
  let releaseBetaReview = () => {};
  const betaReviewGate = new Promise((resolve) => {
    releaseBetaReview = resolve;
  });

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: { ready: true, email: "inbound@example.com" },
          outbound: { ready: false, error: "No outbound account configured" },
        },
      }),
    });
  });
  await page.route("**/api/matters**", async (route) => {
    const requestUrl = new URL(route.request().url());
    const parts = requestUrl.pathname.split("/").filter(Boolean);
    if (requestUrl.pathname === "/api/matters" && route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ matters }),
      });
      return;
    }
    const matterId = parts[2];
    const matter = matterById.get(matterId);
    if (!matter) {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: "Not found" }) });
      return;
    }
    if (requestUrl.pathname.endsWith("/stage")) {
      const payload = route.request().postDataJSON();
      matter.board_column = payload.board_column || matter.board_column;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matter }) });
      return;
    }
    if (requestUrl.pathname.endsWith("/review-refresh")) {
      if (matter.id === "matter_beta_review") {
        await betaReviewGate;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          extracted_text: matter.extracted_text,
          matter,
          review_refresh: { refreshed: true, stale: false },
          review_result: matter.review_result,
        }),
      });
      return;
    }
    if (requestUrl.pathname.endsWith("/review") || requestUrl.pathname.endsWith("/review-refresh")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          extracted_text: matter.extracted_text,
          matter,
          review_refresh: { stale: true },
          review_result: matter.review_result,
        }),
      });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matter }) });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");

  await page.locator(".repository-card").filter({ hasText: "Alpha Review NDA" }).click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Alpha Review NDA");
  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "true");
  await assertTextContains(page.locator("#studioDocTitle"), "Alpha Review NDA");
  await assertTextContains(page.locator("#studioDocumentRender"), "Alpha document text for repeated review opening.");
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector("#repositoryMatterPanel[hidden]", { state: "attached" });
  await page.locator(".repository-card").filter({ hasText: "Beta Review NDA" }).click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Beta Review NDA");
  const betaReviewRequest = page.waitForRequest((request) => (
    request.method() === "POST" && request.url().includes("/api/matters/matter_beta_review/review-refresh")
  ));
  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "true");
  await assertTextContains(page.locator("#studioDocTitle"), "Beta Review NDA");
  await assertTextContains(page.locator("#studioFileMeta"), "Manual Upload matter loading review");
  await betaReviewRequest;
  releaseBetaReview();
  await assertTextContains(page.locator("#studioDocTitle"), "Beta Review NDA");
  await waitForText(page, "#studioDocumentRender", "Beta document text for the second review opening.");
  const betaRefresh = await page.evaluate(() => state.selectedMatter?.review_refresh || null);
  assert.deepEqual(betaRefresh, { refreshed: true, stale: false });

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters**");
}

async function testStaleReviewRefreshWiring(page) {
  const reviewText = "This Agreement shall be governed by the laws of India.";
  const reviewResult = {
    checked_at: "2026-06-01T09:01:00+00:00",
    clauses: [{
      decision: "pass",
      id: "governing_law",
      issue_label: "Pass",
      name: "Governing Law",
      passes: true,
      requirement: "Use an approved governing law.",
      structure_context: {},
      review_state: { state: "pass" },
      why: "Approved governing law found.",
    }],
    overall_status: "meets_requirements",
    paragraphs: [{ id: "p1", index: 1, source_index: 1, text: reviewText }],
    redline_edits: [],
    requirements_failed: 0,
    requirements_needs_review: 0,
    requirements_passed: 1,
  };
  const matter = {
    id: "matter_stale_review",
    attachment_filename: "Stale Review NDA.docx",
    board_column: "in_review",
    can_send_redline: true,
    document_title: "Stale Review NDA",
    extracted_text: reviewText,
    issue_count: 0,
    message_snippet: reviewText,
    received_at: "2026-06-01T09:00:00+00:00",
    recipient_email: "legal@example.com",
    review_result: reviewResult,
    sender: "Legal Team <legal@example.com>",
    source_filename: "Stale Review NDA.docx",
    source_type: "manual_upload",
    subject: "Stale Review NDA",
    triage_status: "approved",
    updated_at: "2026-06-01T09:01:00+00:00",
  };
  let refreshCount = 0;

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: { ready: true, email: "inbound@example.com" },
          outbound: { ready: true, email: "daniyal.ahmad@aspora.com" },
        },
      }),
    });
  });
  await page.route("**/api/matters**", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.pathname === "/api/matters" && route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ matters: [matter] }),
      });
      return;
    }
    if (requestUrl.pathname.endsWith("/stage")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ matter }),
      });
      return;
    }
    if (requestUrl.pathname.endsWith("/review-refresh")) {
      refreshCount += 1;
      const stale = refreshCount === 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          extracted_text: reviewText,
          matter,
          review_refresh: stale
            ? {
                stale: true,
                stale_message: "Active Playbook changed. Refresh review before exporting or sending.",
                stale_reasons: ["playbook_changed"],
              }
            : {
                refreshed: true,
                stale: false,
                stale_reasons: [],
              },
          review_result: reviewResult,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matter }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  await waitForText(page, "#studioFileMeta", "Active Playbook changed");
  await page.waitForSelector("#studioRefreshReviewButton:not([hidden])");
  assert.equal(await page.locator("#studioExportButton").isDisabled(), true);
  assert.equal(await page.locator("#studioSendButton").isDisabled(), true);

  await page.getByRole("button", { name: "Refresh Review" }).click();
  await waitForText(page, "#studioFileMeta", "Review refreshed against the active Playbook.");
  await page.waitForSelector("#studioRefreshReviewButton[hidden]", { state: "attached" });
  assert.equal(await page.locator("#studioExportButton").isEnabled(), true);
  assert.equal(await page.locator("#studioSendButton").isEnabled(), true);
  assert.equal(refreshCount, 2);

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters**");
}

async function testRepositoryStaleBadgeAndRefresh(page) {
  // A matter whose stored review predates the active Playbook is flagged stale in
  // the board list payload (review_stale) and in the inspector. Refreshing from the
  // inspector re-runs the review and clears the stale state.
  let staleAfterRefresh = false;
  let refreshCount = 0;
  const baseMatter = {
    id: "matter_board_stale",
    attachment_filename: "Board Stale NDA.docx",
    board_column: "in_review",
    document_title: "Board Stale NDA",
    issue_count: 0,
    message_snippet: "Confidentiality terms.",
    received_at: "2026-06-01T09:00:00+00:00",
    recipient_email: "legal@example.com",
    sender: "Legal Team <legal@example.com>",
    source_filename: "Board Stale NDA.docx",
    source_type: "manual_upload",
    subject: "Board Stale NDA",
    triage_status: "approved",
    updated_at: "2026-06-01T09:01:00+00:00",
  };
  const listMatter = () => ({
    ...baseMatter,
    review_stale: !staleAfterRefresh,
    review_stale_reasons: !staleAfterRefresh ? ["playbook_changed"] : [],
  });

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ gmail: { inbound: { ready: true }, outbound: { ready: true, email: "legal@example.com" } } }),
    });
  });
  await page.route("**/api/matters**", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.pathname === "/api/matters" && route.request().method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matters: [listMatter()] }) });
      return;
    }
    if (requestUrl.pathname.endsWith("/review-refresh")) {
      refreshCount += 1;
      staleAfterRefresh = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          extracted_text: "Confidentiality terms.",
          matter: baseMatter,
          review_refresh: { refreshed: true, stale: false, stale_reasons: [] },
          review_result: { clauses: [], overall_status: "meets_requirements" },
        }),
      });
      return;
    }
    if (requestUrl.pathname === `/api/matters/${baseMatter.id}`) {
      // Inspector open: matter detail carries the same list-level stale flag.
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matter: listMatter() }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matter: listMatter() }) });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  // Board card shows a Stale badge while the stored review is out of date.
  await page.waitForSelector(".repository-card .repository-stale-badge");
  assert.equal(await page.locator(".repository-card .repository-stale-badge").first().innerText(), "Stale");
  // Searching "stale" keeps the stale card visible.
  const search = page.locator("#repositorySearchInput");
  if (await search.count()) {
    await search.fill("stale");
    await page.waitForTimeout(150);
    assert.equal(await page.locator(".repository-card").count(), 1);
    await search.fill("");
  }

  // Open the inspector: it shows the stale notice and a Refresh Review action.
  await page.locator(".repository-card").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await page.waitForSelector(".repository-stale-notice");
  await assertTextContains(page.locator(".repository-stale-notice"), "Active Playbook changed");
  await page.waitForSelector(".repository-refresh-review");

  // Refresh clears the stale state: badge and notice disappear, message confirms.
  await page.getByRole("button", { name: "Refresh Review" }).click();
  await waitForText(page, ".repository-detail-message", "Review refreshed against the active Playbook.");
  assert.equal(refreshCount, 1);
  await page.waitForSelector(".repository-stale-notice", { state: "detached" });
  assert.equal(await page.locator(".repository-card .repository-stale-badge").count(), 0);
  assert.equal(await page.locator(".repository-refresh-review").count(), 0);

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters**");
}

async function testRepositoryLoadErrorClearsBoard(page) {
  let failMattersLoad = false;
  const matters = [
    {
      id: "matter_success_1",
      source_type: "manual_upload",
      source_filename: "Loaded NDA.docx",
      subject: "Loaded NDA",
      sender: "legal@example.com",
      message_snippet: "Previously loaded matter",
      board_column: "in_review",
      triage_status: "legal_review",
      issue_count: 1,
      created_at: "2026-06-01T12:00:00+00:00",
    },
    {
      id: "matter_success_2",
      source_type: "gmail_inbound",
      attachment_filename: "Ready NDA.docx",
      subject: "Ready NDA",
      sender: "counterparty@example.com",
      message_snippet: "Ready matter",
      board_column: "reviewed",
      triage_status: "needs_redline",
      issue_count: 2,
      created_at: "2026-06-01T12:01:00+00:00",
    },
  ];
  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ gmail: { inbound: { ready: true }, outbound: { ready: true } } }),
    });
  });
  await page.route("**/api/dashboard/search-intent**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ filters: null, fallback: true, reason: "frontend_visual_fixture" }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: failMattersLoad ? 500 : 200,
      contentType: "application/json",
      body: JSON.stringify(failMattersLoad ? { error: "Matter store is not valid JSON." } : { matters }),
    });
  });

  await page.goto(`${BASE_URL}/?v=repository-error-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await waitForRepositoryCount(page, "manual_upload", "1");
  await waitForRepositoryCount(page, "in_review", "0");
  await waitForRepositoryCount(page, "reviewed", "1");
  assert.equal(await page.locator(".repository-card").count(), 2);
  await assertTextContains(page.locator(".repository-card").filter({ hasText: "Ready NDA" }).locator(".repository-source-badge"), "Mail");

  failMattersLoad = true;
  await page.evaluate(() => repositoryController.loadMatters());
  await waitForRepositoryCount(page, "manual_upload", "0");
  await waitForRepositoryCount(page, "in_review", "0");
  await waitForRepositoryCount(page, "reviewed", "0");
  assert.equal(await page.locator(".repository-card").count(), 0);
  for (const column of ["manual_upload", "gmail_demo", "in_review", "reviewed", "sent"]) {
    await assertTextContains(page.locator(`[data-repository-list="${column}"]`), "Matter store is not valid JSON.");
  }

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
}

async function testManualUploadModal(page) {
  const docxPath = path.join(os.tmpdir(), `manual-upload-${Date.now()}.docx`);
  const reviewedDocxPath = path.join(os.tmpdir(), `manual-upload-reviewed-${Date.now()}.docx`);
  const filename = path.basename(docxPath);
  const stem = path.basename(docxPath, ".docx");
  const reviewedFilename = path.basename(reviewedDocxPath);
  const reviewedStem = path.basename(reviewedDocxPath, ".docx");
  makeDocxFixture(docxPath, [
    "This Agreement shall be governed by the laws of California.",
    "The Recipient must not circumvent the Company.",
  ]);
  makeDocxFixture(reviewedDocxPath, [
    "This Agreement shall be governed by the laws of Delaware.",
  ]);

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Submit for Review" }).click();
  await page.waitForSelector("#manualUploadModal:not([hidden])");
  assert.equal(await page.locator("#dashboardTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#manualUploadSubmitButton").isEnabled(), false);
  await assertTextContains(page.locator("#manualUploadStageLabel"), "Upload");

  await page.locator("#manualUploadFileInput").setInputFiles(docxPath);
  await assertTextContains(page.locator("#manualUploadSelectedFile"), filename);
  assert.equal(await page.locator("#manualUploadSubjectInput").inputValue(), stem);
  await page.locator("#manualUploadSenderInput").fill("counterparty@example.com");
  await page.locator("#manualUploadNoteInput").fill("Uploaded outside Gmail.");
  assert.equal(await page.locator("#manualUploadSubmitButton").isEnabled(), true);

  const firstUploadRequestPromise = page.waitForRequest((request) => (
    request.url().endsWith("/api/matters") && request.method() === "POST"
  ));
  await page.getByRole("button", { name: "Upload NDA" }).click();
  const firstUploadRequest = await firstUploadRequestPromise;
  assert.equal(firstUploadRequest.postDataJSON().board_column, "in_review");
  await page.waitForSelector("#manualUploadModal[hidden]", { state: "attached" });
  await page.waitForSelector("#repositoryView:not([hidden])");
  assert.equal(await page.locator("#repositoryTab").getAttribute("aria-selected"), "true");
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), filename);
  await assertTextContains(page.locator("#repositoryMatterPanel"), "MANUAL UPLOAD");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Upload");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "counterparty@example.com");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Uploaded outside Gmail.");
  await assertTextContains(page.locator('[data-repository-list="manual_upload"]'), stem);
  await assertTextContains(page.locator('[data-repository-list="manual_upload"] .repository-card').filter({ hasText: stem }), "Manual Upload");
  await waitForRepositoryCount(page, "manual_upload", "1");
  await waitForRepositoryCount(page, "in_review", "0");

  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  await assertTextContains(page.locator("#studioCounterpartyMeta"), "counterparty@example.com");
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector("#repositoryMatterPanel[hidden]", { state: "attached" });
  await waitForRepositoryCount(page, "manual_upload", "1");
  await waitForRepositoryCount(page, "in_review", "0");
  const uploadedCard = page.locator('[data-repository-list="manual_upload"] .repository-card').filter({ hasText: stem });
  await uploadedCard.getByRole("button", { name: "Delete matter" }).click();
  await assertTextContains(uploadedCard, "Delete matter and stored document?");
  await uploadedCard.getByRole("button", { name: "Confirm delete matter" }).click();
  await page.waitForFunction(
    (uploadedStem) => !document.querySelector('[data-repository-list="manual_upload"]')?.innerText.includes(uploadedStem),
    stem,
  );

  assert.equal(await page.getByRole("button", { name: "Add document to Upload" }).count(), 1);
  assert.equal(await page.getByRole("button", { name: "Add document to Inbox" }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Add document to In Review" }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Add document to Reviewed" }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Add document to Sent" }).count(), 0);

  await page.getByRole("button", { name: "Add document to Upload" }).click();
  await page.waitForSelector("#manualUploadModal:not([hidden])");
  await assertTextContains(page.locator("#manualUploadStageLabel"), "Upload");
  await page.locator("#manualUploadFileInput").setInputFiles(reviewedDocxPath);
  await assertTextContains(page.locator("#manualUploadSelectedFile"), reviewedFilename);
  const uploadRequestPromise = page.waitForRequest((request) => (
    request.url().endsWith("/api/matters") && request.method() === "POST"
  ));
  await page.getByRole("button", { name: "Upload NDA" }).click();
  const uploadRequest = await uploadRequestPromise;
  assert.equal(uploadRequest.postDataJSON().board_column, "in_review");
  await page.waitForSelector("#manualUploadModal[hidden]", { state: "attached" });
  await page.waitForFunction(
    (uploadedStem) => document.querySelector('[data-repository-list="manual_upload"]')?.innerText.includes(uploadedStem),
    reviewedStem,
  );
  await page.getByRole("button", { name: "Close matter inspector" }).click();
  await page.waitForSelector("#repositoryMatterPanel[hidden]", { state: "attached" });

  const reviewedCard = page.locator('[data-repository-list="manual_upload"] .repository-card').filter({ hasText: reviewedStem });
  await reviewedCard.getByRole("button", { name: "Delete matter" }).click();
  await reviewedCard.getByRole("button", { name: "Confirm delete matter" }).click();
  await page.waitForFunction(
    (uploadedStem) => !document.querySelector('[data-repository-list="manual_upload"]')?.innerText.includes(uploadedStem),
    reviewedStem,
  );

  fs.rmSync(docxPath, { force: true });
  fs.rmSync(reviewedDocxPath, { force: true });
}

async function testRepositoryOutboundSendComposer(page) {
  let matter = {
    id: "matter_send",
    attachment_filename: "Counterparty NDA.docx",
    board_column: "gmail_demo",
    can_send_redline: true,
    document_title: "Counterparty NDA",
    gmail_account: "daniyal.ahmad@aspora.com",
    issue_count: 1,
    message_snippet: "Please review the attached NDA.",
    next_action: "Review redline",
    received_at: "2026-05-31T12:00:00+00:00",
    recipient_email: "legal@example.com",
    requirements_failed: 1,
    requirements_passed: 5,
    review_result: {
      clauses: [{
        id: "governing_law",
        issue_label: "Present but wrong",
        name: "Governing Law",
        passes: false,
      }],
    },
    sender: "Legal Team <legal@example.com>",
    source_filename: "Counterparty NDA.docx",
    source_type: "gmail_inbound",
    subject: "Please review NDA",
    triage_status: "needs_redline",
  };
  let capturedSendPayload = null;

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: {
            configured: true,
            email: "daniyal.ahmad@aspora.com",
            query: 'has:attachment (filename:docx OR filename:pdf) newer_than:30d (subject:NDA OR subject:"confidentiality agreement")',
            ready: true,
          },
          outbound: {
            configured: true,
            email: "daniyal.ahmad@aspora.com",
            ready: true,
          },
        },
      }),
    });
  });
  await page.route("**/api/admin/personalisation-settings", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        personalisation: {
          sign_off: "Kind regards,",
          signature: "Daniyal Ahmad",
          signature_block: "Kind regards,\nDaniyal Ahmad\nAspora Legal",
        },
        defaults: {
          sign_off: "Best,",
          signature: "Aspora Legal",
          signature_block: "Best,\nAspora Legal",
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [matter] }),
    });
  });
  await page.route("**/api/matters/matter_send", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matter }),
    });
  });
  await page.route("**/api/gmail/send-redline", async (route) => {
    capturedSendPayload = route.request().postDataJSON();
    matter = {
      ...matter,
      board_column: "sent",
      last_outbound_account: "daniyal.ahmad@aspora.com",
      last_outbound_at: "2026-05-31T20:45:00+00:00",
      last_outbound_filename: "Counterparty-NDA-redlined.docx",
      last_outbound_message_id: "msg_outbound",
      last_outbound_subject: capturedSendPayload.subject,
      last_outbound_thread_id: "thread_outbound",
      last_outbound_to: "legal@example.com",
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        filename: "Counterparty-NDA-redlined.docx",
        matter,
        sent: {
          message_id: "msg_outbound",
          outbound_account: "daniyal.ahmad@aspora.com",
          sent_at: "2026-05-31T20:45:00+00:00",
          subject: capturedSendPayload.subject,
          thread_id: "thread_outbound",
          to: "legal@example.com",
        },
      }),
    });
  });

  const personalisationResponse = page.waitForResponse((response) => (
    response.url().endsWith("/api/admin/personalisation-settings")
    && response.request().method() === "GET"
    && response.ok()
  ));
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await personalisationResponse;
  await page.waitForFunction(() => (
    eval("state.personalisationSettings")?.signature_block === "Kind regards,\nDaniyal Ahmad\nAspora Legal"
  ));
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  const panel = page.locator("#repositoryMatterPanel");
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await panel.getByRole("button", { name: "Send Redline" }).click();
  await page.waitForSelector("#repositorySendSubject");
  await assertTextContains(panel, "daniyal.ahmad@aspora.com");
  await assertTextContains(panel, "legal@example.com");
  assert.equal(await page.locator("#repositorySendSubject").inputValue(), "Re: Please review NDA");
  assert.equal(
    await page.locator("#repositorySendBody").inputValue(),
    "Hi,\n\nPlease find attached the redlined version of Please review NDA.\n\nKind regards,\nDaniyal Ahmad\nAspora Legal",
  );

  await page.locator("#repositorySendSubject").fill("Re: Please review NDA - Aspora redline");
  await page.locator("#repositorySendBody").fill("Please see attached redline.");
  const sendRequest = page.waitForRequest((request) => request.url().endsWith("/api/gmail/send-redline"));
  await panel.getByRole("button", { name: "Confirm Send" }).click();
  await sendRequest;
  await waitForText(page, "#repositoryMatterPanel", "Sent redline to legal@example.com.");
  await waitForRepositoryCount(page, "sent", "1");

  assert.deepEqual(capturedSendPayload, {
    matter_id: "matter_send",
    confirm_send: true,
    confirm_recipient: "legal@example.com",
    subject: "Re: Please review NDA - Aspora redline",
    body: "Please see attached redline.",
  });
  await assertTextContains(panel, "LAST SENT FROM");
  await assertTextContains(panel, "daniyal.ahmad@aspora.com");
  await assertTextContains(panel, "LAST SENT TO");
  await assertTextContains(panel, "legal@example.com");

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/admin/personalisation-settings");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/matter_send");
  await page.unroute("**/api/gmail/send-redline");
}

function driveMatter() {
  return {
    id: "matter_drive",
    attachment_filename: "Counterparty NDA.docx",
    board_column: "gmail_demo",
    document_title: "Counterparty NDA",
    gmail_account: "daniyal.ahmad@aspora.com",
    issue_count: 1,
    message_snippet: "Please review the attached NDA.",
    next_action: "Review redline",
    received_at: "2026-05-31T12:00:00+00:00",
    recipient_email: "legal@example.com",
    requirements_failed: 1,
    requirements_passed: 5,
    review_result: {
      clauses: [{
        id: "governing_law",
        issue_label: "Present but wrong",
        name: "Governing Law",
        passes: false,
      }],
    },
    sender: "Legal Team <legal@example.com>",
    source_filename: "Counterparty NDA.docx",
    source_type: "gmail_inbound",
    subject: "Please review NDA",
    triage_status: "needs_redline",
  };
}

async function routeDriveBoard(page, matter) {
  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: { configured: true, email: "daniyal.ahmad@aspora.com", ready: true },
          outbound: { configured: true, email: "daniyal.ahmad@aspora.com", ready: true },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [matter] }),
    });
  });
  await page.route("**/api/matters/matter_drive", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matter }),
    });
  });
}

async function testRepositorySaveToDriveSuccess(page) {
  const matter = driveMatter();
  let capturedUploadPayload = null;
  await routeDriveBoard(page, matter);
  // Drive v2: the endpoint SYNCS the matter's artifact history into a per-matter
  // folder and returns a folder link + the list of synced files.
  await page.route("**/api/drive/upload-matter", async (route) => {
    capturedUploadPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        drive: {
          matter_folder_id: "folder_matter_drive",
          matter_folder_url: "https://drive.google.com/drive/folders/folder_matter_drive",
          synced_count: 2,
          total_count: 3,
          artifacts: [
            {
              artifact_id: "art_1",
              sequence: 1,
              actor: "counterparty",
              role: "original",
              version: 1,
              filename: "Counterparty NDA.docx",
              drive_file_id: "drive_file_1",
              drive_file_url: "https://drive.google.com/file/d/drive_file_1/view",
              based_on_artifact_id: null,
              created_at: "2026-05-31T12:00:00+00:00",
            },
            {
              artifact_id: "art_2",
              sequence: 2,
              actor: "reviewer",
              role: "reviewed",
              version: 1,
              filename: "Counterparty NDA (redline).docx",
              drive_file_id: "drive_file_2",
              drive_file_url: "https://drive.google.com/file/d/drive_file_2/view",
              based_on_artifact_id: "art_1",
              created_at: "2026-06-01T09:00:00+00:00",
            },
          ],
        },
        matter: { ...matter, board_column: "gmail_demo" },
      }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  const panel = page.locator("#repositoryMatterPanel");
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");

  const uploadRequest = page.waitForRequest((request) => request.url().endsWith("/api/drive/upload-matter"));
  await panel.getByRole("button", { name: "Save to Drive" }).click();
  await uploadRequest;
  await waitForText(page, "#repositoryMatterPanel", "Synced 2 files to Drive");

  assert.deepEqual(capturedUploadPayload, { matter_id: "matter_drive" });

  // Prominent "Open matter folder" link -> matter_folder_url, new tab + noopener.
  const folderLink = panel.locator(".repository-detail-message a.repository-drive-folder-link");
  assert.equal(await folderLink.count(), 1);
  assert.equal(
    await folderLink.getAttribute("href"),
    "https://drive.google.com/drive/folders/folder_matter_drive",
  );
  assert.equal(await folderLink.getAttribute("target"), "_blank");
  assert.equal(await folderLink.getAttribute("rel"), "noopener");
  await assertTextContains(folderLink, "Open matter folder");

  // Compact per-file list: filename -> drive_file_url for each synced artifact.
  const fileLinks = panel.locator(".repository-detail-message a.repository-drive-file-link");
  assert.equal(await fileLinks.count(), 2);
  assert.equal(
    await fileLinks.nth(0).getAttribute("href"),
    "https://drive.google.com/file/d/drive_file_1/view",
  );
  await assertTextContains(fileLinks.nth(0), "Counterparty NDA.docx");
  assert.equal(
    await fileLinks.nth(1).getAttribute("href"),
    "https://drive.google.com/file/d/drive_file_2/view",
  );
  await assertTextContains(fileLinks.nth(1), "Counterparty NDA (redline).docx");

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/matter_drive");
  await page.unroute("**/api/drive/upload-matter");
}

async function testRepositorySaveToDriveUpToDate(page) {
  const matter = driveMatter();
  await routeDriveBoard(page, matter);
  // synced_count == 0: nothing new to upload; the folder is already current.
  await page.route("**/api/drive/upload-matter", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        drive: {
          matter_folder_id: "folder_matter_drive",
          matter_folder_url: "https://drive.google.com/drive/folders/folder_matter_drive",
          synced_count: 0,
          total_count: 3,
          artifacts: [],
        },
        matter: { ...matter, board_column: "gmail_demo" },
      }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  const panel = page.locator("#repositoryMatterPanel");
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");

  const uploadRequest = page.waitForRequest((request) => request.url().endsWith("/api/drive/upload-matter"));
  await panel.getByRole("button", { name: "Save to Drive" }).click();
  await uploadRequest;
  await waitForText(page, "#repositoryMatterPanel", "Matter folder up to date");

  // Still offers the folder link, but no per-file list when nothing synced.
  const folderLink = panel.locator(".repository-detail-message a.repository-drive-folder-link");
  assert.equal(await folderLink.count(), 1);
  assert.equal(
    await folderLink.getAttribute("href"),
    "https://drive.google.com/drive/folders/folder_matter_drive",
  );
  assert.equal(await panel.locator(".repository-detail-message a.repository-drive-file-link").count(), 0);

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/matter_drive");
  await page.unroute("**/api/drive/upload-matter");
}

async function testRepositorySaveToDriveNotConnected(page) {
  const matter = driveMatter();
  await routeDriveBoard(page, matter);
  await page.route("**/api/drive/upload-matter", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        error: "Google Drive is not connected.",
        needs_connect: true,
        connect_url: "/auth/drive/start",
      }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  const panel = page.locator("#repositoryMatterPanel");
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");

  const uploadRequest = page.waitForRequest((request) => request.url().endsWith("/api/drive/upload-matter"));
  await panel.getByRole("button", { name: "Save to Drive" }).click();
  await uploadRequest;
  // Do NOT navigate: assert the Connect affordance + its connect_url are present.
  await waitForText(page, "#repositoryMatterPanel", "not connected");
  const connectLink = panel.locator(".repository-detail-message a.repository-drive-connect");
  assert.equal(await connectLink.count(), 1);
  assert.equal(await connectLink.getAttribute("href"), "/auth/drive/start");
  assert.equal(await connectLink.getAttribute("data-drive-connect-url"), "/auth/drive/start");
  await assertTextContains(connectLink, "Connect Google Drive");

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/matter_drive");
  await page.unroute("**/api/drive/upload-matter");
}

async function testAdminDriveSection(page) {
  let connected = false;
  const driveSettingsPayloads = [];
  const driveStatusBody = () => (connected
    ? {
      connected: true,
      account: "legal-bot@aspora.com",
      folder: { id: "folder_abc", name: "NDA Vault" },
      enabled: true,
    }
    : {
      connected: false,
      account: "alice@example.com",
      folder: null,
      enabled: false,
      connect_url: "/auth/drive/start",
      needs_connect: true,
      signed_in: true,
      user_scoped: true,
      token: {
        configured: false,
        label: "Connect Google for drive",
        source: "missing",
        scope_status: {
          missing: ["https://www.googleapis.com/auth/drive.file"],
          ok: false,
          required: ["https://www.googleapis.com/auth/drive.file"],
        },
      },
      setup: {
        action: "connect_google",
        connect_url: "/auth/drive/start",
        google_oauth_configured: true,
        message: "Connect Drive for the signed-in Google account.",
        signed_in: true,
        state: "ready_to_connect",
      },
      recovery: {
        action: "connect_google",
        connect_url: "/auth/drive/start",
        message: "Connect Drive to create a drive token for this account.",
        scope_status: {
          missing: ["https://www.googleapis.com/auth/drive.file"],
          ok: false,
          required: ["https://www.googleapis.com/auth/drive.file"],
        },
        state: "missing_token",
      },
    });

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: { configured: true, email: "inbound@example.com", ready: true },
          outbound: { configured: true, email: "outbound@example.com", ready: true },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [] }),
    });
  });
  await page.route("**/api/drive/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(driveStatusBody()),
    });
  });
  await page.route("**/api/admin/drive-settings", async (route) => {
    const payload = route.request().postDataJSON();
    driveSettingsPayloads.push(payload);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        drive: {
          enabled: payload.enabled !== undefined ? payload.enabled : true,
          folder_id: payload.folder_id !== undefined ? payload.folder_id : "folder_abc",
          folder_name: payload.folder_name !== undefined ? payload.folder_name : "NDA Vault",
        },
      }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Admin" }).click();

  // Disconnected status: Connect affordance + Not connected facts.
  // The overall status pill is CSS-uppercased, so match the rendered text.
  await page.locator('[data-admin-section="drive"]').click();
  await page.waitForSelector("#adminDrivePanel:not([hidden])");
  await waitForText(page, "#adminDriveOverall", "NEEDS DRIVE ACCESS");
  await assertTextContains(page.locator("#adminDrivePanel"), "Google Drive uploads");
  // Drive v2 relabel: the folder setting is the optional NDAs root + helper copy.
  // The subsection <h3> is CSS-uppercased, so match the rendered text.
  await assertTextContains(page.locator("#adminDrivePanel"), "NDAS ROOT FOLDER (OPTIONAL)");
  await assertTextContains(page.locator('[data-admin-drive="folder-help"]'), "{counterparty}/{matter}");
  // The Drive toggle is now the whole connect/disconnect control: there is no
  // separate Connect button, and the toggle reads Off while disconnected.
  assert.equal(await page.locator("#adminDriveConnectPanel a.integration-connection-action").count(), 0);
  assert.equal(await page.locator("#adminDriveEnabledToggle").getAttribute("aria-checked"), "false");
  assertAttributeMatches(page.locator("#adminDriveEnabledToggle"), "aria-label", /Connect Google Drive/);
  await assertTextContains(page.locator("#adminDriveFacts"), "Needs Drive access");
  await assertTextContains(page.locator("#adminDriveFacts"), "alice@example.com");
  await assertTextContains(page.locator("#adminDriveConnectPanel"), "Connect Drive to create a drive token");
  await assertTextContains(page.locator("#adminDriveConnectPanel"), "Missing: Connect Google for drive");
  await assertTextContains(page.locator("#adminDriveConnectPanel"), "https://www.googleapis.com/auth/drive.file");

  // Save a target folder; assert the POST payload.
  await page.locator("#adminDriveFolderIdInput").fill("folder_xyz");
  await page.locator("#adminDriveFolderNameInput").fill("Signed NDAs");
  const settingsRequest = page.waitForRequest((request) => request.url().endsWith("/api/admin/drive-settings"));
  await page.locator("#adminDriveFolderSaveButton").click();
  await settingsRequest;
  await waitForText(page, "#adminDrivePanel", "NDAs root folder saved.");
  assert.deepEqual(driveSettingsPayloads[driveSettingsPayloads.length - 1], {
    folder_id: "folder_xyz",
    folder_name: "Signed NDAs",
  });

  // Connected status: account + folder render after a refresh.
  connected = true;
  await page.locator("#adminDriveRefreshButton").click();
  await waitForText(page, "#adminDriveOverall", "CONNECTED");
  await assertTextContains(page.locator("#adminDriveFacts"), "legal-bot@aspora.com");
  await assertTextContains(page.locator("#adminDriveFacts"), "NDA Vault");
  await assertTextContains(page.locator("#adminDriveConnectPanel"), "legal-bot@aspora.com");
  assert.equal(await page.locator("#adminDriveEnabledToggle").getAttribute("aria-checked"), "true");

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/drive/status");
  await page.unroute("**/api/admin/drive-settings");
}

async function testAdminPersonalisationSection(page) {
  let savedPayload = null;
  let settings = {
    sign_off: "Kind regards,",
    signature: "Daniyal",
    signature_block: "Kind regards,\nDaniyal\nAspora Legal",
  };

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: { configured: true, email: "inbound@example.com", ready: true },
          outbound: { configured: true, email: "outbound@example.com", ready: true },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [] }),
    });
  });
  await page.route("**/api/admin/personalisation-settings", async (route) => {
    if (route.request().method() === "POST") {
      savedPayload = route.request().postDataJSON();
      settings = { ...savedPayload };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ personalisation: settings }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        personalisation: settings,
        defaults: {
          sign_off: "Best,",
          signature: "Aspora Legal",
          signature_block: "Best,\nAspora Legal",
        },
      }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Admin" }).click();
  await page.locator('[data-admin-section="personalisation"]').click();
  await page.waitForSelector("#adminPersonalisationPanel:not([hidden])");
  await waitForText(page, "#adminPersonalisationOverall", "READY");
  await assertTextContains(page.locator("#adminPersonalisationPanel"), "Email and document sign-off");
  await assertTextContains(page.locator("#adminPersonalisationPanel"), "SIGN-OFF");
  await assertTextContains(page.locator("#adminPersonalisationPanel"), "SIGNATURE");
  await assertTextContains(page.locator("#adminPersonalisationPanel"), "SIGNATURE BLOCK");
  assert.equal(await page.locator("#adminSignOffInput").inputValue(), "Kind regards,");
  assert.equal(await page.locator("#adminSignatureInput").inputValue(), "Daniyal");
  assert.equal(await page.locator("#adminSignatureBlockInput").inputValue(), "Kind regards,\nDaniyal\nAspora Legal");
  assert.equal(await page.locator("#adminPersonalisationSaveButton").isDisabled(), true);

  await page.locator("#adminSignOffInput").fill("Warm regards,");
  await page.locator("#adminSignatureInput").fill("Daniyal Ahmad");
  await page.locator("#adminSignatureBlockInput").fill("Warm regards,\nDaniyal Ahmad\nAspora");
  assert.equal(await page.locator("#adminPersonalisationSaveButton").isEnabled(), true);
  const saveRequest = page.waitForRequest((request) => request.url().endsWith("/api/admin/personalisation-settings") && request.method() === "POST");
  await page.locator("#adminPersonalisationSaveButton").click();
  await saveRequest;
  await waitForText(page, "#adminPersonalisationMessage", "Personalisation settings saved.");
  assert.deepEqual(savedPayload, {
    sign_off: "Warm regards,",
    signature: "Daniyal Ahmad",
    signature_block: "Warm regards,\nDaniyal Ahmad\nAspora",
  });

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/admin/personalisation-settings");
}

async function testReviewOutboundSendModal(page) {
  let matter = {
    id: "matter_review_send",
    attachment_filename: "Counterparty NDA.docx",
    board_column: "gmail_demo",
    can_send_redline: true,
    document_title: "Counterparty NDA",
    gmail_account: "daniyal.ahmad@aspora.com",
    has_redline_draft: true,
    human_reviewed: true,
    issue_count: 1,
    message_snippet: "Please review the attached NDA.",
    next_action: "Review redline",
    received_at: "2026-05-31T12:00:00+00:00",
    recipient_email: "legal@example.com",
    requirements_failed: 1,
    requirements_passed: 5,
    review_result: {
      clauses: [{
        id: "confidential_information",
        issue_label: "Present but wrong",
        name: "Confidential Information",
        passes: false,
      }],
    },
    sender: "Legal Team <legal@example.com>",
    source_filename: "Counterparty NDA.docx",
    source_type: "gmail_inbound",
    subject: "Please review NDA",
    triage_status: "needs_redline",
  };
  const reviewResult = {
    clauses: [{
      evidence: [{ paragraph_id: "p1", text: "Confidential Information only includes marked information." }],
      id: "confidential_information",
      issue_label: "Present but wrong",
      matched_paragraph_ids: ["p1"],
      name: "Confidential Information",
      passes: false,
      requirement: "Confidential Information must be broad.",
      why: "The definition is too narrow.",
    }],
    paragraphs: [
      {
        id: "p1",
        index: 1,
        source_index: 1,
        text: "Confidential Information only includes marked information.",
      },
      {
        id: "p2",
        index: 2,
        source_index: 2,
        text: "Payment terms remain unchanged.",
      },
    ],
    redline_edits: [{
      action: "replace_paragraph",
      action_label: "Replace paragraph",
      clause_id: "confidential_information",
      id: "redline-confidential-information",
      original_text: "Confidential Information only includes marked information.",
      paragraph_id: "p1",
      paragraph_index: 1,
      replacement_text: "Confidential Information means all non-public business, technical, financial, customer, pricing, product, and source code information.",
      status: "proposed",
    }],
  };
  const redlineDraft = {
    manual_redline_edits: [{
      action: "replace_paragraph",
      action_label: "Replace paragraph",
      clause_id: "manual_viewer_edit",
      id: "manual-p2",
      original_text: "Payment terms remain unchanged.",
      paragraph_id: "p2",
      paragraph_index: 2,
      replacement_text: "Payment terms include a 30-day review period.",
      status: "proposed",
    }],
    review_comments: [{
      author: "Reviewer",
      clause_id: "confidential_information",
      clause_name: "Confidential Information",
      id: "comment-confidential-information",
      paragraph_id: "p1",
      paragraph_index: 1,
      scope: "clause",
      text: "Please confirm the carve-outs are acceptable.",
    }],
  };
  let capturedSendPayload = null;
  let sendAttempts = 0;

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          inbound: {
            configured: true,
            email: "daniyal.ahmad@aspora.com",
            query: 'has:attachment (filename:docx OR filename:pdf) newer_than:30d (subject:NDA)',
            ready: true,
          },
          outbound: {
            configured: true,
            email: "daniyal.ahmad@aspora.com",
            ready: true,
          },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [matter] }),
    });
  });
  await page.route("**/api/matters/matter_review_send**", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.pathname.endsWith("/stage")) {
      matter = { ...matter, board_column: "in_review" };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ matter }),
      });
      return;
    }
    if (requestUrl.pathname.endsWith("/review") || requestUrl.pathname.endsWith("/review-refresh")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          extracted_text: reviewResult.paragraphs.map((paragraph) => paragraph.text).join("\n\n"),
          matter: {
            ...matter,
            redline_draft: redlineDraft,
            review_result: reviewResult,
          },
          redline_draft: redlineDraft,
          review_result: reviewResult,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matter }),
    });
  });
  await page.route("**/api/gmail/send-redline", async (route) => {
    capturedSendPayload = route.request().postDataJSON();
    sendAttempts += 1;
    if (sendAttempts === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "Gmail send unavailable." }),
      });
      return;
    }
    matter = {
      ...matter,
      board_column: "sent",
      last_outbound_account: "daniyal.ahmad@aspora.com",
      last_outbound_at: "2026-05-31T20:45:00+00:00",
      last_outbound_filename: "Counterparty-NDA-redlined.docx",
      last_outbound_message_id: "msg_outbound",
      last_outbound_subject: capturedSendPayload.subject,
      last_outbound_thread_id: "thread_outbound",
      last_outbound_to: "legal@example.com",
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        filename: "Counterparty-NDA-redlined.docx",
        matter,
        sent: {
          message_id: "msg_outbound",
          outbound_account: "daniyal.ahmad@aspora.com",
          sent_at: "2026-05-31T20:45:00+00:00",
          subject: capturedSendPayload.subject,
          thread_id: "thread_outbound",
          to: "legal@example.com",
        },
      }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  await page.waitForSelector("#studioSendButton:not(:disabled):not(.blocked)");
  assert.equal(await page.locator("#studioExportPdfButton").count(), 0);
  await assertTextContains(page.locator("#studioSendButton"), "Send Redline");
  const initialSendButtonBox = await page.locator("#studioSendButton").boundingBox();
  assert.ok(initialSendButtonBox && initialSendButtonBox.width >= 96, "send button should keep a stable text-button width");

  await page.locator("#studioSendButton").click();
  await page.waitForSelector("#studioSendModal:not([hidden])");
  assert.equal(await page.locator("#studioSendTo").inputValue(), "legal@example.com");
  assert.equal(await page.locator("#studioSendFrom").innerText(), "daniyal.ahmad@aspora.com");
  assert.equal(await page.locator("#studioSendAttachment").innerText(), "Counterparty-NDA-redlined.docx");
  assert.equal(
    await page.locator("#studioSendSubject").inputValue(),
    "Redline for Counterparty NDA",
  );
  const defaultBody = await page.locator("#studioSendBody").inputValue();
  assert.ok(defaultBody.includes("Confidential Information"), defaultBody);
  assert.ok(defaultBody.includes("Payment terms include a 30-day review period."), defaultBody);
  assert.ok(defaultBody.includes("Please confirm the carve-outs are acceptable."), defaultBody);
  await assertTextContains(page.locator("#studioSendSummary"), "1 included clause redline");
  await assertTextContains(page.locator("#studioSendSummary"), "1 manual viewer edit");
  await assertTextContains(page.locator("#studioSendSummary"), "1 Word comment");

  await page.locator("#studioSendSubject").fill("Edited redline subject");
  await page.locator("#studioSendBody").fill("Edited body before sending.");
  await page.locator("#studioSendConfirmButton").click();
  await page.waitForSelector("#studioSendModal:not([hidden])");
  await waitForText(page, "#studioSendStatus", "Gmail send unavailable.");
  await assertTextContains(page.locator("#studioSendButton"), "Send Redline");
  assert.equal(await page.locator("#studioSendButton.icon-only").count(), 0);
  const failedSendButtonBox = await page.locator("#studioSendButton").boundingBox();
  assert.ok(failedSendButtonBox && failedSendButtonBox.width >= 96, "send button should remain visible after a failed send");

  const sendRequest = page.waitForRequest((request) => request.url().endsWith("/api/gmail/send-redline"));
  await page.locator("#studioSendConfirmButton").click();
  await sendRequest;
  await page.waitForSelector("#studioSendModal[hidden]", { state: "attached" });
  await waitForText(page, "#studioFileMeta", "Sent redline to legal@example.com");

  assert.equal(capturedSendPayload.matter_id, "matter_review_send");
  assert.equal(capturedSendPayload.confirm_send, true);
  assert.equal(capturedSendPayload.confirm_recipient, "legal@example.com");
  assert.equal(capturedSendPayload.to, "legal@example.com");
  assert.equal(capturedSendPayload.subject, "Edited redline subject");
  assert.equal(capturedSendPayload.body, "Edited body before sending.");
  assert.equal(capturedSendPayload.export_redline_edits.length, 1);
  assert.equal(capturedSendPayload.manual_redline_edits.length, 1);
  assert.equal(capturedSendPayload.review_comments.length, 1);
  assert.equal(capturedSendPayload.review_comments[0].text, "Please confirm the carve-outs are acceptable.");

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/matter_review_send**");
  await page.unroute("**/api/gmail/send-redline");
}

async function testRepositoryOutboundSendBlocked(page) {
  const matter = {
    id: "matter_blocked_send",
    attachment_filename: "Blocked NDA.docx",
    board_column: "gmail_demo",
    can_send_redline: true,
    document_title: "Blocked NDA",
    gmail_account: "daniyal.ahmad@aspora.com",
    issue_count: 1,
    message_snippet: "Please review the attached NDA.",
    next_action: "Review redline",
    received_at: "2026-05-31T12:00:00+00:00",
    recipient_email: "legal@example.com",
    requirements_failed: 1,
    requirements_passed: 5,
    review_result: { clauses: [] },
    sender: "Legal Team <legal@example.com>",
    source_filename: "Blocked NDA.docx",
    source_type: "gmail_inbound",
    subject: "Please review NDA",
    triage_status: "needs_redline",
  };
  let sendAttempted = false;

  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          account_match: false,
          inbound: {
            configured: true,
            email: "daniyal.ahmad@aspora.com",
            query: 'has:attachment (filename:docx OR filename:pdf) newer_than:30d (subject:NDA)',
            ready: true,
          },
          outbound: {
            configured: true,
            email: "personal@example.com",
            error: "Outbound Gmail account personal@example.com does not match inbound Gmail account daniyal.ahmad@aspora.com.",
            ready: false,
          },
          settings: {
            inbound_enabled: true,
            outbound_enabled: true,
            sync_frequency: "10_minutes",
          },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [matter] }),
    });
  });
  await page.route("**/api/matters/matter_blocked_send", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matter }),
    });
  });
  await page.route("**/api/gmail/send-redline", async (route) => {
    sendAttempted = true;
    await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "should not send" }) });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  const panel = page.locator("#repositoryMatterPanel");
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(panel, "OUTBOUND STATUS");
  await assertTextContains(panel, "does not match inbound Gmail account");
  const sendButton = panel.getByRole("button", { name: "Account Mismatch" });
  assert.equal(await sendButton.isEnabled(), false);
  assert.equal(sendAttempted, false);

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/matter_blocked_send");
  await page.unroute("**/api/gmail/send-redline");
}

async function testGmailSetupRequiredStatus(page) {
  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          settings: {
            inbound_enabled: true,
            outbound_enabled: true,
            last_sync_at: "2026-06-01T13:08:23+00:00",
            last_sync_imported_count: 0,
            last_sync_skipped_count: 0,
            sync_frequency: "always_on",
            sync_history: [{
              deduplicated_count: 0,
              duplicate_count: 0,
              error: "Set NDA_GMAIL_INBOUND_TOKEN_PATH for the inbound Gmail account.",
              finished_at: "2026-06-01T13:08:23+00:00",
              imported_count: 0,
              query: "in:inbox has:attachment",
              review_failed_count: 0,
              skipped_count: 0,
              started_at: "2026-06-01T13:08:23+00:00",
              status: "error",
            }],
          },
          setup: {
            action: "configure_google_oauth",
            connect_url: "/auth/google/start",
            google_oauth_configured: false,
            message: "Google OAuth is not configured. Set NDA_GOOGLE_OAUTH_CLIENT_ID and NDA_GOOGLE_OAUTH_CLIENT_SECRET, then restart the app.",
            signed_in: false,
            state: "missing_oauth_config",
          },
          inbound: {
            configured: false,
            enabled: true,
            error: "Set NDA_GMAIL_INBOUND_TOKEN_PATH for the inbound Gmail account.",
            recovery: {
              action: "configure_google_oauth",
              connect_url: "/auth/google/start",
              message: "Google OAuth is not configured. Set NDA_GOOGLE_OAUTH_CLIENT_ID and NDA_GOOGLE_OAUTH_CLIENT_SECRET, then restart the app.",
              state: "missing_oauth_config",
            },
            query: "in:inbox has:attachment",
            ready: false,
            token: {
              configured: false,
              label: "NDA_GMAIL_INBOUND_TOKEN_PATH or data/gmail/inbound-token.json",
              source: "missing",
              scope_status: {
                missing: ["https://www.googleapis.com/auth/gmail.readonly"],
                ok: false,
                required: ["https://www.googleapis.com/auth/gmail.readonly"],
              },
            },
          },
          outbound: {
            configured: false,
            enabled: true,
            error: "Set NDA_GMAIL_OUTBOUND_TOKEN_PATH for the outbound Gmail account.",
            recovery: {
              action: "configure_google_oauth",
              connect_url: "/auth/google/start",
              message: "Google OAuth is not configured. Set NDA_GOOGLE_OAUTH_CLIENT_ID and NDA_GOOGLE_OAUTH_CLIENT_SECRET, then restart the app.",
              state: "missing_oauth_config",
            },
            ready: false,
            token: {
              configured: false,
              label: "NDA_GMAIL_OUTBOUND_TOKEN_PATH or data/gmail/outbound-token.json",
              source: "missing",
              scope_status: {
                missing: ["https://www.googleapis.com/auth/gmail.send"],
                ok: false,
                required: ["https://www.googleapis.com/auth/gmail.send"],
              },
            },
          },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [] }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => (
    document.querySelector('[data-dashboard-health="email"]')?.getAttribute("aria-label")?.includes("Google OAuth not configured")
  ));
  assertAttributeMatches(page.locator('[data-dashboard-health="email"]'), "aria-label", /Google OAuth not configured/);
  await page.getByRole("tab", { name: "Repository" }).click();
  const syncStatus = page.locator("[data-repository-sync-status]");
  await assertTextContains(syncStatus, "Gmail inbound setup required");
  assert.equal((await syncStatus.innerText()).includes("Last sync error"), false);

  await page.getByRole("tab", { name: "Admin" }).click();
  await page.locator('[data-admin-section="email"]').click();
  await waitForText(page, "#adminGmailOverall", "NEEDS SETUP");
  const adminPanel = page.locator("#adminIntegrationsPanel");
  await assertTextContains(adminPanel, "NEEDS SETUP");
  await assertTextContains(adminPanel, "Gmail inbound setup required");
  await assertTextContains(adminPanel, "Missing: NDA_GMAIL_INBOUND_TOKEN_PATH or data/gmail/inbound-token.json");
  await assertTextContains(adminPanel, "Google OAuth is not configured. Set NDA_GOOGLE_OAUTH_CLIENT_ID");
  assertAttributeMatches(page.locator("#adminGmailEnabledToggle"), "aria-label", /Gmail enabled; setup required/);

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
}

async function testUserGmailSessionControls(page) {
  const gmailStatusRoute = "**/api/gmail/status*";
  const buildGmailStatus = ({ ready = true, imported = 3, skipped = 1, syncedAt = "2026-06-04T18:00:00+00:00" } = {}) => ({
    user_scoped: true,
    connect_url: "/auth/gmail/start",
    disconnect_url: "/api/gmail/disconnect",
    sync: {
      last_sync_at: syncedAt,
      last_sync_imported_count: imported,
      last_sync_skipped_count: skipped,
      sync_history: [{
        deduplicated_count: 1,
        duplicate_count: 0,
        error: "",
        finished_at: syncedAt,
        imported_count: imported,
        query: 'has:attachment newer_than:30d ("NDA" OR "non-disclosure agreement")',
        review_failed_count: 0,
        skipped_count: skipped,
        started_at: syncedAt,
        status: "success",
      }],
    },
    inbound: {
      configured: ready,
      connect_url: "/auth/gmail/start?role=inbound",
      email: "alice@example.com",
      enabled: true,
      ready,
      token: ready
        ? { configured: true, label: "alice@example.com", source: "user_data" }
        : { configured: false, label: "Connect Gmail for inbound", source: "missing" },
    },
    outbound: {
      configured: ready,
      connect_url: "/auth/gmail/start?role=outbound",
      email: ready ? "alice@example.com" : "",
      enabled: true,
      ready,
      token: ready
        ? { configured: true, label: "alice@example.com", source: "user_data" }
        : { configured: false, label: "Connect Gmail for outbound", source: "missing" },
    },
  });
  let gmailStatus = buildGmailStatus();
  let disconnectPayload = null;

  await page.route("**/api/auth/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: true,
        google_oauth_configured: true,
        login_url: "/auth/google/start",
        logout_url: "/api/auth/logout",
        user: { email: "alice@example.com", id: "user_alice", name: "Alice Reviewer" },
      }),
    });
  });
  await page.route("**/api/deployment/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        deployment: {
          status: "needs_attention",
          checks: [
            { id: "allowed_hosts", ok: false, message: "Set NDA_ALLOWED_HOSTS to the deployed Render hostname." },
            { id: "data_dir", ok: true, message: "Persistent data directory configured." },
          ],
        },
      }),
    });
  });
  await page.route(gmailStatusRoute, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ gmail: gmailStatus }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [] }),
    });
  });
  await page.route("**/api/gmail/import", async (route) => {
    gmailStatus = buildGmailStatus({ imported: 4, skipped: 0, syncedAt: "2026-06-04T18:10:00+00:00" });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: gmailStatus,
        result: { imported: [{ id: "matter_sync_1" }], imported_count: 4, skipped_count: 0 },
      }),
    });
  });
  await page.route("**/api/gmail/disconnect", async (route) => {
    disconnectPayload = route.request().postDataJSON();
    gmailStatus = buildGmailStatus({ ready: false, imported: 4, skipped: 0, syncedAt: "2026-06-04T18:10:00+00:00" });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ disconnected: ["inbound", "outbound"], gmail: gmailStatus }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await waitForText(page, "[data-session-user]", "Hi, Alice!");
  await assertTextContains(page.locator("#sessionStrip"), "Set NDA_ALLOWED_HOSTS to the deployed Render hostname.");
  await page.locator("[data-session-account-toggle]").click();
  await page.locator("[data-session-account-menu]").waitFor({ state: "visible" });
  await page.locator("[data-session-gmail-sync]").waitFor({ state: "visible" });
  assert.equal(await page.locator("[data-session-gmail-sync]").isVisible(), true);
  assert.equal(await page.locator("[data-session-gmail-connect]").isVisible(), false);

  const syncRequestPromise = page.waitForRequest((request) => request.url().endsWith("/api/gmail/import"));
  await page.locator("[data-session-gmail-sync]").click();
  const syncRequest = await syncRequestPromise;
  assert.deepEqual(syncRequest.postDataJSON(), { limit: 25 });

  await page.getByRole("tab", { name: "Repository" }).click();
  await waitForText(page, "[data-repository-sync-status]", "Your last sync");
  await assertTextContains(page.locator("[data-repository-sync-status]"), "4 imported / 0 skipped");

  await page.getByRole("tab", { name: "Admin" }).click();
  await page.locator('[data-admin-section="email"]').click();
  await waitForText(page, "#adminGmailSyncHistory", "4 imported / 0 skipped");
  await assertTextContains(page.locator("#adminGmailSetupPanel"), "User Gmail: alice@example.com");
  // The single Gmail toggle is now the whole connect/disconnect control: it reads
  // On when connected, and there are no separate Connect/Disconnect buttons in the
  // setup panel (the per-role rows stay as read-only status).
  assert.equal(await page.locator("#adminGmailEnabledToggle").getAttribute("aria-checked"), "true");
  assert.equal(await page.locator("#adminGmailSetupPanel [data-gmail-disconnect-role]").count(), 0);
  await assertTextContains(page.locator("#adminGmailSyncHistory"), "4 imported / 0 skipped / 0 duplicates / 1 stale duplicates removed / 0 review failures");

  const disconnectRequestPromise = page.waitForRequest((request) => request.url().endsWith("/api/gmail/disconnect"));
  await page.locator("[data-session-account-toggle]").click();
  await page.locator("[data-session-gmail-disconnect]").click();
  await disconnectRequestPromise;
  assert.deepEqual(disconnectPayload, { role: "all" });
  await waitForText(page, "[data-session-gmail]", "Gmail needs connection");
  assert.equal(await page.locator("[data-session-gmail-connect]").isVisible(), true);
  assert.equal(await page.locator("[data-session-gmail-sync]").isVisible(), false);

  await page.unroute("**/api/auth/status");
  await page.unroute("**/api/deployment/status");
  await page.unroute(gmailStatusRoute);
  await page.unroute("**/api/matters");
  await page.unroute("**/api/gmail/import");
  await page.unroute("**/api/gmail/disconnect");
}

async function testSharedGmailProfileAccountMenu(page) {
  const gmailStatusRoute = "**/api/gmail/status*";
  const avatarUrl = [
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='1' height='1'%3E",
    "%3Crect width='1' height='1' fill='%230f766e'/%3E%3C/svg%3E",
  ].join("");

  await page.route("**/api/auth/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: false,
        google_oauth_configured: false,
        login_url: "",
        logout_url: "/api/auth/logout",
        user: null,
      }),
    });
  });
  await page.route("**/api/deployment/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ deployment: { status: "ok", checks: [] } }),
    });
  });
  await page.route(gmailStatusRoute, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        gmail: {
          user_scoped: false,
          profile: {
            name: "Daniyal Ahmad",
            email: "daniyal.ahmad@aspora.com",
            picture: avatarUrl,
          },
          inbound: {
            ready: true,
            email: "daniyal.ahmad@aspora.com",
            token: { configured: true, label: "Shared inbound", source: "settings" },
          },
          outbound: {
            ready: true,
            email: "daniyal.ahmad@aspora.com",
            token: { configured: true, label: "Shared outbound", source: "settings" },
          },
        },
      }),
    });
  });
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters: [] }),
    });
  });

  const gmailStatusLoaded = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/gmail/status" && response.status() === 200;
  });
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await gmailStatusLoaded;
  await waitForText(page, "[data-session-gmail]", "Shared Gmail configured");
  await page.waitForSelector("[data-session-avatar-image]:not([hidden])");
  await page.waitForFunction(() => (
    document.querySelector("[data-session-avatar-image]")?.getAttribute("src")?.startsWith("data:image/svg+xml,")
  ));
  assert.equal(await page.locator("[data-session-avatar-image]").getAttribute("src"), avatarUrl);
  assert.equal(await page.locator("[data-session-avatar-initial]").isVisible(), false);
  await page.locator("[data-session-account-toggle]").click();
  await assertTextContains(page.locator("[data-session-account-menu]"), "Hi, Daniyal!");
  await assertTextContains(page.locator("[data-session-account-menu]"), "Shared Gmail configured");
  await assertTextContains(page.locator("[data-session-account-menu]"), "Sign out");
  assert.equal(await page.locator("[data-session-menu-avatar-image]").getAttribute("src"), avatarUrl);
  await page.locator("[data-session-avatar-image]").evaluate((node) => node.dispatchEvent(new Event("error")));
  await page.locator("[data-session-menu-avatar-image]").evaluate((node) => node.dispatchEvent(new Event("error")));
  assert.equal(await page.locator("[data-session-avatar-image]").isVisible(), false);
  assert.equal(await page.locator("[data-session-avatar-image]").getAttribute("src"), null);
  assert.equal(await page.locator("[data-session-avatar-initial]").isVisible(), true);
  assert.equal(await page.locator("[data-session-menu-avatar-image]").isVisible(), false);
  assert.equal(await page.locator("[data-session-menu-avatar-image]").getAttribute("src"), null);
  assert.equal(await page.locator("[data-session-menu-avatar-initial]").isVisible(), true);

  await page.unroute("**/api/auth/status");
  await page.unroute("**/api/deployment/status");
  await page.unroute(gmailStatusRoute);
  await page.unroute("**/api/matters");
}

async function testMatterRedlineDraftPersistence(page) {
  const docxPath = path.join(os.tmpdir(), `draft-matter-${Date.now()}.docx`);
  makeDocxFixture(docxPath, [
    "NON-DISCLOSURE AGREEMENT (NDA)",
    "This Agreement shall be governed by the laws of California.",
    "The Recipient must not circumvent the Company.",
  ]);

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector("#repositoryView:not([hidden])");
  await createRepositoryMatter(page, docxPath);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");

  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  assert.equal((await page.locator("#studioDraftMeta").innerText()).trim(), "");
  assert.equal(await page.locator("#studioSaveDraftButton").isEnabled(), false);

  await page.getByRole("button", { name: /Non-Circumvention/ }).click();
  await page.locator("#studioDetailPanel [data-export-redline-id][data-export-decision=\"ignore\"]").first().click();
  await assertTextContains(page.locator("#studioDraftMeta"), "Unsaved redline draft changes");
  assert.equal(await page.locator("#studioSaveDraftButton").isEnabled(), true);
  await page.locator("#studioSaveDraftButton").click();
  await waitForText(page, "#studioDraftMeta", "Draft redline saved");

  await page.getByRole("tab", { name: "Repository" }).click();
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Draft redline saved");

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Draft redline saved");
  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  await waitForText(page, "#studioDraftMeta", "Draft redline saved");
  await page.getByRole("button", { name: /Non-Circumvention/ }).click();
  const ignoredState = await page.locator('#studioDetailPanel [data-export-redline-id][data-export-decision="ignore"]').first().evaluate((node) => ({
    active: node.classList.contains("active"),
    pressed: node.getAttribute("aria-pressed"),
  }));
  assert.deepEqual(ignoredState, { active: true, pressed: "true" });

  await page.locator("#studioDiscardDraftButton").click();
  await page.waitForFunction(() => document.querySelector("#studioDraftMeta")?.textContent.trim() === "");
  await page.getByRole("tab", { name: "Repository" }).click();
  await assertTextContains(page.locator("#repositoryMatterPanel"), "No custom draft");

  fs.rmSync(docxPath, { force: true });
}

async function testSavePickerGuardsAndFallbacks(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  const cases = await page.evaluate(async () => {
    delete window.showSaveFilePicker;
    const missingApiFallback = await chooseExportSaveHandle("missing-api.docx", { allowAutomation: true });

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
      missingApiFallbackType: typeof missingApiFallback,
      webdriverFallbackType: typeof webdriverFallback,
      picked: pickedHandle === handle,
      cancelled,
      failedFallbackType: typeof failedFallback,
    };
  });

  assert.equal(cases.callCount, 1);
  assert.equal(cases.missingApiFallbackType, "undefined");
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
      punctuationSourceSpacing: revisionState(renderDiffOperations([
        { type: "same", token: "This" },
        { type: "same", token: " Agreement" },
        { type: "same", token: " (" },
        { type: "delete", token: "California" },
        { type: "insert", token: "England" },
        { type: "insert", token: " and" },
        { type: "insert", token: " Wales" },
        { type: "same", token: ")" },
        { type: "same", token: " applies" },
        { type: "same", token: "." },
      ])),
      groupedNumber: revisionState(renderDiffOperations([
        { type: "same", token: "Payment" },
        { type: "same", token: "cap" },
        { type: "same", token: "is" },
        { type: "same", token: "1,000" },
        { type: "same", token: "for" },
        { type: "same", token: "café" },
        { type: "delete", token: "records" },
        { type: "insert", token: "documents" },
        { type: "same", token: "." },
      ])),
      groupedNumberSourceSpacing: revisionState(renderDiffOperations([
        { type: "same", token: "Payment" },
        { type: "same", token: " cap" },
        { type: "same", token: " is" },
        { type: "same", token: " 1,000" },
        { type: "same", token: " for" },
        { type: "same", token: " café" },
        { type: "delete", token: " records" },
        { type: "insert", token: " documents" },
        { type: "same", token: "." },
      ])),
      currencyAmount: revisionState(renderDiffOperations([
        { type: "same", token: "Payment" },
        { type: "same", token: "cap" },
        { type: "same", token: "is" },
        { type: "same", token: "$" },
        { type: "same", token: "100" },
        { type: "same", token: "for" },
        { type: "delete", token: "records" },
        { type: "insert", token: "documents" },
        { type: "same", token: "." },
      ])),
      currencyAmountSourceSpacing: revisionState(renderDiffOperations([
        { type: "same", token: "Payment" },
        { type: "same", token: " cap" },
        { type: "same", token: " is" },
        { type: "same", token: " $" },
        { type: "same", token: "100" },
        { type: "same", token: " for" },
        { type: "delete", token: " records" },
        { type: "insert", token: " documents" },
        { type: "same", token: "." },
      ])),
      spacedNumberList: revisionState(renderDiffOperations([
        { type: "same", token: "Payment" },
        { type: "same", token: "caps" },
        { type: "same", token: "are" },
        { type: "same", token: "1" },
        { type: "same", token: "," },
        { type: "same", token: "2" },
        { type: "same", token: "," },
        { type: "same", token: "3" },
        { type: "same", token: "," },
        { type: "same", token: "400" },
        { type: "same", token: "for" },
        { type: "delete", token: "classes" },
        { type: "insert", token: "categories" },
        { type: "same", token: "." },
      ])),
      spacedNumberListSourceSpacing: revisionState(renderDiffOperations([
        { type: "same", token: "Payment" },
        { type: "same", token: " caps" },
        { type: "same", token: " are" },
        { type: "same", token: " 1" },
        { type: "same", token: "," },
        { type: "same", token: " 2" },
        { type: "same", token: "," },
        { type: "same", token: " 3" },
        { type: "same", token: "," },
        { type: "same", token: " 400" },
        { type: "same", token: " for" },
        { type: "delete", token: " classes" },
        { type: "insert", token: " categories" },
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
  assert.equal(cases.punctuationSourceSpacing.original, "This Agreement (California) applies.");
  assert.equal(cases.punctuationSourceSpacing.accepted, "This Agreement (England and Wales) applies.");
  assert.deepEqual(cases.punctuationSourceSpacing.deleted, ["California"]);
  assert.deepEqual(cases.punctuationSourceSpacing.inserted, ["England", " and", " Wales"]);

  assert.equal(cases.groupedNumber.original, "Payment cap is 1,000 for café records.");
  assert.equal(cases.groupedNumber.accepted, "Payment cap is 1,000 for café documents.");
  assert.deepEqual(cases.groupedNumber.deleted, [" records"]);
  assert.deepEqual(cases.groupedNumber.inserted, [" documents"]);
  assert.equal(cases.groupedNumberSourceSpacing.original, "Payment cap is 1,000 for café records.");
  assert.equal(cases.groupedNumberSourceSpacing.accepted, "Payment cap is 1,000 for café documents.");
  assert.deepEqual(cases.groupedNumberSourceSpacing.deleted, [" records"]);
  assert.deepEqual(cases.groupedNumberSourceSpacing.inserted, [" documents"]);
  assert.equal(cases.currencyAmount.original, "Payment cap is $100 for records.");
  assert.equal(cases.currencyAmount.accepted, "Payment cap is $100 for documents.");
  assert.deepEqual(cases.currencyAmount.deleted, [" records"]);
  assert.deepEqual(cases.currencyAmount.inserted, [" documents"]);
  assert.equal(cases.currencyAmountSourceSpacing.original, "Payment cap is $100 for records.");
  assert.equal(cases.currencyAmountSourceSpacing.accepted, "Payment cap is $100 for documents.");
  assert.deepEqual(cases.currencyAmountSourceSpacing.deleted, [" records"]);
  assert.deepEqual(cases.currencyAmountSourceSpacing.inserted, [" documents"]);

  assert.equal(cases.spacedNumberList.original, "Payment caps are 1, 2, 3, 400 for classes.");
  assert.equal(cases.spacedNumberList.accepted, "Payment caps are 1, 2, 3, 400 for categories.");
  assert.deepEqual(cases.spacedNumberList.deleted, [" classes"]);
  assert.deepEqual(cases.spacedNumberList.inserted, [" categories"]);
  assert.equal(cases.spacedNumberListSourceSpacing.original, "Payment caps are 1, 2, 3, 400 for classes.");
  assert.equal(cases.spacedNumberListSourceSpacing.accepted, "Payment caps are 1, 2, 3, 400 for categories.");
  assert.deepEqual(cases.spacedNumberListSourceSpacing.deleted, [" classes"]);
  assert.deepEqual(cases.spacedNumberListSourceSpacing.inserted, [" categories"]);

  assert.equal(cases.fallback.original, "Old paragraph.");
  assert.equal(cases.fallback.accepted, "New paragraph.");
  assert.deepEqual(cases.fallback.deleted, ["Old paragraph."]);
  assert.deepEqual(cases.fallback.inserted, ["New paragraph."]);
}

async function testBackendRedlineModes(page) {
  await runReview(page, redlineNda);
  assert.equal(await page.locator(".studio-check-card").count(), 0);
  assert.equal(await page.locator(".studio-clause-item .studio-issue-pill").count(), 0);
  const checkRowStyles = await page.locator(".studio-clause-item.check").first().evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      boxShadow: styles.boxShadow,
    };
  });
  assert.equal(checkRowStyles.backgroundColor, "rgba(0, 0, 0, 0)");
  assert.equal(checkRowStyles.boxShadow, "none");

  const checkDotStyles = await page.locator(".studio-clause-dot.verify").first().evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      boxShadow: styles.boxShadow,
    };
  });
  assert.equal(checkDotStyles.backgroundColor, "rgb(239, 68, 68)");
  assert.match(checkDotStyles.boxShadow, /252, 165, 165/);

  // Prohibited-clause styling + delete-redline rendering on p2 used to come from
  // the deterministic non_circumvention check, which #12 moved to the dynamic
  // AI-first path. That rendering is now covered against the real AI-first
  // pipeline by testDynamicProhibitedClauseRendering (aiFirstTests).
  assert.equal(await page.locator('[data-paragraph-id="p2"] .paragraph-verdict-label').count(), 0);
  assert.equal(await page.locator("#reviewView .studio-doc-paragraph .redline-label").count(), 0);
  assert.equal(await page.getByRole("button", { name: "Add comment" }).count(), 0);
  // No stray "Comment" affordance in the document viewer.
  assert.equal(await page.locator("#reviewView .studio-page").getByText("Comment", { exact: true }).count(), 0);

  const viewerSpacing = await page.evaluate(() => {
    const pageNode = document.querySelector("#reviewView .studio-page");
    const paragraphNode = document.querySelector('#reviewView [data-paragraph-id="p2"]');
    const contentNode = [...paragraphNode.querySelectorAll(
      ".paragraph-redline-preview, .paragraph-editable, .paragraph-redline-note, .paragraph-insertion"
    )].find((node) => {
      const box = node.getBoundingClientRect();
      return box.width > 0 && box.height > 0;
    });
    const pageBox = pageNode.getBoundingClientRect();
    const paragraphBox = paragraphNode.getBoundingClientRect();
    const contentBox = contentNode.getBoundingClientRect();
    return {
      borderToPageLeft: Math.round(paragraphBox.left - pageBox.left),
      paragraphWidth: Math.round(paragraphBox.width),
      textToPageLeft: Math.round(contentBox.left - pageBox.left),
      textWidth: Math.round(contentBox.width),
      pageWidth: Math.round(pageBox.width),
    };
  });
  assert.ok(viewerSpacing.borderToPageLeft >= 24, `paragraph should sit inside the page margin: ${JSON.stringify(viewerSpacing)}`);
  assert.ok(viewerSpacing.textToPageLeft > viewerSpacing.borderToPageLeft, `paragraph text should be inset from the paragraph frame: ${JSON.stringify(viewerSpacing)}`);
  assert.ok(viewerSpacing.paragraphWidth >= viewerSpacing.pageWidth - 90, `paragraph card should use most of the page width: ${JSON.stringify(viewerSpacing)}`);
  assert.ok(viewerSpacing.textWidth >= viewerSpacing.pageWidth - 120, `paragraph text should use most of the page width: ${JSON.stringify(viewerSpacing)}`);

  // Select text on a paragraph that carries a visible editable body (p2 is the
  // confidential_information/signatures insert anchor under the deterministic
  // engine) and exercise the selection comment composer.
  await page.evaluate(() => {
    const paragraph = document.querySelector('[data-paragraph-id="p2"]');
    const target = paragraph.querySelector('[data-editable-paragraph-id="p2"]') || paragraph;
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => node.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT,
    });
    const textNode = walker.nextNode();
    const range = document.createRange();
    range.setStart(textNode, 0);
    range.setEnd(textNode, Math.min(textNode.nodeValue.length, 14));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
  await page.waitForSelector('[data-paragraph-id="p2"].has-selection .paragraph-comment-add');
  assert.equal(await page.getByRole("button", { name: "Add comment" }).count(), 1);
  const addCommentStyle = await page.getByRole("button", { name: "Add comment" }).evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      height: styles.height,
      text: node.textContent.trim(),
      visibility: styles.visibility,
      width: styles.width,
    };
  });
  assert.deepEqual(addCommentStyle, {
    height: "28px",
    text: "",
    visibility: "visible",
    width: "28px",
  });
  await page.getByRole("button", { name: "Add comment" }).click();
  await page.waitForSelector('[data-paragraph-id="p2"] .comment-thread-card .comment-compose');
  assert.equal(await page.locator('[data-paragraph-id="p2"] .comment-compose-input').getAttribute("placeholder"), "Add a comment");
  assert.equal(await page.getByRole("button", { name: "Add comment" }).count(), 0);
  await page.locator('[data-paragraph-id="p2"] .comment-compose-cancel').click();
}

// Runs on the AI-first + stub server, where non_circumvention (a dynamic,
// engine=="dynamic" prohibited clause) is actually reviewed. Covers the
// prohibited-clause rendering that #12 moved off the deterministic engine:
// the paragraph gets the "prohibited" class, a delete redline, a clean-view
// removed anchor, and a side-by-side deletion — the live generic delete-render
// behavior, exercised through the real AI-first pipeline.
async function testDynamicProhibitedClauseRendering(page) {
  await runReview(page, redlineNda, { baseUrl: AI_FIRST_BASE_URL });

  const nonCircCard = page.locator('[data-studio-lane-id="non_circumvention"]');
  assert.equal(await nonCircCard.count(), 1, "dynamic non_circumvention clause should appear as a lane");
  assert.equal(await page.locator('.studio-clause-dot.verify').count() >= 1, true);
  assert.equal(await page.locator('.clause-engine-badge').count(), 0, "the Dynamic engine badge bubble should no longer render");

  const prohibited = await page.locator('[data-paragraph-id="p2"]').evaluate((node) => ({
    hasProhibitedClass: node.classList.contains("prohibited"),
    hasRedlineDelete: node.classList.contains("redline-delete"),
    backgroundColor: getComputedStyle(node).backgroundColor,
  }));
  assert.equal(prohibited.hasProhibitedClass, true, "prohibited paragraph should carry the prohibited class");
  assert.equal(prohibited.hasRedlineDelete, true, "prohibited paragraph should carry the delete redline class");
  assert.notEqual(prohibited.backgroundColor, "rgba(0, 0, 0, 0)", "prohibited paragraph should be tinted");

  await page.getByRole("button", { name: "Clean" }).click();
  const cleanText = await page.locator("#studioDocumentRender").innerText();
  assert.doesNotMatch(cleanText, /must not circumvent/, "clean view should drop the deleted prohibited paragraph text");
  const cleanDeleteAnchor = page.locator('[data-paragraph-id="p2"]');
  assert.equal(await cleanDeleteAnchor.evaluate((node) => node.classList.contains("doc-clean-removed-anchor")), true);
  assert.equal(await cleanDeleteAnchor.evaluate((node) => (
    node.querySelector(".paragraph-redline-preview, .paragraph-editable, .paragraph-redline-note, .paragraph-insertion")?.textContent || ""
  ).trim()), "");

  await page.getByRole("button", { name: "Side by Side" }).click();
  const deletedSideBySide = await page.locator('[data-paragraph-id="p2"]').evaluate((node) => ({
    original: node.querySelector(".clause-sxs-col.original div")?.innerText || "",
    originalDeleted: node.querySelectorAll(".clause-sxs-col.original .inline-del").length,
    proposedEmpty: node.querySelector(".clause-sxs-col.latest .sxs-empty")?.textContent || "",
  }));
  assert.match(deletedSideBySide.original, /must not circumvent/);
  assert.equal(deletedSideBySide.originalDeleted, 1);
  assert.equal(deletedSideBySide.proposedEmpty, "Removed in proposed text");
}

async function testClauseAnchorCycling(page) {
  await runReview(page, multiAnchorNda, { baseUrl: AI_FIRST_BASE_URL });
  const nonCircumventionCard = page.locator('[data-studio-lane-id="non_circumvention"]');

  await nonCircumventionCard.click();
  await page.waitForSelector('[data-paragraph-id="p1"].paragraph-pulse');
  assert.equal(await page.locator('[data-paragraph-id="p2"]').evaluate((node) => node.classList.contains("paragraph-pulse")), false);

  await nonCircumventionCard.click();
  await page.waitForSelector('[data-paragraph-id="p2"].paragraph-pulse');
  assert.equal(await page.locator('[data-paragraph-id="p1"]').evaluate((node) => node.classList.contains("paragraph-pulse")), false);
}

async function testClauseDecisionControls(page) {
  await runReview(page, redlineNda);
  const nonCircumventionCard = page.locator('[data-studio-lane-id="non_circumvention"]');
  const detailPanel = page.locator("#studioDetailPanel");
  const redlineParagraph = page.locator('[data-paragraph-id="p2"]');

  await nonCircumventionCard.click();
  await detailPanel.locator('[data-export-redline-id][data-export-decision="ignore"]').first().click();
  await page.waitForFunction(() => document.querySelector('#studioDetailPanel [data-export-redline-id][data-export-decision="ignore"]')?.getAttribute("aria-pressed") === "true");
  assert.equal(await redlineParagraph.evaluate((node) => node.classList.contains("redline-delete")), false);
  await nonCircumventionCard.click();
  assert.equal(await redlineParagraph.evaluate((node) => node.classList.contains("redline-delete")), false);

  await detailPanel.locator('[data-export-redline-id][data-export-decision="include"]').first().click();
  await page.waitForFunction(() => document.querySelector('#studioDetailPanel [data-export-redline-id][data-export-decision="include"]')?.getAttribute("aria-pressed") === "true");
  await page.waitForFunction(() => document.querySelector('[data-paragraph-id="p2"]')?.classList.contains("redline-delete"));
  await assertTextContains(redlineParagraph, "must not circumvent");

  await page.locator("#studioUndoEditButton").click();
  assert.equal(await redlineParagraph.evaluate((node) => node.classList.contains("redline-delete")), false);
  await assertTextContains(page.locator("#studioFileMeta"), "Undid clause suggestion change");

  await detailPanel.locator('[data-export-redline-id][data-export-decision="include"]').first().click();
  await page.waitForFunction(() => document.querySelector('#studioDetailPanel [data-export-redline-id][data-export-decision="include"]')?.getAttribute("aria-pressed") === "true");
  await page.waitForFunction(() => document.querySelector('[data-paragraph-id="p2"]')?.classList.contains("redline-delete"));
  await assertTextContains(redlineParagraph, "must not circumvent");

  const [exportRequest, download] = await Promise.all([
    page.waitForRequest((request) => request.url().endsWith("/api/export-review-docx") && request.method() === "POST"),
    page.waitForEvent("download"),
    chooseDownloadFormat(page.locator("#studioExportButton"), "docx"),
  ]);
  const exportPayload = exportRequest.postDataJSON();
  assert.ok(
    exportPayload.export_redline_edits.some((edit) => (
      edit.action === "delete_paragraph"
      && /must not circumvent/.test(edit.original_text || "")
    )),
    "re-included non-circumvention deletion should be sent in export_redline_edits",
  );
  const exportedPath = await download.path();
  assert.ok(exportedPath, "decision export download path should be available");
  const exportedChanges = readDocxTrackChanges(exportedPath);
  assert.equal(exportedChanges.hasTrackRevisions, true);
}

async function testManualViewerEditRedline(page) {
  await runReview(page, passNda);
  assert.equal(await page.locator("#studioUndoEditButton").isEnabled(), false);
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
  await page.locator('[data-view-mode="redline"]').click();

  const editedTitle = "Mutual Non-Disclosure AGREEMdasdasdsa";
  await page.locator('[data-editable-paragraph-id="p1"]').click();
  await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
  await page.keyboard.type(editedTitle);
  await page.waitForSelector('[data-paragraph-id="p1"].manual-redline');
  assert.equal(await page.locator("#studioUndoEditButton").isEnabled(), true);

  const paragraph = page.locator('[data-paragraph-id="p1"]');
  await assertRedlinePreview(paragraph, {
    originalText: "greement",
    insertedText: "GREEMdasdasdsa",
    editableCount: 1,
  });
  await page.locator('[data-editable-paragraph-id="p1"]').evaluate((node) => node.blur());
  await page.waitForSelector('[data-paragraph-id="p1"]:not(.is-editing) .paragraph-redline-preview:not([hidden])');
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

  await page.locator('[data-view-mode="redline"]').click();
  await page.locator("#studioUndoEditButton").click();
  await page.waitForSelector('[data-paragraph-id="p1"]:not(.manual-redline)');
  assert.equal(await page.locator("#studioUndoEditButton").isEnabled(), false);
  await assertTextContains(page.locator('[data-paragraph-id="p1"]'), "Mutual Non-Disclosure Agreement");
  assert.equal(
    await page.locator('[data-paragraph-id="p1"] .paragraph-redline-preview:not([hidden])').count(),
    0,
    "undo should remove the manual redline preview once the source text is restored",
  );
  await assertTextContains(page.locator("#studioFileMeta"), "Undid viewer edit");

  await page.locator('[data-editable-paragraph-id="p5"]').click();
  await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
  await page.keyboard.type("This Agreement shall be governed by the laws of California.");
  await page.waitForSelector('[data-paragraph-id="p5"].manual-redline');
  await assertTextContains(page.locator('[data-paragraph-id="p5"]'), "California");

  const refreshedBaseline = await page.evaluate(() => {
    const paragraphs = [
      { id: "p1", index: 1, start: 0, end: 21, text: "First refreshed block." },
      { id: "p2", index: 2, start: 23, end: 45, text: "Second refreshed block." },
    ];
    applyViewerReviewDetectionResult({
      ...state.latestReviewResult,
      clauses: state.reviewClauses,
      paragraphs,
      redline_edits: [],
    }, paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
    return {
      manualRedlines: manualExportRedlines(),
      originalTexts: state.reviewOriginalParagraphs.map((paragraph) => paragraph.text),
      paragraphTexts: state.reviewParagraphs.map((paragraph) => paragraph.text),
    };
  });
  assert.deepEqual(refreshedBaseline.originalTexts, refreshedBaseline.paragraphTexts);
  assert.deepEqual(
    refreshedBaseline.manualRedlines,
    [],
    "auto-refresh should re-snapshot paragraph originals after paragraph-count changes",
  );
}

async function testViewerAutoRefreshSelection(page) {
  await runReview(page, passNda);

  const selectionState = await page.evaluate(() => {
    const offsetWithin = (root, node, offset) => {
      const range = document.createRange();
      range.selectNodeContents(root);
      range.setEnd(node, offset);
      return range.toString().length;
    };
    const placeSelection = (start, end) => {
      const editable = document.querySelector('[data-editable-paragraph-id="p1"]');
      const textNode = editable.firstChild;
      const range = document.createRange();
      range.setStart(textNode, start);
      range.setEnd(textNode, end);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      editable.focus();
      editable.dataset.editStartText = "Mutual Non-Disclosure Agreement";
      editable.dataset.editHistoryRecorded = "true";
    };
    const applyRefresh = () => {
      applyViewerReviewDetectionResult({
        ...state.latestReviewResult,
        clauses: state.reviewClauses,
        paragraphs: state.reviewParagraphs.map((paragraph) => ({ ...paragraph })),
        redline_edits: state.reviewRedlines,
      }, state.reviewSourceText);
    };
    const currentSelection = () => {
      const editable = document.querySelector('[data-editable-paragraph-id="p1"]');
      const selection = window.getSelection();
      const range = selection.rangeCount ? selection.getRangeAt(0) : null;
      return {
        active: document.activeElement === editable,
        editHistoryRecorded: editable.dataset.editHistoryRecorded,
        editStartText: editable.dataset.editStartText,
        selectedText: selection.toString(),
        startOffset: range ? offsetWithin(editable, range.startContainer, range.startOffset) : null,
        endOffset: range ? offsetWithin(editable, range.endContainer, range.endOffset) : null,
      };
    };

    placeSelection(7, 21);
    applyRefresh();
    const rangeSelection = currentSelection();

    placeSelection(12, 12);
    applyRefresh();
    const caretSelection = currentSelection();

    return { caretSelection, rangeSelection };
  });

  assert.deepEqual(selectionState.rangeSelection, {
    active: true,
    editHistoryRecorded: "true",
    editStartText: "Mutual Non-Disclosure Agreement",
    selectedText: "Non-Disclosure",
    startOffset: 7,
    endOffset: 21,
  });
  assert.deepEqual(selectionState.caretSelection, {
    active: true,
    editHistoryRecorded: "true",
    editStartText: "Mutual Non-Disclosure Agreement",
    selectedText: "",
    startOffset: 12,
    endOffset: 12,
  });
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
    chooseDownloadFormat(page.locator("#studioExportButton"), "docx"),
  ]);
  const exportedPath = await download.path();
  assert.ok(exportedPath, "download path should be available");
  const exportedChanges = readDocxTrackChanges(exportedPath);
  assert.ok(preview.some(({ edit }) => edit.action === "delete_paragraph"), "fixture should include delete redlines");
  assert.equal(exportedChanges.hasTrackRevisions, true);

  for (const { edit, preview: previewParagraph } of preview) {
    const expectedOriginal = edit.action === "insert_after_paragraph" ? "" : edit.original_text;
    const expectedAccepted = edit.action === "delete_paragraph"
      ? ""
      : edit.action === "insert_after_paragraph"
        ? edit.insert_text
        : edit.replacement_text;
    assert.equal(normalizeWhitespace(previewParagraph.original), normalizeWhitespace(expectedOriginal), `${edit.id} preview original`);
    assert.equal(normalizeWhitespace(previewParagraph.accepted), normalizeWhitespace(expectedAccepted), `${edit.id} preview accepted`);
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
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector("#repositoryView:not([hidden])");
  await createRepositoryMatter(page, sourceDocxPath);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Repository" }).click();
  assert.equal(await page.locator("#repositoryTab").getAttribute("aria-selected"), "true");
  await page.waitForSelector(".repository-card");
  await page.locator(".repository-card").filter({ hasText: "Source Redline NDA" }).click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "MANUAL UPLOAD");

  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#studioDocumentRender:not([hidden])");
  await page.waitForSelector(".studio-clause-item.pass, .studio-clause-item.check");

  assert.equal(await page.locator("#studioDocTitle").innerText(), "Source Redline NDA");
  await assertTextContains(page.locator("#studioFileMeta"), "Manual Upload matter loaded");
  assert.ok(await page.locator(".studio-clause-item.check").count() > 0, "source-redline review should produce fail findings");

  await page.locator('[data-editable-paragraph-id="p1"]').fill("Do you see problem?");
  await page.waitForSelector('[data-paragraph-id="p1"].manual-redline');
  await assertTextContains(page.locator("#studioFileMeta"), "Edited in viewer");
  const reviewTimestampBeforeRefresh = await page.evaluate(() => state.latestReviewResult?.checked_at || "");
  await page.waitForFunction((previousCheckedAt) => (
    state.latestReviewResult?.checked_at
    && state.latestReviewResult.checked_at !== previousCheckedAt
    && !document.querySelector("#studioResultMeta")?.textContent.includes("Rechecking")
  ), reviewTimestampBeforeRefresh);
  await page.waitForSelector('[data-paragraph-id="p1"].manual-redline');
  const manualRedlinesAfterRefresh = await page.evaluate(() => manualExportRedlines());
  assert.equal(manualRedlinesAfterRefresh.length, 1, "viewer edit should remain exportable after auto-refresh");
  assert.equal(
    manualRedlinesAfterRefresh[0].source_index,
    1,
    "viewer edit should keep the original DOCX source anchor after auto-refresh",
  );

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    chooseDownloadFormat(page.locator("#studioExportButton"), "docx"),
  ]);
  assert.match(download.suggestedFilename(), /^Source-Redline-NDA-redlined(?:-[0-9a-f]{12})?\.docx$/);
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
  // Clause-level source DOCX mapping is covered by backend export tests. The
  // AI-first frontend fixture may legitimately return no automatic replacement
  // for this uploaded governing-law paragraph.
}

async function testExportMarksCapturedMatterReady(page) {
  await page.goto(`${BASE_URL}/?v=export-matter-race-test`, { waitUntil: "domcontentloaded" });

  let capturedExportPayload = null;
  let exportStartedResolve;
  let releaseExport;
  const exportStarted = new Promise((resolve) => {
    exportStartedResolve = resolve;
  });
  const exportCanFinish = new Promise((resolve) => {
    releaseExport = resolve;
  });
  await page.route("**/api/export-review-docx", async (route) => {
    capturedExportPayload = route.request().postDataJSON();
    exportStartedResolve();
    await exportCanFinish;
    await route.fulfill({
      status: 200,
      headers: {
        "Content-Disposition": 'attachment; filename="matter-a-redlined.docx"',
        "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "X-Export-Verified": "word-package; track-revisions",
      },
      body: "fake-docx",
    });
  });

  await page.evaluate(() => {
    const sourceText = "This Agreement shall be governed by the laws of California.";
    window.__markedRedlineReadyMatterIds = [];
    window.__exportRaceDownloads = [];
    window.downloadBlob = (_blob, filename) => {
      window.__exportRaceDownloads.push(filename);
    };
    repositoryController.markMatterRedlineReady = async (matter) => {
      window.__markedRedlineReadyMatterIds.push(matter?.id || null);
      return matter ? { ...matter, board_column: "reviewed" } : null;
    };
    state.selectedMatter = {
      board_column: "in_review",
      id: "matter_a",
      source_filename: "Matter A.docx",
      title: "Matter A",
    };
    state.selectedDocument = null;
    state.reviewSourceText = sourceText;
    state.reviewClauses = [{
      id: "governing_law",
      name: "Governing Law",
      passes: false,
      status: "check",
    }];
    state.reviewRedlines = [];
    state.reviewParagraphs = [{ id: "p1", index: 1, source_index: 1, text: sourceText }];
    state.reviewOriginalParagraphs = [{ id: "p1", index: 1, source_index: 1, text: sourceText }];
    state.reviewExportOriginalParagraphs = [{ id: "p1", index: 1, source_index: 1, text: sourceText }];
    state.exportClauseDecisions = {};
    state.redlineTemplateSelections = {};
    state.redlineDraftDirty = false;
    studioNdaText.value = sourceText;
    studioDocTitle.textContent = "Matter A";
  });

  const exportPromise = page.evaluate(() => exportReviewDocx());
  await exportStarted;
  await page.evaluate(() => {
    state.selectedMatter = {
      board_column: "in_review",
      id: "matter_b",
      source_filename: "Matter B.docx",
      title: "Matter B",
    };
  });
  releaseExport();
  await exportPromise;

  const exportRaceState = await page.evaluate(() => ({
    downloads: window.__exportRaceDownloads,
    markedReadyMatterIds: window.__markedRedlineReadyMatterIds,
    selectedMatterId: state.selectedMatter?.id || null,
  }));
  assert.equal(capturedExportPayload.matter_id, "matter_a");
  assert.deepEqual(exportRaceState.markedReadyMatterIds, ["matter_a"]);
  assert.deepEqual(exportRaceState.downloads, ["matter-a-redlined.docx"]);
  assert.equal(exportRaceState.selectedMatterId, "matter_b");

  await page.unroute("**/api/export-review-docx");
}

async function testExportFlow(page) {
  await runReview(page, passNda);
  const exportButton = page.locator("#studioExportButton");
  assert.equal(await exportButton.isEnabled(), true);

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    chooseDownloadFormat(exportButton, "docx"),
  ]);
  assert.match(download.suggestedFilename(), /^nda-review-report(?:-[0-9a-f]{12})?\.docx$/);
  const downloadedPath = await download.path();
  assert.ok(downloadedPath, "download path should be available");
  assert.ok(fs.statSync(downloadedPath).size > 1000, "exported DOCX should not be empty");
  await assertTextContains(page.locator("#studioFileMeta"), "Saved export:");
  assert.match(await page.locator("#studioFileMeta").innerText(), /\/exports\/nda-review-report(?:-[0-9a-f]{12})?\.docx/);
  await assertTextContains(page.locator("#studioFileMeta"), "Word package verified");
  await assertTextContains(page.locator("#studioFileMeta"), "Track Changes enabled");
  await assertTextContains(page.locator("#studioFileMeta a.download-again"), "Download again");
  assert.match(
    await page.locator("#studioFileMeta a.download-again").getAttribute("href"),
    /^\/exports\/nda-review-report(?:-[0-9a-f]{12})?\.docx$/,
  );
  assert.equal(await page.locator("#studioFileMeta a.download-again").getAttribute("download"), "nda-review-report.docx");

  await page.locator('[data-editable-paragraph-id="p1"]').fill("Mutual Non-Disclosure Agreement with edits");
  await page.waitForSelector('[data-paragraph-id="p1"].manual-redline');
  assert.equal(await exportButton.isEnabled(), true);

  const [editedDownload] = await Promise.all([
    page.waitForEvent("download"),
    chooseDownloadFormat(exportButton, "docx"),
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

async function testReviewDownloadMenuPdfReconstructionMetadata(page) {
  await runReview(page, passNda);
  const exportButton = page.locator("#studioExportButton");
  assert.equal(await exportButton.isEnabled(), true);

  await page.evaluate(() => {
    state.selectedMatter = {
      id: "matter_pdf",
      review_refresh: { stale: false },
      document_downloads: {
        reviewed: {
          formats: {
            docx: {
              available: true,
              content_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              download_url: "/api/matters/matter_pdf/reviewed.docx",
              fidelity: {
                message: "Best-effort editable Word export reconstructed from the source PDF.",
                status: "best_effort",
              },
              filename: "matter-pdf-reviewed.docx",
              format: "docx",
              label: "Reconstructed reviewed Word",
              source_transform: "pdf_to_reconstructed_reviewed_docx",
            },
            pdf: {
              available: true,
              content_type: "application/pdf",
              download_url: "/api/matters/matter_pdf/reviewed.pdf",
              fidelity: {
                message: "Preserves original PDF bytes with review annotations.",
                status: "native",
              },
              filename: "matter-pdf-reviewed.pdf",
              format: "pdf",
              label: "Annotated PDF",
              source_transform: "reviewed_pdf_annotations",
            },
          },
        },
      },
    };
  });

  const menu = await openDownloadMenu(exportButton);
  const docxOption = menu.locator('[data-download-format="docx"]').first();
  assert.equal(await docxOption.isEnabled(), true);
  assert.equal(await docxOption.getAttribute("data-source-transform"), "pdf_to_reconstructed_reviewed_docx");
  await assertTextContains(docxOption, "Reconstructed reviewed Word");
  await assertTextContains(docxOption, "matter-pdf-reviewed.docx");
  await assertTextContains(docxOption, "PDF-to-Word reconstruction");
  await assertTextContains(docxOption, "Best-effort editable Word export reconstructed from the source PDF.");

  const pdfOption = menu.locator('[data-download-format="pdf"]').first();
  assert.equal(await pdfOption.isEnabled(), true);
  assert.equal(await pdfOption.getAttribute("data-source-transform"), "reviewed_pdf_annotations");
  await assertTextContains(pdfOption, "Annotated PDF");
  await assertTextContains(pdfOption, "PDF annotation export");
  await assertTextContains(pdfOption, "Preserves original PDF bytes with review annotations.");

  await page.keyboard.press("Escape");
  await menu.waitFor({ state: "detached" });

  await page.evaluate(() => {
    const docx = state.selectedMatter.document_downloads.reviewed.formats.docx;
    state.selectedMatter.document_downloads.reviewed.formats.docx = {
      ...docx,
      available: false,
      download_url: "",
      unavailable_reason: "PDF-to-DOCX reconstruction is unavailable because LibreOffice is not installed.",
    };
  });

  const unavailableMenu = await openDownloadMenu(exportButton);
  const unavailableDocx = unavailableMenu.locator('[data-download-format="docx"]').first();
  assert.equal(await unavailableDocx.isDisabled(), true);
  assert.equal(await unavailableDocx.getAttribute("data-source-transform"), "pdf_to_reconstructed_reviewed_docx");
  await assertTextContains(unavailableDocx, "Reconstructed reviewed Word");
  await assertTextContains(unavailableDocx, "PDF-to-DOCX reconstruction is unavailable because LibreOffice is not installed.");
  await assertTextContains(unavailableDocx, "PDF-to-Word reconstruction");
  await assertTextContains(unavailableDocx, "Best-effort editable Word export reconstructed from the source PDF.");
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
  let png;
  let lastError;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      await locator.waitFor({ state: "visible", timeout: 5000 });
      png = PNG.sync.read(await locator.screenshot());
      break;
    } catch (error) {
      lastError = error;
      await wait(120);
    }
  }
  if (!png) throw lastError;
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

// Load a review with a persisted selected matter so the approve endpoint has a
// matter id to POST to. The clauses default to one "review" clause that requires
// attention plus one passing clause.
async function loadReviewWithMatter(page, { matter = {}, clauses, paragraphs, result = {} } = {}) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  const defaultParagraphs = [
    { id: "p1", index: 1, source_index: 1, text: "Confidential Information means all business information." },
    { id: "p2", index: 2, source_index: 2, text: "This Agreement shall be governed by the laws of California." },
  ];
  const defaultClauses = [
    {
      decision: "review",
      evidence_paragraphs: [defaultParagraphs[0]],
      id: "confidential_information",
      issue_label: "Needs review",
      name: "Confidential Information",
      needs_review: true,
      reason: "Broad confidential information definition needs human review.",
      review_state: { blocks_send: true, requires_human_review: true, state: "review" },
      status: "review",
    },
    {
      decision: "pass",
      evidence_paragraphs: [defaultParagraphs[1]],
      id: "mutuality",
      issue_label: "Pass",
      name: "Mutuality",
      passes: true,
      reason: "Mutual obligations present.",
      review_state: { state: "pass" },
      status: "pass",
    },
  ];
  await page.evaluate((payload) => {
    state.selectedMatter = {
      id: "matter_review_panel",
      source_filename: "Counterparty NDA.docx",
      status: "in_review",
      ...payload.matter,
    };
    renderResult(
      {
        checked_at: "2026-06-05T09:00:00+00:00",
        clauses: payload.clauses,
        overall_status: "needs_review",
        paragraphs: payload.paragraphs,
        redline_edits: payload.redlineEdits || [],
        requirements_failed: 0,
        requirements_needs_review: 1,
        requirements_passed: 1,
        ...payload.result,
      },
      payload.paragraphs.map((paragraph) => paragraph.text).join("\n\n"),
    );
  }, {
    clauses: clauses || defaultClauses,
    matter,
    paragraphs: paragraphs || defaultParagraphs,
    redlineEdits: result.redline_edits,
    result,
  });
}

async function testPlaybookPositionAndSpanHighlight(page) {
  await loadReviewWithMatter(page, {
    clauses: [
      {
        citation: { paragraph_id: "p2", quote: "governed by the laws of California.", relevance: "Unapproved governing law." },
        decision: "review",
        evidence_paragraphs: [{ id: "p2", index: 2, text: "This Agreement shall be governed by the laws of California." }],
        grounding: { status: "grounded", confidence: 0.92 },
        id: "governing_law",
        issue_label: "Needs review",
        name: "Governing Law",
        needs_review: true,
        playbook: { preferred_position: "Delaware governing law, with India and DIFC as approved fallbacks." },
        reason: "Governing law is outside the approved set.",
        review_state: { state: "review" },
        status: "review",
        structured_evidence: [{
          paragraph_id: "p2",
          matched_text: "laws of California",
          match_spans: [{ start: 40, end: 58, text: "laws of California", term: "laws of California" }],
        }],
      },
    ],
    paragraphs: [{ id: "p2", index: 2, source_index: 2, start: 0, end: 59, text: "This Agreement shall be governed by the laws of California." }],
  });

  const detailPanel = page.locator("#studioDetailPanel");
  await page.locator('[data-studio-lane-id="governing_law"]').click();

  await assertTextContains(detailPanel.locator(".playbook-position-block"), "Delaware governing law");
  await assertTextContains(detailPanel.locator(".playbook-position-block"), "REQUIRED POSITION");

  await assertTextContains(detailPanel.locator('[data-card-section="document"]'), "laws of California");
  assert.equal(await detailPanel.locator(".clause-confidence-text").count(), 0);
  assert.equal((await detailPanel.textContent()).includes("Confidence"), false);
  const highlight = page.locator('.clause-evidence-highlight.review[data-clause-evidence-id="governing_law"]');
  await assertTextContains(highlight, "laws of California");
}

async function testStructuredProposedChangePanel(page) {
  await loadReviewWithMatter(page, {
    clauses: [
      {
        decision: "fail",
        evidence_paragraphs: [{ id: "p2", index: 2, text: "This Agreement shall be governed by the laws of California." }],
        id: "governing_law",
        issue_label: "Fail",
        name: "Governing Law",
        reason: "Governing law is outside the approved set.",
        review_state: { state: "check" },
        status: "check",
      },
      {
        decision: "review",
        evidence_paragraphs: [{ id: "p3", index: 3, text: "The Recipient shall not interfere with Company customers." }],
        id: "non_circumvention",
        issue_label: "Needs review",
        name: "Non-Circumvention",
        needs_review: true,
        approved_positions: ["Delete the restriction", "Narrow to active introductions only"],
        proposed_change: {
          action: "needs_human_choice",
          approved_alternatives: ["Delete the restriction", "Narrow to active introductions only"],
          confidence: 0.38,
          evidence: { paragraph_id: "p3", quote: "shall not interfere with Company customers" },
          issue_summary: "The restriction may overreach and needs reviewer wording.",
          playbook_rationale: "Preserve legitimate competitive freedom while protecting active introductions.",
          recommended_option: {
            option: "Narrow to active introductions only",
            reason: "It preserves legitimate competitive freedom.",
          },
          resolution_question: "Should this restriction be deleted or narrowed to active introductions only?",
          safety: {
            reason: "No safe replacement was selected because the source wording needs business judgment.",
            requires_human_approval: true,
            status: "needs_human_choice",
          },
          suggested_redline: "The Recipient must not knowingly circumvent active introductions made under this Agreement.",
        },
        reason: "Circumvention language needs human review.",
        review_state: { state: "review" },
        status: "review",
      },
      {
        decision: "review",
        evidence_paragraphs: [{ id: "p4", index: 4, text: "Assignment requires prior written consent." }],
        id: "assignment",
        issue_label: "Needs review",
        name: "Assignment",
        needs_review: true,
        proposed_change: {
          action: "comment_only",
          confidence: 0.44,
          evidence: { paragraph_id: "p4", quote: "Assignment requires prior written consent." },
          issue_summary: "Assignment wording needs reviewer confirmation before any redline.",
          playbook_rationale: "Reviewer should confirm whether this restriction matches the transaction context.",
          safety: {
            reason: "Comment-only finding because no safe automatic edit is available.",
            requires_human_approval: true,
            status: "comment_only",
          },
        },
        reason: "Assignment restriction needs review.",
        review_state: { state: "review" },
        status: "review",
      },
    ],
    paragraphs: [
      { id: "p2", index: 2, source_index: 2, text: "This Agreement shall be governed by the laws of California." },
      { id: "p3", index: 3, source_index: 3, text: "The Recipient shall not interfere with Company customers." },
      { id: "p4", index: 4, source_index: 4, text: "Assignment requires prior written consent." },
    ],
    result: {
      proposed_changes: [
        {
          action: "replace",
          confidence: 0.91,
          evidence: { paragraph_id: "p2", quote: "This Agreement shall be governed by the laws of California." },
          issue_summary: "Unapproved California governing law.",
          clause_id: "governing_law",
          clause_name: "Governing Law",
          playbook_rationale: "Use an approved governing law before export.",
          proposed_text: "This Agreement shall be governed by the laws of Delaware.",
          safety: {
            reason: "Reviewer must approve before export.",
            requires_human_approval: true,
            status: "proposed_redline_available",
          },
          source_text: "This Agreement shall be governed by the laws of California.",
          version: 1,
        },
      ],
    },
  });

  const detailPanel = page.locator("#studioDetailPanel");
  await page.locator('[data-studio-lane-id="governing_law"]').click();
  const redlineBackedChange = detailPanel.locator(".proposed-change-card");
  await assertTextContains(redlineBackedChange, "RECOMMENDED CHANGE");
  await assertTextContains(redlineBackedChange, "California");
  await assertTextContains(redlineBackedChange, "Delaware");
  await assertTextContains(redlineBackedChange, "WHY THIS EDIT");
  await assertTextContains(redlineBackedChange, "Use an approved governing law before export.");
  await assertTextContains(redlineBackedChange, "Reviewer must approve before export.");

  await page.locator('[data-studio-lane-id="non_circumvention"]').click();
  const humanChoiceChange = detailPanel.locator(".proposed-change-card");
  await assertTextContains(humanChoiceChange, "Should this restriction be deleted or narrowed to active introductions only?");
  await assertTextContains(humanChoiceChange, "SUGGESTED EDIT (CONFIRM REQUIRED)");
  await assertTextContains(humanChoiceChange, "must not knowingly circumvent active introductions");
  await assertTextContains(humanChoiceChange, "RECOMMENDED OPTION");
  await assertTextContains(humanChoiceChange, "Narrow to active introductions only");
  await assertTextContains(humanChoiceChange, "APPROVED ALTERNATIVES");
  await assertTextContains(humanChoiceChange, "Delete the restriction");

  await page.locator('[data-studio-lane-id="assignment"]').click();
  const commentOnlyChange = detailPanel.locator(".proposed-change-card");
  await assertTextContains(commentOnlyChange, "What wording or approved playbook position should resolve this clause?");
  await assertTextContains(commentOnlyChange, "No safe wording was selected automatically");
  await assertTextContains(detailPanel.locator('[data-card-section="document"]'), "Assignment requires prior written consent.");
}

async function testNeedsReviewJurisdictionPicker(page) {
  // When a needs-review clause has a redline edit with multiple template_options,
  // the Recommended Change card should show the interactive jurisdiction picker
  // (renderRedlineTemplateOptions) instead of the static approved-alternatives list.
  // Selecting an option must update the insert wording via setRedlineTemplateSelection.
  await loadReviewWithMatter(page, {
    clauses: [
      {
        decision: "review",
        evidence_paragraphs: [{ id: "p2", index: 2, text: "This Agreement shall be governed by the laws of California." }],
        id: "governing_law",
        issue_label: "Needs review",
        name: "Governing Law",
        needs_review: true,
        reason: "Governing law needs human review.",
        review_state: { blocks_send: true, requires_human_review: true, state: "review" },
        status: "review",
      },
    ],
    paragraphs: [
      { id: "p2", index: 2, source_index: 2, text: "This Agreement shall be governed by the laws of California." },
    ],
    result: {
      redline_edits: [
        {
          action: "replace_paragraph",
          clause_id: "governing_law",
          id: "rl_govlaw",
          original_text: "This Agreement shall be governed by the laws of California.",
          paragraph_id: "p2",
          template_options: [
            {
              id: "opt_delaware",
              label: "Delaware",
              replacement_text: "This Agreement shall be governed by the laws of Delaware.",
              selected: true,
            },
            {
              id: "opt_england",
              label: "England and Wales",
              replacement_text: "This Agreement shall be governed by the laws of England and Wales.",
            },
          ],
        },
      ],
    },
  });

  await page.locator('[data-studio-lane-id="governing_law"]').click();
  const changeCard = page.locator('[data-card-section="recommended-change"]');

  // The interactive picker must be present (not the static list).
  const picker = changeCard.locator(".redline-options");
  assert.equal(await picker.count(), 1, "interactive jurisdiction picker should be rendered");
  await assertTextContains(picker, "JURISDICTION OPTIONS");
  await assertTextContains(picker, "Delaware");
  await assertTextContains(picker, "England and Wales");

  // The static approved-alternatives list must NOT be shown (picker replaces it).
  assert.equal(await changeCard.locator(".approved-alternatives").count(), 0,
    "static approved-alternatives list should be absent when interactive picker is rendered");

  // Selecting the second option (England and Wales) updates the template selection.
  const englandButton = picker.locator('[data-redline-option-id="opt_england"]');
  await englandButton.click();
  await page.waitForFunction(() => state.redlineTemplateSelections.rl_govlaw === "opt_england");
  assert.equal(await englandButton.getAttribute("aria-pressed"), "true");
}

async function testRedlineRationaleBlock(page) {
  await loadReviewWithMatter(page, {
    clauses: [
      {
        decision: "fail",
        evidence_paragraphs: [{ id: "p2", index: 2, text: "This Agreement shall be governed by the laws of California." }],
        id: "governing_law",
        issue_label: "Fail",
        name: "Governing Law",
        reason: "Governing law is outside the approved set.",
        review_state: { state: "check" },
        status: "check",
      },
    ],
    paragraphs: [{ id: "p2", index: 2, source_index: 2, text: "This Agreement shall be governed by the laws of California." }],
    result: {
      redline_edits: [
        {
          action: "replace_paragraph",
          clause_id: "governing_law",
          id: "rl_governing_law",
          original_text: "This Agreement shall be governed by the laws of California.",
          paragraph_id: "p2",
          redline_rationale: {
            basis: { paragraph_id: "p2", quote: "governed by the laws of California." },
            explanation: "California is outside the playbook's approved governing-law set; Delaware is preferred.",
          },
          replacement_text: "This Agreement shall be governed by the laws of Delaware.",
        },
      ],
    },
  });

  const detailPanel = page.locator("#studioDetailPanel");
  await page.locator('[data-studio-lane-id="governing_law"]').click();

  await assertTextContains(detailPanel.locator('[data-card-section="recommended-change"]'), "WHY THIS EDIT");
  await assertTextContains(detailPanel.locator('[data-card-section="recommended-change"]'), "California is outside the playbook");
  await assertTextContains(detailPanel.locator('[data-card-section="document"]'), "governed by the laws of California");
}

async function testReasoningTrailCollapse(page) {
  await loadReviewWithMatter(page, {
    clauses: [
      {
        audit_trace: {
          steps: [
            // Plumbing + the decision step the backend emits — must NOT appear in the trail.
            { details: "AI-first assessment was normalized into the review result contract.", name: "AI assessment normalization", outcome: "normalized" },
            { details: "Governing law is outside the approved set.", name: "Decision", outcome: "review" },
            // Deeper reasoning steps — these are what the trail is for.
            { details: "Located the governing-law value.", name: "Locate clause", outcome: "found" },
            { name: "Compare to approved set", outcome: "outside_approved" },
          ],
        },
        decision: "review",
        decision_reason: "Governing law is outside the approved set.",
        evidence_paragraphs: [{ id: "p2", index: 2, text: "This Agreement shall be governed by the laws of California." }],
        id: "governing_law",
        issue_label: "Needs review",
        name: "Governing Law",
        needs_review: true,
        reason_codes: ["unapproved_governing_law", "ai_first_fail"],
        reason: "Governing law is outside the approved set.",
        review_state: { state: "review" },
        status: "review",
      },
    ],
    paragraphs: [{ id: "p2", index: 2, source_index: 2, text: "This Agreement shall be governed by the laws of California." }],
  });

  const detailPanel = page.locator("#studioDetailPanel");
  await page.locator('[data-studio-lane-id="governing_law"]').click();

  const trail = detailPanel.locator(".reasoning-trail-block");
  assert.equal(await trail.count(), 1);
  assert.equal(await trail.evaluate((node) => node.open), false);
  await assertTextContains(detailPanel.locator(".reasoning-trail-summary"), "REASONING TRAIL");
  assert.equal(await trail.locator(".audit-trace-block").count(), 1);

  // Full audit history is retained. Read textContent (not innerText) since the
  // trail body is hidden while the <details> is collapsed.
  assert.equal(await detailPanel.locator(".reason-code-block").count(), 0);
  const trailText = await trail.evaluate((node) => node.textContent);
  assert.equal(trailText.includes("ai_first_fail"), false);
  assert.equal(trailText.includes("unapproved_governing_law"), false);
  assert.match(trailText, /AI assessment normalization/);
  assert.match(trailText, /Decision/);
  assert.match(trailText, /Locate clause/);
  assert.match(trailText, /Compare to approved set/);

  // Opening it persists across a re-render of the same clause.
  await detailPanel.locator(".reasoning-trail-summary").click();
  await page.waitForFunction(() => Boolean(state.reasoningTrailOpen.governing_law));
  await page.evaluate(() => renderStudioDetail());
  assert.equal(await detailPanel.locator(".reasoning-trail-block").evaluate((node) => node.open), true);
}

async function testApproveReviewGate(page) {
  // "Approve Review" is the single human sign-off: one approval covers the whole
  // matter, so there are no per-clause reviewer decisions. The gate blocks ONLY
  // on review staleness (a data-freshness guard).

  // A fresh review with an unresolved fail/review clause and NO per-clause
  // decision is approvable: the button is enabled out of the gate.
  await loadReviewWithMatter(page);

  const approveButton = page.locator("#studioApproveReviewButton");
  await page.waitForFunction(() => !document.querySelector("#studioApproveReviewButton")?.hidden);
  assert.equal(await approveButton.isDisabled(), false);
  await assertTextContains(approveButton, "Approve Review");

  // A 409 from the server (stale playbook) re-blocks the button. Wait on the
  // "blocked" class, which is set only after the 409 RESPONSE is processed — not
  // the optimistic in-flight "Approving…" disable (which toggles `.disabled`
  // alone, without `blocked`).
  await page.route("**/api/matters/matter_review_panel/approve", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        blocks_approval: ["stale_playbook"],
        error: "Approval blocked",
      }),
    });
  });
  await approveButton.click();
  await page.waitForFunction(() => document.querySelector("#studioApproveReviewButton")?.classList.contains("blocked"));
  assert.equal(await approveButton.isDisabled(), true);

  // Clearing the server-induced staleness re-enables the button (no decisions
  // needed), and a successful approve flips it to the approved state.
  await page.unroute("**/api/matters/matter_review_panel/approve");
  await page.evaluate(() => {
    state.selectedMatter = { ...state.selectedMatter, review_refresh: null };
    state.approveServerBlocks = [];
    updateApproveReviewControl();
  });
  await page.route("**/api/matters/matter_review_panel/approve", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        matter: { approved_at: "2026-06-05T11:00:00+00:00", approver: "QA", id: "matter_review_panel", status: "approved" },
      }),
    });
  });
  await page.waitForFunction(() => document.querySelector("#studioApproveReviewButton")?.disabled === false
    && !document.querySelector("#studioApproveReviewButton")?.classList.contains("blocked"));
  await approveButton.click();
  await page.waitForFunction(() => document.querySelector("#studioApproveReviewButton")?.classList.contains("approved"));
  await assertTextContains(approveButton, "Approved");
}

// WCAG 1.4.1: the document paragraph verdict must not be conveyed by colour
// alone — a flagged paragraph carries a text+icon verdict badge.
async function testDocumentVerdictLabel(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Review" }).click();
  await page.evaluate(() => {
    const paragraphs = [
      { id: "p1", index: 1, source_index: 1, text: "Confidential Information means all business information." },
      { id: "p2", index: 2, source_index: 2, text: "This Agreement shall be governed by the laws of California." },
    ];
    renderResult({
      checked_at: "2026-06-05T09:00:00+00:00",
      clauses: [
        {
          decision: "pass",
          evidence_paragraphs: [paragraphs[0]],
          id: "confidential_information",
          issue_label: "Pass",
          matched_paragraph_ids: ["p1"],
          name: "Confidential Information",
          passes: true,
          reason: "Definition is acceptable.",
          review_state: { state: "pass" },
          status: "pass",
        },
        {
          decision: "fail",
          evidence_paragraphs: [paragraphs[1]],
          id: "governing_law",
          issue_label: "Fail",
          matched_paragraph_ids: ["p2"],
          name: "Governing Law",
          reason: "Governing law is outside the approved set.",
          review_state: { state: "check" },
          status: "check",
        },
      ],
      overall_status: "needs_review",
      paragraphs,
      redline_edits: [],
      requirements_failed: 1,
      requirements_needs_review: 0,
      requirements_passed: 1,
    }, paragraphs.map((paragraph) => paragraph.text).join("\n\n"));
  });

  const passBadge = page.locator('[data-paragraph-id="p1"] .paragraph-verdict-badge');
  const failBadge = page.locator('[data-paragraph-id="p2"] .paragraph-verdict-badge');
  assert.equal(await passBadge.count(), 1, "passing paragraph should carry a verdict badge");
  assert.equal(await failBadge.count(), 1, "failing paragraph should carry a verdict badge");

  // The verdict is conveyed by TEXT (not colour alone): the badge has a label.
  assert.match(await passBadge.innerText(), /pass/i);
  assert.match(await failBadge.innerText(), /fail/i);
  // ...and a non-color icon accompanies it.
  assert.equal(await failBadge.locator(".paragraph-verdict-badge-ico").count(), 1, "verdict badge should include an icon");
  assert.equal(await passBadge.locator(".paragraph-verdict-badge-ico").count(), 1, "verdict badge should include an icon");

  // The badge sits outside the editable flow so it cannot be typed into.
  assert.equal(
    await failBadge.evaluate((node) => node.getAttribute("contenteditable")),
    "false",
    "verdict badge must not be editable",
  );
}

// A dirty redline draft must not be silently discarded by Refresh Review — the
// reviewer is asked to confirm, and cancelling aborts the refresh.
async function testRefreshUnsavedEditsGuard(page) {
  await loadReviewWithMatter(page);

  let refreshCount = 0;
  await page.route("**/api/matters/matter_review_panel/review-refresh", async (route) => {
    refreshCount += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        matter: { id: "matter_review_panel", review_result: { clauses: [] } },
        extracted_text: "Refreshed.",
        review_refresh: { stale: false },
      }),
    });
  });
  // Loading the matter list is a side effect of a successful refresh.
  await page.route("**/api/matters", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matters: [] }) });
      return;
    }
    await route.fallback();
  });

  // Clean draft: refresh runs with no confirm dialog.
  let dialogs = 0;
  const countingHandler = (dialog) => { dialogs += 1; dialog.accept(); };
  page.on("dialog", countingHandler);
  await page.evaluate(() => refreshSelectedMatterReview());
  assert.equal(dialogs, 0, "a clean draft should not prompt a confirm dialog");
  assert.equal(refreshCount, 1, "a clean draft should refresh immediately");
  page.off("dialog", countingHandler);

  // Dirty the redline draft, then cancel the confirm: refresh must NOT run.
  await page.evaluate(() => {
    state.reviewClauses = [{ id: "confidential_information", name: "Confidential Information", decision: "review", status: "review", review_state: { state: "review" } }];
    markRedlineDraftDirty();
  });
  assert.equal(await page.evaluate(() => state.redlineDraftDirty), true);
  const cancelHandler = (dialog) => dialog.dismiss();
  page.on("dialog", cancelHandler);
  await page.evaluate(() => refreshSelectedMatterReview());
  assert.equal(refreshCount, 1, "cancelling the unsaved-edits confirm must abort the refresh");
  page.off("dialog", cancelHandler);

  // Accept the confirm: refresh proceeds.
  let confirmMessage = "";
  const acceptHandler = (dialog) => { confirmMessage = dialog.message(); dialog.accept(); };
  page.on("dialog", acceptHandler);
  await page.evaluate(() => refreshSelectedMatterReview());
  assert.match(confirmMessage, /unsaved/i, "the confirm dialog should mention unsaved edits");
  assert.equal(refreshCount, 2, "accepting the unsaved-edits confirm should let the refresh run");
  page.off("dialog", acceptHandler);
}

// Accessibility: with the OS "reduce motion" preference on, transitions and
// animations are clamped to ~0 so the UI does not animate for users who asked
// not to see motion.
async function testReducedMotionPreference(page) {
  const buttonTransition = '[data-studio-lane-id="confidential_information"]';

  // Baseline (no preference): the clause-lane button has a real transition.
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await loadReviewWithMatter(page);
  await page.locator('[data-studio-lane-id="confidential_information"]').click();
  const baselineTransition = await page.locator(buttonTransition).evaluate((node) => getComputedStyle(node).transitionDuration);
  assert.ok(parseFloat(baselineTransition) > 0.05, `transition should animate when motion is allowed, got ${baselineTransition}`);

  // With reduced motion requested, the same transition collapses to ~0.
  await page.emulateMedia({ reducedMotion: "reduce" });
  await loadReviewWithMatter(page);
  await page.locator('[data-studio-lane-id="confidential_information"]').click();
  const reduced = await page.locator(buttonTransition).evaluate((node) => {
    const styles = getComputedStyle(node);
    return { animation: styles.animationDuration, transition: styles.transitionDuration };
  });
  // 0.001ms rounds toward "0s" in computed style; assert it is effectively instant.
  assert.ok(parseFloat(reduced.transition) < 0.01, `reduced-motion transition should be ~0, got ${reduced.transition}`);
  assert.ok(parseFloat(reduced.animation) < 0.01, `reduced-motion animation should be ~0, got ${reduced.animation}`);
}

async function testAdminHealthPanel(page) {
  const alertTelemetry = {
    telemetry: {
      started_at: "2026-06-07T08:00:00+00:00",
      checked_at: "2026-06-07T09:00:00+00:00",
      uptime_seconds: 3600,
      counters: {
        active_review_ai_first_attempted: 40,
        active_review_ai_first_completed: 20,
        active_review_ai_first_failed: 12,
        active_review_ai_first_fail_closed: 11,
        active_review_ai_first_partial: 4,
        active_review_deterministic_completed: 3,
        generate_nda_requests: 20,
        generate_nda_succeeded: 12,
        generate_nda_rejected: 3,
        generate_nda_failed: 6,
        generate_nda_safety_gate_blocked: 5,
        csrf_rejections: 10,
      },
    },
    health: {
      review: {
        attempted: 40,
        completed: 20,
        failed: 12,
        fail_closed: 11,
        partial: 4,
        deterministic_completed: 3,
        fail_closed_rate: 0.275,
        partial_rate: 0.1,
      },
      generation: {
        requests: 20,
        succeeded: 12,
        rejected: 3,
        failed: 6,
        safety_gate_blocked: 5,
        failure_rate: 0.3,
        gate_block_rate: 0.25,
      },
      other: {
        gmail_sync_failures: 0,
        gmail_sync_rate_limit_failures: 0,
        csrf_rejections: 10,
        host_header_rejections: 0,
        rate_limit_hits: 0,
        docx_export_content_failures: 0,
        docx_export_health_failures: 0,
        export_copy_failures: 0,
      },
      status: "alert",
      alerts: [
        "AI review has fail-closed 11 times since start.",
        "NDA generation failure rate is 30% over 20 requests.",
      ],
      note: "Counts are cumulative since process start. Telemetry is in-memory and resets on restart; these figures are NOT windowed.",
    },
  };
  const healthyTelemetry = {
    telemetry: {
      started_at: "2026-06-07T08:00:00+00:00",
      checked_at: "2026-06-07T09:00:00+00:00",
      uptime_seconds: 600,
      counters: {
        active_review_ai_first_attempted: 5,
        active_review_ai_first_completed: 5,
        generate_nda_requests: 3,
        generate_nda_succeeded: 3,
      },
    },
    health: {
      review: {
        attempted: 5,
        completed: 5,
        failed: 0,
        fail_closed: 0,
        partial: 0,
        deterministic_completed: 0,
        fail_closed_rate: 0.0,
        partial_rate: 0.0,
      },
      generation: {
        requests: 3,
        succeeded: 3,
        rejected: 0,
        failed: 0,
        safety_gate_blocked: 0,
        failure_rate: 0.0,
        gate_block_rate: 0.0,
      },
      other: {
        gmail_sync_failures: 0,
        gmail_sync_rate_limit_failures: 0,
        csrf_rejections: 0,
        host_header_rejections: 0,
        rate_limit_hits: 0,
        docx_export_content_failures: 0,
        docx_export_health_failures: 0,
        export_copy_failures: 0,
      },
      status: "ok",
      alerts: ["No AI-review or generation failure thresholds crossed."],
      note: "Counts are cumulative since process start. Telemetry is in-memory and resets on restart; these figures are NOT windowed.",
    },
  };

  let telemetryResponse = alertTelemetry;
  await page.route("**/api/telemetry", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(telemetryResponse),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Admin" }).click();
  assert.equal(await page.locator("#clausesView").getAttribute("data-admin-surface"), "admin");

  // Alerting state: banner red, alerts listed, failing metrics rendered.
  await page.locator('[data-admin-section="health"]').click();
  const healthPanel = page.locator("#adminHealthPanel");
  await assertTextContains(healthPanel, "AI review health");
  await page.waitForFunction(() => document.querySelector("#adminHealthStatus")?.getAttribute("data-health-status") === "alert");
  // .integration-status is text-transform: uppercase, so innerText is uppercased.
  assert.equal(await page.locator("#adminHealthStatus").innerText(), "ALERT");
  assert.equal(await page.locator("#adminHealthStatus").evaluate((node) => node.classList.contains("blocked")), true);
  assert.equal(await page.locator("#adminHealthAlerts").getAttribute("data-health-status"), "alert");
  await assertTextContains(page.locator("#adminHealthAlerts"), "fail-closed 11 times");
  await assertTextContains(page.locator("#adminHealthAlerts"), "failure rate is 30%");
  assert.equal(await page.locator('[data-admin-health="review-attempted"]').innerText(), "40");
  assert.equal(await page.locator('[data-admin-health="review-fail-closed"]').innerText(), "11");
  assert.equal(await page.locator('[data-admin-health="review-fail-closed-rate"]').innerText(), "27.5%");
  assert.equal(await page.locator('[data-admin-health="generation-failed"]').innerText(), "6");
  assert.equal(await page.locator('[data-admin-health="generation-failure-rate"]').innerText(), "30.0%");
  assert.equal(await page.locator('[data-admin-health="generation-gate-blocked"]').innerText(), "5");
  await assertTextContains(page.locator('[data-admin-health="other-failures"]'), "csrf_rejections 10");
  await assertTextContains(healthPanel, "cumulative since process start");
  // Raw counters live inside a collapsed <details>; assert on textContent.
  const rawCounters = await page.locator("#adminHealthRaw").evaluate((node) => node.textContent);
  assert.ok(rawCounters.includes("active_review_ai_first_fail_closed: 11"), `expected raw counters to include the fail-closed count, got "${rawCounters}"`);

  // Healthy state via Refresh: banner green, "ok" status, no failing metrics.
  telemetryResponse = healthyTelemetry;
  await page.locator("#adminHealthRefreshButton").click();
  await page.waitForFunction(() => document.querySelector("#adminHealthStatus")?.getAttribute("data-health-status") === "ok");
  assert.equal(await page.locator("#adminHealthStatus").innerText(), "HEALTHY");
  assert.equal(await page.locator("#adminHealthStatus").evaluate((node) => node.classList.contains("ready")), true);
  assert.equal(await page.locator("#adminHealthAlerts").getAttribute("data-health-status"), "ok");
  await assertTextContains(page.locator("#adminHealthAlerts"), "No AI-review or generation failure thresholds crossed.");
  assert.equal(await page.locator('[data-admin-health="review-attempted"]').innerText(), "5");
  assert.equal(await page.locator('[data-admin-health="review-fail-closed"]').innerText(), "0");
  assert.equal(await page.locator('[data-admin-health="generation-failure-rate"]').innerText(), "0.0%");
  assert.equal(await page.locator('[data-admin-health="other-failures"]').innerText(), "None");

  await page.unroute("**/api/telemetry");
}

function testPngBuffer(width, height) {
  const png = new PNG({ width, height });
  for (let offset = 0; offset < png.data.length; offset += 4) {
    png.data[offset] = 245;
    png.data[offset + 1] = 247;
    png.data[offset + 2] = 250;
    png.data[offset + 3] = 255;
  }
  return PNG.sync.write(png);
}

// Dashboard smart-search (v1): the search bar renders on the dashboard with the
// two solid chips, a chip filters the loaded matters by workflow_state.status to
// a real result, and clicking that result opens the matter (reusing the existing
// repository open-matter flow). Also asserts the page loads with no console
// errors.
async function testDashboardSmartSearch(page) {
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(String(error)));

  const matters = [
    {
      id: "m_pending",
      subject: "Acme Mutual NDA",
      sender: "legal@acme.example",
      board_column: "in_review",
      // v3: a derived counterparty (here exact, from a generated NDA's manifest) plus
      // a multi-artifact lineage so the Relationships expander has a real chain.
      counterparty: "Acme Robotics Ltd",
      current_artifact_id: "a_pending_reviewed",
      workflow_state: { status: "awaiting_approval", label: "Awaiting approval" },
      artifacts: [
        { id: "a_pending_original", role: "original", version: 1, actor: "counterparty", based_on_artifact_id: "", created_at: "2026-06-01T09:00:00+00:00", is_current: false },
        { id: "a_pending_redline", role: "redline", version: 1, actor: "ai", based_on_artifact_id: "a_pending_original", created_at: "2026-06-02T10:00:00+00:00", is_current: false },
        { id: "a_pending_reviewed", role: "reviewed", version: 1, actor: "human", based_on_artifact_id: "a_pending_redline", created_at: "2026-06-03T11:00:00+00:00", is_current: true },
      ],
    },
    {
      id: "m_sent",
      subject: "Globex One-Way NDA",
      sender: "deals@globex.example",
      board_column: "sent",
      // Same counterparty as a second Acme matter below would group together; this one
      // is its own counterparty and carries a single artifact (no earlier versions).
      counterparty: "Globex Ltd",
      current_artifact_id: "a_sent_original",
      workflow_state: { status: "sent_awaiting_counterparty", label: "Awaiting signature" },
      artifacts: [
        { id: "a_sent_original", role: "original", version: 1, actor: "counterparty", based_on_artifact_id: "", created_at: "2026-06-04T09:00:00+00:00", is_current: true },
      ],
    },
    {
      id: "m_reviewing",
      subject: "Initech Confidentiality Agreement",
      sender: "ip@initech.example",
      board_column: "in_review",
      // A second matter sharing the Acme counterparty so the grouping chip renders a
      // counterparty header with two documents under it.
      counterparty: "Acme Robotics Ltd",
      workflow_state: { status: "ai_reviewing", label: "AI reviewing" },
    },
    {
      id: "m_inbox",
      subject: "Northwind Vendor NDA",
      sender: "nda@northwind.example",
      board_column: "gmail_demo",
      counterparty: "Northwind Ltd",
      received_at: "2026-06-06T09:00:00+00:00",
      workflow_state: { status: "new", label: "Inbox" },
    },
  ];
  const openedMatterIds = [];
  await page.route("**/api/matters", async (route) => {
    // Glob also matches /api/matters/<id>; only the bare list path is served here.
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/matters") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matters }),
    });
  });
  await page.route("**/api/matters/*", async (route) => {
    const url = new URL(route.request().url());
    const matterId = decodeURIComponent(url.pathname.split("/").pop());
    const matter = matters.find((item) => item.id === matterId);
    if (!matter) {
      await route.fallback();
      return;
    }
    openedMatterIds.push(matterId);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ matter }),
    });
  });
  await page.route("**/api/dashboard/assistant", async (route) => {
    const body = JSON.parse(route.request().postData() || "{}");
    const query = String(body.query || "");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        intent: "search_filter",
        search: {
          filters: {
            status: null,
            phase: null,
            needs_attention: null,
            human_gate: null,
            has_issues: null,
            text: query,
            min_age_days: null,
            sort: null,
          },
          interpreted: query ? `matching "${query}"` : "",
        },
      }),
    });
  });
  await page.route("**/api/dashboard/search-intent", async (route) => {
    const body = JSON.parse(route.request().postData() || "{}");
    const query = String(body.query || "");
    const normalized = query.toLowerCase();
    const filters = {
      status: normalized.includes("awaiting approval") ? "awaiting_approval" : null,
      phase: null,
      needs_attention: null,
      human_gate: null,
      has_issues: null,
      text: normalized.includes("awaiting approval") ? null : query,
      min_age_days: null,
      sort: null,
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        filters,
        interpreted: filters.status ? "Awaiting approval" : (query ? `matching "${query}"` : ""),
      }),
    });
  });
  // The summary endpoint (v1.1 "Summarize a document"). Registered AFTER the
  // generic /api/matters/* route so it wins for the POST .../summary path. The
  // pending matter returns a grounded summary; the sent matter returns the
  // friendly degradation error (a 503) so we exercise both UI states.
  const summaryRequests = [];
  await page.route("**/api/matters/*/summary", async (route) => {
    const url = new URL(route.request().url());
    const matterId = decodeURIComponent(url.pathname.split("/").slice(-2, -1)[0]);
    summaryRequests.push({ matterId, method: route.request().method() });
    if (matterId === "m_sent") {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "Summary unavailable right now." }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        summary: "Mutual NDA with Acme Corp. Governed by England and Wales; 3-year term. Recommendation: needs human review.",
        model: "x-ai/grok-4.3",
        generated_at: "2026-06-07T10:00:00Z",
      }),
    });
  });
  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ gmail: { inbound: { ready: true }, outbound: { ready: true } } }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  // The simplified assistant bar renders on the dashboard with the accessible
  // search label and both legacy quick-filter chips.
  const searchSection = page.locator("[data-dashboard-search]");
  await searchSection.waitFor({ state: "visible" });
  await assertTextContains(searchSection, "Search documents");
  // Regression guard: the dashboard view owns its own vertical scroll, so a long
  // results list scrolls instead of being clipped by the fixed app-shell frame.
  const dashboardOverflowY = await page.locator("#dashboardView").evaluate((node) => getComputedStyle(node).overflowY);
  assert.equal(dashboardOverflowY, "auto");
  const pendingChip = page.locator('[data-dashboard-search-chip="pending_approval"]');
  const signatureChip = page.locator('[data-dashboard-search-chip="awaiting_signature"]');
  await assertTextContains(pendingChip, "pending approval");
  await assertTextContains(signatureChip, "awaiting signature");

  // The simplified dashboard uses the visible assistant/free-text bar. The
  // legacy chips can stay hidden for compatibility, but the visible search path
  // must still filter to exactly the matching matter.
  await page.fill("#dashboardSearchInput", "mutual");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelectorAll("#dashboardSearchResults [data-dashboard-search-open]").length === 1,
  );
  const results = page.locator("#dashboardSearchResults [data-dashboard-search-open]");
  assert.equal(await results.count(), 1);
  await assertTextContains(page.locator("#dashboardSearchResults"), "Acme Mutual NDA");
  assert.equal(await results.first().getAttribute("data-dashboard-search-open"), "m_pending");

  const resultStyles = await page.locator("#dashboardSearchResults .dashboard-search-result-row").first().evaluate((node) => {
    const button = getComputedStyle(node.querySelector(".dashboard-search-result-button"));
    const title = getComputedStyle(node.querySelector(".dashboard-search-result-title"));
    const status = getComputedStyle(node.querySelector(".dashboard-search-result-status"));
    const summarize = getComputedStyle(node.querySelector(".dashboard-search-result-summarize"));
    const relationships = getComputedStyle(node.querySelector(".dashboard-search-result-relationships"));
    return {
      buttonBackground: button.backgroundColor,
      titleColor: title.color,
      statusColor: status.color,
      summarizeColor: summarize.color,
      relationshipsColor: relationships.color,
    };
  });
  assert.notEqual(resultStyles.buttonBackground, "rgba(255, 255, 255, 0.1)");
  assert.notEqual(resultStyles.titleColor, "rgb(255, 255, 255)");
  assert.notEqual(resultStyles.statusColor, "rgb(255, 255, 255)");
  assert.notEqual(resultStyles.summarizeColor, "rgb(255, 255, 255)");
  assert.notEqual(resultStyles.relationshipsColor, "rgb(255, 255, 255)");
  const dashboardMetrics = page.locator("[data-dashboard-metrics]");
  assert.equal(await dashboardMetrics.locator('[data-dashboard-repository-count="gmail_demo"]').innerText(), "1");
  assert.equal(await dashboardMetrics.locator('[data-dashboard-repository-count="in_review"]').innerText(), "2");
  assert.equal(await dashboardMetrics.locator('[data-dashboard-repository-count="reviewed"]').innerText(), "0");
  assert.equal(await dashboardMetrics.locator('[data-dashboard-repository-count="sent"]').innerText(), "1");

  const inboxTable = page.locator("[data-dashboard-inbox]");
  await inboxTable.waitFor({ state: "visible" });
  await assertTextContains(inboxTable, "Intake Queue");
  assert.equal((await inboxTable.innerText()).includes("Inbox queue"), false);
  await page.waitForFunction(
    () => document.querySelectorAll("[data-dashboard-inbox-body] tr").length === 1,
  );
  assert.deepEqual(
    await page.$$eval(".dashboard-inbox-table thead th", (nodes) => nodes.map((node) => node.textContent.trim())),
    ["Document Name", "Counterparty", "Sender", "Date", "Action"],
  );
  await assertTextContains(inboxTable, "Northwind Vendor NDA");
  await assertTextContains(inboxTable, "Northwind Ltd");
  await assertTextContains(inboxTable, "nda@northwind.example");
  await assertTextContains(inboxTable, "06 Jun");
  assert.equal(await inboxTable.locator("tbody tr").count(), 1);
  assert.equal(await inboxTable.locator("text=Acme Mutual NDA").count(), 0);
  assert.equal(await inboxTable.locator("[data-dashboard-inbox-count]").innerText(), "1 DOCUMENT");
  const inboxStyles = await inboxTable.evaluate((node) => {
    const shell = getComputedStyle(node.querySelector(".dashboard-inbox-table-shell"));
    const header = getComputedStyle(node.querySelector("th"));
    const action = getComputedStyle(node.querySelector(".dashboard-inbox-action"));
    return {
      shellBackground: shell.backgroundColor,
      headerTransform: header.textTransform,
      headerColor: header.color,
      actionColor: action.color,
      actionBorderRadius: action.borderRadius,
    };
  });
  assert.match(inboxStyles.shellBackground, /rgba\(255, 255, 255, 0\.(6|7|8)/);
  assert.equal(inboxStyles.headerTransform, "uppercase");
  assert.notEqual(inboxStyles.headerColor, "rgb(255, 255, 255)");
  assert.notEqual(inboxStyles.actionColor, "rgb(255, 255, 255)");
  assert.equal(inboxStyles.actionBorderRadius, "999px");
  await page.locator('[data-dashboard-inbox-open="m_inbox"]').click();
  await page.waitForFunction(() => document.querySelector('[data-view="repository"]')?.classList.contains("active"));
  assert.ok(openedMatterIds.includes("m_inbox"), "expected the Inbox table action to open m_inbox");
  await page.locator("#repositoryMatterPanel .repository-detail-close").click();
  await page.waitForSelector("#repositoryMatterPanel[hidden]", { state: "attached" });
  await page.locator('[data-tab="dashboard"]').click();

  // Free-text keyword search matches subject; non-matches show the empty state.
  await page.fill("#dashboardSearchInput", "globex");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelector("#dashboardSearchResults")?.innerText.includes("Globex One-Way NDA"),
  );
  await assertTextContains(page.locator("#dashboardSearchResults"), "Globex One-Way NDA");

  await page.fill("#dashboardSearchInput", "no-such-document");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelector("#dashboardSearchResultsStatus")?.innerText.includes("No documents match"),
  );

  // Clicking a result opens that matter via the existing repository flow.
  await page.fill("#dashboardSearchInput", "mutual");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelectorAll("#dashboardSearchResults [data-dashboard-search-open]").length === 1,
  );
  await page.locator('[data-dashboard-search-open="m_pending"]').click();
  await page.waitForFunction(() => document.querySelector('[data-view="repository"]')?.classList.contains("active"));
  assert.ok(openedMatterIds.includes("m_pending"), "expected the repository open-matter flow to fetch m_pending");

  // --- "Summarize a document" (v1.1) ----------------------------------------
  // Back on the dashboard, each result row has a Summarize affordance. Clicking it
  // POSTs to the summary endpoint and renders a grounded, AI-LABELED summary inline.
  // First dismiss the matter inspector the open-matter step left up (it overlays the
  // tab strip as a modal dialog), then return to the dashboard tab.
  await page.locator("#repositoryMatterPanel .repository-detail-close").click();
  await page.waitForSelector("#repositoryMatterPanel[hidden]", { state: "attached" });
  await page.locator("#dashboardTab").click();
  await searchSection.waitFor({ state: "visible" });
  // Use a fresh free-text search (deterministic regardless of the chip toggle state
  // the earlier steps left behind) to surface exactly the m_pending row.
  await page.fill("#dashboardSearchInput", "acme");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelectorAll('#dashboardSearchResults [data-dashboard-search-summarize="m_pending"]').length === 1,
  );
  const summarizeButton = page.locator('[data-dashboard-search-summarize="m_pending"]');
  await summarizeButton.click();
  // The panel resolves to a ready summary, explicitly labeled "AI summary".
  const summaryPanel = page.locator('[data-dashboard-search-summary-for="m_pending"]');
  await page.waitForFunction(
    () => document.querySelector('[data-dashboard-search-summary-for="m_pending"]')?.dataset.state === "ready",
  );
  // The label is uppercased by CSS (text-transform), so the rendered text is
  // "AI SUMMARY". The panel must carry the AI-summary label so it is never mistaken
  // for verified fact.
  await assertTextContains(summaryPanel, "AI SUMMARY");
  await assertTextContains(summaryPanel, "Governed by England and Wales");
  assert.ok(
    summaryRequests.some((req) => req.matterId === "m_pending" && req.method === "POST"),
    "expected a POST to the m_pending summary endpoint",
  );
  // Re-clicking Summarize collapses the open panel (toggle off).
  await summarizeButton.click();
  await page.waitForFunction(
    () => document.querySelector('[data-dashboard-search-summary-for="m_pending"]')?.hidden === true,
  );

  // A degraded summary (503) shows the friendly "Summary unavailable" message,
  // never a stack trace.
  await page.fill("#dashboardSearchInput", "globex");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelectorAll("#dashboardSearchResults [data-dashboard-search-summarize]").length === 1,
  );
  await page.locator('[data-dashboard-search-summarize="m_sent"]').click();
  await page.waitForFunction(
    () => document.querySelector('[data-dashboard-search-summary-for="m_sent"]')?.dataset.state === "error",
  );
  await assertTextContains(
    page.locator('[data-dashboard-search-summary-for="m_sent"]'),
    "Summary unavailable right now.",
  );

  // --- "Show how documents relate" (v3 Relationships expander) ---------------
  // The per-row Relationships affordance expands that matter's document lineage inline
  // as a factual timeline — built from the matter's own artifacts, NOT an AI call.
  await page.fill("#dashboardSearchInput", "acme");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelectorAll('#dashboardSearchResults [data-dashboard-search-relationships="m_pending"]').length === 1,
  );
  const pendingRelationships = page.locator('[data-dashboard-search-relationships="m_pending"]').first();
  await pendingRelationships.click();
  const lineagePanel = page.locator('[data-dashboard-search-lineage-for="m_pending"]').first();
  await page.waitForFunction(
    () => {
      const panel = document.querySelector('[data-dashboard-search-lineage-for="m_pending"]');
      return panel && !panel.hidden && panel.querySelectorAll(".dashboard-search-lineage-node").length === 3;
    },
  );
  // Ordered root -> derived (original -> redline -> reviewed), with the current
  // artifact marked and the actors labelled. This is a structured view, not AI.
  const lineageRoles = await page.$$eval(
    '[data-dashboard-search-lineage-for="m_pending"] .dashboard-search-lineage-role',
    (nodes) => nodes.map((n) => n.textContent.replace(/\s+/g, " ").trim()),
  );
  assert.match(lineageRoles[0], /^Original/);
  assert.match(lineageRoles[1], /^Redline/);
  assert.match(lineageRoles[2], /Reviewed/);
  // Exactly the reviewed (current) node carries the "Current" marker.
  assert.equal(
    await lineagePanel.locator(".dashboard-search-lineage-current").count(),
    1,
    "expected exactly one artifact flagged as current in the lineage",
  );
  await assertTextContains(lineagePanel, "Legal reviewer"); // human actor label
  // Re-clicking Relationships collapses the panel (toggle off).
  await pendingRelationships.click();
  await page.waitForFunction(
    () => document.querySelector('[data-dashboard-search-lineage-for="m_pending"]')?.hidden === true,
  );

  // A single-artifact matter shows the friendly "No earlier versions yet." line.
  await page.fill("#dashboardSearchInput", "globex");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelectorAll('#dashboardSearchResults [data-dashboard-search-relationships="m_sent"]').length === 1,
  );
  const sentRelationships = page.locator('[data-dashboard-search-relationships="m_sent"]').first();
  await sentRelationships.click();
  await page.waitForFunction(
    () => {
      const panel = document.querySelector('[data-dashboard-search-lineage-for="m_sent"]');
      return panel && !panel.hidden && /No earlier versions yet\./.test(panel.innerText);
    },
  );

  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/*");
  await page.unroute("**/api/matters/*/summary");
  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/dashboard/assistant");
  await page.unroute("**/api/dashboard/search-intent");

  // The dashboard search bar must add no console errors of its own. We ignore two
  // feature-independent messages:
  //  1. A pre-existing race: under the mocked-route fast load the classic repository
  //     board can render (using the bridged global `escapeHtml`) a tick before the
  //     deferred global-bridge.mjs module assigns it — a load-order artifact of the
  //     existing app, not of this feature (the search controller's own escapeHtml is
  //     self-contained).
  //  2. The browser's automatic "Failed to load resource ... 503" log for the
  //     summary degradation path we deliberately exercise above. That 503 is the
  //     EXPECTED graceful-degradation response (the UI shows the friendly message);
  //     it is a browser network log, not a JS error the feature emits.
  const unexpectedErrors = consoleErrors.filter(
    (text) =>
      !/escapeHtml is not defined/.test(text) &&
      !/Failed to load resource.*503/.test(text),
  );
  assert.equal(
    unexpectedErrors.length,
    0,
    `expected no console errors from the dashboard search, got: ${unexpectedErrors.join(" | ")}`,
  );
}

// Assistant bar: the free-text box calls /dashboard/assistant. Search-filter
// responses still validate + apply structured filters to real state.matters, while
// repository answers, confirmation-required Generator actions, and unsupported
// messages render as assistant cards.
async function testDashboardSmartSearchV2(page) {
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(String(error)));

  const daysAgo = (n) => new Date(Date.now() - n * 86400000).toISOString();
  const matters = [
    {
      id: "m_old_review",
      subject: "Acme Mutual NDA",
      sender: "legal@acme.example",
      board_column: "in_review",
      created_at: daysAgo(30),
      requirements_failed: 2,
      requirements_needs_review: 0,
      workflow_state: { status: "review_failed", phase: "review", label: "Review failed", needs_attention: true, human_gate: false },
    },
    {
      id: "m_fresh_review",
      subject: "Globex One-Way NDA",
      sender: "deals@globex.example",
      board_column: "in_review",
      created_at: daysAgo(1),
      requirements_failed: 0,
      requirements_needs_review: 0,
      workflow_state: { status: "ai_reviewing", phase: "review", label: "AI reviewing", needs_attention: false, human_gate: false },
    },
    {
      id: "m_sent",
      subject: "Initech Confidentiality Agreement",
      sender: "ip@initech.example",
      board_column: "sent",
      created_at: daysAgo(5),
      requirements_failed: 0,
      requirements_needs_review: 0,
      workflow_state: { status: "sent_awaiting_counterparty", phase: "sent", label: "Awaiting signature", needs_attention: false, human_gate: true },
    },
  ];
  await page.route("**/api/matters", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/matters") {
      await route.fallback();
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matters }) });
  });

  const assistantRequests = [];
  await page.route("**/api/dashboard/assistant", async (route) => {
    const body = JSON.parse(route.request().postData() || "{}");
    const query = String(body.query || "");
    assistantRequests.push(query);
    if (/what can you do/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "system_question",
          domain: "assistant",
          question: "capability_catalog",
          answer: {
            text: "I can search matters, answer repository and Playbook questions, and start safe workflows with confirmation.",
            domains: ["generation", "repository", "gmail", "playbook", "admin"],
            capabilities: [
              {
                name: "generate_nda",
                domain: "generation",
                description: "Open/prefill the Generator after explicit confirmation; never silently generate.",
              },
              {
                name: "count_in_review",
                domain: "repository",
                description: "Count owner-scoped matters currently in review.",
              },
            ],
          },
          citations: [],
        }),
      });
      return;
    }
    if (/playbook clauses/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "system_question",
          domain: "playbook",
          question: "playbook_clause_count",
          answer: {
            text: "Aspora NDA hard clauses has 6 clauses.",
            count: 6,
            playbook_name: "Aspora NDA hard clauses",
          },
          citations: [{ source: "playbook", title: "Aspora NDA hard clauses", version: "0.1.0" }],
        }),
      });
      return;
    }
    if (/message template/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "system_question",
          domain: "gmail",
          question: "outbound_email_templates",
          answer: {
            text: "Outbound redline emails default to a reply-style subject and a short Aspora Legal body.",
          },
          citations: [{ source: "code", title: "nda_automation/gmail_matter_outbox.py" }],
        }),
      });
      return;
    }
    if (/sync gmail/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "action_request",
          domain: "gmail",
          action: "open_gmail_sync",
          label: "Review Gmail sync",
          requires_confirmation: true,
          message: "I can take you to the Gmail controls. Sync/import is not started from the assistant response.",
          target: { tab: "admin" },
          side_effects: ["gmail_import_or_sync"],
        }),
      });
      return;
    }
    if (/how many/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "repository_question",
          question: "count_in_review",
          answer: { text: "2 documents are in review.", count: 2, phase: "review" },
          citations: [
            { matter_id: "m_old_review", title: "Acme Mutual NDA", workflow_phase: "review" },
            { matter_id: "m_fresh_review", title: "Globex One-Way NDA", workflow_phase: "review" },
          ],
        }),
      });
      return;
    }
    if (/generate/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "draft_action_request",
          action: "open_generator",
          requires_confirmation: true,
          message: "I can help start an NDA draft. Open the Generator, review the intake, then choose Generate when you are ready.",
          generator: {
            prefill: { source: "dashboard_assistant", prompt: query },
            missing_fields: ["signing_entity", "counterparty_name", "purpose"],
          },
          side_effects: [],
        }),
      });
      return;
    }
    if (/unsupported/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "unsupported",
          message: "I can search matters, answer repository status questions, or help start an NDA draft. I cannot do that request yet.",
        }),
      });
      return;
    }
    if (/clarify/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "clarification",
          domain: "assistant",
          message: "Which workflow should I inspect?",
          questions: ["Repository", "Gmail inbox", "Review queue"],
        }),
      });
      return;
    }
    if (/globex/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "search_filter",
          search: {
            filters: {
              status: null,
              phase: null,
              needs_attention: null,
              human_gate: null,
              has_issues: null,
              text: "Globex",
              min_age_days: null,
              sort: null,
            },
            interpreted: 'matching "Globex"',
          },
        }),
      });
      return;
    }
    if (/initech/i.test(query)) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          intent: "search_filter",
          search: {
            filters: {
              status: null,
              phase: null,
              needs_attention: null,
              human_gate: null,
              has_issues: null,
              text: "Initech",
              min_age_days: null,
              sort: null,
            },
            interpreted: 'matching "Initech"',
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        intent: "search_filter",
        search: {
          filters: {
            status: null,
            phase: "review",
            needs_attention: null,
            human_gate: null,
            has_issues: null,
            text: null,
            min_age_days: 7,
            sort: null,
          },
          interpreted: "In review · older than 7 days",
        },
      }),
    });
  });
  let generateCalls = 0;
  await page.route("**/api/generate-nda", async (route) => {
    generateCalls += 1;
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: "Generate should not be called by dashboard assistant confirmation." }),
    });
  });
  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ gmail: { inbound: { ready: true }, outbound: { ready: true } } }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  const searchSection = page.locator("[data-dashboard-search]");
  await searchSection.waitFor({ state: "visible" });

  // --- Natural-language query -> AI-translated filter applied to real matters ---
  await page.fill("#dashboardSearchInput", "anything stuck in review for more than a week");
  await page.locator("#dashboardSearchForm").press("Enter");
  // The interpreted line shows HOW the query was read ("Showing: <interpreted>").
  await page.waitForFunction(
    () => document.querySelector("#dashboardSearchInterpreted")?.innerText.includes("In review · older than 7 days"),
  );
  await assertTextContains(page.locator("#dashboardSearchInterpreted"), "Showing: In review · older than 7 days");
  // The validated spec is applied to the REAL matters: exactly the old, in-review
  // matter survives (the fresh one is younger than 7 days; the sent one is not in
  // review). The result is a real matter, never fabricated.
  await page.waitForFunction(
    () => document.querySelectorAll("#dashboardSearchResults [data-dashboard-search-open]").length === 1,
  );
  const results = page.locator("#dashboardSearchResults [data-dashboard-search-open]");
  assert.equal(await results.count(), 1);
  assert.equal(await results.first().getAttribute("data-dashboard-search-open"), "m_old_review");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Acme Mutual NDA");
  assert.ok(assistantRequests.length >= 1, "expected a POST to the assistant endpoint");

  // --- Assistant search_filter with keyword text still filters real matters -----
  await page.fill("#dashboardSearchInput", "globex");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelector("#dashboardSearchResults")?.innerText.includes("Globex One-Way NDA"),
  );
  await assertTextContains(page.locator("#dashboardSearchResults"), "Globex One-Way NDA");
  await page.waitForFunction(
    () => document.querySelectorAll("#dashboardSearchResults [data-dashboard-search-open]").length === 1,
  );
  await assertTextContains(page.locator("#dashboardSearchInterpreted"), 'Showing: matching "Globex"');

  // --- Repository question renders a readable answer + citations ---------------
  await page.fill("#dashboardSearchInput", "How many are in review?");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForSelector('[data-dashboard-assistant-response="repository_question"]');
  await assertTextContains(page.locator("#dashboardSearchResults"), "REPOSITORY ANSWER");
  await assertTextContains(page.locator("#dashboardSearchResults"), "2 documents are in review.");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Acme Mutual NDA");

  // --- System questions render as assistant answers, not document no-results ---
  await page.fill("#dashboardSearchInput", "How many playbook clauses do we have?");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForSelector('[data-dashboard-assistant-response="system_question"]');
  await assertTextContains(page.locator("#dashboardSearchResults"), "SYSTEM ANSWER");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Aspora NDA hard clauses has 6 clauses.");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Aspora NDA hard clauses");

  await page.fill("#dashboardSearchInput", "What is the message template that we have for emails that we send?");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForSelector('[data-dashboard-assistant-response="system_question"]');
  await assertTextContains(page.locator("#dashboardSearchResults"), "Outbound redline emails default");

  await page.fill("#dashboardSearchInput", "What can you do?");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForSelector('[data-dashboard-assistant-response="system_question"]');
  await assertTextContains(page.locator("#dashboardSearchResults"), "Covers: generation, repository, gmail, playbook, admin");
  await assertTextContains(page.locator("#dashboardSearchResults"), "generation: Open/prefill the Generator");

  // --- Safe workflow requests render confirmation-gated action cards ----------
  await page.fill("#dashboardSearchInput", "Sync Gmail inbox");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForSelector('[data-dashboard-assistant-response="action_request"]');
  await assertTextContains(page.locator("#dashboardSearchResults"), "CONFIRMATION REQUIRED");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Action needs confirmation");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Will happen: open Admin so you can inspect Gmail connection and sync controls.");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Will not happen: import, sync, send, archive, delete, or modify Gmail messages.");
  await assertTextContains(page.locator("#dashboardSearchResults"), "No sync or import starts from this dashboard response.");
  assertAttributeMatches(
    page.locator('[data-dashboard-assistant-action="open_gmail_sync"]'),
    "aria-label",
    /Confirm and Review Gmail sync/,
  );

  // --- Action request requires confirmation and only opens/prefills Generator ---
  await page.goto(`${BASE_URL}/?dashboardSearch=Generate+an+NDA`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("[data-dashboard-search]");
  await page.waitForSelector('[data-dashboard-assistant-response="draft_action_request"]');
  await assertTextContains(page.locator("#dashboardSearchResults"), "CONFIRMATION REQUIRED");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Action needs confirmation");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Will happen: open Generator and prefill the prompt as draft context.");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Will not happen: generate, save, send, export, delete, or approve a document.");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Nothing is generated until you choose Generate there.");
  assertAttributeMatches(
    page.locator('[data-dashboard-assistant-action="open_generator"]'),
    "aria-label",
    /Confirm and Open Generator/,
  );
  // The action button calls window.confirm for requires_confirmation actions.
  // Accept the dialog so the generator opens.
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator('[data-dashboard-assistant-action="open_generator"]').click();
  await page.waitForSelector("#generatorView:not([hidden])");
  assert.equal(await page.locator("#generatorTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#draftIntakeProjectPurpose").inputValue(), "Generate an NDA");
  assert.equal(generateCalls, 0, "dashboard assistant must not silently call /api/generate-nda");

  await page.locator("#dashboardTab").click();
  await page.waitForSelector("#dashboardView:not([hidden])");

  // --- Unsupported requests render a clear message -----------------------------
  await page.fill("#dashboardSearchInput", "unsupported command please");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForSelector('[data-dashboard-assistant-response="unsupported"]');
  await assertTextContains(page.locator("#dashboardSearchResults"), "UNSUPPORTED");
  await assertTextContains(page.locator("#dashboardSearchResults"), "I cannot do that request yet");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Repository: ask “How many are in review?”");
  await assertTextContains(page.locator("#dashboardSearchResults"), "System: ask about the Playbook");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Workflows: ask to generate an NDA");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Open Repository");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Open Generator");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Open Admin");
  await page.locator('[data-dashboard-assistant-action="guide_open_admin"]').click();
  await page.waitForSelector("#clausesView[data-admin-surface='admin']");
  assert.equal(await page.locator("#adminTab").getAttribute("aria-selected"), "true");
  await page.locator("#dashboardTab").click();
  await page.waitForSelector("#dashboardView:not([hidden])");

  await page.fill("#dashboardSearchInput", "clarify this request");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForSelector('[data-dashboard-assistant-response="clarification"]');
  await assertTextContains(page.locator("#dashboardSearchResults"), "CLARIFICATION");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Which workflow should I inspect?");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Gmail inbox");
  await assertTextContains(page.locator("#dashboardSearchResults"), "Open Gmail inbox");
  await page.locator('[data-dashboard-assistant-action="clarify_admin_1"]').click();
  await page.waitForSelector("#clausesView[data-admin-surface='admin']");
  assert.equal(await page.locator("#adminTab").getAttribute("aria-selected"), "true");
  await page.locator("#dashboardTab").click();
  await page.waitForSelector("#dashboardView:not([hidden])");

  // Visible free-text search still returns the sent matter through the assistant
  // search_filter path.
  await page.fill("#dashboardSearchInput", "initech");
  await page.locator("#dashboardSearchForm").press("Enter");
  await page.waitForFunction(
    () => document.querySelectorAll("#dashboardSearchResults [data-dashboard-search-open]").length === 1,
  );
  assert.equal(
    await page.locator("#dashboardSearchResults [data-dashboard-search-open]").first().getAttribute("data-dashboard-search-open"),
    "m_sent",
  );

  await page.unroute("**/api/matters");
  await page.unroute("**/api/dashboard/assistant");
  await page.unroute("**/api/generate-nda");
  await page.unroute("**/api/gmail/status");

  const unexpectedErrors = consoleErrors.filter((text) => !/escapeHtml is not defined/.test(text));
  assert.equal(
    unexpectedErrors.length,
    0,
    `expected no console errors from the v2 dashboard search, got: ${unexpectedErrors.join(" | ")}`,
  );
}

// A new inbound NDA arriving in the matter list pops a top-right toast. The inbox
// already present at load is seeded SILENTLY (no toast); only genuinely new arrivals
// during the session toast, and clicking one opens that matter for review.
async function testInboundNotificationToast(page) {
  const matters = [
    {
      id: "m_inbound_seed",
      subject: "Seedco Mutual NDA",
      sender: "legal@seedco.example",
      board_column: "in_review",
      source_type: "gmail_inbound",
      counterparty: "Seedco Ltd",
      created_at: "2026-06-05T09:00:00+00:00",
      workflow_state: { status: "ai_reviewing", label: "AI reviewing" },
    },
  ];
  const newInbound = {
    id: "m_inbound_new",
    subject: "Acme Mutual NDA",
    sender: "legal@acme.example",
    board_column: "in_review",
    source_type: "gmail_inbound",
    counterparty: "Acme Robotics Ltd",
    attachment_filename: "Mutual NDA - Acme.docx",
    created_at: "2026-06-07T15:30:00+00:00",
    workflow_state: { status: "ai_reviewing", label: "AI reviewing" },
  };

  await page.route("**/api/matters", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/matters") {
      await route.fallback();
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matters }) });
  });
  await page.route("**/api/matters/*", async (route) => {
    const url = new URL(route.request().url());
    const matterId = decodeURIComponent(url.pathname.split("/").pop());
    const matter = matters.find((item) => item.id === matterId);
    if (!matter) {
      await route.fallback();
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ matter }) });
  });
  await page.route("**/api/gmail/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ gmail: { inbound: { ready: true }, outbound: { ready: true } } }),
    });
  });

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });

  // Activate Repository so loadMatters resolves and the notifier seeds {seed}
  // SILENTLY. The seed matter's card confirms the seeding observe has run.
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.waitForSelector(".repository-card");
  await page.locator("#toastStack .toast [data-toast-close]").click({ trial: false }).catch(() => {});
  await page.waitForFunction(() => document.querySelectorAll("#toastStack .toast").length === 0);
  assert.equal(await page.locator("#toastStack .toast").count(), 0);

  // A new inbound NDA arrives, then the matter list refreshes (tab re-activation
  // stands in for the 15s poll). Only the NEW matter should toast.
  matters.push(newInbound);
  await page.getByRole("tab", { name: "Dashboard" }).click();
  await page.getByRole("tab", { name: "Repository" }).click();

  const toast = page.locator("#toastStack .toast");
  await toast.first().waitFor({ state: "visible" });
  assert.equal(await toast.count(), 1);
  await assertTextContains(toast.first(), "New NDA from Acme Robotics Ltd");
  await assertTextContains(toast.first(), "Mutual NDA - Acme.docx");
  await assertTextContains(toast.first(), "Click to review");
  // Layout guard: the card must grow to fit title + filename + meta. The global
  // `button { height: 32px }` once clipped the open-button to one line (innerText
  // still passed, but the card was visually cut), so assert a multi-line height.
  const cardHeight = await toast.first().evaluate((node) => node.getBoundingClientRect().height);
  assert.ok(cardHeight >= 50, `toast card should fit its content; got ${cardHeight}px`);

  // Clicking the toast opens that matter for review and dismisses the toast.
  await toast.first().locator("[data-toast-open]").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Acme");
  await page.waitForFunction(() => document.querySelectorAll("#toastStack .toast").length === 0);

  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/*");
  await page.unroute("**/api/dashboard/assistant");
  await page.unroute("**/api/gmail/status");
}

async function assertTextContains(locator, expected) {
  const text = await locator.innerText();
  assert.ok(text.includes(expected), `expected "${text}" to include "${expected}"`);
}

async function assertAttributeMatches(locator, attribute, expected) {
  const value = await locator.getAttribute(attribute);
  assert.match(value || "", expected);
}

async function waitForText(page, selector, expected) {
  await page.waitForFunction(
    ({ selector, expected }) => document.querySelector(selector)?.innerText.includes(expected),
    { selector, expected },
  );
}

async function openDownloadMenu(trigger) {
  await trigger.click();
  const menu = trigger.page().locator("[data-document-download-menu]");
  await menu.waitFor({ state: "visible" });
  return menu;
}

async function chooseDownloadFormat(trigger, format) {
  const menu = await openDownloadMenu(trigger);
  const option = menu.locator(`[data-download-format="${format.toLowerCase()}"]`).first();
  assert.equal(await option.count(), 1, `expected ${format} download option`);
  assert.equal(await option.isDisabled(), false, `${format} download option should be enabled`);
  await option.click();
}

async function waitForRepositoryCount(page, column, expected) {
  await page.waitForFunction(
    ({ column, expected }) => document.querySelector(`[data-repository-count="${column}"]`)?.textContent.trim() === expected,
    { column, expected },
  );
}

async function createRepositoryMatter(page, docxPath, overrides = {}) {
  const filename = path.basename(docxPath);
  const payload = {
    filename,
    content_base64: fs.readFileSync(docxPath).toString("base64"),
    source_type: "manual_upload",
    sender: "Manual upload",
    subject: filename.replace(/\.[^.]*$/, ""),
    received_at: "2026-05-31T12:00:00+00:00",
    message_snippet: `Manual upload of ${filename}.`,
    attachment_filename: filename,
    ...overrides,
  };
  return page.evaluate(async (matterPayload) => {
    const response = await fetch("/api/matters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(matterPayload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Matter could not be created");
    return result.matter;
  }, payload);
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
