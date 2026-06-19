// "Send for signature" action on a reviewed/approved matter in the review
// workstation.
//
// Opens a small modal with a signer + signing-order chooser (counterparty +
// Aspora signatory, prefilled), POSTs the DocuSign REST contract, then shows the
// envelope status. A "Download signed" link appears once the envelope is
// completed:
//   POST /api/matters/<id>/send-for-signature  body {signers?, signing_order?}
//        -> { envelope_id, status }
//   GET  /api/matters/<id>/signature-status     -> { status }
//   GET  /api/matters/<id>/signed-document       -> the completed PDF
//
// The badge/status decisions live in DocuSignModel.signatureView so the browser
// path is the path the frontend test exercises.
function createDocuSignSendController({
  modalNode,
  closeButton,
  cancelButton,
  form,
  signerRows,
  signingOrderControl,
  statusNode,
  badgeNode,
  headerBadgeNode,
  envelopeNode,
  downloadSignedLink,
  submitButton,
  triggerButton,
  getMatter,
  // Optional progressive-disclosure gate. When supplied and it returns false for
  // the current matter, the trigger button stays hidden even though a matter is
  // loaded. The Generator passes nothing (always shown once an NDA exists).
  isTriggerVisible,
  // Optional ENABLE gate (no-jump header). When supplied and it returns false for
  // the current matter, the trigger button stays PRESENT but DISABLED/grayed
  // instead of hidden — so it never appears/disappears between states. The Review
  // workstation passes a gate keyed on ai_review_ran (nothing to send before a
  // review has run); the Generator passes nothing (always enabled once an NDA
  // exists).
  isTriggerEnabled,
  getAsporaSignatory,
  reviewErrorFromPayload,
  downloadUrl,
  onMatterUpdated,
}) {
  // Resolve the shared model LAZILY on each use, not once at construction. The
  // model is exposed on window by the deferred global-bridge module, which runs
  // AFTER this classic controller is constructed in app.js — capturing it here
  // once would freeze a null reference and silently disable status rendering.
  function model() {
    if (typeof window !== "undefined" && window.DocuSignModel) return window.DocuSignModel;
    return typeof DocuSignModel !== "undefined" ? DocuSignModel : null;
  }
  let busy = false;
  let previousFocus = null;
  let pollTimer = null;

  triggerButton?.addEventListener("click", () => openComposer());
  closeButton?.addEventListener("click", () => closeComposer());
  cancelButton?.addEventListener("click", () => closeComposer());
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await sendForSignature();
  });
  modalNode?.addEventListener("click", (event) => {
    if (event.target === modalNode && !busy) closeComposer();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !isOpen() || busy) return;
    event.preventDefault();
    closeComposer();
  });
  downloadSignedLink?.addEventListener("click", (event) => {
    event.preventDefault();
    downloadSigned();
  });

  function html(value) {
    if (typeof window !== "undefined" && typeof window.escapeHtml === "function") return window.escapeHtml(value);
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[ch]
    ));
  }

  function currentMatter() {
    return (typeof getMatter === "function" ? getMatter() : null) || null;
  }

  function asporaSignatory() {
    return (typeof getAsporaSignatory === "function" ? getAsporaSignatory() : null) || {};
  }

  // Render the prefilled signer rows from the matter + Aspora signatory. Each row
  // carries an up/down reorder control ("who signs first"): the order is taken from
  // the row's POSITION, so moving a row up/down changes the signing order that
  // collectSigners derives and the payload carries as per-signer routing_order.
  function renderSignerRows() {
    if (!signerRows) return;
    const matter = currentMatter();
    const m = model();
    const signers = m
      ? m.defaultSigners(matter || {}, { asporaSignatory: asporaSignatory() })
      : [];
    drawSignerRows(signers);
  }

  // Paint the given signer list into the rows DOM, numbering by position and
  // disabling the up control on the first row / down control on the last.
  function drawSignerRows(signers) {
    if (!signerRows) return;
    const total = signers.length;
    signerRows.innerHTML = signers.map((signer, index) => `
      <div class="docusign-signer-row" data-docusign-signer="${index}" draggable="false">
        <span class="docusign-signer-order" data-docusign-signer-order>${index + 1}</span>
        <div class="docusign-signer-reorder" role="group" aria-label="Signing order for ${html(signer.name)}">
          <button type="button" class="docusign-signer-move docusign-signer-move-up" data-docusign-move="up" aria-label="Move ${html(signer.name)} earlier in signing order" title="Sign earlier"${index === 0 ? " disabled" : ""}>
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 5v14M5 12l7-7 7 7"/></svg>
          </button>
          <button type="button" class="docusign-signer-move docusign-signer-move-down" data-docusign-move="down" aria-label="Move ${html(signer.name)} later in signing order" title="Sign later"${index === total - 1 ? " disabled" : ""}>
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 5v14M5 12l7 7 7-7"/></svg>
          </button>
        </div>
        <label class="docusign-signer-field">
          <span>${signer.role === "aspora" ? "Aspora signatory" : "Counterparty"} name</span>
          <input type="text" data-docusign-signer-name autocomplete="off" value="${html(signer.name)}" data-docusign-role="${html(signer.role)}">
        </label>
        <label class="docusign-signer-field">
          <span>Email</span>
          <input type="email" data-docusign-signer-email autocomplete="off" value="${html(signer.email)}">
        </label>
      </div>
    `).join("");
  }

  // Move the signer at `fromIndex` one step in `direction` ("up"/"down"),
  // preserving any edits the user made to the rows (we read the live values back
  // out, reorder, and repaint). Clamped at the ends.
  function moveSigner(fromIndex, direction) {
    const rows = collectSigners();
    const toIndex = direction === "up" ? fromIndex - 1 : fromIndex + 1;
    if (toIndex < 0 || toIndex >= rows.length) return;
    const [moved] = rows.splice(fromIndex, 1);
    rows.splice(toIndex, 0, moved);
    drawSignerRows(rows);
    // Keep focus on the same control after the repaint so keyboard reordering is
    // continuous (the row moved, so the control now lives at toIndex).
    const focusButton = signerRows?.querySelector(
      `[data-docusign-signer="${toIndex}"] [data-docusign-move="${direction}"]:not([disabled])`,
    ) || signerRows?.querySelector(`[data-docusign-signer="${toIndex}"] [data-docusign-move]`);
    focusButton?.focus?.();
  }

  // Delegate reorder clicks from the rows container (rows are re-rendered, so a
  // delegated listener survives repaints where per-button listeners would not).
  signerRows?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-docusign-move]") : null;
    if (!target || target.disabled) return;
    event.preventDefault();
    const row = target.closest("[data-docusign-signer]");
    const index = row ? Number(row.getAttribute("data-docusign-signer")) : -1;
    if (index >= 0) moveSigner(index, target.getAttribute("data-docusign-move"));
  });

  // Collect the (possibly edited + reordered) signer rows from the form. The order
  // is the row's POSITION (1-based), so a reorder changes the per-signer routing
  // order the payload carries.
  function collectSigners() {
    if (!signerRows) return [];
    return Array.from(signerRows.querySelectorAll("[data-docusign-signer]")).map((row, index) => ({
      role: row.querySelector("[data-docusign-signer-name]")?.dataset.docusignRole || "",
      name: row.querySelector("[data-docusign-signer-name]")?.value || "",
      email: row.querySelector("[data-docusign-signer-email]")?.value || "",
      order: index + 1,
    }));
  }

  function selectedSigningOrder() {
    const checked = signingOrderControl?.querySelector("[data-docusign-signing-order]:checked");
    return checked?.value === "parallel" ? "parallel" : "sequential";
  }

  function openComposer() {
    const matter = currentMatter();
    if (!matter?.id) {
      setStatus("Save this review as a matter before sending for signature.", "error");
      return;
    }
    if (!modalNode || !form) {
      setStatus("Signature composer is unavailable.", "error");
      return;
    }
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : triggerButton;
    renderSignerRows();
    setStatus("");
    // Reflect any already-sent envelope state when the composer opens.
    renderSignatureState(matter);
    modalNode.hidden = false;
    document.body.classList.add("modal-open");
    window.setTimeout(() => closeButton?.focus?.(), 0);
  }

  function closeComposer({ restoreFocus = true } = {}) {
    if (!modalNode) return;
    modalNode.hidden = true;
    document.body.classList.remove("modal-open");
    stopPolling();
    if (restoreFocus) {
      const target = previousFocus?.isConnected ? previousFocus : triggerButton;
      target?.focus?.();
    }
    previousFocus = null;
  }

  function isOpen() {
    return Boolean(modalNode && !modalNode.hidden);
  }

  async function sendForSignature() {
    if (busy) return;
    const matter = currentMatter();
    if (!matter?.id) {
      setStatus("Save this review as a matter before sending for signature.", "error");
      return;
    }
    // #30: never POST a second envelope for a matter already out for signature.
    // Once a non-terminal envelope exists the send is a no-op — reflect the
    // already-sent state instead of creating a duplicate. (A terminal envelope —
    // completed/declined/voided — is NOT active, so a legitimate resend is still
    // allowed.) The server enforces the same rule (409) as the real backstop.
    if (hasActiveEnvelope(matter)) {
      renderSignatureState(matter);
      setStatus("This NDA has already been sent for signature.", "success");
      return;
    }
    const m = model();
    const validation = m
      ? m.validateSigners(collectSigners())
      : { ok: true, signers: collectSigners(), error: "" };
    if (!validation.ok) {
      setStatus(validation.error, "error");
      return;
    }
    const payload = m
      ? m.buildSendForSignaturePayload(validation.signers, selectedSigningOrder())
      : { signers: validation.signers, signing_order: selectedSigningOrder() };

    busy = true;
    setSubmitting(true);
    setStatus("Sending for signature.");
    try {
      const response = await fetch(`/api/matters/${encodeURIComponent(matter.id)}/send-for-signature`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await window.AuthExpired.parseOkJson(response, "Could not send for signature", reviewErrorFromPayload);
      const envelopeId = result.envelope_id || matterEnvelopeId(matter) || "";
      const status = result.status || "sent";
      const merged = {
        ...matter,
        // Write the canonical nested field the backend persists/exposes so this
        // in-session matter matches a freshly-fetched one. Keep the flat fields
        // as an in-session fallback for any consumer not yet on the nested path.
        docusign: { ...(matter.docusign || {}), envelope_id: envelopeId, status },
        signature_envelope_id: envelopeId,
        signature_status: status,
      };
      applyMatterUpdate(merged);
      renderSignatureState(merged);
      setStatus(`Sent for signature. Envelope ${envelopeId}.`.trim(), "success");
      // Begin polling the live status so the badge advances to Signed without a reload.
      startPolling(matter.id);
    } catch (error) {
      // #31: a 409 needs_connect is a guided dead-end, not a generic failure —
      // tell the user DocuSign isn't connected and where to connect it, with a
      // clickable link to the connect flow when the server supplied one.
      if (error?.needsConnect) {
        setConnectNeeded(error.connectUrl);
      } else {
        setStatus(error.message || "Could not send for signature.", "error");
      }
    } finally {
      busy = false;
      setSubmitting(false);
    }
  }

  // Poll GET /api/matters/<id>/signature-status until the envelope reaches a
  // terminal state (completed/declined/voided) or the composer closes.
  function startPolling(matterId) {
    stopPolling();
    pollTimer = window.setInterval(() => refreshStatus(matterId), 8000);
  }

  function stopPolling() {
    if (pollTimer != null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // Re-sync the live envelope status from the server (GET /signature-status), which
  // re-fetches the REAL DocuSign status (spoof-proof) and, on `completed`, flips the
  // matter to executed via the same shared lifecycle path the webhook uses. Used by
  // BOTH the send-modal poll loop and the on-demand "Refresh status" button.
  //
  // Returns a small result object so an on-demand caller can react WITHOUT
  // re-issuing the fetch: { ok, status, completed, terminal, needsConnect,
  // connectUrl }. The poll loop ignores the return value (it only needs the
  // side-effects). A 409 needs_connect/needs_reconnect surfaces ok:false +
  // needsConnect:true so the caller can route to setConnectNeeded.
  async function refreshStatus(matterId) {
    try {
      const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/signature-status`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        // 409 needs_connect / needs_reconnect — the grant is dead. Surface it so an
        // on-demand caller routes the user to (re)connect; the poll loop ignores it.
        const needsConnect = Boolean(payload && payload.needs_connect);
        return {
          ok: false,
          status: "",
          completed: false,
          terminal: false,
          needsConnect,
          connectUrl: needsConnect ? String(payload.connect_url || "") : "",
        };
      }
      const matter = currentMatter();
      if (!matter?.id || matter.id !== matterId) {
        return { ok: true, status: payload.status || "", completed: Boolean(payload.completed), terminal: false, needsConnect: false, connectUrl: "" };
      }
      const nextStatus = payload.status || matterView(matter).status;
      const merged = {
        ...matter,
        // Keep the canonical nested field in sync on each poll, not just the flat one.
        docusign: { ...(matter.docusign || {}), status: nextStatus },
        signature_status: nextStatus,
      };
      applyMatterUpdate(merged);
      renderSignatureState(merged);
      const m = model();
      const view = m ? m.signatureView(payload) : null;
      if (view?.terminal) stopPolling();
      return {
        ok: true,
        status: nextStatus,
        completed: Boolean(payload.completed) || Boolean(view?.completed),
        terminal: Boolean(view?.terminal),
        needsConnect: false,
        connectUrl: "",
      };
    } catch (error) {
      // Transient network errors are ignored; the next tick retries.
      return { ok: false, status: "", completed: false, terminal: false, needsConnect: false, connectUrl: "" };
    }
  }

  function applyMatterUpdate(matter) {
    if (typeof onMatterUpdated === "function") onMatterUpdated(matter);
  }

  // The canonical signature view/envelope-id for a matter: the backend persists
  // the envelope state NESTED at matter.docusign = {envelope_id, status} (the
  // durable, server-exposed source that survives a reload), with the flat
  // in-session matter.signature_* fields as a fallback. Route every read through
  // the model's nested-first accessors so a freshly-fetched matter renders its
  // real state instead of resetting to "not sent".
  function matterView(matter) {
    const m = model();
    if (m?.matterSignatureView) return m.matterSignatureView(matter);
    const status = m?.matterSignatureStatus
      ? m.matterSignatureStatus(matter)
      : String(matter?.docusign?.status || matter?.signature_status || "");
    return m
      ? m.signatureView(status)
      : { badge: "", tone: "idle", completed: false, canDownloadSigned: false, sent: false };
  }

  function matterEnvelopeId(matter) {
    const m = model();
    if (m?.matterEnvelopeId) return m.matterEnvelopeId(matter);
    return String(matter?.docusign?.envelope_id || matter?.signature_envelope_id || "");
  }

  // #30: an "active" envelope = sent AND not yet terminal. A matter with such an
  // envelope must not be re-sent (it would create a duplicate envelope to the
  // counterparty). A terminal envelope (completed/declined/voided) is NOT active,
  // so a legitimate resend after a void/decline is still permitted.
  function hasActiveEnvelope(matter) {
    if (!matterEnvelopeId(matter)) return false;
    const view = matterView(matter);
    return Boolean(view?.sent) && !view?.terminal;
  }

  // Apply the badge view (text + tone classes + visibility) to one badge node.
  function applyBadge(node, view) {
    if (!node) return;
    node.textContent = view.badge || "";
    node.hidden = !view.badge;
    node.classList.remove("ready", "pending", "blocked");
    if (view.tone === "ready") node.classList.add("ready");
    else if (view.tone === "pending") node.classList.add("pending");
    else if (view.tone === "blocked") node.classList.add("blocked");
  }

  // Render the badge + envelope id + download-signed visibility for a matter's
  // signature state. Reused on open, after send, and on each poll. Drives BOTH
  // the in-modal badge and the always-visible header badge in the matter-actions
  // group so the status shows even with the composer closed (e.g. on reload).
  function renderSignatureState(matter) {
    const view = matterView(matter);
    applyBadge(badgeNode, view);
    applyBadge(headerBadgeNode, view);
    if (envelopeNode) {
      const envelopeId = matterEnvelopeId(matter);
      envelopeNode.textContent = envelopeId ? `Envelope ${envelopeId}` : "";
      envelopeNode.hidden = !envelopeId;
    }
    if (downloadSignedLink) {
      downloadSignedLink.hidden = !view.canDownloadSigned;
    }
    if (submitButton) {
      // #30: once the matter has an ACTIVE (sent, non-terminal) envelope the
      // primary button must be truly inert — disabled AND relabelled — so a second
      // click can never re-POST and create a duplicate envelope. setSubmitting()
      // would otherwise re-enable it after the send completes. Before the first
      // send (or after a terminal envelope, where a resend is legitimate) the
      // button only reflects the in-flight `busy` state.
      if (hasActiveEnvelope(matter)) {
        submitButton.disabled = true;
        submitButton.textContent = "Sent for signature";
      } else {
        submitButton.disabled = busy;
      }
    }
  }

  function downloadSigned() {
    const matter = currentMatter();
    if (!matter?.id) return;
    const url = `/api/matters/${encodeURIComponent(matter.id)}/signed-document`;
    const filename = signedFilename(matter);
    if (typeof downloadUrl === "function") {
      downloadUrl(url, filename);
    } else {
      window.location.href = url;
    }
  }

  function signedFilename(matter) {
    const base = String(matter?.source_filename || matter?.document_title || "nda")
      .replace(/\.(docx|pdf)$/i, "")
      .trim() || "nda";
    return `${base}-signed.pdf`;
  }

  function setSubmitting(submitting) {
    if (submitButton) {
      // #30: when NOT submitting, don't blindly re-enable the primary button — if
      // the matter now has an active envelope (the send just succeeded) it must
      // stay inert. The `finally` of a successful send calls setSubmitting(false)
      // AFTER renderSignatureState already locked the button; defer to the
      // already-sent state here so it is never re-enabled into a duplicate send.
      if (!submitting && hasActiveEnvelope(currentMatter())) {
        submitButton.disabled = true;
        submitButton.textContent = "Sent for signature";
      } else {
        submitButton.disabled = submitting;
        submitButton.textContent = submitting ? "Sending" : "Send for signature";
      }
    }
    [cancelButton, closeButton].filter(Boolean).forEach((control) => {
      control.disabled = submitting;
    });
  }

  function setStatus(message, tone = "") {
    if (!statusNode) return;
    statusNode.textContent = message;
    statusNode.classList.toggle("error", tone === "error");
    statusNode.classList.toggle("success", tone === "success");
  }

  // #31: a guiding "DocuSign isn't connected" message for the composer. When the
  // server supplied a connect_url, render it as a clickable link to the connect
  // flow; otherwise the guiding text alone is enough. The URL is HTML-escaped
  // before it touches innerHTML (it is server-controlled, but escaping keeps the
  // render path injection-safe regardless).
  function setConnectNeeded(connectUrl) {
    if (!statusNode) return;
    const base = "DocuSign isn't connected — connect it in Admin → Integrations.";
    const url = String(connectUrl || "").trim();
    statusNode.classList.add("error");
    statusNode.classList.remove("success");
    if (url) {
      statusNode.innerHTML = `${html(base)} <a href="${html(url)}" data-docusign-connect-link>Connect DocuSign</a>`;
    } else {
      statusNode.textContent = base;
    }
  }

  // Drive the trigger button's visibility/label from the matter state: only show
  // "Send for signature" on a matter that has a saved review (and is ideally
  // approved). Called by the workstation when the selected matter changes.
  function syncTriggerButton() {
    // The Review instance has NO trigger button (Send moved to the Overview
    // footer), but it still owns the always-visible header signature badge — keep
    // it in sync from matter state even without a button to label.
    if (!triggerButton) {
      renderSignatureState(currentMatter());
      return;
    }
    const matter = currentMatter();
    const gateVisible = typeof isTriggerVisible === "function"
      ? Boolean(isTriggerVisible(matter))
      : true;
    const hasMatter = Boolean(matter?.id) && gateVisible;
    triggerButton.hidden = !hasMatter;
    if (!hasMatter) return;
    // No-jump header: the trigger stays present once a matter is loaded, but is
    // GRAYED/disabled until the enable gate passes (the Review workstation gates on
    // ai_review_ran — nothing to send before a review has run). Default (no gate
    // supplied) = enabled.
    const gateEnabled = typeof isTriggerEnabled === "function"
      ? Boolean(isTriggerEnabled(matter))
      : true;
    triggerButton.disabled = !gateEnabled;
    triggerButton.setAttribute("aria-disabled", String(!gateEnabled));
    // Read the canonical nested status (matter.docusign.status), flat as fallback,
    // so a reloaded/freshly-fetched matter that is already out for signature keeps
    // its "already sent" label instead of resetting to "Send for signature".
    const view = matterView(matter);
    // Update any inline badge that lives outside the modal too.
    renderSignatureState(matter);
    if (!gateEnabled) {
      setTriggerLabel("Send for Signature");
      triggerButton.title = "Run the AI review before sending for signature.";
      return;
    }
    if (view?.sent) {
      setTriggerLabel(view.completed ? "View Signature" : "Signature Status");
      triggerButton.title = view.label;
    } else {
      setTriggerLabel("Send for Signature");
      triggerButton.title = "Send this NDA for e-signature via DocuSign";
    }
  }

  // Write the trigger's label WITHOUT clobbering an icon. The Review trigger now
  // mirrors the Generator's CTA: an SVG pen icon + a <span> label (class
  // `icon-text`). Writing triggerButton.textContent would wipe that icon, so when
  // a child <span> exists we set its text and leave the icon intact; otherwise we
  // fall back to textContent for any plain (icon-less) trigger.
  function setTriggerLabel(text) {
    if (!triggerButton) return;
    const label = triggerButton.querySelector("span");
    if (label) label.textContent = text;
    else triggerButton.textContent = text;
  }

  return {
    openComposer,
    closeComposer,
    renderSignatureState,
    syncTriggerButton,
    refreshStatus,
    // Exposed for the on-demand "Refresh status" button (review workstation): the
    // button is shown only for a matter with an ACTIVE (sent, non-terminal)
    // envelope, mirroring the in-controller gate, and reuses setConnectNeeded to
    // surface a 409 needs_connect/needs_reconnect inline.
    hasActiveEnvelope,
    setConnectNeeded,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { createDocuSignSendController };
}
