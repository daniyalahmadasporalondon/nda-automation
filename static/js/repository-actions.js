const RepositoryActions = (() => {
  function create({
    api,
    downloadBlob,
    downloadFilename,
    downloadUrl,
    getPendingDeleteMatterId,
    getPendingSendMatterId,
    getSelectedMatter,
    hasBoard,
    loadMatterIntoReview,
    prepareMatterReviewLoad,
    redlineDownloadFilename,
    renderBoard,
    renderDetailPanel,
    renderEmptyPanel,
    renderSyncStatus,
    repositoryMatterPanel,
    setPanelMessage,
    setPanelMessageHtml,
    setPendingDeleteMatterId,
    setPendingSendMatterId,
    setSelectedMatter,
    showMatterReviewLoadError,
    state,
  }) {
    // Monotonic token so a slow STALE listMatters() (e.g. the 15s auto-refresh
    // racing an in-flight send or a prior overlapping refresh) that resolves
    // AFTER a newer loadMatters can't clobber fresh state.matters / the board
    // (last-write-wins). Mirrors searchRunToken in dashboard-search.js.
    let loadMattersRunToken = 0;

    async function loadGmailStatus() {
      try {
        state.gmailStatus = await api.loadGmailStatus();
      } catch (error) {
        state.gmailStatus = {
          inbound: { ready: false, error: error.message || "Status unavailable" },
          outbound: { ready: false, error: error.message || "Status unavailable" },
        };
      }
      renderSyncStatus();
    }

    async function loadMatters() {
      if (!hasBoard) return;
      const token = ++loadMattersRunToken;
      try {
        const matters = await api.listMatters();
        // A newer loadMatters (overlapping refresh / a send-triggered reload)
        // superseded this one — drop this STALE response without touching state
        // or the DOM, so a slow stale list can't overwrite fresh matters.
        if (token !== loadMattersRunToken) return;
        state.matters = matters;
        // Preserve the richer selected matter (from api.getMatter): if the open
        // matter is still in the lean list, leave the selected object untouched —
        // do NOT replace it with the lean entry or re-render/blank the panel.
        if (getSelectedMatter() && !state.matters.find((matter) => matter.id === getSelectedMatter().id)) {
          setSelectedMatter(null);
          renderEmptyPanel();
        }
        if (getPendingDeleteMatterId() && !state.matters.find((matter) => matter.id === getPendingDeleteMatterId())) {
          setPendingDeleteMatterId(null);
        }
        renderBoard();
      } catch (error) {
        // A stale error (from a superseded refresh) must not wipe the board
        // either — drop it before mutating anything. The current run's own
        // AuthExpired / generic-error handling below is unaffected.
        if (token !== loadMattersRunToken) return;
        // A 401 means the session expired, not that the repository is empty.
        // The shared API helper already fired the global auth-expired prompt;
        // do NOT wipe `state.matters` or deselect the open matter — that would
        // blank the whole board behind a cryptic error. Leave the existing view
        // in place and surface the expiry on the board banner.
        if (globalThis.AuthExpired?.isAuthError?.(error)) {
          globalThis.AuthExpired.handleAuthExpired();
          renderBoard({ errorMessage: "Your session expired — please sign in again." });
          return;
        }
        state.matters = [];
        setSelectedMatter(null);
        setPendingDeleteMatterId(null);
        renderEmptyPanel();
        renderBoard({ errorMessage: error.message || "Repository could not load" });
      }
    }

    async function openMatter(matterId) {
      try {
        setPendingDeleteMatterId(null);
        const matter = await api.getMatter(matterId);
        setSelectedMatter(matter);
        renderBoard();
        renderDetailPanel(matter);
      } catch (error) {
        console.warn(error.message || "NDA could not load");
      }
    }

    function requestDeleteMatter(matterId) {
      if (!matterId) return;
      setPendingSendMatterId(null);
      setPendingDeleteMatterId(matterId);
      renderBoard();
      document.querySelectorAll("[data-confirm-delete-matter-id]").forEach((button) => {
        if (button.dataset.confirmDeleteMatterId === matterId) button.focus();
      });
    }

    function cancelDeleteMatter(matterId) {
      if (getPendingDeleteMatterId() !== matterId) return;
      setPendingDeleteMatterId(null);
      renderBoard();
    }

    async function deleteMatter(matterId, control) {
      if (!matterId) return;
      if (getPendingDeleteMatterId() !== matterId) {
        requestDeleteMatter(matterId);
        return;
      }
      setPendingSendMatterId(null);
      if (control) {
        control.disabled = true;
        control.setAttribute("aria-busy", "true");
      }
      try {
        await api.deleteMatter(matterId);
        state.matters = state.matters.filter((matter) => matter.id !== matterId);
        setPendingDeleteMatterId(null);
        if (getSelectedMatter()?.id === matterId) {
          setSelectedMatter(null);
          renderEmptyPanel();
        }
        if (state.selectedMatter?.id === matterId) {
          state.selectedMatter = null;
        }
        renderBoard();
      } catch (error) {
        if (control?.isConnected) {
          control.disabled = false;
          control.removeAttribute("aria-busy");
        }
        if (repositoryMatterPanel && !repositoryMatterPanel.hidden) {
          setPanelMessage(error.message || "NDA could not be deleted");
        } else {
          console.warn(error.message || "NDA could not be deleted");
        }
      }
    }

    function closePanel() {
      setSelectedMatter(null);
      setPendingSendMatterId(null);
      setPendingDeleteMatterId(null);
      renderEmptyPanel();
      renderBoard();
    }

    async function openMatterInReview(matter) {
      setPendingSendMatterId(null);
      setPendingDeleteMatterId(null);
      const updatedMatter = await moveMatterToColumn(matter.id, "in_review", { quiet: true });
      setSelectedMatter(updatedMatter || matter);
      renderBoard();
      renderDetailPanel(getSelectedMatter());
      const selectedReviewMatter = getSelectedMatter();
      if (typeof prepareMatterReviewLoad === "function") {
        prepareMatterReviewLoad(selectedReviewMatter);
        closePanel();
      }
      // Opening a matter must NOT run the AI review. Fetch the stored review only
      // (the backend returns it plus a `review_may_be_stale` flag without invoking
      // the model). The AI review is run exclusively by the explicit "Refresh with
      // AI" button via loadMatterReview(..., { refresh: true }).
      const reviewMatter = await loadMatterReview(selectedReviewMatter.id);
      if (!reviewMatter) {
        if (typeof showMatterReviewLoadError === "function") {
          showMatterReviewLoadError("NDA review details could not load.");
        } else {
          setPanelMessage("NDA review details could not load.");
        }
        return;
      }
      loadMatterIntoReview(reviewMatter);
    }

    async function loadMatterReview(matterId, options = {}) {
      try {
        return await api.getMatterReview(matterId, options);
      } catch (error) {
        console.warn(error.message || "NDA review details could not load");
        return null;
      }
    }

    // Re-run a stale matter's review against the active published Playbook from the
    // inspector, then refresh the panel + board so the stale badge clears on success.
    async function refreshMatterReview(matter) {
      // DATA-LOSS GUARD (mirrors the Review tab's refreshSelectedMatterReview): the
      // in-progress branch below overwrites the SHARED global state.selectedMatter so
      // the background-review poll (which keys on selectedMatter.id) tracks this
      // matter. If a DIFFERENT matter currently has unsaved redline edits loaded in
      // the Review tab (state.redlineDraftDirty), that overwrite would silently
      // abandon them. Confirm first — and BAIL before firing the POST or touching any
      // UI if the user cancels — so an unsaved draft on matter A is never lost by
      // refreshing matter B from the Repository inspector. Same-matter refreshes (the
      // dirty draft IS this matter) don't clobber anything, so they skip the prompt.
      const dirtyOtherMatter = Boolean(
        state?.redlineDraftDirty && state.selectedMatter?.id && state.selectedMatter.id !== matter.id,
      );
      if (
        dirtyOtherMatter
        && typeof confirmDiscardUnsavedReviewEdits === "function"
        && !confirmDiscardUnsavedReviewEdits("Refreshing this review will discard the unsaved redline edits on the NDA open in Review.")
      ) {
        return;
      }
      const refreshButton = repositoryMatterPanel?.querySelector(".repository-refresh-review");
      const previousLabel = refreshButton?.textContent || "Refresh Review";
      if (refreshButton) {
        refreshButton.disabled = true;
        refreshButton.textContent = "Refreshing";
      }
      setPanelMessage("Refreshing review against the active Playbook.");
      try {
        const reviewMatter = await api.getMatterReview(matter.id, { refresh: true });

        // ASYNC IN-PROGRESS (POST /review-refresh -> 202): the AI review now runs in
        // a background worker. getMatterReview returns an in-progress SENTINEL
        // ({ inProgress: true, matter }) that carries NO review_result yet — so we
        // must NOT write it as the selected matter (that injected a misleading BLANK
        // finished-but-empty review, the bug). Instead, mirror the Review tab's
        // refreshSelectedMatterReview: enter the in-flight UI and START POLLING. The
        // poll (review-workstation-actions.js) re-reads the matter every few seconds
        // and, on completion/idle/failure, calls repositoryController.loadMatters()
        // which re-renders the board card — clearing the "Reviewing…" badge and
        // surfacing the finished result without the operator reopening the matter.
        if (reviewMatter?.inProgress || MatterUtils.reviewInProgress(reviewMatter?.matter || reviewMatter)) {
          const inProgressMatter = reviewMatter?.matter || reviewMatter;
          // Refresh the board so the card shows the live "Reviewing…" badge now
          // (the sentinel's matter carries review_status:"in_progress").
          await loadMatters();
          renderBoard();
          // The shared background-review poll keys on state.selectedMatter.id (it is
          // the Review tab's poll, and it stops/no-ops once the active matter changes
          // away from the one it is tracking). Point it at this matter so the poll's
          // ticks run from the Repository inspector too; on completion the poll's own
          // loadMatters() clears the board badge and surfaces the finished result.
          state.selectedMatter = { ...(state.selectedMatter || {}), ...inProgressMatter };
          // startReviewPoll / enterReviewInFlightUi are globals from
          // review-workstation-actions.js (same window scope; loaded by click time).
          // typeof-guard so an isolated load order / test harness without them is a
          // no-op rather than a ReferenceError.
          if (typeof startReviewPoll === "function") {
            if (typeof enterReviewInFlightUi === "function") enterReviewInFlightUi();
            startReviewPoll(matter.id);
          }
          setPanelMessage("Review started. It will update on the board when it finishes.");
          if (refreshButton?.isConnected) {
            refreshButton.disabled = false;
            refreshButton.textContent = previousLabel;
          }
          return;
        }

        await loadMatters();
        // loadMatters resets state.matters from the list; keep the richer review
        // payload (with review_refresh) as the selected matter for the panel.
        if (getSelectedMatter()?.id === matter.id || !getSelectedMatter()) {
          setSelectedMatter(reviewMatter);
          renderDetailPanel(reviewMatter);
        }
        renderBoard();
        const refresh = reviewMatter?.review_refresh || {};
        // Non-async terminal outcomes (200): an idle no-op, a still-stale review, or
        // a cleared redline draft. Each surfaces its own inspector message below.
        if (refresh.stale) {
          setPanelMessage(MatterUtils.reviewStaleLabel(reviewMatter) || "Review is still stale.");
        } else if (refresh.redline_draft_cleared) {
          setPanelMessage(refresh.message || "Review refreshed. Saved redline draft was cleared.");
        } else {
          setPanelMessage("Review refreshed against the active Playbook.");
        }
      } catch (error) {
        setPanelMessage(error.message || "Review could not refresh.");
        if (refreshButton?.isConnected) {
          refreshButton.disabled = false;
          refreshButton.textContent = previousLabel;
        }
      }
    }

    function openDownloadMenu(matter, anchor) {
      if (!matter?.id || !anchor) return;
      setPendingSendMatterId(null);
      setPendingDeleteMatterId(null);
      const reviewedDownloads = matter.document_downloads;
      const reviewedDocx = DocumentDownloadMenu.option(reviewedDownloads, "reviewed", "docx");
      const reviewedPdf = DocumentDownloadMenu.option(reviewedDownloads, "reviewed", "pdf");
      const hasManagedDocxOption = Boolean(reviewedDocx?.source_transform || reviewedDocx?.label || reviewedDocx?.fidelity);
      // Disclose the workflow side effect of the DOCX download UP FRONT (it silently
      // moves the matter to Reviewed) plus any contents preview we already have, so
      // the operator is not surprised after the fact.
      const docxDescription = downloadDocxDescription(matter, reviewedDocx?.filename);
      const docxChoice = hasManagedDocxOption
        ? {
            ...DocumentDownloadMenu.contractChoice(reviewedDocx, {
              label: "DOCX",
              onSelect: () => exportMatter(matter),
              unavailableReason: "DOCX is not available for this reviewed NDA yet.",
            }),
            description: docxDescription,
          }
        : null;
      DocumentDownloadMenu.open(anchor, {
        label: "Download reviewed document",
        sections: [{
          label: "Reviewed redline",
          choices: [
            docxChoice || {
              available: true,
              description: docxDescription,
              filename: reviewedDocx?.filename || redlineDownloadFilename(matter.source_filename || matter.document_title || "nda.docx"),
              format: "docx",
              label: "DOCX",
              onSelect: () => exportMatter(matter),
            },
            DocumentDownloadMenu.contractChoice(reviewedPdf, {
              label: "PDF",
              onSelect: (choice) => downloadMatterPdf(matter, choice),
              unavailableReason: "PDF is not available for this reviewed NDA yet.",
            }),
          ],
        }],
      });
    }

    // Build the per-choice description for the DOCX download. It must state the
    // workflow side effect honestly: downloading advances a human-reviewed (or
    // auto-clear) matter to the Reviewed column, while a matter that still needs
    // human review is left in place. A short contents preview is appended only
    // from data the repository panel already carries (issue count / attention
    // clauses); the effective redline/comment change-summary is NOT loaded into
    // the panel, so it is intentionally omitted (see report: backend plumbing).
    function downloadDocxDescription(matter, baseFilename) {
      const parts = [];
      const filename = baseFilename
        || redlineDownloadFilename(matter.source_filename || matter.document_title || "nda.docx");
      if (filename) parts.push(filename);
      parts.push(
        MatterUtils.needsHumanReview(matter)
          ? "Downloads the reviewed Word file. This NDA still needs human review, so it stays where it is."
          : "Downloads and moves this NDA to Reviewed.",
      );
      const preview = downloadContentsPreview(matter);
      if (preview) parts.push(preview);
      return parts.join(" · ");
    }

    // A best-available contents preview from data already loaded into the panel.
    // The repository matter payload carries review findings (issue_count and the
    // review_result clauses) but NOT the effective redline edits / review comments
    // (those live only on the separately fetched review payload's redline_draft),
    // so we surface the flagged-issue count rather than fabricate a change count.
    function downloadContentsPreview(matter) {
      const reviewResult = matter.review_result || {};
      // Verdict gate (mirror repository-detail.js ~17-19): the flagged-issue count
      // is an AI verdict. A deterministic-only matter (ai_review_ran === false) must
      // not claim "Includes N flagged issues" — the AI never flagged them. Only an
      // explicit false suppresses; legacy payloads lacking the flag fall back to
      // "are there clauses" and keep the existing behavior.
      const aiReviewRan = typeof matter.ai_review_ran === "boolean"
        ? matter.ai_review_ran
        : (Array.isArray(reviewResult.clauses) && reviewResult.clauses.length > 0);
      if (!aiReviewRan) return "";
      const attentionCount = Array.isArray(reviewResult.clauses)
        ? reviewResult.clauses.filter((clause) => clause && clauseStatus(clause).requiresAttention).length
        : 0;
      const issueCount = Number(matter.issue_count || 0) || attentionCount;
      if (issueCount > 0) {
        return `Includes ${issueCount} flagged ${issueCount === 1 ? "issue" : "issues"}.`;
      }
      return "";
    }

    async function exportMatter(matter) {
      setPendingSendMatterId(null);
      setPendingDeleteMatterId(null);
      const exportButton = repositoryMatterPanel?.querySelector(".repository-download-document");
      setPanelMessage("");
      if (exportButton) {
        exportButton.disabled = true;
        exportButton.textContent = "Downloading";
      }
      try {
        const response = await api.exportReviewDocx(matter.id);
        const filename = downloadFilename(response) || redlineDownloadFilename(matter.source_filename || matter.document_title || "nda.docx");
        // PDF-source matters return a Word file RECONSTRUCTED from the PDF, not a
        // faithful original. The export endpoint marks this with the same headers
        // the Review tab reads (X-PDF-DOCX-Reconstruction, or X-Export-Verified set
        // to the pdf2docx marker); append the honest fidelity caveat so the repo
        // download toast does not imply faithful original Word output.
        const exportReconstructedFromPdf = Boolean(
          response.headers.get("X-PDF-DOCX-Reconstruction")
          || response.headers.get("X-Export-Verified") === "pdf2docx",
        );
        const reconstructionCaveat = exportReconstructedFromPdf
          ? " Best-effort Word reconstructed from PDF — formatting may differ."
          : "";
        const blob = await response.blob();
        downloadBlob(blob, filename);
        if (MatterUtils.needsHumanReview(matter)) {
          setPanelMessage(`Downloading ${filename}. NDA still needs human review before send.${reconstructionCaveat}`);
        } else {
          const movedMatter = await moveMatterToColumn(matter.id, "reviewed", { quiet: true });
          const stageMessage = movedMatter ? "Moved to Reviewed." : "Stage could not update.";
          setPanelMessage(`Downloading ${filename}. ${stageMessage}${reconstructionCaveat}`);
        }
      } catch (error) {
        setPanelMessage(repositoryStaleReviewMessage(error, "Export could not run"));
      } finally {
        if (exportButton?.isConnected) {
          exportButton.disabled = false;
          exportButton.textContent = "Download";
        }
      }
    }

    function downloadMatterPdf(matter, choice) {
      const filename = choice?.filename || redlineDownloadFilename(matter.source_filename || matter.document_title || "nda.pdf").replace(/\.docx$/i, ".pdf");
      if (!choice?.url) {
        setPanelMessage("PDF is not available for this reviewed NDA yet.");
        return;
      }
      if (typeof downloadUrl === "function") {
        downloadUrl(choice.url, filename);
      }
      setPanelMessage(`Downloading ${filename}.`);
    }

    async function sendRedline(matter) {
      const sendBlockReason = MatterUtils.gmailSendBlock(matter, state.gmailStatus);
      if (sendBlockReason) {
        setPendingSendMatterId(null);
        setPendingDeleteMatterId(null);
        renderDetailPanel(matter);
        setPanelMessage(sendBlockReason);
        return;
      }
      const recipient = MatterUtils.recipientEmail(matter);
      if (!recipient) {
        setPendingSendMatterId(null);
        setPendingDeleteMatterId(null);
        setPanelMessage("NDA does not have a valid reply recipient email address.");
        return;
      }
      if (getPendingSendMatterId() !== matter.id) {
        setPendingSendMatterId(matter.id);
        renderDetailPanel(matter);
        setPanelMessage("Review outbound email details, then confirm send.");
        return;
      }

      const sendButton = repositoryMatterPanel?.querySelector(".repository-send-redline");
      const sendPayload = RepositorySend.sendPayloadFromPanel(repositoryMatterPanel, matter);
      setPanelMessage("");
      if (sendButton) {
        sendButton.disabled = true;
        sendButton.textContent = "Sending";
      }
      try {
        const payload = await api.sendRedline(sendPayload);
        setPendingSendMatterId(null);
        if (payload.matter?.id) {
          replaceMatter(payload.matter);
          renderBoard();
          renderDetailPanel(payload.matter);
        }
        // PDF-source matters send a Word file reconstructed from the PDF; append the
        // honest formatting caveat so the operator does not assume faithful original output.
        const sendCaveat = payload.source_reconstructed_from_pdf
          ? " Note: this Word file was reconstructed from a PDF and may not preserve original formatting."
          : "";
        setPanelMessage(`Sent redline to ${recipient}.${sendCaveat}`);
      } catch (error) {
        setPendingSendMatterId(null);
        renderDetailPanel(matter);
        setPanelMessage(repositoryStaleReviewMessage(error, "Redline email could not send"));
      }
    }

    // Sync the matter's artifact history to its per-matter Google Drive folder
    // (Drive v2). Mirrors exportMatter/sendRedline: disable the button, call the
    // api, then surface the outcome in the panel. The api wrapper does NOT throw
    // on 409 (not connected) - it returns the raw status/payload so we can render
    // a Connect Google Drive affordance whose link navigates to the OAuth consent
    // flow (connect_url) instead of an error.
    async function saveMatterToDrive(matter) {
      if (!matter?.id) return;
      setPendingSendMatterId(null);
      setPendingDeleteMatterId(null);
      const driveButton = repositoryMatterPanel?.querySelector(".repository-save-to-drive");
      setPanelMessage("");
      if (driveButton) {
        driveButton.disabled = true;
        driveButton.textContent = "Syncing";
      }
      try {
        const result = await api.saveMatterToDrive(matter.id);
        const payload = result?.payload || {};
        if (result?.ok) {
          if (payload.matter?.id) {
            replaceMatter(payload.matter);
            renderBoard();
            renderDetailPanel(payload.matter);
          }
          setPanelMessageHtml(driveSyncSuccessHtml(payload.drive || {}));
          return;
        }
        if (result?.status === 409 && payload.needs_connect) {
          const connectUrl = String(payload.connect_url || "/auth/drive/start");
          setPanelMessageHtml(
            `${escapeHtml(payload.error || "Google Drive is not connected.")} `
            + `<a class="repository-detail-link repository-drive-connect" href="${escapeHtml(connectUrl)}" data-drive-connect-url="${escapeHtml(connectUrl)}">Connect Google Drive</a>`,
          );
          return;
        }
        setPanelMessage(payload.error || "NDA could not be saved to Drive.");
      } catch (error) {
        setPanelMessage(error.message || "NDA could not be saved to Drive.");
      } finally {
        if (driveButton?.isConnected) {
          driveButton.disabled = false;
          driveButton.textContent = "Save to Drive";
        }
      }
    }

    // Build the trusted-HTML confirmation for a Drive v2 sync: a folder-level
    // summary, a prominent "Open matter folder" link, and a compact per-file list
    // (filename -> drive_file_url). Every untrusted value (URLs, filenames) is run
    // through escapeHtml before it reaches the innerHTML seam.
    function driveSyncSuccessHtml(drive) {
      const syncedCount = Number(drive.synced_count) || 0;
      const folderUrl = String(drive.matter_folder_url || "");
      const summary = syncedCount > 0
        ? `Synced ${syncedCount} file${syncedCount === 1 ? "" : "s"} to Drive.`
        : "NDA folder up to date.";
      const parts = [`<span class="repository-drive-summary">${escapeHtml(summary)}</span>`];
      if (folderUrl) {
        parts.push(
          `<a class="repository-detail-link repository-drive-folder-link" href="${escapeHtml(folderUrl)}" target="_blank" rel="noopener">Open NDA folder</a>`,
        );
      }
      const artifacts = Array.isArray(drive.artifacts) ? drive.artifacts : [];
      const fileItems = artifacts
        .map((artifact) => {
          const filename = String(artifact?.filename || "Untitled file");
          const fileUrl = String(artifact?.drive_file_url || "");
          if (fileUrl) {
            return `<li><a class="repository-detail-link repository-drive-file-link" href="${escapeHtml(fileUrl)}" target="_blank" rel="noopener">${escapeHtml(filename)}</a></li>`;
          }
          return `<li><span class="repository-drive-file-name">${escapeHtml(filename)}</span></li>`;
        })
        .join("");
      if (fileItems) {
        parts.push(`<ul class="repository-drive-file-list">${fileItems}</ul>`);
      }
      return parts.join(" ");
    }

    async function markMatterRedlineReady(matter) {
      if (!matter?.id) return null;
      if (MatterUtils.needsHumanReview(matter)) return null;
      return moveMatterToColumn(matter.id, "reviewed", { quiet: true });
    }

    async function moveMatterToColumn(matterId, boardColumn, options = {}) {
      try {
        const updatedMatter = await api.moveMatterToColumn(matterId, boardColumn);
        replaceMatter(updatedMatter);
        renderBoard();
        if (options.renderPanel !== false && getSelectedMatter()?.id === updatedMatter.id) {
          renderDetailPanel(getSelectedMatter());
        }
        return updatedMatter;
      } catch (error) {
        if (!options.quiet) {
          setPanelMessage(error.message || "NDA could not move");
        }
        return null;
      }
    }

    function replaceMatter(updatedMatter) {
      if (!updatedMatter?.id) return;
      const matterIndex = state.matters.findIndex((matter) => matter.id === updatedMatter.id);
      state.matters = matterIndex >= 0
        ? state.matters.map((matter) => (matter.id === updatedMatter.id ? updatedMatter : matter))
        : [updatedMatter, ...state.matters];
      if (getSelectedMatter()?.id === updatedMatter.id) {
        setSelectedMatter(updatedMatter);
      }
      if (state.selectedMatter?.id === updatedMatter.id) {
        state.selectedMatter = updatedMatter;
      }
    }

    function repositoryStaleReviewMessage(error, fallback) {
      if (typeof isStaleReviewError === "function" && isStaleReviewError(error)) {
        const message = typeof staleReviewMessage === "function"
          ? staleReviewMessage(error.reviewRefresh, error.message || fallback)
          : (error.message || fallback);
        return `${message} Open Review to refresh.`;
      }
      return error.message || fallback;
    }

    return {
      cancelDeleteMatter,
      closePanel,
      deleteMatter,
      exportMatter,
      loadGmailStatus,
      loadMatters,
      markMatterRedlineReady,
      moveMatterToColumn,
      openDownloadMenu,
      openMatter,
      openMatterInReview,
      refreshMatterReview,
      requestDeleteMatter,
      saveMatterToDrive,
      sendRedline,
    };
  }

  return { create };
})();
