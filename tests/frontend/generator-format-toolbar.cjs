// Generator editor toolbar parity check: the Generator tab's editor gained the
// four run-format controls the Review toolbar already had -- Underline,
// Strikethrough, Text colour, and Highlight colour -- bound to the SAME shared
// run-format helpers (setRunFormatting/normalizeColorValue from
// review-workstation-format.js). The serializer already preserved these props on
// a run; this locks that the toolbar BUTTONS now exist and apply them.
//
// Self-contained Playwright DOM test: loads the real toolbar HTML region from
// static/index.html + the real format/rendering/editor JS into a blank page (no
// app boot, no Python server), drives the editor's showDraft -> render path,
// makes a real selection over the rendered paragraph, fires each new control, and
// asserts the active paragraph's run model carries the expected property.
//
// Run: node tests/frontend/generator-format-toolbar.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const INDEX_HTML = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");
const FORMAT_SRC = fs.readFileSync(path.join(ROOT, "static/js/review-workstation-format.js"), "utf8");
const REDLINE_SRC = fs.readFileSync(path.join(ROOT, "static/js/redline-rendering.js"), "utf8");
const EDITOR_SRC = fs.readFileSync(path.join(ROOT, "static/js/generator-editor.js"), "utf8");

// ---- Source-level guards (no browser) --------------------------------------
// Lock the toolbar markup + the binding contract at their source so the parity
// can't be silently dropped.
function runSourceGuards() {
  // The four new controls exist in the Generator toolbar region of index.html,
  // with gen*-prefixed IDs mirroring the Review studio* toolbar.
  const toolbar = INDEX_HTML.match(/id="generatorFormatToolbar"[\s\S]*?<div class="studio-document-render generator-document-render"/);
  assert.ok(toolbar, "generatorFormatToolbar region present in index.html");
  const region = toolbar[0];
  for (const id of ["genFormatUnderline", "genFormatStrike", "genFormatColor", "genFormatHighlight"]) {
    assert.ok(region.includes(`id="${id}"`), `generator toolbar declares ${id}`);
  }
  // Pre-existing controls remain intact.
  for (const id of ["genFontSelect", "genFontSize", "genFormatBold", "genFormatItalic", "genUndo", "genAlignLeft"]) {
    assert.ok(region.includes(`id="${id}"`), `generator toolbar keeps existing ${id}`);
  }
  // The colour control is a native colour input; the highlight is a named-palette
  // <select> mirroring the Review tab's option set.
  assert.ok(/<input type="color" id="genFormatColor"/.test(region), "genFormatColor is a colour input");
  for (const opt of ["yellow", "green", "cyan", "magenta", "blue", "red", "darkBlue", "lightGray", "black", "white"]) {
    assert.ok(new RegExp(`<option value="${opt}"`).test(region), `highlight palette includes ${opt}`);
  }

  // The binding reuses the shared helpers (no reimplementation): underline/strike
  // route through toggleRun (which calls setRunFormatting), and the new
  // applyColor/applyHighlight call setRunFormatting/normalizeColorValue directly.
  assert.ok(/underline\.onclick = \(\) => toggleRun\("underline"\)/.test(EDITOR_SRC), "underline bound to toggleRun");
  assert.ok(/strike\.onclick = \(\) => toggleRun\("strike"\)/.test(EDITOR_SRC), "strike bound to toggleRun");
  assert.ok(/color\.oninput = \(\) => applyColor\(color\.value\)/.test(EDITOR_SRC), "color bound to applyColor");
  assert.ok(/highlight\.onchange = \(\) => applyHighlight\(highlight\.value\)/.test(EDITOR_SRC), "highlight bound to applyHighlight");
  assert.ok(/setRunFormatting\(para, snapshot\.startOffset, snapshot\.endOffset, "color", next\)/.test(EDITOR_SRC), "applyColor reuses setRunFormatting");
  assert.ok(/normalizeColorValue\(hex\)/.test(EDITOR_SRC), "applyColor reuses the shared normalizeColorValue");
  assert.ok(/setRunFormatting\(para, snapshot\.startOffset, snapshot\.endOffset, "highlight", value \|\| false\)/.test(EDITOR_SRC), "applyHighlight reuses setRunFormatting");
}

const PARAGRAPH_TEXT = "Confidential terms apply here.";
const PREVIEW_HTML = `
  <article class="nda-doc">
    <p>${PARAGRAPH_TEXT}</p>
  </article>
`;

