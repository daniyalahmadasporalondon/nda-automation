async function loadFileIntoReview(file) {
  const extension = file.name.split(".").pop().toLowerCase();

  if (extension === "docx") {
    state.selectedDocument = file;
    state.selectedMatter = null;
    setSourceText("");
    showStudioSourceEditor();
    resizeSourceEditors();
    setSourcePlaceholder("Word document selected");
    setFileMeta(`${file.name} ready for review`);
    setDocumentTitle(file.name);
    resetReviewResults();
    renderStudioEmpty();
    setActiveTab("review");
    return;
  }

  state.selectedDocument = null;
  state.selectedMatter = null;
  const fileText = await file.text();
  setSourceText(fileText);
  showStudioSourceEditor();
  resizeSourceEditors();
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  setFileMeta(`${file.name} loaded as text`);
  setDocumentTitle(file.name);
  resetReviewResults();
  renderStudioEmpty();
  setActiveTab("review");
}

function isWordDocument(file) {
  return file.name.toLowerCase().endsWith(".docx");
}

function clearReview() {
  pendingReviewSendMatterId = null;
  setSourceText("");
  showStudioSourceEditor();
  resizeSourceEditors();
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  fileInput.value = "";
  state.selectedDocument = null;
  state.selectedMatter = null;
  setFileMeta("No file selected");
  setDocumentTitle(DEFAULT_DOCUMENT_TITLE);
  resetReviewResults();
  emptyState();
}

function resetReviewResults() {
  pendingReviewSendMatterId = null;
  state.reviewClauses = [];
  state.reviewOriginalParagraphs = [];
  state.reviewParagraphs = [];
  state.reviewRedlines = [];
  state.reviewDirty = false;
  state.reviewSourceText = "";
  state.selectedReviewClauseId = null;
  state.clauseJumpIndexes = {};
  state.exportClauseDecisions = {};
  state.redlineTemplateSelections = {};
  state.lastExport = null;
}

function setupReviewWorkstationActions() {
  studioClearButton.addEventListener("click", () => {
    clearReview();
  });

  studioReviewButton.addEventListener("click", async () => {
    await runReview(studioNdaText, studioReviewButton);
  });

  studioExportButton.addEventListener("click", async () => {
    await exportReviewDocx();
  });

  studioSendButton.addEventListener("click", async () => {
    await sendReviewRedlineEmail();
  });
}

