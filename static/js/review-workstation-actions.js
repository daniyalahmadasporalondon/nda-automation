let reviewSendModalPreviousFocus = null;

// Upper bound on how long the reviewed-DOCX export request may run before we
// abort it. A hung/very slow export must not permanently disable the Download
// button — on timeout the request aborts, the button re-enables, and the
// reviewer can retry. Generous enough for a legitimately large document.
const EXPORT_REQUEST_TIMEOUT_MS = 120000;

// Upper bound on the explicit "Refresh Review" request. The server re-runs the
// full AI review synchronously; measured at ~50s for a short NDA and ~120s+ for a
// long one. There was previously NO client timeout here, so a genuinely hung
// socket would leave the button stuck disabled forever. 180s is generous enough
// to let a legitimately long review (up to ~3 minutes) complete, while still
// bounding a dead connection so the catch/finally re-enable the button and the
// reviewer can retry. NOT a tight 45s-style guard — a long review must NOT be
// mistaken for a failure.
const REVIEW_REFRESH_TIMEOUT_MS = 180000;

// User-facing copy shown on the matter while the synchronous AI review runs.
const REVIEW_REFRESH_PROGRESS_MESSAGE =
  "Reviewing with AI… long documents can take up to ~2 minutes.";

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
  renderCounterpartyConfirmation(null);
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

  studioExportButton.addEventListener("click", () => {
    openReviewDownloadMenu();
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

  // Approve Review now lives only on the Overview footer, which calls
  // approveSelectedReview() directly (window.approveSelectedReview). There is no
  // header Approve button to wire here anymore.

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

function openReviewDownloadMenu() {
  if (!studioExportButton || studioExportButton.disabled) return;
  const matter = state.selectedMatter || null;
  const docxOption = DocumentDownloadMenu.option(matter?.document_downloads, "reviewed", "docx");
  const pdfOption = DocumentDownloadMenu.option(matter?.document_downloads, "reviewed", "pdf");
  const staleReview = Boolean(matter?.review_refresh?.stale);
  const hasManagedDocxOption = Boolean(docxOption?.source_transform || docxOption?.label || docxOption?.fidelity);
  const docxChoice = staleReview
    ? {
        available: false,
        format: "docx",
        label: "DOCX",
        unavailableReason: "Refresh review before downloading DOCX.",
      }
    : DocumentDownloadMenu.contractChoice(docxOption, {
        label: "DOCX",
        onSelect: exportReviewDocx,
        unavailableReason: matter?.id
          ? "DOCX is not available for this reviewed document yet."
          : "DOCX is available after the review is saved as a matter.",
      });
  const pdfChoice = staleReview
    ? {
        available: false,
        format: "pdf",
        label: "PDF",
        unavailableReason: "Refresh review before downloading PDF.",
      }
    : DocumentDownloadMenu.contractChoice(pdfOption, {
        label: "PDF",
        onSelect: downloadReviewPdf,
        unavailableReason: matter?.id
          ? "PDF is not available for this reviewed document yet."
          : "PDF is available after the review is saved as a matter.",
      });
  DocumentDownloadMenu.open(studioExportButton, {
    label: "Download reviewed document",
    // Preview what the export will include (clause redlines + names, manual
    // edits, added/replaced text, comments) BEFORE the reviewer picks a format.
    // Reuses the exact same summary the email Send composer shows, derived from
    // effectiveReviewRedlines() + currentReviewComments() + manualExportRedlines().
    preview: reviewDownloadContentsPreview(),
    sections: [{
      label: "Reviewed redline",
      choices: [
        hasManagedDocxOption || staleReview ? docxChoice : {
          ...docxChoice,
          available: true,
          description: "Current redline export",
          onSelect: exportReviewDocx,
        },
        pdfChoice,
      ],
    }],
  });
}

// Build the download-menu contents preview from the same change summary the
// email Send composer uses. Returns null when there is no review state to
// preview (no clauses yet) so the menu stays unchanged in that case.
function reviewDownloadContentsPreview() {
  if (!state.reviewClauses.length) return null;
  const lines = reviewSendSummaryLines(reviewSendChangeSummary());
  if (!lines.length) return null;
  return { title: "This download will include:", lines };
}

async function downloadReviewPdf(choice) {
  if (!choice?.url) return;
  if (reviewIsStale()) {
    handleStaleReviewOperationError({ reviewRefresh: state.selectedMatter?.review_refresh }, "Download could not run.");
    return;
  }
  if (state.selectedMatter?.id && state.redlineDraftDirty) {
    await saveReviewRedlineDraft({ quiet: true });
  }
  const filename = choice.filename || "reviewed-document.pdf";
  setFileMeta(`Downloading ${filename}.`);
  downloadUrl(choice.url, filename);
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
  studioExportButton.title = "Exporting…";

  try {
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

    // Guard the export against a slow or hung server. Without a timeout, a request
    // that never resolves would leave the Download button stuck disabled (title
    // "Exporting…") permanently, since the `finally` below never runs. On timeout
    // we abort so the catch/finally re-enable the button and the reviewer can retry.
    const exportAbort = new AbortController();
    const exportTimeoutId = window.setTimeout(() => exportAbort.abort(), EXPORT_REQUEST_TIMEOUT_MS);
    let response;
    try {
      response = await fetch("/api/export-review-docx", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: exportAbort.signal,
      });
    } catch (fetchError) {
      if (fetchError?.name === "AbortError") {
        throw new Error("Export timed out — the server did not respond. Please try again.");
      }
      throw fetchError;
    } finally {
      window.clearTimeout(exportTimeoutId);
    }
    if (!response.ok) {
      const payload = await response.json();
      throw reviewErrorFromPayload(payload, "Export could not run");
    }
    // Retrieve the full blob BEFORE creating/writing any local file so that a
    // slow or failed server response never leaves an empty file on disk.
    const filename = downloadFilename(response) || "nda-review-report.docx";
    const savedPath = response.headers.get("X-Export-Path");
    const savedUrl = response.headers.get("X-Export-URL");
    const exportVerified = response.headers.get("X-Export-Verified");
    // PDF-source matters return an export reconstructed from the PDF (not faithful
    // original Word). The backend marks this with X-PDF-DOCX-Reconstruction (and
    // sets X-Export-Verified to that same marker value); surface a distinct caveat
    // instead of the generic "Word package verified" message.
    const exportReconstructedFromPdf = Boolean(
      response.headers.get("X-PDF-DOCX-Reconstruction") || exportVerified === "pdf2docx",
    );
    if (savedUrl) {
      // Server already saved the file at a known URL — download from there directly;
      // no blob to read, no local empty-file risk.
      renderExportSuccess(filename, savedPath, savedUrl, exportVerified, "exported", exportReconstructedFromPdf);
      downloadUrl(savedUrl, filename);
    } else {
      // Read the full blob first; only trigger the browser download once real bytes
      // are in hand. showSaveFilePicker is intentionally not used here: calling it
      // after an await would throw a user-gesture error, and calling it before the
      // fetch (the old "save-first" design) creates an empty destination file that
      // is left at 0 bytes on any error path.
      const blob = await response.blob();
      downloadBlob(blob, filename);
      renderExportSuccess(filename, savedPath, savedUrl, exportVerified, "downloading", exportReconstructedFromPdf);
    }
    await repositoryController.markMatterRedlineReady(exportMatter);
  } catch (error) {
    if (isStaleReviewError(error)) {
      handleStaleReviewOperationError(error, "Export could not run.");
    } else {
      renderOperationError(error, "Export could not run.");
    }
  } finally {
    studioExportButton.title = "Download";
    updateExportButtonState();
  }
}

async function markMatterReviewed({ sourceButton = studioReviewedButton, clauseId = "" } = {}) {
  const matterId = state.selectedMatter?.id;
  const targetClauseId = clauseId || sourceButton?.dataset?.reviewClauseId || "";
  const targetClauseIds = targetClauseId ? [targetClauseId] : reviewClauseIds();
  if (!targetClauseIds.length) return;
  // The header "Reviewed" button (no clauseId) flips EVERY needs-review clause
  // at once. When it would change more than one clause, confirm with the list of
  // clause names first so the bulk scope is explicit, and disambiguate the
  // un-review (toggle-OFF) direction. Single-clause "mark reviewed" (a clauseId
  // was passed, e.g. from the lane) is unchanged and never prompts.
  const isHeaderBulk = !targetClauseId;
  if (isHeaderBulk && targetClauseIds.length > 1) {
    const willReview = targetClauseIds.some((id) => !clauseReviewAcknowledged(id));
    if (!confirmMarkClausesReviewed(targetClauseIds, willReview)) return;
  }
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
  renderStudioSummary(state.reviewClauses);
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
    renderStudioSummary(state.reviewClauses);
    updateExportButtonState();
  } catch (error) {
    state.reviewedClauseIds = previousReviewedClauseIds;
    if (previousMatter) state.selectedMatter = previousMatter;
    renderStudioSummary(state.reviewClauses);
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
    // PDF-source matters send a Word file reconstructed from the PDF; append the
    // honest formatting caveat so the operator does not assume faithful original output.
    const sendCaveat = result.source_reconstructed_from_pdf
      ? " Note: this Word file was reconstructed from a PDF and may not preserve original formatting."
      : "";
    setFileMeta(`Sent redline to ${recipient}${sendCaveat}`);
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
    reviewSendSignatureBlock(),
  ].join("\n");
}

