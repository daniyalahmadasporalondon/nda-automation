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

  // Render the prefilled signer rows from the matter + Aspora signatory.
  function renderSignerRows() {
    if (!signerRows) return;
    const matter = currentMatter();
    const m = model();
    const signers = m
      ? m.defaultSigners(matter || {}, { asporaSignatory: asporaSignatory() })
      : [];
    signerRows.innerHTML = signers.map((signer, index) => `
      <div class="docusign-signer-row" data-docusign-signer="${index}">
        <span class="docusign-signer-order">${signer.order}</span>
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

  // Collect the (possibly edited) signer rows from the form.
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
      const result = await response.json();
      if (!response.ok) throw reviewErrorFromPayload(result, "Could not send for signature");
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
      setStatus(error.message || "Could not send for signature.", "error");
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

  async function refreshStatus(matterId) {
    try {
      const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/signature-status`);
      const payload = await response.json();
      if (!response.ok) return;
      const matter = currentMatter();
      if (!matter?.id || matter.id !== matterId) return;
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
    } catch (error) {
      // Transient network errors are ignored; the next tick retries.
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
      // Once sent, the primary button becomes a no-op until a fresh resend is
      // meaningful; keep it enabled only before the first send.
      submitButton.disabled = busy;
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
      submitButton.disabled = submitting;
      submitButton.textContent = submitting ? "Sending" : "Send for signature";
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

  return { openComposer, closeComposer, renderSignatureState, syncTriggerButton, refreshStatus };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { createDocuSignSendController };
}
