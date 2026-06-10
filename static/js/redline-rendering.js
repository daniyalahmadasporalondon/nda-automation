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

  const renderOne = (paragraph) => renderDocumentParagraph(paragraphViewModel(paragraph, {
    clauses,
    clausesByParagraphId,
    comments,
    originalParagraphs,
    redlinesByParagraphId,
    selectedClauseId,
    viewMode,
  }));
  return renderReviewParagraphsWithTables(paragraphs, renderOne);
}

function redlineEditContract() {
  return window.RedlineEditContract || null;
}

function manualViewerEditClauseId() {
  return redlineEditContract()?.MANUAL_VIEWER_EDIT_CLAUSE_ID || "manual_viewer_edit";
}

function redlineFormatParagraphAction() {
  return redlineEditContract()?.REDLINE_ACTION_FORMAT_PARAGRAPH || "format_paragraph";
}

// Walks the flat paragraph list and wraps each run of consecutive table-cell
// paragraphs (same table_index) into a presentational CSS grid so multi-cell
// tables (e.g. signature blocks) render as side-by-side columns instead of a
// flat vertical stack. Non-table paragraphs are emitted untouched. Every cell
// paragraph is still rendered by `renderOne` -- it keeps its own
// .studio-doc-paragraph frame, id and clause/redline/comment data-hooks (the
// CSS comment at styles.css ~3935 explains why tables were left flat); we only
// add a grid wrapper around the contiguous run, never nest or destroy frames.
function renderReviewParagraphsWithTables(paragraphs, renderOne) {
  const out = [];
  let i = 0;
  while (i < paragraphs.length) {
    const table = reviewTableMeta(paragraphs[i]);
    if (!table) {
      out.push(renderOne(paragraphs[i]));
      i += 1;
      continue;
    }
    // Consume the whole table (all contiguous paragraphs sharing table_index).
    const tableIndex = table.table_index;
    let j = i;
    while (j < paragraphs.length) {
      const meta = reviewTableMeta(paragraphs[j]);
      if (!meta || meta.table_index !== tableIndex) break;
      j += 1;
    }
    out.push(renderReviewTable(paragraphs.slice(i, j), renderOne));
    i = j;
  }
  return out.join("");
}

function reviewTableMeta(paragraph) {
  const table = paragraph && paragraph.table;
  return table && typeof table === "object" ? table : null;
}

// Renders a contiguous block of table-cell paragraphs as a CSS grid of cells.
// Cells are keyed by (row_index, cell_index) and ordered by first appearance,
// so a single-row two-cell signature table becomes two side-by-side columns and
// multi-row tables stack their rows. Each cell wraps the already-rendered
// .studio-doc-paragraph frames for that (row, cell) -- frames/ids/hooks intact.
function renderReviewTable(cellParagraphs, renderOne) {
  const cells = [];
  const byKey = new Map();
  cellParagraphs.forEach((paragraph) => {
    const meta = reviewTableMeta(paragraph) || {};
    const key = `${meta.row_index ?? 0}:${meta.cell_index ?? 0}`;
    let cell = byKey.get(key);
    if (!cell) {
      cell = { row: Number(meta.row_index) || 0, col: Number(meta.cell_index) || 0, html: [] };
      byKey.set(key, cell);
      cells.push(cell);
    }
    cell.html.push(renderOne(paragraph));
  });
  // Column count is the distinct cell_index count (max index + 1), falling back
  // to the cell count for a degenerate single-row table with no cell indices.
  const columnCount = cells.reduce((max, cell) => Math.max(max, cell.col + 1), 0) || cells.length;
  const inner = cells
    .map((cell) => `<div class="studio-doc-table-cell">${cell.html.join("")}</div>`)
    .join("");
  return `<div class="studio-doc-table" style="--studio-table-cols:${Math.max(columnCount, 1)}">${inner}</div>`;
}

