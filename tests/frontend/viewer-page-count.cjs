// Server-free Playwright check for FIX 2 (P1): the document pager must report the
// REAL page count -- the number of rendered page-image tiles
// (figure[data-review-render-page]) -- not ceil(scrollHeight / viewportHeight),
// which inflated "1 / 7" to "1 / 17" purely because the reconstructed text was
// taller than the viewport. When there are NO tiles (DOCX / faithful continuous
// scroll) the original viewport-slice pagination is preserved.
//
// Loads the REAL static/js/viewer-controls.js (an IIFE that auto-inits) against a
// DOM that mirrors the studio document pane, then asserts #studioPageIndicator.
//
// Run: node tests/frontend/viewer-page-count.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const VIEWER_CONTROLS_JS = fs.readFileSync(path.join(ROOT, "static/js/viewer-controls.js"), "utf8");

// A scroll container with a fixed height; the page is the scrolled content. The
// pager controls live in the toolbar. Tiles are tall so the count is unambiguous.
function pageHtml(innerPage) {
  return `<!doctype html><html><head><style>
    #reviewView .studio-page-wrap { height: 300px; overflow: auto; }
    .review-render-page { height: 800px; display: block; }
  </style></head><body>
  <main id="reviewView">
    <button id="studioPagePrev"></button>
    <span id="studioPageIndicator">1 / 1</span>
    <button id="studioPageNext"></button>
    <button id="studioZoomOut"></button>
    <span id="studioZoomLevel"></span>
    <button id="studioZoomIn"></button>
    <button id="studioFullscreen"></button>
    <div class="studio-document">
      <div class="studio-page-wrap">
        <div class="studio-page">${innerPage}</div>
      </div>
    </div>
  </main>
  </body></html>`;
}

const SEVEN_TILES = Array.from({ length: 7 }, (_n, i) =>
  `<figure class="review-render-page" data-review-render-page="${i + 1}"></figure>`).join("");

// No tiles: a single tall reconstructed text block (continuous scroll).
const TALL_TEXT = `<div class="studio-document-render" style="height: 2400px;"></div>`;

async function indicator(page) {
  return page.evaluate(() => document.getElementById("studioPageIndicator").textContent.trim());
}

async function main() {
  const browser = await chromium.launch();
  const failures = [];
  const check = async (label, fn) => {
    try { await fn(); console.log("  ok -", label); }
    catch (error) { failures.push(label); console.error("  FAIL -", label, "\n   ", error.message); }
  };

  try {
    // ---- 7 page-image tiles -> "1 / 7", NOT a content-height slice count ----
    await check("page count equals the rendered tile count (7 tiles => '/ 7')", async () => {
      const page = await browser.newPage();
      await page.setContent(pageHtml(SEVEN_TILES));
      await page.addScriptTag({ content: VIEWER_CONTROLS_JS });
      // Let the IIFE init + the initial updatePages run.
      await page.waitForFunction(() => {
        const el = document.getElementById("studioPageIndicator");
        return el && /\/\s*7$/.test(el.textContent.trim());
      }, { timeout: 4000 }).catch(() => {});
      const text = await indicator(page);
      assert.match(text, /\/\s*7$/, `expected '/ 7' (real tile count), got '${text}'`);
      assert.match(text, /^1\s*\//, `expected current page 1 at top, got '${text}'`);
      // 7 tiles * 800px = 5600px content over a 300px viewport => the OLD slice
      // formula would have read ceil(5600/300) = 19, not 7. Prove we are not that.
      assert.ok(!/\/\s*1[0-9]$/.test(text), `page total looks like a slice count: '${text}'`);
      await page.close();
    });

    // ---- Next page steps tile-to-tile + current advances -------------------
    await check("Next page advances the current page to the next tile", async () => {
      const page = await browser.newPage();
      await page.setContent(pageHtml(SEVEN_TILES));
      await page.addScriptTag({ content: VIEWER_CONTROLS_JS });
      await page.waitForTimeout(150);
      await page.click("#studioPageNext");
      // Smooth scroll -> wait for the scroll + the rAF-driven updatePages.
      await page.waitForFunction(() => {
        const el = document.getElementById("studioPageIndicator");
        return el && /^2\s*\//.test(el.textContent.trim());
      }, { timeout: 4000 }).catch(() => {});
      const text = await indicator(page);
      assert.match(text, /^2\s*\/\s*7$/, `Next should land on page 2 of 7, got '${text}'`);
      await page.close();
    });

    // ---- No tiles -> original continuous-scroll pagination preserved -------
    await check("no tiles falls back to viewport-slice pagination (regression guard)", async () => {
      const page = await browser.newPage();
      await page.setContent(pageHtml(TALL_TEXT));
      await page.addScriptTag({ content: VIEWER_CONTROLS_JS });
      await page.waitForTimeout(150);
      const text = await indicator(page);
      // 2400px content / 300px viewport => ceil = 8 slices. The point: it is a
      // slice count (>1), proving the continuous-scroll path is intact.
      assert.match(text, /^1\s*\/\s*8$/, `expected slice pagination '1 / 8', got '${text}'`);
      await page.close();
    });

  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error("\nviewer-page-count.cjs FAILED: " + failures.length + " check(s)");
    process.exit(1);
  }
  console.log("\nviewer-page-count.cjs PASSED");
}

main().catch((error) => { console.error(error); process.exit(1); });