function reviewSendSignatureBlock() {
  const personalisation = state?.personalisationSettings || null;
  const signatureBlock = String(personalisation?.signature_block || "").trim();
  if (signatureBlock) return signatureBlock;
  const signOff = String(personalisation?.sign_off || "").trim();
  const signature = String(personalisation?.signature || "").trim();
  const parts = [signOff, signature].filter(Boolean);
  return parts.length ? parts.join("\n") : "Best,\nAspora";
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

// Count, per loss-bucket, what a Reset Draft would actually wipe. Each entry is
// only included when its count is non-zero so the confirm lists only what is at
// risk; a genuinely untouched draft yields no buckets and skips the dialog.
// Crucially, the Accept/Ignore and template buckets count only decisions the
// reviewer changed AWAY from the auto-derived defaults — a freshly loaded review
// already has default export/template maps that the reviewer never chose, and
// resetting back to those defaults loses nothing.
//   - comments: every reviewComment
//   - manualEdits: paragraphs whose text differs from the manual-redline
//     baseline (i.e. the reviewer typed over them)
//   - templateSelections: template choices differing from the default selection
//   - decisions: clause/redline Accept-Ignore decisions differing from default
//   - reviewedMarks: reviewedClauseIds the reviewer toggled
function reviewResetLossBuckets() {
  const buckets = [];

  const commentCount = Array.isArray(state.reviewComments) ? state.reviewComments.length : 0;
  if (commentCount) {
    buckets.push({ key: "comments", count: commentCount, label: `${commentCount} ${plural("comment", commentCount)}` });
  }

  const baseline = manualRedlineBaselineParagraphs();
  const baselineById = new Map((baseline || []).map((paragraph) => [String(paragraph.id || ""), paragraph]));
  const manualEditCount = (state.reviewParagraphs || []).reduce((total, paragraph) => {
    const original = baselineById.get(String(paragraph.id || ""));
    if (original && String(original.text || "") !== String(paragraph.text || "")) return total + 1;
    return total;
  }, 0);
  if (manualEditCount) {
    buckets.push({ key: "manualEdits", count: manualEditCount, label: `${manualEditCount} manual ${plural("edit", manualEditCount)}` });
  }

  const defaultTemplates = defaultRedlineTemplateSelections(state.reviewRedlines);
  const templateCount = Object.keys(state.redlineTemplateSelections || {}).reduce((total, editId) => {
    const current = state.redlineTemplateSelections[editId];
    return current && current !== defaultTemplates[editId] ? total + 1 : total;
  }, 0);
  if (templateCount) {
    buckets.push({ key: "templateSelections", count: templateCount, label: `${templateCount} template ${plural("selection", templateCount)}` });
  }

  const defaultClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  let decisionCount = Object.keys(state.exportClauseDecisions || {}).reduce((total, clauseId) => {
    const current = Boolean(state.exportClauseDecisions[clauseId]);
    const fallback = Boolean(defaultClauseDecisions[clauseId]);
    return current !== fallback ? total + 1 : total;
  }, 0);
  // Per-redline decisions have no auto-default map (absence == "follow clause"),
  // so every explicit redline decision is a reviewer choice.
  decisionCount += Object.keys(state.exportRedlineDecisions || {}).length;
  if (decisionCount) {
    buckets.push({ key: "decisions", count: decisionCount, label: "all Accept/Ignore decisions" });
  }

  const reviewedCount = Object.keys(reviewedClauseMap() || {}).length;
  if (reviewedCount) {
    buckets.push({ key: "reviewedMarks", count: reviewedCount, label: "all reviewed marks" });
  }

  return buckets;
}

// Gate Reset Draft behind a confirm that ENUMERATES the non-empty loss buckets
// with counts. Returns true when it is safe to proceed (nothing to lose, or the
// reviewer confirmed). Skips the dialog entirely when there is nothing to lose.
function confirmResetReviewRedlineDraft() {
  const buckets = reviewResetLossBuckets();
  if (!buckets.length) return true;
  const message = `This will discard: ${formatLossBucketList(buckets)}. Continue?`;
  if (typeof window !== "undefined" && typeof window.confirm === "function") {
    return window.confirm(message);
  }
  return true;
}

// Confirm the bulk header mark/un-mark, listing the affected clause names so the
// reviewer sees exactly which clauses a single click will flip. `willReview` is
// true when the click marks them reviewed, false when it un-reviews them all.
function confirmMarkClausesReviewed(clauseIds, willReview) {
  const names = uniqueStrings(clauseIds.map((id) => clauseNameForId(id)));
  const count = clauseIds.length;
  const verb = willReview ? "Mark" : "Unmark";
  const tail = willReview ? "as reviewed" : "as needing review";
  const message = `${verb} ${count} ${count === 1 ? "clause" : "clauses"} ${tail}?\n\n${names.map((name) => `• ${name}`).join("\n")}`;
  if (typeof window !== "undefined" && typeof window.confirm === "function") {
    return window.confirm(message);
  }
  return true;
}

function formatLossBucketList(buckets) {
  const labels = buckets.map((bucket) => bucket.label);
  if (labels.length <= 1) return labels.join("");
  const head = labels.slice(0, -1).join(", ");
  return `${head}, and ${labels[labels.length - 1]}`;
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
  if (studioRefreshReviewButton) {
    studioRefreshReviewButton.disabled = true;
    studioRefreshReviewButton.textContent = "Reviewing…";
    // A live progress state so a ~2-minute wait reads as working, not frozen:
    // the spinner class animates (CSS), and aria-busy announces it to AT.
    studioRefreshReviewButton.classList.add("is-refreshing");
    studioRefreshReviewButton.setAttribute("aria-busy", "true");
  }
  setFileMeta(REVIEW_REFRESH_PROGRESS_MESSAGE);
  // Bound the synchronous AI review so a hung socket cannot leave the button
  // stuck forever, but stay generous (180s) so a legitimately long review still
  // completes rather than being aborted and mis-reported as a failure.
  const refreshAbort = new AbortController();
  const refreshTimeoutId = window.setTimeout(() => refreshAbort.abort(), REVIEW_REFRESH_TIMEOUT_MS);
  try {
    let response;
    try {
      response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/review-refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: refreshAbort.signal,
      });
    } catch (fetchError) {
      if (fetchError?.name === "AbortError") {
        throw new Error("Review timed out — the server did not respond within ~3 minutes. Please try again.");
      }
      throw fetchError;
    }
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
    window.clearTimeout(refreshTimeoutId);
    if (studioRefreshReviewButton?.isConnected) {
      studioRefreshReviewButton.disabled = false;
      // Clear the live progress state (spinner + AT busy) set on entry...
      studioRefreshReviewButton.classList.remove("is-refreshing");
      studioRefreshReviewButton.removeAttribute("aria-busy");
      // ...then re-derive state from the now-current matter rather than restoring a
      // stale snapshot: after a successful run on a previously UNREVIEWED matter the
      // "Review" button GRAYS (review is current, nothing to do) and the downstream
      // actions (Approve / Send for signature / Mark reviewed) ENABLE via
      // updateExportButtonState. The label stays "Review" throughout (no relabel).
      renderReviewRefreshNotice();
    }
    updateExportButtonState();
  }
}

