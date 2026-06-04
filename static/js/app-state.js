const AppState = (() => {
  function createInitialState({ documentViewMode = "redline" } = {}) {
    return {
      playbookClauses: [],
      selectedClauseId: null,
      selectedDocument: null,
      selectedMatter: null,
      matters: [],
      ...initialReviewState({ documentViewMode }),
      gmailStatus: null,
    };
  }

  function initialReviewState({ documentViewMode = "redline" } = {}) {
    return {
      reviewClauses: [],
      reviewExportOriginalParagraphs: [],
      reviewOriginalParagraphs: [],
      reviewParagraphs: [],
      reviewEditHistory: [],
      reviewRedlines: [],
      reviewComments: [],
      reviewComparison: null,
      reviewComparisonStatus: "idle",
      reviewComparisonError: "",
      latestReviewResult: null,
      reviewSourceText: "",
      selectedReviewClauseId: null,
      clauseJumpIndexes: {},
      exportClauseDecisions: {},
      exportRedlineDecisions: {},
      redlineTemplateSelections: {},
      redlineDraft: null,
      redlineDraftDirty: false,
      reviewedClauseIds: {},
      pendingAiSecondOpinionClauseId: null,
      aiSecondOpinionErrors: {},
      aiDraftValidations: {},
      aiDraftValidationErrors: {},
      pendingAiDraftValidationKey: null,
      reviewInspectorView: "clause",
      documentViewMode,
    };
  }

  function resetReviewResults(state) {
    const reviewInspectorView = state.reviewInspectorView || "clause";
    Object.assign(state, initialReviewState({ documentViewMode: state.documentViewMode }));
    state.reviewInspectorView = reviewInspectorView;
  }

  function clearSourceSelection(state) {
    state.selectedDocument = null;
    state.selectedMatter = null;
  }

  return {
    clearSourceSelection,
    createInitialState,
    resetReviewResults,
  };
})();
