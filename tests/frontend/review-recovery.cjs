"use strict";

// Server-free Playwright proof for the review-pipeline RECOVERY transitions
// (frontend half of the review-recovery fix). No Python backend: we load the REAL
// review-workstation-actions.js + review-workstation-viewer.js modules against a
// minimal DOM, stub fetch + the cross-module render helpers, and drive the poll /
// load funnels directly.
//
// SHARED STATUS CONTRACT under test:
//   review_status === "interrupted"  -> a review was in-flight but the worker/process
//       died (e.g. app restart). RECOVERABLE-TERMINAL: nothing auto-runs, the poll
//       STOPS, the Review button is RE-ENABLED ("Review"), an inline note shows, and
//       NO red failure header/toast appears (distinct from "failed").
//   review_status === "stalled"      -> read-time TTL label for a live-but-slow
//       review: NOT a hard in-flight lock here — the Review button stays ENABLED so a
//       wedged-looking review always has a retry exit.
//   review_status === "in_progress"  -> a live worker. Opening such a matter must
//       RESUME the poll (enter in-flight UI + start polling), never strand it.
//
// Cases:
//   1. open in_progress  -> poll resumes (enterReviewInFlightUi + a scheduled tick).
//   2. poll sees interrupted -> button enabled "Review", calm header (no red "!" mark,
//      no "failed" state), poll stopped, no failure toast.
//   3. stalled -> Review button ENABLED (retryable), reviewInProgress is false.
//   4. nav-away abort -> the stranded "Reviewing…" file-meta is CLEARED, not frozen.
//
// Run: node tests/frontend/review-recovery.cjs

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const ACTIONS_JS = read("static/js/review-workstation-actions.js");
const VIEWER_JS = read("static/js/review-workstation-viewer.js");
// The matter-utils source is an ES module; pull just the reviewInProgress /
// reviewInterrupted / reviewStalled contract into a tiny browser-global MatterUtils so
// the modules' shared discriminator is the REAL one under test (not a re-stub).
const MATTER_UTILS_JS = read("static/js/modules/matter-utils.mjs");

// Minimal DOM: the studio header controls the modules bind by id, plus a render host.
const PAGE_HTML = `<!doctype html><html><head></head><body>
  <div class="studio-toolbar">
    <span id="studioFileMeta"></span>
    <button id="studioRefreshReviewButton" type="button">Review</button>
    <span id="studioReviewStaleIndicator" hidden></span>
  </div>
  <div class="sr-only">
    <h3 id="studioOverallTitle"></h3>
    <span id="studioResultMark"></span>
    <span id="studioResultMeta"></span>
  </div>
  <div class="studio-page-wrap"><div class="studio-page">
    <textarea id="studioNdaText"></textarea>
    <div id="studioDocumentRender" hidden></div>
  </div></div>
</body></html>`;

// The element + cross-module globals the real modules read as bare identifiers. We
// declare them with `var` so they are visible to later script tags' lexical scope.
function bootScript() {
  return `
    // --- element globals (app.js normally owns these) ---
    var studioFileMeta = document.querySelector("#studioFileMeta");
    var studioRefreshReviewButton = document.querySelector("#studioRefreshReviewButton");
    var studioReviewStaleIndicator = document.querySelector("#studioReviewStaleIndicator");
    var studioOverallTitle = document.querySelector("#studioOverallTitle");
    var studioResultMark = document.querySelector("#studioResultMark");
    var studioResultMeta = document.querySelector("#studioResultMeta");
    var studioNdaText = document.querySelector("#studioNdaText");
    var studioDocumentRender = document.querySelector("#studioDocumentRender");
    var DEFAULT_DOCUMENT_TITLE = "Untitled";
    var SOURCE_PLACEHOLDER = "";

    // --- shared app state ---
    var state = {
      selectedMatter: null,
      selectedDocument: null,
      reviewClauses: [],
      reviewParagraphs: [],
      gmailStatus: {},
      redlineDraftDirty: false,
      redlineDraft: null,
    };

    // --- the REAL setFileMeta (review-workstation-source.js) ---
    function setFileMeta(message) { studioFileMeta.textContent = message; }

    // --- spies + no-op stubs for the cross-module render helpers the recovery paths
    //     call. We record calls so the test can assert resume/terminal behaviour. ---
    window.__spy = {
      enterReviewInFlightUi: 0,
      startReviewPoll: [],
      stopReviewPoll: 0,
      setSkeleton: [],
      notify: [],
    };
    var setReviewWorkspaceSkeleton = (on) => { window.__spy.setSkeleton.push(Boolean(on)); };
    function updateExportButtonState() {}
    function renderResult() {}
    function applyMatterRedlineDraft() {}
    function renderCounterpartyConfirmation() {}
    function setCounterpartyMeta() {}
    function setSourceText(t) { studioNdaText.value = t || ""; }
    function setSourcePlaceholder() {}
    function setDocumentTitle() {}
    function activateTab() {}
    function resizeSourceEditors() {}
    var RepositoryView = { sourceTypeLabel: () => "Inbound" };
    var repositoryController = { loadMatters: () => {} };
    // notificationsController records calls so we can assert the interrupted path
    // NEVER fires a failure toast.
    var notificationsController = { notify: (t, m) => { window.__spy.notify.push([t, m]); } };
  `;
}

