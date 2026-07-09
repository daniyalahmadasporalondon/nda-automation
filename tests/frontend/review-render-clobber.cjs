// Server-free Playwright regression checks for the "late original render
// clobbers the displayed redline" race (Render prod symptom: run Review ->
// redlined document renders (pager 7/7, ins/del visible) -> viewer silently
// flips back to the ORIGINAL document (pager 4/5), no error shown).
//
// Mechanism under test:
//   1. renderResult() paints the redline view and calls
//      requestMatterDocumentRenderPreview(), which fetches
//      /api/matters/<id>/render-status. The backend rasterizes the ORIGINAL
//      source document (matter_render_job.py) -- seconds on a cold cache.
//   2. Meanwhile maybeUpgradeSurfaceToFaithfulDocx() swaps in the faithful
//      REDLINE surface (reviewed-docx tracked changes). The user is reading it.
//   3. The /render-status fetch resolves LATE with the ORIGINAL's page images;
//      pre-fix, its resolve handler repainted the pane
//      (renderStudioDocumentHighlights), destroying the faithful surface and
//      prepending the ORIGINAL's page tiles; viewer-controls.js then counted
//      those [data-review-render-page] tiles, flipping the pager to "/ 5".
//
// Contracts asserted (FIX A/B/C in review-workstation-rendering.js +
// viewer-controls.js):
//   FIX A: a late /render-status completion must NOT remove the displayed
//     faithful redline surface nor paint the ORIGINAL's page tiles over it
//     (page images are stored in state for the Original view; no repaint).
//   FIX B: the pager only counts [data-review-render-page] tiles when the
//     source-render surface is actually displayed -- a stray tile can never
//     repage a faithful (continuous-scroll) view.
//   FIX C: when attemptFaithfulRedlineFallback downgrades to a faithful
//     non-redline document, a PERSISTENT notice (not just the transient toast)
//     is painted in the viewer, with a retry affordance.
//
// Loads the REAL functions from static/js/review-workstation-rendering.js
// (brace-walk extraction, same trick as review-source-persist.cjs), the REAL
// viewer-controls.js, stubs the side-effect globals, and drives exactly the
// racing event order. No Python backend, so it cannot hang on server startup.
//
// Run: node tests/frontend/review-render-clobber.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const RENDERING_JS = read("static/js/review-workstation-rendering.js");
const VIEWER_CONTROLS_JS = read("static/js/viewer-controls.js");
const CONFIG_JS = read("static/js/config.js");

// Brace-walk extractor: pulls a single top-level `function name(...) {...}` out
// of the real module so we exercise real production code without the module's
// full global surface.
function extractFn(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start === -1) throw new Error(`could not locate function ${name}`);
  let i = source.indexOf("{", start);
  let depth = 0;
  for (; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return source.slice(start, i + 1);
}

// Optional variant: lets this test run (and FAIL its contracts) against a
// checkout that predates the fix, proving red -> green.
function extractFnOptional(source, name) {
  try {
    return extractFn(source, name);
  } catch (_error) {
    return "";
  }
}

const REAL_FNS = [
  "renderStudioDocumentHighlights",
  "requestMatterDocumentRenderPreview",
  "attemptFaithfulRedlineFallback",
  "renderPdfDocumentSurface",
  "pageImageSurfaceUsable",
  "normalizeReviewDocumentRender",
  "normalizeDocumentOverlay",
  "normalizeRenderPages",
  "normalizeRenderPage",
  "positiveInteger",
  "normalizedRenderStatus",
  "numericPageCount",
  "stringValue",
  "hasDocumentRenderPreview",
  "renderDocumentErrorMessage",
  "renderDocumentPageImage",
  "pageOverlayAnchors",
  "uniqueStrings",
].map((name) => extractFn(RENDERING_JS, name)).join("\n\n")
  + "\n\n"
  + [
    // Introduced by the fix; absent on pre-fix checkouts (red run).
    "faithfulDocxSurfaceActiveForCurrentView",
    "redlineFallbackReasonText",
  ].map((name) => extractFnOptional(RENDERING_JS, name)).join("\n\n");

// Mirrors the studio document pane + viewer toolbar (same skeleton as
// viewer-page-count.cjs). The wrap is a 300px viewport; original page tiles are
// 800px tall so the tile count is unambiguous; redline surfaces are 2000px tall
// (= 7 viewport slices, matching the live symptom's "/ 7").
const PAGE_HTML = `<!doctype html><html><head><style>
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
      <div class="studio-page">
        <div id="studioDocumentRender"></div>
      </div>
    </div>
  </div>
</main>
</body></html>`;

