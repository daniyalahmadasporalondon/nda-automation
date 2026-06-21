// Server-free Playwright check for editor Find & Replace (static/js/find-replace.js).
// It loads the REAL find-replace module plus the shared run helpers it depends on
// (charDiffOperations from redline-rendering.js; mergeAdjacentRuns/normalizeRun from
// review-workstation-format.js) into a minimal page, registers a fake editor adapter
// holding a multi-paragraph doc whose paragraphs carry inline run formatting, then
// drives the panel: Replace-all "Discloser" -> "Disclosing Party" and asserts EVERY
// occurrence changed AND the bold/italic run formatting on the surrounding text
// survived the re-tile. No Python backend, so it cannot hang on server startup.
//
// Run: node tests/frontend/find-replace.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const RENDERING_JS = read("static/js/redline-rendering.js");
const FORMAT_JS = read("static/js/review-workstation-format.js");
const FIND_REPLACE_JS = read("static/js/find-replace.js");

const PAGE_HTML = `<!doctype html><html><body>
  <div id="editorHost" data-view="generator">
    <div id="renderEl"></div>
  </div>
</body></html>`;

// Fake adapter: a 3-paragraph doc. Two paragraphs mention "Discloser" with
// surrounding bold/italic runs; one does not. Runs tile each paragraph's text so the
// formatting-preserving retile path is exercised on replace.
const BOOTSTRAP = `
  window.__doc = [
    {
      id: "p1",
      // "The " (bold) + "Discloser" (plain) + " shall protect." (italic)
      text: "The Discloser shall protect.",
      runs: [
        { text: "The ", bold: true },
        { text: "Discloser" },
        { text: " shall protect.", italic: true },
      ],
    },
    {
      id: "p2",
      // Two occurrences, plain text (no runs) — covers the plain-paragraph path.
      text: "Discloser and Discloser agree.",
    },
    {
      id: "p3",
      text: "The Recipient acknowledges receipt.",
      runs: [{ text: "The Recipient acknowledges receipt.", bold: true }],
    },
  ];
  window.__renderCalls = 0;
  window.__batchCalls = 0;
  window.findReplace.register("generator", {
    paragraphs: () => window.__doc,
    getRenderEl: () => document.getElementById("renderEl"),
    getPanelHost: () => document.getElementById("editorHost"),
    applyReplacement: function (paragraph, newText, oldText) {
      var retiled = window.findReplace._retileRunsForReplace(paragraph.runs, oldText, newText);
      if (retiled) paragraph.runs = retiled;
      else if (Array.isArray(paragraph.runs)) {
        // Mirror the editors: a free-form replace that preserves no formatting drops
        // runs so the paragraph renders as a single clean run.
        var runsWereValid = paragraph.runs.map(function (r) { return String((r && r.text) || ""); }).join("") === oldText;
        var runsFormatted = paragraph.runs.some(function (r) { return r && (r.bold || r.italic); });
        if (!(runsWereValid && runsFormatted)) delete paragraph.runs;
      }
      paragraph.text = newText;
    },
    afterBatch: function () { window.__renderCalls += 1; window.__batchCalls += 1; },
  });
`;