// MatterUtils bridge: evaluate the real ES module's predicate bodies into a global so
// the modules' `MatterUtils.reviewInProgress(...)` is the REAL contract under test.
function matterUtilsBridge() {
  // Extract the three predicate sources from the ESM text and expose them globally.
  const pick = (name) => {
    const re = new RegExp(`export function ${name}\\(matter\\) \\{([\\s\\S]*?)\\n\\}`, "m");
    const m = MATTER_UTILS_JS.match(re);
    if (!m) throw new Error(`could not extract ${name} from matter-utils.mjs`);
    return `function ${name}(matter) {${m[1]}\n}`;
  };
  return `
    ${pick("reviewInProgress")}
    ${pick("reviewInterrupted")}
    ${pick("reviewStalled")}
    ${pick("reviewFailed")}
    var MatterUtils = {
      reviewInProgress,
      reviewInterrupted,
      reviewStalled,
      reviewFailed,
      counterpartyEmail: () => "",
    };
  `;
}

async function loadPage(browser) {
  const page = await browser.newPage();
  await page.setContent(PAGE_HTML);
  await page.addScriptTag({ content: bootScript() });
  await page.addScriptTag({ content: matterUtilsBridge() });
  await page.addScriptTag({ content: ACTIONS_JS });
  await page.addScriptTag({ content: VIEWER_JS });
  // Wrap the real startReviewPoll / enterReviewInFlightUi so the test can observe
  // resumes WITHOUT letting real timers run (we drive ticks manually).
  await page.evaluate(() => {
    window.__realStartReviewPoll = startReviewPoll;
    // Override the timer scheduler so a started poll does not fire real network ticks
    // during the test; we still record that a poll was started + is in-flight.
    window.scheduleReviewPollTick = function (controller) {
      if (controller && !controller.stopped) window.__spy.startReviewPoll.push(controller.matterId);
    };
    const realEnter = enterReviewInFlightUi;
    window.enterReviewInFlightUi = function () { window.__spy.enterReviewInFlightUi += 1; return realEnter(); };
  });
  return page;
}

