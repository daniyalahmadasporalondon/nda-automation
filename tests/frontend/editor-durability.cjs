// Server-free Playwright checks for the editor formatting-durability / data-loss
// fixes on branch fix/editor-durability:
//
//   (a) BUG 1+4 — a live clause-DETECTION refresh must MERGE its structure/clause
//       tags onto the EXISTING (user-edited) paragraphs instead of replacing the
//       run-bearing model. An edit's text/runs/format MUST survive a subsequent
//       detection refresh while the clause/structure mapping still updates.
//       Exercises the real mergeViewerDetectionParagraphs() from the viewer module.
//
//   (b) BUG 2 — a format-only edit (format_paragraph + format_ops) must survive Save
//       Draft + rehydrate. Exercises the real applyDraftManualRedlines() +
//       replayFormatOpsOntoParagraph() from the rendering module: paragraph-scope
//       ops (alignment/font/size) AND run-scope ops (bold over a range) replay back
//       onto the paragraph.
//
//   (c) BUG 3 — a generator edit must persist across reload and feed Send/Download.
//       Loads the REAL generator-editor module, stubs fetch, edits a paragraph,
//       asserts the edit is POSTed to the matter redline-draft (durable) and that a
//       fresh load() of the same matter rehydrates the edit + exportRedlines()/
//       hasEdits() still surface it for the clean Send/Download export.
//
// Run: node tests/frontend/editor-durability.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

// Pull a single named function body out of a source file by walking braces.
function extractFunction(source, name, file) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start === -1) throw new Error(`could not locate ${name} in ${file}`);
  let depth = 0;
  let i = source.indexOf("{", start);
  for (; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return source.slice(start, i + 1);
}

const VIEWER_JS = read("static/js/review-workstation-viewer.js");
const RENDERING_JS = read("static/js/review-workstation-rendering.js");
const GENERATOR_JS = read("static/js/generator-editor.js");

// The const array the merge depends on lives just above the function.
const STRUCTURE_KEYS_DECL = (() => {
  const marker = "const VIEWER_DETECTION_STRUCTURE_KEYS";
  const start = VIEWER_JS.indexOf(marker);
  if (start === -1) throw new Error("could not locate VIEWER_DETECTION_STRUCTURE_KEYS");
  const end = VIEWER_JS.indexOf("];", start) + 2;
  return VIEWER_JS.slice(start, end);
})();

const MERGE_FN = extractFunction(VIEWER_JS, "mergeViewerDetectionParagraphs", "viewer.js");

const REPLAY_FN = extractFunction(RENDERING_JS, "replayFormatOpsOntoParagraph", "rendering.js");
const CHARPROPS_FN = extractFunction(RENDERING_JS, "runCharPropertiesForReplay", "rendering.js");
const RUNSFROM_FN = extractFunction(RENDERING_JS, "runsFromCharProperties", "rendering.js");
const SNAPSHOT_FN = extractFunction(RENDERING_JS, "snapshotReviewParagraphs", "rendering.js");
const APPLY_DRAFT_FN = extractFunction(RENDERING_JS, "applyDraftManualRedlines", "rendering.js");