const BOOTSTRAP = `
  // Module-level sequence counter the extracted functions close over.
  let reviewDocumentRenderRequestSequence = 0;

  window.__calls = { upgradeKicks: 0, renderStatusFetches: 0, fallbackToasts: 0 };
  window.__renderStatusResolvers = [];

  window.state = {
    selectedMatter: { id: "m1", source_filename: "Moorwand - Mutual NDA - 2026 v1.0.docx" },
    documentViewMode: "redline",
    reviewClauses: [{ id: "term" }],
    reviewParagraphs: [{ id: "p1", index: 0, text: "Term paragraph." }],
    reviewOriginalParagraphs: [{ id: "p1", index: 0, text: "Term paragraph." }],
    reviewDocumentRender: null,
    selectedReviewClauseId: null,
    latestReviewResult: {},
    redlineDraftDirty: false,
    reviewComments: [],
  };
  window.studioDocumentRender = document.getElementById("studioDocumentRender");

  // --- minimal real-ish helpers -------------------------------------------
  window.escapeHtml = (value) => String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  window.joinClasses = (...parts) => parts.filter(Boolean).join(" ");
  window.reviewErrorFromPayload = (p, fallback) => new Error((p && p.error) || fallback);

  // --- side-effect stubs (no-ops; not under test) --------------------------
  window.notifyPdfMarkupLeaveOriginal = () => {};
  window.notifyPdfMarkupOriginalRendered = () => {};
  window.bindViewerParagraphEditing = () => {};
  window.bindParagraphCommentControls = () => {};
  window.applyCommentTextHighlights = () => {};
  window.notifyFillHighlights = () => {};
  window.highlightSelectedClauseRefs = () => {};
  window.selectReviewClause = () => {};
  window.showStudioSourceEditor = () => {};
  window.bindOriginalViewFallbackControls = () => {};
  window.renderOriginalDocumentSurface = () => "";
  window.maybeUpgradeOriginalSurfaceToFaithfulDocx = () => {};
  window.paintStudioDocumentRenderError = (error) => { window.__renderError = String(error); };
  window.showStudioDocumentRender = () => { window.studioDocumentRender.hidden = false; };
  window.currentReviewComments = () => [];
  window.manualRedlineBaselineParagraphs = () => window.state.reviewOriginalParagraphs;
  window.effectiveReviewRedlines = () => [];
  window.matterIsDocxSource = () => true;
  window.matterIsPdfSource = () => false;
  window.notifyRedlineFaithfulFallback = () => { window.__calls.fallbackToasts += 1; };
  window.faithfulMappingTelemetry = () => {};

  // The faithful upgrade is RECORDED but does not synchronously repaint (mirrors
  // the live async gap between the clobber and any faithful re-upgrade -- and the
  // cases where the upgrade cannot engage at all: docx-preview libs not loaded,
  // flag kill-switched, PDF source without working DOCX, or /reviewed-docx 409/500).
  window.maybeUpgradeSurfaceToFaithfulDocx = () => { window.__calls.upgradeKicks += 1; };

  // Faithful-render bridge stub for the FIX C fallback check: always paints.
  window.__faithfulStub = {
    render: async (host) => {
      host.innerHTML = '<div style="height:2000px">faithful original body</div>';
      return { ok: true };
    },
  };

  // The redline reconstruction floor: tall (2000px over a 300px viewport => 7
  // scroll slices, matching the user's "7 pages") with visible ins/del markup.
  window.renderReviewDocument = () => (
    '<div class="studio-document-render" style="height:2000px">' +
      '<div class="studio-doc-paragraph" data-paragraph-id="p1">' +
        'The Term is <del>five (5) years</del><ins>two (2) years</ins>.' +
      '</div>' +
    '</div>'
  );

  // /render-status: resolves ONLY when the test calls __resolveRenderStatus()
  // -- the backend rasterization of the ORIGINAL (5 pages) landing late.
  window.fetch = (url) => {
    if (String(url).includes("/render-status")) {
      window.__calls.renderStatusFetches += 1;
      return new Promise((resolve) => {
        window.__renderStatusResolvers.push(() => resolve({
          ok: true,
          json: async () => ({
            document_render: {
              status: "ready",
              source_label: "Converted DOCX",
              page_count: 5,
              pages: [1, 2, 3, 4, 5].map((n) => ({
                page_number: n,
                image_url: "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
                width: 850,
                height: 1100,
              })),
            },
          }),
        }));
      });
    }
    return Promise.reject(new Error("unexpected fetch " + url));
  };
  window.__resolveRenderStatus = () => {
    const resolvers = window.__renderStatusResolvers.splice(0);
    resolvers.forEach((fn) => fn());
    return resolvers.length;
  };
`;

