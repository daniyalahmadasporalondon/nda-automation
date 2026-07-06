// SHARED FIRST-RUN ONBOARDING COMPONENT.
//
// A single source of truth for the "welcome / here's how to get started" cards
// shown on empty pages. Before this, Repository (repository-board.js
// renderBoardOnboarding) and Corpus (corpus.js onboardingEmptyHtml) each hand-
// rolled near-identical markup and copy. This module gives every page one
// dependency-free renderer so new pages (Review is the first) get a consistent,
// accessible onboarding card without copy/markup drift.
//
// Loaded as a classic <script> (see index.html), consistent with the codebase's
// ES-floor (no ESM-only syntax) — it assigns a single global `Onboarding`.
//
// The rendered card carries the app-wide `data-onboarding-goto="<tab>"` hook, so
// its action buttons are routed to the right tab by the one delegated handler in
// app.js — no per-card wiring needed.
const Onboarding = (() => {
  // HTML-escape any value that reaches innerHTML. Prefer the app-wide escaper
  // (window.escapeHtml) when present; fall back to a local escape so the module
  // is safe in isolation (unit tests, partial load order). Mirrors the guarded
  // html() helpers in repository-board.js / corpus.js.
  function html(value) {
    if (typeof window !== "undefined" && typeof window.escapeHtml === "function") {
      return window.escapeHtml(value);
    }
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Build the card's inner markup as a string. Pure + string-only so it is
  // unit-testable and reusable by callers that want the HTML directly (matching
  // corpus.js's onboardingEmptyHtml contract). Every interpolated value is
  // escaped. `steps` is an array of { label, body, actionText?, actionGoto? };
  // a step with actionText + actionGoto renders a routing button.
  function cardHtml(options) {
    const opts = options || {};
    const title = opts.title == null ? "" : String(opts.title);
    const lead = opts.lead == null ? "" : String(opts.lead);
    const ariaLabel = opts.ariaLabel == null ? title : String(opts.ariaLabel);
    const steps = Array.isArray(opts.steps) ? opts.steps : [];
    const stepsHtml = steps
      .map((step, index) => stepHtml(step, index))
      .join("");
    const stepsBlock = stepsHtml
      ? `<ol class="onboarding-steps">${stepsHtml}</ol>`
      : "";
    return `
      <div class="onboarding-card" role="note" aria-label="${html(ariaLabel)}">
        ${title ? `<h2 class="onboarding-title">${html(title)}</h2>` : ""}
        ${lead ? `<p class="onboarding-lead">${html(lead)}</p>` : ""}
        ${stepsBlock}
      </div>
    `;
  }

  // A single numbered step. A step can be marked done (green tick) or carry a
  // routing action button (data-onboarding-goto). `body` is optional supporting
  // copy under the bold label.
  function stepHtml(step, index) {
    const s = step || {};
    const done = Boolean(s.done);
    const label = s.label == null ? "" : String(s.label);
    const body = s.body == null ? "" : String(s.body);
    const icon = done ? "✓" : String(index + 1);
    const action = s.actionText && s.actionGoto
      ? `<button class="onboarding-action${s.actionSecondary ? " secondary" : ""}" type="button" data-onboarding-goto="${html(s.actionGoto)}">${html(s.actionText)}</button>`
      : "";
    return `
      <li class="onboarding-step${done ? " is-done" : ""}">
        <span class="onboarding-step-icon" aria-hidden="true">${html(icon)}</span>
        <span class="onboarding-step-body">
          ${label ? `<strong>${html(label)}</strong>` : ""}
          ${body ? `<span>${html(body)}</span>` : ""}
          ${action}
        </span>
      </li>
    `;
  }

  // Render an onboarding card into `container`. Replaces the container's
  // contents with the card and returns the card element (or null when there is
  // no container). Callers own show/hide of the container itself — this only
  // paints the card. The action buttons route via the app-wide delegated
  // data-onboarding-goto handler in app.js, so no per-card listener is wired.
  function renderOnboardingCard(container, options) {
    if (!container) return null;
    container.innerHTML = cardHtml(options);
    return container.firstElementChild
      || container.querySelector(".onboarding-card");
  }

  return { cardHtml, renderOnboardingCard, stepHtml };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { Onboarding };
}
