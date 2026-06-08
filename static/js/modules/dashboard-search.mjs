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

// --------------------------------------------------------------------------- //
// v2 AI smart-search — the STRUCTURED FILTER SPEC core (no DOM, unit-testable).
// --------------------------------------------------------------------------- //
//
// The golden rule, client side: the AI's only output is a filter spec. The server
// validates it; we validate it AGAIN here (defense in depth) and then apply it to
// the REAL matters deterministically — an AND of the spec's non-null dimensions,
// exactly like the v1 chips. A wrong/hallucinated spec can at worst surface a
// wrong-but-real subset, never a fabricated document.

// The schema endpoint the controller POSTs the natural-language query to.
const SEARCH_INTENT_ENDPOINT = "/api/dashboard/search-intent";

// The allowlists MIRROR the backend (nda_automation/dashboard_search_intent.py +
// workflow.py). Kept here so a compromised/garbled response can never apply an
// out-of-schema filter even if the server validator were bypassed.
const FILTER_SPEC_STATUSES = new Set([
  "received",
  "extracting",
  "extracted",
  "intake_failed",
  "rendering",
  "ai_reviewing",
  "awaiting_human",
  "auto_cleared",
  "review_failed",
  "awaiting_approval",
  "approval_blocked",
  "approved",
  "sending",
  "sent_awaiting_counterparty",
  "send_failed",
  "counter_received",
  "re_reviewing",
  "fully_signed",
]);
const FILTER_SPEC_PHASES = new Set([
  "intake",
  "review",
  "approval",
  "sent",
  "negotiation",
  "executed",
]);
const FILTER_SPEC_SORTS = new Set(["oldest", "newest"]);
const FILTER_SPEC_MAX_TEXT_CHARS = 200;
const FILTER_SPEC_MAX_MIN_AGE_DAYS = 365;

// The canonical all-null spec: every dimension absent (apply nothing).
const NULL_FILTER_SPEC = Object.freeze({
  status: null,
  phase: null,
  needs_attention: null,
  human_gate: null,
  has_issues: null,
  text: null,
  min_age_days: null,
  sort: null,
});

function validateEnumValue(value, allowed) {
  if (typeof value !== "string") return null;
  const token = value.trim().toLowerCase();
  return allowed.has(token) ? token : null;
}

function validateBoolValue(value) {
  // Strict: only a real boolean counts; truthy strings/numbers are dropped so the
  // dimension is simply not applied (mirrors the backend).
  return typeof value === "boolean" ? value : null;
}

function validateTextValue(value) {
  if (typeof value !== "string") return null;
  const cleaned = value.trim().slice(0, FILTER_SPEC_MAX_TEXT_CHARS).trim();
  return cleaned || null;
}

function validateMinAgeDays(value) {
  if (typeof value === "boolean") return null; // true must not become 1
  let days;
  if (typeof value === "number" && Number.isFinite(value)) {
    days = Math.trunc(value);
  } else if (typeof value === "string" && value.trim() !== "" && /^-?\d+$/.test(value.trim())) {
    days = parseInt(value.trim(), 10);
  } else {
    return null;
  }
  if (!Number.isFinite(days) || days < 1) return null;
  return Math.min(days, FILTER_SPEC_MAX_MIN_AGE_DAYS);
}

// Validate a (server- or model-produced) filter spec against the fixed schema.
// Out-of-enum values are dropped to null, ints are clamped, bools are coerced, and
// unknown keys are ignored. Always returns a full spec with exactly the schema's
// keys, so applying it is always safe. A non-object collapses to the all-null spec.
function validateFilterSpec(spec) {
  if (!spec || typeof spec !== "object") return { ...NULL_FILTER_SPEC };
  return {
    status: validateEnumValue(spec.status, FILTER_SPEC_STATUSES),
    phase: validateEnumValue(spec.phase, FILTER_SPEC_PHASES),
    needs_attention: validateBoolValue(spec.needs_attention),
    human_gate: validateBoolValue(spec.human_gate),
    has_issues: validateBoolValue(spec.has_issues),
    text: validateTextValue(spec.text),
    min_age_days: validateMinAgeDays(spec.min_age_days),
    sort: validateEnumValue(spec.sort, FILTER_SPEC_SORTS),
  };
}

