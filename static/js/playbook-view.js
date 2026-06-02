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
        ${checkerVisibilityPanel(clause)}
        ${sharedContextControls(clause)}

        <section class="admin-rules">
          <h3>Raw Engine Rules</h3>
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
        <section class="admin-special">
          <h3>Checker Logic Visibility</h3>
          <p class="admin-muted">The backend now evaluates survival language with document structure, explicit references, and deterministic concepts.</p>
          <dl class="admin-logic-list">
            <div><dt>Duration parser</dt><dd>Reads numeric and mixed word durations such as three (3) years and 3 (three) years.</dd></div>
            <div><dt>Reference resolver</dt><dd>When survival points to clauses or articles, the checker resolves those targets before deciding pass or check.</dd></div>
            <div><dt>Concept classifier</dt><dd>Referenced targets are tagged for confidentiality, use restriction, permitted disclosure, return/destruction, and carve-out concepts.</dd></div>
            <div><dt>Checker output</dt><dd>When references are used, the review result includes term_survival_analysis for audit.</dd></div>
          </dl>
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

  function checkerVisibilityPanel(clause) {
    const visibility = checkerVisibilityForClause(clause);
    const readingOrder = [
      ["1", "Review state", "Start with review_state to see whether the checker produced pass, review, or check and whether it blocks send."],
      ["2", "Reason codes", "Read reason_code and reason_codes to identify the machine-readable cause without parsing prose."],
      ["3", "Structured evidence", "Inspect structured_evidence for paragraph IDs, matched terms, signal type, rule bucket, and counted flag."],
      ["4", "Analysis object", `Open ${visibility.output_field} for checker-specific signals and intermediate classifications.`],
      ["5", "Audit trace", "Use audit_trace to follow the normalized input, evidence, signal, analysis, and final-decision steps."],
    ];
    const statusCards = [
      ["Pass", visibility.pass],
      ["Review", visibility.review],
      ["Check", visibility.check],
    ]
      .map(([label, text]) => `
        <article class="admin-decision-card ${escapeHtml(label.toLowerCase())}">
          <strong>${escapeHtml(label)}</strong>
          <p>${escapeHtml(text)}</p>
        </article>
      `)
      .join("");
    const signalBuckets = visibility.signal_buckets
      .map((bucket) => `
        <article>
          <h4>${escapeHtml(bucket.label)}</h4>
          <p>${escapeHtml(bucket.description)}</p>
          <div class="admin-chip-row">
            ${bucket.fields.map((field) => `<span class="admin-chip">${escapeHtml(field)}</span>`).join("")}
          </div>
        </article>
      `)
      .join("");
    const analysisFields = visibility.analysis_fields
      .map((field) => `<span class="admin-chip">${escapeHtml(field)}</span>`)
      .join("");
    const outputRows = [
      ["Checker module", visibility.module],
      ["Analysis purpose", visibility.purpose],
      ["Primary inputs", visibility.inputs],
      ["Audit output", visibility.output_field],
      ["Review state", "Every checker emits review_state to normalize pass, review, and check routing, send blocking, and redline requirements."],
      ["Reason codes", "Every checker emits reason_code and reason_codes so audit, admin views, and AI handoff can classify the decision without parsing prose."],
      ["Structured evidence", "Every checker emits structured_evidence records with paragraph provenance, matched terms, signal type, rule bucket, counted flag, and reason."],
      ["Audit trace", "Every checker emits audit_trace with normalized decision steps, evidence summary, analysis outputs, and analysis signals."],
      ["Redline behavior", visibility.redline_behavior],
      ["Human-review boundary", visibility.boundary],
    ]
      .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`)
      .join("");
    const readingOrderItems = readingOrder
      .map(([number, label, detail]) => `
        <li>
          <span>${escapeHtml(number)}</span>
          <strong>${escapeHtml(label)}</strong>
          <p>${escapeHtml(detail)}</p>
        </li>
      `)
      .join("");
    const reasonCodeGroups = renderReasonCodeGroups(visibility.reason_codes || {});
    const hardeningGuards = renderHardeningGuards(visibility.hardening_guards || []);

    return `
      <section class="admin-special checker-visibility">
        <h3>Decision Logic Visibility</h3>
        <p class="admin-muted">Each checker is explained with the same analysis model: purpose, inputs, pass/review/check decision path, audit output, redline behavior, and human-review boundary.</p>
        <div class="admin-decision-grid">${statusCards}</div>
        <section class="admin-signal-section">
          <h4>Audit reading order</h4>
          <ol class="admin-reading-order">${readingOrderItems}</ol>
        </section>
        <section class="admin-signal-section">
          <h4>Reason-code taxonomy</h4>
          <div class="admin-code-groups">${reasonCodeGroups}</div>
        </section>
        <section class="admin-signal-section">
          <h4>Signal buckets</h4>
          <div class="admin-signal-grid">${signalBuckets}</div>
        </section>
        <section class="admin-signal-section">
          <h4>Hardening guards</h4>
          <div class="admin-hardening-list">${hardeningGuards}</div>
        </section>
        <section class="admin-signal-section">
          <h4>Analysis output fields</h4>
          <div class="admin-chip-row">${analysisFields || '<span class="admin-muted">No checker-specific analysis object yet</span>'}</div>
        </section>
        <dl class="admin-logic-list">${outputRows}</dl>
      </section>
    `;
  }

  function engineRulesForClause(clause) {
    const visibility = checkerVisibilityForClause(clause);
    const rules = {
      decision_model: {
        pass: visibility.pass,
        review: visibility.review,
        check: visibility.check,
      },
      backend_module: visibility.module,
      analysis_purpose: visibility.purpose,
      primary_inputs: visibility.inputs,
      analysis_output_field: visibility.output_field,
      review_state_field: "review_state",
      reason_code_field: "reason_code",
      reason_codes_field: "reason_codes",
      structured_evidence_field: "structured_evidence",
      audit_trace_field: "audit_trace",
      analysis_fields: visibility.analysis_fields,
      signal_buckets: visibility.signal_buckets,
      reason_code_taxonomy: visibility.reason_codes || {},
      hardening_guards: visibility.hardening_guards || [],
      audit_reading_order: [
        "review_state",
        "reason_code",
        "reason_codes",
        "structured_evidence",
        visibility.output_field,
        "audit_trace",
      ],
      redline_behavior: visibility.redline_behavior,
      human_review_boundary: visibility.boundary,
      taxonomy_groups: clause.taxonomy_groups || [],
      search_terms: clause.search_terms || [],
      shared_review_context: {
        contract_structure_map: true,
        reference_resolver: true,
        concept_classifier: conceptUsageForClause(clause).concepts,
        output_field: "structure_context",
      },
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
        parser_accepts: ["three (3) years", "3 (three) years", "36 months"],
        permitted_longer_survival_terms: clause.longer_survival_carve_out_terms || [],
      };
      rules.reference_resolution = {
        uses_contract_structure_map: true,
        resolves_clause_article_section_targets: true,
        output_field: "term_survival_analysis",
      };
      rules.concept_classifier = {
        ordinary_confidentiality_concepts: [
          "confidential_information_definition",
          "confidentiality_obligation",
          "use_restriction",
          "permitted_disclosure",
          "return_or_destruction",
        ],
      };
      rules.check_terms = clause.indefinite_terms || [];
    }
    return JSON.stringify(rules, null, 2);
  }

  function renderReasonCodeGroups(groups) {
    const entries = Object.entries(groups).filter(([, codes]) => Array.isArray(codes) && codes.length);
    if (!entries.length) {
      return '<p class="admin-muted">No clause-specific reason-code examples configured.</p>';
    }
    return entries
      .map(([label, codes]) => `
        <article class="admin-code-group ${escapeHtml(label)}">
          <strong>${escapeHtml(label)}</strong>
          <div class="admin-chip-row">
            ${codes.map((code) => `<span class="admin-chip">${escapeHtml(code)}</span>`).join("")}
          </div>
        </article>
      `)
      .join("");
  }

  function renderHardeningGuards(guards) {
    if (!guards.length) {
      return '<p class="admin-muted">No clause-specific hardening guard examples configured.</p>';
    }
    return guards
      .map((guard) => `
        <article>
          <strong>${escapeHtml(guard.label || "Guard")}</strong>
          <p>${escapeHtml(guard.detail || "")}</p>
          ${guard.example ? `<code>${escapeHtml(guard.example)}</code>` : ""}
        </article>
      `)
      .join("");
  }

  function checkerVisibilityForClause(clause) {
    const shared = {
      signatures: {
        module: "nda_automation/checks/signatures.py",
        purpose: "Confirm the NDA has an execution block that appears complete enough for both sides to sign.",
        inputs: "Execution-block text, party markers, title/capacity markers, date markers, and the signature redline template.",
        pass: "Execution block appears to include both parties, titles or capacities, and dates.",
        review: "Signatures will be handled as a separate execution-block model rather than expanded in this pass.",
        check: "Execution block is missing or incomplete under the current marker-count checker.",
        output_field: "structure_context",
        analysis_fields: [],
        reason_codes: {
          pass: ["complete_execution_block"],
          review: ["semantic_confidence_below_threshold"],
          check: ["incomplete_execution_block", "missing_execution_block"],
        },
        hardening_guards: [
          {
            label: "Separate execution model",
            detail: "Signature parsing is intentionally not mixed into the legal-concept checker upgrades.",
            example: "Party/title/date marker counting remains a separate execution-block task.",
          },
        ],
        redline_behavior: "Missing or incomplete execution blocks can insert or replace the signature template.",
        boundary: "Current logic is structural marker detection; party/signatory parsing is intentionally separate.",
        signal_buckets: [
          {
            label: "Execution markers",
            description: "Current checker counts party, title, and date markers.",
            fields: ["party marker", "title marker", "date marker"],
          },
        ],
      },
    };
    const visibility = {
      mutuality: {
        module: "nda_automation/checks/mutuality.py",
        purpose: "Confirm the NDA creates reciprocal confidentiality obligations for both parties, not a one-way receiving-party model.",
        inputs: "Reviewed paragraphs, role definitions, reciprocal-obligation terms, one-way terms, and shared structure context.",
        pass: "Strong reciprocal obligation language binds both parties as disclosing and receiving parties.",
        review: "Role definitions or title-only mutuality labels exist without a clear reciprocal obligation.",
        check: "One-way or unilateral language fixes only one side as receiving party or otherwise fails mutuality.",
        output_field: "mutuality_analysis",
        analysis_fields: [
          "strong_mutuality_paragraph_ids",
          "weak_mutuality_paragraph_ids",
          "role_definition_paragraph_ids",
          "one_way_paragraph_ids",
        ],
        reason_codes: {
          pass: ["mutuality_obligation_found"],
          review: ["role_definitions_without_operational_mutuality", "weak_mutuality_signal"],
          check: ["one_way_mutuality_language", "missing_mutuality_obligation"],
        },
        hardening_guards: [
          {
            label: "Title-only guard",
            detail: "A mutual NDA title or mutuality label alone is review evidence, not pass evidence.",
            example: "Mutual Non-Disclosure Agreement",
          },
          {
            label: "One-way override",
            detail: "Unilateral or recipient-only language forces check even when other mutuality words appear.",
            example: "only the Receiving Party",
          },
        ],
        redline_behavior: "Check decisions use the mutuality redline template; review decisions do not auto-redline.",
        boundary: "Definitions alone are not enough for pass; they are treated as human-review evidence.",
        signal_buckets: [
          {
            label: "Strong evidence",
            description: "Operative reciprocal obligation language.",
            fields: ["each party", "both parties", "reciprocal confidentiality", "Disclosing Party and Receiving Party"],
          },
          {
            label: "Review evidence",
            description: "Signals that imply mutuality but do not prove operative obligations.",
            fields: ["title-only mutual NDA", "role definitions", "weak reciprocal labels"],
          },
          {
            label: "Check evidence",
            description: "One-way or non-mutual terms.",
            fields: clause.one_way_terms || [],
          },
        ],
      },
      confidential_information: {
        module: "nda_automation/checks/confidential_information.py",
        purpose: "Confirm the Confidential Information definition is broad enough and does not add exclusions that weaken protection.",
        inputs: "Definition paragraphs, required category terms, exclusion terms, usage-right signals, and shared structure context.",
        pass: "A broad Confidential Information definition covers enough required categories with no extra exclusions.",
        review: "A broad general definition or separate usage-right language may be acceptable but needs human review.",
        check: "The definition is missing, too narrow, or includes prohibited carve-outs.",
        output_field: "confidential_information_analysis",
        analysis_fields: [
          "definition_paragraph_ids",
          "coverage_hits",
          "explicit_exclusion_paragraph_ids",
          "usage_right_review_paragraph_ids",
        ],
        reason_codes: {
          pass: ["broad_confidential_information_definition"],
          review: ["broad_definition_needs_category_review", "usage_right_language_needs_review"],
          check: [
            "missing_confidential_information_definition",
            "narrow_confidential_information_definition",
            "problematic_confidential_information_exclusion",
          ],
        },
        hardening_guards: [
          {
            label: "Qualified independent development",
            detail: "Independent-development carve-outs can pass when the no-use/no-reference qualification is attached before or after the carve-out.",
            example: "without use of or reference to Confidential Information, is independently developed",
          },
          {
            label: "Detached qualification guard",
            detail: "A separate no-use phrase elsewhere in the paragraph does not cure an unqualified independent-development exclusion.",
            example: "without use of Confidential Information, or independently developed information",
          },
          {
            label: "Usage-right review",
            detail: "Usage-right language outside the definition is review, not automatic pass or delete.",
            example: "may use residual knowledge",
          },
        ],
        redline_behavior: "Missing/narrow definitions use broadening language; exclusion-based checks use exclusions cleanup language.",
        boundary: "General broad wording can be reviewed instead of failed when categories are implicit.",
        signal_buckets: [
          {
            label: "Definition breadth",
            description: "Definition anchors and required category hits.",
            fields: ["Confidential Information means", ...(clause.definition_categories || [])],
          },
          {
            label: "Review evidence",
            description: "Usage-right language outside the definition can weaken protection.",
            fields: ["residual knowledge", "reverse engineering", "independent development", "broad general definition"],
          },
          {
            label: "Check evidence",
            description: "Explicit extra exclusions or narrow definitions.",
            fields: clause.problematic_exclusion_terms || [],
          },
        ],
      },
      governing_law: {
        module: "nda_automation/checks/governing_law.py",
        purpose: "Confirm the contract has a clear governing-law value and that the value is in the approved operating set.",
        inputs: "Governing-law candidates, approved jurisdiction aliases, placeholder signals, conflict signals, and shared structure context.",
        pass: "The governing-law value resolves to an approved law.",
        review: "The governing-law value is placeholder, heading-only, conditional, unresolved, or conflicts with another governing-law sentence.",
        check: "A clear governing-law clause names a non-approved jurisdiction.",
        output_field: "governing_law_analysis",
        analysis_fields: [
          "approved_paragraph_ids",
          "unclear_paragraph_ids",
          "unapproved_paragraph_ids",
          "heading_only_paragraph_ids",
          "candidate_records",
        ],
        reason_codes: {
          pass: ["approved_governing_law"],
          review: ["unclear_governing_law", "governing_law_heading_only"],
          check: ["missing_governing_law", "unapproved_governing_law"],
        },
        hardening_guards: [
          {
            label: "Candidate-value extraction",
            detail: "The checker reads the governing-law value, not only the presence of approved-law words somewhere nearby.",
            example: "governed by the laws of California",
          },
          {
            label: "Conflict review",
            detail: "Approved and non-approved governing-law statements in the same document are escalated to review.",
            example: "England and Wales plus California",
          },
          {
            label: "Placeholder review",
            detail: "Placeholders and party-selected laws are not treated as approved values.",
            example: "laws of [jurisdiction]",
          },
        ],
        redline_behavior: "Non-approved laws generate replacement options; review decisions wait for human confirmation.",
        boundary: "Approved law references outside the governing-law value do not rescue a non-approved governing law.",
        signal_buckets: [
          {
            label: "Approved values",
            description: "Accepted governing-law jurisdictions and aliases.",
            fields: clause.approved_laws || [],
          },
          {
            label: "Review evidence",
            description: "Unresolved or conflicting governing-law statements.",
            fields: ["[jurisdiction]", "mutually agreed", "conditional approved law", "heading only"],
          },
          {
            label: "Check evidence",
            description: "Clear non-approved governing-law values.",
            fields: ["California", "France", "New York", "other non-approved jurisdiction"],
          },
        ],
      },
      term_and_survival: {
        module: "nda_automation/checks/term_and_survival.py",
        purpose: "Confirm ordinary confidentiality term or survival is time-limited while preserving approved longer carve-outs.",
        inputs: "Duration expressions, indefinite-survival terms, longer-survival carve-outs, resolved references, concept tags, and shared structure context.",
        pass: "Ordinary confidentiality term or survival period is fixed and within the configured cap.",
        review: "Survival uses cross-references that are unresolved or do not clearly classify as ordinary confidentiality.",
        check: "The term is missing, over-cap, or indefinite outside allowed carve-outs.",
        output_field: "term_survival_analysis",
        analysis_fields: [
          "references",
          "confidentiality_reference_count",
          "unresolved_reference_count",
          "ordinary_confidentiality_concepts",
        ],
        reason_codes: {
          pass: ["term_survival_within_cap", "resolved_survival_reference_within_cap"],
          review: ["unresolved_survival_reference", "survival_reference_scope_unclear"],
          check: ["missing_term_or_survival", "term_survival_over_cap", "indefinite_survival"],
        },
        hardening_guards: [
          {
            label: "Real structure references",
            detail: "Survival references are resolved against the current document structure, not assumed article numbering.",
            example: "Articles 2, 3, 4 and 5 survive",
          },
          {
            label: "Duration scope guard",
            detail: "Unrelated survival durations do not satisfy ordinary confidentiality survival.",
            example: "Claims survive for three years",
          },
          {
            label: "Allowed carve-out scope",
            detail: "Above-cap or indefinite survival only passes when it is tied to configured carve-outs.",
            example: "trade secrets survive for so long as they remain trade secrets",
          },
        ],
        redline_behavior: "Missing or deficient terms generate a term/survival redline; review decisions do not auto-redline.",
        boundary: "Cross-referenced survival is checked against actual resolved sections, not assumed article numbers.",
        signal_buckets: [
          {
            label: "Duration evidence",
            description: "Fixed periods parsed from words, digits, and mixed forms.",
            fields: ["three (3) years", "3 (three) years", "36 months"],
          },
          {
            label: "Reference evidence",
            description: "Resolved article, section, and clause targets are classified by concept.",
            fields: ["reference_resolver", "concept_classifier", "contract_structure"],
          },
          {
            label: "Check evidence",
            description: "Over-cap and indefinite terms outside allowed carve-outs.",
            fields: clause.indefinite_terms || [],
          },
        ],
      },
      non_circumvention: {
        module: "nda_automation/checks/non_circumvention.py",
        purpose: "Confirm the NDA does not contain prohibited non-circumvention, non-solicit, direct-dealing, substitute-purpose, or exclusivity restraints.",
        inputs: "Prohibited restraint terms, review-only commercial signals, lawful-circumvention guards, negated references, and shared structure context.",
        pass: "No operative non-circumvention, introduced-party non-solicit, substitute-purpose, or exclusivity restriction appears.",
        review: "Soft introduced-party, substitute-purpose, or exclusivity language appears without a clear operative restriction.",
        check: "Definite non-circumvention, non-solicit, direct-dealing, substitute-purpose, or exclusivity restriction appears.",
        output_field: "non_circumvention_analysis",
        analysis_fields: [
          "prohibited_paragraph_ids",
          "review_paragraph_ids",
          "lawful_circumvention_paragraph_ids",
          "negated_reference_paragraph_ids",
          "signal_records",
        ],
        reason_codes: {
          pass: [
            "no_non_circumvention_restriction",
            "negated_non_circumvention_reference",
            "lawful_circumvention_reference_ignored",
          ],
          review: ["possible_non_circumvention_restriction"],
          check: ["prohibited_non_circumvention_restriction"],
        },
        hardening_guards: [
          {
            label: "Lawful-circumvention guard",
            detail: "Language about not circumventing law is ignored instead of deleted.",
            example: "Nothing requires a party to circumvent applicable law",
          },
          {
            label: "Negated-reference guard",
            detail: "Clauses saying the agreement does not create non-solicit or exclusivity obligations pass.",
            example: "may not include non-solicitation obligations",
          },
          {
            label: "Soft commercial signal review",
            detail: "Introduced-party or future-exclusivity references without operative restriction require human review.",
            example: "may discuss exclusivity later",
          },
        ],
        redline_behavior: "Definite restrictions generate delete redlines; review decisions do not auto-delete.",
        boundary: "Lawful-circumvention references and negated references are not treated as violations.",
        signal_buckets: [
          {
            label: "Hard restrictions",
            description: "Operative commercial restraints beyond confidentiality.",
            fields: clause.search_terms || [],
          },
          {
            label: "Review evidence",
            description: "Adjacent commercial language that may or may not restrict dealings.",
            fields: ["introduced parties", "future exclusivity discussion", "substitute transaction reference"],
          },
          {
            label: "Pass guards",
            description: "References explicitly outside the prohibited-restriction scope.",
            fields: ["circumvent applicable law", "does not include non-circumvention", "no non-solicitation obligation"],
          },
        ],
      },
    };
    return visibility[clause.id] || shared[clause.id] || {
      module: "nda_automation/checks/",
      purpose: "Apply the configured playbook rule to the reviewed document text.",
      inputs: "Reviewed paragraphs, playbook search terms, semantic signals, and shared structure context.",
      pass: "Clause satisfies the preferred standard position.",
      review: "The checker marks ambiguous evidence for human review.",
      check: clause.type === "prohibited"
        ? "Clause appears when the playbook says it must be absent."
        : "Clause is missing, deficient, unclear, or off-standard.",
      output_field: "structure_context",
      analysis_fields: [],
      reason_codes: {
        pass: ["pass_evidence_found"],
        review: ["unclear_or_ambiguous"],
        check: ["missing_required_clause", "present_but_wrong"],
      },
      hardening_guards: [],
      redline_behavior: "Redline behavior follows the clause registry.",
      boundary: "No clause-specific visibility model configured.",
      signal_buckets: [
        {
          label: "Configured terms",
          description: "Playbook search terms and semantic signals.",
          fields: [...(clause.search_terms || []), ...(clause.semantic_signals || [])],
        },
      ],
    };
  }

  function sharedContextControls(clause) {
    const usage = conceptUsageForClause(clause);
    const chips = usage.concepts
      .map((concept) => `<span class="admin-chip">${escapeHtml(concept)}</span>`)
      .join("");
    return `
      <section class="admin-special">
        <h3>Shared Structure Layer</h3>
        <p class="admin-muted">This checker receives the same Contract Structure Map, Reference Resolver, and Concept Classifier context as the rest of the review engine.</p>
        <dl class="admin-logic-list">
          <div><dt>Structure use</dt><dd>${escapeHtml(usage.structure)}</dd></div>
          <div><dt>Reference use</dt><dd>${escapeHtml(usage.references)}</dd></div>
          <div><dt>Concept use</dt><dd>${escapeHtml(usage.summary)}</dd></div>
          <div><dt>Audit output</dt><dd>Every result includes structure_context with concepts, matching sections, and reference count.</dd></div>
        </dl>
        <div class="admin-chip-row">${chips || '<span class="admin-muted">No clause-specific concepts configured</span>'}</div>
      </section>
    `;
  }

  function conceptUsageForClause(clause) {
    const usage = {
      confidential_information: {
        concepts: ["confidential_information_definition", "confidential_information_exclusion"],
        references: "Reference count is surfaced for audit; definition and exclusion checks use concept-classified paragraphs.",
        structure: "Uses detected sections to show where definitions and exclusions live.",
        summary: "Finds definition and exclusion concepts before applying category breadth and carve-out rules.",
      },
      governing_law: {
        concepts: ["governing_law"],
        references: "Reference count is surfaced for audit; governing law does not usually depend on cross-references.",
        structure: "Uses detected sections to isolate governing-law headings and paragraphs.",
        summary: "Finds governing-law concept paragraphs before applying approved-law checks.",
      },
      mutuality: {
        concepts: ["mutuality", "party_role_definition", "confidentiality_obligation"],
        references: "Reference count is surfaced for audit; mutuality primarily depends on party-role language.",
        structure: "Uses detected sections to show where mutuality and role definitions appear.",
        summary: "Classifies mutuality, party-role definitions, and confidentiality obligations for audit.",
      },
      non_circumvention: {
        concepts: ["non_circumvention"],
        references: "Reference count is surfaced for audit; prohibited business-restraint language is checked directly.",
        structure: "Uses detected sections to show where non-circumvention concepts appear.",
        summary: "Finds non-circumvention concept paragraphs before applying lawful-circumvention guards.",
      },
      signatures: {
        concepts: ["execution"],
        references: "Reference count is surfaced for audit; signature checks do not depend on cross-references.",
        structure: "Uses detected sections to show where execution material appears.",
        summary: "Finds execution concepts before counting party, title, and date markers.",
      },
      term_and_survival: {
        concepts: ["term_or_survival", "trade_secret_or_legal_carveout"],
        references: "Uses resolved references to inspect what referenced clauses or articles actually are.",
        structure: "Uses detected sections to inspect survival targets and carve-out context.",
        summary: "Classifies survival and permitted longer-survival carve-outs, then adds term_survival_analysis when references are used.",
      },
    };
    return usage[clause.id] || {
      concepts: [],
      references: "Reference count is surfaced for audit.",
      structure: "Uses detected sections for checker context.",
      summary: "No clause-specific concept usage configured.",
    };
  }

  function textInput(label, name, value) {
    return `
      <label class="admin-field">
        <span>${escapeHtml(label)}</span>
        <input name="${escapeHtml(name)}" type="text" value="${escapeHtml(value || "")}">
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