function snapshot(page) {
  return page.evaluate(() => window.__doc.map((p) => ({ id: p.id, text: p.text, runs: p.runs || null })));
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
    await page.addScriptTag({ content: RENDERING_JS });
    await page.addScriptTag({ content: FORMAT_JS });
    await page.addScriptTag({ content: FIND_REPLACE_JS });
    await page.addScriptTag({ content: BOOTSTRAP });

    // ---- Open the panel + assert it mounted over the editor host -----------
    await page.evaluate(() => window.findReplace.open("generator"));
    await check("panel opens and is anchored under the editor host", async () => {
      const mounted = await page.evaluate(() => {
        const panel = document.getElementById("findReplacePanel");
        return Boolean(panel) && !panel.hidden && panel.closest("#editorHost") !== null;
      });
      assert.equal(mounted, true);
    });

    // ---- Match count reflects all occurrences -------------------------------
    await check("match count = 3 occurrences of Discloser", async () => {
      await page.fill("#frFind", "Discloser");
      const status = await page.textContent("#frStatus");
      assert.match(status, /3 matches/);
      const found = await page.evaluate(() =>
        window.findReplace._findMatches(
          { paragraphs: () => window.__doc },
          "Discloser",
          true,
        ).length);
      assert.equal(found, 3);
    });

    // ---- Replace all --------------------------------------------------------
    await check("Replace all swaps every Discloser -> Disclosing Party", async () => {
      await page.fill("#frReplace", "Disclosing Party");
      await page.click("#frReplaceAll");

      const doc = await snapshot(page);
      const joined = doc.map((p) => p.text).join(" | ");
      assert.ok(!/Discloser\b/.test(joined), "no Discloser should remain: " + joined);
      // 3 originals -> 3 replacements.
      const count = (joined.match(/Disclosing Party/g) || []).length;
      assert.equal(count, 3, "expected 3 replacements, got " + count + " in: " + joined);

      // p2 had two occurrences in one paragraph; both replaced.
      const p2 = doc.find((p) => p.id === "p2");
      assert.equal(p2.text, "Disclosing Party and Disclosing Party agree.");
    });

    // ---- Surrounding run formatting survives the retile ---------------------
    await check("surrounding bold/italic runs survive the replacement", async () => {
      const doc = await snapshot(page);
      const p1 = doc.find((p) => p.id === "p1");
      // runs must still tile the new text exactly.
      assert.ok(Array.isArray(p1.runs) && p1.runs.length, "p1 should keep runs");
      const joined = p1.runs.map((r) => r.text).join("");
      assert.equal(joined, p1.text, "runs must tile new text");
      assert.equal(p1.text, "The Disclosing Party shall protect.");
      // The leading "The " stays bold; the trailing " shall protect." stays italic.
      const boldText = p1.runs.filter((r) => r.bold).map((r) => r.text).join("");
      const italicText = p1.runs.filter((r) => r.italic).map((r) => r.text).join("");
      assert.ok(boldText.includes("The "), "leading bold lost: " + JSON.stringify(p1.runs));
      assert.ok(italicText.includes("shall protect."), "trailing italic lost: " + JSON.stringify(p1.runs));
    });

    // ---- Batch hook fired once + further passes are clean -------------------
    await check("afterBatch ran for the replace-all pass and no matches remain", async () => {
      const batches = await page.evaluate(() => window.__batchCalls);
      assert.ok(batches >= 1, "afterBatch should have fired");
      // Re-querying for the old needle now yields nothing.
      await page.fill("#frFind", "Discloser");
      const status = await page.textContent("#frStatus");
      assert.match(status, /No matches/);
    });

    // ---- Replace-next walks single occurrences ------------------------------
    await check("Replace next swaps one Disclosing Party at a time", async () => {
      await page.fill("#frFind", "Disclosing Party");
      await page.fill("#frReplace", "DP");
      await page.click("#frReplaceNext");
      const doc = await snapshot(page);
      const joined = doc.map((p) => p.text).join(" | ");
      const remaining = (joined.match(/Disclosing Party/g) || []).length;
      const dp = (joined.match(/\bDP\b/g) || []).length;
      assert.equal(dp, 1, "exactly one replacement: " + joined);
      assert.equal(remaining, 2, "two left: " + joined);
    });

    // ---- Esc closes ---------------------------------------------------------
    await check("Esc closes the panel", async () => {
      await page.focus("#frFind");
      await page.keyboard.press("Escape");
      const hidden = await page.evaluate(() => document.getElementById("findReplacePanel").hidden);
      assert.equal(hidden, true);
    });

  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error("\nfind-replace.cjs FAILED: " + failures.length + " check(s)");
    process.exit(1);
  }
  console.log("\nfind-replace.cjs PASSED");
}

main().catch((error) => { console.error(error); process.exit(1); });
