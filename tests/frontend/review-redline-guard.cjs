// Server-free Playwright check for the SILENT-OUTBOUND-CORRUPTION guard in
// syncViewerParagraphEdit (audit D1/D2).
//
// BUG: the faithful edit-lock lets a paragraph be "editable" using a case- +
// whitespace-INSENSITIVE comparison (faithfulNormalizeText). But the sync reads
// editable.innerText VERBATIM, which is the RENDERED text. docx-preview shows a
// <w:caps/> title in UPPERCASE (stored lower-case) and renders a tab as an
// em-space (U+2003). So merely touching such a paragraph -- without the operator
// typing anything -- used to overwrite paragraph.text with the DISPLAY string and
// emit a REPLACE_PARAGRAPH redline the operator never authored, rewriting
// case/whitespace in the SENT contract.
//
// FIX: on sync, if editable.innerText is EQUAL to the stored paragraph.text under
// the SAME normalization the edit-lock uses (faithfulNormalizeText = collapse
// whitespace + lower-case), treat the paragraph as UNCHANGED: do NOT overwrite
// paragraph.text and emit NO redline. A GENUINE edit (normalized text differs)
// still syncs correctly.
//
// This test drives the REAL syncViewerParagraphEdit + editableParagraphText +
// faithfulNormalizeText (pulled out of the shipping JS), with a real DOM editable
// whose innerText carries the browser's actual text-transform / em-space
// rendering. No Python backend, so it cannot hang on server startup.
//
// Run: node tests/frontend/review-redline-guard.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const VIEWER_JS = read("static/js/review-workstation-viewer.js");
const RENDERING_JS = read("static/js/review-workstation-rendering.js");

