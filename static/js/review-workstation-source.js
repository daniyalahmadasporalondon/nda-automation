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
