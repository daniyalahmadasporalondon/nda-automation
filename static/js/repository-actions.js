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
      try {
        state.matters = await api.listMatters();
        if (getSelectedMatter() && !state.matters.find((matter) => matter.id === getSelectedMatter().id)) {
          setSelectedMatter(null);
          renderEmptyPanel();
        }
        if (getPendingDeleteMatterId() && !state.matters.find((matter) => matter.id === getPendingDeleteMatterId())) {
          setPendingDeleteMatterId(null);
        }
        renderBoard();
      } catch (error) {
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
        console.warn(error.message || "Matter could not load");
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
          setPanelMessage(error.message || "Matter could not be deleted");
        } else {
          console.warn(error.message || "Matter could not be deleted");
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
      const reviewMatter = await loadMatterReview(selectedReviewMatter.id, { refresh: true });
      if (!reviewMatter) {
        if (typeof showMatterReviewLoadError === "function") {
          showMatterReviewLoadError("Matter review details could not load.");
        } else {
          setPanelMessage("Matter review details could not load.");
        }
        return;
      }
      loadMatterIntoReview(reviewMatter);
    }

    async function loadMatterReview(matterId, options = {}) {
      try {
        return await api.getMatterReview(matterId, options);
      } catch (error) {
        console.warn(error.message || "Matter review details could not load");
        return null;
      }
    }

    // Re-run a stale matter's review against the active published Playbook from the
    // inspector, then refresh the panel + board so the stale badge clears on success.
    async function refreshMatterReview(matter) {
      const refreshButton = repositoryMatterPanel?.querySelector(".repository-refresh-review");
      const previousLabel = refreshButton?.textContent || "Refresh Review";
      if (refreshButton) {
        refreshButton.disabled = true;
        refreshButton.textContent = "Refreshing";
      }
      setPanelMessage("Refreshing review against the active Playbook.");
      try {
        const reviewMatter = await api.getMatterReview(matter.id, { refresh: true });
        await loadMatters();
        // loadMatters resets state.matters from the list; keep the richer review
        // payload (with review_refresh) as the selected matter for the panel.
        if (getSelectedMatter()?.id === matter.id || !getSelectedMatter()) {
          setSelectedMatter(reviewMatter);
          renderDetailPanel(reviewMatter);
        }
        renderBoard();
        const refresh = reviewMatter?.review_refresh || {};
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
      const docxChoice = hasManagedDocxOption
        ? DocumentDownloadMenu.contractChoice(reviewedDocx, {
            label: "DOCX",
            onSelect: () => exportMatter(matter),
            unavailableReason: "DOCX is not available for this reviewed document yet.",
          })
        : null;
      DocumentDownloadMenu.open(anchor, {
        label: "Download reviewed document",
        sections: [{
          label: "Reviewed redline",
          choices: [
            docxChoice || {
              available: true,
              filename: reviewedDocx?.filename || redlineDownloadFilename(matter.source_filename || matter.document_title || "nda.docx"),
              format: "docx",
              label: "DOCX",
              onSelect: () => exportMatter(matter),
            },
            DocumentDownloadMenu.contractChoice(reviewedPdf, {
              label: "PDF",
              onSelect: (choice) => downloadMatterPdf(matter, choice),
              unavailableReason: "PDF is not available for this reviewed document yet.",
            }),
          ],
        }],
      });
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
        const blob = await response.blob();
        downloadBlob(blob, filename);
        if (MatterUtils.needsHumanReview(matter)) {
          setPanelMessage(`Downloading ${filename}. Matter still needs human review before send.`);
        } else {
          const movedMatter = await moveMatterToColumn(matter.id, "reviewed", { quiet: true });
          setPanelMessage(movedMatter ? `Downloading ${filename}. Moved to Reviewed.` : `Downloading ${filename}. Stage could not update.`);
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
        setPanelMessage("PDF is not available for this reviewed document yet.");
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
        setPanelMessage("Matter does not have a valid reply recipient email address.");
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
        setPanelMessage(`Sent redline to ${recipient}.`);
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
        : "Matter folder up to date.";
      const parts = [`<span class="repository-drive-summary">${escapeHtml(summary)}</span>`];
      if (folderUrl) {
        parts.push(
          `<a class="repository-detail-link repository-drive-folder-link" href="${escapeHtml(folderUrl)}" target="_blank" rel="noopener">Open matter folder</a>`,
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
          setPanelMessage(error.message || "Matter could not move");
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
