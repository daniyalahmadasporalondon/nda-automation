// Generator-editor toolbar extras: Clear formatting, Superscript/Subscript
// (vertAlign), and the Ctrl/Cmd+B/I/U keyboard shortcuts. Self-contained
// Playwright DOM test: it loads the REAL shared run-format helpers
// (review-workstation-format.js), the shared rendering globals
// (redline-rendering.js) and the generator editor (generator-editor.js) into a
// blank page carrying the generator toolbar + render container, boots the editor
// against a one-paragraph draft, selects the paragraph text, then drives each
// control and asserts the run model. No app boot, no Python server.
//
// Run: node tests/frontend/generator-format-extras.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const FORMAT_SRC = fs.readFileSync(path.join(ROOT, "static/js/review-workstation-format.js"), "utf8");
const REDLINE_SRC = fs.readFileSync(path.join(ROOT, "static/js/redline-rendering.js"), "utf8");
const EDITOR_SRC = fs.readFileSync(path.join(ROOT, "static/js/generator-editor.js"), "utf8");

const PARAGRAPH_TEXT = "Hello world";

// The generator toolbar buttons the editor's bindToolbar() wires by id, plus the
// render container and one editable draft paragraph.
const PAGE_HTML = `<!doctype html><html><body>
  <div id="generatorFormatToolbar" role="toolbar">
    <button id="genFormatBold" type="button" aria-pressed="false">B</button>
    <button id="genFormatItalic" type="button" aria-pressed="false">I</button>
    <button id="genFormatSuperscript" type="button" aria-pressed="false">x2</button>
    <button id="genFormatSubscript" type="button" aria-pressed="false">x2</button>
    <button id="genFormatClear" type="button">clear</button>
    <select id="genFontSelect"><option value="">Default font</option></select>
    <select id="genFontSize"><option value="11" selected>11</option></select>
    <button id="genFontSizeUp"></button>
    <button id="genFontSizeDown"></button>
    <button id="genUndo"></button>
    <button id="genAlignLeft"></button>
    <button id="genAlignCenter"></button>
    <button id="genAlignRight"></button>
    <button id="genAlignJustify"></button>
  </div>
  <div id="generatorDocumentRender"></div>
  <div id="draftIntakePreview"></div>
  <div id="draftIntakeStatus"></div>
</body></html>`;

// Select the whole text of the active editable paragraph so the run-scope toggles
// (which read window.getSelection within the editable) operate on it.
function selectParagraph(page) {
  return page.evaluate(() => {
    const editable = document.querySelector('[data-editable-paragraph-id]');
    const range = document.createRange();
    range.selectNodeContents(editable);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    editable.focus();
    // Dispatch focus explicitly so the editor's focus listener (which records the
    // active paragraph + refreshes/enables the toolbar) fires deterministically in
    // headless, regardless of whether the synthetic focus() bubbled an event.
    editable.dispatchEvent(new FocusEvent("focus"));
  });
}

