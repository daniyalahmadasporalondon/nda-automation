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

  studioExportPdfButton?.addEventListener("click", async () => {
    await exportAnnotatedPdf();
  });

  studioRefreshReviewButton?.addEventListener("click", async () => {
    await refreshSelectedMatterReview();
  });

  studioSendButton.addEventListener("click", () => {
    openReviewSendComposer();
  });

  studioReviewedButton?.addEventListener("click", () => {
    markMatterReviewed();
  });

  studioApproveReviewButton?.addEventListener("click", async () => {
    await approveSelectedReview();
  });

  studioReviewedDocxButton?.addEventListener("click", async () => {
    await downloadReviewedDocx();
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

async function exportAnnotatedPdf() {
  const matter = state.selectedMatter?.id ? state.selectedMatter : null;
  if (!matter || !String(matter.source_filename || "").toLowerCase().endsWith(".pdf")) return;
  if (reviewIsStale()) {
    handleStaleReviewOperationError({ reviewRefresh: state.selectedMatter?.review_refresh }, "Annotated PDF export could not run.");
    return;
  }
  studioExportPdfButton.disabled = true;
  studioExportPdfButton.title = "Choosing file…";
  try {
    const saveHandle = await chooseExportSaveHandle(suggestedAnnotatedPdfFilenameForContext(matter), {
      types: PDF_EXPORT_FILE_PICKER_TYPES,
    });
    if (saveHandle === null) {
      studioFileMeta.textContent = "Export cancelled";
      return;
    }
    studioExportPdfButton.title = "Exporting…";
    const response = await fetch("/api/export-annotated-pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ matter_id: matter.id }),
    });
    if (!response.ok) {
      const payload = await response.json();
      throw reviewErrorFromPayload(payload, "Annotated PDF export could not run");
    }
    const filename = downloadFilename(response) || suggestedAnnotatedPdfFilenameForContext(matter);
    const exportVerified = response.headers.get("X-Export-Verified");
    const annotationCount = response.headers.get("X-PDF-Annotation-Count");
    const unmatchedCount = response.headers.get("X-PDF-Unmatched-Evidence-Count");
    const blob = await response.blob();
    if (saveHandle) {
      await writeBlobToSaveHandle(saveHandle, blob);
      renderAnnotatedPdfExportSuccess(filename, annotationCount, unmatchedCount, exportVerified, "saved");
    } else {
      downloadBlob(blob, filename);
      renderAnnotatedPdfExportSuccess(filename, annotationCount, unmatchedCount, exportVerified, "downloading");
    }
  } catch (error) {
    if (isStaleReviewError(error)) {
      handleStaleReviewOperationError(error, "Annotated PDF export could not run.");
    } else {
      renderOperationError(error, "Annotated PDF export could not run.");
    }
  } finally {
    studioExportPdfButton.title = "Export annotated PDF";
    updateExportButtonState();
  }
}

async function exportReviewDocx() {
  pendingReviewSendMatterId = null;
  const text = studioNdaText.value.trim() || state.reviewSourceText.trim();
  if (!text) return;
  if (reviewIsStale()) {
    handleStaleReviewOperationError({ reviewRefresh: state.selectedMatter?.review_refresh }, "Export could not run.");
    return;
  }
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
      // Inbound-fill tool: blanks the user filled with Aspora entity values.
      // CLEAN fills have already rewritten the paragraph text (and advanced the
      // manual-redline baseline so they don't double-emit as manual redlines);
      // TRACKED fills are left for the backend to render as tracked changes.
      // The backend keys on {paragraph_id, find, value, mode}.
      fills: currentReviewFills(),
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
    if (isStaleReviewError(error)) {
      handleStaleReviewOperationError(error, "Export could not run.");
    } else {
      renderOperationError(error, "Export could not run.");
    }
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

function openReviewSendComposer() {
  if (!state.selectedMatter?.id) return;
  if (reviewIsStale()) {
    handleStaleReviewOperationError({ reviewRefresh: state.selectedMatter?.review_refresh }, "Redline email could not send.");
    return;
  }
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
      // Confirm the exact destination so a spoofed inbound Reply-To cannot
      // silently redirect the outbound redline; the server rejects a mismatch.
      confirm_recipient: recipient,
      text: studioNdaText.value.trim() || state.reviewSourceText.trim(),
      reviewed_text: studioNdaText.value.trim() || state.reviewSourceText.trim(),
      export_redline_edits: effectiveReviewRedlines(),
      manual_redline_edits: manualExportRedlines(),
      review_comments: currentReviewComments(),
      // Carry inbound-fill blanks on send too, so a TRACKED fill isn't dropped
      // when the redline is emailed (CLEAN fills already live in the text).
      fills: currentReviewFills(),
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
    if (isStaleReviewError(error)) {
      closeReviewSendComposer({ restoreFocus: false });
      handleStaleReviewOperationError(error, "Redline email could not send.");
    } else {
      setReviewSendStatus(error.message || "Redline email could not send.");
      renderOperationError(error, "Redline email could not send.");
    }
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
    .filter((edit) => edit.clause_id && edit.clause_id !== manualViewerEditClauseId());
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
  if (isStaleReviewError(error)) {
    handleStaleReviewOperationError(error, fallbackMeta);
    return;
  }
  studioOverallTitle.textContent = error.message || fallbackMeta;
  studioResultMark.textContent = "!";
  studioResultMark.className = "check";
  const details = Array.isArray(error.details) && error.details.length
    ? ` ${error.details.slice(0, 3).join(" ")}`
    : "";
  studioResultMeta.textContent = `${fallbackMeta}${details}`;
}

// Returns true when it is safe to discard the current in-memory redline edits:
// either there is nothing unsaved, or the reviewer confirmed the loss. The
// confirm() is skipped (returns true) when the draft is clean so the common case
// never sees a dialog.
function confirmDiscardUnsavedReviewEdits(reason) {
  if (!hasUnsavedReviewEdits()) return true;
  const message = `${reason} Save Draft first if you want to keep them.\n\nDiscard your unsaved edits and continue?`;
  if (typeof window !== "undefined" && typeof window.confirm === "function") {
    return window.confirm(message);
  }
  return true;
}

function hasUnsavedReviewEdits() {
  return Boolean(state.redlineDraftDirty);
}

async function refreshSelectedMatterReview() {
  const matterId = state.selectedMatter?.id;
  if (!matterId) return;
  // Refreshing reloads the review from the server and discards in-memory redline
  // edits. Guard against silently losing unsaved changes: confirm first, and tell
  // the reviewer they can Save Draft to keep them.
  if (!confirmDiscardUnsavedReviewEdits("Refreshing the review will discard your unsaved redline edits.")) {
    return;
  }
  const previousLabel = studioRefreshReviewButton?.textContent || "Refresh Review";
  if (studioRefreshReviewButton) {
    studioRefreshReviewButton.disabled = true;
    studioRefreshReviewButton.textContent = "Refreshing";
  }
  setFileMeta("Refreshing review against the active Playbook.");
  try {
    const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/review-refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) throw reviewErrorFromPayload(payload, "Review could not refresh");
    const refreshedMatter = matterReviewPayloadToMatter(payload);
    loadMatterIntoReview(refreshedMatter);
    await repositoryController.loadMatters();
    if (payload.review_refresh?.stale) {
      setFileMeta(staleReviewMessage(payload.review_refresh));
    } else if (payload.review_refresh?.redline_draft_cleared) {
      setFileMeta(payload.review_refresh.message || "Review refreshed. Saved redline draft was cleared.");
    } else {
      setFileMeta("Review refreshed against the active Playbook.");
    }
  } catch (error) {
    if (isStaleReviewError(error)) {
      handleStaleReviewOperationError(error, "Review could not refresh.");
    } else {
      renderOperationError(error, "Review could not refresh.");
    }
  } finally {
    if (studioRefreshReviewButton?.isConnected) {
      studioRefreshReviewButton.disabled = false;
      studioRefreshReviewButton.textContent = previousLabel;
    }
    updateExportButtonState();
  }
}

function matterReviewPayloadToMatter(payload) {
  return {
    ...(payload?.matter || {}),
    extracted_text: payload?.extracted_text || "",
    redline_draft: payload?.redline_draft || null,
    review_refresh: payload?.review_refresh || null,
    review_result: payload?.review_result || {},
  };
}

function reviewIsStale() {
  return reviewWorkstationModel()?.reviewIsStale(state) ?? Boolean(state.selectedMatter?.review_refresh?.stale);
}

function isStaleReviewError(error) {
  return Boolean(error?.reviewRefresh?.stale || (Array.isArray(error?.staleReasons) && error.staleReasons.length));
}

function handleStaleReviewOperationError(error, fallbackMeta) {
  const refresh = error?.reviewRefresh || {
    stale: true,
    stale_reasons: Array.isArray(error?.staleReasons) ? error.staleReasons : [],
  };
  if (state.selectedMatter?.id) {
    state.selectedMatter = {
      ...state.selectedMatter,
      review_refresh: refresh,
    };
  }
  const message = staleReviewMessage(refresh, error?.message || fallbackMeta);
  renderReviewRefreshNotice(refresh);
  updateExportButtonState();
  setFileMeta(message);
  studioOverallTitle.textContent = error?.message || "Review is stale";
  studioResultMark.textContent = "!";
  studioResultMark.className = "review";
  studioResultMeta.textContent = message;
  studioRefreshReviewButton?.focus?.();
}

function renderReviewRefreshNotice(refresh = state.selectedMatter?.review_refresh || null) {
  if (!studioRefreshReviewButton) return;
  const stale = Boolean(refresh?.stale);
  studioRefreshReviewButton.hidden = !stale;
  studioRefreshReviewButton.disabled = false;
  studioRefreshReviewButton.textContent = "Refresh Review";
  studioRefreshReviewButton.title = stale ? staleReviewMessage(refresh) : "";
}

function staleReviewMessage(refresh, fallback = "Review is stale. Refresh the review before exporting or sending.") {
  const message = String(refresh?.stale_message || refresh?.message || "").trim();
  if (message) return message;
  const reasons = Array.isArray(refresh?.stale_reasons) ? refresh.stale_reasons : [];
  if (reasons.includes("playbook_changed")) {
    return "Active Playbook changed. Refresh review before exporting or sending.";
  }
  if (reasons.includes("review_engine_version_changed")) {
    return "Review engine changed. Refresh review before exporting or sending.";
  }
  if (reasons.includes("missing_playbook_runtime")) {
    return "Review was created before Playbook runtime tracking. Refresh review before exporting or sending.";
  }
  return fallback;
}

function markRedlineDraftDirty() {
  const transition = reviewWorkstationModel()?.redlineDraftTransition(state, { dirty: true });
  if (transition) {
    state.redlineDraftDirty = transition.redlineDraftDirty;
  } else {
    if (!state.selectedMatter?.id || !state.reviewClauses.length) return;
    state.redlineDraftDirty = true;
  }
  updateRedlineDraftControls();
}

function updateRedlineDraftControls() {
  const controlState = reviewWorkstationModel()?.redlineDraftControlState(state);
  const canDraft = controlState?.canDraft ?? Boolean(state.selectedMatter?.id && state.reviewClauses.length);
  if (studioSaveDraftButton) {
    studioSaveDraftButton.disabled = controlState?.saveDisabled ?? (!canDraft || !state.redlineDraftDirty);
  }
  if (studioDiscardDraftButton) {
    studioDiscardDraftButton.disabled = controlState?.discardDisabled ?? (!canDraft || !state.redlineDraft);
  }
  if (!studioDraftMeta) return;
  if (controlState) {
    studioDraftMeta.textContent = controlState.metaText;
    return;
  }
  studioDraftMeta.textContent = !canDraft
    ? ""
    : state.redlineDraftDirty
      ? "Unsaved redline draft changes"
      : state.redlineDraft
        ? "Draft redline saved"
        : "";
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
      types: options.types || EXPORT_FILE_PICKER_TYPES,
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

function renderAnnotatedPdfExportSuccess(filename, annotationCount, unmatchedCount, verification, fallbackVerb = "exported") {
  studioFileMeta.textContent = "";
  const summary = document.createElement("span");
  summary.className = "export-success";
  const verified = verification ? " · PDF annotations generated" : "";
  const countText = annotationCount ? ` · ${annotationCount} highlight${String(annotationCount) === "1" ? "" : "s"}` : "";
  const unmatched = Number(unmatchedCount || 0);
  const unmatchedText = unmatched > 0 ? ` · ${unmatched} evidence item${unmatched === 1 ? "" : "s"} not located` : "";
  summary.textContent = `${filename} ${fallbackVerb}${verified}${countText}${unmatchedText}`;
  studioFileMeta.append(summary);
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

function suggestedAnnotatedPdfFilenameForContext(matter) {
  const sourceName = String(matter?.source_filename || "nda").replace(/\.[^.]+$/, "");
  const safeName = sourceName
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "nda";
  return `${safeName}-annotated-review.pdf`;
}

// ---------------------------------------------------------------------------
// Approval gate (tasks 3.1–3.5).
//
// "Approve Review" is the sole human sign-off: one approval covers the whole
// matter, so there are no per-clause reviewer decisions. The gate blocks only
// on review staleness (a data-freshness guard) plus any authoritative
// blocks_approval the server returned on a 409.
// ---------------------------------------------------------------------------

// Drive the Approve Review button's enabled/disabled + label state from the
// local view of staleness and unresolved clauses, so it reflects what a POST
// would do before the request is sent.
function updateApproveReviewControl() {
  if (!studioApproveReviewButton) return;
  const matter = state.selectedMatter;
  const hasReview = hasReviewResults();
  const approved = isMatterApproved(matter);
  studioApproveReviewButton.hidden = !(hasReview && matter?.id);
  if (!hasReview || !matter?.id) {
    renderReviewedDocxControl();
    return;
  }
  if (approved) {
    studioApproveReviewButton.disabled = true;
    studioApproveReviewButton.classList.add("approved");
    studioApproveReviewButton.classList.remove("blocked");
    studioApproveReviewButton.textContent = "Approved";
    studioApproveReviewButton.title = approvedReviewTitle(matter);
    studioApproveReviewButton.setAttribute("aria-disabled", "true");
    renderApproveBlockReasons([]);
    renderReviewedDocxControl();
    return;
  }
  const blocks = approveBlockReasons(matter);
  const blocked = blocks.length > 0;
  studioApproveReviewButton.classList.remove("approved");
  studioApproveReviewButton.classList.toggle("blocked", blocked);
  studioApproveReviewButton.disabled = blocked;
  studioApproveReviewButton.textContent = "Approve Review";
  studioApproveReviewButton.title = blocked
    ? "Resolve the blockers below before approving"
    : "Approve this review";
  studioApproveReviewButton.setAttribute("aria-disabled", String(blocked));
  renderApproveBlockReasons(blocks);
  renderReviewedDocxControl();
}

// Local prediction of the server's blocks_approval reason codes. The only
// blocker is "stale_playbook" (a data-freshness guard); per-clause reviewer
// decisions no longer gate approval. Unioned with the last authoritative blocks
// the server returned on a 409, so the displayed gate never understates what the
// backend would reject.
function approveBlockReasons(matter) {
  const reasons = [];
  if (reviewIsStale()) reasons.push("stale_playbook");
  const serverBlocks = Array.isArray(state.approveServerBlocks) ? state.approveServerBlocks : [];
  serverBlocks.forEach((reason) => {
    if (!reasons.includes(reason)) reasons.push(reason);
  });
  return reasons;
}

function isMatterApproved(matter) {
  return String(matter?.status || "").trim().toLowerCase() === "approved";
}

function approvedReviewTitle(matter) {
  const approver = String(matter?.approver || "").trim();
  const approvedAt = matter?.approved_at ? formatReviewTimestamp(matter.approved_at) : "";
  const parts = ["Review approved"];
  if (approver) parts.push(`by ${approver}`);
  if (approvedAt) parts.push(approvedAt);
  return parts.join(" · ");
}

function renderApproveBlockReasons(reasons) {
  if (!studioApproveBlockReasons) return;
  if (!reasons.length) {
    studioApproveBlockReasons.hidden = true;
    studioApproveBlockReasons.innerHTML = "";
    return;
  }
  studioApproveBlockReasons.hidden = false;
  studioApproveBlockReasons.innerHTML = `
    <p class="approve-block-title">Approval is blocked:</p>
    <ul>${reasons.map((reason) => `<li>${escapeHtml(approveBlockReasonLabel(reason))}</li>`).join("")}</ul>
  `;
}

function approveBlockReasonLabel(reason) {
  const code = String(reason || "").trim();
  if (code === "stale_playbook") {
    return "The review is stale — refresh it against the active Playbook.";
  }
  return code;
}

async function approveSelectedReview() {
  const matterId = state.selectedMatter?.id;
  if (!matterId || !studioApproveReviewButton) return;
  if (isMatterApproved(state.selectedMatter)) return;
  const previousLabel = studioApproveReviewButton.textContent;
  studioApproveReviewButton.disabled = true;
  studioApproveReviewButton.textContent = "Approving…";
  setFileMeta("Approving this review.");
  try {
    const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await response.json();
    if (response.status === 409) {
      const blocks = Array.isArray(payload.blocks_approval)
        ? payload.blocks_approval.filter(Boolean).map((reason) => String(reason))
        : [];
      // The server's blocks_approval is authoritative. Stash it so the gate keeps
      // showing it (even where the local predictor would disagree) until the
      // reviewer takes an action that could resolve it.
      state.approveServerBlocks = blocks;
      if (blocks.includes("stale_playbook") && state.selectedMatter?.id) {
        state.selectedMatter = {
          ...state.selectedMatter,
          review_refresh: { ...(state.selectedMatter.review_refresh || {}), stale: true },
        };
      }
      setFileMeta("Approval is blocked. Resolve the listed blockers and try again.");
      updateApproveReviewControl();
      return;
    }
    if (!response.ok) throw reviewErrorFromPayload(payload, "Review could not be approved");
    state.approveServerBlocks = [];
    if (payload.matter && typeof payload.matter === "object") {
      state.selectedMatter = { ...state.selectedMatter, ...payload.matter };
    } else {
      state.selectedMatter = { ...state.selectedMatter, status: "approved" };
    }
    state.reviewResolution = payload.resolution || state.reviewResolution;
    setFileMeta("Review approved. You can download the reviewed DOCX.");
    updateApproveReviewControl();
  } catch (error) {
    renderOperationError(error, "Review could not be approved.");
  } finally {
    if (studioApproveReviewButton?.isConnected && studioApproveReviewButton.textContent === "Approving…") {
      studioApproveReviewButton.textContent = previousLabel;
      studioApproveReviewButton.disabled = false;
    }
    updateApproveReviewControl();
  }
}

// The reviewed-DOCX download is offered only once the matter is approved.
function renderReviewedDocxControl() {
  if (!studioReviewedDocxButton) return;
  const approved = isMatterApproved(state.selectedMatter) && Boolean(state.selectedMatter?.id);
  studioReviewedDocxButton.hidden = !approved;
  studioReviewedDocxButton.disabled = !approved;
}

async function downloadReviewedDocx() {
  const matterId = state.selectedMatter?.id;
  if (!matterId || !isMatterApproved(state.selectedMatter)) return;
  const previousLabel = studioReviewedDocxButton?.textContent || "Download Reviewed DOCX";
  if (studioReviewedDocxButton) {
    studioReviewedDocxButton.disabled = true;
    studioReviewedDocxButton.textContent = "Preparing…";
  }
  try {
    const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/reviewed-docx`);
    if (!response.ok) {
      let payload = {};
      try {
        payload = await response.json();
      } catch (parseError) {
        payload = {};
      }
      throw reviewErrorFromPayload(payload, "Reviewed DOCX could not download");
    }
    const blob = await response.blob();
    downloadBlob(blob, suggestedReviewedDocxFilename());
    setFileMeta("Reviewed DOCX downloaded.");
  } catch (error) {
    renderOperationError(error, "Reviewed DOCX could not download.");
  } finally {
    if (studioReviewedDocxButton?.isConnected) {
      studioReviewedDocxButton.disabled = false;
      studioReviewedDocxButton.textContent = previousLabel;
    }
    renderReviewedDocxControl();
  }
}

function suggestedReviewedDocxFilename() {
  const base = String(state.selectedMatter?.source_filename || "nda")
    .replace(/\.[^.]+$/, "")
    .replace(/[^A-Za-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "nda";
  return `${base}-reviewed.docx`;
}
