function clauseStatus(clause) {
  const rawStatus = clause?.status || "idle";
  const rawDecision = String(clause?.decision || "").toLowerCase();
  const hasDecision = ["pass", "review", "fail"].includes(rawDecision);
  const rawPasses = typeof clause?.passes === "boolean"
    ? clause.passes
    : rawStatus === "pass" || rawStatus === "match";
  const review = rawDecision === "review" || clause?.needs_review === true;
  const fail = rawDecision === "fail" || (!hasDecision && !review && !rawPasses);
  const passes = rawDecision === "pass" || (!hasDecision && rawPasses && !review && !fail);
  const idle = rawStatus === "idle";
  const tone = idle ? "pending" : review ? "review" : fail ? "check" : "pass";
  const dotTone = idle ? "pending" : review ? "review" : fail ? "verify" : "match";
  const resultLabels = {
    not_present: "Not present",
    match: "Match",
    check: "Check",
    pass: "Match",
    fail: "Check",
    review: "Review",
  };

  return {
    dotTone,
    fails: fail && !idle,
    issueLabel: idle ? "Pending" : review ? "Needs review" : clause?.issue_label || "Needs review",
    needsReview: review && !idle,
    passes,
    pillLabel: idle ? "Pending" : review ? "REVIEW" : fail ? "CHECK" : "PASS",
    requiresAttention: (review || fail) && !idle,
    resultLabel: resultLabels[rawDecision] || resultLabels[rawStatus] || "Pending",
    tone,
  };
}

function clausePasses(clause) {
  return clauseStatus(clause).passes;
}
