// Server-free Playwright check for the run-format toolbar additions: underline,
// strikethrough, text color, and named highlight. It loads the REAL toolbar JS
// (static/js/review-workstation-format.js) into a minimal page that carries only
// the toolbar HTML + one editable paragraph, stubs the handful of globals the
// module guards for (state, selection resolver, render/dirty hooks), then drives
// each control and asserts it writes the expected property onto the active
// paragraph's run model. No Python backend, so it cannot hang on server startup.
//
// Run: node tests/frontend/format-toolbar-run-props.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const FORMAT_JS = fs.readFileSync(
  path.join(ROOT, "static", "js", "review-workstation-format.js"),
  "utf8",
);
const RENDERING_JS = fs.readFileSync(
  path.join(ROOT, "static", "js", "redline-rendering.js"),
  "utf8",
);

// Minimal page: the four new controls + bold (as a control twin) and one editable
// paragraph whose full text is the selection target.
const PARAGRAPH_TEXT = "Hello world";
const PAGE_HTML = `<!doctype html><html><body>
  <div id="studioFormatToolbar" role="toolbar">
    <button id="studioFormatBold" type="button" aria-pressed="false">B</button>
    <button id="studioFormatItalic" type="button" aria-pressed="false">I</button>
    <button id="studioFormatUnderline" type="button" aria-pressed="false">U</button>
    <button id="studioFormatStrike" type="button" aria-pressed="false">S</button>
    <input type="color" id="studioFormatColor" value="#000000">
    <select id="studioFormatHighlight">
      <option value="">No highlight</option>
      <option value="yellow">Yellow</option>
      <option value="cyan">Cyan</option>
    </select>
    <select id="studioFontSelect"><option value="">Default font</option></select>
    <select id="studioFontSize"><option value="11" selected>11</option></select>
    <button id="studioFontSizeUp"></button>
    <button id="studioFontSizeDown"></button>
    <button id="studioAlignLeft"></button>
    <button id="studioAlignCenter"></button>
    <button id="studioAlignRight"></button>
    <button id="studioAlignJustify"></button>
  </div>
  <div id="studioDocumentRender"><p data-editable-paragraph-id="p1">${PARAGRAPH_TEXT}</p></div>
</body></html>`;

// Stubs for the globals the module references (all behind typeof guards except
// `state`, which it reads directly). Selection always covers the whole paragraph.
const BOOTSTRAP = `
  window.state = { activeFormatParagraphId: "p1", reviewParagraphs: [{ id: "p1", text: ${JSON.stringify(PARAGRAPH_TEXT)} }] };
  window.studioDocumentRender = document.getElementById("studioDocumentRender");
  window.selectedTextInParagraph = function (id) {
    if (id !== "p1") return null;
    return { selectedText: ${JSON.stringify(PARAGRAPH_TEXT)}, startOffset: 0, endOffset: ${PARAGRAPH_TEXT.length} };
  };
  window.setFileMeta = function () {};
  window.markRedlineDraftDirty = function () {};
  window.renderStudioDocumentHighlights = function () {};
  window.pushReviewEditHistoryEntry = function () {};
`;

function activeRuns(page) {
  return page.evaluate(() => window.state.reviewParagraphs[0].runs || null);
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const failures = [];
  try {
    await page.setContent(PAGE_HTML);
    await page.addScriptTag({ content: BOOTSTRAP });
    await page.addScriptTag({ content: FORMAT_JS });
    await page.evaluate(() => bindFormatToolbar());

    // Underline -> run carries underline:true.
    await page.click("#studioFormatUnderline");
    let runs = await activeRuns(page);
    assert.ok(runs && runs.every((r) => r.underline === true), "underline not set on runs");
    assert.equal(
      await page.getAttribute("#studioFormatUnderline", "aria-pressed"),
      "true",
      "underline button not pressed",
    );

    // Strike -> run carries strike:true (and keeps underline).
    await page.click("#studioFormatStrike");
    runs = await activeRuns(page);
    assert.ok(runs && runs.every((r) => r.strike === true && r.underline === true), "strike not set on runs");

    // Text color picker -> run carries color as bare RRGGBB (no #).
    await page.evaluate(() => {
      const input = document.getElementById("studioFormatColor");
      input.value = "#ff8800";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    runs = await activeRuns(page);
    assert.ok(runs && runs.every((r) => r.color === "FF8800"), `color not set, got ${JSON.stringify(runs)}`);

    // Highlight select -> run carries the NAMED palette value.
    await page.selectOption("#studioFormatHighlight", "cyan");
    runs = await activeRuns(page);
    assert.ok(runs && runs.every((r) => r.highlight === "cyan"), `highlight not set, got ${JSON.stringify(runs)}`);

    // Toggle underline OFF -> property removed, others retained.
    await page.click("#studioFormatUnderline");
    runs = await activeRuns(page);
    assert.ok(
      runs && runs.every((r) => !("underline" in r) && r.strike === true && r.color === "FF8800" && r.highlight === "cyan"),
      `underline toggle-off failed, got ${JSON.stringify(runs)}`,
    );

    // Highlight cleared via the empty option -> property removed.
    await page.selectOption("#studioFormatHighlight", "");
    runs = await activeRuns(page);
    assert.ok(runs && runs.every((r) => !("highlight" in r)), `highlight clear failed, got ${JSON.stringify(runs)}`);

    // ---- CSS-injection defence (run color/highlight rendering) ----------------
    // inlineRunStyle / highlightCssColor interpolate untrusted run color/highlight
    // into an inline style="..."; assert a hostile value is neutralised (no extra
    // declaration, no url() beacon), while legitimate values still render.
    await page.addScriptTag({ content: RENDERING_JS });
    const injection = await page.evaluate(() => {
      const hostileColor = inlineRunStyle({ color: "red;background:url(https://evil/x)" });
      const hostileColorHash = inlineRunStyle({ color: "#fff;background:url(https://evil/x)" });
      const hostileHighlight = inlineRunStyle({ highlight: "red;background:url(https://evil/x)" });
      const validHex = inlineRunStyle({ color: "FF8800" });
      const validNamed = inlineRunStyle({ highlight: "yellow" });
      const validBareword = inlineRunStyle({ color: "rebeccapurple" });
      return { hostileColor, hostileColorHash, hostileHighlight, validHex, validNamed, validBareword };
    });
    for (const [label, style] of [
      ["color", injection.hostileColor],
      ["color#", injection.hostileColorHash],
      ["highlight", injection.hostileHighlight],
    ]) {
      assert.ok(!/url/i.test(style), `${label}: url() leaked into style: ${style}`);
      assert.ok(!/background/i.test(style), `${label}: injected background declaration: ${style}`);
      // A neutralised hostile value emits NO color/background declaration at all.
      assert.equal(style, "", `${label}: hostile value not dropped, got: ${style}`);
    }
    assert.equal(injection.validHex, "color:#FF8800", `valid hex broke: ${injection.validHex}`);
    assert.equal(injection.validNamed, "background-color:#ffff00", `valid named highlight broke: ${injection.validNamed}`);
    assert.equal(injection.validBareword, "color:rebeccapurple", `valid bareword color broke: ${injection.validBareword}`);
  } catch (error) {
    failures.push(error);
  } finally {
    await browser.close();
  }

  if (failures.length) {
    for (const error of failures) console.error(error.message || error);
    console.error("format-toolbar-run-props.cjs FAIL");
    process.exit(1);
  }
  console.log("format-toolbar-run-props.cjs PASS");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
