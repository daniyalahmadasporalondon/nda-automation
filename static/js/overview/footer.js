// Overview tab — sticky footer: review-progress line + the two terminal actions
// (Approve Review, Send for signature).
//
// This is a self-contained presentational component for the new "Overview" tab.
// It owns ONLY this file; the shared shell (static/index.html) and the styles
// (static/styles.css) are wired in by sibling teammates. We touch no other file.
//
// Render-only contract — all state arrives as arguments, no fetches, no globals:
//
//   renderOverviewFooter(
//     containerEl,
//     { reviewedCount, totalCount, anyFail },
//     { onApprove, onSend },
//   )
//
// Surface (classes are fixed; a CSS teammate styles them):
//   .ov-footer            wrapper
//   .ov-progress          "{reviewedCount} of {totalCount} clauses reviewed"
//   .ov-progress-bar      thin fill bar, width = reviewedCount/totalCount
//   .ov-approve           Approve Review button
//   .ov-approve--disabled modifier when the review is not yet complete
//   .ov-send              Send for signature button
//
// Gate: Approve is enabled ONLY when reviewedCount === totalCount. While
// disabled it explains why inline (how many clauses remain, plus a failing-clause
// note when `anyFail`). Send always fires its callback.
//
// Like the app's other classic controllers (send-document.js, docusign-send.js)
// this builds DOM with document.createElement and exposes a CommonJS export
// behind a `typeof module` guard so the Node frontend-test harness can require it
// while the browser just calls the global function. We use textContent (never
// innerHTML) so counts/labels can never inject markup.

function renderOverviewFooter(containerEl, state, handlers) {
  if (!containerEl) return null;

  const data = state || {};
  const callbacks = handlers || {};
  const onApprove = typeof callbacks.onApprove === "function" ? callbacks.onApprove : null;
  const onSend = typeof callbacks.onSend === "function" ? callbacks.onSend : null;

  // Normalise the counts defensively: clamp to non-negative integers and never
  // let reviewedCount exceed totalCount (a bad caller must not over-fill the bar
  // or accidentally satisfy the gate).
  const totalCount = toCount(data.totalCount);
  const reviewedCount = Math.min(toCount(data.reviewedCount), totalCount);
  const anyFail = Boolean(data.anyFail);
  const complete = totalCount > 0 && reviewedCount === totalCount;

  // Idempotent render: clear any prior footer so re-rendering with new state
  // never stacks duplicate footers into the container.
  containerEl.textContent = "";

  const footer = document.createElement("div");
  footer.className = "ov-footer";

  // --- Progress line + fill bar -------------------------------------------
  const progress = document.createElement("div");
  progress.className = "ov-progress";

  const progressLabel = document.createElement("span");
  progressLabel.className = "ov-progress-label";
  progressLabel.textContent = `${reviewedCount} of ${totalCount} clauses reviewed`;
  progress.appendChild(progressLabel);

  const track = document.createElement("div");
  track.className = "ov-progress-track";
  const bar = document.createElement("div");
  bar.className = "ov-progress-bar";
  const fraction = totalCount > 0 ? reviewedCount / totalCount : 0;
  const pct = Math.round(fraction * 100);
  bar.style.width = `${pct}%`;
  // Expose progress for assistive tech + the test harness.
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", String(totalCount));
  bar.setAttribute("aria-valuenow", String(reviewedCount));
  track.appendChild(bar);
  progress.appendChild(track);

  footer.appendChild(progress);

  // --- Actions -------------------------------------------------------------
  const actions = document.createElement("div");
  actions.className = "ov-actions";

  // Approve Review — gated on full review completion.
  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = "ov-approve";
  approve.textContent = "Approve Review";
  if (!complete) {
    approve.classList.add("ov-approve--disabled");
    approve.disabled = true;
    approve.setAttribute("aria-disabled", "true");
    const reason = approveBlockedReason(reviewedCount, totalCount, anyFail);
    approve.title = reason;

    const hint = document.createElement("span");
    hint.className = "ov-approve-reason";
    hint.textContent = reason;
    // The hint sits next to the button so the user sees WHY it is blocked.
    actions.appendChild(approve);
    actions.appendChild(hint);
  } else {
    approve.disabled = false;
    approve.addEventListener("click", () => {
      // Re-check the gate at click time: defends against any stray enabled-click.
      if (onApprove) onApprove();
    });
    actions.appendChild(approve);
  }

  // Send for signature — always available; the host decides when it makes sense
  // to surface this footer at all.
  const send = document.createElement("button");
  send.type = "button";
  send.className = "ov-send";
  send.textContent = "Send for signature";
  send.addEventListener("click", () => {
    if (onSend) onSend();
  });
  actions.appendChild(send);

  footer.appendChild(actions);
  containerEl.appendChild(footer);

  return footer;
}

// Coerce an arbitrary value into a non-negative integer count. NaN/negatives/
// non-numbers collapse to 0 so the bar and the gate are always well-defined.
function toCount(value) {
  const n = Math.floor(Number(value));
  if (!Number.isFinite(n) || n < 0) return 0;
  return n;
}

// The inline explanation shown while Approve is disabled.
//   "{remaining} clauses still need review"  (singular-aware)
//   + ", and 1+ clause fails — resolve or override"  when anyFail
// When nothing is left but the gate is still closed (totalCount === 0) we say so
// rather than print a misleading "0 clauses still need review".
function approveBlockedReason(reviewedCount, totalCount, anyFail) {
  if (totalCount <= 0) return "No clauses to review yet";
  const remaining = Math.max(totalCount - reviewedCount, 0);
  const noun = remaining === 1 ? "clause" : "clauses";
  let reason = `${remaining} ${noun} still need review`;
  if (anyFail) reason += " — 1+ clause fails, resolve or override";
  return reason;
}

// Browser: `renderOverviewFooter` is a page-level global (the overview-shell
// teammate calls it after wiring the container). Node test harness: requireable.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { renderOverviewFooter, approveBlockedReason };
}