async function indicator(page) {
  return page.evaluate(() => document.getElementById("studioPageIndicator").textContent.trim());
}

async function surfaceFacts(page) {
  return page.evaluate(() => {
    const host = window.studioDocumentRender;
    return {
      faithfulPresent: Boolean(host.querySelector("[data-faithful-docx]")),
      redlineMarkupPresent: Boolean(host.querySelector("ins, del")),
      originalTileCount: host.querySelectorAll("[data-review-render-page]").length,
      firstChildClass: host.firstElementChild ? host.firstElementChild.className : "",
      pdfSurfaceFirst: Boolean(host.firstElementChild && host.firstElementChild.matches("[data-review-pdf-surface]")),
      upgradeKicks: window.__calls.upgradeKicks,
      pagesStoredInState: Array.isArray(window.state.reviewDocumentRender?.pages)
        ? window.state.reviewDocumentRender.pages.length
        : 0,
    };
  });
}

// Insert the faithful REDLINE surface the way maybeUpgradeSurfaceToFaithfulDocx's
// success path does (review-workstation-rendering.js, faithful wrapper swap-in).
async function swapInFaithfulRedline(page) {
  await page.evaluate(() => {
    const wrapper = document.createElement("section");
    wrapper.className = "review-faithful-surface review-faithful-redline ready";
    wrapper.setAttribute("data-review-render-surface", "");
    wrapper.setAttribute("data-faithful-docx", "");
    wrapper.setAttribute("data-faithful-view-mode", "redline");
    wrapper.setAttribute("data-render-status", "ready");
    wrapper.innerHTML =
      '<div class="review-faithful-docx-surface" style="height:2000px">' +
      'The Term is <del>five (5) years</del><ins>two (2) years</ins>.</div>';
    window.studioDocumentRender.innerHTML = "";
    window.studioDocumentRender.appendChild(wrapper);
  });
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
    await page.addScriptTag({ content: REAL_FNS });
    await page.addScriptTag({ content: VIEWER_CONTROLS_JS });

    // --- Step 1: review completes -> redline paints + render-status kicked ---
    await page.evaluate(() => {
      renderStudioDocumentHighlights();       // paints the redline reconstruction
      requestMatterDocumentRenderPreview();   // kicks the async /render-status fetch (pending)
    });
    // Let viewer-controls' ResizeObserver recompute the pager off the new content.
    await page.waitForFunction(() => {
      const el = document.getElementById("studioPageIndicator");
      return el && /\/\s*7$/.test(el.textContent.trim());
    }, { timeout: 4000 }).catch(() => {});

    await check("baseline: redline is displayed with a redline page count (no original tiles)", async () => {
      const facts = await surfaceFacts(page);
      assert.equal(facts.redlineMarkupPresent, true, "redline ins/del must be visible after review");
      assert.equal(facts.originalTileCount, 0, "no original page tiles yet");
      const text = await indicator(page);
      assert.match(text, /\/\s*7$/, `expected the redline slice count '/ 7', got '${text}'`);
    });

    // --- Step 2: the faithful REDLINE surface swaps in (live success path) ---
    // The user is now looking at the tracked-changes redline document.
    await swapInFaithfulRedline(page);
    await page.waitForTimeout(80);

    // --- Step 3: the ORIGINAL's render-status completion lands LATE ----------
    await page.evaluate(() => window.__resolveRenderStatus());
    // Wait until the repaint + pager recompute have settled (either outcome).
    await page.waitForFunction(() => {
      const tiles = window.studioDocumentRender.querySelectorAll("[data-review-render-page]").length;
      const ind = document.getElementById("studioPageIndicator").textContent.trim();
      return tiles === 0 || /\/\s*5$/.test(ind);
    }, { timeout: 4000 }).catch(() => {});
    await page.waitForTimeout(120);

    // --- FIX A contracts ------------------------------------------------------
    await check("FIX A: late original render completion must NOT remove the displayed redline surface", async () => {
      const facts = await surfaceFacts(page);
      assert.equal(
        facts.faithfulPresent,
        true,
        `faithful redline surface was destroyed by the late /render-status repaint `
          + `(first child is now '${facts.firstChildClass}')`,
      );
    });

    await check("FIX A: late original render completion must NOT paint the ORIGINAL's page tiles over the redline", async () => {
      const facts = await surfaceFacts(page);
      assert.equal(
        facts.originalTileCount,
        0,
        `the ORIGINAL's ${facts.originalTileCount} page-image tiles are now the top of the pane `
          + `(pdfSurfaceFirst=${facts.pdfSurfaceFirst})`,
      );
    });

    await check("FIX A: the arrived page images are still stored in state for the Original view", async () => {
      const facts = await surfaceFacts(page);
      assert.equal(
        facts.pagesStoredInState,
        5,
        "the late completion's page images must land in state.reviewDocumentRender even when the repaint is skipped",
      );
    });

    await check("pager must keep reporting the redline document, not flip to the original's '/ 5'", async () => {
      const text = await indicator(page);
      assert.ok(!/\/\s*5$/.test(text), `pager flipped to the ORIGINAL's tile count: '${text}'`);
      assert.match(text, /\/\s*7$/, `pager left the redline slice count '/ 7': '${text}'`);
    });

    // --- FIX B contract: a stray tile can never repage a faithful view --------
    // Inject a zero-height stray [data-review-render-page] tile next to the live
    // faithful surface. Pre-fix, viewer-controls counted ANY tile in the pane, so
    // the pager would read "1 / 1"; post-fix tiles are ignored while a faithful
    // surface is displayed, so the pager keeps the faithful slice count "/ 7".
    await page.evaluate(() => {
      const stray = document.createElement("figure");
      stray.setAttribute("data-review-render-page", "1");
      stray.style.height = "0";
      window.studioDocumentRender.appendChild(stray);
      window.dispatchEvent(new Event("resize")); // force a pager recompute
    });
    await page.waitForTimeout(120);

    await check("FIX B: a stray page tile must not repage the displayed faithful view", async () => {
      const text = await indicator(page);
      assert.ok(!/\/\s*1$/.test(text), `pager was repaged by a stray tile: '${text}'`);
      assert.match(text, /\/\s*7$/, `pager left the faithful slice count '/ 7': '${text}'`);
    });

    await page.evaluate(() => {
      const stray = window.studioDocumentRender.querySelector("figure[data-review-render-page]");
      if (stray) stray.remove();
    });

    // --- FIX C contract: the faithful fallback is LOUD and persistent ---------
    // Drive attemptFaithfulRedlineFallback the way maybeUpgradeSurfaceToFaithfulDocx
    // does when /reviewed-docx yields no bytes (409/500/404 -> reason "no_bytes").
    await page.evaluate(() => {
      attemptFaithfulRedlineFallback(
        window.__faithfulStub,
        "redline",
        "m1",
        reviewDocumentRenderRequestSequence,
        "no_bytes",
      );
    });
    await page.waitForTimeout(120);

    await check("FIX C: fallback to the faithful non-redline document shows a PERSISTENT visible notice", async () => {
      const facts = await page.evaluate(() => {
        const host = window.studioDocumentRender;
        const fallbackSurface = host.querySelector("[data-faithful-fallback]");
        const notice = host.querySelector("[data-faithful-fallback-notice]");
        return {
          fallbackPainted: Boolean(fallbackSurface),
          noticePresent: Boolean(notice),
          noticeText: notice ? notice.textContent.trim() : "",
          retryPresent: Boolean(notice && notice.querySelector("[data-faithful-fallback-retry]")),
          toasts: window.__calls.fallbackToasts,
        };
      });
      assert.equal(facts.fallbackPainted, true, "the faithful fallback surface must paint");
      assert.equal(facts.toasts, 1, "the existing transient toast must still fire");
      assert.equal(
        facts.noticePresent,
        true,
        "a persistent in-viewer notice must remain after the toast dies",
      );
      assert.match(
        facts.noticeText,
        /redline/i,
        `the notice must say the redlines could not be displayed, got '${facts.noticeText}'`,
      );
      assert.equal(facts.retryPresent, true, "the notice must carry a retry affordance");
    });

    await check("FIX C: the retry affordance re-runs the render path (re-attempts the faithful upgrade)", async () => {
      const kicksBefore = await page.evaluate(() => window.__calls.upgradeKicks);
      await page.evaluate(() => {
        const retry = window.studioDocumentRender.querySelector("[data-faithful-fallback-retry]");
        if (retry) retry.click();
      });
      const kicksAfter = await page.evaluate(() => window.__calls.upgradeKicks);
      assert.ok(
        kicksAfter > kicksBefore,
        `clicking retry must re-kick the faithful upgrade (before=${kicksBefore}, after=${kicksAfter})`,
      );
    });
  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error("\nreview-render-clobber.cjs: " + failures.length + " check(s) FAILED");
    process.exit(1);
  }
  console.log("\nreview-render-clobber.cjs PASSED");
}

main().catch((error) => { console.error(error); process.exit(1); });
