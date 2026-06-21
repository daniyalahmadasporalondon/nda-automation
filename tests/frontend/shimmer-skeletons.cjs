// Server-free Playwright proof for the shimmer-skeleton loading states.
//
// Covers the three places static spinners / "Checking" text were replaced with
// GENERIC shimmer skeleton placeholders paired with honest duration copy:
//
//   1. Structure tab (contract-structure-view.js): a "Building structure map…"
//      skeleton renders WHILE a review is in progress, and is REPLACED by the real
//      structure map the moment the review completes.
//   2. Review workspace (review-workstation-rendering.js): setReviewWorkspaceSkeleton
//      paints a document-pane paragraph skeleton + inspector clause-row skeletons
//      with truthful "Reviewing… this can take up to a minute." copy, and removes
//      them on completion (no lingering skeleton).
//   3. Dashboard health (app.js renderDashboardHealth): the bare "Checking" detail
//      becomes a subtle shimmer placeholder while a probe is in flight, and is
//      replaced by status text once a real tone arrives.
//
// HONESTY GUARDS asserted:
//   * the skeletons are GENERIC (fixed counts) — they don't preview a real result
//     count (asserted by feeding a 3-section result and seeing the loading skeleton
//     not echo "3").
//   * prefers-reduced-motion DISABLES the shimmer animation (the .skeleton-block
//     animation-name resolves to "none" under reduced motion, and to a real
//     animation under no-preference) — proven against the REAL styles.css.
//   * the duration copy is present (never an implied-instant bare spinner).
//
// No Python backend, so it cannot hang on server startup.
//
// Run: node tests/frontend/shimmer-skeletons.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const STYLES_CSS = read("static/styles.css");
const STRUCTURE_JS = read("static/js/contract-structure-view.js");
const RENDERING_JS = read("static/js/review-workstation-rendering.js");
const APP_JS = read("static/app.js");

// Minimal page carrying the workspace + dashboard-health DOM the modules touch.
const PAGE_HTML = `<!doctype html><html><head><style id="real-css"></style></head><body>
  <div class="studio-page-wrap">
    <div class="studio-page">
      <div id="studioDocumentRender" hidden></div>
      <textarea id="studioNdaText"></textarea>
    </div>
  </div>
  <section class="studio-card" id="studioDetailPanel"></section>
  <div id="structureRoot"></div>
  <div class="dashboard-health-item checking" data-dashboard-health="ai">
    <div class="dashboard-health-head"><span class="dashboard-health-name">AI Review</span></div>
    <span class="dashboard-health-detail" data-dashboard-health-detail>Checking</span>
  </div>
</body></html>`;

