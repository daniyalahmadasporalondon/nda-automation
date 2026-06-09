// Paragraph-level formatting toolbar (#studioFormatToolbar): alignment + font +
// inline Bold / Italic / per-selection font.
//
// When a paragraph's text is byte-identical to its baseline but its alignment
// and/or font differ, that difference is carried as a `format_paragraph` manual
// redline (built in redline-rendering.js: manualParagraphRedline). This file is
// the toolbar wiring: it records the active paragraph, writes the chosen
// alignment/font onto the active `state.reviewParagraphs` entry, then marks the
// draft dirty and re-renders so the redline + on-screen formatting update.
//
// Inline (per-selection) Bold / Italic / font edit `paragraph.runs`, the
// `[{text, bold?, italic?, font?}, ...]` display model. The diff between current
// and baseline runs is emitted as run-scope ops on the SAME format_paragraph
// redline (see paragraphFormatOps in redline-rendering.js). The invariant
// `runs.map(r=>r.text).join("") === paragraph.text` is maintained at all times.

const FORMAT_ALIGN_BUTTONS = [
  ["studioAlignLeft", "left"],
  ["studioAlignCenter", "center"],
  ["studioAlignRight", "right"],
  ["studioAlignJustify", "justify"],
];

function formatToolbarElement() {
  return document.getElementById("studioFormatToolbar");
}

function formatFontSelectElement() {
  return document.getElementById("studioFontSelect");
}

function formatBoldButtonElement() {
  return document.getElementById("studioFormatBold");
}

function formatItalicButtonElement() {
  return document.getElementById("studioFormatItalic");
}

// ---- Run model (paragraph.runs) ---------------------------------------------
// `paragraph.runs` is the source of truth for inline formatting:
// `[{ text, bold?, italic?, font? }, ...]`. The invariant
// `runs.map(r=>r.text).join("") === paragraph.text` is maintained everywhere.

// Ensures `paragraph.runs` exists and is non-empty. If absent/empty, seeds a
// single run carrying the whole paragraph text (unformatted). Returns the runs.
function ensureParagraphRuns(paragraph) {
  if (!paragraph) return [];
  const text = String(paragraph.text || "");
  const runs = Array.isArray(paragraph.runs) ? paragraph.runs : null;
  const joined = runs ? runs.map((run) => String(run?.text || "")).join("") : "";
  if (!runs || !runs.length || joined !== text) {
    paragraph.runs = [{ text }];
  }
  return paragraph.runs;
}

// Returns a copy of `runs` split so that run boundaries fall exactly at `start`
// and `end` (offsets into the joined text). Formatting on each fragment is
// preserved; the joined text is unchanged.
function splitRunsAtOffsets(runs, start, end) {
  const boundaries = new Set([start, end]);
  const result = [];
  let cursor = 0;
  (Array.isArray(runs) ? runs : []).forEach((run) => {
    const text = String(run?.text || "");
    const runStart = cursor;
    const runEnd = cursor + text.length;
    cursor = runEnd;
    if (!text.length) {
      // Preserve a zero-length run rather than dropping it.
      result.push({ ...run, text: "" });
      return;
    }
    // Collect cut points strictly inside this run, in order.
    const cuts = [...boundaries]
      .filter((offset) => offset > runStart && offset < runEnd)
      .sort((a, b) => a - b);
    let localStart = 0;
    cuts.forEach((offset) => {
      const localEnd = offset - runStart;
      result.push({ ...run, text: text.slice(localStart, localEnd) });
      localStart = localEnd;
    });
    result.push({ ...run, text: text.slice(localStart) });
  });
  return result;
}

// Splits at [start, end), then sets `property` to `value` on every run wholly
// inside that range. `value` of `false`/empty deletes the property (so a tidy
// run with no formatting carries no bold/italic/font keys).
function setRunFormatting(paragraph, start, end, property, value) {
  const runs = splitRunsAtOffsets(ensureParagraphRuns(paragraph), start, end);
  let cursor = 0;
  runs.forEach((run) => {
    const text = String(run?.text || "");
    const runStart = cursor;
    const runEnd = cursor + text.length;
    cursor = runEnd;
    // A run is covered when it lies within [start, end). Zero-length runs at the
    // boundary are left untouched.
    if (text.length && runStart >= start && runEnd <= end) {
      if (value === false || value === "" || value === undefined || value === null) {
        delete run[property];
      } else {
        run[property] = value;
      }
    }
  });
  paragraph.runs = mergeAdjacentRuns(runs);
  return paragraph.runs;
}