function matterReviewPayloadToMatter(payload) {
  return {
    ...(payload?.matter || {}),
    extracted_text: payload?.extracted_text || "",
    redline_draft: payload?.redline_draft || null,
    // A successful explicit refresh re-ran the AI, so the review is current unless
    // the server still flags it. Honor the server flag if present, else clear it.
    review_may_be_stale: Boolean(
      payload?.review_may_be_stale ?? payload?.matter?.review_may_be_stale ?? false,
    ),
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

// True when the loaded matter's stored review may no longer reflect the active
// Playbook / engine. Two independent signals:
//   - review_refresh.stale: set by a server staleness check (refresh/operation paths)
//   - review_may_be_stale: set when OPENING a matter (the open path does not run AI)
// Either one means the operator should run an explicit "Refresh with AI".
function reviewMayBeStale(matter = state.selectedMatter, refresh = matter?.review_refresh) {
  return Boolean(refresh?.stale || matter?.review_may_be_stale);
}

// True when an AI review has ACTUALLY run on the open matter. This is the signal
// for progressive disclosure of the review-header actions: on an UNREVIEWED
// matter there is nothing to approve or send, so the header collapses to a single
// "Review" button (which runs the AI review). The explicit backend flag wins;
// fall back to "are there any review clauses" only for old payloads/fixtures that
// predate ai_review_ran, so nothing disappears unexpectedly (matches the demote
// fallback in overview-tab.js hasAiReview()).
function aiReviewRan(matter = state.selectedMatter) {
  if (matter && typeof matter.ai_review_ran === "boolean") return matter.ai_review_ran;
  return hasReviewResults();
}

function renderReviewRefreshNotice(refresh = state.selectedMatter?.review_refresh || null) {
  const stale = reviewMayBeStale(state.selectedMatter, refresh);
  const message = stale ? staleReviewMessage(refresh || state.selectedMatter?.review_refresh) : "";
  // Progressive disclosure: "stale" implies the matter WAS reviewed and has since
  // drifted, so it is only honest once an AI review has actually run. The broad
  // reviewMayBeStale() check ALSO fires on an UNREVIEWED matter (the open path sets
  // review_may_be_stale to flag "no AI review yet"), which would mislabel it. Layer
  // the ai_review_ran gate ON TOP: on an unreviewed matter relabel the indicator
  // "Not reviewed" (reusing the corpus "Not reviewed" badge wording) instead of
  // "Review may be stale". Safe fallback when ai_review_ran is absent: aiReviewRan()
  // -> hasReviewResults(), i.e. the current/reviewed behavior, so old payloads/
  // fixtures are unchanged.
  const reviewed = aiReviewRan();
  if (studioReviewStaleIndicator) {
    if (!reviewed) {
      // Unreviewed: surface the honest "Not reviewed" state, not a stale warning.
      studioReviewStaleIndicator.hidden = false;
      studioReviewStaleIndicator.textContent = "Not reviewed";
      studioReviewStaleIndicator.title = "No AI review has run on this NDA yet. Use Review to run it.";
    } else {
      // Reviewed: the "Review may be stale" warning is meaningful again, and stays
      // gated on the genuine staleness signal.
      studioReviewStaleIndicator.hidden = !stale;
      studioReviewStaleIndicator.textContent = "Review may be stale";
      studioReviewStaleIndicator.title = message;
    }
  }
  if (!studioRefreshReviewButton) return;
  // This button is an always-PRESENT manual action: it runs the AI review on
  // demand whenever a matter is open. It is hidden only when there is no loaded
  // matter to act on. The AI run is explicit/user-initiated, so it is storm-safe
  // (the no-auto-AI-on-open safety is unaffected).
  //
  // No-jump header: the label is ALWAYS "Review" (no "Review"/"Refresh Review"
  // relabel) and the button never appears/disappears between states — it only
  // ENABLES or GRAYS. It is interactive when there is something to review
  // (UNREVIEWED, or a reviewed-but-STALE matter) and DISABLED/grayed when the
  // review is already current (reviewed && !stale), since a click would be a
  // no-op. Safe fallback when ai_review_ran is absent: aiReviewRan() ->
  // hasReviewResults(), i.e. the current/reviewed behavior.
  const reviewLoaded = Boolean(state.selectedMatter?.id) && state.reviewClauses.length > 0;
  // `reviewed` is already resolved above (aiReviewRan()) for the stale indicator.
  // Actionable when not yet reviewed, or reviewed-but-stale (something to re-run).
  const actionable = !reviewed || stale;
  studioRefreshReviewButton.hidden = !reviewLoaded;
  // Disabled (grayed via the global button:disabled rule) when the review is
  // current and there is nothing to do; the .is-refreshing class still drives the
  // in-flight spinner/disabled state during an actual run.
  studioRefreshReviewButton.disabled = !reviewLoaded || !actionable;
  studioRefreshReviewButton.setAttribute("aria-disabled", String(!reviewLoaded || !actionable));
  studioRefreshReviewButton.textContent = "Review";
  studioRefreshReviewButton.title = !reviewed
    ? "Run the AI review against the active Playbook."
    : stale
      ? message
      : "Review is current — re-run is unnecessary.";
}

function staleReviewMessage(refresh, fallback = "Review is stale — refresh before sending.") {
  const message = String(refresh?.stale_message || refresh?.message || "").trim();
  if (message) return message;
  const reasons = Array.isArray(refresh?.stale_reasons) ? refresh.stale_reasons : [];
  if (reasons.includes("playbook_changed")) {
    return "Playbook changed — refresh before sending.";
  }
  if (reasons.includes("review_engine_version_changed")) {
    return "Review engine changed — refresh before sending.";
  }
  if (reasons.includes("missing_playbook_runtime")) {
    return "Review predates runtime tracking — refresh before sending.";
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
  // Reset Draft is destructive: it wipes comments, manual edits, template
  // selections, Accept/Ignore decisions, and reviewed marks. Enumerate the
  // non-empty buckets in a confirm and abort (no POST, no state change) if the
  // reviewer cancels. The dialog is skipped when there is nothing to lose.
  if (!confirmResetReviewRedlineDraft()) return null;
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

function renderExportSuccess(filename, savedPath, savedUrl, verification, fallbackVerb = "exported", reconstructedFromPdf = false) {
  studioFileMeta.textContent = "";
  const summary = document.createElement("span");
  summary.className = "export-success";
  // A PDF-source export is reconstructed from the PDF and is not a faithful
  // original Word package, so it gets a distinct caveat rather than the
  // "Word package verified" assurance used for true DOCX-source exports.
  let verificationText = "";
  if (reconstructedFromPdf) {
    verificationText = " · Best-effort Word reconstructed from PDF — formatting may differ; the original PDF is the faithful source";
  } else if (verification) {
    verificationText = " · Word package verified · Track Changes enabled";
  }
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

// ---------------------------------------------------------------------------
// Approval gate (tasks 3.1–3.5).
//
// "Approve Review" is the sole human sign-off: one approval covers the whole
// matter, so there are no per-clause reviewer decisions. The gate blocks only
// on review staleness (a data-freshness guard) plus any authoritative
// blocks_approval the server returned on a 409.
// ---------------------------------------------------------------------------

// The approve gate now lives ENTIRELY on the Overview footer
// (static/js/overview/footer.js), which reads the same predicates this used to
// (approveBlockReasons / isMatterApproved / aiReviewRan) via overview-tab.js's
// footerData(). There is no header Approve button to paint anymore, and the
// footer is already (re)rendered by renderStudioDetail() inside the main render
// funnel (renderStudioResult), so this is now a no-op kept only so its many
// existing callers (the render funnel + the approve flow) stay valid. The approve
// flow refreshes the footer explicitly via renderStudioDetail() after it mutates
// the matter state.
function updateApproveReviewControl() {}

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

// approveBlockReasonLabel maps a block-reason code to human text for the Overview
// footer's inline reason (window.approveBlockReasonLabel). The header's
// approved-state title + the #studioApproveBlockReasons list were removed with the
// header Approve button — the footer surfaces the single first reason itself.
function approveBlockReasonLabel(reason) {
  const code = String(reason || "").trim();
  if (code === "stale_playbook") {
    return "The review is stale — refresh it against the active Playbook.";
  }
  return code;
}

// Invoked by the Overview footer's Approve button (window.approveSelectedReview).
// The footer owns the button + its gate; this performs the POST and re-renders
// the Overview pane (renderStudioDetail) on each state transition so the footer's
// approved/blocked state reflects the result. In-flight feedback is the file-meta
// status line (the header no longer has a button to relabel "Approving…").
async function approveSelectedReview() {
  const matterId = state.selectedMatter?.id;
  if (!matterId) return;
  if (isMatterApproved(state.selectedMatter)) return;
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
  } catch (error) {
    renderOperationError(error, "Review could not be approved.");
  } finally {
    // Re-render the Overview footer from the new matter state (approved / blocked
    // / re-enabled) — its gate is derived, not held on a header button.
    if (typeof renderStudioDetail === "function") renderStudioDetail();
  }
}

// ---------------------------------------------------------------------------
// Per-clause live re-assessment (POST /api/review/reassess-clause)
//
// When the reviewer commits a clause edit — either by selecting a different
// jurisdiction option or by editing a paragraph that belongs to the clause —
// we debounce a call to the single-clause reassess endpoint and patch ONLY
// that clause's card with the returned verdict, leaving the rest of the review
// untouched.  The prior verdict is preserved during the in-flight request so
// the card never goes blank; a non-destructive inline error is shown on 4xx/5xx.
// ---------------------------------------------------------------------------

const CLAUSE_REASSESS_DELAY_MS = 600;

// Timers and sequence counters keyed by clause_id so parallel clauses don't
// cancel each other's debounce.
const clauseReassessTimers = {};
const clauseReassessSequences = {};

// Track which clause_ids have an active in-flight request so the card can show
// a pending indicator.  Updated synchronously before and after each fetch.
// Maps clause_id -> { pending: bool, error: string|null }
const clauseReassessState = {};

function scheduleClauseReassess(clauseId, editedParagraphs) {
  if (!clauseId) return;
  const matterId = state.selectedMatter?.id;
  if (!matterId) return;

  if (clauseReassessTimers[clauseId] !== undefined) {
    window.clearTimeout(clauseReassessTimers[clauseId]);
  }
  const sequence = (clauseReassessSequences[clauseId] || 0) + 1;
  clauseReassessSequences[clauseId] = sequence;

  clauseReassessTimers[clauseId] = window.setTimeout(() => {
    delete clauseReassessTimers[clauseId];
    runClauseReassess(clauseId, matterId, sequence, editedParagraphs);
  }, CLAUSE_REASSESS_DELAY_MS);
}

async function runClauseReassess(clauseId, matterId, sequence, editedParagraphs) {
  if (clauseReassessSequences[clauseId] !== sequence) return;
  // Mark pending — re-render only that clause card to show the spinner.
  clauseReassessState[clauseId] = { pending: true, error: null };
  renderClauseCardById(clauseId);

  const body = { matter_id: matterId, clause_id: clauseId };
  if (Array.isArray(editedParagraphs) && editedParagraphs.length) {
    body.edited_paragraphs = editedParagraphs;
  } else {
    const sourceText = (studioNdaText && studioNdaText.value.trim()) || state.reviewSourceText.trim();
    if (sourceText) body.edited_text = sourceText;
  }

  try {
    const response = await fetch("/api/review/reassess-clause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // Stale — another reassess has already been scheduled; discard this result.
    if (clauseReassessSequences[clauseId] !== sequence) return;
    const payload = await response.json();
    if (clauseReassessSequences[clauseId] !== sequence) return;
    if (!response.ok) {
      const message = payload.error || `Reassess failed (${response.status})`;
      clauseReassessState[clauseId] = { pending: false, error: message };
      renderClauseCardById(clauseId);
      return;
    }
    // Patch the clause in the review state with the fresh result.
    const updatedClause = payload.clause;
    if (updatedClause && updatedClause.id) {
      state.reviewClauses = state.reviewClauses.map((clause) =>
        clause.id === updatedClause.id ? { ...clause, ...updatedClause } : clause,
      );
    }
    clauseReassessState[clauseId] = { pending: false, error: null };
    // Re-render the full studio result so the summary bar + all dependents stay in sync.
    renderStudioResult({ clauses: state.reviewClauses });
    updateExportButtonState();
  } catch (error) {
    if (clauseReassessSequences[clauseId] !== sequence) return;
    clauseReassessState[clauseId] = { pending: false, error: error.message || "Reassess failed." };
    renderClauseCardById(clauseId);
  }
}

// Re-render only the single clause card in the navigator lane (the cheaper path
// for pending/error state, before we have a full updated clause to render).
function renderClauseCardById(clauseId) {
  if (!studioClauseLane) return;
  const clause = state.reviewClauses.find((item) => item.id === clauseId);
  if (!clause) return;
  // Re-render just this one item by replacing its article element in the lane.
  const existing = studioClauseLane.querySelector(`[data-studio-lane-id="${CSS.escape(clauseId)}"]`);
  const article = existing?.closest("article");
  if (!article) {
    // Fall back to a full lane render if we can't find the specific article.
    renderStudioClauseLane();
    return;
  }
  const reassess = clauseReassessState[clauseId] || { pending: false, error: null };
  const status = clauseDisplayStatus(clause);
  const selected = clause.id === state.selectedReviewClauseId ? "selected" : "";
  const reviewed = hasReviewResults() && clauseReviewAcknowledged(clause.id);
  const clauseRedlines = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id);
  const redlineCount = hasReviewResults() ? clauseRedlines.length : 0;
  const allRedlinesIgnored = redlineCount > 0 && clauseRedlines.every((edit) => !redlineExportIncluded(edit));
  const comment = hasReviewResults() && Boolean(clauseReviewComment(clause.id));
  const displayName = clauseDisplayName(clause);
  const stateLabel = reviewed
    ? "Reviewed"
    : allRedlinesIgnored
      ? "Ignored"
      : redlineCount
        ? `${redlineCount} proposed ${redlineCount === 1 ? "redline" : "redlines"}`
        : status.issueLabel;
  const pendingSpinner = reassess.pending
    ? '<span class="clause-reassess-pending" aria-label="Rechecking…">…</span>'
    : "";
  const errorBadge = reassess.error
    ? `<span class="clause-reassess-error" title="${escapeHtml(reassess.error)}">!</span>`
    : "";
  const newArticle = document.createElement("article");
  newArticle.className = `studio-clause-item ${selected} ${status.tone} ${reviewed ? "reviewed" : ""} ${allRedlinesIgnored ? "ignored" : ""}`;
  newArticle.innerHTML = `
    <button class="studio-clause-select" type="button" data-studio-lane-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}" aria-label="${escapeHtml(`${displayName}: ${stateLabel}`)}" title="${escapeHtml(`${displayName}: ${stateLabel}`)}">
      <span class="studio-clause-dot ${status.dotTone}"></span>
      <span class="studio-clause-title">${escapeHtml(displayName)}</span>
      ${clauseEngineBadge(clause)}
      ${comment ? '<span class="studio-comment-state">Comment</span>' : ""}
      ${pendingSpinner}
      ${errorBadge}
    </button>
  `;
  newArticle.querySelector("[data-studio-lane-id]")?.addEventListener("click", () => {
    selectReviewClause(clause.id, { jump: true });
  });
  article.replaceWith(newArticle);
  // If the detail panel is showing this clause, refresh it too.
  if (state.selectedReviewClauseId === clauseId && state.reviewInspectorView === "clause") {
    renderStudioDetail();
  }
}
