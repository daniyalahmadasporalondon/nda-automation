// Server-free Playwright check for REVIEW-editor Find & Replace + Undo run survival.
// It loads the REAL review viewer module (static/js/review-workstation-viewer.js)
// plus the find-replace module it leans on for the formatting-preserving re-tile
// (_retileRunsForReplace), stubs the handful of globals the two functions under test
// reference, then:
//   1. runs applyReviewFindReplace on a paragraph carrying a bold run, then
//      undoLastViewerEdit, and asserts BOTH the text AND the bold run are restored
//      and the runs.join("") === text invariant holds (the bug: undo dropped runs);
//   2. runs a normal typed-edit history entry (NO captured runs) through the same
//      undo path and asserts the runs are left untouched -> no typed-edit regression.
// No Python backend, so it cannot hang on server startup.
//
// Run: node tests/frontend/review-find-replace-undo.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
// redline-rendering.js exports charDiffOperations; review-workstation-format.js
// exports mergeAdjacentRuns/normalizeRun. retileRunsForReplace needs all three to
// actually re-tile (otherwise it returns null and leaves runs tiling the OLD text),
// so load them to mirror the real runtime.
const RENDERING_JS = read("static/js/redline-rendering.js");
const FORMAT_JS = read("static/js/review-workstation-format.js");
const FIND_REPLACE_JS = read("static/js/find-replace.js");
const VIEWER_JS = read("static/js/review-workstation-viewer.js");

const PAGE_HTML = `<!doctype html><html><body>
  <div id="studioDocumentRender"></div>
  <div id="studioResultMeta"></div>
  <button id="studioUndoEditButton"></button>
</body></html>`;

// Stubs for the globals applyReviewFindReplace + undoLastViewerEdit reach for.
// state holds the document + the undo stack; the render/dirty/sync hooks are no-ops
// (setSourceText is what syncReviewSourceFromParagraphs calls). One paragraph
// carries a bold run that tiles its text exactly so the retile path is exercised.
const BOOTSTRAP = `
  window.state = {
    reviewEditHistory: [],
    reviewParagraphs: [
      {
        id: "p1",
        // "The " (plain) + "Discloser" (bold) + " agrees." (plain)
        text: "The Discloser agrees.",
        runs: [
          { text: "The " },
          { text: "Discloser", bold: true },
          { text: " agrees." },
        ],
      },
    ],
    reviewSourceText: "",
  };
  window.studioDocumentRender = document.getElementById("studioDocumentRender");
  window.studioResultMeta = document.getElementById("studioResultMeta");
  window.studioUndoEditButton = document.getElementById("studioUndoEditButton");
  window.REVIEW_EDIT_HISTORY_LIMIT = 50;
  // Hooks the two functions call; all side-effect-only -> safe no-ops here.
  window.setSourceText = function () {};
  window.setFileMeta = function () {};
  window.markRedlineDraftDirty = function () {};
  window.markSourceEdited = function () {};
  window.renderStudioDocumentHighlights = function () {};
  window.scheduleViewerReviewRefresh = function () {};
  window.markReviewMayBeStaleFromEdit = function () {};
  window.updateExportButtonState = function () {};
`;