// Coalesces neighbouring runs that carry identical bold/italic/font, keeping the
// run list tidy. Drops zero-length runs (unless the whole paragraph is empty).
function mergeAdjacentRuns(runs) {
  const merged = [];
  (Array.isArray(runs) ? runs : []).forEach((run) => {
    const text = String(run?.text || "");
    if (!text.length) return;
    const last = merged[merged.length - 1];
    if (last && runFormattingMatches(last, run)) {
      last.text += text;
      return;
    }
    merged.push(normalizeRun(run));
  });
  if (!merged.length) merged.push({ text: "" });
  return merged;
}

// Strips a run down to text + only the formatting keys that are actually set, so
// equality checks and the emitted ops never see `bold:false`/`font:""` noise.
function normalizeRun(run) {
  const out = { text: String(run?.text || "") };
  if (run?.bold) out.bold = true;
  if (run?.italic) out.italic = true;
  const font = String(run?.font || "").trim();
  if (font) out.font = font;
  return out;
}

function runFormattingMatches(a, b) {
  return Boolean(a?.bold) === Boolean(b?.bold)
    && Boolean(a?.italic) === Boolean(b?.italic)
    && String(a?.font || "").trim() === String(b?.font || "").trim();
}

// True when EVERY character in [start, end) already carries `property` (=value
// for font; truthy for bold/italic). Drives the toggle's pressed/inverse logic.
function runRangeHasFormatting(paragraph, start, end, property, value) {
  if (start >= end) return false;
  const runs = ensureParagraphRuns(paragraph);
  let cursor = 0;
  let covered = false;
  for (const run of runs) {
    const text = String(run?.text || "");
    const runStart = cursor;
    const runEnd = cursor + text.length;
    cursor = runEnd;
    if (!text.length || runEnd <= start || runStart >= end) continue;
    covered = true;
    if (property === "font") {
      if (String(run?.font || "").trim() !== String(value || "").trim()) return false;
    } else if (!run?.[property]) {
      return false;
    }
  }
  return covered;
}

// The active paragraph is whichever editable paragraph last took focus. It is
// stored on state so it survives re-renders; the element itself is re-created on
// every render, so we key off the stable paragraph id.
function activeFormatParagraph() {
  const id = state.activeFormatParagraphId;
  if (!id) return null;
  return (Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [])
    .find((paragraph) => String(paragraph.id) === String(id)) || null;
}

// Re-binds on every document render (the editable nodes are replaced each time).
// Wires the focus tracker, the four alignment buttons, and the font select, then
// refreshes the toolbar's pressed/selected state for the active paragraph.
function bindFormatToolbar() {
  const render = typeof studioDocumentRender !== "undefined" ? studioDocumentRender : null;
  if (render) {
    render.querySelectorAll("[data-editable-paragraph-id]").forEach((editable) => {
      editable.addEventListener("focus", () => {
        state.activeFormatParagraphId = editable.dataset.editableParagraphId || null;
        refreshFormatToolbarState();
      });
    });
  }

  FORMAT_ALIGN_BUTTONS.forEach(([buttonId, alignment]) => {
    const button = document.getElementById(buttonId);
    if (!button) return;
    button.onclick = () => applyParagraphAlignment(alignment);
  });

  const boldButton = formatBoldButtonElement();
  if (boldButton) boldButton.onclick = () => toggleRunFormatting("bold");
  const italicButton = formatItalicButtonElement();
  if (italicButton) italicButton.onclick = () => toggleRunFormatting("italic");

  const fontSelect = formatFontSelectElement();
  if (fontSelect) {
    fontSelect.onchange = () => applyFontChange(fontSelect.value);
  }

  ensureFormatSelectionListener();
  refreshFormatToolbarState();
}

