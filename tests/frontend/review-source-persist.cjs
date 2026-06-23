// Server-free Playwright checks for the review-workstation source-textarea fixes:
//   FIX 1 (P0): typing in #studioNdaText must be RECONCILED into the document model
//     (state.reviewParagraphs) -- the real source for export/render/send -- instead
//     of living only in the DOM .value where syncReviewSourceFromParagraphs() would
//     silently overwrite it.
//   FIX 1 guard: while the textarea is dirty (state.sourceTextDirty), a model->text
//     sync must NOT clobber the pending keystrokes.
//   FIX 4 (P2): the Review-pasted action bar shows only when the source editor is
//     the active surface and enables only when there is text.
//
// Loads the REAL review-workstation-source.js + the syncReviewSourceFromParagraphs
// helper from review-workstation-viewer.js, stubs the handful of globals they reach
// for, and drives the input/blur handlers + the sync guard directly. No Python
// backend, so it cannot hang on server startup.
//
// Run: node tests/frontend/review-source-persist.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const SOURCE_JS = read("static/js/review-workstation-source.js");

// Pull ONLY syncReviewSourceFromParagraphs out of the viewer module so we exercise
// the real guarded sync without dragging in the whole viewer's global surface.
const VIEWER_JS = read("static/js/review-workstation-viewer.js");
const SYNC_FN = (() => {
  const marker = "function syncReviewSourceFromParagraphs() {";
  const start = VIEWER_JS.indexOf(marker);
  if (start === -1) throw new Error("could not locate syncReviewSourceFromParagraphs in viewer.js");
  // Walk braces to find the end of the function body.
  let depth = 0;
  let i = VIEWER_JS.indexOf("{", start);
  for (; i < VIEWER_JS.length; i += 1) {
    if (VIEWER_JS[i] === "{") depth += 1;
    else if (VIEWER_JS[i] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return VIEWER_JS.slice(start, i + 1);
})();

const PAGE_HTML = `<!doctype html><html><body>
  <div class="studio-page-wrap"><div class="studio-page">
    <div id="studioDocumentRender" hidden></div>
    <textarea id="studioNdaText"></textarea>
    <div id="studioSourceReviewBar" hidden>
      <button id="studioReviewPastedButton" disabled>Review pasted text</button>
      <span id="studioReviewPastedStatus" hidden></span>
    </div>
  </div></div>
</body></html>`;

const BOOTSTRAP = `
  window.state = {
    reviewParagraphs: [
      { id: "p1", index: 0, text: "First clause." },
      { id: "p2", index: 1, text: "Second clause." },
    ],
    reviewSourceText: "First clause.\\n\\nSecond clause.",
    sourceTextDirty: false,
  };
  window.studioNdaText = document.getElementById("studioNdaText");
  window.studioDocumentRender = document.getElementById("studioDocumentRender");
  // side-effect-only globals the source module reaches for -> safe no-ops
  window.markSourceEdited = function () {};
  window.setSourceText = function (text) { window.studioNdaText.value = text; };
  window.reviewErrorFromPayload = function (p, fallback) { return new Error((p && p.error) || fallback); };
  window.renderResult = function () { window.__renderResultCalled = true; };
  // Seed the textarea with the model text (as syncReviewSourceFromParagraphs would).
  window.studioNdaText.value = "First clause.\\n\\nSecond clause.";
`;

function paragraphs(page) {
  return page.evaluate(() => window.state.reviewParagraphs.map((p) => ({ id: p.id, text: p.text })));
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
    await page.addScriptTag({ content: BOOTSTRAP });
    await page.addScriptTag({ content: SYNC_FN });
    await page.addScriptTag({ content: SOURCE_JS });
    await page.evaluate(() => setupSourceEditors());

    // ---- FIX 1: typed edit reconciles into the model -----------------------
    await check("editing an existing paragraph persists into reviewParagraphs (preserving id)", async () => {
      await page.evaluate(() => {
        window.studioNdaText.value = "First clause EDITED.\n\nSecond clause.";
        window.studioNdaText.dispatchEvent(new Event("input", { bubbles: true }));
      });
      // dirty flag flips immediately so a sync cannot clobber the pending text.
      const dirty = await page.evaluate(() => window.state.sourceTextDirty);
      assert.equal(dirty, true, "sourceTextDirty should be true right after input");
      // Force the reconcile (the production debounce is 400ms; call it directly).
      await page.evaluate(() => reconcileSourceTextIntoParagraphs());
      const paras = await paragraphs(page);
      assert.equal(paras.length, 2, "paragraph count preserved");
      assert.equal(paras[0].text, "First clause EDITED.", "edit not persisted into the model");
      assert.equal(paras[0].id, "p1", "existing paragraph id must be preserved");
      assert.equal(paras[1].id, "p2", "untouched paragraph id preserved");
      const dirtyAfter = await page.evaluate(() => window.state.sourceTextDirty);
      assert.equal(dirtyAfter, false, "dirty flag cleared after reconcile");
    });

    // ---- FIX 1 guard: sync must not clobber pending keystrokes -------------
    await check("syncReviewSourceFromParagraphs does NOT overwrite the textarea while dirty", async () => {
      await page.evaluate(() => {
        // Simulate mid-typing: pending text in the box, dirty set, NOT yet reconciled.
        window.studioNdaText.value = "Half-typed new parag";
        window.state.sourceTextDirty = true;
      });
      await page.evaluate(() => syncReviewSourceFromParagraphs());
      const value = await page.evaluate(() => window.studioNdaText.value);
      assert.equal(value, "Half-typed new parag", "sync clobbered pending input (silent data-loss)");
    });

    await check("syncReviewSourceFromParagraphs DOES write the model text when not dirty", async () => {
      await page.evaluate(() => {
        window.state.sourceTextDirty = false;
        window.state.reviewParagraphs = [
          { id: "p1", index: 0, text: "Synced clause." },
        ];
        window.studioNdaText.value = "stale";
      });
      await page.evaluate(() => syncReviewSourceFromParagraphs());
      const value = await page.evaluate(() => window.studioNdaText.value);
      assert.equal(value, "Synced clause.", "sync should refresh the textarea from the model when clean");
    });

    // ---- FIX 1: adding a new block mints a paragraph; removing trims --------
    await check("adding a blank-line block appends a new paragraph", async () => {
      await page.evaluate(() => {
        window.state.reviewParagraphs = [{ id: "p1", index: 0, text: "Only clause." }];
        window.studioNdaText.value = "Only clause.\n\nBrand new clause.";
        window.state.sourceTextDirty = true;
        reconcileSourceTextIntoParagraphs();
      });
      const paras = await paragraphs(page);
      assert.equal(paras.length, 2, "new block should append a paragraph");
      assert.equal(paras[0].id, "p1", "existing id preserved");
      assert.equal(paras[1].text, "Brand new clause.");
      assert.ok(paras[1].id && paras[1].id !== "p1", "new paragraph gets a fresh id");
    });

    // ---- FIX 4: Review-pasted bar visibility + enablement -------------------
    await check("Review-pasted bar shows only with the editor active and text present", async () => {
      const state1 = await page.evaluate(() => {
        window.studioNdaText.hidden = false;
        window.studioNdaText.value = "";
        updateSourceReviewBar();
        const bar = document.getElementById("studioSourceReviewBar");
        const btn = document.getElementById("studioReviewPastedButton");
        return { barHidden: bar.hidden, btnDisabled: btn.disabled };
      });
      assert.equal(state1.barHidden, false, "bar visible when editor active");
      assert.equal(state1.btnDisabled, true, "button disabled with no text");

      const state2 = await page.evaluate(() => {
        window.studioNdaText.value = "Some pasted NDA text.";
        updateSourceReviewBar();
        const btn = document.getElementById("studioReviewPastedButton");
        return { btnDisabled: btn.disabled };
      });
      assert.equal(state2.btnDisabled, false, "button enabled once text present");

      const state3 = await page.evaluate(() => {
        window.studioNdaText.hidden = true; // rendered surface took over
        updateSourceReviewBar();
        const bar = document.getElementById("studioSourceReviewBar");
        return { barHidden: bar.hidden, display: bar.style.display };
      });
      assert.equal(state3.barHidden, true, "bar hidden when the rendered surface is showing");
      assert.equal(state3.display, "none", "bar display:none so inline layout can't override [hidden]");
    });

  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error("\nreview-source-persist.cjs FAILED: " + failures.length + " check(s)");
    process.exit(1);
  }
  console.log("\nreview-source-persist.cjs PASSED");
}

main().catch((error) => { console.error(error); process.exit(1); });
