function renderReviewDocument({
  clauses,
  originalParagraphs,
  paragraphs,
  redlines,
  selectedClauseId,
  viewMode,
}) {
  const clausesByParagraphId = new Map();
  clauses.forEach((clause) => {
    (clause.matched_paragraph_ids || []).forEach((paragraphId) => {
      if (!clausesByParagraphId.has(paragraphId)) clausesByParagraphId.set(paragraphId, []);
      clausesByParagraphId.get(paragraphId).push(clause);
    });
  });

  const redlinesByParagraphId = new Map();
  redlines.forEach((edit) => {
    if (!redlinesByParagraphId.has(edit.paragraph_id)) redlinesByParagraphId.set(edit.paragraph_id, []);
    redlinesByParagraphId.get(edit.paragraph_id).push(edit);
  });

  return paragraphs
    .map((paragraph) => renderDocumentParagraph(paragraphViewModel(paragraph, {
      clauses,
      clausesByParagraphId,
      originalParagraphs,
      redlinesByParagraphId,
      selectedClauseId,
      viewMode,
    })))
    .join("");
}

function paragraphViewModel(paragraph, context) {
  const redlines = context.redlinesByParagraphId.get(paragraph.id) || [];
  const redlineClauses = redlines
    .map((edit) => context.clauses.find((clause) => clause.id === edit.clause_id))
    .filter(Boolean);
  const linkedClauses = mergeClauses(context.clausesByParagraphId.get(paragraph.id) || [], redlineClauses);
  const selectedClause = linkedClauses.find((clause) => clause.id === context.selectedClauseId);
  const selectedRedline = redlines.find((edit) => edit.clause_id === context.selectedClauseId);
  const manualRedline = manualParagraphRedline(paragraph, context.originalParagraphs);
  const primaryClause = selectedClause || linkedClauses.find((clause) => !clauseStatus(clause).passes) || linkedClauses[0];
  const primaryRedline = manualRedline || selectedRedline || redlines[0] || null;
  const visibleRedlines = visibleParagraphRedlines(redlines, manualRedline, selectedRedline, primaryRedline);

  return {
    ids: linkedClauses.map((clause) => clause.id).join(" "),
    linkedClauses,
    manualRedline,
    originalParagraphs: context.originalParagraphs,
    paragraph,
    plan: paragraphRedlinePlan(paragraph, redlines, manualRedline),
    primaryClause,
    primaryRedline,
    redlines,
    selected: Boolean(selectedClause),
    visibleRedlines,
    viewMode: context.viewMode,
  };
}

function visibleParagraphRedlines(redlines, manualRedline, selectedRedline, primaryRedline) {
  if (manualRedline) return redlines.filter(isInsertionRedline);
  if (redlines.every(isInsertionRedline)) return selectedRedline ? [selectedRedline] : redlines;
  return primaryRedline ? [primaryRedline] : [];
}

function renderDocumentParagraph(model) {
  if (model.viewMode === VIEW_MODE_CLEAN) return renderCleanDocumentParagraph(model);
  if (model.viewMode === VIEW_MODE_SIDE_BY_SIDE) return renderSideBySideDocumentParagraph(model);
  return renderRedlineDocumentParagraph(model);
}

function renderCleanDocumentParagraph(model) {
  let html = "";
  if (!model.plan.remove) {
    html += renderParagraphFrame(model, {
      body: escapeHtml(model.plan.cleanText),
      classes: ["doc-clean-paragraph"],
    });
  }
  return html + renderInsertedParagraphs(model.plan.inserts, VIEW_MODE_CLEAN, model.paragraph.id);
}

function renderSideBySideDocumentParagraph(model) {
  const original = escapeHtml(originalParagraphText(model.paragraph, model.originalParagraphs));
  const redlined = renderSideBySideRedline(model.paragraph, model.plan) || '<span class="sxs-empty">-</span>';
  const body = `
    <div class="clause-sxs">
      <div class="clause-sxs-col"><span class="clause-sxs-tag">Original</span><div>${original}</div></div>
      <div class="clause-sxs-col latest"><span class="clause-sxs-tag">Redline</span><div>${redlined}</div></div>
    </div>
  `;
  return renderParagraphFrame(model, {
    body,
    classes: ["doc-sxs-paragraph"],
  }) + renderInsertedParagraphs(model.plan.inserts, VIEW_MODE_SIDE_BY_SIDE, model.paragraph.id);
}