// Keep the Bold/Italic pressed-state in sync with the live text selection. A
// single document-level listener (guarded so render churn never stacks copies)
// refreshes the toolbar whenever the selection moves.
let formatSelectionListenerBound = false;
function ensureFormatSelectionListener() {
  if (formatSelectionListenerBound) return;
  formatSelectionListenerBound = true;
  document.addEventListener("selectionchange", refreshFormatToolbarState);
}

// Snapshot the active paragraph's formatting onto the shared viewer undo stack
// BEFORE a change, so Undo reverts an alignment/font change (the derived
// format_paragraph redline recomputes from the restored values). Records whether
// each property existed so undo can delete vs. restore. Dispatched as type
// "paragraph_format" in review-workstation-viewer.js (restoreParagraphFormat).
function pushParagraphFormatHistory(paragraph) {
  if (typeof pushReviewEditHistoryEntry !== "function" || !paragraph) return;
  const hadAlignment = Object.prototype.hasOwnProperty.call(paragraph, "alignment");
  const hadFont = Object.prototype.hasOwnProperty.call(paragraph, "font");
  const hadRuns = Object.prototype.hasOwnProperty.call(paragraph, "runs") && Array.isArray(paragraph.runs);
  pushReviewEditHistoryEntry({
    type: "paragraph_format",
    paragraphId: String(paragraph.id),
    hadAlignment,
    hadFont,
    hadRuns,
    previousAlignment: hadAlignment ? paragraph.alignment : undefined,
    previousFont: hadFont ? paragraph.font : undefined,
    // Deep-ish copy each run ({text, bold?, italic?, font?} are all primitives)
    // so a later mutation can never corrupt the captured undo state.
    previousRuns: hadRuns ? paragraph.runs.map((run) => ({ ...run })) : undefined,
  });
}

// Sets the active paragraph's alignment ("left"/"center"/"right"/"justify").
function applyParagraphAlignment(alignment) {
  const paragraph = activeFormatParagraph();
  if (!paragraph) return;
  const next = String(alignment || "").trim().toLowerCase();
  if (String(paragraph.alignment || "").trim().toLowerCase() === next) {
    // No change — still refresh pressed-state so the click reads as a no-op.
    refreshFormatToolbarState();
    return;
  }
  pushParagraphFormatHistory(paragraph);
  paragraph.alignment = next;
  commitParagraphFormatChange();
}

// Sets the active paragraph's font to the chosen Word font NAME (e.g. "Arial").
// An empty selection clears the paragraph's font override.
function applyParagraphFont(fontName) {
  const paragraph = activeFormatParagraph();
  if (!paragraph) {
    refreshFormatToolbarState();
    return;
  }
  const next = String(fontName || "").trim();
  if (String(paragraph.font || "").trim() === next) {
    refreshFormatToolbarState();
    return;
  }
  pushParagraphFormatHistory(paragraph);
  if (next) {
    paragraph.font = next;
  } else {
    delete paragraph.font;
  }
  commitParagraphFormatChange();
}

// Toggles a run-scope boolean property ("bold"|"italic") across the active
// paragraph's current text selection. No selection -> no-op + a hint. The toggle
// is "all-on -> off, otherwise on": if every covered char already has the
// property, it is cleared across the selection, else it is set.
function toggleRunFormatting(property) {
  const paragraph = activeFormatParagraph();
  if (!paragraph) return;
  const selection = selectionForActiveParagraph();
  if (!selection) {
    setFileMeta("Select text to format");
    return;
  }
  const { startOffset, endOffset } = selection;
  pushParagraphFormatHistory(paragraph);
  const allHave = runRangeHasFormatting(paragraph, startOffset, endOffset, property, true);
  setRunFormatting(paragraph, startOffset, endOffset, property, !allHave);
  commitParagraphFormatChange();
}

// Font dropdown: with a non-empty selection, apply the font as a run op over the
// selection only; with no selection, fall back to the whole-paragraph font
// override (the pre-existing behaviour).
function applyFontChange(fontName) {
  const paragraph = activeFormatParagraph();
  if (!paragraph) {
    refreshFormatToolbarState();
    return;
  }
  const selection = selectionForActiveParagraph();
  if (!selection) {
    applyParagraphFont(fontName);
    return;
  }
  const next = String(fontName || "").trim();
  pushParagraphFormatHistory(paragraph);
  // An empty choice clears the run-level font override over the selection.
  setRunFormatting(paragraph, selection.startOffset, selection.endOffset, "font", next || false);
  commitParagraphFormatChange();
}

