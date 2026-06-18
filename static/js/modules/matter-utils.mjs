const EMAIL_PATTERN = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;

export function emailAddress(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const bracketed = text.match(/<([^<>]+)>/);
  const candidate = bracketed?.[1] || text;
  return candidate.match(EMAIL_PATTERN)?.[0] || "";
}

export function sameEmailAddress(left, right) {
  return Boolean(left && right && String(left).trim().toLowerCase() === String(right).trim().toLowerCase());
}

export function recipientEmail(matter) {
  return String(matter?.recipient_email || "");
}

export function counterpartyEmail(matter, gmailStatus = {}) {
  // Prefer the backend-derived counterparty email when present: for a matter with
  // a DocuSign envelope this is the real COUNTERPARTY signer's address, which can
  // diverge from the inbound reply recipient the derivation chain below reflects.
  // Defensive: only honour a non-empty string; otherwise fall back to derivation.
  const derived = emailAddress(matter?.counterparty_email);
  if (derived) return derived;
  const ownEmails = [
    matter?.gmail_account,
    gmailStatus?.inbound?.email,
    gmailStatus?.outbound?.email,
  ].map(emailAddress).filter(Boolean);
  const candidates = [
    matter?.recipient_email,
    matter?.reply_to,
    matter?.sender,
    matter?.last_outbound_to,
  ];
  for (const candidate of candidates) {
    const email = emailAddress(candidate);
    if (!email) continue;
    if (ownEmails.some((ownEmail) => sameEmailAddress(ownEmail, email))) continue;
    return email;
  }
  return "";
}

export function reviewState(matter) {
  const directState = matter?.review_state;
  if (directState && typeof directState === "object") return directState;
  const resultState = matter?.review_result?.review_state;
  if (resultState && typeof resultState === "object") return resultState;
  return null;
}

export function needsHumanReview(matter) {
  const state = reviewState(matter);
  if (state) {
    if (state.requires_human_review === true || state.blocks_send === true) return true;
    if (String(state.state || "").toLowerCase() === "review") return true;
    const counts = state.counts && typeof state.counts === "object" ? state.counts : null;
    if (counts && Number(counts.review || 0) > 0) return true;
  }
  const reviewResult = matter?.review_result || {};
  const overallStatus = String(reviewResult.overall_status || matter?.overall_status || "");
  const reviewCount = Number(matter?.requirements_needs_review ?? reviewResult.requirements_needs_review ?? 0);
  return overallStatus === "needs_review" || reviewCount > 0;
}

export function canSendRedline(matter) {
  return Boolean(matter?.can_send_redline && recipientEmail(matter) && (!needsHumanReview(matter) || matter?.human_reviewed));
}

export function gmailSendBlock(matter, gmailStatus = {}) {
  if (matter?.send_block_reason) return String(matter.send_block_reason);
  if (needsHumanReview(matter) && !matter?.human_reviewed) return "Matter needs human review before a redline can be sent.";
  if (!canSendRedline(matter)) return "Matter does not have a valid reply recipient email address.";
  const outbound = gmailStatus?.outbound || {};
  if (outbound.enabled === false) return "Gmail outbound is disabled in Admin.";
  if (outbound.ready === false) return outbound.error || "Outbound Gmail is not ready.";
  const recipient = recipientEmail(matter).trim().toLowerCase();
  const ownEmails = [
    matter?.gmail_account,
    gmailStatus?.inbound?.email,
    outbound.email,
  ].map((email) => String(email || "").trim().toLowerCase()).filter(Boolean);
  if (recipient && ownEmails.includes(recipient)) {
    return `Matter appears to be an outbound or self-sent Gmail message; refusing to send a redline back to ${recipient}.`;
  }
  const matterInbound = String(matter?.gmail_account || "").trim().toLowerCase();
  const outboundEmail = String(outbound.email || "").trim().toLowerCase();
  if (matterInbound && outboundEmail && matterInbound !== outboundEmail) {
    return `Outbound Gmail account ${outbound.email} does not match inbound Gmail account ${matter.gmail_account}.`;
  }
  return "";
}

