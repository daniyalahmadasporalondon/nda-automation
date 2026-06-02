function clauseStatus(clause) {
  const rawStatus = clause?.status || "idle";
  const reviewState = clause?.review_state && typeof clause.review_state === "object" ? clause.review_state : {};
  const rawReviewState = String(reviewState.state || "").toLowerCase();
  const rawDecision = String(clause?.decision || "").toLowerCase();
  const hasDecision = ["pass", "review", "fail"].includes(rawDecision);
  const rawPasses = typeof clause?.passes === "boolean"
    ? clause.passes
    : rawStatus === "pass" || rawStatus === "match";
  const hasReviewState = ["pass", "review", "check", "pending"].includes(rawReviewState);
  const review = rawReviewState === "review" || rawDecision === "review" || clause?.needs_review === true;
  const fail = rawReviewState === "check" || rawDecision === "fail" || (!hasReviewState && !hasDecision && !review && !rawPasses);
  const passes = rawReviewState === "pass" || rawDecision === "pass" || (!hasReviewState && !hasDecision && rawPasses && !review && !fail);
  const idle = rawStatus === "idle";
  const tone = idle ? "pending" : review ? "review" : fail ? "check" : "pass";
  const dotTone = idle ? "pending" : review ? "review" : fail ? "verify" : "match";
  const resultLabels = {
    not_present: "Not present",
    match: "Match",
    check: "Fail",
    pass: "Match",
    fail: "Fail",
    review: "Review",
  };

  return {
    dotTone,
    fails: fail && !idle,
    issueLabel: idle ? "Pending" : review ? "Needs review" : clause?.issue_label || "Needs review",
    blocksSend: Boolean(reviewState.blocks_send),
    needsReview: review && !idle,
    passes,
    pillLabel: idle ? "Pending" : review ? "REVIEW" : fail ? "FAIL" : reviewState.label || "PASS",
    reviewState: rawReviewState || tone,
    requiresAttention: (review || fail) && !idle,
    requiresHumanReview: Boolean(reviewState.requires_human_review) || (review && !idle),
    requiresRedline: Boolean(reviewState.requires_redline) || (fail && !idle),
    resultLabel: resultLabels[rawReviewState] || resultLabels[rawDecision] || resultLabels[rawStatus] || "Pending",
    tone,
  };
}

function clausePasses(clause) {
  return clauseStatus(clause).passes;
}
