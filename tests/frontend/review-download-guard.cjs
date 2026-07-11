"use strict";

// Server-free Playwright proof for the download/gating audit fixes:
//
//   D6  A reviewed-PDF download goes through the ok-checked guard
//       (downloadUrlGuarded), so a 4xx/5xx JSON error body — or a 200 that is
//       actually a JSON/HTML error — is SURFACED as an error and NEVER saved to
//       disk as a broken ".pdf". A real PDF (200 + application/pdf) DOES save.
//
//   D8  matterIsPdf / selectedMatterIsPdfSource actually tests that the selected
//       matter's SOURCE is a PDF (filename sniff), so DOCX matters don't get the
//       PDF markup tools whose marked-up download 400s.
//
//   D4  When a reviewed-PDF export/download fails with a `recovery` pointer, the
//       reviewer gets a "Download marked-up PDF" action that fetches the
//       source-PDF annotation endpoint ({matter_id} substituted) through the same
//       guard — instead of dead-ending on the error.
//
// No Python backend: we load the REAL review-workstation-actions.js against a
// minimal DOM, inject the REAL app.js download helpers (extracted by name), stub
// fetch + the cross-module render helpers, and drive the flows directly.
//
// Run: node tests/frontend/review-download-guard.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const ACTIONS_JS = read("static/js/review-workstation-actions.js");
const APP_JS = read("static/app.js");