function paragraphViewModel(paragraph, context) {
  const redlines = context.redlinesByParagraphId.get(paragraph.id) || [];
  const redlineClauses = redlines
    .map((edit) => context.clauses.find((clause) => clause.id === edit.clause_id))
    .filter(Boolean);
  // The document title (Word "Title" style) is the document's name, never clause
  // content. Some stored reviews list it in a clause's matched_paragraph_ids (the
  // AI cited it as on-topic evidence), which would paint the title green. Suppress
  // any clause linkage on a title paragraph so it is never highlighted as a clause.
  const linkedClauses = paragraphIsDocumentTitle(paragraph)
    ? []
    : mergeClauses(context.clausesByParagraphId.get(paragraph.id) || [], redlineClauses);
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
    body: applyParagraphFormatToBody(model, body),
    classes: [...classes, ...paragraphStructureClasses(model.paragraph)],
    clauseIds: model.ids,
    commentCount: model.commentCount,
    paragraphId: model.paragraph.id,
    selected: model.selected,
    attributes: structureAttributes,
  });
}

// Applies the paragraph's own alignment/font to the rendered body and, when a
// `format_paragraph` redline is pending for this paragraph, appends a small
// "Formatted: …" tracked-change note (reusing the .paragraph-redline-note
// pattern). When the paragraph has no formatting and no format redline, the body
// is returned unchanged so existing paragraphs render exactly as before.
function applyParagraphFormatToBody(model, body) {
  const styleAttribute = paragraphFormatStyleAttribute(model.paragraph);
  const formatRedline = isFormatParagraphRedline(model.manualRedline) ? model.manualRedline : null;
  if (!styleAttribute && !formatRedline) return body;
  const note = formatRedline ? renderParagraphFormatNote(formatRedline) : "";
  if (!styleAttribute) return `${body}${note}`;
  return `<div class="studio-doc-paragraph-body"${styleAttribute}>${body}</div>${note}`;
}

function isFormatParagraphRedline(edit) {
  return edit?.action === redlineFormatParagraphAction() && Array.isArray(edit.format_ops) && edit.format_ops.length > 0;
}

// "Formatted: …" tracked-change note summarising the format_ops, so a
// formatting-only change reads as a tracked change alongside the paragraph.
function renderParagraphFormatNote(edit) {
  const summary = [...new Set(
    (edit.format_ops || []).map(formatOpSummary).filter(Boolean),
  )].join("; ");
  if (!summary) return "";
  return `<div class="paragraph-redline-note paragraph-format-note" data-redline-note contenteditable="false"><span class="redline-replacement">Formatted: ${escapeHtml(summary)}</span></div>`;
}

function formatOpSummary(op) {
  if (!op || typeof op !== "object") return "";
  if (op.scope === "run") return runOpSummary(op);
  const to = String(op.to || "").trim();
  if (op.property === "alignment") {
    return to ? `align ${to}` : "";
  }
  if (op.property === "font") {
    return to ? `font ${to}` : "";
  }
  if (op.property === "size") {
    return Number(op.to) > 0 ? `size ${Number(op.to)}` : "";
  }
  return "";
}

// Summary for an inline (run-scope) op, e.g. "Bold", "Italic",
// "Font: Arial (selection)". A bold/italic op that turns the property OFF reads
// "No bold" / "No italic"; clearing a font reads "Default font (selection)".
function runOpSummary(op) {
  if (op.property === "bold") return op.to ? "Bold" : "No bold";
  if (op.property === "italic") return op.to ? "Italic" : "No italic";
  if (op.property === "font") {
    const to = String(op.to || "").trim();
    return to ? `Font: ${to} (selection)` : "Default font (selection)";
  }
  if (op.property === "size") {
    return Number(op.to) > 0 ? `Size: ${Number(op.to)} (selection)` : "";
  }
  return "";
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
      ${count ? `<button type="button" class="paragraph-comment-count" data-edit-paragraph-comments-id="${escapeHtml(paragraphId)}" aria-label="View, edit or remove comment" title="View, edit or remove comment"><svg class="comment-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5Z"/></svg>${count}</button>` : ""}
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
  const baseline = originalParagraphFor(paragraph, originalParagraphs);
  const original = baseline ? String(baseline.text || "") : String(paragraph.text || "");
  const current = String(paragraph.text || "");
  // Text-only path (replace / delete) is unchanged.
  if (current !== original) {
    const action = current.trim() ? REDLINE_REPLACE_PARAGRAPH : REDLINE_DELETE_PARAGRAPH;
    return {
      action,
      action_label: current.trim() ? "Your edit" : "Delete paragraph",
      is_manual: true,
      // Clause-applied paragraph replacements (e.g. the governing-law picker) stay
      // whole-paragraph; free-form typing leaves this false so it word-diffs.
      whole_paragraph: paragraph?.clauseRedlineWholeParagraph === true,
      original_text: original,
      paragraph_id: paragraph.id,
      paragraph_index: paragraph.index,
      replacement_text: current,
    };
  }
  // Text byte-identical to baseline: a paragraph-level alignment/font change is a
  // tracked formatting redline. (Formatting WITH a text change is a later
  // milestone, so we only reach here when text is unchanged.)
  return paragraphFormatRedline(paragraph, baseline);
}

