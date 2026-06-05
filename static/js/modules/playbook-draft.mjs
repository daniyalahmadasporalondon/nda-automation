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

// Normalize a validation response into a flat, render-ready error list.
// Accepts `{ valid, errors:[...] }` where each error may be a string or an object.
// The backend emits `{ location, clause, field, message, severity }`; we also
// accept `clause_id`/`code` and bare arrays for resilience.
function normalizeValidation(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const rawErrors = Array.isArray(source) ? source : (Array.isArray(source.errors) ? source.errors : []);
  const errors = rawErrors
    .map((entry) => {
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
      return normalized;
    })
    .filter(Boolean);

  // `valid` defaults to "no errors" when the backend omits the flag.
  const valid = typeof source.valid === "boolean" ? source.valid : errors.length === 0;
  return { valid: valid && errors.length === 0, errors };
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
  hashOf,
  isWorkingDirty,
  normalizePlaybookResponse,
  normalizeValidation,
  playbookOf,
  shortHash,
  validationSummary,
  versionLabel,
  versionOf,
};
