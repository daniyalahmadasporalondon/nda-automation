const RepositoryModel = (() => {
  const BOARD_COLUMNS = [
    { id: "gmail_demo", label: "Gmail Demo" },
    { id: "in_review", label: "In Review" },
    { id: "redline_ready", label: "Redline Ready" },
    { id: "signed_closed", label: "Signed / Closed" },
  ];
  const BOARD_COLUMN_IDS = new Set(BOARD_COLUMNS.map((column) => column.id));

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
      gmail_inbound: "Gmail Inbound",
      manual_upload: "Manual Upload",
    };
    return labels[sourceType] || sourceType || "Source";
  }

  function sourceBadgeClass(sourceType) {
    if (sourceType === "gmail_inbound") return "inbound";
    if (sourceType === "manual_upload") return "manual";
    return "demo";
  }

  function boardColumnLabel(boardColumn) {
    return BOARD_COLUMNS.find((column) => column.id === boardColumn)?.label || "Gmail Demo";
  }

  function matterColumn(matter) {
    return BOARD_COLUMN_IDS.has(matter?.board_column) ? matter.board_column : "gmail_demo";
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

  return {
    BOARD_COLUMNS,
    BOARD_COLUMN_IDS,
    boardColumnLabel,
    compareMatterRecency,
    formatMatterDate,
    formatMatterDateTime,
    matterColumn,
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
