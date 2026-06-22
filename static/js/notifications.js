// In-app toast notifications for newly-arrived inbound NDAs.
//
// The server-side Gmail scheduler imports inbound NDAs on the configured cadence,
// so new matters appear in /api/matters on their own. This controller watches the
// matter list (fed by the repository poll on the Repository tab, and by its own
// lightweight poll on every other tab) and pops a top-right toast for each newly
// seen `gmail_inbound` matter. Clicking a toast opens that matter for review.
//
// GOLDEN RULE: the first observation after load SEEDS the seen-set silently, so the
// existing inbox never floods the screen on page load -- only genuinely new arrivals
// during the session toast. Detection is purely client-side; no backend changes.
const NotificationsView = (() => {
  const MAX_VISIBLE = 4; // cap the stack; oldest is dropped past this
  const AGGREGATE_THRESHOLD = 3; // more than this arriving at once -> one summary toast
  const AUTO_DISMISS_MS = 12_000;
  const LEAVE_ANIMATION_MS = 240;

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function isMeaningfulCounterparty(value) {
    const text = String(value || "").trim();
    return text.length > 0 && text.toLowerCase() !== "unknown counterparty";
  }

  function senderName(sender) {
    const raw = String(sender || "").trim();
    if (!raw) return "";
    // Prefer a display name ("Jane Doe <jane@acme.com>"), else the address local part.
    const angle = raw.match(/^\s*"?([^"<]+?)"?\s*</);
    if (angle && angle[1].trim()) return angle[1].trim();
    const email = raw.match(/[^\s<>]+@[^\s<>]+/);
    if (email) return email[0];
    return raw;
  }

  function matterTitle(matter) {
    if (isMeaningfulCounterparty(matter.counterparty)) return `New NDA from ${matter.counterparty}`;
    const sender = senderName(matter.sender);
    if (sender) return `New NDA from ${sender}`;
    return "New NDA in your inbox";
  }

  function matterSubtitle(matter) {
    return (
      matter.attachment_filename ||
      matter.source_filename ||
      matter.subject ||
      matter.message_snippet ||
      "NDA document"
    );
  }

  function timeAgo(isoValue) {
    const then = Date.parse(String(isoValue || ""));
    if (Number.isNaN(then)) return "Just now";
    const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 45) return "Just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    return `${days}d ago`;
  }

  function createController({ container, openMatter, openRepository, fetchMatters }) {
    let seeded = false;
    const seen = new Set();
    // Matter ids whose review we've ALREADY toasted as failed, so a repeated poll
    // that keeps reporting review_status="failed" fires the toast only once.
    const failedReviewSeen = new Set();

    function inboundMatters(matters) {
      return (Array.isArray(matters) ? matters : []).filter(
        (matter) => matter && matter.source_type === "gmail_inbound" && matter.id,
      );
    }

    // True when the matter's async AI review has transitioned to a hard failure.
    // The backend stamps review_status="failed" (with a human-readable review_error
    // reason) -- e.g. a scanned-PDF that can't be parsed, an AI-reviewer outage, or
    // the in_progress->failed TTL staleness override. Applies to ANY matter the
    // owner can see, not just gmail_inbound ones (a generated/manual NDA can fail
    // review too).
    function isFailedReview(matter) {
      return Boolean(
        matter && matter.id && String(matter.review_status || "") === "failed",
      );
    }

    // A best-available human label for the failed matter: the derived counterparty,
    // else the sender, else a neutral fallback. Mirrors matterTitle's name logic so
    // the failure toast names the matter the same way the arrival toast did.
    function matterLabel(matter) {
      if (isMeaningfulCounterparty(matter.counterparty)) return String(matter.counterparty).trim();
      const sender = senderName(matter.sender);
      if (sender) return sender;
      return (
        matter.subject ||
        matter.attachment_filename ||
        matter.source_filename ||
        "your NDA"
      );
    }

    // Detect newly-FAILED reviews and toast them. The first observe SEEDS the
    // failed-set silently (alongside the inbound seed) so an inbox that ALREADY
    // holds failed reviews never floods the screen on page load -- only a review
    // that transitions to failed DURING the session toasts, exactly once.
    function observeFailures(matters, seedOnly) {
      const failed = (Array.isArray(matters) ? matters : []).filter(isFailedReview);
      if (seedOnly) {
        failed.forEach((matter) => failedReviewSeen.add(String(matter.id)));
        return;
      }
      failed.forEach((matter) => {
        const id = String(matter.id);
        if (failedReviewSeen.has(id)) return;
        failedReviewSeen.add(id);
        showReviewFailedToast(matter);
      });
    }

    // Detect newly-arrived inbound matters and toast them. The first call seeds the
    // seen-set silently (no toasts) so the existing inbox never floods on page load.
    function observe(matters) {
      const inbound = inboundMatters(matters);
      if (!seeded) {
        inbound.forEach((matter) => seen.add(String(matter.id)));
        observeFailures(matters, /* seedOnly */ true);
        seeded = true;
        return;
      }
      observeFailures(matters, /* seedOnly */ false);
      const fresh = inbound.filter((matter) => !seen.has(String(matter.id)));
      if (!fresh.length) return;
      fresh.forEach((matter) => seen.add(String(matter.id)));
      fresh.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
      if (fresh.length > AGGREGATE_THRESHOLD) {
        showAggregateToast(fresh);
      } else {
        fresh.forEach((matter) => showMatterToast(matter));
      }
    }

    async function poll() {
      if (typeof fetchMatters !== "function") return;
      try {
        observe(await fetchMatters());
      } catch (error) {
        // A transient network blip just means we try again on the next tick.
      }
    }

    function enforceStackCap() {
      if (!container) return;
      // Toasts already animating out (".toast--leaving") still linger in the DOM
      // until their leave animation ends. Counting them would inflate the visible
      // stack and prematurely evict toasts the user can still see, so cap only the
      // not-yet-leaving toasts.
      const toasts = container.querySelectorAll(".toast:not(.toast--leaving)");
      for (let index = 0; index < toasts.length - MAX_VISIBLE; index += 1) {
        removeToast(toasts[index]);
      }
    }

    function removeToast(toast) {
      if (!toast || toast.dataset.leaving === "true") return;
      toast.dataset.leaving = "true";
      toast.classList.add("toast--leaving");
      window.setTimeout(() => toast.remove(), LEAVE_ANIMATION_MS);
    }

    function mountToast(node, onActivate) {
      if (!container) return;
      const dismiss = () => removeToast(node);
      let dismissTimer = window.setTimeout(dismiss, AUTO_DISMISS_MS);
      node.addEventListener("mouseenter", () => window.clearTimeout(dismissTimer));
      node.addEventListener("mouseleave", () => {
        dismissTimer = window.setTimeout(dismiss, AUTO_DISMISS_MS);
      });
      node.querySelector("[data-toast-close]")?.addEventListener("click", (event) => {
        event.stopPropagation();
        window.clearTimeout(dismissTimer);
        dismiss();
      });
      node.querySelector("[data-toast-open]")?.addEventListener("click", () => {
        window.clearTimeout(dismissTimer);
        dismiss();
        onActivate();
      });
      container.appendChild(node);
      enforceStackCap();
    }

    function showMatterToast(matter) {
      const node = document.createElement("div");
      node.className = "toast";
      node.setAttribute("role", "status");
      node.dataset.toastMatterId = String(matter.id);
      node.innerHTML = `
        <button class="toast-close" type="button" data-toast-close aria-label="Dismiss notification">&times;</button>
        <button class="toast-open" type="button" data-toast-open>
          <span class="toast-icon" aria-hidden="true">\u{1F4E9}</span>
          <span class="toast-body">
            <span class="toast-title">${esc(matterTitle(matter))}</span>
            <span class="toast-subtitle">${esc(matterSubtitle(matter))}</span>
            <span class="toast-meta">${esc(timeAgo(matter.created_at))} · Click to review →</span>
          </span>
        </button>
      `;
      mountToast(node, () => {
        if (typeof openMatter === "function") openMatter(String(matter.id));
      });
    }

    // A top-right ERROR toast for a matter whose AI review FAILED. Reuses the same
    // toast machinery (mountToast / esc / stack cap / auto-dismiss) as the arrival
    // toasts, styled red via the `toast--error` modifier. Names the matter and
    // surfaces the backend's human-readable failure reason (review_error), and is
    // clickable to open the matter so the user can retry the review.
    function showReviewFailedToast(matter) {
      const label = matterLabel(matter);
      const reason = String(matter.review_error || "").trim();
      const node = document.createElement("div");
      node.className = "toast toast--error";
      // role="alert" (vs the arrival toast's "status") so assistive tech announces
      // the failure assertively rather than politely.
      node.setAttribute("role", "alert");
      node.dataset.toastReviewFailedId = String(matter.id);
      node.innerHTML = `
        <button class="toast-close" type="button" data-toast-close aria-label="Dismiss notification">&times;</button>
        <button class="toast-open" type="button" data-toast-open>
          <span class="toast-icon" aria-hidden="true">\u{26A0}\u{FE0F}</span>
          <span class="toast-body">
            <span class="toast-title">Review failed — ${esc(label)}</span>
            <span class="toast-subtitle toast-subtitle--wrap">${esc(reason || "The AI review could not be completed.")}</span>
            <span class="toast-meta">Click to open →</span>
          </span>
        </button>
      `;
      mountToast(node, () => {
        if (typeof openMatter === "function") openMatter(String(matter.id));
      });
    }

    function showAggregateToast(fresh) {
      const newest = fresh[0];
      const node = document.createElement("div");
      node.className = "toast";
      node.setAttribute("role", "status");
      node.dataset.toastAggregate = String(fresh.length);
      node.innerHTML = `
        <button class="toast-close" type="button" data-toast-close aria-label="Dismiss notification">&times;</button>
        <button class="toast-open" type="button" data-toast-open>
          <span class="toast-icon" aria-hidden="true">\u{1F4E9}</span>
          <span class="toast-body">
            <span class="toast-title">${esc(fresh.length)} new NDAs in your inbox</span>
            <span class="toast-subtitle">Latest: ${esc(matterTitle(newest).replace(/^New NDA from /, ""))}</span>
            <span class="toast-meta">Just now · Click to review →</span>
          </span>
        </button>
      `;
      mountToast(node, () => {
        if (typeof openRepository === "function") openRepository();
        else if (typeof openMatter === "function") openMatter(String(newest.id));
      });
    }

    // Fire an arbitrary in-app toast (not tied to an inbound matter). Reuses the
    // same toast machinery (mountToast / esc / stack cap / auto-dismiss) as the
    // inbound-arrival toasts, so callers get a notification through the one existing
    // notification mechanism rather than a bespoke banner. Used by the Review tab to
    // report "Review can't be completed — no AI reviewer available."
    function notify(title, subtitle) {
      const node = document.createElement("div");
      node.className = "toast";
      node.setAttribute("role", "status");
      node.dataset.toastAlert = "true";
      node.innerHTML = `
        <button class="toast-close" type="button" data-toast-close aria-label="Dismiss notification">&times;</button>
        <span class="toast-open toast-open--static">
          <span class="toast-icon" aria-hidden="true">\u{26A0}\u{FE0F}</span>
          <span class="toast-body">
            <span class="toast-title">${esc(title)}</span>
            ${subtitle ? `<span class="toast-subtitle">${esc(subtitle)}</span>` : ""}
          </span>
        </span>
      `;
      mountToast(node, () => {});
    }

    // Fire a transient SUCCESS toast (green ".toast--success" variant) through the
    // same toast machinery as the inbound/alert toasts. Used for the post-generate
    // "NDA generated" confirmation, which replaced the persistent green inline
    // status text in the Generator. role="status" + aria-live="polite" so a screen
    // reader announces the success without stealing focus; static (non-clickable)
    // body like notify(), auto-dismissed by mountToast.
    function notifySuccess(title, subtitle) {
      const node = document.createElement("div");
      node.className = "toast toast--success";
      node.setAttribute("role", "status");
      node.setAttribute("aria-live", "polite");
      node.dataset.toastSuccess = "true";
      node.innerHTML = `
        <button class="toast-close" type="button" data-toast-close aria-label="Dismiss notification">&times;</button>
        <span class="toast-open toast-open--static">
          <span class="toast-icon" aria-hidden="true">\u{2705}</span>
          <span class="toast-body">
            <span class="toast-title">${esc(title)}</span>
            ${subtitle ? `<span class="toast-subtitle">${esc(subtitle)}</span>` : ""}
          </span>
        </span>
      `;
      mountToast(node, () => {});
    }

    // Mark a matter's review failure as ALREADY notified, so the background poll's
    // observeFailures() won't fire a SECOND (duplicate) review-failed toast for it.
    // The foreground review workstation calls this right after it pops its own
    // immediate failure toast (it resolves the review faster than the inbox poll),
    // routing both the immediate and the polled paths through the one seen-set.
    // Idempotent; a falsy id is a no-op.
    function markReviewFailureNotified(matterId) {
      const id = String(matterId == null ? "" : matterId);
      if (id) failedReviewSeen.add(id);
    }

    return { observe, poll, notify, notifySuccess, markReviewFailureNotified };
  }

  return { createController };
})();

function createNotificationsController(options) {
  return NotificationsView.createController(options);
}
