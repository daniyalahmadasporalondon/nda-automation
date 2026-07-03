// Server-free Playwright proof for the mobile tab-bar scroll affordance.
//
// At phone widths (~390px) the 8-tab nav.tabbar overflows horizontally; before
// this affordance the off-screen tabs (Review/Playbook/Admin/Guide) were simply
// invisible with ZERO hint that more existed. The fix is the standard pure-CSS
// scroll-shadow trick on .tabbar inside the (max-width: 920px) media query:
// edge shadows (background-attachment: scroll) signal hidden content, while
// panel-colored cover gradients (background-attachment: local) scroll with the
// content and hide the shadow at a fully-scrolled edge.
//
// Asserted against the REAL styles.css in a real Chromium at a 390x844 viewport:
//   1. the tabbar actually overflows (scrollWidth > clientWidth) and is
//      user-scrollable (overflow-x: auto) — the affordance has something to hint at;
//   2. the scroll-shadow paint is live: computed background-image carries the
//      4 gradient layers and background-attachment mixes local + scroll;
//   3. scrolling works: the last tab (Guide) starts fully off-screen and comes
//      into view after scrollLeft is advanced;
//   4. at desktop width the media query does not apply (no scroll shadows, no
//      overflow scrolling) — the affordance is mobile-scoped, not a redesign.
//
// No Python backend, so it cannot hang on server startup.
//
// Run: node tests/frontend/tabbar-mobile-scroll.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const STYLES_CSS = fs.readFileSync(path.join(ROOT, "static/styles.css"), "utf8");
const INDEX_HTML = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");

// The REAL topbar markup, lifted from index.html so the test cannot drift from
// the shipped tab roster (a 9th tab or renamed class breaks this extraction).
const topbarMatch = INDEX_HTML.match(/<header class="topbar">[\s\S]*?<nav class="tabbar"[\s\S]*?<\/nav>/);
assert.ok(topbarMatch, "could not extract the topbar/tabbar markup from static/index.html");
const TOPBAR_HTML = `${topbarMatch[0]}</header>`;

const PAGE_HTML = `<!doctype html><html><head><style id="real-css"></style></head><body>
  <div class="app-shell"><section class="workspace-shell">${TOPBAR_HTML}</section></div>
</body></html>`;

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  process.stdout.write(`  ok ${name}\n`);
}

async function main() {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.setContent(PAGE_HTML);
    // Inject the real stylesheet (setContent cannot resolve /static/ URLs).
    await page.evaluate((css) => {
      document.getElementById("real-css").textContent = css;
    }, STYLES_CSS);

    const metrics = () => page.evaluate(() => {
      const bar = document.querySelector("nav.tabbar");
      const style = getComputedStyle(bar);
      return {
        clientWidth: bar.clientWidth,
        scrollWidth: bar.scrollWidth,
        scrollLeft: bar.scrollLeft,
        overflowX: style.overflowX,
        backgroundImage: style.backgroundImage,
        backgroundAttachment: style.backgroundAttachment,
      };
    });

    await test("tabbar overflows and is scrollable at 390px", async () => {
      const m = await metrics();
      assert.equal(m.overflowX, "auto", "tabbar must be user-scrollable at phone width");
      assert.ok(
        m.scrollWidth > m.clientWidth + 40,
        `expected real horizontal overflow, got scrollWidth=${m.scrollWidth} clientWidth=${m.clientWidth}`,
      );
    });

    await test("scroll-shadow affordance paints at 390px", async () => {
      const m = await metrics();
      const gradientLayers = (m.backgroundImage.match(/gradient\(/g) || []).length;
      assert.ok(
        gradientLayers >= 4,
        `expected the 4-layer scroll-shadow paint (2 covers + 2 shadows), got ${gradientLayers} in ${m.backgroundImage}`,
      );
      assert.match(m.backgroundAttachment, /local/, "cover layers must scroll with content (attachment: local)");
      assert.match(m.backgroundAttachment, /scroll/, "shadow layers must pin to the visible edge (attachment: scroll)");
    });

    await test("off-screen Guide tab scrolls into view", async () => {
      const guideVisible = () => page.evaluate(() => {
        const bar = document.querySelector("nav.tabbar");
        const guide = document.getElementById("guideTab");
        const barBox = bar.getBoundingClientRect();
        const box = guide.getBoundingClientRect();
        return box.left >= barBox.left && box.right <= barBox.right + 1;
      });
      assert.equal(await guideVisible(), false, "Guide tab should start off-screen at 390px");
      await page.evaluate(() => {
        const bar = document.querySelector("nav.tabbar");
        bar.scrollLeft = bar.scrollWidth;
      });
      assert.equal(await guideVisible(), true, "Guide tab should be reachable by scrolling the tabbar");
      const m = await metrics();
      assert.ok(m.scrollLeft > 0, "scrollLeft should have advanced");
    });

    await test("desktop width is untouched (no mobile scroll styling)", async () => {
      await page.setViewportSize({ width: 1280, height: 800 });
      const m = await metrics();
      assert.notEqual(m.overflowX, "auto", "desktop tabbar should not adopt the mobile overflow mode");
      assert.equal(m.backgroundImage, "none", "desktop tabbar should carry no scroll-shadow paint");
    });
  } finally {
    await browser.close();
  }
  process.stdout.write(`tabbar-mobile-scroll: ${passed} checks passed\n`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
