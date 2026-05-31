function renderResult(result, reviewedText) {
  pendingReviewSendMatterId = null;
  state.latestReviewResult = result;
  state.reviewClauses = result.clauses || [];
  state.reviewParagraphs = result.paragraphs || [];
  state.reviewOriginalParagraphs = state.reviewParagraphs.map((paragraph) => ({
    id: paragraph.id,
    text: String(paragraph.text || ""),
  }));
  state.reviewRedlines = result.redline_edits || [];
  state.exportClauseDecisions = defaultExportClauseDecisions(state.reviewClauses, state.reviewRedlines);
  state.redlineTemplateSelections = defaultRedlineTemplateSelections(state.reviewRedlines);
  state.reviewSourceText = reviewedText || studioNdaText.value.trim();
  state.clauseJumpIndexes = {};
  state.selectedReviewClauseId =
    state.reviewClauses.find((clause) => !clausePasses(clause))?.id || state.reviewClauses[0]?.id || null;
  renderStudioResult(result);
  updateExportButtonState();
}

function renderStudioEmpty() {
  state.latestReviewResult = null;
  showStudioSourceEditor();
  studioMatchSummary.textContent = `0/${getClauseTotal()}`;
  studioResultMark.textContent = "-";
  studioResultMark.className = "";
  studioOverallTitle.textContent = "Awaiting review";
  studioResultMeta.textContent = "No hard-clause review has run yet.";
  studioDetailPanel.innerHTML = `
    <p class="eyebrow">selected clause</p>
    <p>No review yet.</p>
  `;
  updateExportButtonState();
  renderStudioClauseLane();
}

function updateExportButtonState() {
  const canExport = state.reviewClauses.length && (studioNdaText.value.trim() || state.reviewSourceText.trim());
  if (studioExportButton) {
    studioExportButton.disabled = !canExport;
  }
  if (!studioSendButton) return;
  const canSend = Boolean(canExport && state.selectedMatter?.id && MatterUtils.canSendRedline(state.selectedMatter));
  studioSendButton.disabled = !canSend;
  if (!canSend) {
    pendingReviewSendMatterId = null;
    studioSendButton.textContent = "Send Redline";
  }
}

function renderStudioResult(result) {
  const clauses = result.clauses || [];
  renderStudioSummary(clauses);
  renderStudioClauseLane();
  renderStudioDetail();
  renderStudioDocumentHighlights();
}

function renderStudioSummary(clauses) {
  const passedCount = clauses.filter((clause) => clauseStatus(clause).passes).length;
  const failedCount = clauses.filter((clause) => clauseStatus(clause).needsReview).length;
  studioMatchSummary.textContent = `${passedCount}/${getClauseTotal(clauses)}`;
  studioResultMark.textContent = failedCount ? "CHECK" : "PASS";
  studioResultMark.className = failedCount ? "check" : "pass";
  studioOverallTitle.textContent = failedCount ? "Does not meet requirements" : "Meets requirements";
  const warning = reviewWarningSummary();
  studioResultMeta.textContent = warning || (failedCount
    ? `${failedCount} hard ${failedCount === 1 ? "clause needs" : "clauses need"} checking.`
    : "All hard clauses are currently satisfied.");
}

function reviewWarningSummary() {
  const trust = state.latestReviewResult?.evidence_trust;
  if (trust?.status === "flagged") {
    const firstError = Array.isArray(trust.errors) && trust.errors.length ? ` ${trust.errors[0]}` : "";
    return `Evidence provenance warning.${firstError}`;
  }
  const warnings = Array.isArray(state.latestReviewResult?.review_warnings) ? state.latestReviewResult.review_warnings : [];
  const firstWarning = warnings.find((warning) => warning?.message);
  return firstWarning?.message || "";
}

function renderClauseExportState(clause, canDecide, included) {
  if (!canDecide) return "";
  return `<span class="studio-export-state ${included ? "included" : "ignored"}">${included ? "Included in export" : "Ignored in export"}</span>`;
}

function renderClauseExportControls(clause, canDecide, included) {
  if (!canDecide) return "";
  return `
    <span class="studio-export-controls" role="group" aria-label="${escapeHtml(clause.name)} export decision">
      <button class="export-choice ${included ? "active" : ""}" type="button" data-export-clause-id="${escapeHtml(clause.id)}" data-export-decision="include" aria-pressed="${included ? "true" : "false"}">Include</button>
      <button class="export-choice ${!included ? "active" : ""}" type="button" data-export-clause-id="${escapeHtml(clause.id)}" data-export-decision="ignore" aria-pressed="${!included ? "true" : "false"}">Ignore</button>
    </span>
  `;
}

