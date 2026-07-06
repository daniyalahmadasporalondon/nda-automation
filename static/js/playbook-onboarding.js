// First-run orientation banner for the Playbook page. A new user landing on the
// Playbook has no idea what it is: this is a dismissible intro card, rendered at
// the top of the Playbook panel, that explains the rulebook in a sentence and
// points at the one action that makes changes live (Publish). Self-contained by
// design (its own render + dismiss + localStorage), so it never touches the
// clause editor / publish flow in playbook-view.js. Mirrors the existing
// onboarding pattern (repository-board.js renderBoardOnboarding + corpus.js
// onboardingEmptyHtml): same markup shape, role="note", and a
// [data-onboarding-goto] CTA consumed by the one delegated handler in app.js.
const PlaybookOnboarding = (() => {
  // localStorage key remembering that the user dismissed the intro, so it does
  // not nag on every visit. Namespaced to this panel.
  const DISMISS_KEY = "nda.playbook.onboarding.dismissed";

  // localStorage is unavailable in some privacy modes / test harnesses; degrade
  // to "never dismissed / cannot persist" rather than throwing and stranding the
  // panel render. Same defensive posture as the guarded helpers elsewhere.
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
      // No persistence available: the banner will reappear next load, which is
      // an acceptable (and non-breaking) degradation.
    }
  }

  // Pure, string-only markup (no user-controlled values interpolated, so nothing
  // to escape) — unit-testable in isolation. Matches the repository onboarding
  // card classes/structure; the "Go to Publish?" CTA routes to the playbook tab
  // (we are already on it, but the shared [data-onboarding-goto] contract keeps
  // the CTA consistent with the other panels and harmlessly re-activates it).
  function onboardingHtml() {
    return `
      <div class="repository-onboarding-card" role="note" aria-label="About your playbook">
        <button class="playbook-onboarding-dismiss" type="button" data-playbook-onboarding-dismiss aria-label="Dismiss playbook introduction" title="Dismiss">×</button>
        <h2 class="repository-onboarding-title">Your playbook</h2>
        <p class="repository-onboarding-lead">This is the rulebook the AI reviews every NDA against — your clauses, acceptable language, and thresholds.</p>
        <p class="playbook-onboarding-hint">Review a clause, edit a rule, then <strong>Publish</strong> to make your changes live for every future review.</p>
      </div>
    `;
  }

  // Render into the panel's onboarding slot. Hidden entirely once dismissed.
  function render() {
    const node = document.querySelector("[data-playbook-onboarding]");
    if (!node) return;
    if (isDismissed()) {
      node.hidden = true;
      node.innerHTML = "";
      return;
    }
    node.hidden = false;
    node.innerHTML = onboardingHtml();
  }

  function dismiss() {
    rememberDismissed();
    const node = document.querySelector("[data-playbook-onboarding]");
    if (node) {
      node.hidden = true;
      node.innerHTML = "";
    }
  }

  // One delegated listener on the slot handles the dismiss ×. Attached once; the
  // [data-onboarding-goto] CTA is intentionally NOT handled here — the global
  // delegated handler in app.js owns that shared contract.
  function init() {
    const node = document.querySelector("[data-playbook-onboarding]");
    if (!node || node.__playbookOnboardingWired) return;
    node.__playbookOnboardingWired = true;
    node.addEventListener("click", (event) => {
      const target = event.target;
      if (!target || typeof target.closest !== "function") return;
      if (target.closest("[data-playbook-onboarding-dismiss]")) {
        event.preventDefault();
        dismiss();
      }
    });
    render();
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }

  return { DISMISS_KEY, dismiss, init, isDismissed, onboardingHtml, render };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { PlaybookOnboarding };
}