// Resolves the current text selection within the ACTIVE paragraph, or null when
// there is no non-empty selection there. Reuses selectedTextInParagraph.
function selectionForActiveParagraph() {
  const id = state.activeFormatParagraphId;
  if (!id || typeof selectedTextInParagraph !== "function") return null;
  const selection = selectedTextInParagraph(id);
  if (!selection || !String(selection.selectedText || "").trim()) return null;
  if (!(selection.endOffset > selection.startOffset)) return null;
  return selection;
}

// Shared post-change path: mark the redline draft dirty and re-render the
// document so the format_paragraph redline + on-screen alignment/font update.
function commitParagraphFormatChange() {
  if (typeof markRedlineDraftDirty === "function") markRedlineDraftDirty();
  if (typeof renderStudioDocumentHighlights === "function") renderStudioDocumentHighlights();
  // renderStudioDocumentHighlights re-runs bindFormatToolbar, which refreshes the
  // pressed-state; refresh again here for the no-render guard paths above.
  refreshFormatToolbarState();
}

// Reflects the active paragraph's formatting on the toolbar: alignment buttons'
// aria-pressed, the font select's value, and disables every control when no
// paragraph is active (so the toolbar never acts on nothing).
function refreshFormatToolbarState() {
  const toolbar = formatToolbarElement();
  if (!toolbar) return;
  const paragraph = activeFormatParagraph();
  const hasActive = Boolean(paragraph);

  const alignment = paragraph ? String(paragraph.alignment || "").trim().toLowerCase() : "";
  FORMAT_ALIGN_BUTTONS.forEach(([buttonId, value]) => {
    const button = document.getElementById(buttonId);
    if (!button) return;
    const pressed = hasActive && alignment === value;
    button.setAttribute("aria-pressed", pressed ? "true" : "false");
    button.disabled = !hasActive;
  });

  // Bold/Italic press-state reflects the live selection: pressed only when the
  // entire selection already carries the property. With no selection they fall
  // back to unpressed (the toggle would hint rather than act).
  const selection = hasActive ? selectionForActiveParagraph() : null;
  [["studioFormatBold", "bold"], ["studioFormatItalic", "italic"]].forEach(([buttonId, property]) => {
    const button = document.getElementById(buttonId);
    if (!button) return;
    const pressed = Boolean(selection)
      && runRangeHasFormatting(paragraph, selection.startOffset, selection.endOffset, property, true);
    button.setAttribute("aria-pressed", pressed ? "true" : "false");
    button.disabled = !hasActive;
  });

  const fontSelect = formatFontSelectElement();
  if (fontSelect) {
    // With a selection, reflect that selection's font (uniform -> show it, mixed
    // -> Default). With no selection, reflect the paragraph-level font override.
    const selectionFont = selection ? uniformSelectionFont(paragraph, selection) : null;
    const shownFont = selection ? (selectionFont || "") : String(paragraph?.font || "");
    fontSelect.value = paragraph ? shownFont : "";
    // A font not present in the option list leaves the select with no match;
    // fall back to the empty "Default font" option so it never shows stale text.
    if (fontSelect.selectedIndex < 0) fontSelect.value = "";
    fontSelect.disabled = !hasActive;
  }
}

// Returns the font name shared by every run touching the selection, or null when
// the selection spans more than one font (so the select shows "Default font").
function uniformSelectionFont(paragraph, selection) {
  const runs = ensureParagraphRuns(paragraph);
  const { startOffset, endOffset } = selection;
  let cursor = 0;
  let seen = null;
  for (const run of runs) {
    const text = String(run?.text || "");
    const runStart = cursor;
    const runEnd = cursor + text.length;
    cursor = runEnd;
    if (!text.length || runEnd <= startOffset || runStart >= endOffset) continue;
    const font = String(run?.font || "").trim();
    if (seen === null) seen = font;
    else if (seen !== font) return null;
  }
  return seen || null;
}