// Extract a single top-level `function NAME(...) { ... }` by walking braces, so
// the test exercises the REAL shipping function body, not a copy.
function extractFn(src, signature) {
  const start = src.indexOf(signature);
  if (start === -1) throw new Error("could not locate: " + signature);
  let depth = 0;
  let i = src.indexOf("{", start);
  for (; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return src.slice(start, i + 1);
}

const FAITHFUL_NORMALIZE_FN = extractFn(RENDERING_JS, "function faithfulNormalizeText(value) {");
const EDITABLE_TEXT_FN = extractFn(VIEWER_JS, "function editableParagraphText(editable) {");
const CURRENT_PARA_TEXT_FN = extractFn(VIEWER_JS, "function currentParagraphText(paragraphId) {");
const SYNC_FN = extractFn(VIEWER_JS, "function syncViewerParagraphEdit(editable) {");
const RECORD_HISTORY_FN = extractFn(VIEWER_JS, "function recordViewerEditHistoryEntry(editable) {");

const PAGE_HTML = `<!doctype html><html><body>
  <div id="studioDocumentRender">
    <div id="pCaps"  data-editable-paragraph-id="p_caps"  contenteditable="true"></div>
    <div id="pTab"   data-editable-paragraph-id="p_tab"   contenteditable="true"></div>
    <div id="pReal"  data-editable-paragraph-id="p_real"  contenteditable="true"></div>
  </div>
</body></html>`;

// Side-effect globals the real sync/record functions reach for. Each spy records
// that it fired so the test can assert whether a redline was emitted.
const BOOTSTRAP = `
  window.__spies = { redlinePreview: 0, redlineDirty: 0, sourceEdited: 0, historyPushed: 0 };
  window.state = {
    reviewParagraphs: [
      // <w:caps/> title: STORED lower-case, DISPLAYED uppercase via text-transform.
      { id: "p_caps", index: 0, text: "confidential disclosure agreement" },
      // Tabbed heading: STORED with a real tab, DISPLAYED as an em-space (U+2003).
      { id: "p_tab",  index: 1, text: "Term:\\tThree (3) years" },
      // Ordinary body paragraph the operator will genuinely edit.
      { id: "p_real", index: 2, text: "The term shall be three years." },
    ],
    reviewEditHistory: [],
    reviewClauses: [],
    reviewRedlines: [],
    selectedMatter: null,
  };
  // Emit-a-redline signals -> spies.
  window.updateManualRedlinePreview = function () { window.__spies.redlinePreview += 1; };
  window.markRedlineDraftDirty       = function () { window.__spies.redlineDirty += 1; };
  window.markSourceEdited            = function () { window.__spies.sourceEdited += 1; };
  window.pushReviewEditHistoryEntry  = function () { window.__spies.historyPushed += 1; };
  // Inert side effects.
  window.syncReviewSourceFromParagraphs = function () {};
  window.scheduleViewerReviewRefresh    = function () {};
  window.markReviewMayBeStaleFromEdit   = function () {};
  window.updateExportButtonState        = function () {};
  window.updateReviewUndoButtonState    = function () {};
  window.setFileMeta                    = function () {};

  // Render each paragraph the way docx-preview would: real DOM, real rendering.
  // Caps title: store lower-case textContent, uppercase via CSS text-transform.
  var caps = document.getElementById("pCaps");
  caps.textContent = "confidential disclosure agreement";
  caps.style.textTransform = "uppercase";
  // Tabbed heading: the tab is rendered as an em-space (U+2003) in the DOM text.
  document.getElementById("pTab").textContent = "Term:\\u2003Three (3) years";
  // Ordinary paragraph: displayed verbatim.
  document.getElementById("pReal").textContent = "The term shall be three years.";
`;

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const failures = [];
  const check = async (label, fn) => {
    try { await fn(); console.log("  ok -", label); }
    catch (error) { failures.push(label); console.error("  FAIL -", label, "\n   ", error.message); }
  };

  const paraText = (id) =>
    page.evaluate((pid) => window.state.reviewParagraphs.find((p) => p.id === pid).text, id);
  const spies = () => page.evaluate(() => window.__spies);
  const resetSpies = () =>
    page.evaluate(() => { window.__spies = { redlinePreview: 0, redlineDirty: 0, sourceEdited: 0, historyPushed: 0 }; });

  try {
    await page.setContent(PAGE_HTML);
    await page.addScriptTag({ content: BOOTSTRAP });
    await page.addScriptTag({ content: FAITHFUL_NORMALIZE_FN });
    await page.addScriptTag({ content: EDITABLE_TEXT_FN });
    await page.addScriptTag({ content: CURRENT_PARA_TEXT_FN });
    await page.addScriptTag({ content: SYNC_FN });
    await page.addScriptTag({ content: RECORD_HISTORY_FN });

    // Sanity: the browser really does render the display noise the guard defends
    // against, so this test exercises the corrupting condition, not a strawman.
    await check("fixture actually renders caps/em-space display noise (guard is under real load)", async () => {
      const rendered = await page.evaluate(() => ({
        caps: document.getElementById("pCaps").innerText.trim(),
        tabHasEmSpace: document.getElementById("pTab").innerText.includes(String.fromCharCode(0x2003)),
        tabHasTab: document.getElementById("pTab").innerText.includes("\t"),
      }));
      assert.equal(rendered.caps, "CONFIDENTIAL DISCLOSURE AGREEMENT",
        "caps paragraph must render UPPERCASE (differs verbatim from the lower-case stored text)");
      assert.equal(rendered.tabHasEmSpace, true,
        "tab paragraph must render an em-space U+2003 (differs verbatim from the stored \\t)");
      assert.equal(rendered.tabHasTab, false,
        "the rendered em-space must NOT be a literal tab (proves display != stored)");
    });

    // ---- CASE 1: caps/text-transform paragraph merely touched --------------
    await check("caps paragraph touched -> paragraph.text UNCHANGED, no redline emitted", async () => {
      await resetSpies();
      await page.evaluate(() => syncViewerParagraphEdit(document.getElementById("pCaps")));
      assert.equal(await paraText("p_caps"), "confidential disclosure agreement",
        "stored lower-case text must NOT be overwritten with the UPPERCASE display string");
      const s = await spies();
      assert.equal(s.redlinePreview, 0, "no manual-redline preview may be emitted for a display-only touch");
      assert.equal(s.redlineDirty, 0, "the redline draft must NOT be marked dirty");
      assert.equal(s.sourceEdited, 0, "the source must NOT be marked edited");
    });

    // ---- CASE 2: em-space-over-tab paragraph merely touched ----------------
    await check("em-space-over-tab paragraph touched -> no phantom redline, tab preserved", async () => {
      await resetSpies();
      await page.evaluate(() => syncViewerParagraphEdit(document.getElementById("pTab")));
      assert.equal(await paraText("p_tab"), "Term:\tThree (3) years",
        "stored tab must NOT be overwritten with the em-space display string");
      const s = await spies();
      assert.equal(s.redlinePreview, 0, "no manual-redline preview may be emitted for a tab->em-space touch");
      assert.equal(s.redlineDirty, 0, "the redline draft must NOT be marked dirty");
    });

    // ---- CASE 3: a genuine word change still syncs + emits a redline --------
    await check("real word change -> paragraph.text updated and redline emitted", async () => {
      await resetSpies();
      await page.evaluate(() => {
        document.getElementById("pReal").textContent = "The term shall be five years.";
        syncViewerParagraphEdit(document.getElementById("pReal"));
      });
      assert.equal(await paraText("p_real"), "The term shall be five years.",
        "a genuine operator edit MUST persist into paragraph.text");
      const s = await spies();
      assert.equal(s.redlinePreview, 1, "a genuine edit MUST emit the manual-redline preview");
      assert.equal(s.redlineDirty, 1, "a genuine edit MUST mark the redline draft dirty");
      assert.equal(s.sourceEdited, 1, "a genuine edit MUST mark the source edited");
    });

    // ---- Undo-history mirror of the guard ----------------------------------
    // recordViewerEditHistoryEntry must ALSO skip a display-only touch, else a
    // later Undo would write the DISPLAY text into paragraph.text -- reintroducing
    // exactly the corruption the sync guard prevents.
    await check("recordViewerEditHistoryEntry skips a display-only touch (no undo entry)", async () => {
      await resetSpies();
      await page.evaluate(() => {
        const el = document.getElementById("pCaps");
        // No editStartText -> beforeText falls back to the STORED lower-case text,
        // which DIFFERS verbatim from the uppercase display innerText. That clears
        // the plain beforeText===afterText check, so this exercises the NEW
        // normalize-equal guard specifically: without it, an Undo entry keyed on the
        // uppercase display text would later corrupt paragraph.text.
        delete el.dataset.editStartText;
        el.dataset.editHistoryRecorded = "";
        recordViewerEditHistoryEntry(el);
      });
      const s = await spies();
      assert.equal(s.historyPushed, 0,
        "no undo entry may be recorded for a caps display-only touch (would let Undo corrupt the text)");
    });

    await check("recordViewerEditHistoryEntry DOES record a genuine edit", async () => {
      await resetSpies();
      await page.evaluate(() => {
        const el = document.getElementById("pReal");
        el.dataset.editHistoryRecorded = "";
        el.dataset.editStartText = "The term shall be three years.";
        el.textContent = "The term shall be seven years.";
        recordViewerEditHistoryEntry(el);
      });
      const s = await spies();
      assert.equal(s.historyPushed, 1, "a genuine edit MUST record an undo entry");
    });

  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error("\nreview-redline-guard.cjs FAILED: " + failures.length + " check(s)");
    process.exit(1);
  }
  console.log("\nreview-redline-guard.cjs PASSED");
}

main().catch((error) => { console.error(error); process.exit(1); });
