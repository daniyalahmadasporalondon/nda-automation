function manualExportRedlines() {
  const baseline = manualRedlineBaselineParagraphs();
  const originalById = new Map(baseline.map((paragraph) => [paragraph.id, paragraph]));
  return state.reviewParagraphs
    .map((paragraph) => {
      const original = originalById.get(paragraph.id);
      if (!original) return null;
      const originalText = String(original.text || "").trim();
      const replacementText = String(paragraph.text || "").trim();
      // Trimmed text equal to baseline: emit a paragraph-level format redline
      // (alignment/font) if one is pending, otherwise nothing. Only a
      // format_paragraph result is taken here so a whitespace-only difference
      // keeps its prior behaviour (no redline) rather than becoming a replace.
      if (originalText === replacementText) {
        const formatRedline = manualParagraphRedline(paragraph, baseline);
        return formatRedline?.action === redlineFormatParagraphAction() ? formatRedline : null;
      }
      const isDelete = !replacementText;
      const redline = {
        id: `manual-${paragraph.id}`,
        clause_id: manualViewerEditClauseId(),
        status: "proposed",
        action: isDelete ? REDLINE_DELETE_PARAGRAPH : REDLINE_REPLACE_PARAGRAPH,
        action_label: isDelete ? "Remove paragraph" : "Replace paragraph",
        paragraph_id: paragraph.id,
        paragraph_index: original.index || paragraph.index,
        original_text: originalText,
        replacement_text: replacementText,
      };
      if (original.source_index !== undefined || paragraph.source_index !== undefined) {
        redline.source_index = original.source_index || paragraph.source_index || paragraph.index;
      }
      if (original.source_part) redline.source_part = original.source_part;
      return redline;
    })
    .filter(Boolean);
}

function setSourceText(text) {
  studioNdaText.value = text;
}

function setSourcePlaceholder(placeholder) {
  studioNdaText.placeholder = placeholder;
}

function setFileMeta(message) {
  studioFileMeta.textContent = message;
}

function setCounterpartyMeta(counterparty) {
  if (!studioCounterpartyMeta) return;
  const value = String(counterparty || "").trim();
  studioCounterpartyMeta.textContent = value || "-";
  studioCounterpartyMeta.title = value;
}

// --- counterparty human-confirmation field ---------------------------------
// The AI extracts the counterparty from the NDA preamble; when the verifier
// refutes it or confidence is low the matter is flagged needs_confirmation. This
// field shows the current name + confidence and lets a human confirm (accept the
// shown name) or edit (type the real one), POSTing the override and refreshing.

function renderCounterpartyConfirmation(matter) {
  if (!studioCounterpartyField) return;
  if (!matter || !matter.id) {
    studioCounterpartyField.hidden = true;
    return;
  }
  studioCounterpartyField.hidden = false;
  const name = String(matter.counterparty || "").trim();
  if (studioCounterpartyName) {
    studioCounterpartyName.textContent = name || "Unknown Counterparty";
    studioCounterpartyName.title = name;
  }
  const needsConfirmation = matter.counterparty_needs_confirmation !== false;
  if (studioCounterpartyUnconfirmed) studioCounterpartyUnconfirmed.hidden = !needsConfirmation;
  if (studioCounterpartyField.classList) {
    studioCounterpartyField.classList.toggle("is-unconfirmed", needsConfirmation);
  }
  if (studioCounterpartyConfidence) {
    const raw = Number(matter.counterparty_confidence);
    const source = String(matter.counterparty_source || "");
    if (source === "human") {
      studioCounterpartyConfidence.textContent = "confirmed by you";
      studioCounterpartyConfidence.hidden = false;
    } else if (Number.isFinite(raw) && raw > 0) {
      studioCounterpartyConfidence.textContent = `${Math.round(raw * 100)}% confidence`;
      studioCounterpartyConfidence.hidden = false;
    } else {
      studioCounterpartyConfidence.textContent = "";
      studioCounterpartyConfidence.hidden = true;
    }
  }
  // The Confirm button accepts the shown name; disable it when there is nothing
  // confident to accept (no extracted name to confirm).
  if (studioCounterpartyConfirmButton) studioCounterpartyConfirmButton.disabled = !name;
  closeCounterpartyEdit();
  setCounterpartyStatus("");
}

function setCounterpartyStatus(message, { error = false } = {}) {
  if (!studioCounterpartyStatus) return;
  studioCounterpartyStatus.textContent = message || "";
  studioCounterpartyStatus.hidden = !message;
  if (studioCounterpartyStatus.classList) {
    studioCounterpartyStatus.classList.toggle("is-error", Boolean(error));
  }
}

function openCounterpartyEdit() {
  if (!studioCounterpartyEditForm) return;
  studioCounterpartyEditForm.hidden = false;
  if (studioCounterpartyEditInput) {
    studioCounterpartyEditInput.value = String(state.selectedMatter?.counterparty || "").trim();
    studioCounterpartyEditInput.focus();
    studioCounterpartyEditInput.select?.();
  }
}