// Builds a `format_paragraph` redline when the paragraph's alignment and/or font
// differ from the baseline, otherwise null. `original_text`/`replacement_text`
// are both the current (unchanged) text per the redline contract.
function paragraphFormatRedline(paragraph, baseline) {
  if (!baseline) return null;
  const formatOps = paragraphFormatOps(paragraph, baseline);
  if (!formatOps.length) return null;
  const current = String(paragraph.text || "");
  const redline = {
    id: `manual-${paragraph.id}-fmt`,
    clause_id: manualViewerEditClauseId(),
    status: "proposed",
    action: redlineFormatParagraphAction(),
    action_label: "Format paragraph",
    is_manual: true,
    paragraph_id: paragraph.id,
    paragraph_index: paragraph.index,
    original_text: current,
    replacement_text: current,
    format_ops: formatOps,
  };
  if (baseline.source_index !== undefined || paragraph.source_index !== undefined) {
    redline.source_index = baseline.source_index !== undefined ? baseline.source_index : paragraph.source_index;
  }
  if (baseline.source_part !== undefined) {
    redline.source_part = baseline.source_part;
  } else if (paragraph.source_part !== undefined) {
    redline.source_part = paragraph.source_part;
  }
  return redline;
}

// Diffs the two paragraph-level formatting properties (alignment, font) against
// the baseline, emitting one op per property that actually changed. Font values
// are Word font NAME strings (e.g. "Arial"), never a CSS stack. Run-scope ops
// (inline bold/italic/font over a selection) are appended afterwards.
function paragraphFormatOps(paragraph, baseline) {
  const ops = [];
  const fromAlignment = normalizeFormatValue(baseline.alignment);
  const toAlignment = normalizeFormatValue(paragraph.alignment);
  if (fromAlignment !== toAlignment) {
    ops.push({ scope: "paragraph", property: "alignment", from: fromAlignment, to: toAlignment });
  }
  const fromFont = normalizeFormatValue(baseline.font);
  const toFont = normalizeFormatValue(paragraph.font);
  if (fromFont !== toFont) {
    ops.push({ scope: "paragraph", property: "font", from: fromFont, to: toFont });
  }
  const fromSize = normalizeSizeValue(baseline.fontSize);
  const toSize = normalizeSizeValue(paragraph.fontSize);
  if (fromSize !== toSize) {
    ops.push({ scope: "paragraph", property: "size", from: fromSize, to: toSize });
  }
  return ops.concat(runFormatOps(paragraph, baseline));
}

// Derives run-scope ops by diffing CURRENT `paragraph.runs` against BASELINE
// `runs`. Both tile the SAME paragraph text (run formatting only ever changes
// when the text is byte-identical to baseline), so we build per-character
// property values for each side and emit one op per contiguous range where a
// property differs. Returns [] when runs are absent or identical.
function runFormatOps(paragraph, baseline) {
  const text = String(paragraph?.text || "");
  if (!text.length) return [];
  const current = runCharProperties(paragraph.runs, text);
  const original = runCharProperties(baseline?.runs, text);
  if (!current || !original) return [];
  const ops = [];
  ["bold", "italic", "font", "size"].forEach((property) => {
    let index = 0;
    while (index < text.length) {
      const from = original[property][index];
      const to = current[property][index];
      if (runPropEqual(from, to)) {
        index += 1;
        continue;
      }
      let end = index + 1;
      while (
        end < text.length
        && runPropEqual(original[property][end], from)
        && runPropEqual(current[property][end], to)
      ) {
        end += 1;
      }
      ops.push({
        scope: "run",
        property,
        start: index,
        end,
        from: runOpValue(property, from),
        to: runOpValue(property, to),
      });
      index = end;
    }
  });
  return ops;
}

