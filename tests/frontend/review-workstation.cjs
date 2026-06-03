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
  ["edits playbook admin drafts with Pass/Check policy framing", testPlaybookAdminEditor],
  ["renders contract structure map in review and engine logic in admin", testContractStructureReviewPanel],
  ["surfaces review and export error details", testFailureUxDetails],
  ["surfaces structured evidence and rationale", testStructuredEvidenceAndRationale],
  ["guards Save-As picker fallbacks", testSavePickerGuardsAndFallbacks],
  ["renders server-provided inline diff operations", testInlineDiffOperationRendering],
  ["renders backend redlines across all document modes", testBackendRedlineModes],
  ["imports repository matters and re-reviews as fresh text", testRepositoryMatterImportAndFreshReview],
  ["clears repository board after load errors", testRepositoryLoadErrorClearsBoard],
  ["uploads local NDAs through the Upload tab", testManualUploadTab],
  ["sends repository redline email with composer details", testRepositoryOutboundSendComposer],
  ["sends review redline email from editable composer", testReviewOutboundSendModal],
  ["blocks repository outbound send when Gmail is not ready", testRepositoryOutboundSendBlocked],
  ["shows Gmail setup required instead of stale sync errors", testGmailSetupRequiredStatus],
  ["persists matter redline drafts", testMatterRedlineDraftPersistence],
  ["cycles clause-to-paragraph anchors", testClauseAnchorCycling],
  ["exports selected clause decisions and template options", testClauseDecisionControls],
  ["renders manual viewer edits as local redlines", testManualViewerEditRedline],
  ["preserves viewer caret through auto-refresh", testViewerAutoRefreshSelection],
  ["keeps browser preview aligned with exported DOCX redlines", testPreviewMatchesExportedDocx],
  ["guards source-redline export regression", testSourceRedlineExportRegression],
  ["marks the exported matter ready after a mid-export switch", testExportMarksCapturedMatterReady],
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
  assert.equal(await page.getByRole("tablist", { name: "Workspace" }).count(), 1);
  assert.equal(await page.locator("#reviewTab").getAttribute("role"), "tab");
  assert.equal(await page.locator("#clausesTab").getAttribute("role"), "tab");
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#clausesTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#clausesView").getAttribute("hidden"), "");
  assert.equal(await page.getByRole("textbox", { name: "NDA source text" }).count(), 1);
  const matterCardStyles = await page.locator(".studio-matter-card").evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      borderRadius: styles.borderRadius,
      boxShadow: styles.boxShadow,
    };
  });
  assert.equal(matterCardStyles.borderRadius, "16px");
  assert.equal(matterCardStyles.boxShadow, "none");
  assert.equal(await page.locator(".studio-check-card").count(), 0);
  assert.equal(await page.locator(".studio-playbook > h2").innerText(), "SELECTED CLAUSE");
  assert.equal(await page.locator("#studioMatchSummary").innerText(), "0/6");

  await page.locator("#reviewTab").focus();
  await page.locator("#reviewTab").press("ArrowRight");
  assert.equal(await page.locator("#clausesTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#clausesTab").getAttribute("tabindex"), "0");
  assert.equal(await page.locator("#reviewTab").getAttribute("tabindex"), "-1");
  await page.locator("#clausesTab").press("Home");
  assert.equal(await page.locator("#repositoryTab").getAttribute("aria-selected"), "true");
  await page.locator("#repositoryTab").press("End");
  assert.equal(await page.locator("#clausesTab").getAttribute("aria-selected"), "true");
  await page.locator("#clausesTab").press("Home");
  assert.equal(await page.locator("#repositoryTab").getAttribute("aria-selected"), "true");

  await page.getByRole("tab", { name: "Admin" }).click();
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
  assert.equal(activePlaybookRow.backgroundColor, "rgb(250, 250, 252)");
  assert.equal(activePlaybookRow.borderLeftColor, "rgb(96, 40, 200)");
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
  await page.getByRole("tab", { name: "Admin" }).click();
  await assertTextContains(page.locator("#adminPlaybookPanel"), "Aspora playbook");
  await assertTextContains(page.locator("#clauseDetail"), "Edit Clause: Mutuality");
  await assertTextContains(page.locator("#clauseDetail"), "Check Trigger Position");
  await assertTextContains(page.locator("#clauseDetail"), "Required - Check if absent or deficient");
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
  await assertTextContains(page.locator("#clauseDetail"), "analysis_purpose");
  await assertTextContains(page.locator("#clauseDetail"), "primary_inputs");
  await assertTextContains(page.locator("#clauseDetail"), "reason_code_taxonomy");
  await assertTextContains(page.locator("#clauseDetail"), "hardening_guards");
  await assertTextContains(page.locator("#clauseDetail"), "mutuality_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "weak_mutuality_paragraph_ids");
  await assertTextContains(page.locator("#clauseDetail"), "mutuality");
  assert.equal(await page.getByText("Walk-away", { exact: false }).count(), 0);
  assert.equal(await page.getByText("Negotiate", { exact: false }).count(), 0);
  assert.equal(await page.getByText("Severity", { exact: false }).count(), 0);
  assert.equal(await page.getByText("Category Group", { exact: false }).count(), 0);
  await page.getByRole("button", { name: "Confidential Information" }).click();
  await assertTextContains(page.locator("#clauseDetail"), "Standard Exclusions Language");
  await assertTextContains(page.locator("#clauseDetail"), "confidential_information_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "usage_right_review_paragraph_ids");
  assert.equal(await page.getByText("Confidential-Info Exclusions Allowlist", { exact: false }).count(), 0);
  assert.equal(await page.getByPlaceholder("Add exclusion key").count(), 0);
  await page.locator('textarea[name="standard_exclusions_template"]').fill("Publicly known information is excluded.");
  await assertTextContains(page.locator("#playbookDraftDiff"), "standard_exclusions_template");
  await page.locator('[data-clause-id="term_and_survival"]').click();
  await assertTextContains(page.locator("#clauseDetail"), "Ordinary Confidentiality Cap (years)");
  await assertTextContains(page.locator("#clauseDetail"), "Permitted Perpetual / Longer Survival Carve-outs");
  await assertTextContains(page.locator("#clauseDetail"), "Perpetual / Indefinite Trigger Terms");
  await assertTextContains(page.locator("#clauseDetail"), "Checker Logic Visibility");
  await assertTextContains(page.locator("#clauseDetail"), "REFERENCE RESOLVER");
  await assertTextContains(page.locator("#clauseDetail"), "CONCEPT CLASSIFIER");
  await assertTextContains(page.locator("#clauseDetail"), "term_or_survival");
  await assertTextContains(page.locator("#clauseDetail"), "term_survival_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "Claims survive for three years");
  await assertTextContains(page.locator("#clauseDetail"), "unresolved_reference_count");
  await page.getByPlaceholder("Add carve-out term").fill("regulatory obligation");
  await page.locator("#addSurvivalCarveOut").click();
  await assertTextContains(page.locator("#clauseDetail"), "regulatory obligation");
  await assertTextContains(page.locator("#playbookDraftDiff"), "longer_survival_carve_out_terms");
  await page.locator('[data-clause-id="governing_law"]').click();
  await assertTextContains(page.locator("#clauseDetail"), "governing_law_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "heading_only_paragraph_ids");
  await page.locator('[data-clause-id="non_circumvention"]').click();
  await assertTextContains(page.locator("#clauseDetail"), "non_circumvention_analysis");
  await assertTextContains(page.locator("#clauseDetail"), "negated_reference_paragraph_ids");
  await assertTextContains(page.locator("#clauseDetail"), "may not include non-solicitation obligations");
  await page.locator('[data-clause-id="mutuality"]').click();

  await page.locator('textarea[name="check_trigger"]').fill("One-way obligations need Check review.");
  await assertTextContains(page.locator("#playbookDraftDiff"), "check_trigger");
  assert.equal(await page.getByRole("button", { name: "Commit & Save Playbook" }).isEnabled(), true);

  let savedPayload;
  await page.route("**/api/playbook", async (route) => {
    savedPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ playbook: savedPayload.playbook, saved_at: "2026-05-31T20:00:00+00:00" }),
    });
  });
  await page.getByRole("button", { name: "Commit & Save Playbook" }).click();
  await page.waitForFunction(() => document.querySelector("#playbookDraftDiff")?.textContent.includes("No unsaved changes."));
  assert.equal(savedPayload.playbook.clauses[0].check_trigger, "One-way obligations need Check review.");
  const savedConfidentialInfo = savedPayload.playbook.clauses.find((clause) => clause.id === "confidential_information");
  assert.equal(savedConfidentialInfo.standard_exclusions_template, "Publicly known information is excluded.");
  const savedTerm = savedPayload.playbook.clauses.find((clause) => clause.id === "term_and_survival");
  assert.ok(savedTerm.longer_survival_carve_out_terms.includes("regulatory obligation"));
  await page.getByRole("button", { name: "Email Gmail accounts and sync state" }).click();
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Gmail");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "INBOUND ACCOUNT");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "inbound@example.com");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "OUTBOUND ACCOUNT");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "outbound@example.com");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "CONNECTION SETUP");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Inbound connection");
  await assertTextContains(page.locator("#adminGmailSetupPanel"), "inbound@example.com");
  await assertTextContains(page.locator("#adminGmailSetupPanel"), "outbound@example.com");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Local data: data/gmail/inbound-token.json");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Environment: NDA_GMAIL_OUTBOUND_TOKEN_PATH");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Ready for scheduled sync.");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Ready to send redlines.");
  assert.equal(await page.locator("#adminGmailInboundToggle").getAttribute("aria-checked"), "true");
  assert.equal(await page.locator("#adminGmailOutboundToggle").getAttribute("aria-checked"), "true");
  assert.equal(await page.locator('[data-gmail-frequency="manual"]').count(), 0);
  assert.equal(await page.locator('[data-gmail-frequency="10_minutes"]').getAttribute("aria-pressed"), "true");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "SYNC FREQUENCY");
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Every 10 minutes.");
  await page.locator('[data-gmail-frequency="30_minutes"]').click();
  await page.waitForFunction(() => document.querySelector('[data-gmail-frequency="30_minutes"]')?.getAttribute("aria-pressed") === "true");
  assert.deepEqual(gmailSettingsPayloads[gmailSettingsPayloads.length - 1], { sync_frequency: "30_minutes" });
  await assertTextContains(page.locator("#adminIntegrationsPanel"), "Every 30 minutes.");
  await page.locator("#adminGmailInboundToggle").click();
  await page.waitForFunction(() => document.querySelector("#adminGmailInboundToggle")?.getAttribute("aria-checked") === "false");
  assert.deepEqual(gmailSettingsPayloads[gmailSettingsPayloads.length - 1], { inbound_enabled: false });
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
  await page.unroute("**/api/playbook");
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
  let aiProvider = "gemini";
  let aiModel = "gemini-3.5-flash";
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
  });
  await page.route("**/api/ai/settings", async (route) => {
    if (route.request().method() === "POST") {
      const payload = route.request().postDataJSON();
      aiSettingsPayloads.push(payload);
      aiEnabled = payload.enabled === true;
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
      if (String(payload.api_key || "").startsWith("sk-or-")) {
        aiProvider = "openrouter";
        aiModel = "openai/gpt-4o-mini";
      }
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
  await assertTextContains(reviewPanel, "clause:1");
  await assertTextContains(reviewPanel, "clause:1a");
  await assertTextContains(reviewPanel, "section:10b");
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
  await assertTextContains(page.locator("#studioDetailPanel"), "REQUIREMENT");

  await page.getByRole("tab", { name: "Admin" }).click();
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

  await page.locator('[data-admin-section="ai"]').click();
  const aiPanel = page.locator("#adminAiPanel");
  await assertTextContains(aiPanel, "AI review layer");
  await assertTextContains(aiPanel, "How AI checks the deterministic result");
  await assertTextContains(aiPanel, "GEMINI_API_KEY");
  await assertTextContains(aiPanel, "OPENROUTER_API_KEY");
  await assertTextContains(aiPanel, "ai_review_analysis");
  await assertTextContains(aiPanel, "AI disagreement");
  await assertTextContains(aiPanel, "AI Semantic Review");
  await page.waitForFunction(() => document.querySelector("#adminAiEnabledToggle")?.getAttribute("aria-checked") === "false");
  assert.equal(await page.locator('[data-admin-ai="enabled-copy"]').innerText(), "Off");
  assert.equal(await page.locator('[data-admin-ai="api-key"]').innerText(), "Missing AI API key");
  await page.locator("#adminAiApiKeyInput").fill("sk-or-v1-browser-local-key");
  await page.locator("#adminAiSaveKeyButton").click();
  await page.waitForFunction(() => document.querySelector("#adminAiEnabledToggle")?.getAttribute("aria-checked") === "true");
  assert.deepEqual(aiKeyPayloads[aiKeyPayloads.length - 1], { api_key: "sk-or-v1-browser-local-key", enabled: true });
  assert.equal(await page.locator("#adminAiApiKeyInput").inputValue(), "");
  assert.equal(await page.locator('[data-admin-ai="enabled-copy"]').innerText(), "On");
  assert.equal(await page.locator('[data-admin-ai="provider"]').innerText(), "openrouter");
  assert.equal(await page.locator('[data-admin-ai="model"]').innerText(), "openai/gpt-4o-mini");
  assert.equal(await page.locator('[data-admin-ai="api-key"]').innerText(), "Configured from saved local OpenRouter key");
  assert.equal(await page.locator('[data-admin-ai="source"]').innerText(), "Admin toggle");
  assert.equal(await page.locator("#adminAiOverall").innerText(), "ON");
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

async function testStructuredEvidenceAndRationale(page) {
  await runReview(page, "This Agreement shall be governed by the laws of California.");
  await page.getByRole("button", { name: /Governing Law/ }).click();

  await assertTextContains(page.locator("#studioDetailPanel"), "EVIDENCE");
  await assertTextContains(page.locator("#studioDetailPanel"), "PARAGRAPH 1");
  await assertTextContains(page.locator("#studioDetailPanel"), "This Agreement shall be governed by the laws of California.");
  await assertTextContains(page.locator("#studioDetailPanel"), "EVIDENCE SIGNALS");
  await assertTextContains(page.locator("#studioDetailPanel"), "CHECK_EVIDENCE");
  await assertTextContains(page.locator("#studioDetailPanel"), "REASON CODES");
  await assertTextContains(page.locator("#studioDetailPanel"), "unapproved_governing_law");
  await assertTextContains(page.locator("#studioDetailPanel"), "laws of");
  await assertTextContains(page.locator("#studioDetailPanel"), "AUDIT TRACE");
  await assertTextContains(page.locator("#studioDetailPanel"), "Evidence collection");
  await assertTextContains(page.locator("#studioDetailPanel"), "Signal classification");
  await assertTextContains(page.locator("#studioDetailPanel"), "Analysis outputs");
  await assertTextContains(page.locator("#studioDetailPanel"), "Decision");
  await assertTextContains(page.locator("#studioDetailPanel"), "WHY");
  await assertTextContains(page.locator("#studioDetailPanel"), "A governing law clause was found, but it does not use an approved law.");
  await assertTextContains(page.locator("#studioDetailPanel"), "PLAYBOOK RATIONALE");
  await assertTextContains(page.locator("#studioDetailPanel"), "approved operating set");
  await assertTextContains(page.locator("#studioDetailPanel"), "EVIDENCE GUIDANCE");

  await page.evaluate(() => {
    state.latestReviewResult.ai_review = {
      model: "qwen3.7-plus",
      provider: "alibaba",
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
      disagreement: true,
      deterministic_decision: "fail",
      issues: ["unapproved_governing_law"],
      reason: "AI semantic review confirmed the deterministic decision.",
      status: "confirmed",
      suggested_fix: "Use Delaware, India, England and Wales, or DIFC.",
      validation_errors: [],
    };
    renderStudioResult({ clauses: state.reviewClauses });
  });
  await assertTextContains(page.locator("#studioDetailPanel"), "AI EVIDENCE");
  await assertTextContains(page.locator("#studioDetailPanel"), "AI CONFIRMED");
  await assertTextContains(page.locator("#studioDetailPanel"), "FAIL vs FAIL");
  await assertTextContains(page.locator("#studioDetailPanel"), "95%");
  await assertTextContains(page.locator("#studioDetailPanel"), "alibaba / qwen3.7-plus");
  await assertTextContains(page.locator("#studioDetailPanel"), "California is outside the approved governing-law set.");
  await assertTextContains(page.locator("#studioDetailPanel"), "Paragraph 1");
  await assertTextContains(page.locator("#studioDetailPanel"), "UNAPPROVED GOVERNING LAW.");
  await assertTextContains(page.locator("#studioDetailPanel"), "unapproved_governing_law");
  await assertTextContains(page.locator("#studioDetailPanel"), "Use Delaware, India, England and Wales, or DIFC.");
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
  await page.getByRole("button", { name: "Email Gmail accounts and sync state" }).click();
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
  assert.equal(await page.locator('[data-repository-count="in_review"]').innerText(), "2");
  await assertTextContains(page.locator(".repository-card").first(), deleteStem);
  const deleteCard = page.locator(".repository-card").filter({ hasText: deleteStem });
  await deleteCard.getByRole("button", { name: "Delete matter" }).click();
  await assertTextContains(deleteCard, "Delete matter and stored document?");
  assert.equal(await page.locator(".repository-card").filter({ hasText: deleteStem }).count(), 1);
  assert.equal(await page.locator('[data-repository-count="in_review"]').innerText(), "2");
  await deleteCard.getByRole("button", { name: "Cancel delete matter" }).click();
  assert.equal(await deleteCard.getByRole("group", { name: "Delete matter confirmation" }).count(), 0);
  await deleteCard.getByRole("button", { name: "Delete matter" }).click();
  await deleteCard.getByRole("button", { name: "Confirm delete matter" }).click();
  await waitForRepositoryCount(page, "in_review", "1");
  assert.equal(await page.locator(".repository-card").filter({ hasText: deleteStem }).count(), 0);
  assert.equal(await page.locator("#repositoryMatterPanel:not([hidden])").count(), 0);
  assert.equal(await page.locator('[data-repository-count="redline_ready"]').innerText(), "0");
  await assertTextContains(page.locator(".repository-card"), "Manual upload");
  await assertTextContains(page.locator(".repository-card"), "Manual Upload");
  await assertTextContains(page.locator(".repository-card"), "Manual upload of repository-matter");

  await page.locator(".repository-card").click();
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "MANUAL UPLOAD");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Manual upload");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "repository-matter-");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "KEY FAILED CLAUSES");
  assert.equal(await page.getByRole("button", { name: "No Reply" }).isEnabled(), false);

  const [matterExportRequest, matterDownload] = await Promise.all([
    page.waitForRequest((request) => request.url().endsWith("/api/export-review-docx")),
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export Redline" }).click(),
  ]);
  const matterExportPayload = matterExportRequest.postDataJSON();
  assert.ok(matterExportPayload.matter_id, "Repository panel export should send a matter id");
  assert.match(matterDownload.suggestedFilename(), /^repository-matter-\d+-redlined(?:-[0-9a-f]{12})?\.docx$/);
  await waitForRepositoryCount(page, "in_review", "0");
  await waitForRepositoryCount(page, "redline_ready", "1");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Redline Ready");

  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "true");
  await assertTextContains(page.locator("#studioDocTitle"), "repository-matter-");
  await assertTextContains(page.locator("#studioFileMeta"), "Manual Upload matter loaded");
  await waitForRepositoryCount(page, "in_review", "1");
  await waitForRepositoryCount(page, "redline_ready", "0");
  await page.getByRole("tab", { name: "Repository" }).click();
  await assertTextContains(page.locator("#repositoryMatterPanel"), "In Review");
  assert.equal(await page.locator(".repository-card.active").count(), 1);
  await page.getByRole("tab", { name: "Review" }).click();

  const [reviewMatterExportRequest, reviewMatterDownload] = await Promise.all([
    page.waitForRequest((request) => request.url().endsWith("/api/export-review-docx")),
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export DOCX" }).click(),
  ]);
  const reviewMatterExportPayload = reviewMatterExportRequest.postDataJSON();
  assert.ok(reviewMatterExportPayload.matter_id, "Loaded repository matter export should send a matter id");
  assert.match(reviewMatterDownload.suggestedFilename(), /^repository-matter-\d+-redlined(?:-[0-9a-f]{12})?\.docx$/);
  await waitForRepositoryCount(page, "in_review", "0");
  await waitForRepositoryCount(page, "redline_ready", "1");
  assert.equal(await page.getByRole("button", { name: "Review NDA" }).count(), 0);

  await page.getByRole("tab", { name: "Repository" }).click();
  await page.getByRole("button", { name: "Close Matter", exact: true }).click();
  await waitForRepositoryCount(page, "redline_ready", "0");
  await waitForRepositoryCount(page, "signed_closed", "1");
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
  await waitForRepositoryCount(page, "signed_closed", "0");

  fs.rmSync(docxPath, { force: true });
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
      board_column: "redline_ready",
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
  await waitForRepositoryCount(page, "in_review", "1");
  await waitForRepositoryCount(page, "redline_ready", "1");
  assert.equal(await page.locator(".repository-card").count(), 2);

  failMattersLoad = true;
  await page.evaluate(() => repositoryController.loadMatters());
  await waitForRepositoryCount(page, "in_review", "0");
  await waitForRepositoryCount(page, "redline_ready", "0");
  assert.equal(await page.locator(".repository-card").count(), 0);
  for (const column of ["gmail_demo", "in_review", "redline_ready", "signed_closed"]) {
    await assertTextContains(page.locator(`[data-repository-list="${column}"]`), "Matter store is not valid JSON.");
  }

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
}

async function testManualUploadTab(page) {
  const docxPath = path.join(os.tmpdir(), `manual-upload-${Date.now()}.docx`);
  const filename = path.basename(docxPath);
  const stem = path.basename(docxPath, ".docx");
  makeDocxFixture(docxPath, [
    "This Agreement shall be governed by the laws of California.",
    "The Recipient must not circumvent the Company.",
  ]);

  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "Upload" }).click();
  assert.equal(await page.locator("#uploadTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#uploadView").isHidden(), false);
  assert.equal(await page.locator("#manualUploadSubmitButton").isEnabled(), false);

  await page.locator("#manualUploadFileInput").setInputFiles(docxPath);
  await assertTextContains(page.locator("#manualUploadSelectedFile"), filename);
  assert.equal(await page.locator("#manualUploadSubjectInput").inputValue(), stem);
  await page.locator("#manualUploadSenderInput").fill("counterparty@example.com");
  await page.locator("#manualUploadNoteInput").fill("Uploaded outside Gmail.");
  assert.equal(await page.locator("#manualUploadSubmitButton").isEnabled(), true);

  await page.getByRole("button", { name: "Upload NDA" }).click();
  await page.waitForSelector("#repositoryView:not([hidden])");
  assert.equal(await page.locator("#repositoryTab").getAttribute("aria-selected"), "true");
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await assertTextContains(page.locator("#repositoryMatterPanel"), filename);
  await assertTextContains(page.locator("#repositoryMatterPanel"), "MANUAL UPLOAD");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "In Review");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "counterparty@example.com");
  await assertTextContains(page.locator("#repositoryMatterPanel"), "Uploaded outside Gmail.");
  await assertTextContains(page.locator('[data-repository-list="in_review"]'), stem);
  await assertTextContains(page.locator('[data-repository-list="in_review"] .repository-card').filter({ hasText: stem }), "Manual Upload");

  await page.getByRole("button", { name: "Open Review" }).click();
  await page.waitForSelector("#reviewView:not([hidden])");
  await assertTextContains(page.locator("#studioCounterpartyMeta"), "counterparty@example.com");
  await page.getByRole("tab", { name: "Repository" }).click();
  await page.getByRole("button", { name: "Close matter inspector" }).click();
  const uploadedCard = page.locator('[data-repository-list="in_review"] .repository-card').filter({ hasText: stem });
  await uploadedCard.getByRole("button", { name: "Delete matter" }).click();
  await assertTextContains(uploadedCard, "Delete matter and stored document?");
  await uploadedCard.getByRole("button", { name: "Confirm delete matter" }).click();
  await page.waitForFunction(
    (uploadedStem) => !document.querySelector('[data-repository-list="in_review"]')?.innerText.includes(uploadedStem),
    stem,
  );

  fs.rmSync(docxPath, { force: true });
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
      board_column: "redline_ready",
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
  const panel = page.locator("#repositoryMatterPanel");
  await page.waitForSelector("#repositoryMatterPanel:not([hidden])");
  await panel.getByRole("button", { name: "Send Redline" }).click();
  await page.waitForSelector("#repositorySendSubject");
  await assertTextContains(panel, "daniyal.ahmad@aspora.com");
  await assertTextContains(panel, "legal@example.com");
  assert.equal(await page.locator("#repositorySendSubject").inputValue(), "Re: Please review NDA");
  assert.equal(
    await page.locator("#repositorySendBody").inputValue(),
    "Hi,\n\nPlease find attached the redlined version of Please review NDA.\n\nBest,\nAspora Legal",
  );

  await page.locator("#repositorySendSubject").fill("Re: Please review NDA - Aspora redline");
  await page.locator("#repositorySendBody").fill("Please see attached redline.");
  const sendRequest = page.waitForRequest((request) => request.url().endsWith("/api/gmail/send-redline"));
  await panel.getByRole("button", { name: "Confirm Send" }).click();
  await sendRequest;
  await waitForText(page, "#repositoryMatterPanel", "Sent redline to legal@example.com.");
  await waitForRepositoryCount(page, "redline_ready", "1");

  assert.deepEqual(capturedSendPayload, {
    matter_id: "matter_send",
    confirm_send: true,
    subject: "Re: Please review NDA - Aspora redline",
    body: "Please see attached redline.",
  });
  await assertTextContains(panel, "LAST SENT FROM");
  await assertTextContains(panel, "daniyal.ahmad@aspora.com");
  await assertTextContains(panel, "LAST SENT TO");
  await assertTextContains(panel, "legal@example.com");

  await page.unroute("**/api/gmail/status");
  await page.unroute("**/api/matters");
  await page.unroute("**/api/matters/matter_send");
  await page.unroute("**/api/gmail/send-redline");
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
    if (requestUrl.pathname.endsWith("/review")) {
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
    matter = {
      ...matter,
      board_column: "redline_ready",
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
  await page.waitForSelector("#studioSendButton:not(:disabled)");

  await page.locator("#studioSendButton").click();
  await page.waitForSelector("#studioSendModal:not([hidden])");
  assert.equal(await page.locator("#studioSendTo").innerText(), "legal@example.com");
  assert.equal(await page.locator("#studioSendFrom").innerText(), "daniyal.ahmad@aspora.com");
  assert.equal(await page.locator("#studioSendAttachment").innerText(), "Counterparty-NDA-redlined.docx");
  assert.equal(
    await page.locator("#studioSendSubject").inputValue(),
    "Redline for Counterparty NDA - clauses: Confidential Information; 2 text changes; 1 comment",
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
  const sendRequest = page.waitForRequest((request) => request.url().endsWith("/api/gmail/send-redline"));
  await page.locator("#studioSendConfirmButton").click();
  await sendRequest;
  await page.waitForSelector("#studioSendModal[hidden]", { state: "attached" });
  await waitForText(page, "#studioFileMeta", "Sent redline to legal@example.com");

  assert.equal(capturedSendPayload.matter_id, "matter_review_send");
  assert.equal(capturedSendPayload.confirm_send, true);
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
          inbound: {
            configured: false,
            enabled: true,
            error: "Set NDA_GMAIL_INBOUND_TOKEN_PATH for the inbound Gmail account.",
            query: "in:inbox has:attachment",
            ready: false,
            token: {
              configured: false,
              label: "NDA_GMAIL_INBOUND_TOKEN_PATH or data/gmail/inbound-token.json",
              source: "missing",
            },
          },
          outbound: {
            configured: false,
            enabled: true,
            error: "Set NDA_GMAIL_OUTBOUND_TOKEN_PATH for the outbound Gmail account.",
            ready: false,
            token: {
              configured: false,
              label: "NDA_GMAIL_OUTBOUND_TOKEN_PATH or data/gmail/outbound-token.json",
              source: "missing",
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
  await page.getByRole("tab", { name: "Repository" }).click();
  const syncStatus = page.locator("[data-repository-sync-status]");
  await assertTextContains(syncStatus, "Gmail inbound setup required");
  assert.equal((await syncStatus.innerText()).includes("Last sync error"), false);

  await page.getByRole("tab", { name: "Admin" }).click();
  await page.getByRole("button", { name: "Email Gmail accounts and sync state" }).click();
  await waitForText(page, "#adminGmailOverall", "NEEDS SETUP");
  const adminPanel = page.locator("#adminIntegrationsPanel");
  await assertTextContains(adminPanel, "NEEDS SETUP");
  await assertTextContains(adminPanel, "Gmail inbound setup required");
  await assertTextContains(adminPanel, "Missing: NDA_GMAIL_INBOUND_TOKEN_PATH or data/gmail/inbound-token.json");
  await assertTextContains(adminPanel, "Add data/gmail/inbound-token.json or set NDA_GMAIL_INBOUND_TOKEN_PATH.");

  await page.unroute("**/api/gmail/status");
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

  await page.getByRole("button", { name: /Governing Law/ }).click();
  await page.locator('[data-export-clause-id="governing_law"][data-export-decision="ignore"]').first().click();
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
  const ignoredState = await page.locator('[data-export-clause-id="governing_law"][data-export-decision="ignore"]').first().evaluate((node) => ({
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
  assert.equal(prohibitedParagraphStyles.backgroundColor, "rgba(0, 0, 0, 0)");

  const viewerSpacing = await page.evaluate(() => {
    const pageNode = document.querySelector("#reviewView .studio-page");
    const paragraphNode = document.querySelector('#reviewView [data-paragraph-id="p2"]');
    const commentToolsNode = paragraphNode.querySelector(".paragraph-comment-tools");
    const contentNode = [...paragraphNode.querySelectorAll(
      ".paragraph-redline-preview, .paragraph-editable, .paragraph-redline-note, .paragraph-insertion"
    )].find((node) => {
      const box = node.getBoundingClientRect();
      return box.width > 0 && box.height > 0;
    });
    const pageBox = pageNode.getBoundingClientRect();
    const paragraphBox = paragraphNode.getBoundingClientRect();
    const commentToolsBox = commentToolsNode.getBoundingClientRect();
    const contentBox = contentNode.getBoundingClientRect();
    return {
      borderToPageLeft: Math.round(paragraphBox.left - pageBox.left),
      commentRightToPageLeft: Math.round(commentToolsBox.right - pageBox.left),
      textToPageLeft: Math.round(contentBox.left - pageBox.left),
      textWidth: Math.round(contentBox.width),
      pageWidth: Math.round(pageBox.width),
    };
  });
  assert.ok(viewerSpacing.borderToPageLeft <= 2, `paragraph marker should attach to page edge: ${JSON.stringify(viewerSpacing)}`);
  assert.ok(viewerSpacing.commentRightToPageLeft <= -8, `comment controls should sit in the gray gutter: ${JSON.stringify(viewerSpacing)}`);
  assert.ok(viewerSpacing.textToPageLeft <= 40, `paragraph text should start closer to page edge: ${JSON.stringify(viewerSpacing)}`);
  assert.ok(viewerSpacing.textWidth >= viewerSpacing.pageWidth - 95, `paragraph text should use the page width: ${JSON.stringify(viewerSpacing)}`);

  await page.locator('[data-studio-lane-id="term_and_survival"]').click();

  const termParagraph = page.locator('[data-paragraph-id="p1"]');
  const termParagraphStyles = await termParagraph.evaluate((node) => {
    const styles = getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      borderLeftColor: styles.borderLeftColor,
    };
  });
  assert.equal(termParagraphStyles.backgroundColor, "rgba(96, 40, 200, 0.08)");
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
  assert.equal(await page.locator('[data-paragraph-id="p2"]').count(), 1);
  const cleanDeleteAnchor = page.locator('[data-paragraph-id="p2"]');
  assert.equal(await cleanDeleteAnchor.evaluate((node) => node.classList.contains("doc-clean-removed-anchor")), true);
  assert.equal((await cleanDeleteAnchor.innerText()).trim(), "");
  await page.locator('[data-studio-lane-id="non_circumvention"]').click();
  await page.waitForSelector('[data-paragraph-id="p2"].paragraph-pulse');

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

  const insertedBlocks = await page.locator('[data-redline-edit-id]').evaluateAll((nodes) => (
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
  const nonCircumventionCard = page.locator('[data-studio-lane-id="non_circumvention"]');

  await nonCircumventionCard.click();
  await page.waitForSelector('[data-paragraph-id="p1"].paragraph-pulse');
  assert.equal(await page.locator('[data-paragraph-id="p2"]').evaluate((node) => node.classList.contains("paragraph-pulse")), false);

  await nonCircumventionCard.click();
  await page.waitForSelector('[data-paragraph-id="p2"].paragraph-pulse');
  assert.equal(await page.locator('[data-paragraph-id="p1"]').evaluate((node) => node.classList.contains("paragraph-pulse")), false);
}

async function testClauseDecisionControls(page) {
  await runReview(page, "This Agreement shall be governed by the laws of California.");
  const signaturesCard = page.locator(".studio-clause-item").filter({
    has: page.locator('[data-studio-lane-id="signatures"]'),
  });

  await page.locator('[data-studio-lane-id="governing_law"]').click();
  await page.getByRole("button", { name: "DIFC This Agreement shall be governed by the laws of the DIFC." }).click();
  await assertTextContains(page.locator(".redline-option.selected"), "DIFC");
  await page.locator("#studioUndoEditButton").click();
  assert.equal(await page.locator(".redline-option.selected").filter({ hasText: "DIFC" }).count(), 0);
  await page.getByRole("button", { name: "DIFC This Agreement shall be governed by the laws of the DIFC." }).click();
  await assertTextContains(page.locator(".redline-option.selected"), "DIFC");

  await page.locator('[data-export-clause-id="signatures"][data-export-decision="ignore"]').click();
  await assertTextContains(signaturesCard.locator(".studio-export-state"), "IGNORED IN EXPORT");
  assert.equal(await page.locator('[data-redline-edit-id]').filter({ hasText: "For [Party 1 legal name]" }).count(), 0);
  await page.locator('[data-studio-lane-id="signatures"]').click();
  assert.equal(await page.locator('[data-redline-edit-id]').filter({ hasText: "For [Party 1 legal name]" }).count(), 0);
  assert.equal(await page.locator('[data-redline-edit-id].paragraph-pulse').count(), 0);

  await signaturesCard.locator('[data-export-clause-id="signatures"][data-export-decision="include"]').click();
  assert.equal(await signaturesCard.locator(".studio-export-state").count(), 0);
  await page.waitForSelector('[data-redline-edit-id].paragraph-pulse');
  await assertTextContains(page.locator('[data-redline-edit-id]').filter({ hasText: "For [Party 1 legal name]" }), "For [Party 1 legal name]");

  await page.locator("#studioUndoEditButton").click();
  await assertTextContains(signaturesCard.locator(".studio-export-state"), "IGNORED IN EXPORT");
  assert.equal(await page.locator('[data-redline-edit-id]').filter({ hasText: "For [Party 1 legal name]" }).count(), 0);
  await assertTextContains(page.locator("#studioFileMeta"), "Undid clause suggestion change");

  await signaturesCard.locator('[data-export-clause-id="signatures"][data-export-decision="include"]').click();
  assert.equal(await signaturesCard.locator(".studio-export-state").count(), 0);
  await page.waitForSelector('[data-redline-edit-id].paragraph-pulse');
  await assertTextContains(page.locator('[data-redline-edit-id]').filter({ hasText: "For [Party 1 legal name]" }), "For [Party 1 legal name]");

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
    true,
    "re-included signature redline should be exported",
  );
  assert.equal(
    exportedChanges.insertions.some((text) => text.includes("England and Wales")),
    false,
    "default governing-law template should not leak after choosing DIFC",
  );
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
  await page.waitForFunction(() => {
    const governingLaw = document.querySelector('[data-studio-lane-id="governing_law"]')?.closest(".studio-clause-item");
    return governingLaw?.classList.contains("check");
  });
  await assertTextContains(page.locator("#studioOverallTitle"), "Does not meet requirements");
  await assertTextContains(page.locator("#studioResultMeta"), "1 hard clause has failed.");
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
    page.locator("#studioExportButton").click(),
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
      return matter ? { ...matter, board_column: "redline_ready" } : null;
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
    exportButton.click(),
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