// True when a matter's stored review is stale against the active published
// Playbook and should be refreshed before export/send. Reads both the list-level
// `review_stale` flag (from GET /api/matters) and the richer `review_refresh.stale`
// present once a matter is opened into review — so the indicator works on the
// board and in the inspector regardless of which payload the matter came from.
export function reviewStale(matter) {
  if (matter?.review_stale === true) return true;
  return Boolean(matter?.review_refresh?.stale);
}

// Stale reasons (e.g. ["playbook_changed"]) from whichever payload carries them.
export function reviewStaleReasons(matter) {
  const fromRefresh = matter?.review_refresh?.stale_reasons;
  if (Array.isArray(fromRefresh) && fromRefresh.length) return fromRefresh.map(String);
  const fromList = matter?.review_stale_reasons;
  if (Array.isArray(fromList) && fromList.length) return fromList.map(String);
  return [];
}

// Short, human label for a stale matter badge/tooltip.
export function reviewStaleLabel(matter) {
  if (!reviewStale(matter)) return "";
  const message = String(matter?.review_refresh?.stale_message || matter?.review_stale_message || "").trim();
  if (message) return message;
  const reasons = reviewStaleReasons(matter);
  if (reasons.includes("playbook_changed")) {
    return "Active Playbook changed since this review. Refresh before exporting or sending.";
  }
  if (reasons.includes("review_engine_version_changed")) {
    return "Review engine changed since this review. Refresh before exporting or sending.";
  }
  return "Review is out of date. Refresh against the active Playbook.";
}

// True when a matter has a review action a user can run right now: it has either
// never been AI-reviewed (so the FIRST review can be run) OR its stored review is
// stale against the active Playbook (so it can be refreshed). Drives whether the
// inspector exposes a "Run AI review" / "Refresh Review" control. Never-reviewed
// is detected via the explicit ``ai_review_ran === false`` flag; older payloads
// that lack the flag fall back to the stale-only signal so nothing regresses.
export function reviewActionable(matter) {
  if (matter?.ai_review_ran === false) return true;
  return reviewStale(matter);
}

// True when a matter has never had an AI review run. Lets the inspector label the
// review control "Run AI review" (first run) vs "Refresh Review" (re-run).
export function reviewNeverRan(matter) {
  return matter?.ai_review_ran === false;
}

// True when an AI review is currently running in the background for this matter.
// The backend now schedules the review asynchronously (POST /review-refresh
// returns 202) and stamps `review_status` on every matter payload. An
// `in_progress` status means a worker is mid-review; the board and review header
// surface a "Reviewing…" affordance and downstream actions stay disabled until it
// resolves to `completed`/`failed`/`idle`.
export function reviewInProgress(matter) {
  return String(matter?.review_status || "") === "in_progress";
}

// True when the most recent background review failed (e.g. the worker crashed or
// the server aged out a stuck in_progress past its TTL). Drives the inline error +
// Retry affordance.
export function reviewFailed(matter) {
  return String(matter?.review_status || "") === "failed";
}

export function gmailSendButtonLabel(blockReason) {
  if (!blockReason) return "";
  if (blockReason.includes("disabled")) return "Outbound Off";
  if (blockReason.includes("does not match")) return "Account Mismatch";
  if (blockReason.includes("human review")) return "Needs Review";
  if (blockReason.includes("self-sent")) return "Self-Sent";
  if (blockReason.includes("sender") || blockReason.includes("reply recipient")) return "No Reply";
  return "Gmail Setup";
}

export const MatterUtils = {
  canSendRedline,
  counterpartyEmail,
  emailAddress,
  gmailSendBlock,
  gmailSendButtonLabel,
  needsHumanReview,
  recipientEmail,
  reviewActionable,
  reviewFailed,
  reviewInProgress,
  reviewNeverRan,
  reviewStale,
  reviewStaleLabel,
  reviewStaleReasons,
  reviewState,
};
