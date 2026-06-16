// Overview tab — sticky footer: the two terminal actions (Approve Review,
// Send for signature).
//
// This is a self-contained presentational component for the new "Overview" tab.
// It owns ONLY this file; the shared shell (static/index.html) and the styles
// (static/styles.css) are wired in by sibling teammates. We touch no other file.
//
// Render-only contract — all state arrives as arguments, no fetches, no globals:
//
//   renderOverviewFooter(
//     containerEl,
//     { approveDisabled, approveReason },
//     { onApprove, onSend },
//   )
//
// The Approve gate is NOT computed here. The shell decides `approveDisabled`
// from the app's existing stale-playbook / review logic and passes the boolean
// (plus an optional human-readable `approveReason`) in. This component only
// renders that decision and routes clicks.
//
// Surface (classes are fixed; a CSS teammate styles them):
//   .ov-footer            wrapper
//   .ov-approve           Approve Review button
//   .ov-approve--disabled modifier when `approveDisabled` is true
//   .ov-approve-reason    inline explanation shown while disabled
//   .ov-send              Send for signature button
//
// Like the app's other classic controllers (send-document.js, docusign-send.js)
// this builds DOM with document.createElement and exposes a CommonJS export
// behind a `typeof module` guard so the Node frontend-test harness can require it
// while the browser just calls the global function. We use textContent (never
// innerHTML) so the reason label can never inject markup.

function renderOverviewFooter(containerEl, state, handlers) {
  if (!containerEl) return null;

  const data = state || {};
  const callbacks = handlers || {};
  const onApprove = typeof callbacks.onApprove === "function" ? callbacks.onApprove : null;
  const onSend = typeof callbacks.onSend === "function" ? callbacks.onSend : null;

  const approveDisabled = Boolean(data.approveDisabled);
  const approveReason = data.approveReason == null ? "" : String(data.approveReason);

  // Idempotent render: clear any prior footer so re-rendering with new state
  // never stacks duplicate footers into the container.
  containerEl.textContent = "";

  const footer = document.createElement("div");
  footer.className = "ov-footer";

  const actions = document.createElement("div");
  actions.className = "ov-actions";

  // Approve Review — the gate decision arrives from the shell via
  // `approveDisabled`; this component does not compute it.
  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = "ov-approve";
  approve.textContent = "Approve Review";
  if (approveDisabled) {
    approve.classList.add("ov-approve--disabled");
    approve.disabled = true;
    approve.setAttribute("aria-disabled", "true");
    if (approveReason) approve.title = approveReason;
    actions.appendChild(approve);

    // Only surface an inline hint when the shell gave us a reason to show.
    if (approveReason) {
      const hint = document.createElement("span");
      hint.className = "ov-approve-reason";
      hint.textContent = approveReason;
      actions.appendChild(hint);
    }
  } else {
    approve.disabled = false;
    approve.addEventListener("click", () => {
      if (onApprove) onApprove();
    });
    actions.appendChild(approve);
  }

  // Send for signature — always available; the host decides when to surface
  // this footer at all.
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

// Browser: `renderOverviewFooter` is a page-level global (the overview-shell
// teammate calls it after wiring the container). Node test harness: requireable.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { renderOverviewFooter };
}
