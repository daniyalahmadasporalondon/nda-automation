const RepositoryView = (() => {
  function createController({
    state,
    gmailDemoMatterList,
    repositorySearchInput,
    repositoryMatterPanel,
    downloadBlob,
    downloadFilename,
    downloadUrl,
    loadMatterIntoReview,
    prepareMatterReviewLoad,
    redlineDownloadFilename,
    showMatterReviewLoadError,
    reviewErrorFromPayload,
  }) {
    let selectedMatter = null;
    let pendingSendMatterId = null;
    let pendingDeleteMatterId = null;
    let searchQuery = "";
    const repositoryWorkspace = repositoryMatterPanel?.closest(".repository-workspace");
    const api = RepositoryApi.create({ reviewErrorFromPayload });
    let actions;

    repositoryMatterPanel?.addEventListener("click", (event) => {
      if (event.target === repositoryMatterPanel) actions.closePanel();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && repositoryMatterPanel && !repositoryMatterPanel.hidden) actions.closePanel();
    });
    repositorySearchInput?.addEventListener("input", () => {
      searchQuery = repositorySearchInput.value;
      renderBoard();
    });
    repositorySearchInput?.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !repositorySearchInput.value) return;
      event.preventDefault();
      event.stopPropagation();
      repositorySearchInput.value = "";
      searchQuery = "";
      renderBoard();
    });

    function renderBoard({ errorMessage = "" } = {}) {
      RepositoryBoard.renderBoard({
        errorMessage,
        gmailDemoMatterList,
        handlers: actions,
        pendingDeleteMatterId,
        searchQuery,
        selectedMatter,
        state,
      });
    }

    function renderSyncStatus() {
      RepositoryBoard.renderSyncStatus(state);
    }

    function renderDetailPanel(matter) {
      RepositoryDetail.renderDetailPanel({
        handlers: actions,
        matter,
        pendingSendMatterId,
        repositoryMatterPanel,
        repositoryWorkspace,
        state,
      });
    }

    function renderEmptyPanel() {
      RepositoryDetail.renderEmptyPanel({ repositoryMatterPanel, repositoryWorkspace });
    }

    function setPanelMessage(message) {
      RepositoryDetail.setPanelMessage(repositoryMatterPanel, message);
    }

    function setPanelMessageHtml(html) {
      RepositoryDetail.setPanelMessageHtml(repositoryMatterPanel, html);
    }

    actions = RepositoryActions.create({
      api,
      downloadBlob,
      downloadFilename,
      downloadUrl,
      getPendingDeleteMatterId: () => pendingDeleteMatterId,
      getPendingSendMatterId: () => pendingSendMatterId,
      getSelectedMatter: () => selectedMatter,
      hasBoard: Boolean(gmailDemoMatterList),
      loadMatterIntoReview,
      prepareMatterReviewLoad,
      redlineDownloadFilename,
      renderBoard,
      renderDetailPanel,
      renderEmptyPanel,
      renderSyncStatus,
      showMatterReviewLoadError,
      repositoryMatterPanel,
      setPanelMessage,
      setPanelMessageHtml,
      setPendingDeleteMatterId: (matterId) => { pendingDeleteMatterId = matterId; },
      setPendingSendMatterId: (matterId) => { pendingSendMatterId = matterId; },
      setSelectedMatter: (matter) => { selectedMatter = matter; },
      state,
    });

    return {
      loadGmailStatus: actions.loadGmailStatus,
      loadMatters: actions.loadMatters,
      markMatterRedlineReady: actions.markMatterRedlineReady,
      openMatter: actions.openMatter,
      openMatterInReview: actions.openMatterInReview,
      renderBoard,
    };
  }

  return {
    boardColumnLabel: RepositoryModel.boardColumnLabel,
    createController,
    formatMatterDate: RepositoryModel.formatMatterDate,
    renderMatterCard: RepositoryBoard.renderMatterCard,
    sourceTypeLabel: RepositoryModel.sourceTypeLabel,
    triageLabel: RepositoryModel.triageLabel,
  };
})();

function createRepositoryController(options) {
  return RepositoryView.createController(options);
}