// Per-character property arrays for a run list tiling `text`. bold/italic carry a
// boolean per char; font carries a font-name string ("" when none). Returns null
// when runs are absent or do not tile the text (so the diff is skipped).
function runCharProperties(runs, text) {
  if (!Array.isArray(runs) || !runs.length) {
    // Absent runs = the unformatted baseline: every char is plain.
    return runs === undefined || runs === null
      ? { bold: new Array(text.length).fill(false), italic: new Array(text.length).fill(false), font: new Array(text.length).fill(""), size: new Array(text.length).fill(0) }
      : null;
  }
  if (runs.map((run) => String(run?.text || "")).join("") !== text) return null;
  const bold = [];
  const italic = [];
  const font = [];
  const size = [];
  runs.forEach((run) => {
    const runText = String(run?.text || "");
    const isBold = Boolean(run?.bold);
    const isItalic = Boolean(run?.italic);
    const fontName = String(run?.font || "").trim();
    const pointSize = Number(run?.size) > 0 ? Number(run?.size) : 0;
    for (let i = 0; i < runText.length; i += 1) {
      bold.push(isBold);
      italic.push(isItalic);
      font.push(fontName);
      size.push(pointSize);
    }
  });
  return { bold, italic, font, size };
}

function runPropEqual(a, b) {
  if (typeof a === "boolean" || typeof b === "boolean") return Boolean(a) === Boolean(b);
  return String(a || "") === String(b || "");
}

// Contract value for an op: bold/italic -> boolean; font -> Word name string;
// size -> point number (0 when none).
function runOpValue(property, value) {
  if (property === "font") return String(value || "");
  if (property === "size") return Number(value) > 0 ? Number(value) : 0;
  return Boolean(value);
}

function normalizeFormatValue(value) {
  if (value === undefined || value === null) return null;
  const text = String(value).trim();
  return text || null;
}

// Paragraph-level point size as a number, 0 when unset (the op contract value).
function normalizeSizeValue(value) {
  const size = Math.round(Number(value));
  return Number.isFinite(size) && size > 0 ? size : 0;
}

function originalParagraphFor(paragraph, originalParagraphs = []) {
  return originalParagraphs.find((item) => item.id === paragraph.id) || null;
}

// True for the document title paragraph (Word "Title" paragraph style). Mirrors
// the backend's _is_document_title_paragraph so the title is never treated as
// clause evidence. Clause headings use Heading styles, not Title.
function paragraphIsDocumentTitle(paragraph) {
  return ["style_id", "style_name"].some(
    (key) => String(paragraph?.[key] || "").trim().toLowerCase() === "title",
  );
}

function originalParagraphText(paragraph, originalParagraphs = []) {
  const original = originalParagraphFor(paragraph, originalParagraphs);
  return original ? String(original.text || "") : String(paragraph.text || "");
}

function renderInlineRedline(paragraph, edit) {
  const original = String(edit.original_text ?? paragraph.text ?? "");
  if (edit.action === REDLINE_DELETE_PARAGRAPH) {
    return `<span class="inline-del">${escapeHtml(original)}</span>`;
  }
  const replacement = String(edit.replacement_text || "");
  // Backend/AI redlines keep their token-level ops; clause / whole-paragraph edits
  // replace the whole paragraph. A free-form manual edit diffs at the CHARACTER
  // level so only the changed letters are struck/inserted -- rendered verbatim,
  // because char tokens carry their own whitespace (renderDiffOperations would
  // re-insert inter-token spaces and corrupt them).
  const previewMode = redlineEditContract()?.redlineInlinePreviewMode(edit)
    || (Array.isArray(edit?.inline_diff_operations) && edit.inline_diff_operations.length
      ? "operations"
      : isFreeformManualReplacement(edit)
        ? "character_diff"
        : "whole_paragraph");
  if (previewMode === "operations") {
    return renderDiffOperations(edit.inline_diff_operations);
  }
  if (previewMode === "character_diff") {
    return renderVerbatimDiffOperations(charDiffOperations(original, replacement));
  }
  return renderDiffOperations(fullReplacementOperations(original, replacement));
}

