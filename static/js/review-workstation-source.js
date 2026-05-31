function manualExportRedlines() {
  const originalById = new Map(state.reviewOriginalParagraphs.map((paragraph) => [paragraph.id, paragraph]));
  return state.reviewParagraphs
    .map((paragraph) => {
      const original = originalById.get(paragraph.id);
      if (!original) return null;
      const originalText = String(original.text || "").trim();
      const replacementText = String(paragraph.text || "").trim();
      if (originalText === replacementText) return null;
      const isDelete = !replacementText;
      return {
        id: `manual-${paragraph.id}`,
        clause_id: "manual_viewer_edit",
        status: "proposed",
        action: isDelete ? REDLINE_DELETE_PARAGRAPH : REDLINE_REPLACE_PARAGRAPH,
        action_label: isDelete ? "Remove paragraph" : "Replace paragraph",
        paragraph_id: paragraph.id,
        paragraph_index: paragraph.index,
        source_index: paragraph.source_index || paragraph.index,
        original_text: originalText,
        replacement_text: replacementText,
      };
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
