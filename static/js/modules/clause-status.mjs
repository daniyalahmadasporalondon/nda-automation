export function clauseStatus(clause) {
  const rawStatus = clause?.status || "idle";
  const reviewState = clause?.review_state && typeof clause.review_state === "object" ? clause.review_state : {};
  const rawReviewState = String(reviewState.state || "").toLowerCase();
  const rawDecision = String(clause?.decision || "").toLowerCase();
  const hasDecision = ["pass", "review", "fail"].includes(rawDecision);
  const rawPasses = typeof clause?.passes === "boolean"
    ? clause.passes
    : rawStatus === "pass" || rawStatus === "match";
  const hasReviewState = ["pass", "review", "check", "pending"].includes(rawReviewState);

  // Single source of truth: the backend already ran the canonical verdict
  // (decision_arbiter -> review_state, including the confidence < 0.75 rule and
  // the unknown -> review fail-safe) and attaches it as clause.review_state.state
  // / clause.decision. CONSUME that verdict here instead of re-deriving a second,
  // drifting opinion from the raw fields. The legacy raw-field derivation below
  // is the fallback only for clauses that predate the canonical verdict (old
  // fixtures, or a dynamic result that carries neither review_state nor decision).
  const canonicalState = hasReviewState ? rawReviewState : (hasDecision ? _decisionState(rawDecision) : "");
  let review;
  let fail;
  let passes;
  let idle;
  if (canonicalState) {
    review = canonicalState === "review";
    fail = canonicalState === "check";
    passes = canonicalState === "pass";
    idle = canonicalState === "pending";
  } else {
    // No canonical verdict present -> fall back to the legacy raw-field
    // derivation. needs_review still escalates; a clause with no result signal
    // at all stays pre-review "Pending" (idle), matching prior behavior.
    review = clause?.needs_review === true;
    fail = !review && !rawPasses && (rawStatus === "check" || rawStatus === "not_present" || rawStatus === "fail");
    passes = rawPasses && !review && !fail;
    const hasResultSignal = typeof clause?.passes === "boolean"
      || rawStatus === "pass" || rawStatus === "match";
    idle = !review && !fail && !passes && !hasResultSignal;
  }
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

// Map a canonical clause decision (the backend verdict) onto the review_state
// `state` vocabulary, so a clause that carries only `decision` (no nested
// review_state) is read through the same single source of truth. A `fail`
// decision is the "check" state (needs a redline), mirroring review_state.py.
function _decisionState(decision) {
  if (decision === "fail") return "check";
  if (decision === "review") return "review";
  if (decision === "pass") return "pass";
  return "";
}

export function clausePasses(clause) {
  return clauseStatus(clause).passes;
}

// Dynamic clause types may arrive with only an id (no curated display name),
// so every rendering path resolves a label off the result data rather than
// assume a name is present. Falls back name -> id -> "Clause" so the UI never
// shows "undefined" for a clause type the code has never seen.
export function clauseDisplayName(clause) {
  if (!clause || typeof clause !== "object") return "Clause";
  const name = String(clause.name || clause.title || clause.label || "").trim();
  if (name) return name;
  const id = String(clause.id || "").trim();
  return id || "Clause";
}

// True for a data-defined Playbook clause (engine === "dynamic"). Native clauses
// carry engine "native" or omit the field, so this is false for them — the
// Dynamic badge is purely additive and never appears on the original clauses.
export function clauseIsDynamic(clause) {
  return Boolean(clause) && typeof clause === "object"
    && String(clause.engine || "").trim().toLowerCase() === "dynamic";
}
