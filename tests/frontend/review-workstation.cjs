const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const { chromium } = require("playwright");
const { PNG } = require("pngjs");

const ROOT = path.resolve(__dirname, "../..");
const PORT = Number(process.env.FRONTEND_TEST_PORT || 19000 + Math.floor(Math.random() * 1000));
const BASE_URL = `http://127.0.0.1:${PORT}`;
const PYTHON = process.env.PYTHON || "python3";
const VIEWPORT = { width: 1440, height: 1000 };

const passNda = fs.readFileSync(path.join(ROOT, "samples", "pass-nda.txt"), "utf8").trim();
const inlineDiffVectors = JSON.parse(fs.readFileSync(
  path.join(ROOT, "tests", "fixtures", "inline_diff_vectors.json"),
  "utf8",
));
const redlineNda = [
  "The confidentiality obligations survive for seven years.",
  "The Recipient must not circumvent the Company or deal directly with introduced parties.",
].join("\n\n");
const allActionRedlineNda = [
  "The confidentiality obligations survive for seven years.",
  "The Recipient must not circumvent the Company or deal directly with introduced parties.",
  "For Aspora Technology Services Private Limited\nBy: __________________\nTitle: Director\nDate: 2026-05-30",
  "For Counterparty Limited\nBy: __________________\nTitle: Chief Executive Officer\nDate: 2026-05-30",
].join("\n\n");

const tests = [
  ["exposes accessible tab, toggle, and live-region state", testAccessibleControlState],
  ["covers inline diff algorithm edge cases", testInlineDiffAlgorithmEdges],
  ["renders backend redlines across all document modes", testBackendRedlineModes],
  ["renders manual viewer edits as local redlines", testManualViewerEditRedline],
  ["keeps browser preview aligned with exported DOCX redlines", testPreviewMatchesExportedDocx],
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
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
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
  await page.waitForSelector(".studio-issue-card");
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

  await page.getByRole("tab", { name: "Clauses" }).click();
  assert.equal(await page.locator("#reviewTab").getAttribute("aria-selected"), "false");
  assert.equal(await page.locator("#clausesTab").getAttribute("aria-selected"), "true");
  assert.equal(await page.locator("#reviewView").getAttribute("hidden"), "");

  await page.getByRole("tab", { name: "Review" }).click();
  await page.getByRole("button", { name: "Clean" }).click();
  assert.equal(await page.locator('[data-view-mode="redline"]').getAttribute("aria-pressed"), "false");
  assert.equal(await page.locator('[data-view-mode="clean"]').getAttribute("aria-pressed"), "true");
}

