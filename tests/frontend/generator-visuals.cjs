// Generator "playbook colour" visuals: the live-preview fields and the
// always-visible editor must consistently show FILLED values in the violet
// `.nda-fill` highlight and EMPTY placeholders in the amber `.nda-blank`
// highlight, and neither marker may leak into export run formatting.
//
// This is a self-contained Playwright DOM test: it loads the real
// redline-rendering.js globals + generator-editor.js into a blank page (no app
// boot, no Python server) and drives the editor's own showDraft -> render path
// against a crafted preview fragment, then asserts the rendered editor HTML.
//
// Run: node tests/frontend/generator-visuals.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const REDLINE_SRC = fs.readFileSync(path.join(ROOT, "static/js/redline-rendering.js"), "utf8");
const EDITOR_SRC = fs.readFileSync(path.join(ROOT, "static/js/generator-editor.js"), "utf8");
const DRAFT_INTAKE_SRC = fs.readFileSync(path.join(ROOT, "static/js/draft-intake.js"), "utf8");
const STYLES_SRC = fs.readFileSync(path.join(ROOT, "static/styles.css"), "utf8");

// ---- Source-level guards (no browser) --------------------------------------
// These lock the colour contract at its source so it can't be silently dropped:
// every Generator preview field flows through the violet/amber wrapper, and the
// clean-export run-copy helpers never carry the viewer-only fill/blank markers.
function runSourceGuards() {
  // draft-intake.js: filled -> `nda-fill nda-fill-entity`, empty -> `nda-blank`,
  // and the legacy plain-`.nda-fill` wrapper (which the editor flattens) is gone.
  assert.ok(
    DRAFT_INTAKE_SRC.includes('class="nda-fill nda-fill-entity"'),
    "renderLivePreview wraps filled values in the violet .nda-fill .nda-fill-entity",
  );
  assert.ok(
    DRAFT_INTAKE_SRC.includes('class="nda-blank"'),
    "renderLivePreview wraps empty placeholders in the amber .nda-blank",
  );
  assert.ok(
    !/`<span class="nda-fill">\$\{escapeHtml/.test(DRAFT_INTAKE_SRC),
    "no field uses the legacy plain .nda-fill wrapper (the editor flattens it)",
  );

  // generator-editor.js: the clean-export run copy whitelists real DOCX run
  // formatting only — the fill/blank markers must NOT be copied onto export runs.
  const replacementRunsFor = EDITOR_SRC.match(/function replacementRunsFor[\s\S]*?\n  }\n/);
  assert.ok(replacementRunsFor, "replacementRunsFor present");
  assert.ok(
    !/\bfill\b|\bblank\b/.test(replacementRunsFor[0]),
    "replacementRunsFor (clean export) drops the viewer-only fill/blank markers",
  );
  const runFormatOnly = EDITOR_SRC.match(/function runFormatOnly[\s\S]*?\n  }\n/);
  assert.ok(runFormatOnly, "runFormatOnly present");
  assert.ok(
    !/\bfill\b|\bblank\b/.test(runFormatOnly[0]),
    "runFormatOnly (retile on edit) drops the viewer-only fill/blank markers",
  );

  // styles.css (owned by another teammate — read-only here): confirm the classes
  // the preview/editor emit are themed, so the colours are not invisible.
  assert.ok(/\.nda-blank\s*\{[\s\S]*?var\(--amber-bg\)/.test(STYLES_SRC), ".nda-blank uses --amber-bg");
  assert.ok(/\.nda-blank\s*\{[\s\S]*?var\(--amber-ink\)/.test(STYLES_SRC), ".nda-blank uses --amber-ink");
  assert.ok(/\.nda-fill\s*\{[\s\S]*?var\(--violet-bg\)/.test(STYLES_SRC), ".nda-fill uses --violet-bg");
  assert.ok(/\.nda-fill-entity\s*\{[\s\S]*?var\(--violet-bg\)/.test(STYLES_SRC), ".nda-fill-entity uses --violet-bg");
}

// A preview fragment shaped exactly like draft-intake.js renderLivePreview():
// a filled value carries `nda-fill nda-fill-entity`, an unfilled one `nda-blank`.
const PREVIEW_HTML = `
  <article class="nda-doc">
    <p>
      <span class="nda-fill nda-fill-entity">England and Wales</span>
      governs, and the
      <span class="nda-blank">[governing law]</span>
      is yet to be chosen.
    </p>
  </article>
`;

async function main() {
  // Source guards first — fast, no browser; fail early if the contract regressed.
  runSourceGuards();

  const browser = await chromium.launch();
  const page = await browser.newPage();
  try {
    // Minimal globals the editor borrows from the app's utility bridge.
    await page.setContent(`
      <div id="generatorDocumentRender"></div>
      <div id="draftIntakePreview"></div>
    `);
    await page.evaluate(() => {
      // Match the app's global escapeHtml contract (used by the editor directly).
      window.escapeHtml = (value) =>
        String(value == null ? "" : value)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
      globalThis.escapeHtml = window.escapeHtml;
      // normalizeRun is provided by review-workstation-format.js in the app; the
      // editor only uses it during text-edit retiling, not the render path under
      // test, so a faithful pass-through keeping known format keys is enough.
      globalThis.normalizeRun = (run) => {
        const out = { text: String((run && run.text) || "") };
        if (run && run.bold) out.bold = true;
        if (run && run.italic) out.italic = true;
        if (run && run.underline) out.underline = true;
        return out;
      };
    });

    // The editor reads a free `state` binding that the app declares at classic-
    // script top level (the shared global lexical environment). Mirror that here
    // with an empty object the editor's showDraft Object.assign()s into.
    await page.addScriptTag({ content: "const state = {};" });
    await page.addScriptTag({ content: REDLINE_SRC });
    await page.addScriptTag({ content: EDITOR_SRC });

    // Feed the editor the preview fragment via its real showDraft entrypoint, then
    // read back what it rendered into the always-visible editor container.
    const result = await page.evaluate((previewHtml) => {
      const preview = document.getElementById("draftIntakePreview");
      preview.innerHTML = previewHtml;
      window.generatorEditor.showDraft(preview);
      const render = document.getElementById("generatorDocumentRender");
      const snapshot = window.generatorEditor.edits();
      const runs = [].concat(...(snapshot.paragraphs || []).map((p) => p.runs || []));
      return {
        html: render.innerHTML,
        hasEdits: window.generatorEditor.hasEdits(),
        runFlags: runs.map((r) => ({ text: r.text, fill: Boolean(r.fill), blank: Boolean(r.blank) })),
      };
    }, PREVIEW_HTML);

    // 1. The editor preserves the violet filled-value highlight. The shared
    //    renderFormattedRun maps a `fill` run to `.nda-fill-entity`, so a filled
    //    value carries the violet class through to the editor.
    assert.ok(
      /class="nda-fill-entity"[^>]*>England and Wales</.test(result.html)
        || /nda-fill-entity[\s\S]*England and Wales/.test(result.html),
      `editor lost the violet fill highlight on the filled value:\n${result.html}`,
    );
    assert.ok(result.html.includes("England and Wales"), "filled value text missing from editor");

    // 2. The editor preserves the amber placeholder highlight: the `.nda-blank`
    //    span survives instead of being flattened to plain text.
    assert.ok(
      /class="nda-blank"[^>]*>\[governing law\]</.test(result.html)
        || /nda-blank[\s\S]*\[governing law\]/.test(result.html),
      `editor flattened the amber placeholder (expected .nda-blank):\n${result.html}`,
    );

    // 3. The markers live in the editor's run model (proving the colours are
    //    carried, not flattened): the filled run is tagged `fill`, the placeholder
    //    run is tagged `blank`.
    const filled = result.runFlags.find((r) => r.text.includes("England and Wales"));
    const blank = result.runFlags.find((r) => r.text.includes("[governing law]"));
    assert.ok(filled && filled.fill, "filled run should carry the render-only `fill` marker");
    assert.ok(blank && blank.blank, "placeholder run should carry the render-only `blank` marker");

    // 4. Neither marker is a real edit: a freshly mirrored draft has nothing to
    //    export, so the render-only highlights never become run formatting / a
    //    redline and so can never reach the generated/exported DOCX.
    assert.equal(result.hasEdits, false, "mirroring a draft must not register exportable edits");

    console.log("generator-visuals.cjs PASS");
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
