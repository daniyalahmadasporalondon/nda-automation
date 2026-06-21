// Find & Replace for the paragraph editors (Review workstation + Generator).
//
// Both editors share the same flat-paragraph + inline-run model: each paragraph
// is `{ id, text, runs? }` and a text edit re-tiles `runs` via a char-level diff so
// run formatting (bold/italic/font/size/...) survives around the changed span
// (generator-editor.js retileRuns ~:498). Find & Replace reuses exactly that path:
// it locates matches by scanning paragraph TEXT, splices the replacement into the
// text, and asks the editor to re-tile the runs against the new text. The change
// then rides the editors' EXISTING manual-redline export — no new serializer op.
//
// This module is editor-agnostic: each editor registers a small ADAPTER describing
// how to read its paragraphs, render container, and how to apply a single
// paragraph's text change (so its own history/dirty/render hooks fire). The shared
// overlay panel (one per page, re-parented under the active editor) drives Find,
// Replace (next) and Replace all against whichever adapter is currently active.
window.findReplace = (function () {
  // Registered editor adapters, keyed by context name (e.g. "review", "generator").
  const adapters = new Map();
  let activeContext = null;
  let panel = null; // the shared overlay element (lazily built)
  let els = null; // cached panel sub-elements
  // The current match cursor for Replace (next): {paragraphId, start, end} into the
  // CURRENT paragraph text, recomputed after every replace so it never goes stale.
  let cursor = null;

  // ---- Shared re-tiling (formatting-preserving text replacement) ----------
  // Re-derive a paragraph's run model after a text replacement so the invariant
  // `runs.map(r=>r.text).join("") === text` keeps holding and surrounding run
  // formatting is preserved. Mirrors generator-editor.js retileRuns but is written
  // here too so the Review adapter (bare globals, no retile of its own) can reuse it.
  // Returns the new run array, or null when nothing is worth preserving (caller then
  // drops para.runs and the editor falls back to a single plain run).
  function retileRunsForReplace(oldRuns, oldText, newText) {
    const runs = Array.isArray(oldRuns) ? oldRuns : null;
    if (!runs || !runs.length) return null;
    if (runs.map((r) => String((r && r.text) || "")).join("") !== oldText) return null;
    if (typeof charDiffOperations !== "function") return null;

    // Per-old-character format object (everything on the run except its text).
    let anyFormat = false;
    const props = [];
    runs.forEach((run) => {
      const fmt = runFormatOnly(run);
      const hasFmt = Object.keys(fmt).length > 0;
      if (hasFmt) anyFormat = true;
      for (const _ch of [...String((run && run.text) || "")]) props.push(hasFmt ? fmt : null);
    });
    if (!anyFormat) return null;

    const oldChars = [...oldText];
    const newChars = [...newText];
    if (oldChars.length * newChars.length > 1000000) return null;

    const ops = charDiffOperations(oldText, newText);
    if (!ops) return null;

    const perChar = new Array(newChars.length);
    const isInsert = new Array(newChars.length).fill(false);
    let oldIndex = 0;
    let newIndex = 0;
    let lastRetainedFormat = null;
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
        oldIndex += tokenChars.length;
      } else {
        for (let k = 0; k < tokenChars.length; k += 1) {
          if (newIndex < perChar.length) {
            perChar[newIndex] = seenRetained ? lastRetainedFormat : null;
            isInsert[newIndex] = true;
          }
          newIndex += 1;
        }
      }
    }

    const firstRetainedIndex = isInsert.findIndex((ins) => !ins);
    if (firstRetainedIndex > 0) {
      const fill = perChar[firstRetainedIndex];
      for (let i = 0; i < firstRetainedIndex; i += 1) perChar[i] = fill;
    }

    const out = [];
    for (let i = 0; i < newChars.length; i += 1) {
      const fmt = perChar[i] || {};
      const last = out.length ? out[out.length - 1] : null;
      if (last && formatEquals(last._fmt, fmt)) {
        last.text += newChars[i];
      } else {
        out.push({ ...fmt, text: newChars[i], _fmt: fmt });
      }
    }
    const tidy = out.map((run) => {
      delete run._fmt;
      return typeof normalizeRun === "function" ? normalizeRun(run) : run;
    });
    const merged = typeof mergeAdjacentRuns === "function" ? mergeAdjacentRuns(tidy) : tidy;
    if (merged.map((r) => String(r.text || "")).join("") !== newText) return null;
    if (!merged.some(hasAnyFormatting)) return null;
    return merged;
  }

  function runFormatOnly(run) {
    const out = {};
    if (run && run.bold) out.bold = true;
    if (run && run.italic) out.italic = true;
    if (run && run.underline) out.underline = true;
    if (run && run.strike) out.strike = true;
    const font = String((run && run.font) || "").trim();
    if (font) out.font = font;
    const size = Number(run && run.size);
    if (Number.isFinite(size) && size > 0) out.size = size;
    const color = String((run && run.color) || "").trim();
    if (color) out.color = color;
    const highlight = String((run && run.highlight) || "").trim();
    if (highlight) out.highlight = highlight;
    return out;
  }

  function hasAnyFormatting(run) {
    return Boolean(
      run && (run.bold || run.italic || run.underline || run.strike
        || String(run.font || "").trim() || Number(run.size) > 0
        || String(run.color || "").trim() || String(run.highlight || "").trim()),
    );
  }

  function formatEquals(a, b) {
    const x = a || {};
    const y = b || {};
    return Boolean(x.bold) === Boolean(y.bold)
      && Boolean(x.italic) === Boolean(y.italic)
      && Boolean(x.underline) === Boolean(y.underline)
      && Boolean(x.strike) === Boolean(y.strike)
      && String(x.font || "").trim() === String(y.font || "").trim()
      && Number(x.size || 0) === Number(y.size || 0)
      && String(x.color || "").trim() === String(y.color || "").trim()
      && String(x.highlight || "").trim() === String(y.highlight || "").trim();
  }

  // ---- Match scanning -----------------------------------------------------
  // All matches of `needle` across the live paragraphs, in document order. Each is
  // {paragraph, paragraphId, start, end}. Case-insensitive when caseInsensitive.
  function findMatches(adapter, needle, caseInsensitive) {
    const out = [];
    if (!needle) return out;
    const paras = adapter.paragraphs() || [];
    const hay = (text) => (caseInsensitive ? String(text).toLowerCase() : String(text));
    const probe = caseInsensitive ? needle.toLowerCase() : needle;
    paras.forEach((paragraph) => {
      const text = String(paragraph.text || "");
      const search = hay(text);
      let from = 0;
      while (from <= search.length) {
        const at = search.indexOf(probe, from);
        if (at === -1) break;
        out.push({ paragraph, paragraphId: paragraph.id, start: at, end: at + needle.length });
        from = at + needle.length; // non-overlapping
      }
    });
    return out;
  }

  // Apply a single replacement to one paragraph: splice [start,end) out of its text,
  // insert `replacement`, re-tile runs, and let the editor commit (render + dirty +
  // history). Returns the new paragraph text length delta so a per-paragraph loop can
  // advance its scan cursor.
  function replaceInParagraph(adapter, paragraph, start, end, replacement) {
    const oldText = String(paragraph.text || "");
    const newText = oldText.slice(0, start) + replacement + oldText.slice(end);
    if (newText === oldText) return false;
    adapter.applyReplacement(paragraph, newText, oldText);
    return true;
  }

  // ---- Replace (next) -----------------------------------------------------
  function doReplaceNext() {
    const adapter = adapters.get(activeContext);
    if (!adapter) return;
    const needle = els.find.value;
    const replacement = els.replace.value;
    const caseInsensitive = els.caseToggle.checked;
    if (!needle) { setStatus("Enter text to find", "neutral"); return; }
    const matches = findMatches(adapter, needle, caseInsensitive);
    if (!matches.length) { cursor = null; setStatus("No matches", "empty"); return; }

    // Pick the first match at/after the cursor (wrapping to the top), so repeated
    // clicks walk through the document. The cursor is keyed by paragraph order so it
    // survives the text shift the previous replace caused.
    let target = matches[0];
    if (cursor) {
      const order = matchOrderKey(adapter, matches);
      const cursorKey = paragraphOrderIndex(adapter, cursor.paragraphId) * 1e6 + cursor.start;
      const next = order.find((m) => m.key >= cursorKey);
      target = (next || order[0]).match;
    }

    const ok = replaceInParagraph(adapter, target.paragraph, target.start, target.end, replacement);
    if (!ok) { setStatus("No matches", "empty"); return; }
    // Advance the cursor PAST the just-inserted replacement in the same paragraph.
    cursor = { paragraphId: target.paragraphId, start: target.start + replacement.length, end: target.start + replacement.length };
    adapter.afterBatch();
    const remaining = findMatches(adapter, needle, caseInsensitive).length;
    setStatus(remaining ? `Replaced 1 · ${remaining} left` : "Replaced 1 · no matches left", remaining ? "ok" : "empty");
    scrollToCursor(adapter);
  }

  // Stable cross-paragraph ordering of matches as {key, match}, key = paraOrder*1e6+start.
  function matchOrderKey(adapter, matches) {
    return matches
      .map((m) => ({ key: paragraphOrderIndex(adapter, m.paragraphId) * 1e6 + m.start, match: m }))
      .sort((a, b) => a.key - b.key);
  }

  function paragraphOrderIndex(adapter, paragraphId) {
    const paras = adapter.paragraphs() || [];
    const idx = paras.findIndex((p) => String(p.id) === String(paragraphId));
    return idx < 0 ? paras.length : idx;
  }

  // ---- Replace all --------------------------------------------------------
  function doReplaceAll() {
    const adapter = adapters.get(activeContext);
    if (!adapter) return;
    const needle = els.find.value;
    const replacement = els.replace.value;
    const caseInsensitive = els.caseToggle.checked;
    if (!needle) { setStatus("Enter text to find", "neutral"); return; }

    let total = 0;
    const paras = adapter.paragraphs() || [];
    const hay = (text) => (caseInsensitive ? String(text).toLowerCase() : String(text));
    const probe = caseInsensitive ? needle.toLowerCase() : needle;
    // Rebuild each paragraph in one pass: walk its text, copying spans and swapping
    // every (non-overlapping) match for the replacement, then apply ONCE so the run
    // re-tile + history entry are per-paragraph rather than per-occurrence.
    paras.forEach((paragraph) => {
      const oldText = String(paragraph.text || "");
      const search = hay(oldText);
      let from = 0;
      let next = "";
      let count = 0;
      while (from <= search.length) {
        const at = search.indexOf(probe, from);
        if (at === -1) { next += oldText.slice(from); break; }
        next += oldText.slice(from, at) + replacement;
        from = at + needle.length;
        count += 1;
      }
      if (count > 0 && next !== oldText) {
        adapter.applyReplacement(paragraph, next, oldText);
        total += count;
      }
    });

    cursor = null;
    if (total > 0) {
      adapter.afterBatch();
      setStatus(`Replaced ${total} occurrence${total === 1 ? "" : "s"}`, "ok");
    } else {
      setStatus("No matches", "empty");
    }
  }

  // ---- Find-only (count + scroll) -----------------------------------------
  function refreshCount() {
    const adapter = adapters.get(activeContext);
    if (!adapter) return;
    const needle = els.find.value;
    if (!needle) { setStatus("", "neutral"); cursor = null; return; }
    const matches = findMatches(adapter, needle, els.caseToggle.checked);
    setStatus(matches.length ? `${matches.length} match${matches.length === 1 ? "" : "es"}` : "No matches", matches.length ? "neutral" : "empty");
    if (!matches.length) cursor = null;
  }

  function scrollToCursor(adapter) {
    if (!cursor) return;
    const root = adapter.getRenderEl();
    if (!root) return;
    const frame = root.querySelector(`[data-paragraph-id="${cssSafe(cursor.paragraphId)}"]`);
    if (frame && typeof frame.scrollIntoView === "function") {
      frame.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  function cssSafe(value) {
    if (typeof window !== "undefined" && window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(String(value));
    }
    return String(value).replace(/["\\\]]/g, "\\$&");
  }

  // ---- Panel UI -----------------------------------------------------------
  function buildPanel() {
    if (panel) return panel;
    panel = document.createElement("div");
    panel.className = "find-replace-panel";
    panel.id = "findReplacePanel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Find and replace");
    panel.hidden = true;
    panel.innerHTML = `
      <div class="fr-row">
        <input type="text" class="fr-input" id="frFind" placeholder="Find" aria-label="Find" autocomplete="off" spellcheck="false">
        <span class="fr-status" id="frStatus" aria-live="polite"></span>
        <button type="button" class="fr-close" id="frClose" aria-label="Close find and replace" title="Close (Esc)">&times;</button>
      </div>
      <div class="fr-row">
        <input type="text" class="fr-input" id="frReplace" placeholder="Replace with" aria-label="Replace with" autocomplete="off" spellcheck="false">
        <button type="button" class="fr-btn" id="frReplaceNext" title="Replace next match">Replace</button>
        <button type="button" class="fr-btn fr-btn-primary" id="frReplaceAll" title="Replace every match">Replace all</button>
      </div>
      <label class="fr-opt"><input type="checkbox" id="frCaseInsensitive" checked> Ignore case</label>`;
    document.body.appendChild(panel);
    els = {
      find: panel.querySelector("#frFind"),
      replace: panel.querySelector("#frReplace"),
      status: panel.querySelector("#frStatus"),
      caseToggle: panel.querySelector("#frCaseInsensitive"),
      replaceNext: panel.querySelector("#frReplaceNext"),
      replaceAll: panel.querySelector("#frReplaceAll"),
      close: panel.querySelector("#frClose"),
    };
    els.find.addEventListener("input", refreshCount);
    els.caseToggle.addEventListener("change", () => { cursor = null; refreshCount(); });
    els.replaceNext.addEventListener("click", doReplaceNext);
    els.replaceAll.addEventListener("click", doReplaceAll);
    els.close.addEventListener("click", () => close());
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
      else if (event.key === "Enter") {
        // Enter in the Find field = Replace next (convenience); shift+Enter = all.
        event.preventDefault();
        if (event.shiftKey) doReplaceAll(); else doReplaceNext();
      }
    });
    return panel;
  }

  function setStatus(message, tone) {
    if (!els) return;
    els.status.textContent = message || "";
    els.status.dataset.tone = tone || "neutral";
  }

  // Open the panel for a given editor context, anchored over that editor's container.
  function open(context) {
    const adapter = adapters.get(context);
    if (!adapter) return;
    activeContext = context;
    cursor = null;
    buildPanel();
    const host = adapter.getPanelHost ? adapter.getPanelHost() : adapter.getRenderEl();
    if (host && host !== panel.parentElement) {
      // Anchor the panel inside the editor's relatively-positioned container so it
      // floats over the document without covering a different tab's editor.
      host.appendChild(panel);
    }
    panel.hidden = false;
    // Seed Find with the current selection text if any (common editor affordance).
    const selected = String(window.getSelection ? window.getSelection().toString() : "").trim();
    if (selected && selected.length <= 200) els.find.value = selected;
    refreshCount();
    els.find.focus();
    els.find.select();
  }

  function close() {
    if (panel) panel.hidden = true;
    cursor = null;
    const adapter = adapters.get(activeContext);
    if (adapter && adapter.getRenderEl) {
      const root = adapter.getRenderEl();
      // Return focus to the editor so keyboard editing resumes naturally.
      const firstEditable = root && root.querySelector("[data-editable-paragraph-id]");
      if (firstEditable && typeof firstEditable.focus === "function") firstEditable.focus({ preventScroll: true });
    }
  }

  function isOpen() { return Boolean(panel && !panel.hidden); }

  // ---- Registration + keyboard wiring -------------------------------------
  function register(context, adapter) {
    if (!context || !adapter || typeof adapter.paragraphs !== "function"
      || typeof adapter.applyReplacement !== "function") return;
    if (typeof adapter.afterBatch !== "function") adapter.afterBatch = () => {};
    if (typeof adapter.getRenderEl !== "function") adapter.getRenderEl = () => null;
    adapters.set(context, adapter);
  }

  // Decide which registered editor a Ctrl/Cmd+F should target: the one whose view is
  // currently visible (its render container is in the DOM and not hidden). Returns the
  // context name, or null when no editor is on screen.
  function visibleContext() {
    for (const [context, adapter] of adapters.entries()) {
      const root = adapter.getRenderEl();
      if (!root) continue;
      const view = root.closest("[data-view]") || root.closest(".view");
      const hidden = (view && view.hidden) || root.hidden
        || (root.offsetParent === null && root.getClientRects().length === 0);
      if (!hidden) return context;
    }
    return null;
  }

  let keyboardBound = false;
  function bindKeyboard() {
    if (keyboardBound) return;
    keyboardBound = true;
    document.addEventListener("keydown", (event) => {
      const isFind = (event.ctrlKey || event.metaKey) && !event.altKey
        && (event.key === "f" || event.key === "F");
      if (!isFind) return;
      const context = visibleContext();
      if (!context) return; // no editor visible -> let the browser's native find run
      event.preventDefault();
      open(context);
    });
  }

  // Auto-bind the keyboard handler once the DOM is ready.
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", bindKeyboard);
    } else {
      bindKeyboard();
    }
  }

  return {
    register,
    open,
    close,
    isOpen,
    bindKeyboard,
    // Test seams: the pure logic (match scan + formatting-preserving re-tile) so the
    // FE test can verify Replace-all behaviour without driving the full UI.
    _findMatches: findMatches,
    _retileRunsForReplace: retileRunsForReplace,
  };
})();
