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

  // Generic snake_case -> Title Case fallback for an unmapped enum token, so a raw
  // internal code (e.g. "send_document") never leaks into the UI. Prefers the shared
  // window.humanizeId when the page provides it; otherwise applies the same rule
  // locally (display-only — the underlying value is never touched).
  function humanizeToken(token) {
    if (typeof window !== "undefined" && typeof window.humanizeId === "function") {
      return window.humanizeId(token);
    }
    const str = String(token == null ? "" : token).trim();
    if (!str) return "";
    return str
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }

  function triageLabel(status) {
    const labels = {
      ready_to_sign: "Ready to sign",
      needs_redline: "Needs redline",
      legal_review: "Legal review",
      intake_error: "Intake error",
      sent: "Sent",
    };
    return labels[status] || "Needs review";
  }

  function sourceTypeLabel(sourceType) {
    const labels = {
      gmail_demo: "Inbox",
      gmail_inbound: "Mail",
      manual_upload: "Manual Upload",
      generated: "Generated",
      send_document: "Sent NDA",
    };
    return labels[sourceType] || humanizeToken(sourceType) || "Document";
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

  function isMatterExecuted(matter) {
    // Shared contract: a matter is EXECUTED (fully-signed, 2/2, work done) when
    // matter.executed === true (workflow status "fully_signed" / phase
    // "executed"), set by DocuSign completion or a manual mark. An executed
    // matter is dropped from the WIP board -- it belongs to no column. A
    // half-signed (1/2, not executed) matter never trips this, so it stays in
    // Sent. The backend already excludes executed matters from the board
    // payload; this is the frontend backstop so a stale/cached executed matter
    // is never bucketed into a column either.
    if (!matter || typeof matter !== "object") return false;
    if (matter.executed === true) return true;
    const workflowState = matter.workflow_state;
    if (workflowState && typeof workflowState === "object") {
      if (workflowState.phase === "executed") return true;
      if (workflowState.status === "fully_signed") return true;
    }
    return false;
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

  // The signature terminal-not-signed statuses the backend derives when a DocuSign
  // envelope is declined or voided. Data-driven: the backend owns the label; we
  // only key off the status to know WHEN to surface it over the board-column label.
  const SIGNATURE_TERMINAL_STATUSES = new Set(["signature_declined", "signature_voided"]);

  // The matter's human-facing status label. Defaults to the board-column label,
  // but when the backend workflow_state reports a signature terminal-not-signed
  // state (declined / voided) we surface ITS label ("Declined — needs attention" /
  // "Voided — ready to re-send") so a dead/cancelled deal no longer reads as a
  // generic "Sent". Falls back to the column label whenever workflow_state is
  // absent or not one of those states (fully backward compatible).
  function statusLabel(matter) {
    const workflowState = matter && typeof matter.workflow_state === "object" ? matter.workflow_state : null;
    if (workflowState && SIGNATURE_TERMINAL_STATUSES.has(workflowState.status) && workflowState.label) {
      return String(workflowState.label);
    }
    return matterColumnLabel(matter);
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
    isMatterExecuted,
    manualUploadSubmissionColumn,
    matterColumn,
    matterColumnLabel,
    statusLabel,
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