function renderRedlineDocumentParagraph(model) {
  return renderParagraphFrame(model, {
    body: renderRedlineParagraphBody(model.paragraph, model.primaryRedline, model.visibleRedlines),
    classes: [
      model.linkedClauses.length ? "has-clause" : "",
      model.redlines.length || model.manualRedline ? "has-redline" : "",
      model.manualRedline ? "manual-redline" : "",
      model.primaryRedline?.action === REDLINE_DELETE_PARAGRAPH ? "redline-delete" : "",
      model.primaryRedline?.action === REDLINE_INSERT_AFTER_PARAGRAPH ? "redline-insert" : "",
      model.primaryClause && !clauseStatus(model.primaryClause).passes ? "verify" : "",
      model.primaryClause && clauseStatus(model.primaryClause).passes ? "match" : "",
    ],
  });
}

function renderParagraphFrame(model, { body, classes = [] }) {
  return renderStudioParagraphFrame({
    body,
    classes,
    clauseIds: model.ids,
    paragraphId: model.paragraph.id,
    selected: model.selected,
  });
}

function renderStudioParagraphFrame({ body, classes = [], clauseIds = "", paragraphId = "", selected = false, attributes = "" }) {
  const frameAttributes = [];
  if (paragraphId) frameAttributes.push(`data-paragraph-id="${escapeHtml(paragraphId)}"`);
  if (clauseIds) frameAttributes.push(`data-clause-ids="${escapeHtml(clauseIds)}"`);
  if (attributes) frameAttributes.push(attributes);
  return `
    <div class="${joinClasses("studio-doc-paragraph", classes, selected ? "selected" : "")}"${frameAttributes.length ? ` ${frameAttributes.join(" ")}` : ""}>
      ${body}
    </div>
  `;
}

function renderInsertedParagraphs(inserts, viewMode, anchorParagraphId = "") {
  return inserts.map((edit) => {
    const inserted = escapeHtml(String(edit.insert_text || edit.replacement_text || ""));
    const attributes = `data-redline-edit-id="${escapeHtml(edit.id || "")}" data-redline-anchor-id="${escapeHtml(anchorParagraphId)}"`;
    if (viewMode === VIEW_MODE_SIDE_BY_SIDE) {
      return renderStudioParagraphFrame({
        body: `
          <div class="clause-sxs">
            <div class="clause-sxs-col"><span class="clause-sxs-tag">Original</span><div class="sxs-empty">-</div></div>
            <div class="clause-sxs-col latest"><span class="clause-sxs-tag">Redline</span><div><span class="inline-ins">${inserted}</span></div></div>
          </div>
        `,
        attributes,
        classes: ["doc-sxs-paragraph"],
      });
    }
    return renderStudioParagraphFrame({
      body: inserted,
      attributes,
      classes: ["doc-clean-paragraph"],
    });
  }).join("");
}

function paragraphRedlinePlan(paragraph, redlines, manualRedline = null) {
  const replace = manualRedline?.action === REDLINE_REPLACE_PARAGRAPH
    ? manualRedline
    : redlines.find((edit) => edit.action === REDLINE_REPLACE_PARAGRAPH);
  const remove = manualRedline?.action === REDLINE_DELETE_PARAGRAPH
    ? manualRedline
    : redlines.find((edit) => edit.action === REDLINE_DELETE_PARAGRAPH);
  const inserts = redlines.filter(isInsertionRedline);
  const cleanText = remove
    ? ""
    : replace
      ? String(replace.replacement_text || "")
      : String(paragraph.text || "");
  return { replace, remove, inserts, cleanText };
}

function renderRedlineParagraphBody(paragraph, primaryRedline, visibleRedlines) {
  const editableParagraph = renderEditableParagraph(paragraph);
  if (primaryRedline?.action === REDLINE_REPLACE_PARAGRAPH || primaryRedline?.action === REDLINE_DELETE_PARAGRAPH) {
    const replacement = primaryRedline.action === REDLINE_REPLACE_PARAGRAPH && !primaryRedline.is_manual
      ? renderRedlineReplacement(primaryRedline, "span")
      : "";
    const insertionHtml = visibleRedlines.filter(isInsertionRedline).map(renderParagraphInsertion).join("");
    return `<div class="paragraph-redline-preview" data-redline-preview contenteditable="false">${renderInlineRedline(paragraph, primaryRedline)}</div><div class="paragraph-source-editor">${editableParagraph}</div><div class="paragraph-redline-note" data-redline-note contenteditable="false"><span class="redline-label" data-redline-label>${escapeHtml(redlineActionLabel(primaryRedline))}</span>${replacement}</div>${insertionHtml}`;
  }
  const redlineHtml = visibleRedlines.length ? renderParagraphRedlines(visibleRedlines) : "";
  return `<div class="paragraph-redline-preview" data-redline-preview contenteditable="false" hidden></div>${editableParagraph}${redlineHtml}`;
}

