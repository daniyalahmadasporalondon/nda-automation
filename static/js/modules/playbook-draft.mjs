// Pure state helpers for the Playbook draft/publish editor.
//
// The editor distinguishes three things:
//   - active:  the published Playbook the review engine uses right now (version + hash)
//   - draft:   a server-persisted working copy that editing mutates (version + hash)
//   - working: the in-memory clauses the form binds to, started from the draft
//
// Save Draft persists `working` back to the draft. Publish promotes the draft to
// active. Nothing here renders or fetches; the controller wires those. Keeping the
// state machine pure makes the dirty/publishable/label logic unit-testable without a DOM.

const HASH_DISPLAY_LENGTH = 8;

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function stableJson(value) {
  return JSON.stringify(value === undefined ? null : value);
}

// Pull the clause-bearing playbook object out of a block that may be either
// `{ playbook: {...} }` or the playbook object itself. Backend may send either.
function playbookOf(block) {
  if (!block || typeof block !== "object") return null;
  if (block.playbook && typeof block.playbook === "object") return block.playbook;
  if (Array.isArray(block.clauses)) return block;
  return null;
}

function clausesOf(block) {
  const playbook = playbookOf(block);
  return Array.isArray(playbook?.clauses) ? playbook.clauses : [];
}

// Short, display-friendly version of a hash. Strips an algorithm prefix like
// "sha256:" so the meaningful hex shows, then truncates. Accepts already-short
// strings; never throws on missing/oddly-typed input.
function shortHash(hash) {
  let text = hash == null ? "" : String(hash);
  if (!text) return "";
  const colon = text.indexOf(":");
  if (colon !== -1 && /^[a-z0-9]+$/i.test(text.slice(0, colon))) {
    text = text.slice(colon + 1);
  }
  return text.length > HASH_DISPLAY_LENGTH ? text.slice(0, HASH_DISPLAY_LENGTH) : text;
}

// Read the version id from a block. The backend nests version/hash under a
// `metadata` object (active_version_id / draft_id), but we also accept a flat
// `version` for resilience.
function versionOf(block) {
  if (!block || typeof block !== "object") return null;
  const meta = block.metadata && typeof block.metadata === "object" ? block.metadata : {};
  return (
    meta.active_version_id ?? meta.draft_id ?? meta.playbook_version
    ?? block.version ?? null
  );
}

// Read the content hash from a block (metadata.active_hash / draft_hash, or flat).
function hashOf(block) {
  if (!block || typeof block !== "object") return null;
  const meta = block.metadata && typeof block.metadata === "object" ? block.metadata : {};
  return meta.active_hash ?? meta.draft_hash ?? block.hash ?? null;
}

// Human label like "v4 · a1b2c3d4". A numeric version id gets a "v" prefix; a
// string id (e.g. "pbv_8") is shown verbatim. Tolerant of either field absent.
function versionLabel(block) {
  if (!block || typeof block !== "object") return "";
  const parts = [];
  const version = versionOf(block);
  if (version !== undefined && version !== null && version !== "") {
    const numeric = typeof version === "number" || /^\d+$/.test(String(version));
    parts.push(numeric ? `v${version}` : String(version));
  }
  const hash = shortHash(hashOf(block));
  if (hash) parts.push(hash);
  return parts.join(" · ");
}

// The raw developer version id (e.g. "pbv_20260604T...Z_e2e5..."), for tooltips.
function rawVersionId(block) {
  const version = versionOf(block);
  return version == null ? "" : String(version);
}

// Best timestamp for a block: the backend's ISO published_at / draft_updated_at
// when present, otherwise the timestamp embedded in a pbv_/pbd_ id like
// "pbv_20260604T230958581923Z_<hash>". Returns a Date or null.
function versionTimestamp(block) {
  if (!block || typeof block !== "object") return null;
  const meta = block.metadata && typeof block.metadata === "object" ? block.metadata : {};
  const iso = meta.published_at ?? meta.draft_updated_at ?? block.published_at ?? block.updated_at;
  if (iso) {
    const date = new Date(iso);
    if (!Number.isNaN(date.getTime())) return date;
  }
  return timestampFromVersionId(versionOf(block));
}

