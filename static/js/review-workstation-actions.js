let reviewSendModalPreviousFocus = null;

function clearReview() {
  closeReviewSendComposer({ restoreFocus: false });
  pendingReviewSendMatterId = null;
  setSourceText("");
  showStudioSourceEditor();
  resizeSourceEditors();
  setSourcePlaceholder(SOURCE_PLACEHOLDER);
  AppState.clearSourceSelection(state);
  setFileMeta("");
  setCounterpartyMeta("");
  setDocumentTitle(DEFAULT_DOCUMENT_TITLE);
  resetReviewResults();
  emptyState();
}

function resetReviewResults() {
  cancelViewerReviewRefresh();
  pendingReviewSendMatterId = null;
  AppState.resetReviewResults(state);
  updateReviewUndoButtonState();
}

function setupReviewWorkstationActions() {
  studioClearButton.addEventListener("click", () => {
    clearReview();
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

  studioSendButton.addEventListener("click", () => {
    openReviewSendComposer();
  });

  studioReviewedButton?.addEventListener("click", () => {
    markMatterReviewed();
  });

  studioSendModalClose?.addEventListener("click", () => closeReviewSendComposer());
  studioSendCancelButton?.addEventListener("click", () => closeReviewSendComposer());
  studioSendForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await sendReviewRedlineEmail({ fromComposer: true });
  });
  studioSendModal?.addEventListener("click", (event) => {
    if (event.target === studioSendModal) closeReviewSendComposer();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !isReviewSendComposerOpen()) return;
    if (studioSendConfirmButton?.disabled) return;
    event.preventDefault();
    closeReviewSendComposer();
  });
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
  studioExportButton.title = "Choosing file…";

  try {
    const saveHandle = await chooseExportSaveHandle(suggestedExportFilenameForContext(exportMatter, exportDocument));
    if (saveHandle === null) {
      studioFileMeta.textContent = "Export cancelled";
      return;
    }

    studioExportButton.title = "Exporting…";
    if (exportDraftDirty && state.selectedMatter?.id === exportMatter.id) {
      await saveReviewRedlineDraft({ quiet: true });
    }
    const payload = {
      text,
      reviewed_text: text,
      title: exportTitle,
      export_redline_edits: exportRedlines,
      manual_redline_edits: exportManualRedlines,
      review_comments: currentReviewComments(),
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
    studioExportButton.title = "Export DOCX";
    updateExportButtonState();
  }
}

async function markMatterReviewed({ sourceButton = studioReviewedButton, clauseId = "" } = {}) {
  const matterId = state.selectedMatter?.id;
  const targetClauseId = clauseId || sourceButton?.dataset?.reviewClauseId || "";
  const targetClauseIds = targetClauseId ? [targetClauseId] : reviewClauseIds();
  if (!targetClauseIds.length) return;
  const previousReviewedClauseIds = { ...reviewedClauseMap() };
  const previousMatter = state.selectedMatter ? { ...state.selectedMatter } : null;
  const previousMatterReviewed = Boolean(previousMatter?.human_reviewed);

  if (state.selectedMatter?.human_reviewed) {
    reviewClauseIds().forEach((id) => {
      if (!Object.prototype.hasOwnProperty.call(reviewedClauseMap(), id)) {
        reviewedClauseMap()[id] = true;
      }
    });
  }

  const nextReviewed = targetClauseIds.some((id) => !clauseReviewAcknowledged(id));
  targetClauseIds.forEach((id) => {
    if (state.reviewClauses.some((clause) => clause.id === id)) {
      reviewedClauseMap()[id] = nextReviewed;
    }
  });
  const allReviewed = humanReviewAcknowledged();
  const shouldPersistMatterReviewed = Boolean(matterId && allReviewed !== previousMatterReviewed);

  if (state.selectedMatter && shouldPersistMatterReviewed) {
    state.selectedMatter = { ...state.selectedMatter, human_reviewed: allReviewed };
    if (allReviewed) delete state.selectedMatter.send_block_reason;
  }

  markRedlineDraftDirty();
  setFileMeta(
    allReviewed
      ? "All review clauses marked reviewed. You can send the redline now."
      : nextReviewed
        ? "Marked clause reviewed."
        : "Marked clause not reviewed.",
  );
  renderStudioClauseLane();
  renderStudioDetail();
  updateExportButtonState();

  if (!shouldPersistMatterReviewed) return;

  try {
    const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/reviewed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewed: allReviewed }),
    });
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, "Could not mark this matter reviewed");
    if (payload.matter?.id) {
      const merged = { ...state.selectedMatter, ...payload.matter };
      // The server omits send_block_reason once it clears; drop any stale value
      // so the client gate (which checks it first) unblocks too.
      if (allReviewed && !payload.matter.send_block_reason) delete merged.send_block_reason;
      state.selectedMatter = merged;
    }
    updateExportButtonState();
  } catch (error) {
    state.reviewedClauseIds = previousReviewedClauseIds;
    if (previousMatter) state.selectedMatter = previousMatter;
    renderStudioClauseLane();
    renderStudioDetail();
    updateExportButtonState();
    renderOperationError(error, "Could not mark this matter reviewed.");
  }
}

