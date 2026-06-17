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
      personalisationSettings: null,
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
      reviewDocumentRender: null,
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
      reasoningTrailOpen: {},
      reviewResolution: null,
      approveServerBlocks: [],
      // "overview" is the default/first inspector sub-tab — the at-a-glance pane
      // (facts + clause roster + footer) shown before drilling into a clause.
      reviewInspectorView: "overview",
      // Inbound-fill tool: records the blanks the user has filled with Aspora
      // entity values. Each entry: { id, paragraph_id, find, value, field, mode }
      // (mode = "clean" | "tracked"). The export payload carries the
      // {paragraph_id, find, value, mode} subset as a top-level `fills` array.
      filledBlanks: [],
      documentViewMode,
    };
  }

  function resetReviewResults(state) {
    const reviewInspectorView = state.reviewInspectorView || "overview";
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