async function runReview(sourceInput, button) {
  const text = sourceInput.value.trim();
  const rerunningLoadedMatter = Boolean(state.selectedMatter?.id && !state.selectedDocument);
  if (!text && !state.selectedDocument) {
    emptyState();
    studioOverallTitle.textContent = "Add NDA text";
    studioResultMark.textContent = "-";
    studioResultMeta.textContent = "Paste NDA text or upload a document to run the checklist.";
    studioMatchSummary.textContent = `0/${getClauseTotal()}`;
    return;
  }

  button.disabled = true;
  button.textContent = "Reviewing";

  try {
    const response = state.selectedDocument
      ? await reviewDocument(state.selectedDocument)
      : await fetch("/api/review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, "Review could not run");
    const reviewedText = payload.extracted_text || text;
    if (rerunningLoadedMatter) {
      state.selectedMatter = null;
      setFileMeta("Repository text reviewed as a fresh draft");
    }
    if (payload.extracted_text) {
      setSourceText(payload.extracted_text);
      resizeSourceEditors();
      setSourcePlaceholder(SOURCE_PLACEHOLDER);
      setFileMeta(`${payload.source.filename} reviewed from Word document`);
    }
    renderResult(payload, reviewedText);
  } catch (error) {
    renderOperationError(error, "Review could not run.");
  } finally {
    button.disabled = false;
    button.textContent = "Review NDA";
  }
}

async function exportReviewDocx() {
  pendingReviewSendMatterId = null;
  const text = studioNdaText.value.trim() || state.reviewSourceText.trim();
  if (!text) return;

  studioExportButton.disabled = true;
  studioExportButton.textContent = "Choosing file";

  try {
    const saveHandle = await chooseExportSaveHandle(suggestedExportFilename());
    if (saveHandle === null) {
      studioFileMeta.textContent = "Export cancelled";
      return;
    }

    studioExportButton.textContent = "Exporting";
    const payload = {
      text,
      reviewed_text: text,
      title: studioDocTitle.textContent || DEFAULT_DOCUMENT_TITLE,
      export_redline_edits: effectiveReviewRedlines(),
      manual_redline_edits: manualExportRedlines(),
    };
    if (state.selectedMatter?.id) {
      payload.matter_id = state.selectedMatter.id;
    } else if (state.selectedDocument) {
      payload.filename = state.selectedDocument.name;
      payload.content_base64 = await fileToBase64(state.selectedDocument);
    }

    const response = await fetch("/api/export-review-docx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const payload = await response.json();
      throw reviewErrorFromPayload(payload, "Export could not run");
    }
    const filename = downloadFilename(response) || "nda-review-report.docx";
    const savedPath = response.headers.get("X-Export-Path");
    const savedUrl = response.headers.get("X-Export-URL");
    const exportVerified = response.headers.get("X-Export-Verified");
    if (saveHandle) {
      const blob = await response.blob();
      await writeBlobToSaveHandle(saveHandle, blob);
      renderExportSuccess(filename, savedPath, savedUrl, exportVerified, "saved");
    } else if (savedUrl) {
      renderExportSuccess(filename, savedPath, savedUrl, exportVerified);
      downloadUrl(savedUrl, filename);
    } else {
      const blob = await response.blob();
      downloadBlob(blob, filename);
      renderExportSuccess(filename, savedPath, savedUrl, exportVerified, "downloading");
    }
    await repositoryController.markMatterRedlineReady(state.selectedMatter);
  } catch (error) {
    renderOperationError(error, "Export could not run.");
  } finally {
    studioExportButton.textContent = "Export DOCX";
    updateExportButtonState();
  }
}

async function sendReviewRedlineEmail() {
  if (!state.selectedMatter?.id) return;
  const recipient = MatterUtils.recipientEmail(state.selectedMatter);
  if (!recipient) {
    pendingReviewSendMatterId = null;
    studioSendButton.textContent = "Send Redline";
    setFileMeta("Matter sender is not an email address");
    updateExportButtonState();
    return;
  }
  if (pendingReviewSendMatterId !== state.selectedMatter.id) {
    pendingReviewSendMatterId = state.selectedMatter.id;
    studioSendButton.textContent = "Confirm Send";
    setFileMeta(`Click Confirm Send to email the redline to ${recipient}`);
    return;
  }

  studioSendButton.disabled = true;
  studioSendButton.textContent = "Sending";
  try {
    const payload = {
      matter_id: state.selectedMatter.id,
      confirm_send: true,
      export_redline_edits: effectiveReviewRedlines(),
      manual_redline_edits: manualExportRedlines(),
    };
    const response = await fetch("/api/gmail/send-redline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(result, "Redline email could not send");
    if (result.matter?.id) {
      state.selectedMatter = result.matter;
      await repositoryController.loadMatters();
    }
    pendingReviewSendMatterId = null;
    setFileMeta(`Sent redline to ${recipient}`);
  } catch (error) {
    pendingReviewSendMatterId = null;
    renderOperationError(error, "Redline email could not send.");
  } finally {
    studioSendButton.textContent = "Send Redline";
    updateExportButtonState();
  }
}

function renderOperationError(error, fallbackMeta) {
  studioOverallTitle.textContent = error.message || fallbackMeta;
  studioResultMark.textContent = "!";
  studioResultMark.className = "check";
  const details = Array.isArray(error.details) && error.details.length
    ? ` ${error.details.slice(0, 3).join(" ")}`
    : "";
  studioResultMeta.textContent = `${fallbackMeta}${details}`;
}

async function chooseExportSaveHandle(suggestedName, options = {}) {
  if (!shouldUseSaveFilePicker(options)) return undefined;
  try {
    return await window.showSaveFilePicker({
      suggestedName,
      types: DOCX_FILE_PICKER_TYPES,
    });
  } catch (error) {
    if (error?.name === "AbortError") return null;
    console.warn("Save picker unavailable; falling back to browser download.", error);
    return undefined;
  }
}

function shouldUseSaveFilePicker({ allowAutomation = false } = {}) {
  return (
    typeof window.showSaveFilePicker === "function"
    && window.isSecureContext
    && (!navigator.webdriver || allowAutomation)
  );
}

async function writeBlobToSaveHandle(fileHandle, blob) {
  const writable = await fileHandle.createWritable();
  try {
    await writable.write(blob);
  } finally {
    await writable.close();
  }
}

async function reviewDocument(file) {
  const contentBase64 = await fileToBase64(file);
  return fetch("/api/review-document", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filename: file.name,
      content_base64: contentBase64,
    }),
  });
}

function renderExportSuccess(filename, savedPath, savedUrl, verification, fallbackVerb = "exported") {
  state.lastExport = { filename, savedPath, savedUrl, verification };
  studioFileMeta.textContent = "";
  const summary = document.createElement("span");
  summary.className = "export-success";
  const verificationText = verification ? " · Word package verified · Track Changes enabled" : "";
  summary.textContent = `${savedUrl ? `Saved export: ${savedUrl}` : `${filename} ${fallbackVerb}`}${verificationText}`;
  studioFileMeta.append(summary);
  if (savedUrl) {
    studioFileMeta.append(document.createTextNode(" "));
    const link = document.createElement("a");
    link.className = "download-again";
    link.href = savedUrl;
    link.download = filename;
    link.textContent = "Download again";
    studioFileMeta.append(link);
  } else if (savedPath) {
    studioFileMeta.append(document.createTextNode(` ${savedPath}`));
  }
}

function suggestedExportFilename() {
  if (state.selectedMatter?.source_filename) return redlineDownloadFilename(state.selectedMatter.source_filename);
  if (state.selectedDocument?.name) return redlineDownloadFilename(state.selectedDocument.name);
  return "nda-review-report.docx";
}