function syncRenderedManualRedline(container, { paragraph, manualRedline, backendRedline, hasBackendRedline }) {
  container.classList.toggle("manual-redline", Boolean(manualRedline));
  container.classList.toggle("has-redline", Boolean(manualRedline) || hasBackendRedline);

  const preview = container.querySelector("[data-redline-preview]");
  if (!preview) return;

  const previewRedline = manualRedline || backendRedline;
  preview.hidden = !previewRedline;
  preview.innerHTML = previewRedline ? renderInlineRedline(paragraph, previewRedline) : "";

  const label = container.querySelector("[data-redline-label]");
  if (label) {
    label.textContent = redlineActionLabel(manualRedline || backendRedline || {});
  }

  const backendReplacement = container.querySelector("[data-redline-replacement]");
  if (backendReplacement) {
    backendReplacement.hidden = Boolean(manualRedline);
  }
}

function renderSideBySideRedline(paragraph, plan) {
  if (plan.replace) return renderInlineRedline(paragraph, plan.replace);
  if (plan.remove) return `<span class="inline-del">${escapeHtml(String(paragraph.text || ""))}</span>`;
  return escapeHtml(String(paragraph.text || ""));
}

function manualParagraphRedline(paragraph, originalParagraphs = []) {
  const original = originalParagraphText(paragraph, originalParagraphs);
  const current = String(paragraph.text || "");
  if (current === original) return null;
  const action = current.trim() ? REDLINE_REPLACE_PARAGRAPH : REDLINE_DELETE_PARAGRAPH;
  return {
    action,
    action_label: current.trim() ? "Your edit" : "Delete paragraph",
    is_manual: true,
    original_text: original,
    paragraph_id: paragraph.id,
    paragraph_index: paragraph.index,
    replacement_text: current,
  };
}

function originalParagraphText(paragraph, originalParagraphs = []) {
  const original = originalParagraphs.find((item) => item.id === paragraph.id);
  return original ? String(original.text || "") : String(paragraph.text || "");
}

function renderInlineRedline(paragraph, edit) {
  const original = String(edit.original_text ?? paragraph.text ?? "");
  if (edit.action === REDLINE_DELETE_PARAGRAPH) {
    return `<span class="inline-del">${escapeHtml(original)}</span>`;
  }
  return renderInlineDiff(original, String(edit.replacement_text || ""));
}

function renderEditableParagraph(paragraph, extraClasses = []) {
  const classes = joinClasses("paragraph-editable", extraClasses);
  return `<div class="${classes}" contenteditable="plaintext-only" spellcheck="true" role="textbox" aria-multiline="true" data-editable-paragraph-id="${escapeHtml(paragraph.id)}" aria-label="Edit paragraph ${escapeHtml(paragraph.index || "")}">${escapeHtml(String(paragraph.text || ""))}</div>`;
}

function renderParagraphRedlines(edits) {
  if (edits.every(isInsertionRedline)) return edits.map(renderParagraphInsertion).join("");
  return renderParagraphRedline(edits[0]);
}

function renderParagraphRedline(edit) {
  if (isInsertionRedline(edit)) return renderParagraphInsertion(edit);
  return `<div class="paragraph-redline-note" data-redline-note contenteditable="false"><span class="redline-label" data-redline-label>${escapeHtml(redlineActionLabel(edit))}</span>${renderRedlineReplacement(edit, "span")}</div>`;
}

function renderParagraphInsertion(edit) {
  return `<div class="paragraph-insertion" contenteditable="false"><span class="redline-label">${escapeHtml(redlineActionLabel(edit))}</span><span class="redline-insertion">${escapeHtml(edit.insert_text || edit.replacement_text || "")}</span></div>`;
}

function redlineActionLabel(edit) {
  if (edit.action === REDLINE_DELETE_PARAGRAPH) return edit.action_label || "Remove paragraph";
  if (edit.action === REDLINE_INSERT_AFTER_PARAGRAPH) return edit.action_label || "Insert after paragraph";
  if (edit.action === REDLINE_REPLACE_PARAGRAPH) return edit.action_label || "Replace paragraph";
  return edit.action_label || "Proposed edit";
}

function renderRedlineReplacement(edit, tagName) {
  if (edit.action === REDLINE_DELETE_PARAGRAPH) {
    return `<${tagName} class="redline-removal">Remove this paragraph.</${tagName}>`;
  }
  if (edit.action === REDLINE_INSERT_AFTER_PARAGRAPH) {
    return `<${tagName} class="redline-insertion">${escapeHtml(edit.insert_text || edit.replacement_text || "")}</${tagName}>`;
  }
  return `<${tagName} class="redline-replacement" data-redline-replacement>${escapeHtml(edit.replacement_text || "")}</${tagName}>`;
}

function isInsertionRedline(edit) {
  return edit?.action === REDLINE_INSERT_AFTER_PARAGRAPH;
}