// Extract a top-level function declaration BY NAME from a source string via brace
// matching, so the test exercises the REAL app.js helper rather than a re-stub.
// (Every `{`/`}` inside these particular helpers is balanced — object literals,
// `${...}` template holes and regexes — so the naive counter lands correctly.)
function extractFunction(src, name) {
  const start = src.search(new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`));
  if (start === -1) throw new Error(`could not find function ${name} in source`);
  let i = src.indexOf("{", start);
  let depth = 0;
  for (; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) { i += 1; break; }
    }
  }
  return src.slice(start, i);
}

// The REAL app.js download helpers the actions module reaches for as bare globals.
function appDownloadHelpers() {
  const names = [
    "reviewErrorFromPayload",
    "downloadBlob",
    "downloadUrl",
    "downloadFilename",
    "downloadUrlGuarded",
    "selectedMatterIsPdfSource",
  ];
  return names.map((name) => extractFunction(APP_JS, name)).join("\n\n");
}

const PAGE_HTML = `<!doctype html><html><head></head><body>
  <div class="studio-toolbar">
    <span id="studioFileMeta"></span>
    <button id="studioRefreshReviewButton" type="button">Review</button>
  </div>
  <div class="sr-only">
    <h3 id="studioOverallTitle"></h3>
    <span id="studioResultMark"></span>
    <span id="studioResultMeta"></span>
  </div>
  <textarea id="studioNdaText"></textarea>
</body></html>`;

function bootScript() {
  return `
    // --- element globals (app.js normally owns these) ---
    var studioFileMeta = document.querySelector("#studioFileMeta");
    var studioRefreshReviewButton = document.querySelector("#studioRefreshReviewButton");
    var studioOverallTitle = document.querySelector("#studioOverallTitle");
    var studioResultMark = document.querySelector("#studioResultMark");
    var studioResultMeta = document.querySelector("#studioResultMeta");
    var studioNdaText = document.querySelector("#studioNdaText");

    // downloadBlob revoke delay (app.js const).
    var DOWNLOAD_URL_REVOKE_DELAY_MS = 1000;

    // --- shared app state ---
    var state = {
      selectedMatter: null,
      selectedDocument: null,
      reviewClauses: [],
      redlineDraftDirty: false,
    };

    // --- REAL setFileMeta (review-workstation-source.js) ---
    function setFileMeta(message) { studioFileMeta.textContent = message; }

    // reviewIsStale() reaches for this cross-module model; null -> the honest
    // fallback (state.selectedMatter.review_refresh.stale) runs.
    function reviewWorkstationModel() { return null; }
    function updateExportButtonState() {}
    function renderReviewRefreshNotice() {}
    function staleReviewMessage(_refresh, message) { return message || "stale"; }

    // Record every actual browser save so the tests can assert save-or-not.
    window.__saved = { blob: [], url: [] };
  `;
}

async function loadPage(browser) {
  const page = await browser.newPage();
  await page.setContent(PAGE_HTML);
  await page.addScriptTag({ content: bootScript() });
  await page.addScriptTag({ content: appDownloadHelpers() });
  await page.addScriptTag({ content: ACTIONS_JS });
  // Spy the two LOW-LEVEL save primitives (leave the real downloadUrlGuarded
  // guard intact) so we can prove a broken body never reaches disk.
  await page.evaluate(() => {
    const realBlob = downloadBlob;
    downloadBlob = function (blob, filename) {
      window.__saved.blob.push({ filename, type: blob && blob.type });
      // Do NOT actually trigger a browser download in the test.
    };
    downloadUrl = function (url, filename) {
      window.__saved.url.push({ url, filename });
    };
    void realBlob;
  });
  return page;
}

// Install a fetch stub that returns a single scripted response.
async function stubFetch(page, response) {
  await page.evaluate((resp) => {
    window.fetch = async function () {
      const headers = new Headers(resp.headers || {});
      return {
        ok: resp.ok,
        status: resp.status,
        headers,
        json: async () => {
          if (resp.jsonThrows) throw new Error("not json");
          return resp.json;
        },
        blob: async () => new Blob([resp.blobText || "%PDF-1.4 stub"], { type: resp.blobType || "application/pdf" }),
      };
    };
  }, response);
}

async function main() {
  const browser = await chromium.launch();
  const failures = [];

  // --- D6.1: a 4xx JSON error body is surfaced, NOT saved as .pdf -------------
  try {
    const page = await loadPage(browser);
    await stubFetch(page, {
      ok: false,
      status: 503,
      headers: { "Content-Type": "application/json" },
      json: { error: "Reviewed PDF is temporarily unavailable." },
    });
    const result = await page.evaluate(async () => {
      state.selectedMatter = { id: "m1" };
      await downloadReviewPdf({ url: "/api/matters/m1/reviewed.pdf", filename: "reviewed-document.pdf" });
      return {
        saved: window.__saved,
        title: studioOverallTitle.textContent,
        meta: studioResultMeta.textContent,
        fileMeta: studioFileMeta.textContent,
      };
    });
    assert.equal(result.saved.blob.length, 0, "a 4xx error body was SAVED as a blob download");
    assert.equal(result.saved.url.length, 0, "a 4xx error body was SAVED via <a download>");
    assert.match(result.title, /temporarily unavailable/i, "server error message was not surfaced to the reviewer");
    assert.doesNotMatch(result.fileMeta, /Downloaded/i, "a failed download falsely reported success");
    process.stdout.write("  ok D6.1 - 4xx JSON error body is surfaced, never saved as .pdf\n");
    await page.close();
  } catch (error) { failures.push(["D6.1 4xx error not saved", error]); }

  // --- D6.2: a 200 whose body is JSON (not a file) is ALSO not saved ----------
  try {
    const page = await loadPage(browser);
    await stubFetch(page, {
      ok: true,
      status: 200,
      headers: { "Content-Type": "application/json" },
      json: { error: "Export produced no document." },
    });
    const result = await page.evaluate(async () => {
      state.selectedMatter = { id: "m2" };
      await downloadReviewPdf({ url: "/api/matters/m2/reviewed.pdf", filename: "reviewed-document.pdf" });
      return { saved: window.__saved, title: studioOverallTitle.textContent };
    });
    assert.equal(result.saved.blob.length, 0, "a 200 JSON error body was SAVED as a blob download");
    assert.equal(result.saved.url.length, 0, "a 200 JSON error body was SAVED via <a download>");
    assert.match(result.title, /no document|downloadable file/i, "200-but-JSON error was not surfaced");
    process.stdout.write("  ok D6.2 - a 200 JSON body is treated as an error, not saved as a file\n");
    await page.close();
  } catch (error) { failures.push(["D6.2 200 JSON not saved", error]); }

  // --- D6.3: a real PDF (200 + application/pdf) DOES download ------------------
  try {
    const page = await loadPage(browser);
    await stubFetch(page, {
      ok: true,
      status: 200,
      headers: {
        "Content-Type": "application/pdf",
        "Content-Disposition": 'attachment; filename="server-named.pdf"',
      },
    });
    const result = await page.evaluate(async () => {
      state.selectedMatter = { id: "m3" };
      await downloadReviewPdf({ url: "/api/matters/m3/reviewed.pdf", filename: "reviewed-document.pdf" });
      return { saved: window.__saved, fileMeta: studioFileMeta.textContent };
    });
    assert.equal(result.saved.blob.length, 1, "a valid PDF was not downloaded");
    assert.equal(result.saved.blob[0].filename, "server-named.pdf", "server Content-Disposition filename was not used");
    assert.match(result.fileMeta, /Downloaded/i, "a successful download did not report success");
    process.stdout.write("  ok D6.3 - a real application/pdf downloads (server filename honoured)\n");
    await page.close();
  } catch (error) { failures.push(["D6.3 real pdf downloads", error]); }

  // --- D8: markup gate is PDF-only (DOCX / no-source / no-id are excluded) -----
  try {
    const page = await loadPage(browser);
    const gate = await page.evaluate(() => {
      const check = (matter) => { state.selectedMatter = matter; return selectedMatterIsPdfSource(); };
      return {
        pdf: check({ id: "p", source_filename: "mutual-nda.pdf" }),
        pdfAttachment: check({ id: "p2", attachment_filename: "inbound.PDF" }),
        docx: check({ id: "d", source_filename: "mutual-nda.docx" }),
        docxAttachment: check({ id: "d2", attachment_filename: "inbound.docx" }),
        noSource: check({ id: "n" }),
        noId: check({ source_filename: "orphan.pdf" }),
        none: check(null),
      };
    });
    assert.equal(gate.pdf, true, "a .pdf source matter was not recognised as PDF");
    assert.equal(gate.pdfAttachment, true, "a .PDF attachment matter was not recognised as PDF");
    assert.equal(gate.docx, false, "a DOCX matter was wrongly offered PDF markup tools");
    assert.equal(gate.docxAttachment, false, "a DOCX attachment matter was wrongly offered PDF markup tools");
    assert.equal(gate.noSource, false, "a matter with no source filename was wrongly treated as PDF");
    assert.equal(gate.noId, false, "a matter with no id was wrongly treated as PDF");
    assert.equal(gate.none, false, "no selected matter was wrongly treated as PDF");
    process.stdout.write("  ok D8  - markup gate is PDF-source-only (DOCX matters excluded)\n");
    await page.close();
  } catch (error) { failures.push(["D8 pdf gate", error]); }

  // --- D4: reviewed-PDF recovery action fetches the annotated-PDF endpoint -----
  try {
    const page = await loadPage(browser);
    // Spy the guard so we can capture the endpoint the recovery button hits.
    await page.evaluate(() => {
      window.__guarded = [];
      downloadUrlGuarded = async function (url, filename) { window.__guarded.push({ url, filename }); };
    });
    const setup = await page.evaluate(() => {
      state.selectedMatter = { id: "matter 42", source_filename: "counterparty nda.pdf" };
      // The 503 payload redline_export_service returns, run through the REAL
      // reviewErrorFromPayload so error.recovery is populated exactly as in prod.
      const error = reviewErrorFromPayload({
        error: "Could not place all proposed changes in the reconstructed document.",
        recovery: {
          path: "annotated_pdf",
          endpoint: "/api/matters/{matter_id}/annotated-pdf",
          message: "Download the source PDF with the proposed changes marked up as annotations.",
        },
      }, "Export could not run");
      renderExportRecoveryAction(error);
      const button = studioFileMeta.querySelector("button.export-recovery-action");
      return { hasButton: Boolean(button), buttonText: button ? button.textContent : "", meta: studioFileMeta.textContent };
    });
    assert.ok(setup.hasButton, "no recovery action was rendered for a recovery payload");
    assert.match(setup.buttonText, /marked-up pdf/i, "recovery button label is not the marked-up-PDF action");
    assert.match(setup.meta, /marked up as annotations/i, "recovery message was not surfaced");

    const clicked = await page.evaluate(async () => {
      studioFileMeta.querySelector("button.export-recovery-action").click();
      // let the async click handler run
      await new Promise((resolve) => setTimeout(resolve, 0));
      return window.__guarded.slice();
    });
    assert.equal(clicked.length, 1, "clicking the recovery action did not fetch the annotated PDF");
    assert.equal(
      clicked[0].url,
      "/api/matters/matter%2042/annotated-pdf",
      "recovery endpoint did not substitute + URL-encode the matter id",
    );
    assert.match(clicked[0].filename, /marked-up\.pdf$/i, "recovery download used no sensible filename");
    process.stdout.write("  ok D4  - recovery action fetches the annotated-PDF endpoint (matter id substituted)\n");
    await page.close();
  } catch (error) { failures.push(["D4 recovery action", error]); }

  await browser.close();

  if (failures.length) {
    for (const [label, error] of failures) {
      process.stderr.write(`  FAIL ${label}\n${error && error.stack ? error.stack : error}\n`);
    }
    process.stderr.write("review-download-guard.cjs FAIL\n");
    process.exit(1);
  }
  process.stdout.write("review-download-guard.cjs PASS\n");
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
