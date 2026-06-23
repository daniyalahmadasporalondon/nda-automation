const DEFAULT_DOCUMENT_TITLE = "Untitled NDA";
const SOURCE_PLACEHOLDER = "Paste NDA text here";

const VIEW_MODE_REDLINE = "redline";
const VIEW_MODE_CLEAN = "clean";
const VIEW_MODE_SIDE_BY_SIDE = "sidebyside";
const VIEW_MODE_ORIGINAL = "original";
const DOCUMENT_VIEW_MODES = [VIEW_MODE_REDLINE, VIEW_MODE_CLEAN, VIEW_MODE_SIDE_BY_SIDE, VIEW_MODE_ORIGINAL];

// Faithful-DOCX render feature flag (Review workstation "Original"/redline/clean/
// sidebyside surfaces). SINGLE control path: localStorage. Default ON.
//
//   DISABLE (ops/user kill-switch; persists across reloads):
//     localStorage.setItem("nda.faithfulDocxRender", "false")  // then re-render the matter
//   RE-ENABLE / clear the override (back to the ON default):
//     localStorage.removeItem("nda.faithfulDocxRender")
//
// FAITHFUL_DOCX_RENDER_FLAG_KEY is the one localStorage key; FAITHFUL_DOCX_RENDER_DEFAULT
// is the default used when the key is absent (true = ON). The flag defaults ON now:
// enabled = (value !== "false"), so only an explicit "false" disables it (the kill-switch).
// There is no window flag.
const FAITHFUL_DOCX_RENDER_FLAG_KEY = "nda.faithfulDocxRender";
const FAITHFUL_DOCX_RENDER_DEFAULT = true;

const REDLINE_DELETE_PARAGRAPH = "delete_paragraph";
const REDLINE_INSERT_AFTER_PARAGRAPH = "insert_after_paragraph";
const REDLINE_REPLACE_PARAGRAPH = "replace_paragraph";

const FILE_BASE64_CHUNK_SIZE = 0x8000;
const DOWNLOAD_URL_REVOKE_DELAY_MS = 30000;
const EXPORT_FILE_PICKER_TYPES = [
  {
    description: "Word document",
    accept: {
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
    },
  },
];
const REVIEWABLE_DOCUMENT_ACCEPT = ".docx,.pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/pdf";
const DEFAULT_FONT_SIZE_PX = 16;
const LINE_HEIGHT_FALLBACK_MULTIPLIER = 1.7;
const SOURCE_SCROLL_MIN_WIDTH_PX = 80;
const SOURCE_SCROLL_MIN_CHARS_PER_LINE = 24;
const SOURCE_SCROLL_AVG_CHAR_WIDTH_EM = 0.55;
const SOURCE_SCROLL_CONTEXT_RATIO = 0.32;
const RENDERED_SCROLL_CONTEXT_RATIO = 0.24;

// Shared, generic snake_case / kebab-case -> Title Case humanizer for opaque enum
// tokens that have no curated map (e.g. a brand-new source_kind the backend adds
// before the UI catches up). Callers prefer their own curated maps and fall back to
// this only for unknown tokens, so a reviewer reads "Some New Kind" rather than the
// raw "some_new_kind". Exposed on window so every view can reuse one implementation.
// Defined idempotently so a re-load never clobbers a live reference.
if (typeof window !== "undefined" && typeof window.humanizeId !== "function") {
  window.humanizeId = function humanizeId(token) {
    return String(token == null ? "" : token)
      .replace(/[_-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/\b\w/g, (character) => character.toUpperCase());
  };
}
