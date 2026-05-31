function createPlaybookController({ state, playbookList, clauseDetail, renderStudioEmpty }) {
  async function loadPlaybook() {
    playbookList.innerHTML = '<div class="playbook-loading">Loading clauses</div>';
    clauseDetail.innerHTML = '<div class="detail-empty">Loading playbook</div>';

    try {
      const response = await fetch("/playbook");
      const playbook = await response.json();
      if (!response.ok) throw new Error(playbook.error || "Playbook could not load");

      state.playbookClauses = playbook.clauses || [];
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
        return `
          <button class="playbook-row ${selected}" type="button" data-clause-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}">
            <span class="clause-number">${position}</span>
            <span>
              <strong>${escapeHtml(clause.name)}</strong>
              <small>${escapeHtml(clause.type)}</small>
            </span>
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
    const clause = state.playbookClauses.find((item) => item.id === state.selectedClauseId);
    if (!clause) {
      clauseDetail.innerHTML = '<div class="detail-empty">No clause selected</div>';
      return;
    }

    const lawChips = (clause.approved_laws || [])
      .map((law) => `<span>${escapeHtml(law)}</span>`)
      .join("");
    const maxTermYears = clause.max_term_years || clause.term_years;
    const termYears = maxTermYears
      ? `<div class="fact-box"><small>Term cap</small><strong>Up to ${escapeHtml(maxTermYears)} years</strong></div>`
      : "";
    const approvedLaws = lawChips
      ? `<div class="law-strip">${lawChips}</div>`
      : "";

    clauseDetail.innerHTML = `
      <div class="detail-header">
        <div>
          <p class="eyebrow">clause ${escapeHtml(clause.id)}</p>
          <h2>${escapeHtml(clause.name)}</h2>
        </div>
        <span class="policy-chip ${escapeHtml(clause.type)}">${escapeHtml(clause.type)}</span>
      </div>

      <div class="requirement-panel">
        <small>Requirement</small>
        <p>${escapeHtml(clause.requirement)}</p>
      </div>

      <div class="detail-grid">
        <div class="fact-box">
          <small>Checker outcome</small>
          <strong>${clause.type === "prohibited" ? "Must be absent" : "Must be present"}</strong>
        </div>
        <div class="fact-box">
          <small>Source</small>
          <strong>playbook.json</strong>
        </div>
        ${termYears}
      </div>

      ${approvedLaws}
    `;
  }

  return { loadPlaybook, renderClauseDetail, renderPlaybookList };
}
