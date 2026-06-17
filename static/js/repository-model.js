const RepositoryModel = (() => {
  const BOARD_COLUMNS = [
    { id: "generated", label: "Generated" },
    { id: "manual_upload", label: "Upload" },
    { id: "gmail_demo", label: "Inbox" },
    { id: "in_review", label: "In Review" },
    { id: "reviewed", label: "Reviewed" },
    { id: "sent", label: "Sent" },
  ];
  const MANUAL_UPLOAD_COLUMN_ID = "manual_upload";
  const MANUAL_UPLOAD_STORAGE_COLUMN_ID = "in_review";
  // The raw arrival columns a matter sits in BEFORE any AI review runs. An
  // AI-reviewed matter must escape these and advance to "In Review"; the later
  // columns (in_review/reviewed/sent) are never pulled backward.
  const INTAKE_COLUMN_IDS = new Set(["manual_upload", "gmail_demo", "generated"]);
  const BOARD_COLUMN_IDS = new Set(BOARD_COLUMNS.map((column) => column.id));
  const LEGACY_BOARD_COLUMN_IDS = {
    redline_ready: "reviewed",
    signed_closed: "sent",
  };

  function triageLabel(status) {
    const labels = {
      ready_to_sign: "Ready",
      needs_redline: "Redline",
      legal_review: "Legal",
      intake_error: "Error",
    };
    return labels[status] || "Review";
  }

  function sourceTypeLabel(sourceType) {
    const labels = {
      gmail_demo: "Gmail Demo",
      gmail_inbound: "Mail",
      manual_upload: "Manual Upload",
      generated: "Generated",
    };
    return labels[sourceType] || sourceType || "Source";
  }

  function sourceBadgeClass(sourceType) {
    if (sourceType === "gmail_inbound") return "inbound";
    if (sourceType === "manual_upload") return "manual";
    return "demo";
  }

  function boardColumnLabel(boardColumn) {
    const columnId = canonicalBoardColumn(boardColumn);
    return BOARD_COLUMNS.find((column) => column.id === columnId)?.label || "Inbox";
  }

  function canonicalBoardColumn(boardColumn) {
    if (BOARD_COLUMN_IDS.has(boardColumn)) return boardColumn;
    return LEGACY_BOARD_COLUMN_IDS[boardColumn] || "gmail_demo";
  }

  function matterColumn(matter) {
    const boardColumn = canonicalBoardColumn(matter?.board_column);
    // A manual upload is STORED as "in_review" and normally displayed back as
    // "Upload" (the remap below). Treat that displayed-as-Upload state as an
    // intake column for the advance check, so an AI-reviewed upload escapes
    // "Upload" too.
    const isUploadDisplay =
      matter?.source_type === "manual_upload" && boardColumn === MANUAL_UPLOAD_STORAGE_COLUMN_ID;
    // Forward-only advance: once an AI review has run, an intake matter (Upload /
    // Inbox / Generated) jumps to "In Review", regardless of source. This OVERRIDES
    // the manual-upload remap below so AI-reviewed uploads escape "Upload" too. We
    // only advance intake columns -- reviewed/sent (and an already-in_review
    // non-upload matter) are left untouched, so nothing is ever pulled backward.
    if (matter?.ai_review_ran === true && (INTAKE_COLUMN_IDS.has(boardColumn) || isUploadDisplay)) {
      return "in_review";
    }
    if (isUploadDisplay) {
      return MANUAL_UPLOAD_COLUMN_ID;
    }
    return boardColumn;
  }

  function matterColumnLabel(matter) {
    return boardColumnLabel(matterColumn(matter));
  }

  function manualUploadSubmissionColumn(boardColumn) {
    const column = canonicalBoardColumn(boardColumn);
    return column === MANUAL_UPLOAD_COLUMN_ID ? MANUAL_UPLOAD_STORAGE_COLUMN_ID : column;
  }

  function matterSubject(matter) {
    return matter?.subject || matter?.document_title || matter?.source_filename || "Untitled NDA";
  }

  function matterSender(matter) {
    return matter?.sender || sourceTypeLabel(matter?.source_type);
  }

  function compareMatterRecency(left, right) {
    return matterTimeValue(right) - matterTimeValue(left);
  }

  function matterTimeValue(matter) {
    const timestamp = Date.parse(matter?.received_at || matter?.created_at || matter?.updated_at || "");
    return Number.isNaN(timestamp) ? 0 : timestamp;
  }

  function formatMatterDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
  }

  function formatMatterDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString(undefined, {
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      month: "short",
    });
  }

  function reviewStateCount(counts, key, fallback) {
    if (!counts || typeof counts !== "object") return fallback;
    const value = Number(counts[key]);
    return Number.isFinite(value) ? value : fallback;
  }

  function playbookMatchLabel(matter, reviewResult) {
    const counts = MatterUtils.reviewState({ ...matter, review_result: reviewResult })?.counts || {};
    const passed = reviewStateCount(counts, "pass", Number(matter?.requirements_passed ?? reviewResult?.requirements_passed ?? 0));
    const failed = reviewStateCount(counts, "check", Number(matter?.requirements_failed ?? reviewResult?.requirements_failed ?? 0));
    const review = reviewStateCount(counts, "review", Number(matter?.requirements_needs_review ?? reviewResult?.requirements_needs_review ?? 0));
    const total = passed + failed + review;
    if (!total) return "Not checked";
    return `${Math.round((passed / total) * 100)}%`;
  }

  function reviewCountSummary(matter, reviewResult = {}) {
    const counts = MatterUtils.reviewState({ ...matter, review_result: reviewResult })?.counts || {};
    const passed = reviewStateCount(counts, "pass", Number(matter?.requirements_passed ?? reviewResult.requirements_passed ?? 0));
    const review = reviewStateCount(counts, "review", Number(matter?.requirements_needs_review ?? reviewResult.requirements_needs_review ?? 0));
    const failed = reviewStateCount(counts, "check", Number(matter?.requirements_failed ?? reviewResult.requirements_failed ?? 0));
    const parts = [`${passed} passed`];
    if (review) parts.push(`${review} review`);
    parts.push(`${failed} failed`);
    return parts.join(" / ");
  }

  function counterpartyNeedsConfirmation(matter) {
    // The backend derives the authoritative flag (missing extraction OR
    // unverified OR confidence < 0.75 -> true). Trust it when present; only when
    // absent do we fail open to "needs confirmation" (an unconfirmed name should
    // never silently look confirmed). A non-flag/undefined value is treated as
    // present-and-false only when it is an explicit boolean false.
    if (!matter || typeof matter !== "object") return true;
    const flag = matter.counterparty_needs_confirmation;
    if (flag === true) return true;
    if (flag === false) return false;
    return true;
  }

  return {
    BOARD_COLUMNS,
    BOARD_COLUMN_IDS,
    boardColumnLabel,
    canonicalBoardColumn,
    compareMatterRecency,
    counterpartyNeedsConfirmation,
    formatMatterDate,
    formatMatterDateTime,
    manualUploadSubmissionColumn,
    matterColumn,
    matterColumnLabel,
    matterSender,
    matterSubject,
    playbookMatchLabel,
    reviewCountSummary,
    reviewStateCount,
    sourceBadgeClass,
    sourceTypeLabel,
    triageLabel,
  };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { RepositoryModel };
}
