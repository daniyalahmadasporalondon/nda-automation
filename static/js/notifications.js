// In-app toast notifications for newly-arrived inbound NDAs AND reviewer
// attention transitions.
//
// The server-side Gmail scheduler imports inbound NDAs on the configured cadence,
// so new matters appear in /api/matters on their own. This controller watches the
// matter list (fed by the repository poll on the Repository tab, and by its own
// lightweight poll on every other tab) and pops a top-right toast for each newly
// seen `gmail_inbound` matter. Clicking a toast opens that matter for review.
//
// REVIEWER NOTIFICATIONS (TIER 1, in-app only): the same poll also drives
// `observeTransitions(matters)`. It snapshots each visible matter's "attention
// state" client-side and, on the NEXT poll, diffs against that snapshot. Only a
// TRANSITION INTO an attention state (-> needs review / review failed / send
// failed) is an event: it pops a single (collapsed) corner toast AND bumps a
// persistent unread-count badge so a faded toast is never lost. A transition into
// a clean/ready/signed state is FYI -- it never toasts and never bumps the count.
// "Still needs review" across polls is not an event; only the transition is. This
// RIDES THE EXISTING BOARD POLL -- there is no new polling loop and no backend feed.
//
// GOLDEN RULE: the first observation after load SEEDS the snapshot/seen-sets
// silently, so the existing inbox / already-flagged matters never flood the screen
// on page load -- only genuine session transitions notify. Detection is purely
// client-side; no backend changes.
const NotificationsView = (() => {
  const MAX_VISIBLE = 4; // cap the stack; oldest is dropped past this
  const AGGREGATE_THRESHOLD = 3; // more than this arriving at once -> one summary toast
  const AUTO_DISMISS_MS = 12_000;
  // Attention transition toasts are LOUD-but-transient: a shorter ~6s auto-dismiss
  // (the persistent badge is the durable signal, the toast is just the nudge).
  const ATTENTION_DISMISS_MS = 6_000;
  const LEAVE_ANIMATION_MS = 240;
  const UNREAD_STORAGE_KEY = "ndaReviewerUnread";

  // The matter-utils predicates live on `window` (published by the module bridge)
  // in the app, but a test may load this file in isolation. Resolve lazily and fall
  // back to reading the same raw fields the predicates read, so the transition
  // taxonomy works with or without the bridge present.
  function matterUtils() {
    return (typeof window !== "undefined" && window.MatterUtils) || null;
  }
  function reviewInProgress(matter) {
    const utils = matterUtils();
    if (utils && typeof utils.reviewInProgress === "function") return utils.reviewInProgress(matter);
    return String(matter?.review_status || "") === "in_progress";
  }
  function reviewFailed(matter) {
    const utils = matterUtils();
    if (utils && typeof utils.reviewFailed === "function") return utils.reviewFailed(matter);
    return String(matter?.review_status || "") === "failed";
  }
  function needsHumanReview(matter) {
    const utils = matterUtils();
    if (utils && typeof utils.needsHumanReview === "function") return utils.needsHumanReview(matter);
    const overall = String(matter?.review_result?.overall_status || matter?.overall_status || "");
    const count = Number(matter?.requirements_needs_review ?? matter?.review_result?.requirements_needs_review ?? 0);
    return overall === "needs_review" || count > 0;
  }

  // A background send failed. The board payload does not yet carry a dedicated
  // field, so we read whatever the backend may stamp (without inventing a new
  // required status): an explicit send_status/send failure, or a workflow_state
  // that reports a send error. Absent any of these, a matter simply has no
  // send-failure signal and never trips this branch.
  function sendFailed(matter) {
    if (String(matter?.send_status || "") === "failed") return true;
    if (matter?.send_failed === true) return true;
    const workflow = matter && typeof matter.workflow_state === "object" ? matter.workflow_state : null;
    if (workflow && (workflow.status === "send_failed" || workflow.send_status === "failed")) return true;
    return false;
  }

  // The single source of a matter's attention CLASSIFICATION used for diffing. We
  // return a stable string token so a TRANSITION is just "token changed". The order
  // is a priority: a failed review/send outranks a plain needs-review.
  //   review_failed / send_failed / needs_review  -> ATTENTION (loud)
  //   reviewing                                    -> neutral (in-flight, not an event)
  //   clean                                        -> FYI (quiet; good/steady news)
  const ATTENTION_STATES = new Set(["review_failed", "send_failed", "needs_review"]);
  function attentionState(matter) {
    if (sendFailed(matter)) return "send_failed";
    if (reviewFailed(matter)) return "review_failed";
    if (reviewInProgress(matter)) return "reviewing";
    // human_reviewed clears the needs-review attention (the reviewer has acted).
    if (needsHumanReview(matter) && !matter?.human_reviewed) return "needs_review";
    return "clean";
  }
  function isAttention(stateToken) {
    return ATTENTION_STATES.has(stateToken);
  }

  const ATTENTION_LABELS = {
    review_failed: "Review failed",
    send_failed: "Send failed",
    needs_review: "Needs review",
  };

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

  function shortMatterName(matter) {
    if (isMeaningfulCounterparty(matter?.counterparty)) return String(matter.counterparty);
    const subject = String(matter?.subject || matter?.document_title || "").trim();
    if (subject) return subject;
    const sender = senderName(matter?.sender);
    if (sender) return sender;
    const file = String(matter?.attachment_filename || matter?.source_filename || "").trim();
    if (file) return file;
    return "A matter";
  }

  function loadStoredUnread() {
    try {
      if (typeof localStorage === "undefined") return [];
      const raw = localStorage.getItem(UNREAD_STORAGE_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.map(String) : [];
    } catch (error) {
      return [];
    }
  }

  function persistUnread(ids) {
    try {
      if (typeof localStorage === "undefined") return;
      localStorage.setItem(UNREAD_STORAGE_KEY, JSON.stringify([...ids]));
    } catch (error) {
      // Persistence is best-effort: a quota/serialization failure must not break
      // the in-memory badge, so swallow it.
    }
  }

  function createController({ container, openMatter, openRepository, fetchMatters, bellNode, onUnreadChange }) {
    let seeded = false;
    const seen = new Set();
    // Transition tracking (reviewer notifications). `prevState` is the per-matter
    // attention-state snapshot from the PREVIOUS poll; `unread` is the persistent
    // set of matter ids the reviewer has not yet looked at since their last
    // attention transition. Unread survives the 15s refreshes (in memory) and a
    // page reload (localStorage), and is cleared on view (markSeen).
    let transitionSeeded = false;
    const prevState = new Map();
    const unread = new Set(loadStoredUnread());

    function renderBadge() {
      const count = unread.size;
      if (typeof onUnreadChange === "function") onUnreadChange(count);
      if (!bellNode) return;
      bellNode.dataset.unreadCount = String(count);
      bellNode.setAttribute("data-has-unread", count > 0 ? "true" : "false");
      const countNode = bellNode.querySelector("[data-unread-count]");
      if (countNode) {
        countNode.textContent = count > 99 ? "99+" : String(count);
        countNode.hidden = count === 0;
      }
      const label = count === 0
        ? "No matters need your attention"
        : `${count} ${count === 1 ? "matter needs" : "matters need"} your attention`;
      bellNode.setAttribute("aria-label", label);
      bellNode.title = label;
    }

    function bumpUnread(matterId) {
      const id = String(matterId);
      if (unread.has(id)) return;
      unread.add(id);
      persistUnread(unread);
      renderBadge();
    }

    // Clear a matter's unread (opening it, or clicking the bell to clear all).
    function markSeen(matterId) {
      if (matterId == null) {
        if (!unread.size) return;
        unread.clear();
        persistUnread(unread);
        renderBadge();
        return;
      }
      const id = String(matterId);
      if (!unread.delete(id)) return;
      persistUnread(unread);
      renderBadge();
    }

    // Detect meaningful state TRANSITIONS across two successive polls. The FIRST
    // call seeds the snapshot silently (no toast, no bump) so already-flagged
    // matters never notify on load. On later polls we diff each visible matter's
    // attention-state token against its previous token and react only to a
    // transition INTO an attention state. Multiple such transitions in one poll
    // COLLAPSE into a single toast ("N matters need review"). FYI transitions
    // (-> clean) and "still needs review" (token unchanged) are silent.
    function observeTransitions(matters) {
      const list = (Array.isArray(matters) ? matters : []).filter((matter) => matter && matter.id);
      const nextState = new Map();
      const transitions = [];
      list.forEach((matter) => {
        const id = String(matter.id);
        const current = attentionState(matter);
        nextState.set(id, current);
        if (!transitionSeeded) return;
        const previous = prevState.get(id);
        // A genuine transition is a CHANGE into an attention state. A matter we have
        // never seen before that arrives already-attention also counts (previous is
        // undefined != current attention token) -- it is new attention to surface.
        if (previous === current) return; // unchanged -> "still needs review" is not an event
        if (isAttention(current)) {
          transitions.push({ matter, state: current });
          bumpUnread(id);
        }
      });
      // Replace the snapshot with this poll's states. Drop matters that vanished
      // from the board (they no longer have a tracked previous state); their unread
      // (if any) persists until explicitly seen so a disappeared-then-returned
      // matter is not silently lost.
      prevState.clear();
      nextState.forEach((value, key) => prevState.set(key, value));
      if (!transitionSeeded) {
        transitionSeeded = true;
        renderBadge();
        return;
      }
      if (transitions.length) showTransitionToast(transitions);
    }

    function showTransitionToast(transitions) {
      if (transitions.length === 1) {
        showSingleTransitionToast(transitions[0]);
      } else {
        showCollapsedTransitionToast(transitions);
      }
    }

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
        const matters = await fetchMatters();
        observe(matters);
        // Drive the reviewer-attention transitions from the same off-tab poll so the
        // unread bell keeps counting app-wide (the on-tab board poll calls
        // observeTransitions directly).
        observeTransitions(matters);
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

    function mountToast(node, onActivate, dismissMs = AUTO_DISMISS_MS) {
      if (!container) return;
      const dismiss = () => removeToast(node);
      let dismissTimer = window.setTimeout(dismiss, dismissMs);
      node.addEventListener("mouseenter", () => window.clearTimeout(dismissTimer));
      node.addEventListener("mouseleave", () => {
        dismissTimer = window.setTimeout(dismiss, dismissMs);
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

    // One matter transitioned into an attention state: "Acme NDA -> Needs review",
    // clickable to jump straight to that matter. role="alert" + assertive so the
    // attention nudge is announced (vs the polite "new inbound" status toast).
    function showSingleTransitionToast({ matter, state }) {
      const label = ATTENTION_LABELS[state] || "Needs attention";
      const node = document.createElement("div");
      node.className = "toast toast--attention";
      node.setAttribute("role", "alert");
      node.setAttribute("aria-live", "assertive");
      node.dataset.toastAttentionId = String(matter.id);
      node.dataset.toastAttentionState = state;
      node.innerHTML = `
        <button class="toast-close" type="button" data-toast-close aria-label="Dismiss notification">&times;</button>
        <button class="toast-open" type="button" data-toast-open>
          <span class="toast-icon" aria-hidden="true">\u{1F514}</span>
          <span class="toast-body">
            <span class="toast-title">${esc(shortMatterName(matter))} → ${esc(label)}</span>
            <span class="toast-meta">Click to review →</span>
          </span>
        </button>
      `;
      mountToast(node, () => {
        markSeen(String(matter.id));
        if (typeof openMatter === "function") openMatter(String(matter.id));
      }, ATTENTION_DISMISS_MS);
    }

    // Multiple matters transitioned in one poll: ONE collapsed toast
    // ("3 matters need review"), not N. Clicking opens the board.
    function showCollapsedTransitionToast(transitions) {
      const count = transitions.length;
      const allNeedReview = transitions.every((t) => t.state === "needs_review");
      const headline = allNeedReview
        ? `${count} matters need review`
        : `${count} matters need attention`;
      const node = document.createElement("div");
      node.className = "toast toast--attention";
      node.setAttribute("role", "alert");
      node.setAttribute("aria-live", "assertive");
      node.dataset.toastAttentionCount = String(count);
      node.innerHTML = `
        <button class="toast-close" type="button" data-toast-close aria-label="Dismiss notification">&times;</button>
        <button class="toast-open" type="button" data-toast-open>
          <span class="toast-icon" aria-hidden="true">\u{1F514}</span>
          <span class="toast-body">
            <span class="toast-title">${esc(headline)}</span>
            <span class="toast-meta">Click to review →</span>
          </span>
        </button>
      `;
      mountToast(node, () => {
        if (typeof openRepository === "function") openRepository();
      }, ATTENTION_DISMISS_MS);
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

    // Clicking the bell clears the whole unread count (the reviewer is now looking)
    // and jumps to the board so they can triage the flagged matters.
    bellNode?.addEventListener("click", () => {
      markSeen(null);
      if (typeof openRepository === "function") openRepository();
    });
    // Reflect any reload-persisted unread count immediately on construction.
    renderBadge();

    return { observe, observeTransitions, poll, notify, markSeen, unreadCount: () => unread.size };
  }

  return { createController };
})();

function createNotificationsController(options) {
  return NotificationsView.createController(options);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { NotificationsView, createNotificationsController };
}
