function clearReview() {
  pendingReviewSendMatterId = null;
  setSourceText("");
  showStudioSourceEditor();
  resizeSourceEditors();
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  state.selectedDocument = null;
  state.selectedMatter = null;
  setFileMeta("No file selected");
  setDocumentTitle(DEFAULT_DOCUMENT_TITLE);
  resetReviewResults();
  emptyState();
}

function resetReviewResults() {
  cancelViewerReviewRefresh();
  pendingReviewSendMatterId = null;
  state.reviewClauses = [];
  state.reviewExportOriginalParagraphs = [];
  state.reviewOriginalParagraphs = [];
  state.reviewParagraphs = [];
  resetReviewEditHistory();
  state.reviewRedlines = [];
  state.reviewSourceText = "";
  state.selectedReviewClauseId = null;
  state.clauseJumpIndexes = {};
  state.exportClauseDecisions = {};
  state.redlineTemplateSelections = {};
  state.redlineDraft = null;
  state.redlineDraftDirty = false;
}

function setupReviewWorkstationActions() {
  studioClearButton.addEventListener("click", () => {
    clearReview();
  });

  studioReviewButton.addEventListener("click", async () => {
    await runReview(studioNdaText, studioReviewButton);
  });

  studioSaveDraftButton.addEventListener("click", async () => {
    await saveReviewRedlineDraft();
  });

  studioDiscardDraftButton.addEventListener("click", async () => {
    await resetReviewRedlineDraft();
  });

  studioExportButton.addEventListener("click", async () => {
    await exportReviewDocx();
  });

  studioSendButton.addEventListener("click", async () => {
    await sendReviewRedlineEmail();
  });
}

