function createManualUploadController({
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
  fileToBase64,
  repositoryController,
  activateTab,
  reviewErrorFromPayload,
  onFileSelected,
}) {
  let selectedFile = null;
  let busy = false;

  fileInput?.addEventListener("change", () => {
    setSelectedFile(fileInput.files?.[0] || null);
  });

  clearButton?.addEventListener("click", () => resetForm());

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await uploadSelectedFile();
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
    if (selectedFile && typeof onFileSelected === "function") {
      onFileSelected(selectedFile);
    }
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
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw reviewErrorFromPayload(payload, "Manual upload could not be imported");

      const matter = payload.matter || {};
      resetForm({ status: `Uploaded ${matter.source_filename || selectedFile.name}.` });
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

  function setStatus(message, tone = "") {
    if (!statusNode) return;
    statusNode.textContent = message;
    statusNode.classList.toggle("error", tone === "error");
    statusNode.classList.toggle("success", tone === "success");
  }

  renderSelectedFile();
  return { openFilePicker, resetForm, uploadSelectedFile };
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
