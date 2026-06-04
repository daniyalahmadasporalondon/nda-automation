const RepositoryActions = (() => {
  function create({
    api,
    downloadBlob,
    downloadFilename,
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

    async function exportMatter(matter) {
      setPendingSendMatterId(null);
      setPendingDeleteMatterId(null);
      const exportButton = repositoryMatterPanel?.querySelector(".repository-export-redline");
      setPanelMessage("");
      if (exportButton) {
        exportButton.disabled = true;
        exportButton.textContent = "Exporting";
      }
      try {
        const response = await api.exportReviewDocx(matter.id);
        const filename = downloadFilename(response) || redlineDownloadFilename(matter.source_filename || matter.document_title || "nda.docx");
        const blob = await response.blob();
        downloadBlob(blob, filename);
        if (MatterUtils.needsHumanReview(matter)) {
          setPanelMessage(`Downloading ${filename}. Matter still needs human review before send.`);
        } else {
          const movedMatter = await moveMatterToColumn(matter.id, "redline_ready", { quiet: true });
          setPanelMessage(movedMatter ? `Downloading ${filename}. Moved to Redline Ready.` : `Downloading ${filename}. Stage could not update.`);
        }
      } catch (error) {
        setPanelMessage(error.message || "Export could not run");
      } finally {
        if (exportButton?.isConnected) {
          exportButton.disabled = false;
          exportButton.textContent = "Export Redline";
        }
      }
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
        setPanelMessage(error.message || "Redline email could not send");
      }
    }

    async function closeMatterWorkflow(matter) {
      setPendingSendMatterId(null);
      setPendingDeleteMatterId(null);
      const closeButton = repositoryMatterPanel?.querySelector(".repository-close-matter");
      setPanelMessage("");
      if (closeButton) {
        closeButton.disabled = true;
        closeButton.textContent = "Closing";
      }
      const movedMatter = await moveMatterToColumn(matter.id, "signed_closed", { quiet: true });
      setPanelMessage(movedMatter ? "Moved to Signed / Closed." : "Matter could not move.");
      if (closeButton?.isConnected && !movedMatter) {
        closeButton.disabled = false;
        closeButton.textContent = "Close Matter";
      }
    }

    async function markMatterRedlineReady(matter) {
      if (!matter?.id) return null;
      if (MatterUtils.needsHumanReview(matter)) return null;
      return moveMatterToColumn(matter.id, "redline_ready", { quiet: true });
    }

    async function compareMatterReview(matterId) {
      const targetMatterId = matterId || state.selectedMatter?.id || getSelectedMatter()?.id;
      if (!targetMatterId) return null;
      const previousReviewMatter = state.selectedMatter?.id === targetMatterId ? state.selectedMatter : null;
      const payload = await api.compareMatterReview(targetMatterId);
      const comparison = payload.review_comparison || null;
      if (payload.matter?.id) {
        replaceMatter(payload.matter);
      }
      if (previousReviewMatter) {
        state.selectedMatter = {
          ...previousReviewMatter,
          ...(payload.matter || {}),
          review_comparison: comparison,
        };
        setReviewComparison(comparison);
      }
      return comparison;
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

    return {
      cancelDeleteMatter,
      closeMatterWorkflow,
      closePanel,
      compareMatterReview,
      deleteMatter,
      exportMatter,
      loadGmailStatus,
      loadMatters,
      markMatterRedlineReady,
      moveMatterToColumn,
      openMatter,
      openMatterInReview,
      requestDeleteMatter,
      sendRedline,
    };
  }

  return { create };
})();