async function testInlineDiffAlgorithmEdges(page) {
  await page.goto(`${BASE_URL}/?v=frontend-test`, { waitUntil: "domcontentloaded" });
  const operationsByVector = await page.evaluate((vectors) => {
    const tokenBlockText = (block) => Array.from({ length: block.count }, (_, index) => `${block.prefix}${index}`).join(" ");
    const expectedOperations = (vector) => [
      ...(vector.operations || []),
      ...(vector.operationBlocks || []).flatMap((block) => (
        Array.from({ length: block.count }, (_, index) => ({ type: block.type, token: `${block.prefix}${index}` }))
      )),
    ];
    const diffTextOperations = (original, replacement) => {
      const oldTokens = tokenizeInlineDiff(original);
      const newTokens = tokenizeInlineDiff(replacement);
      if (!oldTokens.length) return newTokens.map((token) => ({ type: "insert", token }));
      if (!newTokens.length) return oldTokens.map((token) => ({ type: "delete", token }));
      if (oldTokens.length * newTokens.length > INLINE_DIFF_MAX_MATRIX_CELLS) {
        return [
          ...oldTokens.map((token) => ({ type: "delete", token })),
          ...newTokens.map((token) => ({ type: "insert", token })),
        ];
      }
      return diffTokenOperations(oldTokens, newTokens);
    };
    return vectors.map((vector) => {
      const original = vector.originalTokenBlock ? tokenBlockText(vector.originalTokenBlock) : vector.original;
      const replacement = vector.replacementTokenBlock ? tokenBlockText(vector.replacementTokenBlock) : vector.replacement;
      return {
        name: vector.name,
        actual: diffTextOperations(original, replacement),
        expected: expectedOperations(vector),
      };
    });
  }, inlineDiffVectors);

  for (const vector of operationsByVector) {
    assert.deepEqual(vector.actual, vector.expected, vector.name);
  }

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
    const oldLong = Array.from({ length: 201 }, (_, index) => `old${index}`).join(" ");
    const newLong = Array.from({ length: 200 }, (_, index) => `new${index}`).join(" ");
    return {
      emptyInsert: revisionState(renderInlineDiff("", "Alpha, beta.")),
      emptyDelete: revisionState(renderInlineDiff("Alpha, beta.", "")),
      punctuation: revisionState(renderInlineDiff(
        "This Agreement (California) applies.",
        "This Agreement (England and Wales) applies.",
      )),
      fallback: revisionState(renderInlineDiff(oldLong, newLong)),
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

  assert.equal(cases.fallback.deleted.length, 201);
  assert.equal(cases.fallback.inserted.length, 200);
  assert.equal(cases.fallback.deleted[0], "old0");
  assert.equal(cases.fallback.inserted[0], "new0");
  assert.match(cases.fallback.original, /^old0 old1/);
  assert.match(cases.fallback.accepted, /^new0 new1/);
}

async function testBackendRedlineModes(page) {
  await runReview(page, redlineNda);
  await page.locator('[data-studio-clause-id="term_and_survival"]').click();

  const termParagraph = page.locator('[data-paragraph-id="p1"]');
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
    delCount: node.querySelectorAll(".clause-sxs-col.latest .inline-del").length,
    insCount: node.querySelectorAll(".clause-sxs-col.latest .inline-ins").length,
  }));
  assert.deepEqual(sideBySide.labels, ["Original", "Redline"]);
  assert.match(sideBySide.original, /seven years/);
  assert.match(sideBySide.redline, /fixed period of up to five years/);
  assert.ok(sideBySide.delCount >= 1, "side-by-side redline should show deletions");
  assert.ok(sideBySide.insCount >= 1, "side-by-side redline should show insertions");
  await assertRedGreenPixels(page.locator('[data-paragraph-id="p1"] .clause-sxs-col.latest'));
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

  assert.equal(await page.locator("#studioExportButton").isDisabled(), true);
  await assertTextContains(page.locator("#studioResultMeta"), "Run Review NDA again");
  await assertTextContains(page.locator("#studioFileMeta"), "Edited in viewer");

  await page.getByRole("button", { name: "Side by Side" }).click();
  const sideBySide = await page.locator('[data-paragraph-id="p1"]').evaluate((node) => ({
    original: node.querySelector(".clause-sxs-col:first-child div")?.innerText || "",
    redline: node.querySelector(".clause-sxs-col.latest div")?.innerText || "",
    delCount: node.querySelectorAll(".clause-sxs-col.latest .inline-del").length,
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
      return {
        original: paragraph?.querySelector(".clause-sxs-col:first-child div")?.innerText || "",
        redline: latest?.innerText || "",
        accepted: textWithoutDeleted(latest),
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
  await assertTextContains(page.locator("#studioFileMeta"), "nda-review-report.docx exported");
  await assertTextContains(page.locator("#studioFileMeta"), "/exports/nda-review-report.docx");
  assert.equal(await page.locator("#studioFileMeta a").getAttribute("href"), "/exports/nda-review-report.docx");

  await page.locator('[data-editable-paragraph-id="p1"]').fill("Mutual Non-Disclosure Agreement with edits");
  await page.waitForSelector('[data-paragraph-id="p1"].manual-redline');
  assert.equal(await exportButton.isDisabled(), true);
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
  assert.ok(redPixels > 10, `expected visible redline deletion pixels, found ${redPixels}`);
  assert.ok(greenPixels > 10, `expected visible redline insertion pixels, found ${greenPixels}`);
}

async function assertTextContains(locator, expected) {
  const text = await locator.innerText();
  assert.ok(text.includes(expected), `expected "${text}" to include "${expected}"`);
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeWhitespace(value) {
  return String(value).replace(/\s+/g, " ").trim();
}

function readDocxTrackChanges(docxPath) {
  const script = `
import json
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
with ZipFile(sys.argv[1]) as archive:
    root = ET.fromstring(archive.read("word/document.xml"))

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
print(json.dumps({"deletions": deletions, "insertions": insertions, "revisionParagraphs": revision_paragraphs}))
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
