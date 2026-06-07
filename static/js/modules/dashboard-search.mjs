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
// DEFERRED to v1.1 (need backend work we don't have yet — left here as a map of
// the chips the mockup showed but we are NOT shipping):
//   * "Summarize a document"                  -> needs an AI summarization endpoint.
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

export {
  DASHBOARD_SEARCH_CHIPS,
  chipById,
  filterMattersByStatus,
  filterMattersByText,
  matterHaystack,
  matterStatus,
  matterStatusLabel,
  matterTitle,
  queryTerms,
  runChip,
};