function closeCounterpartyEdit() {
  if (studioCounterpartyEditForm) studioCounterpartyEditForm.hidden = true;
}

async function submitCounterpartyOverride(name) {
  const matterId = state.selectedMatter?.id;
  const cleaned = String(name || "").trim();
  if (!matterId || !cleaned) {
    setCounterpartyStatus("Enter a counterparty name to confirm.", { error: true });
    return;
  }
  setCounterpartyStatus("Saving...");
  if (studioCounterpartyConfirmButton) studioCounterpartyConfirmButton.disabled = true;
  try {
    const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/counterparty`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: cleaned }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload?.error || "Could not confirm the counterparty.");
    }
    const updated = payload?.matter;
    if (updated && updated.id) {
      state.selectedMatter = { ...state.selectedMatter, ...updated };
      // Reload so the meta + badge + any name-derived UI reflect the confirmed value.
      loadMatterIntoReview(state.selectedMatter);
    }
    setCounterpartyStatus("Counterparty confirmed.");
  } catch (error) {
    setCounterpartyStatus(error?.message || "Could not confirm the counterparty.", { error: true });
    if (studioCounterpartyConfirmButton) studioCounterpartyConfirmButton.disabled = false;
  }
}

function setupCounterpartyConfirmation() {
  studioCounterpartyConfirmButton?.addEventListener("click", () => {
    submitCounterpartyOverride(state.selectedMatter?.counterparty);
  });
  studioCounterpartyEditButton?.addEventListener("click", openCounterpartyEdit);
  studioCounterpartyEditCancel?.addEventListener("click", () => {
    closeCounterpartyEdit();
    setCounterpartyStatus("");
  });
  studioCounterpartyEditForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitCounterpartyOverride(studioCounterpartyEditInput?.value);
  });
}

function setDocumentTitle(title) {
  studioDocTitle.textContent = title;
}

// FIX 1 (P0): typed edits in the #studioNdaText source textarea used to be a pure
// no-op against the document model -- the input handler only resized + marked the
// file "edited", so the typed characters lived ONLY in the DOM .value. The model
// (state.reviewParagraphs) is the real source for redline export / render / send,
// and syncReviewSourceFromParagraphs() later writes the model text back OVER the
// textarea, so any pending typing silently VANISHED. The fix below reconciles the
// textarea text back INTO state.reviewParagraphs (the inverse of
// syncReviewSourceFromParagraphs, which joins paragraph.text on "\n\n"): we split
// the text on blank-line blocks and re-derive the paragraph model from it,
// PRESERVING the existing paragraph ids / index / source_index / clause bindings
// positionally so we never corrupt the round-trip. We commit on debounce while
// typing AND eagerly on blur. A `sourceTextDirty` flag (mirrors the
// redlineDraftDirty guard pattern) then stops syncReviewSourceFromParagraphs from
// discarding input that has not yet been reconciled.

let sourceReconcileTimer = null;
const SOURCE_RECONCILE_DEBOUNCE_MS = 400;

// Split the source text into blank-line-delimited blocks, exactly mirroring how
// syncReviewSourceFromParagraphs() rebuilt the text (trimmed paragraph text joined
// by "\n\n"). Empty blocks are dropped so a run of blank lines does not spawn
// phantom paragraphs.
function sourceTextBlocks(text) {
  return String(text || "")
    .split(/\n[ \t]*\n+/)
    .map((block) => block.trim())
    .filter(Boolean);
}

// Re-derive state.reviewParagraphs from the current textarea contents. The model
// stays the authoritative source for export/render/send, so we mutate the EXISTING
// paragraph objects in place where a block lines up positionally (keeping id /
// index / source_index / source_part / clause bindings intact) and only mint a new
// synthetic-id paragraph for blocks beyond the prior count. Returns true when the
// model actually changed.
function reconcileSourceTextIntoParagraphs() {
  if (!studioNdaText || studioNdaText.hidden) return false;
  if (!Array.isArray(state.reviewParagraphs)) return false;
  const blocks = sourceTextBlocks(studioNdaText.value);
  const previous = state.reviewParagraphs;
  let changed = blocks.length !== previous.length;
  const next = blocks.map((blockText, position) => {
    const existing = previous[position];
    if (existing) {
      if (String(existing.text || "") !== blockText) {
        changed = true;
        // A free-form text edit invalidates any inline runs that tiled the OLD
        // text; leave them present (they go inert via the join==text render guard,
        // mirroring syncViewerParagraphEdit) rather than corrupt offsets.
        return { ...existing, text: blockText };
      }
      return existing;
    }
    changed = true;
    return {
      id: `source-${Date.now()}-${position}`,
      index: position,
      text: blockText,
    };
  });
  if (!changed) {
    state.sourceTextDirty = false;
    return false;
  }
  state.reviewParagraphs = next;
  state.reviewSourceText = blocks.join("\n\n");
  state.sourceTextDirty = false;
  return true;
}

// FIX 4 (P2): the Review-tab "Paste NDA text here" workspace had NO way to run a
// review on the pasted text -- it was a dead end. These helpers add a "Review
// pasted text" control that runs the SAME /api/review path the rest of the app uses
// (a full, non-offline review) and renders the result in place via the existing
// renderResult(). The action bar is shown only while the source textarea is the
// active surface (no review loaded) and is enabled only when there is text. We do
// NOT invent an endpoint -- /api/review already accepts { text } and returns the
// full { clauses, paragraphs, redline_edits } result that renderResult expects.

function sourceReviewBarEl() {
  return document.getElementById("studioSourceReviewBar");
}
function reviewPastedButtonEl() {
  return document.getElementById("studioReviewPastedButton");
}
function reviewPastedStatusEl() {
  return document.getElementById("studioReviewPastedStatus");
}

function setReviewPastedStatus(message, { error = false } = {}) {
  const el = reviewPastedStatusEl();
  if (!el) return;
  el.textContent = message || "";
  el.hidden = !message;
  if (el.classList) el.classList.toggle("is-error", Boolean(error));
}

// Show the Review-pasted bar only when the source textarea is the live surface;
// enable the button only when there is text to review.
function updateSourceReviewBar() {
  const bar = sourceReviewBarEl();
  const button = reviewPastedButtonEl();
  const editorActive = Boolean(studioNdaText && !studioNdaText.hidden);
  if (bar) {
    bar.hidden = !editorActive;
    // The inline layout style would otherwise win over [hidden]; drive display
    // directly so the bar truly hides when the rendered surface is showing.
    bar.style.display = editorActive ? "flex" : "none";
  }
  if (button) button.disabled = !editorActive || !String(studioNdaText.value || "").trim();
  if (!editorActive) setReviewPastedStatus("");
}

async function reviewPastedSourceText() {
  const button = reviewPastedButtonEl();
  const text = String(studioNdaText.value || "").trim();
  if (!text) {
    setReviewPastedStatus("Paste NDA text first.", { error: true });
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = "Reviewing…";
  }
  setReviewPastedStatus("Reviewing pasted text…");
  try {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw (typeof reviewErrorFromPayload === "function"
        ? reviewErrorFromPayload(payload, "Review could not run")
        : new Error(payload?.error || "Review could not run"));
    }
    // renderResult swaps the textarea for the rendered review surface and clears
    // the source-dirty guard, so the pasted text is now the live review model.
    renderResult(payload, text);
    setReviewPastedStatus("");
  } catch (error) {
    setReviewPastedStatus(error?.message || "Review could not run.", { error: true });
  } finally {
    if (button) button.textContent = "Review pasted text";
    updateSourceReviewBar();
  }
}

function setupSourceEditors() {
  studioNdaText.addEventListener("input", () => {
    resizeSourceEditor(studioNdaText);
    if (studioNdaText.value.trim()) {
      markSourceEdited("Text edited");
    }
    // Mark the textarea dirty immediately so a sync from the model cannot discard
    // the pending characters before the debounced reconcile lands.
    state.sourceTextDirty = true;
    if (sourceReconcileTimer !== null) window.clearTimeout(sourceReconcileTimer);
    sourceReconcileTimer = window.setTimeout(() => {
      sourceReconcileTimer = null;
      reconcileSourceTextIntoParagraphs();
    }, SOURCE_RECONCILE_DEBOUNCE_MS);
    updateSourceReviewBar();
  });
  // Commit eagerly on blur so a click straight to Export/Send/Save never races the
  // debounce timer and loses the last keystrokes.
  studioNdaText.addEventListener("blur", () => {
    if (sourceReconcileTimer !== null) {
      window.clearTimeout(sourceReconcileTimer);
      sourceReconcileTimer = null;
    }
    reconcileSourceTextIntoParagraphs();
  });
  reviewPastedButtonEl()?.addEventListener("click", () => {
    reviewPastedSourceText();
  });
  resizeSourceEditor(studioNdaText);
  updateSourceReviewBar();
}

function resizeSourceEditors() {
  resizeSourceEditor(studioNdaText);
}

function resizeSourceEditor(input) {
  if (!input || input.hidden) return;
  input.style.height = "auto";
  input.style.height = `${Math.max(input.scrollHeight, input.clientHeight)}px`;
}

function showStudioSourceEditor() {
  if (!studioDocumentRender) return;
  studioDocumentRender.hidden = true;
  studioDocumentRender.innerHTML = "";
  studioNdaText.hidden = false;
  resizeSourceEditor(studioNdaText);
  updateSourceReviewBar();
}

function showStudioDocumentRender() {
  if (!studioDocumentRender) return;
  studioNdaText.hidden = true;
  studioDocumentRender.hidden = false;
  updateSourceReviewBar();
}