async function runReviewComparison({ text = "", matterId = "" } = {}) {
  const targetMatterId = matterId || state.selectedMatter?.id || "";
  const comparisonText = String(text || studioNdaText.value.trim() || state.reviewSourceText.trim()).trim();
  state.reviewComparisonStatus = "running";
  state.reviewComparisonError = "";
  try {
    const comparison = targetMatterId
      ? await runMatterReviewComparison(targetMatterId)
      : await runTextReviewComparison(comparisonText);
    setReviewComparison(comparison);
    return comparison;
  } catch (error) {
    setReviewComparisonError(error);
    throw error;
  }
}

async function runTextReviewComparison(text) {
  if (!text) throw new Error("Provide NDA text to compare.");
  const response = await fetch("/api/review/compare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const payload = await response.json();
  if (!response.ok) throw reviewErrorFromPayload(payload, "Review comparison could not run");
  return payload.review_comparison || null;
}

async function runMatterReviewComparison(matterId) {
  const comparison = await repositoryController.compareMatterReview(matterId);
  if (!comparison) throw new Error("Matter review comparison did not return a comparison payload.");
  return comparison;
}


function openReviewSendComposer() {
  if (!state.selectedMatter?.id) return;
  const sendBlockReason = MatterUtils.gmailSendBlock(state.selectedMatter, state.gmailStatus);
  const missingRecipientBlock = isMissingRecipientSendBlock(sendBlockReason);
  if (sendBlockReason && !missingRecipientBlock) {
    pendingReviewSendMatterId = null;
    setStudioSendButtonLabel(MatterUtils.gmailSendButtonLabel(sendBlockReason), sendBlockReason);
    setFileMeta(sendBlockReason);
    updateExportButtonState();
    return;
  }
  const recipient = MatterUtils.recipientEmail(state.selectedMatter);
  if (!studioSendModal || !studioSendForm) {
    setFileMeta("Email composer is unavailable.");
    return;
  }

  const draft = buildReviewSendDraft(recipient);
  reviewSendModalPreviousFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : studioSendButton;
  if (studioSendFrom) studioSendFrom.textContent = reviewOutboundAccountLabel();
  if (studioSendTo) studioSendTo.value = recipient;
  if (studioSendAttachment) studioSendAttachment.textContent = reviewSendAttachmentLabel();
  if (studioSendSubject) studioSendSubject.value = draft.subject;
  if (studioSendBody) studioSendBody.value = draft.body;
  renderReviewSendSummary(draft.summary);
  if (studioSendStatus) studioSendStatus.textContent = missingRecipientBlock ? "Enter a recipient email address before sending." : "";
  setReviewSendComposerBusy(false);
  studioSendModal.hidden = false;
  document.body.classList.add("modal-open");
  window.setTimeout(() => (recipient ? studioSendSubject : studioSendTo)?.focus(), 0);
}

function closeReviewSendComposer({ restoreFocus = true } = {}) {
  if (!studioSendModal) return;
  studioSendModal.hidden = true;
  document.body.classList.remove("modal-open");
  if (studioSendStatus) studioSendStatus.textContent = "";
  setReviewSendComposerBusy(false);
  if (restoreFocus) {
    const focusTarget = reviewSendModalPreviousFocus?.isConnected
      ? reviewSendModalPreviousFocus
      : studioSendButton;
    focusTarget?.focus?.();
  }
  reviewSendModalPreviousFocus = null;
}

function isReviewSendComposerOpen() {
  return Boolean(studioSendModal && !studioSendModal.hidden);
}

async function sendReviewRedlineEmail({ fromComposer = false } = {}) {
  if (!state.selectedMatter?.id) return;
  if (!fromComposer || !isReviewSendComposerOpen()) {
    openReviewSendComposer();
    return;
  }
  const recipient = reviewComposerRecipient();
  const sendBlockReason = MatterUtils.gmailSendBlock(state.selectedMatter, state.gmailStatus);
  const missingRecipientBlock = isMissingRecipientSendBlock(sendBlockReason);
  if (missingRecipientBlock && !recipient) {
    setReviewSendStatus("Enter a valid recipient email address.");
    updateExportButtonState();
    return;
  }
  if (sendBlockReason && !(missingRecipientBlock && recipient)) {
    setReviewSendStatus(sendBlockReason);
    updateExportButtonState();
    return;
  }
  if (!recipient) {
    setReviewSendStatus("Enter a valid recipient email address.");
    updateExportButtonState();
    return;
  }
  const subject = studioSendSubject?.value || "";
  const body = studioSendBody?.value || "";

  studioSendButton.disabled = true;
  setStudioSendButtonLabel("Sending", `Sending redline to ${recipient}`);
  setReviewSendComposerBusy(true);
  setReviewSendStatus("Sending email...");
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
      review_comments: currentReviewComments(),
      to: recipient,
      subject: subject.trim(),
      body: body.trim(),
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
    closeReviewSendComposer({ restoreFocus: false });
    setFileMeta(`Sent redline to ${recipient}`);
    studioSendButton?.focus?.();
  } catch (error) {
    pendingReviewSendMatterId = null;
    setReviewSendStatus(error.message || "Redline email could not send.");
    renderOperationError(error, "Redline email could not send.");
  } finally {
    setReviewSendComposerBusy(false);
    setStudioSendButtonLabel("Send Redline");
    updateExportButtonState();
  }
}

