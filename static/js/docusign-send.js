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
  envelopeNode,
  downloadSignedLink,
  submitButton,
  triggerButton,
  getMatter,
  getAsporaSignatory,
  reviewErrorFromPayload,
  downloadUrl,
  onMatterUpdated,
}) {
  const model = (typeof window !== "undefined" && window.DocuSignModel) || (typeof DocuSignModel !== "undefined" ? DocuSignModel : null);
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
    const signers = model
      ? model.defaultSigners(matter || {}, { asporaSignatory: asporaSignatory() })
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
    const validation = model
      ? model.validateSigners(collectSigners())
      : { ok: true, signers: collectSigners(), error: "" };
    if (!validation.ok) {
      setStatus(validation.error, "error");
      return;
    }
    const payload = model
      ? model.buildSendForSignaturePayload(validation.signers, selectedSigningOrder())
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
      const merged = {
        ...matter,
        signature_envelope_id: result.envelope_id || matter.signature_envelope_id || "",
        signature_status: result.status || "sent",
      };
      applyMatterUpdate(merged);
      renderSignatureState(merged);
      setStatus(`Sent for signature. Envelope ${result.envelope_id || ""}.`.trim(), "success");
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
      const merged = { ...matter, signature_status: payload.status || matter.signature_status };
      applyMatterUpdate(merged);
      renderSignatureState(merged);
      const view = model ? model.signatureView(payload) : null;
      if (view?.terminal) stopPolling();
    } catch (error) {
      // Transient network errors are ignored; the next tick retries.
    }
  }

  function applyMatterUpdate(matter) {
    if (typeof onMatterUpdated === "function") onMatterUpdated(matter);
  }

  // Render the badge + envelope id + download-signed visibility for a matter's
  // signature state. Reused on open, after send, and on each poll.
  function renderSignatureState(matter) {
    const view = model
      ? model.signatureView(matter?.signature_status)
      : { badge: "", tone: "idle", completed: false, canDownloadSigned: false, sent: false };
    if (badgeNode) {
      badgeNode.textContent = view.badge || "";
      badgeNode.hidden = !view.badge;
      badgeNode.classList.remove("ready", "pending", "blocked");
      if (view.tone === "ready") badgeNode.classList.add("ready");
      else if (view.tone === "pending") badgeNode.classList.add("pending");
      else if (view.tone === "blocked") badgeNode.classList.add("blocked");
    }
    if (envelopeNode) {
      const envelopeId = String(matter?.signature_envelope_id || "");
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
    if (!triggerButton) return;
    const matter = currentMatter();
    const hasMatter = Boolean(matter?.id);
    triggerButton.hidden = !hasMatter;
    if (!hasMatter) return;
    const view = model ? model.signatureView(matter?.signature_status) : null;
    // Update any inline badge that lives outside the modal too.
    renderSignatureState(matter);
    if (view?.sent) {
      triggerButton.textContent = view.completed ? "View signature" : "Signature status";
      triggerButton.title = view.label;
    } else {
      triggerButton.textContent = "Send for signature";
      triggerButton.title = "Send this NDA for e-signature via DocuSign";
    }
  }

  return { openComposer, closeComposer, renderSignatureState, syncTriggerButton, refreshStatus };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { createDocuSignSendController };
}