function redlineDiffOperations(edit, original, replacement) {
  const previewMode = redlineEditContract()?.redlineOperationPreviewMode(edit)
    || (Array.isArray(edit?.inline_diff_operations) && edit.inline_diff_operations.length
      ? "operations"
      : isFreeformManualReplacement(edit)
        ? "word_diff"
        : "whole_paragraph");
  if (previewMode === "operations") {
    return edit.inline_diff_operations;
  }
  // Clause-level redlines (and the governing-law picker) stay whole-paragraph; a
  // free-form manual edit diffs at the word level so only the changed words redline.
  if (previewMode === "word_diff") {
    return wordDiffOperations(original, replacement);
  }
  return fullReplacementOperations(original, replacement);
}

function isFreeformManualReplacement(edit) {
  return edit?.action === REDLINE_REPLACE_PARAGRAPH
    && !edit?.whole_paragraph
    && (edit?.clause_id === manualViewerEditClauseId() || edit?.is_manual === true);
}

// Word-level diff (LCS) for free-form manual edits: emits equal/delete/insert word
// tokens so only the changed words are struck/inserted, not the whole paragraph.
function wordDiffOperations(original, replacement) {
  const a = String(original || "").split(/\s+/).filter(Boolean);
  const b = String(replacement || "").split(/\s+/).filter(Boolean);
  const n = a.length;
  const m = b.length;
  if (!n && !m) return [];
  // Guard against pathological cost on very long paragraphs.
  if (n * m > 1000000) return fullReplacementOperations(original, replacement);
  const lcs = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const operations = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      operations.push({ type: "equal", token: a[i] });
      i += 1;
      j += 1;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      operations.push({ type: "delete", token: a[i] });
      i += 1;
    } else {
      operations.push({ type: "insert", token: b[j] });
      j += 1;
    }
  }
  while (i < n) {
    operations.push({ type: "delete", token: a[i] });
    i += 1;
  }
  while (j < m) {
    operations.push({ type: "insert", token: b[j] });
    j += 1;
  }
  return operations;
}

// Character-level diff (LCS) for free-form manual edits: a small change (e.g.
// deleting one word inside a quoted phrase) redlines only the changed letters,
// not the whole word/phrase. Consecutive same-type characters are merged into
// runs whose tokens carry their own whitespace, so they MUST be rendered with
// renderVerbatimDiffOperations (renderDiffOperations re-inserts inter-token
// spaces, which is correct for word tokens but corrupts character runs).
function charDiffOperations(original, replacement) {
  const a = [...String(original || "")];
  const b = [...String(replacement || "")];
  const n = a.length;
  const m = b.length;
  if (!n && !m) return [];
  // Char-level LCS is O(n*m); fall back to a whole-paragraph replacement on very
  // long paragraphs so a keystroke never stalls the live preview.
  if (n * m > 1000000) return fullReplacementOperations(original, replacement);
  const lcs = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const operations = [];
  const pushChar = (type, token) => {
    const last = operations[operations.length - 1];
    if (last && last.type === type) last.token += token;
    else operations.push({ type, token });
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { pushChar("equal", a[i]); i += 1; j += 1; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) { pushChar("delete", a[i]); i += 1; }
    else { pushChar("insert", b[j]); j += 1; }
  }
  while (i < n) { pushChar("delete", a[i]); i += 1; }
  while (j < m) { pushChar("insert", b[j]); j += 1; }
  return operations;
}

