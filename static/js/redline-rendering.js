function renderReviewDocument({
  clauses,
  originalParagraphs,
  paragraphs,
  comments = [],
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
      comments,
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
  const primaryRedline = manualRedline || selectedRedline || primaryBackendRedline(redlines, redlineClauses) || null;
  const primaryClause = (
    selectedClause
    || linkedClauses.find((clause) => clause.id === primaryRedline?.clause_id)
    || linkedClauses.find((clause) => clauseStatus(clause).requiresAttention)
    || linkedClauses[0]
  );
  const visibleRedlines = visibleParagraphRedlines(redlines, manualRedline, selectedRedline, primaryRedline);

  return {
    commentCount: paragraphCommentCount(paragraph.id, context.comments),
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

function primaryBackendRedline(redlines, redlineClauses) {
  const prohibitedDelete = redlines.find((edit) => {
    const clause = redlineClauses.find((candidate) => candidate.id === edit.clause_id);
    return edit.action === REDLINE_DELETE_PARAGRAPH && isFailedProhibitedClause(clause);
  });
  return prohibitedDelete || redlines.find((edit) => !isInsertionRedline(edit)) || redlines[0];
}

function renderDocumentParagraph(model) {
  if (model.viewMode === VIEW_MODE_CLEAN) return renderCleanDocumentParagraph(model);
  if (model.viewMode === VIEW_MODE_SIDE_BY_SIDE) return renderSideBySideDocumentParagraph(model);
  return renderRedlineDocumentParagraph(model);
}

function renderCleanDocumentParagraph(model) {
  let html = "";
  // When the clean text is the unedited paragraph text, render its run-level
  // formatting; for a replacement the clean text is the proposed string (no
  // run breakdown), so fall back to the plain escaped text.
  const cleanBody = model.plan.remove
    ? ""
    : model.plan.cleanText === String(model.paragraph.text || "")
      ? renderParagraphRichText(model.paragraph)
      : escapeHtml(model.plan.cleanText);
  html += renderParagraphFrame(model, {
    body: cleanBody,
    classes: ["doc-clean-paragraph", model.plan.remove ? "doc-clean-removed-anchor" : ""],
  });
  return html + renderInsertedParagraphs(model.plan.inserts, VIEW_MODE_CLEAN);
}

function renderSideBySideDocumentParagraph(model) {
  const sideBySide = sideBySideParagraphColumns(model.paragraph, model.plan);
  const body = `
    <div class="clause-sxs">
      <div class="${sideBySide.originalClass}"><span class="clause-sxs-tag">Original</span><div>${sideBySide.original}</div></div>
      <div class="${sideBySide.latestClass}"><span class="clause-sxs-tag">Proposed</span><div>${sideBySide.latest}</div></div>
    </div>
  `;
  return renderParagraphFrame(model, {
    body,
    classes: ["doc-sxs-paragraph"],
  }) + renderInsertedParagraphs(model.plan.inserts, VIEW_MODE_SIDE_BY_SIDE);
}

function renderRedlineDocumentParagraph(model) {
  const status = model.primaryClause ? clauseStatus(model.primaryClause) : null;
  const prohibited = model.linkedClauses.some(isFailedProhibitedClause);
  return renderParagraphFrame(model, {
    body: renderRedlineParagraphBody(model.paragraph, model.primaryRedline, model.visibleRedlines),
    classes: [
      model.linkedClauses.length ? "has-clause" : "",
      model.redlines.length || model.manualRedline ? "has-redline" : "",
      model.manualRedline ? "manual-redline" : "",
      model.primaryRedline?.action === REDLINE_DELETE_PARAGRAPH ? "redline-delete" : "",
      model.primaryRedline?.action === REDLINE_INSERT_AFTER_PARAGRAPH ? "redline-insert" : "",
      prohibited ? "prohibited" : "",
      status?.needsReview ? "review" : "",
      status?.fails ? "verify" : "",
      status && !status.requiresAttention ? "match" : "",
    ],
    // WCAG 1.4.1: the paragraph verdict is otherwise conveyed by background
    // colour alone, so emit a text+icon badge so the verdict is not colour-only.
    badge: paragraphVerdictBadge(status, prohibited),
  });
}

// Text+icon verdict badge for a flagged document paragraph. Returns "" when the
// paragraph carries no clause verdict, so unflagged paragraphs are unchanged.
function paragraphVerdictBadge(status, prohibited = false) {
  if (!status) return "";
  let tone = "";
  let label = "";
  if (prohibited || status.fails) {
    tone = "verify";
    label = prohibited ? "Prohibited" : "Fail";
  } else if (status.needsReview) {
    tone = "review";
    label = "Review";
  } else if (!status.requiresAttention) {
    tone = "match";
    label = "Pass";
  } else {
    return "";
  }
  return `
    <span class="paragraph-verdict-badge ${tone}" contenteditable="false" aria-hidden="false">
      ${paragraphVerdictIcon(tone)}
      <span class="paragraph-verdict-badge-label">${escapeHtml(label)}</span>
    </span>
  `;
}

function paragraphVerdictIcon(tone) {
  if (tone === "match") {
    return '<svg class="paragraph-verdict-badge-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m5 13 4 4L19 7"/></svg>';
  }
  if (tone === "review") {
    return '<svg class="paragraph-verdict-badge-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>';
  }
  // verify / fail
  return '<svg class="paragraph-verdict-badge-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M18 6 6 18M6 6l12 12"/></svg>';
}

function isFailedProhibitedClause(clause) {
  if (!clause || !clauseStatus(clause).fails) return false;
  // Native prohibited clauses are tagged type === "prohibited". A dynamic
  // Playbook clause (engine === "dynamic") that was migrated off a native
  // check may arrive without that tag, so recognize it from the fields it does
  // carry: a delete_paragraph fallback redline action marks it as prohibited
  // (its remedy is to remove the paragraph, never replace it).
  if (clause.type === "prohibited") return true;
  return clauseIsDynamic(clause)
    && String(clause.fallback?.redline_action || "").trim() === REDLINE_DELETE_PARAGRAPH;
}

function renderParagraphFrame(model, { body, classes = [], badge = "" }) {
  const structureAttributes = paragraphStructureAttributes(model.paragraph);
  return renderStudioParagraphFrame({
    badge,
    body,
    classes: [...classes, ...paragraphStructureClasses(model.paragraph)],
    clauseIds: model.ids,
    commentCount: model.commentCount,
    paragraphId: model.paragraph.id,
    selected: model.selected,
    attributes: structureAttributes,
  });
}

function renderStudioParagraphFrame({ body, classes = [], clauseIds = "", commentCount = 0, paragraphId = "", selected = false, attributes = "", badge = "" }) {
  const frameAttributes = [];
  if (paragraphId) frameAttributes.push(`data-paragraph-id="${escapeHtml(paragraphId)}"`);
  if (clauseIds) frameAttributes.push(`data-clause-ids="${escapeHtml(clauseIds)}"`);
  if (attributes) frameAttributes.push(attributes);
  const commentTools = paragraphId ? renderParagraphCommentTools(paragraphId, commentCount).trim() : "";
  const verdictBadge = badge ? badge.trim() : "";
  return `<div class="${joinClasses("studio-doc-paragraph", classes, selected ? "selected" : "", commentCount ? "has-comments" : "")}"${frameAttributes.length ? ` ${frameAttributes.join(" ")}` : ""}>${commentTools}${verdictBadge}${body}</div>`;
}

function renderParagraphCommentTools(paragraphId, commentCount) {
  const count = Number(commentCount || 0);
  return `
    <div class="paragraph-comment-tools" contenteditable="false">
      <button type="button" class="paragraph-comment-add" data-add-selection-comment-id="${escapeHtml(paragraphId)}" aria-label="Add comment" title="Add comment"><svg class="comment-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5Z"/></svg></button>
      ${count ? `<span class="paragraph-comment-count"><svg class="comment-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5Z"/></svg>${count}</span>` : ""}
    </div>
  `;
}

function paragraphCommentCount(paragraphId, comments) {
  if (!Array.isArray(comments)) return 0;
  return comments.filter((comment) => comment?.paragraph_id === paragraphId).length;
}

function renderInsertedParagraphs(inserts, viewMode) {
  return inserts.map((edit) => {
    const inserted = escapeHtml(String(edit.insert_text || edit.replacement_text || ""));
    const attributes = `data-redline-edit-id="${escapeHtml(edit.id || "")}"`;
    if (viewMode === VIEW_MODE_SIDE_BY_SIDE) {
      return renderStudioParagraphFrame({
        body: `
          <div class="clause-sxs">
            <div class="clause-sxs-col original empty"><span class="clause-sxs-tag">Original</span><div class="sxs-empty">No source paragraph</div></div>
            <div class="clause-sxs-col latest inserted"><span class="clause-sxs-tag">Proposed</span><div><span class="inline-ins">${inserted}</span></div></div>
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
    const redlineNote = replacement
      ? `<div class="paragraph-redline-note" data-redline-note contenteditable="false">${replacement}</div>`
      : "";
    return `<div class="paragraph-redline-preview" data-redline-preview contenteditable="false">${renderInlineRedline(paragraph, primaryRedline)}</div><div class="paragraph-source-editor">${editableParagraph}</div>${redlineNote}${insertionHtml}`;
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

  const backendReplacement = container.querySelector("[data-redline-replacement]");
  if (backendReplacement) {
    backendReplacement.hidden = Boolean(manualRedline);
  }
}

function sideBySideParagraphColumns(paragraph, plan) {
  if (plan.replace) {
    const original = String(plan.replace.original_text ?? paragraph.text ?? "");
    const replacement = String(plan.replace.replacement_text || "");
    return {
      original: renderSideBySideDiffColumn(original, replacement, "original", plan.replace),
      originalClass: "clause-sxs-col original removed",
      latest: renderSideBySideDiffColumn(original, replacement, "latest", plan.replace),
      latestClass: "clause-sxs-col latest inserted",
    };
  }
  if (plan.remove) {
    const original = String(plan.remove.original_text ?? paragraph.text ?? "");
    return {
      original: `<span class="inline-del">${escapeHtml(original)}</span>`,
      originalClass: "clause-sxs-col original removed",
      latest: '<span class="sxs-empty">Removed in proposed text</span>',
      latestClass: "clause-sxs-col latest empty",
    };
  }
  const richText = renderParagraphRichText(paragraph);
  return {
    original: richText,
    originalClass: "clause-sxs-col original",
    latest: richText,
    latestClass: "clause-sxs-col latest",
  };
}

function renderSideBySideDiffColumn(original, replacement, side, edit = null) {
  const operations = redlineDiffOperations(edit, original, replacement);
  const visibleOperations = operations.filter((operation) => (
    side === "original" ? operation.type !== "insert" : operation.type !== "delete"
  ));
  return renderSideBySideOperations(visibleOperations, side);
}

function renderSideBySideOperations(operations, side) {
  let previousToken = "";
  return operations
    .map((operation) => {
      const prefix = needsInlineSpace(previousToken, operation.token) ? " " : "";
      previousToken = operation.token;
      const className = operation.type === "delete"
        ? "inline-del"
        : operation.type === "insert"
          ? "inline-ins"
          : "";
      const token = `${prefix}${operation.token}`;
      if (!className && side === "latest") return escapeHtml(token);
      return renderInlineToken(token, className);
    })
    .join("") || '<span class="sxs-empty">No text</span>';
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
  return renderDiffOperations(redlineDiffOperations(edit, original, String(edit.replacement_text || "")));
}

function redlineDiffOperations(edit, original, replacement) {
  if (Array.isArray(edit?.inline_diff_operations) && edit.inline_diff_operations.length) {
    return edit.inline_diff_operations;
  }
  return fullReplacementOperations(original, replacement);
}

function renderEditableParagraph(paragraph, extraClasses = []) {
  const classes = joinClasses("paragraph-editable", extraClasses);
  return `<div class="${classes}" contenteditable="plaintext-only" spellcheck="true" role="textbox" aria-multiline="true" data-editable-paragraph-id="${escapeHtml(paragraph.id)}" aria-label="Edit paragraph ${escapeHtml(paragraph.index || "")}">${renderParagraphRichText(paragraph)}</div>`;
}

// Renders the paragraph's text with run-level bold/italic/underline when the
// extractor captured a `runs` breakdown, otherwise the plain escaped text. The
// run markup is display-only: editing reads innerText, so the editable identity
// and the round-trip to `paragraph.text` are unchanged.
function renderParagraphRichText(paragraph) {
  const text = String(paragraph?.text || "");
  const runs = Array.isArray(paragraph?.runs) ? paragraph.runs : null;
  if (!runs || !runs.length) return escapeHtml(text);
  if (runs.map((run) => String(run?.text || "")).join("") !== text) {
    // Defensive: never let a drifted run breakdown change what the editable body
    // shows. Fall back to the authoritative flat text.
    return escapeHtml(text);
  }
  return runs.map(renderFormattedRun).join("");
}

function renderFormattedRun(run) {
  let html = escapeHtml(String(run?.text || ""));
  if (run?.bold) html = `<strong>${html}</strong>`;
  if (run?.italic) html = `<em>${html}</em>`;
  if (run?.underline) html = `<u>${html}</u>`;
  return html;
}

// Structural CSS classes + data hooks for a paragraph frame, driven by metadata
// the DOCX extractor already captures (heading level, list numbering, table
// context). These are additive: the paragraph keeps its id and clause/redline
// data-hooks; only its typography/indentation changes.
function paragraphStructureClasses(paragraph) {
  const classes = [];
  const headingLevel = Number(paragraph?.heading_level);
  if (Number.isFinite(headingLevel) && headingLevel >= 1) {
    classes.push("doc-heading", `doc-heading-${Math.min(Math.max(Math.floor(headingLevel), 1), 6)}`);
  }
  const numbering = paragraph?.numbering;
  if (numbering && typeof numbering === "object") {
    classes.push("doc-list");
    const level = Number(numbering.level);
    if (Number.isFinite(level) && level > 0) {
      classes.push(`doc-list-level-${Math.min(Math.floor(level), 6)}`);
    }
  }
  if (paragraph?.table && typeof paragraph.table === "object") {
    classes.push("doc-table-cell");
  }
  return classes;
}

function paragraphStructureAttributes(paragraph) {
  const attributes = [];
  const label = String(paragraph?.structure_label || paragraph?.numbering?.label || "").trim();
  if (label) attributes.push(`data-structure-label="${escapeHtml(label)}"`);
  const table = paragraph?.table;
  if (table && typeof table === "object") {
    attributes.push(`data-table-index="${escapeHtml(table.table_index ?? "")}"`);
    attributes.push(`data-table-row="${escapeHtml(table.row_index ?? "")}"`);
    attributes.push(`data-table-cell="${escapeHtml(table.cell_index ?? "")}"`);
  }
  return attributes.join(" ");
}

function renderParagraphRedlines(edits) {
  if (edits.every(isInsertionRedline)) return edits.map(renderParagraphInsertion).join("");
  return renderParagraphRedline(edits[0]);
}

function renderParagraphRedline(edit) {
  if (isInsertionRedline(edit)) return renderParagraphInsertion(edit);
  return `<div class="paragraph-redline-note" data-redline-note contenteditable="false">${renderRedlineReplacement(edit, "span")}</div>`;
}

function renderParagraphInsertion(edit) {
  return `<div class="paragraph-insertion" data-redline-edit-id="${escapeHtml(edit.id || "")}" contenteditable="false"><span class="redline-insertion">${escapeHtml(edit.insert_text || edit.replacement_text || "")}</span></div>`;
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