// True when every dimension is null (the query mapped to nothing / no constraint).
function filterSpecIsEmpty(spec) {
  if (!spec || typeof spec !== "object") return true;
  return Object.keys(NULL_FILTER_SPEC).every((key) => spec[key] == null);
}

// --- per-dimension matchers (read REAL matter fields) ----------------------

function matterPhase(matter) {
  return String(matter?.workflow_state?.phase || "").trim().toLowerCase();
}

function matterNeedsAttention(matter) {
  return matter?.workflow_state?.needs_attention === true;
}

function matterHumanGate(matter) {
  return matter?.workflow_state?.human_gate === true;
}

// "Has issues" = the review flagged at least one failed OR needs-review requirement.
function matterHasIssues(matter) {
  const failed = Number(matter?.requirements_failed || 0);
  const needsReview = Number(matter?.requirements_needs_review || 0);
  return (Number.isFinite(failed) && failed > 0) || (Number.isFinite(needsReview) && needsReview > 0);
}

// The matter's age in whole days, from created_at (fallback updated_at) vs `now`.
// Returns null when no usable timestamp is present (so a min_age_days filter never
// silently includes an undated matter).
function matterAgeDays(matter, now) {
  const stamp = matter?.created_at || matter?.updated_at || "";
  const created = Date.parse(String(stamp));
  if (!Number.isFinite(created)) return null;
  const millis = (Number.isFinite(now) ? now : Date.now()) - created;
  if (!(millis >= 0)) return 0;
  return Math.floor(millis / 86400000);
}

// Apply a VALIDATED filter spec to the real matters: a deterministic AND of every
// non-null dimension, then an optional sort by created_at. `now` is injectable so
// the age dimension is testable. Empty spec -> [] (the controller shows the idle
// hint, mirroring filterMattersByText's empty-query contract). A spec is re-validated
// here so a caller can never apply an out-of-schema dimension by mistake.
function applyFilterSpec(matters, rawSpec, now = Date.now()) {
  const list = Array.isArray(matters) ? matters : [];
  const spec = validateFilterSpec(rawSpec);
  if (filterSpecIsEmpty(spec)) return [];

  let results = list.filter((matter) => {
    if (spec.status !== null && matterStatus(matter) !== spec.status) return false;
    if (spec.phase !== null && matterPhase(matter) !== spec.phase) return false;
    if (spec.needs_attention !== null && matterNeedsAttention(matter) !== spec.needs_attention) return false;
    if (spec.human_gate !== null && matterHumanGate(matter) !== spec.human_gate) return false;
    if (spec.has_issues !== null && matterHasIssues(matter) !== spec.has_issues) return false;
    if (spec.text !== null) {
      const haystack = matterHaystack(matter);
      const terms = queryTerms(spec.text);
      if (!terms.every((term) => haystack.includes(term))) return false;
    }
    if (spec.min_age_days !== null) {
      const age = matterAgeDays(matter, now);
      if (age === null || age < spec.min_age_days) return false;
    }
    return true;
  });

  if (spec.sort === "oldest" || spec.sort === "newest") {
    const direction = spec.sort === "oldest" ? 1 : -1;
    results = results
      .map((matter, index) => ({ matter, index }))
      .sort((a, b) => {
        const aKey = Date.parse(String(a.matter?.created_at || a.matter?.updated_at || "")) || 0;
        const bKey = Date.parse(String(b.matter?.created_at || b.matter?.updated_at || "")) || 0;
        if (aKey !== bKey) return (aKey - bKey) * direction;
        return a.index - b.index; // stable for equal timestamps
      })
      .map((entry) => entry.matter);
  }
  return results;
}

export {
  DASHBOARD_SEARCH_CHIPS,
  NULL_FILTER_SPEC,
  SEARCH_INTENT_ENDPOINT,
  SUMMARY_LABEL,
  SUMMARY_UNAVAILABLE_MESSAGE,
  applyFilterSpec,
  chipById,
  filterMattersByStatus,
  filterMattersByText,
  filterSpecIsEmpty,
  formatSummaryResult,
  matterHaystack,
  matterStatus,
  matterStatusLabel,
  matterTitle,
  queryTerms,
  runChip,
  summaryEndpoint,
  summaryErrorMessage,
  validateFilterSpec,
};