function activeRuns(page) {
  return page.evaluate(() => {
    const snapshot = window.generatorEditor.edits();
    const para = (snapshot.paragraphs || [])[0] || {};
    return para.runs || null;
  });
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const failures = [];
  try {
    await page.setContent(PAGE_HTML);
    await page.evaluate(() => {
      window.escapeHtml = (value) =>
        String(value == null ? "" : value)
          .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
      globalThis.escapeHtml = window.escapeHtml;
      // The generator editor's selection helpers live in review-workstation-viewer.js
      // (not under test). Faithful pure-DOM stubs of the three it calls so the
      // selection->offset mapping behaves exactly as in the app.
      globalThis.editableSelectionTextOffset = function (editable, node, offset) {
        const range = document.createRange();
        range.selectNodeContents(editable);
        try { range.setEnd(node, offset); } catch (e) { return editable.innerText.length; }
        return range.toString().length;
      };
      globalThis.editableTextPositionForOffset = function (editable, offset) {
        const walker = document.createTreeWalker(editable, NodeFilter.SHOW_TEXT);
        let current; let remaining = offset; let lastTextNode = null;
        while ((current = walker.nextNode())) {
          lastTextNode = current;
          const length = current.textContent.length;
          if (remaining <= length) return { node: current, offset: remaining };
          remaining -= length;
        }
        if (lastTextNode) return { node: lastTextNode, offset: lastTextNode.textContent.length };
        return { node: editable, offset: 0 };
      };
      globalThis.editableParagraphText = function (editable) {
        return editable.innerText.replace(/ /g, " ").replace(/\r\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
      };
    });
    // The shared `state` free binding + real shared run-format helpers, then the
    // rendering globals, then the editor.
    await page.addScriptTag({ content: "const state = {};" });
    await page.addScriptTag({ content: FORMAT_SRC });
    await page.addScriptTag({ content: REDLINE_SRC });
    await page.addScriptTag({ content: EDITOR_SRC });

    // Boot the editor against a single-paragraph draft preview and render it.
    await page.evaluate((text) => {
      const preview = document.getElementById("draftIntakePreview");
      preview.innerHTML = `<p>${text}</p>`;
      window.generatorEditor.showDraft(preview);
    }, PARAGRAPH_TEXT);

    // ---- Superscript / Subscript (vertAlign) ---------------------------------
    await selectParagraph(page);
    await page.click("#genFormatSuperscript");
    let runs = await activeRuns(page);
    assert.ok(runs && runs.every((r) => r.vertAlign === "superscript"), `gen superscript not set, got ${JSON.stringify(runs)}`);

    // Subscript replaces superscript (mutually exclusive).
    await selectParagraph(page);
    await page.click("#genFormatSubscript");
    runs = await activeRuns(page);
    assert.ok(runs && runs.every((r) => r.vertAlign === "subscript"), `gen subscript did not replace superscript, got ${JSON.stringify(runs)}`);

    // Toggle subscript off -> vertAlign removed.
    await selectParagraph(page);
    await page.click("#genFormatSubscript");
    runs = await activeRuns(page);
    assert.ok(runs && runs.every((r) => !("vertAlign" in r)), `gen subscript toggle-off failed, got ${JSON.stringify(runs)}`);

    // ---- Clear formatting -----------------------------------------------------
    await selectParagraph(page);
    await page.click("#genFormatBold");
    await selectParagraph(page);
    await page.click("#genFormatItalic");
    await selectParagraph(page);
    await page.click("#genFormatSuperscript");
    runs = await activeRuns(page);
    assert.ok(
      runs && runs.every((r) => r.bold && r.italic && r.vertAlign === "superscript"),
      `gen pre-clear formatting not set, got ${JSON.stringify(runs)}`,
    );
    await selectParagraph(page);
    await page.click("#genFormatClear");
    runs = await activeRuns(page);
    assert.ok(
      runs && runs.every((r) => Object.keys(r).filter((k) => k !== "text").length === 0),
      `gen clear formatting left props behind, got ${JSON.stringify(runs)}`,
    );
    assert.equal(runs.map((r) => r.text).join(""), PARAGRAPH_TEXT, "gen clear must not alter text");

    // ---- Ctrl+B / Ctrl+I keyboard shortcuts ----------------------------------
    await selectParagraph(page);
    const ctrlB = await page.evaluate(() => {
      const editable = document.querySelector('[data-editable-paragraph-id]');
      editable.dispatchEvent(new KeyboardEvent("keydown", { key: "b", ctrlKey: true, bubbles: true, cancelable: true }));
      const snapshot = window.generatorEditor.edits();
      return (snapshot.paragraphs[0].runs || []).every((r) => r.bold === true);
    });
    assert.ok(ctrlB, "gen Ctrl+B keydown did not set bold");
    await selectParagraph(page);
    const ctrlI = await page.evaluate(() => {
      const editable = document.querySelector('[data-editable-paragraph-id]');
      editable.dispatchEvent(new KeyboardEvent("keydown", { key: "i", ctrlKey: true, bubbles: true, cancelable: true }));
      const snapshot = window.generatorEditor.edits();
      return (snapshot.paragraphs[0].runs || []).every((r) => r.italic === true && r.bold === true);
    });
    assert.ok(ctrlI, "gen Ctrl+I keydown did not set italic (bold retained)");
  } catch (error) {
    failures.push(error);
  } finally {
    await browser.close();
  }

  if (failures.length) {
    for (const error of failures) console.error(error.message || error);
    console.error("generator-format-extras.cjs FAIL");
    process.exit(1);
  }
  console.log("generator-format-extras.cjs PASS");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