async function runReview(sourceInput, button) {
  cancelViewerReviewRefresh();
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
      setFileMeta(`${payload.source.filename} reviewed from ${payload.source.type?.toUpperCase() || "document"}`);
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
  const exportMatter = state.selectedMatter?.id ? state.selectedMatter : null;
  const exportDocument = !exportMatter && state.selectedDocument ? state.selectedDocument : null;
  const exportTitle = studioDocTitle.textContent || DEFAULT_DOCUMENT_TITLE;
  const exportRedlines = effectiveReviewRedlines();
  const exportManualRedlines = manualExportRedlines();
  const exportDraftDirty = Boolean(exportMatter?.id && state.redlineDraftDirty);

  studioExportButton.disabled = true;
  studioExportButton.textContent = "Choosing file";

  try {
    const saveHandle = await chooseExportSaveHandle(suggestedExportFilenameForContext(exportMatter, exportDocument));
    if (saveHandle === null) {
      studioFileMeta.textContent = "Export cancelled";
      return;
    }

    studioExportButton.textContent = "Exporting";
    if (exportDraftDirty && state.selectedMatter?.id === exportMatter.id) {
      await saveReviewRedlineDraft({ quiet: true });
    }
    const payload = {
      text,
      reviewed_text: text,
      title: exportTitle,
      export_redline_edits: exportRedlines,
      manual_redline_edits: exportManualRedlines,
    };
    if (exportMatter?.id) {
      payload.matter_id = exportMatter.id;
    } else if (exportDocument) {
      payload.filename = exportDocument.name;
      payload.content_base64 = await fileToBase64(exportDocument);
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
    await repositoryController.markMatterRedlineReady(exportMatter);
  } catch (error) {
    renderOperationError(error, "Export could not run.");
  } finally {
    studioExportButton.textContent = "Export DOCX";
    updateExportButtonState();
  }
}

async function sendReviewRedlineEmail() {
  if (!state.selectedMatter?.id) return;
  const sendBlockReason = MatterUtils.gmailSendBlock(state.selectedMatter, state.gmailStatus);
  if (sendBlockReason) {
    pendingReviewSendMatterId = null;
    studioSendButton.textContent = MatterUtils.gmailSendButtonLabel(sendBlockReason);
    setFileMeta(sendBlockReason);
    updateExportButtonState();
    return;
  }
  const recipient = MatterUtils.recipientEmail(state.selectedMatter);
  if (!recipient) {
    pendingReviewSendMatterId = null;
    studioSendButton.textContent = "Send Redline";
    setFileMeta("Matter does not have a valid reply recipient email address");
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
    if (state.redlineDraftDirty) {
      await saveReviewRedlineDraft({ quiet: true });
    }
    const payload = {
      matter_id: state.selectedMatter.id,
      confirm_send: true,
      text: studioNdaText.value.trim() || state.reviewSourceText.trim(),
      reviewed_text: studioNdaText.value.trim() || state.reviewSourceText.trim(),
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

function markRedlineDraftDirty() {
  if (!state.selectedMatter?.id || !state.reviewClauses.length) return;
  state.redlineDraftDirty = true;
  updateRedlineDraftControls();
}

function updateRedlineDraftControls() {
  const canDraft = Boolean(state.selectedMatter?.id && state.reviewClauses.length);
  if (studioSaveDraftButton) {
    studioSaveDraftButton.disabled = !canDraft || !state.redlineDraftDirty;
  }
  if (studioDiscardDraftButton) {
    studioDiscardDraftButton.disabled = !canDraft || !state.redlineDraft;
  }
  if (!studioDraftMeta) return;
  if (!canDraft) {
    studioDraftMeta.textContent = "No custom draft";
  } else if (state.redlineDraftDirty) {
    studioDraftMeta.textContent = "Unsaved redline draft changes";
  } else if (state.redlineDraft) {
    studioDraftMeta.textContent = "Draft redline saved";
  } else {
    studioDraftMeta.textContent = "No custom draft";
  }
}

function currentRedlineDraftPayload() {
  return {
    clause_decisions: { ...state.exportClauseDecisions },
    template_selections: { ...state.redlineTemplateSelections },
    export_redline_edits: effectiveReviewRedlines(),
    manual_redline_edits: manualExportRedlines(),
  };
}

async function saveReviewRedlineDraft({ quiet = false } = {}) {
  if (!state.selectedMatter?.id || !state.reviewClauses.length) return null;
  if (studioSaveDraftButton && !quiet) {
    studioSaveDraftButton.disabled = true;
    studioSaveDraftButton.textContent = "Saving";
  }
  try {
    const draftPayload = currentRedlineDraftPayload();
    const response = await fetch(`/api/matters/${encodeURIComponent(state.selectedMatter.id)}/redline-draft`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ redline_draft: draftPayload }),
    });
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, "Draft could not save");
    if (payload.matter?.id) {
      state.selectedMatter = payload.matter;
      state.redlineDraft = draftPayload;
      state.redlineDraftDirty = false;
      await repositoryController.openMatter(payload.matter.id);
    }
    updateRedlineDraftControls();
    if (!quiet) setFileMeta("Draft redline saved");
    return payload.matter || null;
  } catch (error) {
    if (!quiet) renderOperationError(error, "Draft could not save.");
    throw error;
  } finally {
    if (studioSaveDraftButton) studioSaveDraftButton.textContent = "Save Draft";
    updateRedlineDraftControls();
  }
}

async function resetReviewRedlineDraft() {
  if (!state.selectedMatter?.id) return null;
  if (studioDiscardDraftButton) {
    studioDiscardDraftButton.disabled = true;
    studioDiscardDraftButton.textContent = "Resetting";
  }
  try {
    const response = await fetch(`/api/matters/${encodeURIComponent(state.selectedMatter.id)}/redline-draft`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ redline_draft: null }),
    });
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, "Draft could not reset");
    if (payload.matter?.id) {
      state.selectedMatter = payload.matter;
      await repositoryController.openMatter(payload.matter.id);
    }
    resetCurrentRedlineDraftToDefaults();
    setFileMeta("Draft redline reset");
    return payload.matter || null;
  } catch (error) {
    renderOperationError(error, "Draft could not reset.");
    return null;
  } finally {
    if (studioDiscardDraftButton) studioDiscardDraftButton.textContent = "Reset Draft";
    updateRedlineDraftControls();
  }
}

async function chooseExportSaveHandle(suggestedName, options = {}) {
  if (!shouldUseSaveFilePicker(options)) return undefined;
  try {
    return await window.showSaveFilePicker({
      suggestedName,
      types: EXPORT_FILE_PICKER_TYPES,
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
  return suggestedExportFilenameForContext(state.selectedMatter, state.selectedDocument);
}

function suggestedExportFilenameForContext(matter, document) {
  if (matter?.source_filename) return redlineDownloadFilename(matter.source_filename);
  if (document?.name) return redlineDownloadFilename(document.name);
  return "nda-review-report.docx";
}
