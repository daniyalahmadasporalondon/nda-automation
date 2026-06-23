// Server-free Playwright checks for FIX 3 (P2): clause-anchor jumping on the
// rendered review surface.
//   (a) target FOUND: scrollRenderedClauseToView must actually bring the paragraph
//       INTO VIEW (the "highlights-but-doesn't-scroll" bug -- the right paragraph
//       got the pulse class while .studio-page-wrap stayed at scrollTop 0). We
//       assert the target's scrollIntoView is invoked.
//   (b) no target in the Original page-image view: it must FALL BACK to the
//       structured/redline view (which renders data-paragraph-id anchors) and
//       re-jump -- never a silent dead click.
//   (c) no target and no fallback possible: surface an inline notice (not silent).
//
// Loads the REAL config.js + review-workstation-viewer.js, stubs the globals the
// jump path reaches for, and drives scrollRenderedClauseToView directly.
//
// Run: node tests/frontend/review-clause-anchor.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const CONFIG_JS = read("static/js/config.js");
const VIEWER_JS = read("static/js/review-workstation-viewer.js");

const PAGE_HTML = `<!doctype html><html><body>
  <div class="studio-page-wrap">
    <div id="studioDocumentRender"></div>
  </div>
  <p id="studioResultMeta"></p>
</body></html>`;

// state + the globals the jump path needs. setDocumentViewMode is a SPY that also
// repaints the render surface with a data-paragraph-id anchor so the fallback
// re-jump can succeed.
const BOOTSTRAP = `
  window.__calls = { scrollIntoView: 0, setViewMode: [], lastScrolledId: null };
  window.state = {
    documentViewMode: "redline",
    reviewClauses: [
      { id: "governing_law", matched_paragraph_ids: ["p68"] },
    ],
    reviewParagraphs: [{ id: "p68", index: 0, text: "Governing law clause." }],
    clauseJumpIndexes: {},
    reviewDocumentRender: null,
  };
  window.studioDocumentRender = document.getElementById("studioDocumentRender");
  window.studioResultMeta = document.getElementById("studioResultMeta");
  window.effectiveReviewRedlines = function () { return []; };
  window.referencedParagraphIds = function () { return []; };
  // Paint a paragraph anchor for the matched id (mirrors the redline render).
  function paintAnchor(id) {
    window.studioDocumentRender.innerHTML =
      '<div class="studio-doc-paragraph" data-paragraph-id="' + id + '">para</div>';
    const el = window.studioDocumentRender.querySelector('[data-paragraph-id]');
    // Stub scrollIntoView so we can detect the scroll without a real layout.
    el.scrollIntoView = function () { window.__calls.scrollIntoView += 1; window.__calls.lastScrolledId = id; };
    return el;
  }
  window.__paintAnchor = paintAnchor;
  // setDocumentViewMode is defined in the REAL viewer module (loaded after this
  // bootstrap), so we DON'T override it -- we exercise the real one. It calls
  // updateDocumentViewModeButtons() + renderStudioDocumentHighlights(), which we
  // stub here. The render stub records the switch and paints the data-paragraph-id
  // anchor the Original page-image view lacked (mirroring the redline render).
  window.updateDocumentViewModeButtons = function () {};
  window.renderStudioDocumentHighlights = function () {
    window.__calls.setViewMode.push(window.state.documentViewMode);
    if (window.state.documentViewMode === "redline") window.__paintAnchor("p68");
  };
`;

async function calls(page) {
  return page.evaluate(() => window.__calls);
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
    await page.addScriptTag({ content: CONFIG_JS });
    await page.addScriptTag({ content: BOOTSTRAP });
    await page.addScriptTag({ content: VIEWER_JS });

    // ---- (a) target found -> the paragraph is actually scrolled into view ---
    await check("target found: scrollRenderedClauseToView scrolls the paragraph into view + pulses it", async () => {
      await page.evaluate(() => { window.__paintAnchor("p68"); });
      await page.evaluate(() => scrollRenderedClauseToView("governing_law"));
      const c = await calls(page);
      assert.equal(c.scrollIntoView, 1, "scrollIntoView must be called on the found target");
      assert.equal(c.lastScrolledId, "p68", "scrolled the wrong paragraph");
      const pulsed = await page.evaluate(() =>
        Boolean(document.querySelector('[data-paragraph-id="p68"]')?.classList.contains("paragraph-pulse")));
      assert.equal(pulsed, true, "target must get the pulse highlight");
    });

    // ---- (b) Original page-image view, no paragraph anchor -> fall back -----
    await check("no anchor in Original view: falls back to Redline and re-jumps (not silent)", async () => {
      await page.evaluate(() => {
        window.__calls = { scrollIntoView: 0, setViewMode: [], lastScrolledId: null };
        window.state.documentViewMode = "original";
        window.state.clauseJumpIndexes = {};
        // Original view: only a page figure, NO data-paragraph-id anchor.
        window.studioDocumentRender.innerHTML =
          '<figure class="review-render-page" data-review-render-page="5"></figure>';
      });
      await page.evaluate(() => scrollRenderedClauseToView("governing_law"));
      // The fallback re-jump runs on the next animation frame.
      await page.waitForFunction(() => window.__calls.scrollIntoView > 0, { timeout: 2000 }).catch(() => {});
      const c = await calls(page);
      assert.deepEqual(c.setViewMode, ["redline"], "must switch Original -> Redline to get paragraph anchors");
      assert.equal(c.scrollIntoView, 1, "must re-jump (scroll) after the view switch");
      assert.equal(c.lastScrolledId, "p68", "re-jump scrolled the wrong paragraph");
    });

    // ---- (c) no target + no fallback -> inline notice, not silent ----------
    await check("no anchor and no structured fallback: surfaces an inline notice", async () => {
      await page.evaluate(() => {
        window.__calls = { scrollIntoView: 0, setViewMode: [], lastScrolledId: null };
        window.state.documentViewMode = "original";
        window.state.reviewParagraphs = []; // no structured model to fall back to
        window.state.reviewClauses = [{ id: "ghost", matched_paragraph_ids: [] }];
        window.state.clauseJumpIndexes = {};
        window.studioResultMeta.textContent = "";
        window.studioDocumentRender.innerHTML =
          '<figure class="review-render-page" data-review-render-page="1"></figure>';
      });
      await page.evaluate(() => scrollRenderedClauseToView("ghost"));
      const c = await calls(page);
      assert.deepEqual(c.setViewMode, [], "must NOT switch views with no structured model");
      assert.equal(c.scrollIntoView, 0, "no scroll when there is no target");
      const meta = await page.evaluate(() => window.studioResultMeta.textContent);
      assert.match(meta, /Jump unavailable/i, `expected an inline notice, got '${meta}'`);
    });

  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error("\nreview-clause-anchor.cjs FAILED: " + failures.length + " check(s)");
    process.exit(1);
  }
  console.log("\nreview-clause-anchor.cjs PASSED");
}

main().catch((error) => { console.error(error); process.exit(1); });
