"use strict";

// Frontend unit test for the SHARED first-run onboarding component
// (static/js/onboarding.js -> Onboarding.renderOnboardingCard / cardHtml).
//
// This is the single source of truth every page's "get started" empty-state
// card is meant to route through, so we exercise the shipped wiring directly:
// the accessible role/aria, the numbered/done step icons, the app-wide
// data-onboarding-goto routing hook on action buttons, and HTML-escaping of
// interpolated copy. onboarding.js is a classic browser script exposing a
// CommonJS export behind a `typeof module` guard, so we require it as the page
// would load it.

const assert = require("node:assert/strict");
const path = require("node:path");

const { Onboarding } = require(
  path.join(__dirname, "..", "..", "static", "js", "onboarding.js"),
);

// --- cardHtml: pure string builder -----------------------------------------

(function cardHtmlHasAccessibleShellAndCopy() {
  const html = Onboarding.cardHtml({
    title: "Review an NDA",
    lead: "The AI checks an NDA clause-by-clause against your playbook.",
    ariaLabel: "Get started with reviewing an NDA",
    steps: [
      {
        label: "Open an NDA from your Repository",
        body: "Pick one from your Inbox or In Review column.",
        actionText: "Go to Repository",
        actionGoto: "repository",
      },
    ],
  });
  assert.match(html, /class="onboarding-card"/, "renders the shared card class");
  assert.match(html, /role="note"/, "card is a note landmark");
  assert.match(
    html,
    /aria-label="Get started with reviewing an NDA"/,
    "card carries the provided aria-label",
  );
  assert.match(html, /class="onboarding-title">Review an NDA</, "renders the title");
  assert.match(
    html,
    /class="onboarding-lead">The AI checks an NDA clause-by-clause/,
    "renders the lead",
  );
  assert.match(html, /class="onboarding-steps"/, "renders a steps list");
  assert.match(
    html,
    /class="onboarding-action"[^>]*data-onboarding-goto="repository"/,
    "action button carries the app-wide routing hook to the repository tab",
  );
  assert.match(html, />Go to Repository</, "action button shows its label");
  assert.match(html, /class="onboarding-step-body">\s*<strong>Open an NDA/, "step label is bold");
})();

// --- ariaLabel falls back to the title -------------------------------------

(function ariaLabelDefaultsToTitle() {
  const html = Onboarding.cardHtml({ title: "Welcome" });
  assert.match(html, /aria-label="Welcome"/, "aria-label defaults to the title when omitted");
})();

// --- done steps render a tick, not a number --------------------------------

(function doneStepShowsTick() {
  const html = Onboarding.cardHtml({
    title: "T",
    steps: [
      { label: "First", done: false },
      { label: "Connected", done: true },
    ],
  });
  assert.match(html, /onboarding-step-icon" aria-hidden="true">1</, "first pending step is numbered 1");
  assert.match(html, /class="onboarding-step is-done"/, "done step carries the is-done class");
  assert.match(html, /onboarding-step-icon" aria-hidden="true">✓</, "done step shows a tick");
})();

// --- secondary action variant ----------------------------------------------

(function secondaryActionVariant() {
  const html = Onboarding.cardHtml({
    title: "T",
    steps: [{ label: "L", actionText: "Do it", actionGoto: "admin", actionSecondary: true }],
  });
  assert.match(
    html,
    /class="onboarding-action secondary"[^>]*data-onboarding-goto="admin"/,
    "secondary action renders the secondary modifier + goto",
  );
})();

// --- HTML-escaping of interpolated copy (no window.escapeHtml present) ------

(function escapesInterpolatedCopy() {
  const html = Onboarding.cardHtml({
    title: '<script>alert(1)</script>',
    lead: 'a & b "c"',
    steps: [{ label: "<b>x</b>", actionText: "<i>go</i>", actionGoto: "rep<o>" }],
  });
  assert.ok(!/<script>alert/.test(html), "script tag in title is escaped");
  assert.match(html, /&lt;script&gt;/, "title angle brackets are entity-escaped");
  assert.match(html, /a &amp; b &quot;c&quot;/, "lead ampersand + quotes are escaped");
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/, "step label is escaped");
  assert.match(html, /data-onboarding-goto="rep&lt;o&gt;"/, "goto attribute value is escaped");
})();

// --- renderOnboardingCard: writes into a container + returns the card ------

(function renderIntoContainer() {
  let written = "";
  const cardStub = { className: "onboarding-card" };
  const container = {
    set innerHTML(value) { written = value; },
    get innerHTML() { return written; },
    firstElementChild: cardStub,
    querySelector() { return cardStub; },
  };
  const returned = Onboarding.renderOnboardingCard(container, {
    title: "Review an NDA",
    steps: [{ label: "L", actionText: "Go", actionGoto: "repository" }],
  });
  assert.match(written, /class="onboarding-card"/, "paints the card into the container innerHTML");
  assert.match(written, /data-onboarding-goto="repository"/, "container markup carries the routing hook");
  assert.equal(returned, cardStub, "returns the rendered card element");
})();

// --- renderOnboardingCard tolerates a missing container --------------------

(function renderNullContainerIsSafe() {
  assert.equal(
    Onboarding.renderOnboardingCard(null, { title: "x" }),
    null,
    "no container -> no-op returning null (no throw)",
  );
})();

// --- empty steps -> no <ol> -------------------------------------------------

(function noStepsNoList() {
  const html = Onboarding.cardHtml({ title: "Just a title" });
  assert.ok(!/onboarding-steps/.test(html), "omits the steps list when there are no steps");
})();

console.log("onboarding-component: all checks passed");