function getClauseTotal(clauses = []) {
  return clauses.length || state.playbookClauses.length || 0;
}

function hasReviewResults() {
  return state.reviewClauses.length > 0;
}

function defaultExportClauseDecisions(clauses, redlines) {
  const clausesWithRedlines = new Set((redlines || []).map((edit) => edit.clause_id).filter(Boolean));
  return Object.fromEntries((clauses || []).map((clause) => [
    clause.id,
    clausesWithRedlines.has(clause.id),
  ]));
}

function defaultRedlineTemplateSelections(redlines) {
  const selections = {};
  (redlines || []).forEach((edit) => {
    const selected = (edit.template_options || []).find((option) => option.selected) || (edit.template_options || [])[0];
    if (selected?.id) selections[edit.id] = selected.id;
  });
  return selections;
}

function clauseExportIncluded(clauseId) {
  return state.exportClauseDecisions[clauseId] !== false;
}

function redlineExportIncluded(edit) {
  return clauseExportIncluded(edit.clause_id);
}

function effectiveReviewRedlines() {
  return state.reviewRedlines
    .filter(redlineExportIncluded)
    .map(applyTemplateSelectionToRedline);
}

function applyTemplateSelectionToRedline(edit) {
  const selectedOptionId = state.redlineTemplateSelections[edit.id];
  const selectedOption = (edit.template_options || []).find((option) => option.id === selectedOptionId);
  if (!selectedOption) return { ...edit };

  const nextEdit = {
    ...edit,
    template_options: (edit.template_options || []).map((option) => ({
      ...option,
      selected: option.id === selectedOption.id,
    })),
  };
  const selectedReplacement = selectedOption.replacement_text || selectedOption.text || "";
  const selectedInsert = selectedOption.insert_text || selectedOption.replacement_text || selectedOption.text || "";
  if (edit.action === REDLINE_INSERT_AFTER_PARAGRAPH) {
    if (selectedInsert.trim()) nextEdit.insert_text = selectedInsert;
    if (selectedReplacement.trim()) nextEdit.replacement_text = selectedReplacement;
  } else if (selectedReplacement.trim()) {
    nextEdit.replacement_text = selectedReplacement;
  }
  if (Array.isArray(selectedOption.inline_diff_operations)) {
    nextEdit.inline_diff_operations = selectedOption.inline_diff_operations;
  } else {
    delete nextEdit.inline_diff_operations;
  }
  return nextEdit;
}

function getDisplayClauses() {
  return hasReviewResults()
    ? state.reviewClauses
    : state.playbookClauses.map((clause) => ({ ...clause, status: "idle" }));
}

function getSelectedReviewClause() {
  return state.reviewClauses.find((item) => item.id === state.selectedReviewClauseId);
}

function getSelectedRedlineEdits() {
  return effectiveReviewRedlines().filter((edit) => edit.clause_id === state.selectedReviewClauseId);
}

function bindClauseSelection(container, selector, datasetKey) {
  container.querySelectorAll(selector).forEach((item) => {
    item.addEventListener("click", () => {
      selectReviewClause(item.dataset[datasetKey], { jump: true });
    });
  });
}

function bindExportDecisionControls(container) {
  container.querySelectorAll("[data-export-clause-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setClauseExportDecision(button.dataset.exportClauseId, button.dataset.exportDecision === "include");
    });
  });
}

function setClauseExportDecision(clauseId, included) {
  state.exportClauseDecisions[clauseId] = included;
  state.selectedReviewClauseId = clauseId;
  renderStudioResult({ clauses: state.reviewClauses });
  if (included) {
    const clause = state.reviewClauses.find((item) => item.id === clauseId);
    requestAnimationFrame(() => jumpToClauseSource(clause));
  }
  updateExportButtonState();
}

function setRedlineTemplateSelection(editId, optionId) {
  state.redlineTemplateSelections[editId] = optionId;
  renderStudioResult({ clauses: state.reviewClauses });
}

