// First-run "Set up your workspace" checklist for the Admin page.
//
// A new admin lands on the Admin surface with a console full of panels and no
// sense of the ORDER to wire things up in. This module renders a small
// getting-started checklist at the top of the Admin view — the three steps that
// take the system from empty to working end-to-end:
//
//   1. Connect Gmail   — import inbound NDAs
//   2. Connect Drive   — archive signed NDAs
//   3. Signing entities & AI models — the rules the review/generation runs on
//
// It deliberately mirrors the repository board's fresh-user onboarding panel
// (repository-board.js renderBoardOnboarding) so the two read as one family:
// same numbered-step markup, same `repository-onboarding-*` classes (defined in
// css/repository.css and loaded globally), same ✓-when-done affordance. This is
// self-contained for now; an integrator can later hoist the shared markup into
// one component.
//
// DONE-STATE DETECTION reuses only status the Admin page ALREADY loads into the
// shared app `state` — no new endpoints, no fetches of its own:
//   * Gmail:  state.gmailStatus (loaded globally at boot by auth-session).
//   * Drive:  state.driveStatus (populated once the Drive section is opened;
//             null before then, which reads as "not done" — fail toward showing
//             guidance, matching the repository panel's Gmail nudge).
// Step 3 (entities & models) has no single cheap connected/not signal, so it is
// always shown as a plain action step. Re-rendering on every Admin activation
// means a checkmark lights up the next time the admin returns to the page after
// wiring something up.
const AdminOnboarding = (() => {
  // localStorage key remembering the admin dismissed the setup checklist, so it
  // does not nag once they are set up (step 3 has no done-signal, so the card
  // never self-completes — the × is the way to be "done with it"). Namespaced to
  // this panel, mirroring PlaybookOnboarding / the Generator onboarding card.
  const DISMISS_KEY = "nda.admin.onboarding.dismissed";

  // localStorage is unavailable in some privacy modes / the node test harness;
  // degrade to "not dismissed / cannot persist" rather than throwing.
  function isDismissed() {
    try {
      return window.localStorage.getItem(DISMISS_KEY) === "1";
    } catch (error) {
      return false;
    }
  }

  function rememberDismissed() {
    try {
      window.localStorage.setItem(DISMISS_KEY, "1");
    } catch (error) {
      // No persistence: the checklist reappears next load — acceptable degradation.
    }
  }

  // Hide the checklist for good on this device and clear its slot.
  function dismiss() {
    rememberDismissed();
    const node = document.querySelector("[data-admin-onboarding]");
    if (node) {
      node.hidden = true;
      node.innerHTML = "";
    }
  }

  // A step the admin has completed: swap the number badge for a ✓ and drop the
  // call-to-action, exactly like the repository panel's "Gmail is connected".
  function doneStep(title, detail) {
    return `
      <li class="repository-onboarding-step is-done">
        <span class="repository-onboarding-step-icon" aria-hidden="true">✓</span>
        <span class="repository-onboarding-step-body">
          <strong>${title}</strong>
          <span>${detail}</span>
        </span>
      </li>`;
  }

  // A step still to do: numbered badge plus a CTA that switches the Admin console
  // to the relevant section. `data-admin-onboarding-goto` is handled by a single
  // delegated listener in app.js (activateAdminSection), so the button needs no
  // per-instance wiring.
  function todoStep(number, title, detail, gotoSection, actionLabel) {
    return `
      <li class="repository-onboarding-step">
        <span class="repository-onboarding-step-icon" aria-hidden="true">${number}</span>
        <span class="repository-onboarding-step-body">
          <strong>${title}</strong>
          <span>${detail}</span>
          <button class="repository-onboarding-action secondary" type="button" data-admin-onboarding-goto="${gotoSection}">${actionLabel}</button>
        </span>
      </li>`;
  }

  // Gmail inbound is genuinely wired only when the status reports ready. Any
  // unknown / not-ready / null state keeps the "Connect Gmail" nudge (fail toward
  // showing guidance), mirroring repository-board.js gmailInboundReady.
  function gmailConnected(state) {
    const inbound = state?.gmailStatus?.inbound;
    return Boolean(inbound && inbound.ready === true);
  }

  // Drive is connected when the Drive status (loaded on first open of the Drive
  // section) reports connected. Null/absent reads as not-done.
  function driveConnected(state) {
    return state?.driveStatus?.connected === true;
  }

  // Build the checklist markup. All copy is static (no user-controlled values are
  // interpolated), so there is nothing to escape here — same as the repository
  // and corpus onboarding panels.
  function checklistHtml(state) {
    const gmailStep = gmailConnected(state)
      ? doneStep(
          "Gmail is connected",
          "Inbound NDAs are imported into your repository automatically.",
        )
      : todoStep(
          "1",
          "Connect Gmail to import inbound NDAs",
          "Incoming NDAs land in your repository, ready to review.",
          "email",
          "Connect Gmail",
        );
    const driveStep = driveConnected(state)
      ? doneStep(
          "Google Drive is connected",
          "Signed NDAs are archived to your Drive and reconciled into the corpus.",
        )
      : todoStep(
          "2",
          "Connect Google Drive to archive signed NDAs",
          "Fully-signed NDAs are filed to Drive so your corpus stays complete.",
          "drive",
          "Connect Drive",
        );
    // Step 3 has no single cheap done-signal, so it is always an action step.
    const setupStep = todoStep(
      "3",
      "Set your signing entities & AI models",
      "Choose the entities you sign as and the models each AI role runs on.",
      "models",
      "Open AI Models",
    );
    return `
      <div class="admin-onboarding-card repository-onboarding-card" role="note" aria-label="Set up your workspace">
        <button class="admin-onboarding-dismiss" type="button" data-admin-onboarding-dismiss aria-label="Dismiss setup checklist" title="Dismiss">×</button>
        <h2 class="repository-onboarding-title">Set up your workspace</h2>
        <p class="repository-onboarding-lead">A few steps to get the system working end-to-end.</p>
        <ol class="repository-onboarding-steps">
          ${gmailStep}
          ${driveStep}
          ${setupStep}
        </ol>
      </div>`;
  }

  // Render (or refresh) the checklist into its container. Safe to call on every
  // Admin activation: it is a no-op when the container is absent, and re-renders
  // in place so a newly-detected connection lights its ✓.
  function render(state) {
    const node = document.querySelector("[data-admin-onboarding]");
    if (!node) return;
    if (isDismissed()) {
      node.hidden = true;
      node.innerHTML = "";
      return;
    }
    node.hidden = false;
    node.innerHTML = checklistHtml(state || {});
    // Wire the dismiss × once. The [data-admin-onboarding-goto] CTAs stay owned
    // by the global delegated handler in app.js; we only claim the dismiss ×.
    if (!node.__adminOnboardingWired) {
      node.__adminOnboardingWired = true;
      node.addEventListener("click", (event) => {
        const target = event.target;
        if (target && typeof target.closest === "function"
            && target.closest("[data-admin-onboarding-dismiss]")) {
          event.preventDefault();
          dismiss();
        }
      });
    }
  }

  return {
    DISMISS_KEY,
    checklistHtml,
    dismiss,
    gmailConnected,
    driveConnected,
    isDismissed,
    render,
  };
})();

// Node/test consumption: the frontend .cjs suite requires this file directly.
if (typeof module !== "undefined" && module.exports) {
  module.exports = AdminOnboarding;
}
