const DEFAULT_DOCUMENT_TITLE = "Untitled NDA";
const SOURCE_PLACEHOLDER = "Paste NDA text here";

const VIEW_MODE_REDLINE = "redline";
const VIEW_MODE_CLEAN = "clean";
const VIEW_MODE_SIDE_BY_SIDE = "sidebyside";
const VIEW_MODE_ORIGINAL = "original";
const DOCUMENT_VIEW_MODES = [VIEW_MODE_REDLINE, VIEW_MODE_CLEAN, VIEW_MODE_SIDE_BY_SIDE, VIEW_MODE_ORIGINAL];

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
const PDF_EXPORT_FILE_PICKER_TYPES = [
  {
    description: "PDF document",
    accept: {
      "application/pdf": [".pdf"],
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
