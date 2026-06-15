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

function setupSourceEditors() {
  studioNdaText.addEventListener("input", () => {
    resizeSourceEditor(studioNdaText);
    if (studioNdaText.value.trim()) {
      markSourceEdited("Text edited");
    }
  });
  resizeSourceEditor(studioNdaText);
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
}

function showStudioDocumentRender() {
  if (!studioDocumentRender) return;
  studioNdaText.hidden = true;
  studioDocumentRender.hidden = false;
}