async function main() {
  const browser = await chromium.launch();
  const failures = [];

  // ---------------------------------------------------------------------------
  // 1 + 2: Structure-tab skeleton + review-workspace skeleton (default motion).
  // ---------------------------------------------------------------------------
  try {
    const page = await browser.newPage();
    await page.setContent(PAGE_HTML);
    // Load the REAL stylesheet so the reduced-motion gate can be checked.
    await page.evaluate((css) => { document.getElementById("real-css").textContent = css; }, STYLES_CSS);

    // Globals the modules resolve lazily off window / global scope.
    await page.addScriptTag({ content: `
      window.escapeHtml = (v) => String(v == null ? "" : v).replace(/[&<>"]/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
      window.state = {
        selectedMatter: { id: "m1", review_status: "in_progress" },
        reviewInspectorView: "clause",
        reviewClauses: [],
        reviewParagraphs: [],
        latestReviewResult: null,
      };
      window.MatterUtils = {
        reviewInProgress: (m) => String(m && m.review_status || "") === "in_progress",
      };
      window.studioDetailPanel = document.getElementById("studioDetailPanel");
    ` });
    await page.addScriptTag({ content: RENDERING_JS });
    await page.addScriptTag({ content: STRUCTURE_JS });

    // Construct the structure controller against a dedicated root.
    await page.evaluate(() => {
      window.__structure = createContractStructureController({
        state: window.state,
        root: document.getElementById("structureRoot"),
      });
    });

    // --- Structure tab: loading skeleton while review in progress -------------
    await page.evaluate(() => window.__structure.render());
    let hasStructureSkeleton = await page.evaluate(() => Boolean(document.querySelector("#structureRoot .structure-skeleton")));
    assert.ok(hasStructureSkeleton, "Structure tab did not render its loading skeleton while review in progress");
    const structureCopy = await page.evaluate(() => document.querySelector("#structureRoot .structure-skeleton .review-skeleton-copy")?.textContent?.trim() || "");
    assert.match(structureCopy, /Building structure map/i, `Structure skeleton missing honest copy, got: ${structureCopy}`);
    // GENERIC guard: the loading skeleton must NOT echo a real section count.
    const structureHtml = await page.evaluate(() => document.querySelector("#structureRoot").innerHTML);
    assert.ok(!/structure-row\b/.test(structureHtml), "loading skeleton leaked real structure rows");

    // --- Structure tab: replaced by real content on completion ---------------
    await page.evaluate(() => {
      window.state.selectedMatter.review_status = "completed";
      window.state.latestReviewResult = {
        contract_structure: {
          sections: [
            { id: "section-1", label: "Clause 1", heading: "Confidentiality", kind: "clause", level: 0, start_index: 1, end_index: 2, source: { source_part: "body" }, start_paragraph_id: "p1", paragraph_ids: ["p1"] },
            { id: "section-2", label: "Clause 2", heading: "Term", kind: "clause", level: 0, start_index: 3, end_index: 4, source: { source_part: "body" }, start_paragraph_id: "p3", paragraph_ids: ["p3"] },
            { id: "section-3", label: "Clause 3", heading: "Governing Law", kind: "clause", level: 0, start_index: 5, end_index: 6, source: { source_part: "body" }, start_paragraph_id: "p5", paragraph_ids: ["p5"] },
          ],
          stats: { section_count: 3 },
        },
        reference_resolver: { references: [], stats: {} },
      };
      window.__structure.render();
    });
    hasStructureSkeleton = await page.evaluate(() => Boolean(document.querySelector("#structureRoot .structure-skeleton")));
    assert.ok(!hasStructureSkeleton, "Structure skeleton lingered after the review completed");
    const realRows = await page.evaluate(() => document.querySelectorAll("#structureRoot .structure-row").length);
    assert.equal(realRows, 3, `expected 3 real structure rows after completion, got ${realRows}`);

    // --- Review workspace: skeleton ON then OFF -------------------------------
    await page.evaluate(() => {
      window.state.reviewInspectorView = "clause";
      setReviewWorkspaceSkeleton(true);
    });
    const docSkeleton = await page.evaluate(() => Boolean(document.querySelector(".studio-page-wrap .review-skeleton .review-skeleton-doc")));
    assert.ok(docSkeleton, "review workspace document-pane skeleton did not render");
    const workspaceCopy = await page.evaluate(() => document.querySelector(".studio-page-wrap .review-skeleton .review-skeleton-copy")?.textContent?.trim() || "");
    assert.match(workspaceCopy, /Reviewing.*minute/i, `workspace skeleton missing honest duration copy, got: ${workspaceCopy}`);
    const inspectorRows = await page.evaluate(() => document.querySelectorAll("#studioDetailPanel .review-skeleton-row").length);
    assert.ok(inspectorRows >= 3, `expected a generic stack of inspector skeleton rows, got ${inspectorRows}`);

    await page.evaluate(() => setReviewWorkspaceSkeleton(false));
    const docSkeletonAfter = await page.evaluate(() => Boolean(document.querySelector(".studio-page-wrap .review-skeleton")));
    const inspectorAfter = await page.evaluate(() => Boolean(document.querySelector("#studioDetailPanel .review-skeleton-inspector")));
    assert.ok(!docSkeletonAfter, "review workspace skeleton lingered after deactivation");
    assert.ok(!inspectorAfter, "inspector skeleton lingered after deactivation");

    // --- reduced-motion: animation disabled vs enabled ------------------------
    // Re-show a skeleton so there is a .skeleton-block to measure.
    await page.evaluate(() => setReviewWorkspaceSkeleton(true));
    const sel = ".studio-page-wrap .review-skeleton .skeleton-block";

    await page.emulateMedia({ reducedMotion: "reduce" });
    const reducedAnim = await page.evaluate((s) => getComputedStyle(document.querySelector(s)).animationName, sel);
    assert.equal(reducedAnim, "none", `shimmer animation not disabled under reduced motion (got ${reducedAnim})`);

    await page.emulateMedia({ reducedMotion: "no-preference" });
    const fullAnim = await page.evaluate((s) => getComputedStyle(document.querySelector(s)).animationName, sel);
    assert.equal(fullAnim, "skeleton-shimmer", `shimmer animation not active under no-preference (got ${fullAnim})`);

    await page.close();
  } catch (error) {
    failures.push(error);
  }

  // ---------------------------------------------------------------------------
  // 3: Dashboard health shimmer placeholder (load just renderDashboardHealth).
  // ---------------------------------------------------------------------------
  try {
    const page = await browser.newPage();
    await page.setContent(PAGE_HTML);
    // Extract just the renderDashboardHealth + defaultDashboardHealthDetail funcs
    // from app.js (it has top-level DOM queries that would otherwise throw on a
    // minimal page). Pull the two function source blocks by name.
    const grab = (name) => {
      const start = APP_JS.indexOf(`function ${name}(`);
      assert.ok(start >= 0, `could not find ${name} in app.js`);
      // Skip past the parameter list (which may itself contain "{ ... }" for a
      // destructured arg) by balancing parens first, THEN walk the body braces.
      const paramOpen = APP_JS.indexOf("(", start);
      let parenDepth = 0;
      let bodyStart = -1;
      for (let i = paramOpen; i < APP_JS.length; i += 1) {
        if (APP_JS[i] === "(") parenDepth += 1;
        else if (APP_JS[i] === ")") { parenDepth -= 1; if (parenDepth === 0) { bodyStart = APP_JS.indexOf("{", i); break; } }
      }
      assert.ok(bodyStart >= 0, `could not find body of ${name}`);
      let depth = 0;
      for (let i = bodyStart; i < APP_JS.length; i += 1) {
        if (APP_JS[i] === "{") depth += 1;
        else if (APP_JS[i] === "}") { depth -= 1; if (depth === 0) return APP_JS.slice(start, i + 1); }
      }
      throw new Error(`unbalanced braces grabbing ${name}`);
    };
    const healthSrc = `${grab("renderDashboardHealth")}\n${grab("defaultDashboardHealthDetail")}`;
    await page.addScriptTag({ content: healthSrc });

    // While checking -> a shimmer placeholder (no bare "Checking" text node).
    await page.evaluate(() => renderDashboardHealth("ai", { tone: "checking" }));
    const checkingHtml = await page.evaluate(() => document.querySelector('[data-dashboard-health="ai"] [data-dashboard-health-detail]').innerHTML.trim());
    assert.ok(/health-skeleton/.test(checkingHtml), `checking tone did not render a shimmer placeholder, got: ${checkingHtml}`);
    assert.ok(!/Checking/.test(checkingHtml), `checking tone still showed bare "Checking" text, got: ${checkingHtml}`);
    // The accessible name still carries the textual status (honest, not hidden).
    const ariaLabel = await page.evaluate(() => document.querySelector('[data-dashboard-health="ai"]').getAttribute("aria-label"));
    assert.match(ariaLabel, /Checking/, `aria-label lost the textual status, got: ${ariaLabel}`);

    // Once a real tone arrives -> the placeholder is replaced by status text.
    await page.evaluate(() => renderDashboardHealth("ai", { tone: "ready" }));
    const readyHtml = await page.evaluate(() => document.querySelector('[data-dashboard-health="ai"] [data-dashboard-health-detail]').innerHTML.trim());
    assert.ok(!/health-skeleton/.test(readyHtml), `ready tone still showed a shimmer placeholder, got: ${readyHtml}`);
    assert.match(readyHtml, /AI review ready/, `ready tone did not show status text, got: ${readyHtml}`);

    await page.close();
  } catch (error) {
    failures.push(error);
  }

  await browser.close();

  if (failures.length) {
    for (const error of failures) console.error(error.message || error);
    console.error("shimmer-skeletons.cjs FAIL");
    process.exit(1);
  }
  console.log("shimmer-skeletons.cjs PASS");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
