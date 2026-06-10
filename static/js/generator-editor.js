// Generator document editor: the Generator tab's right pane is ALWAYS this editor
// (toolbar + editable document) -- never gated behind a button. Before a
// generation exists it shows the live draft (derived from the form preview),
// editable; "Generate" loads the authoritative generated NDA in its place. Edits
// use the same formatting toolbar as the Review workstation, plus Undo.
//
// It reuses the context-agnostic globals the Review editor is built from
// (renderFormattedRun, setRunFormatting, the font-size ladder, the editable-text
// helpers, ...) but keeps its own container/state/toolbar so the Review engine is
// never touched.
window.generatorEditor = (function () {
  const RENDER_ID = "generatorDocumentRender";
  const PREVIEW_ID = "draftIntakePreview";
  const HISTORY_LIMIT = 60;
  const TOOLBAR = {
    fontSelect: "genFontSelect",
    fontSize: "genFontSize",
    sizeUp: "genFontSizeUp",
    sizeDown: "genFontSizeDown",
    bold: "genFormatBold",
    italic: "genFormatItalic",
    undo: "genUndo",
  };
  const ALIGN_BUTTONS = [
    ["genAlignLeft", "left"],
    ["genAlignCenter", "center"],
    ["genAlignRight", "right"],
    ["genAlignJustify", "justify"],
  ];

  function renderEl() { return document.getElementById(RENDER_ID); }
  function paragraphs() { return Array.isArray(state.generatorParagraphs) ? state.generatorParagraphs : []; }
  function paragraphById(id) { return paragraphs().find((p) => String(p.id) === String(id)) || null; }
  function activeParagraph() { return paragraphById(state.generatorActiveParagraphId); }
  function history() {
    if (!Array.isArray(state.generatorHistory)) state.generatorHistory = [];
    return state.generatorHistory;
  }

  // ---- Draft (pre-generation) --------------------------------------------
  // Mirror the live form preview as editable paragraphs. Re-derived on every form
  // change UNTIL the user edits the draft (then we preserve their edits) or a real
  // NDA is generated (then the draft is replaced for good).
  function showDraft(sourceEl) {
    if (state.generatorMode === "generated") return;
    if (state.generatorDraftTouched && paragraphs().length) return;
    const source = sourceEl || document.getElementById(PREVIEW_ID);
    const derived = parseDraftParagraphs(source);
    state.generatorMode = "draft";
    state.generatorMatterId = null;
    state.generatorParagraphs = derived;
    state.generatorActiveParagraphId = null;
    state.generatorHistory = [];
    render();
  }

  function parseDraftParagraphs(sourceEl) {
    if (!sourceEl) return [];
    const blocks = sourceEl.querySelectorAll("h1,h2,h3,h4,h5,h6,p,li");
    const paras = [];
    let index = 0;
    blocks.forEach((block) => {
      if (block.closest(".nda-doc-kicker") || block.classList.contains("nda-doc-kicker")
        || block.classList.contains("nda-doc-foot")) return;
      const runs = runsFromElement(block);
      const text = runs.map((run) => run.text).join("");
      if (!text.trim()) return;
      index += 1;
      paras.push({ id: `draft-${index}`, index, text, runs: runs.length > 1 || runs[0].bold ? runs : undefined });
    });
    return paras;
  }

  // Flatten an element to {text, bold?} runs, treating <b>/<strong> as bold, then
  // trim the edges so the run list tiles a clean paragraph string.
  function runsFromElement(block) {
    const runs = [];
    (function walk(node, bold) {
      node.childNodes.forEach((child) => {
        if (child.nodeType === 3) {
          const text = child.nodeValue.replace(/\s+/g, " ");
          if (text) runs.push(bold ? { text, bold: true } : { text });
        } else if (child.nodeType === 1) {
          walk(child, bold || /^(B|STRONG)$/.test(child.tagName));
        }
      });
    })(block, false);
    if (runs.length) {
      runs[0].text = runs[0].text.replace(/^\s+/, "");
      runs[runs.length - 1].text = runs[runs.length - 1].text.replace(/\s+$/, "");
    }
    const tidy = runs.filter((run) => run.text.length);
    return tidy.length ? tidy : [{ text: "" }];
  }

  // ---- Load the generated NDA --------------------------------------------
  async function load(matterId) {
    if (!matterId) return false;
    let matter = null;
    try {
      const res = await fetch(`/api/matters/${encodeURIComponent(matterId)}/review`, {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) return false;
      matter = await res.json();
    } catch (error) {
      return false;
    }
    const result = matter.review_result || matter;
    const paras = Array.isArray(result.paragraphs) ? result.paragraphs : [];
    state.generatorMode = "generated";
    state.generatorMatterId = matterId;
    state.generatorParagraphs = paras.map((p) => ({
      ...p,
      runs: Array.isArray(p.runs) ? p.runs.map((r) => ({ ...r })) : p.runs,
    }));
    // Snapshot the as-generated paragraphs so the clean export can diff edits.
    state.generatorOriginalParagraphs = state.generatorParagraphs.map((p) => ({
      ...p,
      runs: Array.isArray(p.runs) ? p.runs.map((r) => ({ ...r })) : p.runs,
    }));
    state.generatorActiveParagraphId = null;
    state.generatorDraftTouched = false;
    state.generatorHistory = [];
    render();
    return true;
  }

  function clear() {
    state.generatorParagraphs = [];
    state.generatorMatterId = null;
    state.generatorActiveParagraphId = null;
    state.generatorDraftTouched = false;
    state.generatorMode = "draft";
    state.generatorHistory = [];
    render();
  }

  function isActive() { return paragraphs().length > 0; }

  // ---- Render -------------------------------------------------------------
  function render() {
    const el = renderEl();
    if (!el) return;
    el.innerHTML = paragraphs().map(renderParagraph).join("");
    bindEditing();
    bindToolbar();
    refreshToolbar();
  }

  function renderParagraph(paragraph) {
    const text = String(paragraph.text || "");
    const runs = Array.isArray(paragraph.runs) ? paragraph.runs : null;
    const tiles = runs && runs.map((r) => String(r && r.text || "")).join("") === text;
    const body = tiles ? runs.map((run) => renderFormattedRun(run, false)).join("") : escapeHtml(text);
    const id = escapeHtml(String(paragraph.id));
    const style = paragraphFormatStyleAttribute(paragraph);
    return `<div class="studio-doc-paragraph generator-doc-paragraph" data-paragraph-id="${id}"${style}>`
      + `<div class="paragraph-editable" contenteditable="plaintext-only" spellcheck="true" role="textbox"`
      + ` aria-multiline="true" data-editable-paragraph-id="${id}">${body}</div></div>`;
  }

  function rerenderParagraph(id) {
    const el = renderEl();
    const paragraph = paragraphById(id);
    if (!el || !paragraph) { render(); return; }
    const container = el.querySelector(`.studio-doc-paragraph[data-paragraph-id="${cssSafe(id)}"]`);
    if (!container) { render(); return; }
    const wrapper = document.createElement("div");
    wrapper.innerHTML = renderParagraph(paragraph);
    const fresh = wrapper.firstElementChild;
    container.replaceWith(fresh);
    bindParagraphEditing(fresh.querySelector("[data-editable-paragraph-id]"));
  }

  function cssSafe(value) {
    if (typeof window !== "undefined" && window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(String(value));
    }
    return String(value).replace(/["\\\]]/g, "\\$&");
  }

  // ---- Editing ------------------------------------------------------------
  function bindEditing() {
    const el = renderEl();
    if (!el) return;
    el.querySelectorAll("[data-editable-paragraph-id]").forEach(bindParagraphEditing);
  }

  function bindParagraphEditing(editable) {
    if (!editable) return;
    editable.addEventListener("focus", () => {
      state.generatorActiveParagraphId = editable.dataset.editableParagraphId || null;
      delete editable.dataset.histRecorded;
      refreshToolbar();
    });
    editable.addEventListener("input", () => {
      const para = paragraphById(editable.dataset.editableParagraphId);
      if (!para) return;
      const newText = editableParagraphText(editable);
      if (String(para.text || "") === newText) return;
      // Capture the pre-edit paragraph ONCE per focus so Undo reverts the whole edit.
      if (editable.dataset.histRecorded !== "true") {
        pushHistory(para);
        editable.dataset.histRecorded = "true";
      }
      para.text = newText;
      markTouched();
      refreshToolbar();
    });
    editable.addEventListener("keyup", refreshToolbar);
    editable.addEventListener("mouseup", refreshToolbar);
    if (typeof pastePlainText === "function") editable.addEventListener("paste", pastePlainText);
  }

  function markTouched() {
    if (state.generatorMode !== "generated") state.generatorDraftTouched = true;
  }

  // ---- History / Undo -----------------------------------------------------
  function snapshotParagraph(paragraph) {
    return {
      id: paragraph.id,
      text: String(paragraph.text || ""),
      runs: Array.isArray(paragraph.runs) ? paragraph.runs.map((r) => ({ ...r })) : undefined,
      alignment: paragraph.alignment,
      font: paragraph.font,
      fontSize: paragraph.fontSize,
    };
  }

  function pushHistory(paragraph) {
    if (!paragraph) return;
    const stack = history();
    stack.push(snapshotParagraph(paragraph));
    if (stack.length > HISTORY_LIMIT) stack.shift();
  }

  function undo() {
    const stack = history();
    const entry = stack.pop();
    if (!entry) { refreshToolbar(); return; }
    const para = paragraphById(entry.id);
    if (!para) { refreshToolbar(); return; }
    para.text = entry.text;
    if (entry.runs) para.runs = entry.runs.map((r) => ({ ...r })); else delete para.runs;
    if (entry.alignment !== undefined) para.alignment = entry.alignment; else delete para.alignment;
    if (entry.font !== undefined) para.font = entry.font; else delete para.font;
    if (entry.fontSize !== undefined) para.fontSize = entry.fontSize; else delete para.fontSize;
    state.generatorActiveParagraphId = para.id;
    render();
  }

  // ---- Selection ----------------------------------------------------------
  function activeSelection() {
    const id = state.generatorActiveParagraphId;
    const el = renderEl();
    if (!id || !el) return null;
    const editable = el.querySelector(`[data-editable-paragraph-id="${cssSafe(id)}"]`);
    const selection = window.getSelection();
    if (!editable || !selection || !selection.rangeCount || selection.isCollapsed) return null;
    const range = selection.getRangeAt(0);
    if (!editable.contains(range.startContainer) || !editable.contains(range.endContainer)) return null;
    const startOffset = editableSelectionTextOffset(editable, range.startContainer, range.startOffset);
    const endOffset = editableSelectionTextOffset(editable, range.endContainer, range.endOffset);
    if (!(endOffset > startOffset)) return null;
    return { startOffset, endOffset };
  }

  function captureSelection() {
    const sel = activeSelection();
    return sel ? { id: state.generatorActiveParagraphId, ...sel } : null;
  }

  function restoreSelection(snapshot) {
    if (!snapshot) return;
    const el = renderEl();
    const editable = el && el.querySelector(`[data-editable-paragraph-id="${cssSafe(snapshot.id)}"]`);
    if (!editable) return;
    try {
      const start = editableTextPositionForOffset(editable, snapshot.startOffset);
      const end = editableTextPositionForOffset(editable, snapshot.endOffset);
      if (!start || !end) return;
      const range = document.createRange();
      range.setStart(start.node, start.offset);
      range.setEnd(end.node, end.offset);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      editable.focus({ preventScroll: true });
    } catch (error) {
      /* best-effort caret restore */
    }
  }

  function commit(snapshot) {
    markTouched();
    rerenderParagraph(state.generatorActiveParagraphId);
    restoreSelection(snapshot);
    refreshToolbar();
  }

  // ---- Toolbar apply ------------------------------------------------------
  function applyAlignment(alignment) {
    const para = activeParagraph();
    if (!para) return;
    const next = String(alignment || "").trim().toLowerCase();
    if (String(para.alignment || "").trim().toLowerCase() === next) { refreshToolbar(); return; }
    pushHistory(para);
    para.alignment = next;
    markTouched();
    rerenderParagraph(para.id);
    refreshToolbar();
  }

  function applyFont(name) {
    const para = activeParagraph();
    if (!para) return;
    const snapshot = captureSelection();
    pushHistory(para);
    const value = String(name || "").trim();
    if (snapshot) setRunFormatting(para, snapshot.startOffset, snapshot.endOffset, "font", value || false);
    else if (value) para.font = value; else delete para.font;
    commit(snapshot);
  }

  function applyFontSize(size) {
    const para = activeParagraph();
    if (!para) return;
    const snapshot = captureSelection();
    pushHistory(para);
    const value = normalizeFontSize(size);
    if (snapshot) setRunFormatting(para, snapshot.startOffset, snapshot.endOffset, "size", value || false);
    else if (value) para.fontSize = value; else delete para.fontSize;
    commit(snapshot);
  }

  function stepFontSize(direction) {
    if (!activeParagraph()) return;
    const current = currentSize() || DEFAULT_FONT_SIZE;
    const next = nextLadderSize(current, direction);
    if (next && next !== current) applyFontSize(next);
  }

  function toggleRun(property) {
    const para = activeParagraph();
    const snapshot = captureSelection();
    if (!para || !snapshot) { setStatusHint("Select text to format"); return; }
    pushHistory(para);
    const allHave = runRangeHasFormatting(para, snapshot.startOffset, snapshot.endOffset, property, true);
    setRunFormatting(para, snapshot.startOffset, snapshot.endOffset, property, !allHave);
    commit(snapshot);
  }

  function currentSize() {
    const para = activeParagraph();
    if (!para) return null;
    const sel = activeSelection();
    if (sel) return uniformSelectionSize(para, sel);
    return Number(para.fontSize) > 0 ? Number(para.fontSize) : null;
  }

  function setStatusHint(message) {
    const node = document.getElementById("draftIntakeStatus");
    if (node) node.textContent = message;
  }

  // ---- Toolbar binding + state -------------------------------------------
  let toolbarBound = false;
  function bindToolbar() {
    if (toolbarBound) return;
    toolbarBound = true;
    const fontSelect = document.getElementById(TOOLBAR.fontSelect);
    if (fontSelect) fontSelect.onchange = () => applyFont(fontSelect.value);
    const fontSize = document.getElementById(TOOLBAR.fontSize);
    if (fontSize) fontSize.onchange = () => applyFontSize(fontSize.value);
    const up = document.getElementById(TOOLBAR.sizeUp);
    if (up) up.onclick = () => stepFontSize(1);
    const down = document.getElementById(TOOLBAR.sizeDown);
    if (down) down.onclick = () => stepFontSize(-1);
    const bold = document.getElementById(TOOLBAR.bold);
    if (bold) bold.onclick = () => toggleRun("bold");
    const italic = document.getElementById(TOOLBAR.italic);
    if (italic) italic.onclick = () => toggleRun("italic");
    const undoBtn = document.getElementById(TOOLBAR.undo);
    if (undoBtn) undoBtn.onclick = () => undo();
    ALIGN_BUTTONS.forEach(([id, alignment]) => {
      const button = document.getElementById(id);
      if (button) button.onclick = () => applyAlignment(alignment);
    });
  }

  function refreshToolbar() {
    const para = activeParagraph();
    const hasActive = Boolean(para);
    const sel = hasActive ? activeSelection() : null;

    const alignment = para ? String(para.alignment || "").trim().toLowerCase() : "";
    ALIGN_BUTTONS.forEach(([id, value]) => {
      const button = document.getElementById(id);
      if (!button) return;
      button.setAttribute("aria-pressed", hasActive && alignment === value ? "true" : "false");
      button.disabled = !hasActive;
    });

    [[TOOLBAR.bold, "bold"], [TOOLBAR.italic, "italic"]].forEach(([id, property]) => {
      const button = document.getElementById(id);
      if (!button) return;
      const pressed = Boolean(sel) && runRangeHasFormatting(para, sel.startOffset, sel.endOffset, property, true);
      button.setAttribute("aria-pressed", pressed ? "true" : "false");
      button.disabled = !hasActive;
    });

    const fontSelect = document.getElementById(TOOLBAR.fontSelect);
    if (fontSelect) {
      const selectionFont = sel ? uniformSelectionFont(para, sel) : null;
      fontSelect.value = para ? (sel ? (selectionFont || "") : String(para.font || "")) : "";
      if (fontSelect.selectedIndex < 0) fontSelect.value = "";
      fontSelect.disabled = !hasActive;
    }

    const fontSize = document.getElementById(TOOLBAR.fontSize);
    if (fontSize) {
      const size = hasActive ? currentSize() : null;
      fontSize.value = String(size || DEFAULT_FONT_SIZE);
      if (fontSize.selectedIndex < 0) fontSize.value = String(DEFAULT_FONT_SIZE);
      fontSize.disabled = !hasActive;
    }
    [TOOLBAR.sizeUp, TOOLBAR.sizeDown].forEach((id) => {
      const button = document.getElementById(id);
      if (button) button.disabled = !hasActive;
    });

    const undoBtn = document.getElementById(TOOLBAR.undo);
    if (undoBtn) undoBtn.disabled = history().length === 0;
  }

  // The edited document, for the clean export (Download / Send).
  function edits() {
    return {
      matterId: state.generatorMatterId || null,
      mode: state.generatorMode || "draft",
      paragraphs: paragraphs().map((p) => ({
        id: p.id,
        text: String(p.text || ""),
        runs: Array.isArray(p.runs) ? p.runs : undefined,
        alignment: p.alignment,
        font: p.font,
        fontSize: p.fontSize,
        source_index: p.source_index,
        source_part: p.source_part,
      })),
      dirty: Boolean(state.generatorDraftTouched),
    };
  }

  // ---- Clean export (Download / Send) ------------------------------------
  // Derive manual redlines by diffing the current paragraphs against the snapshot
  // taken at load -- mirrors the Review editor's manualExportRedlines, scoped to the
  // generator's own state. Text edits -> replace/delete; format-only edits ->
  // format_paragraph (which carries the run bold/italic/font/size ops).
  function exportRedlines() {
    const baseline = Array.isArray(state.generatorOriginalParagraphs) ? state.generatorOriginalParagraphs : [];
    const originalById = new Map(baseline.map((p) => [p.id, p]));
    return paragraphs().map((paragraph) => {
      const original = originalById.get(paragraph.id);
      if (!original) return null;
      const originalText = String(original.text || "").trim();
      const replacementText = String(paragraph.text || "").trim();
      if (originalText === replacementText) {
        const formatRedline = manualParagraphRedline(paragraph, baseline);
        return formatRedline && formatRedline.action === "format_paragraph" ? formatRedline : null;
      }
      const isDelete = !replacementText;
      const redline = {
        id: `manual-${paragraph.id}`,
        clause_id: "manual_viewer_edit",
        status: "proposed",
        action: isDelete ? REDLINE_DELETE_PARAGRAPH : REDLINE_REPLACE_PARAGRAPH,
        action_label: isDelete ? "Remove paragraph" : "Your edit",
        is_manual: true,
        whole_paragraph: false,
        paragraph_id: paragraph.id,
        paragraph_index: original.index || paragraph.index,
        original_text: originalText,
        replacement_text: replacementText,
      };
      if (original.source_index !== undefined || paragraph.source_index !== undefined) {
        redline.source_index = original.source_index !== undefined ? original.source_index : paragraph.source_index;
      }
      if (original.source_part) redline.source_part = original.source_part;
      return redline;
    }).filter(Boolean);
  }

  function hasEdits() {
    return state.generatorMode === "generated" && Boolean(state.generatorMatterId) && exportRedlines().length > 0;
  }

  // POST the edits to the export endpoint in CLEAN mode and return the .docx blob
  // (edits baked in, no redline marks). Null when there are no edits or on failure,
  // so the caller falls back to the original generated file.
  async function exportCleanDocx() {
    if (!hasEdits()) return null;
    let res;
    try {
      res = await fetch("/api/export-review-docx", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          matter_id: state.generatorMatterId,
          manual_redline_edits: exportRedlines(),
          clean: true,
        }),
      });
    } catch (error) {
      return null;
    }
    if (!res.ok) return null;
    return res.blob();
  }

  return { showDraft, load, clear, isActive, edits, undo, render, hasEdits, exportCleanDocx };
})();
