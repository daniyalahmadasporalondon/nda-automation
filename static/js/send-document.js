// Dashboard "Send Document" outbound flow controller.
// Mirrors createManualUploadController: pick/drop a .docx, enter a recipient and
// optional subject/body, then POST /api/send-document which reuses the existing
// Gmail send plumbing and lands a card in the Sent column.
function createSendDocumentController({
  modalNode,
  closeButton,
  fileInput,
  form,
  selectedFileNode,
  statusNode,
  recipientInput,
  subjectInput,
  bodyInput,
  submitButton,
  clearButton,
  dropzone,
  fileToBase64,
  repositoryController,
  activateTab,
  reviewErrorFromPayload,
}) {
  let selectedFile = null;
  let busy = false;
  let previousFocus = null;

  fileInput?.addEventListener("change", () => {
    setSelectedFile(fileInput.files?.[0] || null);
  });

  clearButton?.addEventListener("click", () => resetForm());
  closeButton?.addEventListener("click", () => closeModal({ reset: true }));

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await sendSelectedDocument();
  });

  modalNode?.addEventListener("click", (event) => {
    if (event.target === modalNode && !busy) closeModal({ reset: true });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !isModalOpen() || busy) return;
    event.preventDefault();
    closeModal({ reset: true });
  });

  dropzone?.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });

  dropzone?.addEventListener("dragleave", () => {
    dropzone.classList.remove("dragging");
  });

  dropzone?.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
    const file = event.dataTransfer?.files?.[0] || null;
    if (!file) return;
    setSelectedFile(file);
  });

  function setSelectedFile(file) {
    selectedFile = file;
    if (fileInput && fileInput.files?.[0] !== file) {
      fileInput.value = "";
    }
    if (selectedFile && subjectInput && !subjectInput.value.trim()) {
      subjectInput.value = sendFileStem(selectedFile.name);
    }
    setStatus("");
    renderSelectedFile();
  }

  function renderSelectedFile() {
    const hasFile = Boolean(selectedFile);
    if (selectedFileNode) {
      selectedFileNode.classList.toggle("empty", !hasFile);
      selectedFileNode.innerHTML = hasFile
        ? `
          <strong>${escapeHtml(selectedFile.name)}</strong>
          <span>${escapeHtml(formatBytes(selectedFile.size))}</span>
        `
        : "No file selected";
    }
    updateSubmitState();
  }

  function updateSubmitState() {
    if (!submitButton) return;
    const recipient = recipientInput?.value || "";
    const ready = Boolean(selectedFile) && isValidSendRecipient(recipient) && isSupportedSendUpload(selectedFile?.name);
    submitButton.disabled = busy || !ready;
  }

  recipientInput?.addEventListener("input", () => {
    setStatus("");
    updateSubmitState();
  });

  async function sendSelectedDocument() {
    if (busy) return;
    if (!selectedFile) {
      setStatus("Attach a .docx document first.", "error");
      return;
    }
    if (!isSupportedSendUpload(selectedFile.name)) {
      setStatus("Attach a .docx Word document to send.", "error");
      return;
    }
    const recipient = (recipientInput?.value || "").trim();
    if (!isValidSendRecipient(recipient)) {
      setStatus("Enter a valid recipient email address.", "error");
      return;
    }

    busy = true;
    setStatus("Sending document.");
    renderSelectedFile();
    if (clearButton) clearButton.disabled = true;

    try {
      const contentBase64 = await fileToBase64(selectedFile);
      const subject = (subjectInput?.value || "").trim() || sendFileStem(selectedFile.name);
      const requestBody = {
        filename: selectedFile.name,
        content_base64: contentBase64,
        to: recipient,
        subject,
      };
      const note = (bodyInput?.value || "").trim();
      if (note) requestBody.body = note;

      const response = await fetch("/api/send-document", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      const payload = await response.json();
      if (!response.ok) throw reviewErrorFromPayload(payload, "Document could not be sent");

      const matter = payload.matter || {};
      resetForm({ status: `Sent ${matter.source_filename || selectedFile.name} to ${recipient}.` });
      closeModal({ restoreFocus: false });
      await repositoryController.loadMatters();
      activateTab("repository");
    } catch (error) {
      setStatus(error.message || "Document could not be sent.", "error");
    } finally {
      busy = false;
      if (clearButton) clearButton.disabled = false;
      renderSelectedFile();
    }
  }

  function resetForm({ status = "" } = {}) {
    selectedFile = null;
    if (form) form.reset();
    setStatus(status, status ? "success" : "");
    renderSelectedFile();
  }

  function openFilePicker() {
    if (busy) return;
    fileInput?.click();
  }

  function openModal() {
    if (!modalNode) {
      openFilePicker();
      return;
    }
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    modalNode.hidden = false;
    document.body.classList.add("modal-open");
    window.setTimeout(() => closeButton?.focus?.(), 0);
  }

  function closeModal({ reset = false, restoreFocus = true } = {}) {
    if (!modalNode) return;
    modalNode.hidden = true;
    document.body.classList.remove("modal-open");
    if (reset) resetForm();
    if (restoreFocus) {
      const focusTarget = previousFocus?.isConnected ? previousFocus : null;
      focusTarget?.focus?.();
    }
    previousFocus = null;
  }

  function isModalOpen() {
    return Boolean(modalNode && !modalNode.hidden);
  }

  function setStatus(message, tone = "") {
    if (!statusNode) return;
    statusNode.textContent = message;
    statusNode.classList.toggle("error", tone === "error");
    statusNode.classList.toggle("success", tone === "success");
  }

  renderSelectedFile();
  return { closeModal, openFilePicker, openModal, resetForm, sendSelectedDocument };
}

function isSupportedSendUpload(filename) {
  return /\.docx$/i.test(String(filename || ""));
}

function isValidSendRecipient(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value || "").trim());
}

function sendFileStem(filename) {
  return String(filename || "Document").split(/[\\/]/).pop().replace(/\.[^.]+$/, "") || "Document";
}
