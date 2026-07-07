"use strict";

// Frontend unit test for the Admin "Set up your workspace" checklist
// (static/js/admin-onboarding.js -> AdminOnboarding).
//
// Two behaviours this guards, both reported as bugs from the shipped Admin page:
//   1. Done-state detection: once Drive is connected (state.driveStatus.connected),
//      step 2 must render as a ✓ "Google Drive is connected" and MUST NOT keep
//      nagging with a "Connect Drive" call-to-action.
//   2. Dismissibility: the card must carry a dismiss × (data-admin-onboarding-
//      dismiss) so an admin can be "done with it" — step 3 has no done-signal, so
//      the card never self-completes and the × is the only way to retire it.
//
// admin-onboarding.js is a browser script exposing a CommonJS export behind a
// `typeof module` guard and touches document/window only inside functions, so it
// requires cleanly in node with no DOM.

const assert = require("node:assert/strict");
const path = require("node:path");

const AdminOnboarding = require(
  path.join(__dirname, "..", "..", "static", "js", "admin-onboarding.js"),
);

// --- Drive done-state: connected => ✓, not a "Connect Drive" nag --------------

(function driveConnectedRendersDoneStep() {
  const html = AdminOnboarding.checklistHtml({
    gmailStatus: { inbound: { ready: true } },
    driveStatus: { connected: true },
  });
  assert.match(html, /Google Drive is connected/, "connected Drive shows the done copy");
  assert.match(html, /Signed NDAs are archived/, "done detail present");
  assert.doesNotMatch(
    html,
    /data-admin-onboarding-goto="drive"/,
    "connected Drive must NOT render the Connect Drive CTA",
  );
  assert.ok(AdminOnboarding.driveConnected({ driveStatus: { connected: true } }));
})();

(function driveUnknownRendersTodoStep() {
  const html = AdminOnboarding.checklistHtml({
    gmailStatus: { inbound: { ready: true } },
    // no driveStatus at all -> not-done, fail toward guidance
  });
  assert.match(html, /data-admin-onboarding-goto="drive"/, "unknown Drive shows the CTA");
  assert.match(html, /Connect Drive/, "unknown Drive shows Connect Drive label");
  assert.doesNotMatch(html, /Google Drive is connected/, "unknown Drive is not marked done");
  assert.equal(AdminOnboarding.driveConnected({}), false);
  assert.equal(AdminOnboarding.driveConnected({ driveStatus: { connected: false } }), false);
})();

(function gmailDoneStateMirrorsStatus() {
  assert.ok(AdminOnboarding.gmailConnected({ gmailStatus: { inbound: { ready: true } } }));
  assert.equal(AdminOnboarding.gmailConnected({ gmailStatus: { inbound: { ready: false } } }), false);
  assert.equal(AdminOnboarding.gmailConnected({}), false);
})();

// --- Dismiss control ----------------------------------------------------------

(function checklistCarriesDismissControl() {
  const html = AdminOnboarding.checklistHtml({});
  assert.match(
    html,
    /data-admin-onboarding-dismiss/,
    "checklist renders a dismiss control the admin can close it with",
  );
  assert.match(html, /aria-label="Dismiss setup checklist"/, "dismiss × is labelled");
})();

(function isDismissedDegradesWithoutLocalStorage() {
  // No window/localStorage in node: must return false, never throw, so the card
  // renders rather than stranding on an exception.
  assert.equal(AdminOnboarding.isDismissed(), false);
  assert.equal(typeof AdminOnboarding.DISMISS_KEY, "string");
  assert.match(AdminOnboarding.DISMISS_KEY, /admin/, "dismiss key namespaced to this panel");
})();

console.log("admin-onboarding.cjs: all assertions passed");