function buildReviewSendDraft(recipient) {
  const summary = reviewSendChangeSummary();
  return {
    body: reviewSendDefaultBody(summary),
    recipient,
    subject: reviewSendDefaultSubject(summary),
    summary,
  };
}

function isMissingRecipientSendBlock(reason) {
  return String(reason || "").toLowerCase().includes("valid reply recipient");
}

function reviewComposerRecipient() {
  return MatterUtils.emailAddress(studioSendTo?.value || studioSendTo?.textContent || "");
}

function reviewSendChangeSummary() {
  const clauseRedlines = effectiveReviewRedlines()
    .filter((edit) => edit.clause_id && edit.clause_id !== "manual_viewer_edit");
  const manualRedlines = manualExportRedlines();
  const comments = currentReviewComments();
  const clauseNames = uniqueStrings(clauseRedlines.map((edit) => clauseNameForId(edit.clause_id)));
  const textSnippets = uniqueStrings([...clauseRedlines, ...manualRedlines]
    .map(redlineTextSnippet)
    .filter(Boolean));
  const commentSnippets = uniqueStrings(comments
    .map(reviewCommentSnippet)
    .filter(Boolean));

  return {
    clauseNames,
    clauseRedlineCount: clauseRedlines.length,
    commentCount: comments.length,
    commentSnippets,
    manualCount: manualRedlines.length,
    textSnippets,
  };
}

function reviewSendDefaultSubject(summary) {
  return truncateText(`Redline for ${reviewSendMatterTitle()}`, 80);
}

function reviewSendDefaultBody(summary) {
  const summaryLines = reviewSendSummaryLines(summary);
  return [
    "Hi,",
    "",
    `Please find attached the redline for ${reviewSendMatterTitle()}.`,
    "",
    "Summary of changes:",
    ...summaryLines.map((line) => `- ${line}`),
    "",
    "Best,",
    "Aspora",
  ].join("\n");
}

function reviewSendSummaryLines(summary) {
  const lines = [];
  if (summary.clauseRedlineCount) {
    lines.push(`${summary.clauseRedlineCount} included clause ${plural("redline", summary.clauseRedlineCount)}: ${formatCompactList(summary.clauseNames, 4)}.`);
  }
  if (summary.manualCount) {
    lines.push(`${summary.manualCount} manual viewer ${plural("edit", summary.manualCount)}.`);
  }
  if (summary.textSnippets.length) {
    lines.push(`Text added or replaced: ${formatSnippetList(summary.textSnippets, 3)}.`);
  }
  if (summary.commentCount) {
    lines.push(`${summary.commentCount} Word ${plural("comment", summary.commentCount)}: ${formatCompactList(summary.commentSnippets, 3)}.`);
  }
  if (!lines.length) {
    lines.push("Redline generated from the current review state.");
  }
  return lines;
}

