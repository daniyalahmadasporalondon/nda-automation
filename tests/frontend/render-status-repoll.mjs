// Regression lock for D1: the /render-status poll must NOT dead-end on a cold
// render's transient status:"rendering" payload.
//
// The backend serves rendering_in_progress_payload -> { status:"rendering",
// working_docx_ready:<bool>, ... } while PyMuPDF/soffice is still rasterizing the
// ORIGINAL source. The FE used to (a) normalize "rendering" to "unavailable",
// nulling the whole render state out and DISCARDING working_docx_ready (silently
// disabling the converted-PDF faithful auto-on lane), and (b) fetch /render-status
// exactly ONCE with no re-poll, so a cold PDF Original dead-ended on a blank
// surface forever.
//
// This drives the REAL classic requestMatterDocumentRenderPreview +
// pollMatterDocumentRenderStatus (loaded via vm) and proves:
//   (1) a "rendering" payload normalizes to a "loading" state that KEEPS
//       workingDocxReady (no longer nulled/discarded);
//   (2) a still-"rendering" render is RE-POLLED (more than one /render-status
//       fetch) until it resolves to "ready", at which point workingDocxReady is
//       committed;
//   (3) a render that never resolves stops after the attempt cap (no infinite loop).

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const STATIC_JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../static/js");
const tick = () => new Promise((resolve) => setImmediate(resolve));

function makeResponse(payload) {
  return { ok: true, json: () => Promise.resolve(payload) };
}

// Build a sandbox whose scripted fetch returns `renderingCount` in-flight
// "rendering" payloads before flipping to a terminal "ready" payload. setTimeout
// is captured (not real) so the test drives the backoff re-poll deterministically.
function build(renderingCount) {
  const fetchCalls = [];
  const timers = [];
  let calls = 0;
  const sandbox = {
    console,
    setTimeout: (fn) => {
      timers.push(fn);
      return timers.length;
    },
    clearTimeout: () => {},
    fetch: (url) => {
      fetchCalls.push(String(url));
      calls += 1;
      const payload = calls <= renderingCount
        ? { document_render: { status: "rendering", working_docx_ready: true, source_label: "Rendered PDF" } }
        : {
          document_render: {
            status: "ready",
            pdf_url: "/api/matters/m1/render-pdf",
            working_docx_ready: true,
            source_label: "Rendered PDF",
          },
        };
      return Promise.resolve(makeResponse(payload));
    },
    // Seed studioDocumentRender null so the REAL renderStudioDocumentHighlights /
    // faithfulDocxSurfaceActiveForCurrentView short-circuit at their null guards --
    // this test only asserts the fetch/re-poll + render-state contract.
    studioDocumentRender: null,
    reviewErrorFromPayload: (_payload, message) => new Error(message),
    state: {
      selectedMatter: { id: "m1", source_filename: "cold.pdf" },
      reviewDocumentRender: null,
    },
  };
  vm.createContext(sandbox);
  for (const file of ["config.js", "review-workstation-rendering.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC_JS_DIR, file), "utf8"), sandbox, { filename: file });
  }
  return { sandbox, fetchCalls, timers };
}

// Flush the most recently-captured backoff timer (the re-poll kick), then let the
// resulting fetch promise chain settle.
async function flushTimer(timers) {
  const fn = timers.shift();
  if (typeof fn === "function") fn();
  await tick();
}

async function main() {
  // (1) Direct normalize contract: "rendering" -> "loading", workingDocxReady kept.
  {
    const { sandbox } = build(0);
    const normed = vm.runInContext(
      'normalizeReviewDocumentRender({status:"rendering", working_docx_ready:true, source_label:"Rendered PDF"})',
      sandbox,
    );
    assert.ok(normed, "a rendering payload must NOT normalize to null (used to be discarded)");
    assert.equal(normed.status, "loading", "rendering must normalize to a loading state");
    assert.equal(normed.workingDocxReady, true, "working_docx_ready must survive a rendering payload");
  }

  // (2) Re-poll to resolution: two "rendering" payloads, then "ready".
  {
    const { sandbox, fetchCalls, timers } = build(2);
    vm.runInContext("requestMatterDocumentRenderPreview()", sandbox);
    await tick(); // first fetch resolves -> still rendering -> schedules a re-poll
    assert.equal(fetchCalls.length, 1, "cold render issues its first /render-status fetch");
    assert.equal(timers.length, 1, "a still-rendering render must schedule a re-poll (single-shot bug)");
    assert.ok(
      sandbox.state.reviewDocumentRender && sandbox.state.reviewDocumentRender.status === "loading",
      "while rendering the loading placeholder stands (render state is NOT nulled out)",
    );

    await flushTimer(timers); // 2nd fetch -> still rendering -> schedules again
    assert.equal(fetchCalls.length, 2, "still-rendering render is re-polled a second time");
    assert.equal(timers.length, 1, "second rendering payload schedules another re-poll");

    await flushTimer(timers); // 3rd fetch -> ready -> commit, no more timers
    assert.equal(fetchCalls.length, 3, "re-poll continues until the render resolves");
    assert.equal(timers.length, 0, "a resolved (ready) render stops the re-poll");
    const committed = sandbox.state.reviewDocumentRender;
    assert.ok(committed, "resolved render commits a render state");
    assert.equal(committed.status, "ready", "final committed status is ready");
    assert.equal(committed.workingDocxReady, true, "working_docx_ready reaches the committed render state");
  }

  // (3) Attempt cap: a render that never resolves must stop (no infinite loop).
  // The cap constant is a LOCAL of requestMatterDocumentRenderPreview (nested so
  // the review-render-clobber brace-walk extracts the poller as one unit), so we
  // prove the cap behaviourally: drive re-polls to exhaustion and assert they
  // STOP at a finite count well below the effectively-infinite rendering budget.
  {
    const RENDERING_BUDGET = 1000;
    const { sandbox, fetchCalls, timers } = build(RENDERING_BUDGET);
    vm.runInContext("requestMatterDocumentRenderPreview()", sandbox);
    await tick();
    // Drive every scheduled re-poll to exhaustion (guard well above any sane cap).
    let guard = 0;
    while (timers.length && guard < RENDERING_BUDGET + 5) {
      await flushTimer(timers);
      guard += 1;
    }
    assert.equal(timers.length, 0, "an unresolving render eventually stops scheduling re-polls");
    assert.ok(
      fetchCalls.length > 1,
      "an unresolving render is re-polled more than once (proves re-poll engaged)",
    );
    assert.ok(
      fetchCalls.length < RENDERING_BUDGET,
      `an unresolving render stops at a finite cap (${fetchCalls.length}), never looping to the budget`,
    );
  }

  console.log("render-status-repoll: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
