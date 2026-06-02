function clauseStatus(clause) {
  const rawStatus = clause?.status || "idle";
  const passes = typeof clause?.passes === "boolean"
    ? clause.passes
    : rawStatus === "pass" || rawStatus === "match";
  const idle = rawStatus === "idle";
  const tone = idle ? "pending" : passes ? "pass" : "check";
  const dotTone = idle ? "pending" : passes ? "match" : "verify";
  const resultLabels = {
    not_present: "Not present",
    match: "Match",
    check: "Check",
    pass: "Match",
    fail: "Check",
  };

  return {
    dotTone,
    issueLabel: idle ? "Pending" : clause?.issue_label || "Needs review",
    needsReview: !passes && !idle,
    passes,
    pillLabel: idle ? "Pending" : passes ? "PASS" : "CHECK",
    resultLabel: resultLabels[rawStatus] || "Pending",
    tone,
  };
}

function clausePasses(clause) {
  return clauseStatus(clause).passes;
}