// Select the full text of the editor's single rendered editable paragraph, set
// the editor's active-paragraph id to match, then refresh the toolbar so its
// disabled state clears. Returns the editable id selected.
async function selectWholeParagraph(page) {
  return page.evaluate(() => {
    const render = document.getElementById("generatorDocumentRender");
    const editable = render.querySelector("[data-editable-paragraph-id]");
    if (!editable) throw new Error("no editable paragraph rendered");
    const id = editable.dataset.editableParagraphId;
    // Mirror the focus handler the editor wires on the editable element.
    editable.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    const range = document.createRange();
    range.selectNodeContents(editable);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return id;
  });
}

function activeRuns(page) {
  return page.evaluate(() => {
    const snap = window.generatorEditor.edits();
    const paras = (snap && snap.paragraphs) || [];
    return paras.length ? (paras[0].runs || null) : null;
  });
}

async function main() {
  runSourceGuards();

  const browser = await chromium.launch();
  const page = await browser.newPage();
  try {
    await page.setContent(`
      <div id="generatorDocumentRender"></div>
      <div id="draftIntakePreview"></div>
      <div id="draftIntakeStatus"></div>
    `);
    await page.evaluate(() => {
      window.escapeHtml = (value) =>
        String(value == null ? "" : value)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
      globalThis.escapeHtml = window.escapeHtml;
    });
    // `state` is a top-level binding the editor (and the app) share lexically.
    await page.addScriptTag({ content: "const state = {};" });
    await page.addScriptTag({ content: REDLINE_SRC });
    // The real shared run-format helpers (setRunFormatting / normalizeColorValue /
    // runRangeHasFormatting / normalizeRun) -- reused, not reimplemented.
    await page.addScriptTag({ content: FORMAT_SRC });
    await page.addScriptTag({ content: EDITOR_SRC });

    // Inject the REAL generator toolbar markup so bindToolbar binds real elements.
    const toolbarHtml = INDEX_HTML.match(/(<div class="studio-format-toolbar" id="generatorFormatToolbar"[\s\S]*?<\/div>\s*)<div class="studio-document-render generator-document-render"/);
    assert.ok(toolbarHtml, "extracted generator toolbar markup");
    await page.evaluate((html) => {
      const holder = document.createElement("div");
      holder.innerHTML = html;
      document.body.insertBefore(holder.firstElementChild, document.getElementById("generatorDocumentRender"));
    }, toolbarHtml[1]);

    // Render the draft through the editor's real entrypoint (binds the toolbar).
    await page.evaluate((previewHtml) => {
      const preview = document.getElementById("draftIntakePreview");
      preview.innerHTML = previewHtml;
      window.generatorEditor.showDraft(preview);
    }, PREVIEW_HTML);

    const failures = [];

    // --- Underline ---
    await selectWholeParagraph(page);
    await page.click("#genFormatUnderline");
    let runs = await activeRuns(page);
    if (!(runs && runs.length && runs.every((r) => r.underline))) {
      failures.push(`underline not applied to run model: ${JSON.stringify(runs)}`);
    }

    // --- Strikethrough ---
    await selectWholeParagraph(page);
    await page.click("#genFormatStrike");
    runs = await activeRuns(page);
    if (!(runs && runs.length && runs.every((r) => r.strike))) {
      failures.push(`strike not applied to run model: ${JSON.stringify(runs)}`);
    }

    // --- Text colour ---
    await selectWholeParagraph(page);
    await page.evaluate(() => {
      const input = document.getElementById("genFormatColor");
      input.value = "#ff0000";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    runs = await activeRuns(page);
    if (!(runs && runs.length && runs.every((r) => String(r.color || "").toUpperCase() === "FF0000"))) {
      failures.push(`color not applied to run model: ${JSON.stringify(runs)}`);
    }

    // --- Highlight colour ---
    await selectWholeParagraph(page);
    await page.evaluate(() => {
      const select = document.getElementById("genFormatHighlight");
      select.value = "yellow";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    runs = await activeRuns(page);
    if (!(runs && runs.length && runs.every((r) => r.highlight === "yellow"))) {
      failures.push(`highlight not applied to run model: ${JSON.stringify(runs)}`);
    }

    if (failures.length) {
      throw new Error("generator-format-toolbar.cjs FAIL:\n" + failures.join("\n"));
    }
    console.log("generator-format-toolbar.cjs PASS");
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
