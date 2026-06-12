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

  function model() { return window.GeneratorWorkstationModel || null; }
  function renderEl() { return document.getElementById(RENDER_ID); }
  function paragraphs() {
    return model()?.generatorParagraphs(state) || (Array.isArray(state.generatorParagraphs) ? state.generatorParagraphs : []);
  }
  function paragraphById(id) {
    return model()?.generatorParagraphById(state, id)
      || paragraphs().find((p) => String(p.id) === String(id)) || null;
  }
  function activeParagraph() { return model()?.activeGeneratorParagraph(state) || paragraphById(state.generatorActiveParagraphId); }
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
    Object.assign(state, model()?.draftGeneratorState(derived) || {
      generatorMode: "draft",
      generatorMatterId: null,
      generatorParagraphs: derived,
      generatorActiveParagraphId: null,
      generatorHistory: [],
    });
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
      const para = { id: `draft-${index}`, index, text, runs: runs.length > 1 || runs[0].bold || runs.some((r) => r.fill || r.blank) ? runs : undefined };
      // For list items, derive the ordinal marker + nesting level from the source
      // <ol>/<ul> so the draft shows clause numbers / sub-clause letters (1., (a))
      // just like the generated document does (the shared structure renderer reads
      // paragraph.numbering for the marker + indentation).
      if (block.tagName === "LI") {
        const numbering = draftListNumbering(block);
        if (numbering) para.numbering = numbering;
      }
      paras.push(para);
    });
    return paras;
  }

  // Marker + nesting level for a draft <li>, from its position + depth in the source
  // <ol>/<ul>. Level 0 -> "1.", level 1 -> "(a)", deeper -> "(i)". Shaped to match the
  // generated document's numbering metadata so paragraphStructureClasses /
  // paragraphStructureAttributes draw the same markers + indentation.
  function draftListNumbering(li) {
    const list = li.parentElement;
    if (!list || (list.tagName !== "OL" && list.tagName !== "UL")) return null;
    const ordinal = Array.from(list.children).filter((el) => el.tagName === "LI").indexOf(li) + 1;
    if (ordinal < 1) return null;
    let level = 0;
    let ancestor = list.parentElement;
    while (ancestor) {
      if (ancestor.tagName === "OL" || ancestor.tagName === "UL") level += 1;
      ancestor = ancestor.parentElement;
    }
    const label = level === 0 ? `${ordinal}.` : level === 1 ? `(${draftAlpha(ordinal)})` : `(${draftRoman(ordinal)})`;
    return { level, value: ordinal, label };
  }

  function draftAlpha(n) {
    let out = "";
    let value = n;
    while (value > 0) {
      value -= 1;
      out = String.fromCharCode(97 + (value % 26)) + out;
      value = Math.floor(value / 26);
    }
    return out || "a";
  }

  function draftRoman(n) {
    const numerals = [[10, "x"], [9, "ix"], [5, "v"], [4, "iv"], [1, "i"]];
    let value = n;
    let out = "";
    for (const [size, sym] of numerals) {
      while (value >= size) { out += sym; value -= size; }
    }
    return out || "i";
  }

  // Flatten an element to {text, bold?, fill?, blank?} runs, treating <b>/<strong>
  // as bold, `.nda-fill` / `.nda-fill-entity` spans as a viewer-only violet
  // "filled-in value" highlight, and `.nda-blank` spans as the amber "unfilled
  // placeholder" highlight — so the always-visible editor mirrors the same
  // playbook colours as the live preview instead of flattening them to plain text.
  // Both `fill` and `blank` are render-only markers: they survive in the editor
  // model but are NOT export formats (the export normalizers only copy
  // bold/italic/underline/etc.), so they never reach the generated/sent document.
  function runsFromElement(block) {
    const runs = [];
    (function walk(node, bold, fill, blank) {
      node.childNodes.forEach((child) => {
        if (child.nodeType === 3) {
          const text = child.nodeValue.replace(/\s+/g, " ");
          if (text) {
            const run = { text };
            if (bold) run.bold = true;
            if (fill) run.fill = true;
            if (blank) run.blank = true;
            runs.push(run);
          }
        } else if (child.nodeType === 1) {
          // Nested lists become their own paragraphs -- don't fold their text into
          // the parent list item's runs (the parent <li> keeps only its own intro).
          if (child.tagName === "OL" || child.tagName === "UL") return;
          const cls = child.classList;
          const childFill = fill || (cls && (cls.contains("nda-fill") || cls.contains("nda-fill-entity")));
          const childBlank = blank || (cls && cls.contains("nda-blank"));
          walk(child, bold || /^(B|STRONG)$/.test(child.tagName), childFill, childBlank);
        }
      });
    })(block, false, false, false);
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
    Object.assign(state, model()?.generatedGeneratorState({ matterId, paragraphs: paras }) || {
      generatorMode: "generated",
      generatorMatterId: matterId,
      generatorParagraphs: paras.map((p) => ({
        ...p,
        runs: Array.isArray(p.runs) ? p.runs.map((r) => ({ ...r })) : p.runs,
      })),
      generatorOriginalParagraphs: paras.map((p) => ({
        ...p,
        runs: Array.isArray(p.runs) ? p.runs.map((r) => ({ ...r })) : p.runs,
      })),
      generatorActiveParagraphId: null,
      generatorDraftTouched: false,
      generatorHistory: [],
    });
    render();
    return true;
  }

  function clear() {
    Object.assign(state, model()?.clearGeneratorState() || {
      generatorParagraphs: [],
      generatorMatterId: null,
      generatorActiveParagraphId: null,
      generatorDraftTouched: false,
      generatorMode: "draft",
      generatorHistory: [],
    });
    render();
  }

  function isActive() { return paragraphs().length > 0; }

  // ---- Render -------------------------------------------------------------
  function render() {
    const el = renderEl();
    if (!el) return;
    el.innerHTML = renderParagraphs(paragraphs());
    bindEditing();
    bindToolbar();
    refreshToolbar();
  }

  // Walks the flat paragraph list and wraps each run of consecutive table-cell
  // paragraphs (same table) into a presentational table grid so the signature
  // block renders as the side-by-side columns the export has, instead of a flat
  // vertical stack. Every paragraph frame inside the grid keeps its own id and
  // editable hooks, so editing / export are unchanged.
  function renderParagraphs(list) {
    const out = [];
    let i = 0;
    while (i < list.length) {
      const table = tableMeta(list[i]);
      if (!table) {
        out.push(renderParagraph(list[i]));
        i += 1;
        continue;
      }
      // Consume the whole table (all rows/cells) starting here.
      const tableIndex = table.table_index;
      let j = i;
      while (j < list.length) {
        const meta = tableMeta(list[j]);
        if (!meta || meta.table_index !== tableIndex) break;
        j += 1;
      }
      out.push(renderTable(list.slice(i, j)));
      i = j;
    }
    return out.join("");
  }

  function tableMeta(paragraph) {
    const table = paragraph && paragraph.table;
    return table && typeof table === "object" ? table : null;
  }

  // Renders a contiguous block of table-cell paragraphs as a CSS grid of cells.
  // Cells are keyed by (row_index, cell_index) and ordered by first appearance,
  // so a single-row two-cell signature table becomes two side-by-side columns.
  function renderTable(cellParagraphs) {
    const cells = [];
    const byKey = new Map();
    cellParagraphs.forEach((paragraph) => {
      const meta = tableMeta(paragraph) || {};
      const key = `${meta.row_index ?? 0}:${meta.cell_index ?? 0}`;
      let cell = byKey.get(key);
      if (!cell) {
        cell = { row: Number(meta.row_index) || 0, col: Number(meta.cell_index) || 0, paragraphs: [] };
        byKey.set(key, cell);
        cells.push(cell);
      }
      cell.paragraphs.push(paragraph);
    });
    const columnCount = cells.reduce((max, cell) => Math.max(max, cell.col), 0) || cells.length;
    const inner = cells
      .map((cell) => `<div class="generator-doc-table-cell">${cell.paragraphs.map(renderParagraph).join("")}</div>`)
      .join("");
    return `<div class="generator-doc-table" style="--gen-table-cols:${Math.max(columnCount, 1)}">${inner}</div>`;
  }

  // Render a single editor run to HTML, preserving the Generator's playbook
  // colours. The shared global renderFormattedRun already wraps a `fill` run in
  // the violet `.nda-fill-entity` highlight; here we additionally wrap a `blank`
  // run in the amber `.nda-blank` placeholder highlight so unfilled placeholders
  // keep the same look in the always-visible editor as they have in the live
  // preview. Like `fill`, `blank` is a render-only marker: it is never emitted as
  // run formatting on export, so neither highlight reaches the generated document.
  function renderGeneratorRun(run) {
    if (run && run.blank) {
      return `<span class="nda-blank">${renderFormattedRun(run, false)}</span>`;
    }
    return renderFormattedRun(run, false);
  }

  function renderParagraph(paragraph) {
    const text = String(paragraph.text || "");
    const runs = Array.isArray(paragraph.runs) ? paragraph.runs : null;
    const tiles = runs && runs.map((r) => String(r && r.text || "")).join("") === text;
    const body = tiles ? runs.map((run) => renderGeneratorRun(run)).join("") : escapeHtml(text);
    const id = escapeHtml(String(paragraph.id));
    const style = paragraphFormatStyleAttribute(paragraph);
    // Reuse the Review editor's structure derivation so headings, numbered/lettered
    // clauses (indent + captured marker) and signature-table cells render with the
    // same fidelity as the exported .docx. These are global helpers from
    // redline-rendering.js; the resulting classes/attributes are styled by the
    // shared .studio-doc-paragraph.doc-* rules in styles.css.
    const structureClasses = paragraphStructureClasses(paragraph);
    const classAttr = ["studio-doc-paragraph", "generator-doc-paragraph", ...structureClasses].join(" ");
    const structureAttrs = paragraphStructureAttributes(paragraph);
    const frameAttrs = `data-paragraph-id="${id}"${structureAttrs ? ` ${structureAttrs}` : ""}${style}`;
    return `<div class="${classAttr}" ${frameAttrs}>`
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
      const oldText = String(para.text || "");
      if (oldText === newText) return;
      // Capture the pre-edit paragraph ONCE per focus so Undo reverts the whole edit.
      if (editable.dataset.histRecorded !== "true") {
        pushHistory(para);
        editable.dataset.histRecorded = "true";
      }
      // Re-tile the run model against the edited text BEFORE we move para.text on, so
      // formatting on the characters that survived the edit is preserved (retained
      // chars keep their run props, inserted chars inherit the adjacent run). Without
      // this the runs go stale (stop tiling the new text), formatting visually drops
      // and the clean export loses it. Capture the OLD runs first.
      const retiled = retileRuns(para.runs, oldText, newText);
      para.text = newText;
      if (retiled) para.runs = retiled; else delete para.runs;
      markTouched();
      refreshToolbar();
    });
    editable.addEventListener("keyup", refreshToolbar);
    editable.addEventListener("mouseup", refreshToolbar);
    if (typeof pastePlainText === "function") editable.addEventListener("paste", pastePlainText);
  }

  function markTouched() {
    Object.assign(state, model()?.generatorTouchedState(state) || (state.generatorMode !== "generated" ? { generatorDraftTouched: true } : {}));
  }

  // ---- Run re-tiling on text edits ---------------------------------------
  // When a paragraph's TEXT is edited the run model must follow so the invariant
  // `runs.map(r=>r.text).join("") === text` keeps holding (renderParagraph and the
  // clean export's replacementRunsFor both fall back to plain text when it doesn't,
  // dropping formatting). We re-derive runs from a character-level diff of old->new
  // text: characters that survive keep their original run's formatting; inserted
  // characters inherit the formatting of the preceding surviving character (or the
  // following one at the very start). Returns the new run array (tidy, normalized),
  // or null when no formatting is worth carrying so the caller drops para.runs.
  function retileRuns(oldRuns, oldText, newText) {
    const props = formatKeyMap(oldRuns, oldText);
    // No source formatting at all -> nothing to preserve; let the model fall back to
    // plain text (renderParagraph re-derives a single unformatted run as needed).
    if (!props) return null;

    const oldChars = [...oldText];
    const newChars = [...newText];
    // Guard pathological diffs (same budget as charDiffOperations): on a huge edit,
    // re-tiling char-by-char isn't worth a stall -> drop runs to plain text.
    if (oldChars.length * newChars.length > 1000000) return null;

    const ops = typeof charDiffOperations === "function"
      ? charDiffOperations(oldText, newText)
      : null;
    if (!ops) return null;

    // Walk the diff, building a per-NEW-character format array. `props[oldIndex]` is
    // the format object (or null) for each old character; retained chars copy their
    // old format EXACTLY (an unformatted retained char stays unformatted), inserted
    // chars inherit the PRECEDING retained char's format. `isInsert` records which
    // new chars were inserted so the leading-insert prefix can be back-filled below
    // without disturbing retained-unformatted chars.
    const perChar = new Array(newChars.length);
    const isInsert = new Array(newChars.length).fill(false);
    let oldIndex = 0;
    let newIndex = 0;
    let lastRetainedFormat = null; // format of the most recent RETAINED (equal) char
    let seenRetained = false;
    for (const op of ops) {
      const tokenChars = [...String(op.token || "")];
      if (op.type === "equal") {
        for (let k = 0; k < tokenChars.length; k += 1) {
          const fmt = props[oldIndex] || null;
          lastRetainedFormat = fmt;
          seenRetained = true;
          if (newIndex < perChar.length) perChar[newIndex] = fmt;
          oldIndex += 1;
          newIndex += 1;
        }
      } else if (op.type === "delete") {
        oldIndex += tokenChars.length; // consumed from old, not present in new
      } else { // insert
        for (let k = 0; k < tokenChars.length; k += 1) {
          // Inherit the preceding retained char's format. Before any retained char
          // (a leading insert) this is null; the backward pass below patches it from
          // the following retained char so it joins the adjacent run.
          if (newIndex < perChar.length) {
            perChar[newIndex] = seenRetained ? lastRetainedFormat : null;
            isInsert[newIndex] = true;
          }
          newIndex += 1;
        }
      }
    }

    // Back-fill the leading-insert prefix (inserted chars before any retained char)
    // from the first RETAINED char's format, so a prepend inherits the adjacent run.
    // The first non-insert entry IS that retained char's format. Stop there:
    // retained-but-unformatted chars keep their own (null) format untouched.
    const firstRetainedIndex = isInsert.findIndex((ins) => !ins);
    if (firstRetainedIndex > 0) {
      const fill = perChar[firstRetainedIndex];
      for (let i = 0; i < firstRetainedIndex; i += 1) perChar[i] = fill;
    }

    // Coalesce consecutive same-format characters back into runs, then normalize +
    // merge so the run list is tidy and carries only set formatting keys.
    const runs = [];
    for (let i = 0; i < newChars.length; i += 1) {
      const fmt = perChar[i] || {};
      const last = runs.length ? runs[runs.length - 1] : null;
      if (last && formatEquals(last._fmt, fmt)) {
        last.text += newChars[i];
      } else {
        runs.push({ ...fmt, text: newChars[i], _fmt: fmt });
      }
    }
    const tidy = runs.map((run) => {
      delete run._fmt;
      return typeof normalizeRun === "function" ? normalizeRun(run) : run;
    });
    const merged = typeof mergeAdjacentRuns === "function" ? mergeAdjacentRuns(tidy) : tidy;

    // Invariant guard: the runs MUST tile the new text exactly. If anything drifted
    // (it shouldn't), drop runs so callers fall back to plain text rather than
    // shipping a broken model.
    if (merged.map((r) => String(r.text || "")).join("") !== newText) return null;
    // If no run carries any formatting, there's nothing to preserve -> let the model
    // drop para.runs (a single unformatted run is re-derived on render/export).
    if (!merged.some(hasAnyFormatting)) return null;
    return merged;
  }

  // Build a per-character array of format objects (everything on the run except its
  // text: bold/italic/underline/font/size). Returns null when the runs don't tile
  // oldText or carry no formatting -> nothing to preserve.
  function formatKeyMap(oldRuns, oldText) {
    const runs = Array.isArray(oldRuns) ? oldRuns : null;
    if (!runs || !runs.length) return null;
    if (runs.map((r) => String(r && r.text || "")).join("") !== oldText) return null;
    let anyFormat = false;
    const map = [];
    runs.forEach((run) => {
      const chars = [...String(run && run.text || "")];
      const fmt = runFormatOnly(run);
      if (Object.keys(fmt).length) anyFormat = true;
      chars.forEach(() => map.push(Object.keys(fmt).length ? fmt : null));
    });
    return anyFormat ? map : null;
  }

  // The formatting props of a run (no text). Mirrors normalizeRun's key set but
  // keeps it generic so an unknown formatting key (e.g. underline) still rides along.
  function runFormatOnly(run) {
    const out = {};
    if (run && run.bold) out.bold = true;
    if (run && run.italic) out.italic = true;
    if (run && run.underline) out.underline = true;
    const font = String(run && run.font || "").trim();
    if (font) out.font = font;
    const size = Number(run && run.size);
    if (Number.isFinite(size) && size > 0) out.size = size;
    return out;
  }

  function hasAnyFormatting(run) {
    return Boolean(run && (run.bold || run.italic || run.underline
      || String(run.font || "").trim() || Number(run.size) > 0));
  }

  function formatEquals(a, b) {
    const x = a || {};
    const y = b || {};
    return Boolean(x.bold) === Boolean(y.bold)
      && Boolean(x.italic) === Boolean(y.italic)
      && Boolean(x.underline) === Boolean(y.underline)
      && String(x.font || "").trim() === String(y.font || "").trim()
      && Number(x.size || 0) === Number(y.size || 0);
  }

  // ---- History / Undo -----------------------------------------------------
  function snapshotParagraph(paragraph) {
    return model()?.snapshotGeneratorParagraph(paragraph) || {
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
    if (model()?.pushGeneratorHistory) {
      state.generatorHistory = model().pushGeneratorHistory(history(), paragraph, HISTORY_LIMIT);
      return;
    }
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
    return model()?.generatorEditSnapshot(state) || {
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
  // The edited paragraph's run model (bold/italic/font/size), normalised + trimmed so
  // its joined text equals the stripped replacement_text -- attached to a replace
  // redline so the CLEAN export keeps the paragraph's formatting, not just plain text.
  // Null when the runs don't tile the current text (e.g. a free-form text edit dropped
  // them) -> the export falls back to the plain replacement_text.
  function replacementRunsFor(paragraph, replacementText) {
    const runs = Array.isArray(paragraph.runs) ? paragraph.runs : null;
    if (!runs || !runs.length) return null;
    if (runs.map((r) => String(r && r.text || "")).join("") !== String(paragraph.text || "")) return null;
    const copy = runs.map((r) => {
      const out = { text: String(r.text || "") };
      if (r.bold) out.bold = true;
      if (r.italic) out.italic = true;
      const font = String(r.font || "").trim();
      if (font) out.font = font;
      const size = Number(r.size);
      if (Number.isFinite(size) && size > 0) out.size = size;
      return out;
    });
    copy[0].text = copy[0].text.replace(/^\s+/, "");
    copy[copy.length - 1].text = copy[copy.length - 1].text.replace(/\s+$/, "");
    const tidy = copy.filter((r) => r.text.length);
    if (tidy.map((r) => r.text).join("") !== replacementText) return null;
    return tidy;
  }

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
      // Carry the run model through the replace path so the clean export preserves
      // formatting applied to the edited text (bold/italic/font/size).
      if (!isDelete) {
        const runs = replacementRunsFor(paragraph, replacementText);
        if (runs) redline.replacement_runs = runs;
      }
      return redline;
    }).filter(Boolean);
  }

  function hasEdits() {
    const redlines = exportRedlines();
    return model()?.generatorExportReady(state, redlines)
      || (state.generatorMode === "generated" && Boolean(state.generatorMatterId) && redlines.length > 0);
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

  return {
    showDraft, load, clear, isActive, edits, undo, render, hasEdits, exportCleanDocx,
    // Test seam: re-tiling logic is pure (old runs + old/new text -> new runs), so it
    // can be verified directly without simulating typing/caret in a headless preview.
    _retileRuns: retileRuns,
  };
})();
