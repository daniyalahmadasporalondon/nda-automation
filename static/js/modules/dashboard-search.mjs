// Dashboard smart-search — v1 (DETERMINISTIC ONLY).
//
// The golden rule: every result this module returns is a real matter the app
// already loaded. We NEVER fabricate a result list and we make NO AI calls in
// v1. Free text runs a keyword filter over the matter's own fields; the quick
// chips are exact workflow_state.status filters.
//
// This file is the PURE core (no DOM) so it can be unit-tested in
// tests/frontend/utility-modules.mjs. The DOM controller lives in
// static/js/dashboard-search.js and consumes these functions.
//
// v1.1 SHIPPED: "Summarize a document" — each result row now has a Summarize
// affordance that POSTs to /api/matters/<id>/summary and renders a grounded AI
// summary inline. The pure helpers for that live at the bottom of this file
// (summaryEndpoint / formatSummaryResult / summaryErrorMessage / SUMMARY_LABEL);
// the DOM controller in static/js/dashboard-search.js consumes them.
//
// STILL DEFERRED (need backend work we don't have yet):
//   * "Find documents linked to a counterparty" -> counterparty data is weak for
//        inbound matters; matching would be noisy/misleading.
//   * "Show how documents relate"             -> needs artifact-lineage rendering.

// The two solid v1 chips, each backed by a real workflow_state.status value.
// `status` is matched exactly against matter.workflow_state.status.
const DASHBOARD_SEARCH_CHIPS = [
  {
    id: "pending_approval",
    label: "Show all documents pending approval",
    kind: "status",
    status: "awaiting_approval",
  },
  {
    id: "awaiting_signature",
    label: "Show all documents awaiting signature",
    kind: "status",
    // The sent-out / waiting-on-the-other-side phase.
    status: "sent_awaiting_counterparty",
  },
];

// The workflow status of a matter, normalized to a lowercase string ("" when
// absent). The canonical source is matter.workflow_state.status; we tolerate a
// flat matter.status only as a last-resort fallback.
function matterStatus(matter) {
  const fromWorkflow = matter?.workflow_state?.status;
  if (fromWorkflow) return String(fromWorkflow).trim().toLowerCase();
  return String(matter?.status || "").trim().toLowerCase();
}

// A friendly status label for the result row. Prefer the backend's own derived
// label (workflow_state.label / next_action.label) before falling back to a
// title-cased status token, so we don't drift from the server's wording.
function matterStatusLabel(matter) {
  const label = matter?.workflow_state?.label;
  if (label) return String(label);
  const nextAction = matter?.workflow_state?.next_action?.label;
  if (nextAction) return String(nextAction);
  const status = matterStatus(matter);
  if (!status) return "";
  return status
    .split("_")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

// The display title for a result row (mirrors RepositoryModel.matterSubject).
function matterTitle(matter) {
  return (
    matter?.subject ||
    matter?.document_title ||
    matter?.source_filename ||
    "Untitled NDA"
  );
}

// The free-text haystack for one matter: every field a user might type a
// fragment of — subject/title, sender, counterparty, and the status (both the
// raw token and the friendly label). Lowercased, joined with spaces.
function matterHaystack(matter) {
  const parts = [
    matter?.subject,
    matter?.document_title,
    matter?.source_filename,
    matter?.sender,
    matter?.reply_to,
    matter?.recipient_email,
    matter?.last_outbound_to,
    matter?.counterparty,
    matter?.counterparty_name,
    matterStatus(matter),
    matterStatusLabel(matter),
  ];
  return parts
    .filter(Boolean)
    .map((part) => String(part).toLowerCase())
    .join(" ");
}

// Split a free-text query into lowercased terms. Multiple terms are ANDed: a
// matter matches only if its haystack contains every term (a simple, predictable
// keyword AND — no ranking, no fuzzy matching in v1).
function queryTerms(query) {
  return String(query || "")
    .toLowerCase()
    .split(/\s+/)
    .map((term) => term.trim())
    .filter(Boolean);
}

// Deterministic free-text keyword filter over real matters. Empty/whitespace
// query returns [] (the caller shows the idle hint, not the whole list).
function filterMattersByText(matters, query) {
  const list = Array.isArray(matters) ? matters : [];
  const terms = queryTerms(query);
  if (!terms.length) return [];
  return list.filter((matter) => {
    const haystack = matterHaystack(matter);
    return terms.every((term) => haystack.includes(term));
  });
}

// Deterministic exact filter by workflow_state.status (powers the quick chips).
function filterMattersByStatus(matters, status) {
  const list = Array.isArray(matters) ? matters : [];
  const target = String(status || "").trim().toLowerCase();
  if (!target) return [];
  return list.filter((matter) => matterStatus(matter) === target);
}

// Run a chip's backing filter against the real matters. Unknown chip -> [].
function runChip(matters, chip) {
  if (!chip || chip.kind !== "status") return [];
  return filterMattersByStatus(matters, chip.status);
}

// Find a chip definition by its id.
function chipById(chipId) {
  return DASHBOARD_SEARCH_CHIPS.find((chip) => chip.id === chipId) || null;
}

// --------------------------------------------------------------------------- //
// "Summarize a document" (v1.1) — pure helpers (no DOM, unit-testable).
// --------------------------------------------------------------------------- //

// The label the UI puts on every summary panel. The GOLDEN RULE: a generated
// summary must always be visibly marked as AI, never mistaken for verified fact.
const SUMMARY_LABEL = "AI summary";

// The fallback shown whenever the backend can't produce a summary (AI disabled,
// no key, the call failed, or any non-OK response). Matches the backend's
// friendly copy so the message is consistent wherever it surfaces.
const SUMMARY_UNAVAILABLE_MESSAGE = "Summary unavailable right now.";

// The summary endpoint for one matter. Encodes the id so an id with odd
// characters can't break out of the path.
function summaryEndpoint(matterId) {
  return `/api/matters/${encodeURIComponent(String(matterId || ""))}/summary`;
}

// Normalize a successful summary response into the fields the UI renders. We only
// ever surface the model's summary text plus its provenance (model + when it was
// generated); we never fabricate text. Returns null when the payload has no usable
// summary so the caller falls back to the unavailable message.
function formatSummaryResult(payload) {
  const text = payload && typeof payload.summary === "string" ? payload.summary.trim() : "";
  if (!text) return null;
  return {
    label: SUMMARY_LABEL,
    summary: text,
    model: payload && payload.model ? String(payload.model) : "",
    generatedAt: payload && payload.generated_at ? String(payload.generated_at) : "",
  };
}

// The user-facing error message for a failed summary. Prefer the backend's own
// friendly `error` field (it returns the exact "Summary unavailable right now."
// copy on degradation), falling back to our constant. Never surfaces a stack/HTTP
// detail.
function summaryErrorMessage(payload) {
  const fromPayload = payload && typeof payload.error === "string" ? payload.error.trim() : "";
  return fromPayload || SUMMARY_UNAVAILABLE_MESSAGE;
}

export {
  DASHBOARD_SEARCH_CHIPS,
  SUMMARY_LABEL,
  SUMMARY_UNAVAILABLE_MESSAGE,
  chipById,
  filterMattersByStatus,
  filterMattersByText,
  formatSummaryResult,
  matterHaystack,
  matterStatus,
  matterStatusLabel,
  matterTitle,
  queryTerms,
  runChip,
  summaryEndpoint,
  summaryErrorMessage,
};