// HTML scaffold the generator editor module reaches for at module-eval time.
const PAGE_HTML = `<!doctype html><html><body>
  <div id="generatorEditor"><div id="generatorDocumentRender"></div></div>
  <div id="draftIntakePreview"></div>
</body></html>`;

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

    // ============================================================
    // (a) BUG 1+4 — detection refresh MERGE preserves edited model
    // ============================================================
    await page.addScriptTag({ content: STRUCTURE_KEYS_DECL });
    await page.addScriptTag({ content: MERGE_FN });

    await check("detection MERGE preserves edited text + runs + alignment while updating clause tags", async () => {
      const result = await page.evaluate(() => {
        // Existing paragraphs carry the user's EDITS (bold runs, alignment, edited text).
        const existing = [
          {
            id: "p1", index: 1, source_index: 0,
            text: "Edited confidentiality clause.",
            runs: [{ text: "Edited ", bold: true }, { text: "confidentiality clause." }],
            alignment: "center", font: "Arial", fontSize: 12,
            clause_id: "old_clause",
          },
          {
            id: "p2", index: 2, source_index: 1,
            text: "Second clause untouched.",
          },
        ];
        // Detection result: SAME identities, but it carries fresh structure/clause
        // tags AND its own (un-edited / plain) text + no runs. The merge must keep the
        // existing run-bearing model and ONLY take the detector's tags.
        const detected = [
          {
            id: "p1", index: 1, source_index: 0,
            text: "DETECTION PLAIN TEXT THAT MUST NOT WIN",
            clause_id: "confidentiality", heading_level: 2, structure_label: "1.",
            structure_number: "1",
          },
          {
            id: "p2", index: 2, source_index: 1,
            text: "Second clause untouched.",
            clause_id: "term", structure_label: "2.",
          },
        ];
        return mergeViewerDetectionParagraphs(existing, detected);
      });
      // Run-bearing model PRESERVED:
      assert.equal(result[0].text, "Edited confidentiality clause.", "edited text must survive the refresh");
      assert.deepEqual(
        result[0].runs,
        [{ text: "Edited ", bold: true }, { text: "confidentiality clause." }],
        "edited runs must survive the refresh",
      );
      assert.equal(result[0].alignment, "center", "alignment preserved");
      assert.equal(result[0].font, "Arial", "font preserved");
      assert.equal(result[0].fontSize, 12, "fontSize preserved");
      // Clause/structure TAGS updated (detection's purpose):
      assert.equal(result[0].clause_id, "confidentiality", "clause tag must update from detection");
      assert.equal(result[0].heading_level, 2, "structure tag must update from detection");
      assert.equal(result[0].structure_label, "1.", "structure label updated");
      assert.equal(result[1].clause_id, "term", "second paragraph clause tag updated");
    });

    await check("detection MERGE keys on unique id/index, not non-unique source_index", async () => {
      const result = await page.evaluate(() => {
        // Two split blocks SHARE source_index 5 (non-unique provenance) but have unique
        // ids/indexes. The merge must map by id/index so each keeps its OWN edited text.
        const existing = [
          { id: "p1", index: 1, source_index: 5, text: "EDIT A", runs: [{ text: "EDIT A", bold: true }] },
          { id: "p2", index: 2, source_index: 5, text: "EDIT B" },
        ];
        const detected = [
          { id: "p1", index: 1, source_index: 5, text: "plain a", clause_id: "c1" },
          { id: "p2", index: 2, source_index: 5, text: "plain b", clause_id: "c2" },
        ];
        return mergeViewerDetectionParagraphs(existing, detected);
      });
      assert.equal(result[0].text, "EDIT A", "split block A keeps its own edit (not cross-attached)");
      assert.equal(result[1].text, "EDIT B", "split block B keeps its own edit");
      assert.equal(result[0].clause_id, "c1");
      assert.equal(result[1].clause_id, "c2");
      assert.deepEqual(result[0].runs, [{ text: "EDIT A", bold: true }], "runs not cross-attached");
    });

    await check("detection MERGE adopts detector-added paragraphs (clause anchoring stays whole)", async () => {
      const result = await page.evaluate(() => {
        const existing = [{ id: "p1", index: 1, text: "Kept." }];
        const detected = [
          { id: "p1", index: 1, text: "plain", clause_id: "c1" },
          { id: "p9", index: 9, text: "New detector paragraph.", clause_id: "c9" },
        ];
        return mergeViewerDetectionParagraphs(existing, detected);
      });
      assert.equal(result.length, 2, "detector-added paragraph is adopted");
      assert.equal(result[1].id, "p9");
      assert.equal(result[1].text, "New detector paragraph.", "added paragraph takes detection copy");
      assert.equal(result[1].clause_id, "c9");
    });

    // ============================================================
    // (b) BUG 2 — format_paragraph survives Save Draft + rehydrate
    // ============================================================
    const RENDER_BOOTSTRAP = `
      ${SNAPSHOT_FN}
      ${CHARPROPS_FN}
      ${RUNSFROM_FN}
      ${REPLAY_FN}
      ${APPLY_DRAFT_FN}
      // Globals the rendering helpers reach for.
      const REDLINE_DELETE_PARAGRAPH = "delete_paragraph";
      function redlineFormatParagraphAction() { return "format_paragraph"; }
      let __syncCalled = 0;
      function syncReviewSourceFromParagraphs() { __syncCalled += 1; }
      window.__getSyncCalled = () => __syncCalled;
      window.__applyDraftManualRedlines = applyDraftManualRedlines;
      window.__snapshot = snapshotReviewParagraphs;
    `;
    await page.addScriptTag({ content: RENDER_BOOTSTRAP });

    await check("paragraph-scope format_ops (alignment/font/size) replay on rehydrate", async () => {
      const para = await page.evaluate(() => {
        window.state = {
          reviewParagraphs: [
            { id: "p1", index: 1, text: "Aligned clause.", alignment: "left", font: "Calibri", fontSize: 11 },
          ],
        };
        // Saved draft: a format-only edit recorded alignment->center, font->Arial, size->14.
        const manualRedlines = [{
          id: "manual-p1-fmt",
          action: "format_paragraph",
          paragraph_id: "p1",
          original_text: "Aligned clause.",
          replacement_text: "Aligned clause.",
          format_ops: [
            { scope: "paragraph", property: "alignment", from: "left", to: "center" },
            { scope: "paragraph", property: "font", from: "Calibri", to: "Arial" },
            { scope: "paragraph", property: "size", from: 11, to: 14 },
          ],
        }];
        window.__applyDraftManualRedlines(manualRedlines);
        return window.state.reviewParagraphs[0];
      });
      assert.equal(para.text, "Aligned clause.", "text unchanged on a format-only rehydrate");
      assert.equal(para.alignment, "center", "alignment format op replayed");
      assert.equal(para.font, "Arial", "font format op replayed");
      assert.equal(para.fontSize, 14, "size format op replayed");
    });

    await check("run-scope format_ops (bold over a char range) replay into the run model", async () => {
      const para = await page.evaluate(() => {
        window.state = {
          reviewParagraphs: [
            { id: "p2", index: 2, text: "Bold me." },
          ],
        };
        const manualRedlines = [{
          id: "manual-p2-fmt",
          action: "format_paragraph",
          paragraph_id: "p2",
          original_text: "Bold me.",
          replacement_text: "Bold me.",
          // Bold the first 4 chars ("Bold").
          format_ops: [
            { scope: "run", property: "bold", start: 0, end: 4, from: false, to: true },
          ],
        }];
        window.__applyDraftManualRedlines(manualRedlines);
        return window.state.reviewParagraphs[0];
      });
      assert.equal(para.text, "Bold me.", "text preserved");
      assert.ok(Array.isArray(para.runs) && para.runs.length >= 2, "runs rebuilt from the run op");
      assert.equal(para.runs.map((r) => r.text).join(""), "Bold me.", "runs tile the full text");
      assert.equal(para.runs[0].text, "Bold", "first run is the bolded range");
      assert.equal(para.runs[0].bold, true, "first run is bold");
      assert.ok(!para.runs[1].bold, "remaining run is not bold");
    });

    await check("format-only rehydrate does NOT wipe the paragraph to a text replace", async () => {
      // Regression guard for the original bug: applyDraftManualRedlines used to set
      // paragraph.text = replacement_text for EVERY redline, which for a format_paragraph
      // redline still set text (fine) but DROPPED format_ops -> formatting lost. Assert
      // the formatting is now applied (i.e. the format branch was taken).
      const para = await page.evaluate(() => {
        window.state = { reviewParagraphs: [{ id: "p3", index: 3, text: "Keep me.", alignment: "left" }] };
        window.__applyDraftManualRedlines([{
          action: "format_paragraph", paragraph_id: "p3",
          replacement_text: "Keep me.",
          format_ops: [{ scope: "paragraph", property: "alignment", from: "left", to: "right" }],
        }]);
        return window.state.reviewParagraphs[0];
      });
      assert.equal(para.alignment, "right", "format op applied (not silently dropped)");
    });

    // ============================================================
    // (c) BUG 3 — generator edits persist across reload + feed Send/Download
    // ============================================================
    // Reset the page so the generator module evaluates against a clean global scope.
    await page.setContent(PAGE_HTML);

    // fetch stub: a tiny in-memory matter "server" that serves /review and accepts
    // /redline-draft saves, so a real load() -> edit -> save -> reload round-trips.
    const FETCH_STUB = `
      window.__matters = {
        "m1": {
          review_result: {
            paragraphs: [
              { id: "g1", index: 1, text: "Generated clause one.", alignment: "left" },
              { id: "g2", index: 2, text: "Generated clause two." },
            ],
          },
          redline_draft: null,
        },
      };
      window.__saves = [];
      window.fetch = async (url, opts) => {
        const u = String(url);
        if (/\\/review$/.test(u)) {
          const id = u.match(/matters\\/([^/]+)\\/review/)[1];
          return { ok: true, json: async () => JSON.parse(JSON.stringify(window.__matters[id])) };
        }
        if (/\\/redline-draft$/.test(u)) {
          const id = u.match(/matters\\/([^/]+)\\/redline-draft/)[1];
          const body = JSON.parse(opts.body);
          window.__saves.push({ id, draft: body.redline_draft });
          // Persist onto the in-memory matter so a reload sees it (server round-trip).
          window.__matters[id].redline_draft = body.redline_draft;
          return { ok: true, json: async () => ({ matter: { id } }) };
        }
        if (/export-review-docx/.test(u)) {
          window.__exportBody = JSON.parse(opts.body);
          return { ok: true, blob: async () => new Blob(["docx"]) };
        }
        return { ok: false, json: async () => ({}) };
      };
      window.state = {};
      // Minimal globals the generator module reaches for at edit/export time.
      window.REDLINE_DELETE_PARAGRAPH = "delete_paragraph";
      window.REDLINE_REPLACE_PARAGRAPH = "replace_paragraph";
      window.REDLINE_INSERT_AFTER_PARAGRAPH = "insert_after_paragraph";
      window.manualParagraphRedline = function () { return null; };
      window.DEFAULT_FONT_SIZE = 11;
      window.escapeHtml = function (value) {
        return String(value)
          .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
      };
      // Shared structure/format render helpers (from redline-rendering.js) the
      // generator's renderParagraph reaches for. Stubbed to no-op output: this test
      // exercises the durability data path, not DOM rendering fidelity.
      window.paragraphFormatStyleAttribute = function () { return ""; };
      window.paragraphStructureClasses = function () { return []; };
      window.paragraphStructureAttributes = function () { return ""; };
    `;
    await page.addScriptTag({ content: FETCH_STUB });
    await page.addScriptTag({ content: GENERATOR_JS });

    await check("a generator edit is POSTed to the matter redline-draft (durable)", async () => {
      await page.evaluate(async () => {
        await window.generatorEditor.load("m1");
        // Simulate an in-place text edit on g1 + run the same persistence path a
        // keystroke takes: mutate the paragraph then markTouched via a Find&Replace
        // apply (which calls markTouched internally).
        window.generatorEditor._applyFindReplace(
          window.state.generatorParagraphs.find((p) => p.id === "g1"),
          "Generated clause one EDITED.",
          "Generated clause one.",
        );
      });
      // Wait for the debounced save (600ms) to flush.
      await page.waitForFunction(() => window.__saves.length > 0, { timeout: 3000 });
      const saved = await page.evaluate(() => window.__saves[window.__saves.length - 1]);
      assert.equal(saved.id, "m1", "save targets the matter");
      assert.ok(Array.isArray(saved.draft.manual_redline_edits), "draft carries manual_redline_edits");
      const edit = saved.draft.manual_redline_edits.find((e) => e.paragraph_id === "g1");
      assert.ok(edit, "the edited paragraph has a manual redline");
      assert.equal(edit.replacement_text, "Generated clause one EDITED.", "edit text persisted");
    });

    await check("reloading the matter REHYDRATES the generator edit", async () => {
      const text = await page.evaluate(async () => {
        // Fresh load of the SAME matter — the in-memory server now holds the saved draft.
        await window.generatorEditor.clear();
        await window.generatorEditor.load("m1");
        return window.state.generatorParagraphs.find((p) => p.id === "g1").text;
      });
      assert.equal(text, "Generated clause one EDITED.", "edit restored on reload (was lost before the fix)");
    });

    await check("rehydrated edit still feeds Send/Download (hasEdits + exportRedlines)", async () => {
      const result = await page.evaluate(async () => {
        // The reloaded editor (with rehydrated edit) must report edits and export them.
        const has = window.generatorEditor.hasEdits();
        const blob = await window.generatorEditor.exportCleanDocx();
        return { has, exported: window.__exportBody };
      });
      assert.equal(result.has, true, "hasEdits() true after rehydrate");
      assert.ok(result.exported, "exportCleanDocx POSTed to export endpoint");
      const edit = (result.exported.manual_redline_edits || []).find((e) => e.paragraph_id === "g1");
      assert.ok(edit, "export carries the rehydrated edit");
      assert.equal(edit.replacement_text, "Generated clause one EDITED.", "Send/Download uses the edited text");
      assert.equal(result.exported.clean, true, "export is the clean (edits-baked-in) variant");
    });

  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error(`\n${failures.length} check(s) failed.`);
    process.exit(1);
  }
  console.log("\nAll editor-durability checks passed.");
}

main().catch((error) => { console.error(error); process.exit(1); });
