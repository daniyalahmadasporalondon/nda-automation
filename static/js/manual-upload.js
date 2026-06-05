function createManualUploadController({
  modalNode,
  closeButton,
  fileInput,
  form,
  selectedFileNode,
  statusNode,
  subjectInput,
  senderInput,
  noteInput,
  submitButton,
  clearButton,
  dropzone,
  routeStageNode,
  allowedBoardColumns = ["in_review"],
  defaultBoardColumn = "in_review",
  boardColumnLabel = (boardColumn) => boardColumn || "In Review",
  fileToBase64,
  repositoryController,
  activateTab,
  reviewErrorFromPayload,
}) {
  let selectedFile = null;
  let busy = false;
  let previousFocus = null;
  let targetBoardColumn = defaultBoardColumn;
  const allowedBoardColumnIds = new Set(allowedBoardColumns);

  fileInput?.addEventListener("change", () => {
    setSelectedFile(fileInput.files?.[0] || null);
  });

  clearButton?.addEventListener("click", () => resetForm());
  closeButton?.addEventListener("click", () => closeModal({ reset: true }));

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await uploadSelectedFile();
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
    if (selectedFile && !subjectInput.value.trim()) {
      subjectInput.value = fileStem(selectedFile.name);
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
    if (submitButton) {
      submitButton.disabled = busy || !hasFile;
    }
  }

  async function uploadSelectedFile() {
    if (busy) return;
    if (!selectedFile) {
      setStatus("Select a .docx or text-based .pdf first.", "error");
      return;
    }
    if (!isSupportedUpload(selectedFile.name)) {
      setStatus("Upload a .docx Word document or text-based PDF.", "error");
      return;
    }

    busy = true;
    setStatus("Uploading and reviewing NDA.");
    renderSelectedFile();
    if (clearButton) clearButton.disabled = true;

    try {
      const contentBase64 = await fileToBase64(selectedFile);
      const response = await fetch("/api/matters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: selectedFile.name,
          content_base64: contentBase64,
          source_type: "manual_upload",
          sender: senderInput.value.trim(),
          subject: subjectInput.value.trim() || fileStem(selectedFile.name),
          received_at: new Date().toISOString(),
          message_snippet: noteInput.value.trim() || `Manual upload of ${selectedFile.name}.`,
          attachment_filename: selectedFile.name,
          board_column: targetBoardColumn,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw reviewErrorFromPayload(payload, "Manual upload could not be imported");

      const matter = payload.matter || {};
      resetForm({ status: `Uploaded ${matter.source_filename || selectedFile.name}.` });
      closeModal({ restoreFocus: false });
      await repositoryController.loadMatters();
      activateTab("repository");
      if (matter.id) {
        await repositoryController.openMatter(matter.id);
      }
    } catch (error) {
      setStatus(error.message || "Manual upload could not be imported.", "error");
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

  function openModal(options = {}) {
    setTargetBoardColumn(options.boardColumn || defaultBoardColumn);
    if (!modalNode) {
      openFilePicker();
      return;
    }
    previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
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

  function setTargetBoardColumn(boardColumn) {
    targetBoardColumn = allowedBoardColumnIds.has(boardColumn) ? boardColumn : defaultBoardColumn;
    if (routeStageNode) {
      routeStageNode.textContent = boardColumnLabel(targetBoardColumn);
    }
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
  setTargetBoardColumn(defaultBoardColumn);
  return { closeModal, openFilePicker, openModal, resetForm, uploadSelectedFile };
}

function isSupportedUpload(filename) {
  return /\.(docx|pdf)$/i.test(String(filename || ""));
}

function fileStem(filename) {
  return String(filename || "Untitled NDA").split(/[\\/]/).pop().replace(/\.[^.]+$/, "") || "Untitled NDA";
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
