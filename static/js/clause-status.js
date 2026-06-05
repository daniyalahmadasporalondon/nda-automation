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
  // A dynamic clause result may key everything off review_state/decision and
  // omit the top-level `status` field, which defaults to "idle". Only treat a
  // clause as idle (pre-review "Pending") when no real result signal is
  // present, so a reviewed dynamic clause never renders as Pending.
  const hasExplicitPasses = typeof clause?.passes === "boolean";
  const hasResultSignal = hasReviewState || hasDecision || hasExplicitPasses
    || rawStatus === "pass" || rawStatus === "match";
  const idle = rawStatus === "idle" && !hasResultSignal;
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
    issueLabel: idle ? "Pending" : review ? "Needs review" : fail ? "Fail" : "Pass",
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

// Dynamic clause types may arrive with only an id (no curated display name),
// so every rendering path resolves a label off the result data rather than
// assume a name is present. Falls back name -> id -> "Clause" so the UI never
// shows "undefined" for a clause type the code has never seen.
function clauseDisplayName(clause) {
  if (!clause || typeof clause !== "object") return "Clause";
  const name = String(clause.name || clause.title || clause.label || "").trim();
  if (name) return name;
  const id = String(clause.id || "").trim();
  return id || "Clause";
}