// Parse the compact timestamp embedded in a version id: the segment between the
// first and last underscore, shaped "YYYYMMDDTHHMMSS<fraction>Z". Returns a Date
// or null when the id has no parseable timestamp.
function timestampFromVersionId(versionId) {
  const text = versionId == null ? "" : String(versionId);
  const match = text.match(/(\d{8}T\d{6})(\d*)(Z)?/);
  if (!match) return null;
  const [, ymdhms, fraction = ""] = match;
  const y = ymdhms.slice(0, 4);
  const mo = ymdhms.slice(4, 6);
  const d = ymdhms.slice(6, 8);
  const h = ymdhms.slice(9, 11);
  const mi = ymdhms.slice(11, 13);
  const s = ymdhms.slice(13, 15);
  // Use milliseconds (first 3 digits of the fractional part) for a valid ISO.
  const ms = fraction.slice(0, 3).padEnd(3, "0");
  const iso = `${y}-${mo}-${d}T${h}:${mi}:${s}.${ms}Z`;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

// Friendly absolute date/time, e.g. "Jun 4, 2026, 11:09 PM". Accepts a Date,
// ISO string, or epoch ms; returns "" for anything unparseable.
function formatVersionDateTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (!value || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

// Human-readable headline for a version card: "Published <date>" for the active
// version, "Draft saved <date>" for a draft. Falls back to a semver/label when
// no timestamp is available, and to "Not yet published" / "No saved draft" when
// the block is empty.
function friendlyVersionLabel(block, kind = "active") {
  const date = versionTimestamp(block);
  const friendlyDate = date ? formatVersionDateTime(date) : "";
  if (friendlyDate) {
    return kind === "draft" ? `Draft saved ${friendlyDate}` : `Published ${friendlyDate}`;
  }
  // No timestamp: fall back to a human version number if the backend exposes one.
  const meta = block && typeof block.metadata === "object" ? block.metadata : {};
  const semver = meta.playbook_version;
  if (semver) return kind === "draft" ? `Draft (v${semver})` : `Version ${semver}`;
  return kind === "draft" ? "No saved draft yet" : "Not yet published";
}

// Normalize the GET response into a stable internal shape. Accepts the
// `{ active, draft, history }` contract where active/draft are
// `{ playbook, metadata }` blocks (draft may be null when none exists). Falls
// back to a legacy single-playbook `{ playbook, history }` payload by treating
// that playbook as both the active version and the draft baseline.
function normalizePlaybookResponse(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const history = Array.isArray(source.history) ? source.history : [];

  let active = source.active && typeof source.active === "object" ? source.active : null;
  let draft = source.draft && typeof source.draft === "object" ? source.draft : null;

  if (!active && !draft && playbookOf(source)) {
    // Legacy single-playbook payload.
    const legacy = { playbook: clone(playbookOf(source)), version: source.version ?? null, hash: source.hash ?? null };
    active = clone(legacy);
    draft = clone(legacy);
  }

  // No draft on the server yet → the active playbook is the draft baseline. The
  // editor still loads the active clauses for editing, but the draft is in sync.
  if (active && !draft) draft = clone(active);
  if (draft && !active) active = clone(draft);

  return {
    active: active || { playbook: { clauses: [] }, metadata: {} },
    draft: draft || { playbook: { clauses: [] }, metadata: {} },
    history,
  };
}

// True when the in-memory working clauses differ from the saved draft clauses,
// i.e. there are local edits that Save Draft would persist.
function isWorkingDirty(workingClauses, draftBlock) {
  return stableJson(workingClauses || []) !== stableJson(clausesOf(draftBlock));
}

// True when the saved draft differs from the active published version, i.e.
// Publish would actually change what the engine uses. Prefer an explicit backend
// flag when present; otherwise compare the draft's base-active hash and content
// hash against the active hash, falling back to a clause-level comparison.
function draftDiffersFromActive(draftBlock, activeBlock) {
  if (draftBlock && typeof draftBlock.has_unpublished_changes === "boolean") {
    return draftBlock.has_unpublished_changes;
  }
  const draftHash = hashOf(draftBlock);
  const activeHash = hashOf(activeBlock);
  if (draftHash && activeHash) {
    return String(draftHash) !== String(activeHash);
  }
  return stableJson(clausesOf(draftBlock)) !== stableJson(clausesOf(activeBlock));
}

// Normalize a single error/warning entry into a flat, render-ready record.
// Accepts a bare string or the backend object shape
// `{ location, clause, field, message, severity, check_id, confidence }`; also
// accepts `clause_id`/`code` aliases. Returns null for empty entries.
function normalizeValidationEntry(entry) {
  if (entry == null) return null;
  if (typeof entry === "string") return { message: entry };
  if (typeof entry !== "object") return { message: String(entry) };
  const message = entry.message || entry.error || entry.detail || "";
  const normalized = { message: String(message || "Invalid value") };
  const clauseId = entry.clause_id ?? entry.clause;
  if (clauseId != null && clauseId !== "") normalized.clause_id = String(clauseId);
  if (entry.field != null && entry.field !== "") normalized.field = String(entry.field);
  const code = entry.code ?? entry.severity;
  if (code != null && code !== "") normalized.code = String(code);
  // Layer-2 semantic-lint warnings carry a check_id and the model's self-reported
  // confidence; preserve both so the UI can label the advisory and show its strength.
  if (entry.check_id != null && entry.check_id !== "") normalized.check_id = String(entry.check_id);
  if (typeof entry.confidence === "number" && Number.isFinite(entry.confidence)) {
    normalized.confidence = entry.confidence;
  }
  return normalized;
}

// Normalize a validation response into flat, render-ready error and warning lists.
// Accepts `{ valid, errors:[...], warnings:[...] }`. Errors block publish; warnings
// are the ADVISORY Layer-2 semantic-lint findings and never affect `valid`.
function normalizeValidation(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const rawErrors = Array.isArray(source) ? source : (Array.isArray(source.errors) ? source.errors : []);
  const errors = rawErrors.map(normalizeValidationEntry).filter(Boolean);
  const rawWarnings = Array.isArray(source.warnings) ? source.warnings : [];
  const warnings = rawWarnings.map(normalizeValidationEntry).filter(Boolean);

  // `valid` defaults to "no errors" when the backend omits the flag. Warnings are
  // advisory and deliberately excluded from the publish gate.
  const valid = typeof source.valid === "boolean" ? source.valid : errors.length === 0;
  return { valid: valid && errors.length === 0, errors, warnings };
}

// One-line summary suitable for an aria-live status region.
function validationSummary(result) {
  const normalized = result && Array.isArray(result.errors) ? result : { valid: true, errors: [] };
  if (normalized.valid) return "Draft is valid.";
  const count = normalized.errors.length;
  return count === 1 ? "1 validation issue found." : `${count} validation issues found.`;
}

export {
  HASH_DISPLAY_LENGTH,
  clausesOf,
  draftDiffersFromActive,
  formatVersionDateTime,
  friendlyVersionLabel,
  hashOf,
  isWorkingDirty,
  normalizePlaybookResponse,
  normalizeValidation,
  playbookOf,
  rawVersionId,
  shortHash,
  validationSummary,
  versionLabel,
  versionOf,
  versionTimestamp,
};