async function main() {
  const browser = await chromium.launch();
  const failures = [];

  // --- Case 1: opening an in_progress matter RESUMES the poll -----------------
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => {
      loadMatterIntoReview({
        id: "m-inprogress",
        review_status: "in_progress",
        ai_review_ran: false,
        review_result: {},
        extracted_text: "Body",
      });
    });
    const spy = await page.evaluate(() => ({
      enter: window.__spy.enterReviewInFlightUi,
      started: window.__spy.startReviewPoll.slice(),
      inFlight: reviewPollInFlight(),
    }));
    assert.ok(spy.enter >= 1, "opening in_progress did not enter the in-flight UI");
    assert.ok(spy.started.includes("m-inprogress"), "opening in_progress did not start/schedule the poll");
    assert.ok(spy.inFlight, "opening in_progress left no poll in flight (matter would strand)");
    process.stdout.write("  ok 1 - open in_progress resumes the background-review poll\n");
    await page.close();
  } catch (error) { failures.push(["1 open in_progress resumes poll", error]); }

  // --- Case 1b: opening a STALLED matter ALSO resumes (live-but-slow) ----------
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => {
      loadMatterIntoReview({ id: "m-stalled-open", review_status: "stalled", ai_review_ran: true, review_result: { clauses: [] }, extracted_text: "B" });
    });
    const started = await page.evaluate(() => window.__spy.startReviewPoll.slice());
    assert.ok(started.includes("m-stalled-open"), "opening a stalled (live-but-slow) matter did not resume the poll");
    process.stdout.write("  ok 1b - open stalled also resumes the poll (still running server-side)\n");
    await page.close();
  } catch (error) { failures.push(["1b open stalled resumes poll", error]); }

  // --- Case 1c: opening an INTERRUPTED matter does NOT resume (terminal) -------
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => {
      loadMatterIntoReview({ id: "m-int-open", review_status: "interrupted", ai_review_ran: false, review_result: {}, extracted_text: "B" });
    });
    const r = await page.evaluate(() => ({
      started: window.__spy.startReviewPoll.slice(),
      // The Review button must be ENABLED (interrupted is not in-flight) so it is retryable.
      btnDisabled: studioRefreshReviewButton.disabled,
      btnLabel: studioRefreshReviewButton.textContent,
    }));
    assert.ok(!r.started.includes("m-int-open"), "interrupted wrongly resumed a poll (it is recoverable-terminal)");
    assert.equal(r.btnDisabled, false, "interrupted left the Review button disabled (not retryable)");
    assert.equal(r.btnLabel, "Review", "interrupted did not label the button 'Review'");
    process.stdout.write("  ok 1c - open interrupted does NOT resume; Review button stays enabled/retryable\n");
    await page.close();
  } catch (error) { failures.push(["1c open interrupted retryable", error]); }

  // --- Case 2: a poll tick reading "interrupted" is TERMINAL + calm -----------
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => {
      state.selectedMatter = { id: "m-int", review_status: "in_progress", ai_review_ran: false };
      // Seed an in-flight poll, then feed the tick an interrupted matter.
      __realStartReviewPoll("m-int");
      // pollReviewMatter is the network read; stub it to return interrupted.
      window.pollReviewMatter = async () => ({ id: "m-int", review_status: "interrupted", ai_review_ran: false });
    });
    await page.evaluate(async () => { await runReviewPollTick(reviewPollController); });
    const r = await page.evaluate(() => ({
      inFlight: reviewPollInFlight(),
      title: studioOverallTitle.textContent,
      mark: studioResultMark.textContent,
      markClass: studioResultMark.className,
      meta: studioResultMeta.textContent,
      fileMeta: studioFileMeta.textContent,
      btnDisabled: studioRefreshReviewButton.disabled,
      btnLabel: studioRefreshReviewButton.textContent,
      notify: window.__spy.notify.slice(),
      hasRetryButton: Boolean(document.querySelector(".review-retry-button")),
    }));
    assert.equal(r.inFlight, false, "interrupted poll tick did not STOP polling (terminal)");
    assert.notEqual(r.title, "Review failed", "interrupted rendered the RED failure header");
    assert.notEqual(r.mark, "!", "interrupted rendered the red failure '!' mark");
    assert.match(r.title, /interrupted/i, "interrupted header missing the calm 'interrupted' wording");
    assert.match(r.fileMeta, /interrupted/i, "interrupted inline note missing");
    assert.match(r.fileMeta, /click Review|run it again/i, "interrupted note does not tell the user to click Review");
    assert.equal(r.btnDisabled, false, "interrupted left the Review button disabled");
    assert.equal(r.btnLabel, "Review", "interrupted button not labelled 'Review'");
    assert.equal(r.notify.length, 0, "interrupted fired a notification toast (must be silent — not a failure)");
    process.stdout.write("  ok 2 - interrupted poll tick: terminal, calm header, enabled Review, no failure toast\n");
    await page.close();
  } catch (error) { failures.push(["2 interrupted poll terminal+calm", error]); }

  // --- Case 2b: a poll tick reading "failed" STILL renders the red failure -----
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => {
      state.selectedMatter = { id: "m-fail", review_status: "in_progress", ai_review_ran: false };
      __realStartReviewPoll("m-fail");
      window.pollReviewMatter = async () => ({ id: "m-fail", review_status: "failed", review_error: "Scanned PDF unreadable", ai_review_ran: false });
    });
    await page.evaluate(async () => { await runReviewPollTick(reviewPollController); });
    const r = await page.evaluate(() => ({
      inFlight: reviewPollInFlight(),
      title: studioOverallTitle.textContent,
      mark: studioResultMark.textContent,
    }));
    assert.equal(r.inFlight, false, "failed poll tick did not stop polling");
    assert.equal(r.title, "Review failed", "a genuine failure must STILL render the red failure header");
    assert.equal(r.mark, "!", "a genuine failure must still render the '!' mark");
    process.stdout.write("  ok 2b - a genuine 'failed' poll tick still renders the red failure (interrupted didn't weaken it)\n");
    await page.close();
  } catch (error) { failures.push(["2b failed still red", error]); }

  // --- Case 3: stalled keeps the Review button ENABLED (retryable) ------------
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => {
      state.selectedMatter = { id: "m-stall", review_status: "stalled", ai_review_ran: true, review_refresh: null };
      renderReviewRefreshNotice();
    });
    const r = await page.evaluate(() => ({
      btnDisabled: studioRefreshReviewButton.disabled,
      btnLabel: studioRefreshReviewButton.textContent,
      inProgress: MatterUtils.reviewInProgress(state.selectedMatter),
      indicator: studioReviewStaleIndicator.textContent,
    }));
    assert.equal(r.inProgress, false, "stalled must NOT count as in-progress (it is retryable)");
    assert.equal(r.btnDisabled, false, "stalled left the Review button disabled (must stay retryable)");
    assert.equal(r.btnLabel, "Review", "stalled button not labelled 'Review'");
    process.stdout.write("  ok 3 - stalled keeps the Review button enabled/retryable\n");
    await page.close();
  } catch (error) { failures.push(["3 stalled retryable", error]); }

  // --- Case 4 (Task 4): a live in_progress review shows the NEUTRAL indicator --
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => {
      // ai_review_ran true would normally light GREEN "Reviewed"; an active review
      // must override that with the neutral "Reviewing…" tone, not the stored verdict.
      state.selectedMatter = { id: "m-live", review_status: "in_progress", ai_review_ran: true };
      renderReviewRefreshNotice();
    });
    const r = await page.evaluate(() => ({
      text: studioReviewStaleIndicator.textContent,
      hidden: studioReviewStaleIndicator.hidden,
      reviewingClass: studioReviewStaleIndicator.classList.contains("is-reviewing"),
      reviewedClass: studioReviewStaleIndicator.classList.contains("is-reviewed"),
    }));
    assert.equal(r.hidden, false, "active-review indicator was hidden");
    assert.match(r.text, /Reviewing/i, "active review did not show the neutral 'Reviewing…' indicator");
    assert.ok(r.reviewingClass, "active review missing the neutral .is-reviewing tone");
    assert.ok(!r.reviewedClass, "active review wrongly reused the green 'Reviewed' verdict mid-review");
    process.stdout.write("  ok 4 - active review shows the neutral 'Reviewing…' indicator, not the stored verdict\n");
    await page.close();
  } catch (error) { failures.push(["4 neutral reviewing indicator", error]); }

  // --- Case 5 (Task 3): nav-away clears the stranded 'Reviewing…' file-meta ----
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => {
      state.selectedMatter = { id: "m-nav", review_status: "in_progress" };
      enterReviewInFlightUi(); // writes the optimistic "Reviewing with AI…" meta
    });
    const before = await page.evaluate(() => studioFileMeta.textContent);
    assert.match(before, /Reviewing with AI/i, "precondition: enterReviewInFlightUi should set the in-flight meta");
    // Simulate the nav-away/supersede teardown path.
    await page.evaluate(() => { stopReviewPoll(); exitReviewInFlightUi(); });
    const after = await page.evaluate(() => studioFileMeta.textContent);
    assert.equal(after, "", "exitReviewInFlightUi left the stranded 'Reviewing…' meta frozen (incoherent header)");
    process.stdout.write("  ok 5 - nav-away/abort clears the stranded 'Reviewing…' file-meta\n");
    await page.close();
  } catch (error) { failures.push(["5 stranded meta cleared", error]); }

  // --- Case 5b: exit does NOT stomp an unrelated file-meta --------------------
  try {
    const page = await loadPage(browser);
    await page.evaluate(() => { setFileMeta("Sent redline to a@b.com"); exitReviewInFlightUi(); });
    const after = await page.evaluate(() => studioFileMeta.textContent);
    assert.equal(after, "Sent redline to a@b.com", "exitReviewInFlightUi wrongly cleared an unrelated file-meta");
    process.stdout.write("  ok 5b - exit only clears the in-flight meta, never an unrelated message\n");
    await page.close();
  } catch (error) { failures.push(["5b exit preserves unrelated meta", error]); }

  await browser.close();

  if (failures.length) {
    process.stderr.write("\nreview-recovery.cjs FAIL\n");
    for (const [name, error] of failures) {
      process.stderr.write(`  x ${name}: ${error && error.message ? error.message : error}\n`);
    }
    process.exit(1);
  }
  process.stdout.write("review-recovery.cjs PASS\n");
}

main().catch((error) => {
  process.stderr.write(`review-recovery.cjs ERROR: ${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