// Renders diff ops verbatim, wrapping deletes/inserts in the redline spans. The
// tokens already carry their own whitespace (as charDiffOperations produces), so
// unlike renderDiffOperations it never inserts inter-token spaces.
function renderVerbatimDiffOperations(operations) {
  return operations
    .map((operation) => {
      const className = operation.type === "delete"
        ? "inline-del"
        : operation.type === "insert"
          ? "inline-ins"
          : "";
      return renderInlineToken(operation.token, className);
    })
    .join("");
}

// Word font NAME -> CSS family stack, for on-screen rendering only. The redline
// op carries the bare Word name (e.g. "Arial"); this maps it to a display stack.
// A name with no entry falls back to itself so an unknown font still renders.
const FORMAT_FONT_CSS_STACKS = {
  Calibri: "Calibri, sans-serif",
  Arial: "Arial, Helvetica, sans-serif",
  "Times New Roman": "'Times New Roman', Times, serif",
  Georgia: "Georgia, serif",
  Cambria: "Cambria, Georgia, serif",
  Garamond: "Garamond, 'Times New Roman', serif",
  Verdana: "Verdana, Geneva, sans-serif",
  Tahoma: "Tahoma, Geneva, sans-serif",
  "Trebuchet MS": "'Trebuchet MS', Helvetica, sans-serif",
  "Courier New": "'Courier New', Courier, monospace",
};

function fontCssStackForName(name) {
  const fontName = String(name || "").trim();
  if (!fontName) return "";
  if (Object.prototype.hasOwnProperty.call(FORMAT_FONT_CSS_STACKS, fontName)) {
    return FORMAT_FONT_CSS_STACKS[fontName];
  }
  // Unknown name: render it as a single-family stack so the choice still shows.
  return /\s/.test(fontName) ? `'${fontName}'` : fontName;
}

// Word highlight color NAME -> CSS color, for on-screen rendering. The run
// carries the bare Word highlight name (the w:highlight values); this maps it to
// a CSS color. A name with no entry falls back to itself so an unknown but
// CSS-understood name (e.g. "orange") still renders.
const HIGHLIGHT_NAME_CSS = {
  yellow: "#ffff00",
  green: "#00ff00",
  cyan: "#00ffff",
  magenta: "#ff00ff",
  blue: "#0000ff",
  red: "#ff0000",
  darkYellow: "#808000",
  darkGreen: "#008000",
  darkCyan: "#008080",
  darkMagenta: "#800080",
  darkBlue: "#000080",
  darkRed: "#800000",
  lightGray: "#c0c0c0",
  darkGray: "#808080",
  black: "#000000",
  white: "#ffffff",
};

function highlightCssColor(name) {
  const raw = String(name || "").trim();
  if (!raw) return "";
  if (Object.prototype.hasOwnProperty.call(HIGHLIGHT_NAME_CSS, raw)) {
    return HIGHLIGHT_NAME_CSS[raw];
  }
  // Case-insensitive retry so "DarkYellow"/"darkyellow" still map.
  const lower = raw.toLowerCase();
  const key = Object.keys(HIGHLIGHT_NAME_CSS).find((k) => k.toLowerCase() === lower);
  if (key) return HIGHLIGHT_NAME_CSS[key];
  // Unknown name: hand the raw value to CSS, which understands many color names.
  return raw;
}

const PARAGRAPH_TEXT_ALIGNMENTS = new Set(["left", "center", "right", "justify"]);

// Inline `text-align` + `font-family` for a paragraph's own formatting. Returns
// "" when neither is set, so unformatted paragraphs render exactly as before.
function paragraphFormatStyle(paragraph) {
  const declarations = [];
  const alignment = String(paragraph?.alignment || "").trim().toLowerCase();
  if (PARAGRAPH_TEXT_ALIGNMENTS.has(alignment)) {
    declarations.push(`text-align:${alignment}`);
  }
  const fontStack = fontCssStackForName(paragraph?.font);
  if (fontStack) declarations.push(`font-family:${fontStack}`);
  const size = Number(paragraph?.fontSize);
  if (Number.isFinite(size) && size > 0) declarations.push(`font-size:${size}pt`);
  // Per-paragraph left indentation (points) so sub-clauses / indented text nest
  // exactly as the source. This is the SOURCE-absolute indent: when present it
  // must WIN over the level-class `.doc-list-level-N` padding rather than stack
  // on top of it (which would double-indent). The level-class lives on the outer
  // frame and this style on the inner body div, so they would otherwise compose;
  // paragraphHasIndentLeft() tags the frame so CSS can neutralise the class
  // padding. Only emitted when indent_left > 0, leaving unformatted paragraphs
  // byte-identical to before.
  const indentLeft = Number(paragraph?.indent_left);
  if (Number.isFinite(indentLeft) && indentLeft > 0) {
    declarations.push(`padding-left:${indentLeft}pt`);
  }
  return declarations.join(";");
}