function renderStudioClauseLane() {
  if (!studioClauseLane) return;

  const sourceClauses = getDisplayClauses();

  if (!sourceClauses.length) {
    studioClauseLane.innerHTML = '<div class="studio-empty">Loading clauses</div>';
    return;
  }

  studioClauseLane.innerHTML = sourceClauses
    .map((clause, index) => {
      const selected = clause.id === state.selectedReviewClauseId ? "selected" : "";
      const status = clauseStatus(clause);
      const redlineCount = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id).length;
      const canDecide = hasReviewResults() && redlineCount > 0;
      const included = clauseExportIncluded(clause.id);
      const exportState = renderClauseExportState(clause, canDecide, included);
      const exportControls = renderClauseExportControls(clause, canDecide, included);
      const finding = hasReviewResults()
        ? `<span class="studio-clause-finding">${escapeHtml(clause.reason || clause.finding || "Clause review available.")}</span>`
        : "";
      const pill = hasReviewResults()
        ? `<strong class="studio-issue-pill ${status.tone}">${status.pillLabel}</strong>`
        : "";
      const selectable = hasReviewResults()
        ? `
          <button class="studio-clause-select" type="button" data-studio-lane-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}">
            <span class="studio-clause-dot ${status.dotTone}"></span>
            <strong class="studio-clause-number">${index + 1}</strong>
            <span class="studio-clause-title">${escapeHtml(clause.name)}</span>
            ${pill}
            ${finding}
            ${exportState}
          </button>
        `
        : `
          <div class="studio-clause-select">
            <span class="studio-clause-dot ${status.dotTone}"></span>
            <strong class="studio-clause-number">${index + 1}</strong>
            <span class="studio-clause-title">${escapeHtml(clause.name)}</span>
          </div>
        `;
      return `
        <article class="studio-clause-item ${selected} ${status.tone}">
          ${selectable}
          ${exportControls}
        </article>
      `;
    })
    .join("");

  bindClauseSelection(studioClauseLane, "[data-studio-lane-id]", "studioLaneId");
  bindExportDecisionControls(studioClauseLane);
}

function renderStudioDetail() {
  const clause = getSelectedReviewClause();
  if (!clause) return;
  const status = clauseStatus(clause);
  const whyText = clause.reason || clause.finding || "Clause review available.";
  const excerpt = renderEvidenceBlock(clause);
  const fixBlock = status.needsReview && clause.what_to_fix
    ? `<div class="studio-detail-block fix-block"><small>What to fix</small><p>${escapeHtml(clause.what_to_fix)}</p></div>`
    : "";
  const rationaleBlock = clause.rationale
    ? `<div class="studio-detail-block rationale-block"><small>Playbook rationale</small><p>${escapeHtml(clause.rationale)}</p></div>`
    : "";
  const evidenceGuidanceBlock = clause.evidence_guidance
    ? `<div class="studio-detail-block evidence-guidance-block"><small>Evidence guidance</small><p>${escapeHtml(clause.evidence_guidance)}</p></div>`
    : "";
  const redlineEdits = getSelectedRedlineEdits();
  const selectedClauseRedlineCount = state.reviewRedlines.filter((edit) => edit.clause_id === clause.id).length;
  const exportDecisionBlock = selectedClauseRedlineCount
    ? `
      <div class="studio-detail-block export-decision-block">
        <small>Export decision</small>
        <div class="detail-export-controls" role="group" aria-label="${escapeHtml(clause.name)} export decision">
          <button class="export-choice ${clauseExportIncluded(clause.id) ? "active" : ""}" type="button" data-export-clause-id="${escapeHtml(clause.id)}" data-export-decision="include" aria-pressed="${clauseExportIncluded(clause.id) ? "true" : "false"}">Include redline</button>
          <button class="export-choice ${!clauseExportIncluded(clause.id) ? "active" : ""}" type="button" data-export-clause-id="${escapeHtml(clause.id)}" data-export-decision="ignore" aria-pressed="${!clauseExportIncluded(clause.id) ? "true" : "false"}">Ignore</button>
        </div>
      </div>
    `
    : "";
  const redlineBlock = redlineEdits.length
    ? `
      <div class="studio-detail-block redline-block">
        <small>Proposed redline</small>
        ${redlineEdits.map(renderDetailRedlineEdit).join("")}
      </div>
    `
    : "";
  const acceptableLanguage = clause.acceptable_language
    ? `<div class="studio-detail-block"><small>Acceptable language</small><p>${escapeHtml(clause.acceptable_language)}</p></div>`
    : "";
  studioDetailPanel.innerHTML = `
    <p class="eyebrow">selected clause</p>
    <div class="studio-detail-heading">
      <h3>${escapeHtml(clause.name)}</h3>
      <span class="status ${status.tone}">${escapeHtml(status.pillLabel)}</span>
    </div>
    <div class="studio-detail-stack">
      <div class="studio-detail-block requirement-block">
        <small>Requirement</small>
        <p>${escapeHtml(clause.requirement)}</p>
      </div>
      ${excerpt}
      <div class="studio-detail-block issue-block ${escapeHtml(status.tone)}">
        <small>Issue type</small>
        <p>${escapeHtml(status.issueLabel)}</p>
      </div>
      <div class="studio-detail-block finding-block">
        <small>Why</small>
        <p>${escapeHtml(whyText)}</p>
      </div>
      ${rationaleBlock}
      ${evidenceGuidanceBlock}
      ${fixBlock}
      ${exportDecisionBlock}
      ${redlineBlock}
      <div class="studio-detail-block">
        <small>Backend result</small>
        <p>${escapeHtml(status.resultLabel)}</p>
      </div>
      ${acceptableLanguage}
    </div>
  `;
  bindExportDecisionControls(studioDetailPanel);
  bindTemplateOptionControls(studioDetailPanel);
}

