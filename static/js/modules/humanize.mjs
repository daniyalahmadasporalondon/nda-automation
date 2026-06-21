// Shared humanizers that keep machine strings off the screens legal users read.
//
// Two classes of machine string have leaked to non-technical reviewers:
//   1. raw snake_case / kebab-case identifiers (a clause id like `ip_assignment`,
//      a structure-tab `kind`, a section id) rendered as a label, and
//   2. raw AI model ids (`anthropic/claude-opus-4.8-fast`, `deepseek/deepseek-v4-pro`)
//      surfaced outside the admin AI panel.
//
// These helpers are the single source for turning each into something a human
// reads. Apply them ONLY at user-facing DISPLAY strings — never to data keys,
// identifiers, routes, or anything the code compares/stores.

// Tokens that must stay upper-cased after title-casing so a humanized id reads
// correctly (`ip_assignment` -> "IP Assignment", not "Ip Assignment";
// `difc_governing_law` -> "DIFC Governing Law"). The map is keyed by the
// lower-cased token so lookup is case-insensitive. Extend sensibly — every entry
// here is an acronym/abbreviation a legal reviewer expects to see in caps.
const ACRONYMS = new Map(
  [
    "difc",
    "ip",
    "nda",
    "ai",
    "us",
    "uk",
    "eu",
    "llc",
    "id",
    "url",
    "uae",
    "msa",
    "dpa",
    "sow",
    "api",
    "pdf",
    "ndas",
    "ie", // "i.e." style joiners survive title-casing oddly; keep explicit
  ].map((token) => [token, token.toUpperCase()]),
);

// snake_case / kebab-case id -> human Title Case, preserving known acronyms.
// Graceful on empty / non-string input -> "". Never throws.
export function humanizeId(id) {
  if (id == null) return "";
  const raw = String(id).trim();
  if (!raw) return "";
  return raw
    .split(/[\s_-]+/)
    .filter((word) => word.length > 0)
    .map((word) => {
      const acronym = ACRONYMS.get(word.toLowerCase());
      if (acronym) return acronym;
      // Title-case the first letter; leave the rest of the token as-is so a token
      // that already carries meaningful casing/digits (e.g. "v2", "4.8") is not
      // mangled. A purely lower-case word like "assignment" -> "Assignment".
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(" ");
}

// Raw AI model id -> accurate, friendly name. CRITICAL: this preserves the REAL
// version — it never downgrades (an `opus-4.8-fast` is still "Claude Opus 4.8").
// The keys are the exact ids this system uses (see nda_automation/ai_review.py
// DEFAULT_OPENROUTER_MODEL and ai_verifier.py DEFAULT_VERIFIER_MODEL). An
// unmapped id resolves to a safe generic — it NEVER leaks the raw id to a
// non-admin reviewer. The admin AI panel is the one place that keeps raw ids;
// it does NOT call this.
const MODEL_LABELS = new Map([
  ["anthropic/claude-opus-4.8-fast", "Claude Opus 4.8"],
  ["anthropic/claude-opus-4.8", "Claude Opus 4.8"],
  ["deepseek/deepseek-v4-pro", "DeepSeek V4 Pro"],
  ["deepseek/deepseek-v4-flash", "DeepSeek V4 Flash"],
]);

const GENERIC_MODEL_LABEL = "AI model";

export function friendlyModelName(modelId) {
  if (modelId == null) return GENERIC_MODEL_LABEL;
  const raw = String(modelId).trim();
  if (!raw) return GENERIC_MODEL_LABEL;
  const mapped = MODEL_LABELS.get(raw) || MODEL_LABELS.get(raw.toLowerCase());
  return mapped || GENERIC_MODEL_LABEL;
}