function renderReviewSendSummary(summary) {
  if (!studioSendSummary) return;
  studioSendSummary.innerHTML = "";
  reviewSendSummaryLines(summary).forEach((line) => {
    const item = document.createElement("li");
    item.textContent = line;
    studioSendSummary.append(item);
  });
}

function reviewOutboundAccountLabel() {
  const outbound = state.gmailStatus?.outbound || {};
  if (outbound.ready && outbound.email) return outbound.email;
  return outbound.email || outbound.error || "Outbound Gmail";
}

function reviewSendAttachmentLabel() {
  return suggestedExportFilenameForContext(state.selectedMatter, state.selectedDocument) || "nda-redlined.docx";
}

function reviewSendMatterTitle() {
  return String(
    state.selectedMatter?.document_title
      || state.selectedMatter?.subject
      || state.selectedMatter?.source_filename
      || studioDocTitle.textContent
      || "this NDA"
  ).trim();
}

function clauseNameForId(clauseId) {
  const clause = state.reviewClauses.find((item) => item.id === clauseId);
  return clause?.name || humanizeClauseId(clauseId);
}

function humanizeClauseId(clauseId) {
  return String(clauseId || "Clause")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function redlineTextSnippet(edit) {
  if (!edit || edit.action === "delete_paragraph") return "";
  const text = edit.insert_text || edit.replacement_text || edit.text || "";
  return truncateText(collapseWhitespace(text), 110);
}

function reviewCommentSnippet(comment) {
  const label = comment.clause_name || (comment.clause_id ? clauseNameForId(comment.clause_id) : "");
  const scope = label || (comment.selected_text ? "Selected text" : "Paragraph");
  const text = truncateText(collapseWhitespace(comment.text), 80);
  return text ? `${scope}: ${text}` : scope;
}

function uniqueStrings(values) {
  const seen = new Set();
  return values
    .map((value) => String(value || "").trim())
    .filter((value) => {
      const key = value.toLowerCase();
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function formatCompactList(values, limit = 3) {
  const cleanValues = uniqueStrings(values);
  if (cleanValues.length <= limit) return cleanValues.join(", ");
  return `${cleanValues.slice(0, limit).join(", ")} + ${cleanValues.length - limit} more`;
}

function formatSnippetList(values, limit = 3) {
  return formatCompactList(values, limit);
}

function plural(word, count) {
  return count === 1 ? word : `${word}s`;
}

function collapseWhitespace(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function truncateText(text, maxLength) {
  const cleanText = String(text || "").trim();
  if (cleanText.length <= maxLength) return cleanText;
  return `${cleanText.slice(0, Math.max(0, maxLength - 1)).trim()}...`;
}

function setReviewSendComposerBusy(busy) {
  [
    studioSendSubject,
    studioSendBody,
    studioSendCancelButton,
    studioSendModalClose,
    studioSendConfirmButton,
  ].filter(Boolean).forEach((control) => {
    control.disabled = busy;
  });
  if (studioSendConfirmButton) {
    studioSendConfirmButton.textContent = busy ? "Sending" : "Send email";
  }
}

function setReviewSendStatus(message) {
  if (studioSendStatus) studioSendStatus.textContent = message || "";
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
    studioDraftMeta.textContent = "";
  } else if (state.redlineDraftDirty) {
    studioDraftMeta.textContent = "Unsaved redline draft changes";
  } else if (state.redlineDraft) {
    studioDraftMeta.textContent = "Draft redline saved";
  } else {
    studioDraftMeta.textContent = "";
  }
}

function currentRedlineDraftPayload() {
  return {
    clause_decisions: { ...state.exportClauseDecisions },
    redline_decisions: { ...state.exportRedlineDecisions },
    template_selections: { ...state.redlineTemplateSelections },
    reviewed_clause_ids: { ...reviewedClauseMap() },
    export_redline_edits: effectiveReviewRedlines(),
    manual_redline_edits: manualExportRedlines(),
    review_comments: currentReviewComments(),
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