function renderEvidenceBlock(clause) {
  const evidenceParagraphs = Array.isArray(clause.evidence_paragraphs)
    ? clause.evidence_paragraphs.filter((paragraph) => paragraph && paragraph.text)
    : [];
  if (evidenceParagraphs.length) {
    return `
      <div class="studio-detail-block studio-detail-evidence">
        <small>Evidence</small>
        <div class="evidence-list">
          ${evidenceParagraphs.map((paragraph, index) => {
            const paragraphNumber = paragraph.index || paragraph.source_index || index + 1;
            return `
              <figure class="evidence-item">
                <figcaption>Paragraph ${escapeHtml(paragraphNumber)}</figcaption>
                <p>${escapeHtml(paragraph.text)}</p>
              </figure>
            `;
          }).join("")}
        </div>
      </div>
    `;
  }
  if (clause.matched_text) {
    return `<div class="studio-detail-block studio-detail-evidence"><small>Evidence</small><p>${escapeHtml(clause.matched_text)}</p></div>`;
  }
  return '<div class="studio-detail-block studio-detail-evidence muted"><small>Evidence</small><p>No matching paragraph identified.</p></div>';
}

function renderDetailRedlineEdit(edit) {
  const replacement = renderRedlineReplacement(edit, "p");
  const original = edit.action === "insert_after_paragraph"
    ? renderRedlineAnchor(edit)
    : `<p class="redline-original">${escapeHtml(edit.original_text || "")}</p>`;
  return `
    <div class="detail-redline-edit">
      <span class="redline-label">${escapeHtml(redlineActionLabel(edit))}</span>
      ${original}
      ${replacement}
      ${renderRedlineTemplateOptions(edit)}
    </div>
  `;
}

function renderRedlineAnchor(edit) {
  const paragraphLabel = edit.paragraph_index ? `Paragraph ${edit.paragraph_index}` : "Selected paragraph";
  const anchorText = edit.anchor_text || "";
  return `
    <p class="redline-anchor">
      <strong>${escapeHtml(paragraphLabel)}</strong>
      ${escapeHtml(anchorText)}
    </p>
  `;
}

function renderRedlineTemplateOptions(edit) {
  const options = edit.template_options || [];
  if (options.length <= 1) return "";

  return `
    <div class="redline-options">
      <span class="redline-options-title">Jurisdiction options</span>
      ${options.map((option) => `
        <button class="redline-option ${option.selected ? "selected" : ""}" type="button" data-redline-edit-id="${escapeHtml(edit.id)}" data-redline-option-id="${escapeHtml(option.id || "")}" aria-pressed="${option.selected ? "true" : "false"}">
          <strong>${escapeHtml(option.label || "Option")}${option.selected ? " - Default" : ""}</strong>
          <span>${escapeHtml(option.text || option.replacement_text || option.insert_text || "")}</span>
        </button>
      `).join("")}
    </div>
  `;
}

function bindTemplateOptionControls(container) {
  container.querySelectorAll("[data-redline-edit-id][data-redline-option-id]").forEach((button) => {
    button.addEventListener("click", () => {
      setRedlineTemplateSelection(button.dataset.redlineEditId, button.dataset.redlineOptionId);
    });
  });
}

function renderStudioDocumentHighlights() {
  if (!studioDocumentRender) return;

  if (!state.reviewClauses.length) {
    showStudioSourceEditor();
    return;
  }

  if (!state.reviewParagraphs.length) {
    showStudioSourceEditor();
    return;
  }
  const viewMode = state.documentViewMode || VIEW_MODE_REDLINE;
  studioDocumentRender.innerHTML = renderReviewDocument({
    clauses: state.reviewClauses,
    originalParagraphs: state.reviewOriginalParagraphs,
    paragraphs: state.reviewParagraphs,
    redlines: effectiveReviewRedlines(),
    selectedClauseId: state.selectedReviewClauseId,
    viewMode,
  });

  studioDocumentRender.querySelectorAll("[data-clause-ids]").forEach((paragraph) => {
    paragraph.addEventListener("click", (event) => {
      if (event.target.closest("[data-editable-paragraph-id]")) return;
      const clauseId = paragraph.dataset.clauseIds.split(" ").filter(Boolean)[0];
      if (clauseId) selectReviewClause(clauseId, { jump: false });
    });
  });
  bindViewerParagraphEditing();

  showStudioDocumentRender();
}
