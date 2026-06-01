function createPlaybookController({ state, playbookList, clauseDetail, renderStudioEmpty }) {
  async function loadPlaybook() {
    playbookList.innerHTML = '<div class="playbook-loading">Loading clauses</div>';
    clauseDetail.innerHTML = '<div class="detail-empty">Loading playbook</div>';

    try {
      const response = await fetch("/playbook");
      const playbook = await response.json();
      if (!response.ok) throw new Error(playbook.error || "Playbook could not load");

      state.playbook = clonePlaybook(playbook);
      state.savedPlaybook = clonePlaybook(playbook);
      state.playbookClauses = state.playbook.clauses || [];
      state.selectedClauseId = state.playbookClauses[0]?.id || null;
      renderStudioEmpty();
      renderPlaybookList();
      renderClauseDetail();
    } catch (error) {
      playbookList.innerHTML = `<div class="playbook-loading">${escapeHtml(error.message)}</div>`;
      clauseDetail.innerHTML = '<div class="detail-empty">Playbook unavailable</div>';
    }
  }

  function renderPlaybookList() {
    playbookList.innerHTML = state.playbookClauses
      .map((clause, index) => {
        const selected = clause.id === state.selectedClauseId ? "selected active" : "";
        const position = String(index + 1).padStart(2, "0");
        const draft = hasClauseDraft(clause.id) ? '<em>Draft</em>' : "";
        return `
          <button class="playbook-row ${selected}" type="button" data-clause-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}">
            <span class="clause-number">${position}</span>
            <span>
              <strong>${escapeHtml(clause.name)}</strong>
              <small>${escapeHtml(stanceLabel(clause))}</small>
            </span>
            ${draft}
          </button>
        `;
      })
      .join("");

    playbookList.querySelectorAll("[data-clause-id]").forEach((row) => {
      row.addEventListener("click", () => {
        state.selectedClauseId = row.dataset.clauseId;
        renderPlaybookList();
        renderClauseDetail();
      });
    });
  }

  function renderClauseDetail() {
    const clause = selectedClause();
    if (!clause) {
      clauseDetail.innerHTML = '<div class="detail-empty">No clause selected</div>';
      return;
    }

    clauseDetail.innerHTML = `
      <form class="playbook-editor" id="playbookEditor">
        <div class="admin-head">
          <div>
            <p class="eyebrow">clause ${escapeHtml(clause.id)}</p>
            <h2>Edit Clause: ${escapeHtml(clause.name)}</h2>
          </div>
          <span class="policy-chip ${escapeHtml(clause.type)}">${escapeHtml(stanceLabel(clause))}</span>
        </div>

        <div class="admin-grid">
          ${textInput("Clause Name", "name", clause.name)}
        </div>

        <fieldset class="admin-fieldset">
          <legend>Stance</legend>
          <label>
            <input type="radio" name="type" value="required" ${clause.type === "prohibited" ? "" : "checked"}>
            <span>Required - Check if absent or deficient</span>
          </label>
          <label>
            <input type="radio" name="type" value="prohibited" ${clause.type === "prohibited" ? "checked" : ""}>
            <span>Prohibited - Check if present</span>
          </label>
        </fieldset>

        ${textArea("Preferred Standard Position", "preferred_position", preferredPosition(clause), 3)}
        ${textArea("Check Trigger Position", "check_trigger", checkTrigger(clause), 3)}
        ${textArea("Suggested Redline / Counter-language", "redline_template", clause.redline_template || clause.acceptable_language || "", 4)}

        ${specialControls(clause)}

        <section class="admin-rules">
          <h3>Engine Rules</h3>
          <pre>${escapeHtml(engineRulesForClause(clause))}</pre>
        </section>

        <section class="admin-rules diff">
          <h3>Draft Modifications Diff</h3>
          <pre id="playbookDraftDiff">${escapeHtml(diffForClause(clause.id) || "No unsaved changes.")}</pre>
        </section>

        <div class="admin-actions">
          <span class="admin-save-status" id="playbookSaveStatus" aria-live="polite"></span>
          <button class="secondary" type="button" id="discardPlaybookDraft" ${hasClauseDraft(clause.id) ? "" : "disabled"}>Discard Draft</button>
          <button type="submit" id="savePlaybookButton" ${hasAnyDraft() ? "" : "disabled"}>Commit & Save Playbook</button>
        </div>
      </form>
    `;

    const editor = clauseDetail.querySelector("#playbookEditor");
    editor.addEventListener("input", handleEditorInput);
    editor.addEventListener("submit", savePlaybook);
    clauseDetail.querySelector("#discardPlaybookDraft").addEventListener("click", discardSelectedDraft);
    setupSpecialControls(clause);
  }

  function handleEditorInput() {
    const clause = selectedClause();
    if (!clause) return;
    const form = clauseDetail.querySelector("#playbookEditor");
    const data = new FormData(form);
    clause.name = String(data.get("name") || "").trim() || clause.name;
    clause.type = data.get("type") === "prohibited" ? "prohibited" : "required";
    clause.preferred_position = String(data.get("preferred_position") || "").trim();
    clause.check_trigger = String(data.get("check_trigger") || "").trim();
    clause.redline_template = String(data.get("redline_template") || "").trim();
    clause.standard_exclusions_template = String(data.get("standard_exclusions_template") || "").trim();
    if (clause.id === "term_and_survival") {
      clause.max_term_years = Math.max(1, Number.parseInt(data.get("max_term_years"), 10) || 5);
    }
    renderDraftState();
  }

  function renderDraftState() {
    const clause = selectedClause();
    const diff = diffForClause(clause.id);
    const diffNode = clauseDetail.querySelector("#playbookDraftDiff");
    const discard = clauseDetail.querySelector("#discardPlaybookDraft");
    const save = clauseDetail.querySelector("#savePlaybookButton");
    if (diffNode) diffNode.textContent = diff || "No unsaved changes.";
    if (discard) discard.disabled = !diff;
    if (save) save.disabled = !hasAnyDraft();
    renderPlaybookList();
  }

  function setupSpecialControls(clause) {
    if (clause.id !== "term_and_survival") return;
    const addButton = clauseDetail.querySelector("#addSurvivalCarveOut");
    const input = clauseDetail.querySelector("#survivalCarveOutInput");
    if (addButton && input) {
      addButton.addEventListener("click", () => {
        const value = input.value.trim();
        if (!value) return;
        clause.longer_survival_carve_out_terms = dedupeList([
          ...(clause.longer_survival_carve_out_terms || []),
          value,
        ]);
        input.value = "";
        renderClauseDetail();
      });
      input.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        addButton.click();
      });
    }
    clauseDetail.querySelectorAll("[data-remove-survival-carveout]").forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.removeSurvivalCarveout;
        clause.longer_survival_carve_out_terms = (clause.longer_survival_carve_out_terms || [])
          .filter((item) => item !== value);
        renderClauseDetail();
      });
    });
  }

  async function savePlaybook(event) {
    event.preventDefault();
    const status = clauseDetail.querySelector("#playbookSaveStatus");
    const saveButton = clauseDetail.querySelector("#savePlaybookButton");
    if (status) status.textContent = "Saving playbook...";
    if (saveButton) saveButton.disabled = true;

    try {
      const response = await fetch("/api/playbook", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ playbook: state.playbook }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Playbook could not be saved");
      state.playbook = clonePlaybook(payload.playbook);
      state.savedPlaybook = clonePlaybook(payload.playbook);
      state.playbookClauses = state.playbook.clauses || [];
      if (status) status.textContent = "Playbook saved.";
      renderPlaybookList();
      renderClauseDetail();
    } catch (error) {
      if (status) status.textContent = error.message;
      if (saveButton) saveButton.disabled = !hasAnyDraft();
    }
  }

  function discardSelectedDraft() {
    const clause = selectedClause();
    if (!clause) return;
    const saved = savedClause(clause.id);
    if (!saved) return;
    Object.keys(clause).forEach((key) => delete clause[key]);
    Object.assign(clause, clonePlaybook(saved));
    renderPlaybookList();
    renderClauseDetail();
  }

  function selectedClause() {
    return state.playbookClauses.find((item) => item.id === state.selectedClauseId);
  }

  function savedClause(clauseId) {
    return (state.savedPlaybook?.clauses || []).find((item) => item.id === clauseId);
  }

  function hasClauseDraft(clauseId) {
    return Boolean(diffForClause(clauseId));
  }

  function hasAnyDraft() {
    return state.playbookClauses.some((clause) => hasClauseDraft(clause.id));
  }

  function diffForClause(clauseId) {
    const clause = state.playbookClauses.find((item) => item.id === clauseId);
    const saved = savedClause(clauseId);
    if (!clause || !saved) return "";
    const fields = [
      "name",
      "type",
      "preferred_position",
      "check_trigger",
      "redline_template",
      "standard_exclusions_template",
      "max_term_years",
      "longer_survival_carve_out_terms",
    ];
    return fields
      .filter((field) => stableJson(clause[field]) !== stableJson(saved[field]))
      .map((field) => `${field}:\n- ${formatDiffValue(saved[field])}\n+ ${formatDiffValue(clause[field])}`)
      .join("\n\n");
  }

  function specialControls(clause) {
    if (clause.id === "confidential_information") {
      return `
        ${textArea("Standard Exclusions Language", "standard_exclusions_template", clause.standard_exclusions_template || "", 3)}
      `;
    }
    if (clause.id === "term_and_survival") {
      const carveOuts = (clause.longer_survival_carve_out_terms || [])
        .map((item) => `
          <button class="admin-chip removable" type="button" data-remove-survival-carveout="${escapeHtml(item)}">
            ${escapeHtml(item)} <span aria-hidden="true">x</span>
          </button>
        `)
        .join("");
      const indefiniteTerms = (clause.indefinite_terms || [])
        .map((item) => `<span class="admin-chip">${escapeHtml(item)}</span>`)
        .join("");
      return `
        <label class="admin-field compact">
          <span>Ordinary Confidentiality Cap (years)</span>
          <input name="max_term_years" type="number" min="1" max="25" step="1" value="${escapeHtml(clause.max_term_years || 5)}">
        </label>
        <section class="admin-special">
          <h3>Permitted Perpetual / Longer Survival Carve-outs</h3>
          <p class="admin-muted">Only these carve-out terms can justify indefinite, perpetual, or above-cap survival. Ordinary confidentiality still has to stay within the cap.</p>
          <div class="admin-chip-row">${carveOuts || '<span class="admin-muted">No longer-survival carve-outs configured</span>'}</div>
          <div class="admin-inline-add">
            <input id="survivalCarveOutInput" type="text" placeholder="Add carve-out term">
            <button class="secondary" id="addSurvivalCarveOut" type="button">Add</button>
          </div>
        </section>
        <section class="admin-special">
          <h3>Perpetual / Indefinite Trigger Terms</h3>
          <p class="admin-muted">When these terms appear outside the permitted carve-out context, the clause is checked.</p>
          <div class="admin-chip-row">${indefiniteTerms}</div>
        </section>
      `;
    }
    if (clause.id === "governing_law") {
      return `
        <section class="admin-special">
          <h3>Approved Governing Laws</h3>
          <div class="admin-chip-row">
            ${(clause.approved_laws || []).map((law) => `<span class="admin-chip">${escapeHtml(law)}</span>`).join("")}
          </div>
        </section>
      `;
    }
    return "";
  }

  function engineRulesForClause(clause) {
    const rules = {
      pass_check_model: {
        pass: "Clause satisfies the preferred standard position.",
        check: clause.type === "prohibited"
          ? "Clause appears when the playbook says it must be absent."
          : "Clause is missing, deficient, unclear, or off-standard.",
      },
      taxonomy_groups: clause.taxonomy_groups || [],
      search_terms: clause.search_terms || [],
      semantic_signals: clause.semantic_signals || [],
    };
    if (clause.id === "mutuality") {
      rules.check_terms = clause.one_way_terms || [];
    }
    if (clause.id === "confidential_information") {
      rules.required_categories = clause.definition_categories || [];
      rules.problematic_exclusions = clause.problematic_exclusion_terms || [];
    }
    if (clause.id === "term_and_survival") {
      rules.duration = {
        ordinary_confidentiality_cap_years: clause.max_term_years || 5,
        permitted_longer_survival_terms: clause.longer_survival_carve_out_terms || [],
      };
      rules.check_terms = clause.indefinite_terms || [];
    }
    return JSON.stringify(rules, null, 2);
  }

  function textInput(label, name, value) {
    return `
      <label class="admin-field">
        <span>${escapeHtml(label)}</span>
        <input name="${escapeHtml(name)}" type="text" value="${escapeHtml(value || "")}">
      </label>
    `;
  }

  function selectInput(label, name, value, options) {
    return `
      <label class="admin-field">
        <span>${escapeHtml(label)}</span>
        <select name="${escapeHtml(name)}">
          ${options.map((option) => `<option value="${escapeHtml(option)}" ${option === value ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
        </select>
      </label>
    `;
  }

  function textArea(label, name, value, rows) {
    return `
      <label class="admin-field">
        <span>${escapeHtml(label)}</span>
        <textarea name="${escapeHtml(name)}" rows="${rows}">${escapeHtml(value || "")}</textarea>
      </label>
    `;
  }

  function preferredPosition(clause) {
    return clause.preferred_position || clause.acceptable_language || clause.requirement || "";
  }

  function checkTrigger(clause) {
    return clause.check_trigger || clause.evidence_guidance || "";
  }

  function stanceLabel(clause) {
    return clause.type === "prohibited" ? "Prohibited" : "Required";
  }

  function stableJson(value) {
    return JSON.stringify(value === undefined ? null : value);
  }

  function formatDiffValue(value) {
    if (Array.isArray(value)) return `[${value.join(", ")}]`;
    if (typeof value === "boolean") return value ? "true" : "false";
    if (value === undefined || value === null || value === "") return "(blank)";
    return String(value);
  }

  function clonePlaybook(value) {
    return JSON.parse(JSON.stringify(value || {}));
  }

  function dedupeList(values) {
    const seen = new Set();
    return values.filter((value) => {
      const key = String(value).trim();
      const normalized = key.toLowerCase();
      if (!key || seen.has(normalized)) return false;
      seen.add(normalized);
      return true;
    });
  }

  return { loadPlaybook, renderClauseDetail, renderPlaybookList };
}