// True when the paragraph carries a source-absolute left indent. Used to add a
// frame marker class so the `.doc-list-level-N` padding can be neutralised (the
// absolute indent wins) instead of stacking with it.
function paragraphHasIndentLeft(paragraph) {
  const indentLeft = Number(paragraph?.indent_left);
  return Number.isFinite(indentLeft) && indentLeft > 0;
}

function paragraphFormatStyleAttribute(paragraph) {
  const style = paragraphFormatStyle(paragraph);
  return style ? ` style="${escapeHtml(style)}"` : "";
}

function renderEditableParagraph(paragraph, extraClasses = []) {
  const classes = joinClasses("paragraph-editable", extraClasses);
  return `<div class="${classes}" contenteditable="plaintext-only" spellcheck="true" role="textbox" aria-multiline="true" data-editable-paragraph-id="${escapeHtml(paragraph.id)}" aria-label="Edit paragraph ${escapeHtml(paragraph.index || "")}">${renderParagraphRichText(paragraph)}</div>`;
}

// Renders the paragraph's text with run-level bold/italic/underline/font when the
// extractor (or an inline-format edit) produced a `runs` breakdown, otherwise the
// plain escaped text. Any run whose formatting DIFFERS from the baseline run-state
// is wrapped in a tracked-change span (.inline-ins, the existing redline inline
// class) so a manual inline format reads as a tracked change. The run markup is
// display-only: editing reads innerText, so the editable identity and the
// round-trip to `paragraph.text` are unchanged.
function renderParagraphRichText(paragraph) {
  const text = String(paragraph?.text || "");
  const runs = Array.isArray(paragraph?.runs) ? paragraph.runs : null;
  if (!runs || !runs.length) return escapeHtml(text);
  if (runs.map((run) => String(run?.text || "")).join("") !== text) {
    // Defensive: never let a drifted run breakdown change what the editable body
    // shows. Fall back to the authoritative flat text.
    return escapeHtml(text);
  }
  const baselineChars = baselineRunCharProperties(paragraph, text);
  let cursor = 0;
  return runs.map((run) => {
    const runText = String(run?.text || "");
    const changed = baselineChars
      ? runDiffersFromBaseline(run, baselineChars, cursor, cursor + runText.length)
      : false;
    cursor += runText.length;
    return renderFormattedRun(run, changed);
  }).join("");
}

function renderFormattedRun(run, changed = false) {
  let html = escapeHtml(String(run?.text || ""));
  if (run?.bold) html = `<strong>${html}</strong>`;
  if (run?.italic) html = `<em>${html}</em>`;
  if (run?.underline) html = `<u>${html}</u>`;
  // Document strikethrough is run formatting from the SOURCE, not a tracked
  // deletion. It MUST use a dedicated class (never .inline-del / any redline
  // class) so it is visually distinct from — and never confused with — a
  // redline. Inner-most so it composes with bold/italic/underline above.
  if (run?.strike) html = `<span class="doc-run-strike">${html}</span>`;
  // Super/subscript: a real sup/sub element so it lifts/drops and shrinks like
  // the source. Inner-most wrap, composing with the emphasis wrappers above.
  const vertAlign = String(run?.vertAlign || "").trim().toLowerCase();
  if (vertAlign === "superscript") html = `<sup>${html}</sup>`;
  else if (vertAlign === "subscript") html = `<sub>${html}</sub>`;
  const runStyle = inlineRunStyle(run);
  if (runStyle) html = `<span style="${escapeHtml(runStyle)}">${html}</span>`;
  // Wrap a run whose formatting differs from baseline in the tracked-change
  // inline class so the manual inline format surfaces as a redline.
  if (changed) html = `<span class="inline-ins redline-format-ins" data-redline-format-ins>${html}</span>`;
  return html;
}

