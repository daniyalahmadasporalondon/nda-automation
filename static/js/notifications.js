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

    function inboundMatters(matters) {
      return (Array.isArray(matters) ? matters : []).filter(
        (matter) => matter && matter.source_type === "gmail_inbound" && matter.id,
      );
    }

    // Detect newly-arrived inbound matters and toast them. The first call seeds the
    // seen-set silently (no toasts) so the existing inbox never floods on page load.
    function observe(matters) {
      const inbound = inboundMatters(matters);
      if (!seeded) {
        inbound.forEach((matter) => seen.add(String(matter.id)));
        seeded = true;
        return;
      }
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

    return { observe, poll, notify };
  }

  return { createController };
})();

function createNotificationsController(options) {
  return NotificationsView.createController(options);
}