function paragraph(page) {
  return page.evaluate(() => {
    const p = window.state.reviewParagraphs[0];
    return { text: p.text, runs: p.runs || null };
  });
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const failures = [];
  const check = async (label, fn) => {
    try { await fn(); console.log("  ok -", label); }
    catch (error) { failures.push(label); console.error("  FAIL -", label, "\n   ", error.message); }
  };

  try {
    await page.setContent(PAGE_HTML);
    // BOOTSTRAP defines window.state first (format.js reads it). Then the run/diff
    // helpers, find-replace (the retile), and finally the module under test.
    await page.addScriptTag({ content: BOOTSTRAP });
    await page.addScriptTag({ content: RENDERING_JS });
    await page.addScriptTag({ content: FORMAT_JS });
    await page.addScriptTag({ content: FIND_REPLACE_JS });
    await page.addScriptTag({ content: VIEWER_JS });

    // ---- 1. Review F/R on a bold-run paragraph, then Undo -------------------
    await check("review Find & Replace retiles runs over the new text", async () => {
      await page.evaluate(() => {
        const p = window.state.reviewParagraphs[0];
        applyReviewFindReplace(p, "The Disclosing Party agrees.", "The Discloser agrees.");
      });
      const p1 = await paragraph(page);
      assert.equal(p1.text, "The Disclosing Party agrees.");
      assert.ok(Array.isArray(p1.runs) && p1.runs.length, "runs should survive the replace");
      assert.equal(p1.runs.map((r) => r.text).join(""), p1.text, "runs must tile new text");
      // One history entry was pushed and it captured the pre-retile runs.
      const entry = await page.evaluate(() => window.state.reviewEditHistory.at(-1));
      assert.equal(entry.type, "paragraph_text");
      assert.equal(entry.hadRuns, true, "F/R entry must capture hadRuns");
      assert.equal(entry.previousText, "The Discloser agrees.");
    });

    await check("Undo restores BOTH the original text and the bold run", async () => {
      await page.evaluate(() => undoLastViewerEdit());
      const p1 = await paragraph(page);
      // Text restored.
      assert.equal(p1.text, "The Discloser agrees.", "text not restored");
      // Runs restored (the bug: undo left them tiling the post-replace text).
      assert.ok(Array.isArray(p1.runs) && p1.runs.length, "runs not restored");
      // Invariant: runs.join("") === text.
      assert.equal(p1.runs.map((r) => r.text).join(""), p1.text, "runs.join() !== text after undo");
      // The bold run is present and is exactly "Discloser".
      const boldText = p1.runs.filter((r) => r.bold).map((r) => r.text).join("");
      assert.equal(boldText, "Discloser", "bold run lost on undo: " + JSON.stringify(p1.runs));
      // History was popped.
      const len = await page.evaluate(() => window.state.reviewEditHistory.length);
      assert.equal(len, 0, "history not popped");
    });

    await check("captured + restored runs are deep copies (no shared aliasing)", async () => {
      const aliased = await page.evaluate(() => {
        const p = window.state.reviewParagraphs[0];
        // Push an F/R entry capturing the current runs, then mutate the LIVE runs.
        // If the capture deep-copied, the entry's previousRuns must be unaffected.
        applyReviewFindReplace(p, "The Disclosing Party agrees.", "The Discloser agrees.");
        const entry = window.state.reviewEditHistory.at(-1);
        // Mutate the live run that the entry snapshotted from.
        p.runs[0].bold = "MUTATED";
        const entryUntouched = entry.previousRuns.every((r) => r.bold !== "MUTATED");
        // Undo restores from the entry; the restored runs must also be independent
        // of the entry (mutating live runs after undo must not touch the entry copy).
        undoLastViewerEdit();
        return { entryUntouched };
      });
      assert.equal(aliased.entryUntouched, true, "captured previousRuns aliased the live runs");
    });

    // ---- 2. Normal typed-edit Undo: no captured runs -> runs untouched ------
    await check("typed-edit Undo restores text and leaves runs untouched (no regression)", async () => {
      await page.evaluate(() => {
        const p = window.state.reviewParagraphs[0];
        // Simulate a typed edit: history entry carries previousText ONLY (the shape
        // recordViewerEditHistoryEntry pushes), the live text/runs already advanced.
        pushReviewEditHistoryEntry({
          paragraphId: p.id,
          previousText: p.text,
          type: "paragraph_text",
        });
        p.text = "The Discloser strongly agrees.";
        // A typed edit leaves the stale runs in place (they go inert via the
        // join!==text render guard); the undo must NOT clobber them from an absent
        // previousRuns.
      });
      const beforeUndoRuns = await page.evaluate(() => JSON.stringify(window.state.reviewParagraphs[0].runs));
      await page.evaluate(() => undoLastViewerEdit());
      const p1 = await paragraph(page);
      assert.equal(p1.text, "The Discloser agrees.", "typed-edit text not restored");
      // Runs are exactly what they were before the undo (untouched), NOT wiped to [].
      assert.equal(JSON.stringify(p1.runs), beforeUndoRuns, "typed-edit undo clobbered runs");
      assert.ok(Array.isArray(p1.runs) && p1.runs.some((r) => r.bold), "typed-edit undo dropped runs");
    });

  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error("\nreview-find-replace-undo.cjs FAILED: " + failures.length + " check(s)");
    process.exit(1);
  }
  console.log("\nreview-find-replace-undo.cjs PASSED");
}

main().catch((error) => { console.error(error); process.exit(1); });