// Inline `font-family` + `font-size` + run COLOR + HIGHLIGHT for a run's own
// formatting. Size is in POINTS (rendered at true point size; unsized runs
// inherit the document base). color/highlight are source run formatting that the
// extractor captures; they render as the run's text color / background swatch.
function inlineRunStyle(run) {
  const declarations = [];
  const fontStack = fontCssStackForName(run?.font);
  if (fontStack) declarations.push(`font-family:${fontStack}`);
  const size = Number(run?.size);
  if (Number.isFinite(size) && size > 0) declarations.push(`font-size:${size}pt`);
  const color = String(run?.color || "").trim();
  if (color) declarations.push(`color:${color}`);
  const highlight = highlightCssColor(run?.highlight);
  if (highlight) declarations.push(`background-color:${highlight}`);
  return declarations.join(";");
}

// Per-character baseline property arrays for `paragraph`, used to decide which
// runs differ from the original formatting. Resolves the baseline paragraph from
// the manual-redline baseline; returns null when no baseline / text drift so the
// caller renders without any tracked-format wrapping.
function baselineRunCharProperties(paragraph, text) {
  const baseline = baselineParagraphForRichText(paragraph);
  if (!baseline) return null;
  if (String(baseline.text || "") !== text) return null;
  return runCharProperties(baseline.runs, text);
}

function baselineParagraphForRichText(paragraph) {
  if (!paragraph || typeof manualRedlineBaselineParagraphs !== "function") return null;
  const baseline = manualRedlineBaselineParagraphs();
  if (!Array.isArray(baseline)) return null;
  return baseline.find((item) => item.id === paragraph.id) || null;
}

// True when any character of the run [start, end) carries bold/italic/font that
// differs from the baseline char-state at that index.
function runDiffersFromBaseline(run, baselineChars, start, end) {
  if (!baselineChars) return false;
  const isBold = Boolean(run?.bold);
  const isItalic = Boolean(run?.italic);
  const font = String(run?.font || "").trim();
  const size = Number(run?.size) > 0 ? Number(run?.size) : 0;
  for (let index = start; index < end; index += 1) {
    if (Boolean(baselineChars.bold[index]) !== isBold) return true;
    if (Boolean(baselineChars.italic[index]) !== isItalic) return true;
    if (String(baselineChars.font[index] || "") !== font) return true;
    if (Number((baselineChars.size && baselineChars.size[index]) || 0) !== size) return true;
  }
  return false;
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
  // A source-absolute left indent (rendered as padding-left on the body div by
  // paragraphFormatStyle) supersedes the level-class padding so the two never
  // stack into a double indent. Tag the frame so CSS can zero the class padding.
  if (paragraphHasIndentLeft(paragraph)) {
    classes.push("doc-indent-explicit");
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
  const inserted = redlineEditContract()?.redlineInsertedText(edit) || edit.insert_text || edit.replacement_text || "";
  return `<div class="paragraph-insertion" data-redline-edit-id="${escapeHtml(edit.id || "")}" contenteditable="false"><span class="redline-insertion">${escapeHtml(inserted)}</span></div>`;
}

function redlineActionLabel(edit) {
  if (redlineEditContract()) return redlineEditContract().redlineActionLabel(edit);
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
    const inserted = redlineEditContract()?.redlineInsertedText(edit) || edit.insert_text || edit.replacement_text || "";
    return `<${tagName} class="redline-insertion">${escapeHtml(inserted)}</${tagName}>`;
  }
  const replacement = redlineEditContract()?.redlineReplacementText(edit) || edit.replacement_text || "";
  return `<${tagName} class="redline-replacement" data-redline-replacement>${escapeHtml(replacement)}</${tagName}>`;
}

function isInsertionRedline(edit) {
  return redlineEditContract()?.isInsertionRedlineEdit(edit) ?? edit?.action === REDLINE_INSERT_AFTER_PARAGRAPH;
}
